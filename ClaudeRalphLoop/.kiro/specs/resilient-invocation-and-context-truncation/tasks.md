# Implementation Plan: Resilient Invocation and Context Truncation

## Overview

Convert the design in `design.md` into a series of prompts for a code-generation
LLM that will implement each step with incremental progress. Each prompt builds
on the previous ones and ends with wiring things together so no hanging or
orphaned code remains. Focus is on writing, modifying, or testing code.

The plan is phased so the two capabilities can be verified independently and
then integrated:

- **Phase 1 — Config additivity.** Add `Config.max_context_file_bytes` with a
  65536 default, locked in by Property 23 before anything reads it.
- **Phase 2 — Per-file truncation helpers.** Implement the pure
  `_truncate_to_codepoint_boundary` and the side-effecting
  `_inline_one_context_file`, thread `max_file_bytes` through
  `inline_context_files` and `compose_context`, and cover them with unit tests
  plus Properties 21 and 22.
- **Phase 3 — Wire truncation into the loop.** Pass
  `config.max_context_file_bytes` from `_run_loop` into `compose_context` so
  the new cap actually reaches the Kiro CLI prompt.
- **Phase 4 — Graceful Invocation_Error handler.** Add
  `_handle_invocation_error` in `cli.py`, replace the per-iteration
  `except Exception: ... break` with the handler + `continue` pattern, and
  cover the observable behaviour with unit tests and Property 20.
- **Phase 5 — Integration test + Property 24 harness.** Drive the full
  `_run_loop` through the graceful-continue scenario from the design's
  Testing Strategy section, and enumerate the four scripted scenarios for
  Property 24.
- **Phase 6 — Verification.** Run the full test suite, confirm no regressions,
  and confirm coverage for the new helpers.

## Tasks

- [x] 1. Add `max_context_file_bytes` to `Config`
  - [x] 1.1 Add field `max_context_file_bytes: int = Field(default=65536, gt=0)` in the "Context" group of `ralph_loop/models.py::Config`, just below `max_context_tokens`, and update the adjacent comment to mention R2.1/R2.2
    - No other model changes; the field is strictly additive so existing `ralph.config.json` files continue to load
    - _Validates: R2.1, R2.2, R3.4_
    - _Exercises: Property 23_

  - [x] 1.2 Add Property 23 to `tests/test_config_loader_properties.py` covering: field absent → `max_context_file_bytes == 65536`; field present with any positive int `v` → roundtrips to `v`; non-positive int or non-int → `ValidationError`
    - Use a Hypothesis `fixed_dictionaries` strategy for valid existing configs and compose with `st.integers(min_value=1, max_value=10 * 1024 * 1024)` for the present case
    - **Property 23: Config field is additive with default 65536**
    - **Validates: R2.1, R2.2, R3.4**

- [x] 2. Implement per-file truncation helpers in `ralph_loop/context.py`
  - [x] 2.1 Add `TRUNCATION_MARKER_TEMPLATE = "[truncated: {original_bytes} bytes, showing first {cap} bytes]"` and the pure `_truncate_to_codepoint_boundary(data: bytes, cap: int) -> bytes` helper matching the design pseudocode (continuation-byte rewind, expected-length check for 2/3/4-byte leaders, malformed-lead fallback)
    - Function must satisfy: `0 <= len(retained) <= cap`; when `len(data) > cap`, `len(retained) >= cap - 3`; `retained.decode("utf-8")` succeeds strictly; idempotent on any output already produced
    - _Validates: R2.4, R2.8_
    - _Exercises: Property 21_

  - [x] 2.2 Add `_inline_one_context_file(rel_path: str, abs_path: Path, *, max_file_bytes: int) -> str` using `_truncate_to_codepoint_boundary` and emitting a `structlog.get_logger().warning("context_file_truncated", ...)` Truncation_Event with keys `path`, `original_bytes`, `retained_bytes`, `cap_bytes`
    - Under-cap and at-cap paths return `"### File: <rel_path>\n\n<body>"` verbatim with no marker
    - Over-cap path returns `"### File: <rel_path>\n\n<body>\n<marker>"` with the marker on its own line
    - Decode with `errors="replace"` to match existing `inline_context_files` behaviour
    - _Validates: R2.3, R2.4, R2.5, R2.6, R2.7_
    - _Exercises: Property 22_

  - [x] 2.3 Refactor `inline_context_files` to accept keyword-only `max_file_bytes: int = 65536` and delegate to `_inline_one_context_file` for each existing `candidate.is_file()` path, preserving the current missing-file warning and `(inlined_text, missing_paths)` return contract
    - Keep the default at 65536 so in-tree test callers that pass no cap keep working
    - _Validates: R2.3, R2.4_
    - _Exercises: Properties 21, 22_

  - [x] 2.4 Extend `compose_context` with keyword-only `max_file_bytes: int = 65536` and pass it through to `inline_context_files`
    - Keep the R6.7 whole-window brief truncation logic untouched; per-file truncation runs before assembly and is independent of the token-budget fallback
    - _Validates: R2.3, R2.4, R2.10_
    - _Exercises: Properties 21, 22_

  - [x] 2.5 Add unit tests for `_truncate_to_codepoint_boundary` in `tests/test_context_composer.py` covering: `cap=0` returns `b""`; `cap >= len(data)` returns `data` unchanged; ASCII over-cap truncates to exactly `cap`; 2-byte `"é"` mid-codepoint cut rewinds 1 byte; 3-byte `"€"` mid-codepoint cut rewinds 1 or 2 bytes; 4-byte `"🎉"` mid-codepoint cut rewinds up to 3 bytes; pure-ASCII input never rewinds; pure-multi-byte input always lands on a codepoint boundary; continuation byte at position 0 returns empty bytes
    - _Validates: R2.4, R2.8_
    - _Exercises: Property 21_

  - [x] 2.6 Add unit tests for `_inline_one_context_file` in `tests/test_context_composer.py` covering: file smaller than cap emits verbatim with no marker; file exactly at cap emits verbatim with no marker (boundary is inclusive); file larger than cap ends with `"\n[truncated: N bytes, showing first CAP bytes]"`; marker matches the design's `TRUNCATION_MARKER_REGEX`; WARNING log captured via `structlog.testing.capture_logs` carries `event="context_file_truncated"` and keys `path`, `original_bytes`, `retained_bytes`, `cap_bytes`
    - _Validates: R2.3, R2.4, R2.5, R2.6, R2.7_
    - _Exercises: Property 22_

  - [x] 2.7 Add Property 21 to `tests/test_context_composer_properties.py` using `st.binary(max_size=2048)` and `st.integers(min_value=1, max_value=1024)`, asserting all three predicates (byte bounds, strict UTF-8 decode, idempotence)
    - **Property 21: Truncation is idempotent and byte-bounded**
    - **Validates: R2.4, R2.8**

  - [x] 2.8 Add Property 22 to `tests/test_context_composer_properties.py` writing the random payload to a tempfile, calling `_inline_one_context_file`, and asserting `TRUNCATION_MARKER_REGEX = r"\n\[truncated: (\d+) bytes, showing first (\d+) bytes\]\Z"` matches iff `N > cap`, with captured groups equal to `(str(N), str(cap))`
    - **Property 22: Truncation marker invariant**
    - **Validates: R2.4, R2.5, R2.7**

- [x] 3. Wire truncation into the iteration loop
  - [x] 3.1 Update the `compose_context(...)` call site in `ralph_loop/cli.py::_run_loop` to pass `max_file_bytes=config.max_context_file_bytes` alongside the existing `max_tokens=config.max_context_tokens`
    - No other call-site changes in the loop body
    - _Validates: R2.3, R2.10_
    - _Exercises: (integration)_

- [x] 4. Add the graceful Invocation_Error handler in `ralph_loop/cli.py`
  - [x] 4.1 Add module-level `CHUNK_LIMIT_SUBSTRING = "chunk exceed the limit"`, the `_excerpt(s: str, limit: int = 2000) -> str` helper, and the `_handle_invocation_error(*, exc, task, persona_name, tasks, tasks_path) -> None` helper matching the design pseudocode
    - Import `CheckResult` from `ralph_loop.models` and `KiroInvocationTimeout` from `ralph_loop.kiro` as needed
    - Build a synthetic `CheckResult(type="shell", name="kiro_invocation", verdict="fail", output=f"invocation_error: {type(exc).__name__}: {exc}", duration_ms=0, timed_out=isinstance(exc, KiroInvocationTimeout))` and pass it to `status_after_validation` to get `(new_status, new_retry)`
    - Persist via `_update_task(tasks, task.id, {"status": new_status, "retry_count": new_retry}, tasks_path)` BEFORE emitting the `structlog.get_logger().error("iteration_invocation_error", ...)` record so a logger failure cannot lose the status update
    - Compute `chunk_limit_detected` by lower-casing the concatenation of `str(exc)`, `getattr(exc, "stderr", "") or ""`, and `getattr(exc, "stdout", "") or ""` and testing for `CHUNK_LIMIT_SUBSTRING`
    - Structured log keys (all always present, `""` when the source attribute is missing): `task_id`, `persona_name`, `exception_type`, `exception_message`, `stdout_excerpt`, `stderr_excerpt`, `chunk_limit_detected`, `failure_mode` ("chunk_limit" or "generic"), `new_status`, `new_retry_count`
    - _Validates: R1.1, R1.2, R1.4, R1.5, R1.7, R1.8_
    - _Exercises: Property 20_

  - [x] 4.2 Replace the existing `except Exception as exc:` block around `invoker.invoke` in `_run_loop` with: an explicit `except (KeyboardInterrupt, SystemExit): raise` guard first, then `except Exception as exc: _handle_invocation_error(...); tasks = _load_tasks(tasks_path); continue`
    - Remove the `exit_code = EXIT_INVOCATION_ERROR` assignment and the `break` that previously terminated the loop on a caught exception
    - The Validator, Task_Creation_Processor, and Git commit blocks must stay below the `continue` so they are skipped for the failing iteration (R1.9)
    - Keep the outer top-level `except Exception` in the `run` Click command unchanged so uncaught startup errors still write `logs/crash.log`
    - _Validates: R1.3, R1.6, R1.9_
    - _Exercises: Property 20, Property 24_

  - [x] 4.3 Add unit tests for `_handle_invocation_error` in a new `tests/test_cli_invocation_error_handler.py` covering: chunk-limit detected in `str(exc)` only; chunk-limit detected in `exc.stderr` only; chunk-limit detected in `exc.stdout` only; uppercase `"CHUNK EXCEED THE LIMIT"` is detected case-insensitively; no chunk-limit marker → `failure_mode="generic"`; `KiroInvocationTimeout` instance → synthetic `CheckResult.timed_out == True`; `RuntimeError` with no `stdout`/`stderr` attrs → `stdout_excerpt == ""` and `stderr_excerpt == ""` with no `AttributeError`; persistence ordering (inject a structlog processor that raises on first call and assert `tasks.json` on disk still reflects the new status/retry); `_excerpt` round-trips strings under 2000 chars and truncates longer strings with the `"...[truncated N chars]"` suffix
    - _Validates: R1.1, R1.2, R1.4, R1.5, R1.7_
    - _Exercises: Property 20_

  - [x] 4.4 Add Property 20 to a new `tests/test_cli_properties.py` using a Hypothesis strategy that generates a random `Task` (any `retry_count`, any non-terminal status) and a random exception (chosen from `RuntimeError`, `ValueError`, `KiroInvocationTimeout`, `subprocess.CalledProcessError`) with random `str(exc)`, `stderr`, and `stdout`, writing a tempdir `tasks.json`, invoking `_handle_invocation_error`, reloading via `_load_tasks`, and asserting the reloaded task's `(status, retry_count)` equals `status_after_validation(task, [failing_check]).` The oracle is `status_after_validation` itself
    - **Property 20: Invocation_Error converges to Iteration_Failure via the same rule as a failing check**
    - **Validates: R1.1, R1.5, R3.2, R3.3**

- [x] 5. Integration test for graceful-continue and Property 24 scripted harness
  - [x] 5.1 Add integration test `test_graceful_invocation_error_continues_loop` in `tests/test_cli.py` walking the three-task scenario from the design's Testing Strategy section: `tasks.json` with A, B, C (all `status=pending`, priority 1); stub `KiroInvoker` raises `RuntimeError("chunk exceed the limit")` on A's first invocation and returns a passing `KiroInvocationResult` on B and C; stub `Validator` returns `overall="pass"` for B and C. Assert: `_run_loop` returns via `termination_decision` (success or blocked depending on retry-cap exhaustion), never `EXIT_INVOCATION_ERROR`; final `tasks.json` has `A.status=="failing"` with `A.retry_count==1`, `B.status=="passing"`, `C.status=="passing"`; exactly one `iteration_invocation_error` log record captured for A with `chunk_limit_detected=True` and `failure_mode="chunk_limit"`; Validator call count on A == 0; Git manager iteration-commit call count for A's iteration == 0; invoker call count on A == 1 (no same-iteration retry)
    - _Validates: R1.3, R1.6, R1.9, R3.1_
    - _Exercises: Property 24 (enumerated scenarios)_

  - [x] 5.2 Add the Property 24 scripted harness in `tests/test_cli_invocation_error_handler.py` (alongside the unit tests from 4.3) enumerating the four scenarios from the design: (a) all-pass sequence returns `EXIT_SUCCESS`; (b) handler + pass mix returns the termination-decision exit code, never `EXIT_INVOCATION_ERROR`; (c) repeated handler fires on the same task until `retry_count == Config.max_retries_per_task` transitions it to `stuck` via the existing rule and the loop exits with `EXIT_BLOCKED`; (d) handler fire on one task followed by a passing iteration on a different task returns the termination-decision exit code. For each scenario assert the returned exit code equals `termination_decision(final_tasks).exit_code` (or the appropriate `BudgetTracker` code)
    - **Property 24: Loop exit code is termination-decision-driven, not Invocation_Error-driven**
    - **Validates: R1.3, R1.6, R3.1**

- [x] 6. Verification checkpoint
  - [x] 6.1 Run `python -m pytest tests/ -q` and confirm all previously-passing tests plus the new tests pass. If any previously-passing test fails, fix production code rather than the test. Confirm new coverage spans `_truncate_to_codepoint_boundary`, `_inline_one_context_file`, and `_handle_invocation_error` (100 percent line and branch for those three functions)
    - Ensure all tests pass, ask the user if questions arise.
    - _Validates: R3.5_
    - _Exercises: regression_


## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP.
  Required tasks alone (1.1, 2.1, 2.2, 2.3, 2.4, 3.1, 4.1, 4.2, 6.1) deliver
  the production code and the verification gate; the starred tasks add the
  unit tests, property-based tests, integration test, and final commit.
- Each task references the specific requirements it validates and, where
  applicable, the correctness property it exercises.
- Properties 21–23 are implemented as Hypothesis tests with
  `max_examples=200`; Property 24 is implemented as a scripted unit harness
  enumerating the four scenarios from the design's Testing Strategy section.
- The feature preserves all existing correctness properties from the
  ralph-loop design (Properties 1–19); no edits to other specs' design docs
  are required.
