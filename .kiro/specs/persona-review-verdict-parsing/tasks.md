# Implementation Plan

This bugfix follows the bug-condition methodology: first **explore** the bug
with a property-based test that encodes `C(X)` — the test must FAIL on unfixed
code to prove the bug exists; then **fix** the defect by consolidating the two
`_extract_first_json_object` helpers behind the new `ralph_loop.json_extract`
module; then **preserve** existing behavior with property-based tests pinned
against a copy of the naive helper plus per-scenario regression tests; and
finally **verify** by running the full suite and confirming Property 16 now
passes on fixed code. Only the commit task (5) is optional; every other task
is required.

- [x] 1. Write bug-condition exploration test (Property 16)
  - **Property 1: Bug Condition** — PersonaReviewVerdict recovered from any legitimate envelope
  - **CRITICAL**: This test MUST FAIL on unfixed code — failure confirms the bug exists. Do NOT attempt to repair the production code or the test when it fails; the failure is the signal we need.
  - **NOTE**: This test encodes the expected behavior from design.md §Correctness Properties (Property 16). When it passes after the fix is applied in Task 2, it validates the fix.
  - Add Property 16 to `tests/test_validator_properties.py` using the exact `@given` strategy in design.md §Testing Strategy §Fix Checking: `verdict` sampled from `["pass", "fail"]`, `rationale` drawn from `st.text` (excluding surrogates and NUL), `use_fence` boolean, `lang_tag` sampled from `["", "json", "JSON"]`, `prepend_tool_use` boolean, plus `leading_prose` and `trailing_prose` bounded text.
  - Assemble stdout per design.md §1.1–1.5: optional leading prose, optional tool-use envelope, body either bare or wrapped in a ```` ```<lang_tag> ``` ```` fence, optional trailing prose.
  - Call `extract_validating_object(stdout, PersonaReviewVerdict)` (the helper does not yet exist — import will fail on unfixed code, which is also an acceptable failure mode; update the import after Task 2.1 lands).
  - Assert the returned verdict matches the embedded payload (`recovered.verdict == verdict` and `recovered.rationale == rationale`).
  - Run the test on UNFIXED code: **expected outcome = FAIL** (import error or `AssertionError`). Capture the exact Hypothesis counterexample (e.g. `use_fence=True, rationale='missing } in expression'`) and record it via `updatePBTStatus` with status `failed` and the failing example.
  - Mark this task complete when the test is written, has been run on unfixed code, and the counterexample has been recorded.
  - _Validates: 2.1, 2.2, 2.3, 2.4, 2.5_
  - _Exercises: Property 16_

- [x] 2. Implement the fix: extract the JSON helper and migrate both call sites

  - [x] 2.1 Create `ralph_loop/json_extract.py` with the shared helper API
    - Create a new module `ralph_loop/json_extract.py` exposing three public functions exactly as specified in design.md §Fix Implementation §Changes Required:
      - `strip_markdown_fences(text: str) -> str` — one-pass removal of a leading ```` ```<lang> ```` line and a trailing ```` ``` ```` line; no-op when either fence is absent; no regex.
      - `iter_balanced_json_objects(text: str) -> Iterator[str]` — yields every top-level balanced `{...}` substring in order of appearance, tracking JSON string state (`"..."`) and escape state (`\"`, `\\`); O(n) in `len(text)`; never raises; silently skips unbalanced spans by advancing past the unmatched opening brace.
      - `extract_validating_object(text: str, model: type[T]) -> T | None` where `T: BaseModel` — applies `strip_markdown_fences`, iterates candidates from `iter_balanced_json_objects`, returns the first one that `json.loads` + `model.model_validate` successfully; returns `None` when no candidate validates.
    - Implementation MUST follow the pseudocode in design.md §Fix Implementation exactly (depth counter with `in_string` and `escape_next` flags; fence stripping via split-drop-rejoin on `"\n"`; `extract_validating_object` catches `(json.JSONDecodeError, pydantic.ValidationError)` only).
    - Add no new third-party dependencies; use only `json`, `pydantic`, and `typing`.
    - _Validates: 2.1, 2.2, 2.3, 2.4, 2.5_
    - _Exercises: Property 16, Property 17_
    - _Bug_Condition: `isBugCondition(stdout)` from design.md §Bug Details_
    - _Expected_Behavior: For all stdout satisfying `isBugCondition`, `extract_validating_object(stdout, PersonaReviewVerdict)` returns the embedded payload (design.md §Correctness Properties Property 16)_

  - [x] 2.2 Migrate `ralph_loop/validator.py::_run_persona_review_check` to use the shared helper
    - Add `from ralph_loop.json_extract import extract_validating_object` to the module imports in `ralph_loop/validator.py`.
    - In `_run_persona_review_check`, replace the current `_extract_first_json_object` + `json.loads` + `PersonaReviewVerdict.model_validate` block (the lines beginning `json_text = _extract_first_json_object(invocation.stdout)` through the `parsed_verdict = None` in the `except` branch) with the single call `parsed_verdict = extract_validating_object(invocation.stdout, PersonaReviewVerdict)` as shown in design.md §Fix Implementation.
    - Delete the local `_extract_first_json_object` helper from `ralph_loop/validator.py` (the block around line 145).
    - Everything downstream of `parsed_verdict` — the fail-marking, the `"could not parse verdict"` log line, the `CheckResult` construction with `output=invocation.stdout` — is unchanged.
    - Keep the module-level `json` import (used elsewhere in the module) and keep the `ValidationError` import (still referenced by other paths).
    - _Validates: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.3_
    - _Exercises: Property 16_
    - _Bug_Condition: `isBugCondition(invocation.stdout)` for target model `PersonaReviewVerdict`_
    - _Expected_Behavior: `expectedBehavior(parsed_verdict)` — parsed_verdict equals the embedded payload_
    - _Preservation: R3.1, R3.2, R3.3 — existing `test_valid_verdict_captures_rationale_and_condition`, `test_verdict_wrapped_in_prose_is_extracted`, and `test_unparseable_verdict_marks_fail` MUST still pass_

  - [x] 2.3 Migrate `ralph_loop/orchestrator.py::Orchestrator._parse_decision` to use the shared helper
    - Add `from ralph_loop.json_extract import extract_validating_object` to the module imports in `ralph_loop/orchestrator.py`.
    - Replace the body of `Orchestrator._parse_decision` with the three-line version in design.md §Fix Implementation: early-return `None` on empty `raw`, else return `extract_validating_object(raw, OrchestratorDecision)`.
    - Delete the local `_extract_first_json_object` helper from `ralph_loop/orchestrator.py` (the block around line 166).
    - Remove the now-unused `import json` from `ralph_loop/orchestrator.py` if no other code path in that module references `json` (verify with a grepSearch before deleting). Keep the `from pydantic import BaseModel, ValidationError` import — `BaseModel` is still needed for `OrchestratorDecision`; `ValidationError` is unused after the helper extraction and may be dropped from the import tuple if nothing else in the module uses it.
    - _Validates: 3.5_
    - _Exercises: Property 17, Property 19_
    - _Bug_Condition: `isBugCondition(raw)` for target model `OrchestratorDecision`_
    - _Expected_Behavior: `expectedBehavior(decision)` — decision equals the embedded `{"persona": ..., "rationale": ...}` payload_
    - _Preservation: R3.5 — existing orchestrator tests that feed flat `{"persona":"X","rationale":"Y"}` stdout MUST still pass unchanged_

- [x] 3. Add preservation properties and per-scenario regression tests

  - [x] 3.1 Add Property 18 (preservation, PersonaReviewVerdict) and Property 19 (preservation, OrchestratorDecision) to `tests/test_validator_properties.py`
    - **Property 2: Preservation** — Single-flat-object happy path matches the naive pipeline byte-for-byte
    - **IMPORTANT**: Follow observation-first methodology. Pin a module-level copy of the original helper as `_extract_first_json_object_naive` inside `tests/test_validator_properties.py` so preservation keeps comparing against original behavior even after the production helper is deleted. Copy the naive body verbatim from the pre-fix `ralph_loop/validator.py` (brace-counting scan with no string/escape tracking).
    - Add **Property 18** with `@given(verdict=st.sampled_from(["pass","fail"]), rationale=st.text(max_size=100))` exactly as in design.md §Testing Strategy §Preservation Checking: construct `stdout = json.dumps({"verdict": verdict, "rationale": rationale})`, run the naive pipeline (`_extract_first_json_object_naive` + `json.loads` + `PersonaReviewVerdict.model_validate` with `(json.JSONDecodeError, ValidationError)` caught), run the fixed pipeline (`extract_validating_object(stdout, PersonaReviewVerdict)`), assert equality.
    - Add **Property 19** mirroring Property 18 for `OrchestratorDecision` with `@given(persona=st.text(min_size=1, max_size=20), rationale=st.text(max_size=100))`. Import `OrchestratorDecision` from `ralph_loop.orchestrator`.
    - Both properties MUST pass on fixed code (Property 18/19 are preservation, not fix-checking).
    - _Validates: 3.1, 3.2, 3.5_
    - _Exercises: Property 18, Property 19_

  - [x] 3.2 Add direct unit tests for `ralph_loop/json_extract.py` in a new `tests/test_json_extract.py`
    - Create `tests/test_json_extract.py` with targeted unit tests for each helper. Cover at minimum:
      - `strip_markdown_fences`: fence with language tag (```` ```json\n...\n``` ````), fence without language tag (```` ```\n...\n``` ````), no-op when no fence is present, fence with trailing whitespace on the close line.
      - `iter_balanced_json_objects`: string-state tracking (`{"a": "contains { brace"}` yields the whole object, not a truncated substring), escape-state tracking (`{"a": "saw \"{\" in string"}` and `{"a": "backslash \\\\"}`), multiple balanced objects (`{"a":1}{"b":2}` yields both in order), unbalanced-span skipping (`{"a":1` followed by `{"b":2}` yields only `{"b":2}`).
      - `extract_validating_object`: validates successfully against `PersonaReviewVerdict` (import from `ralph_loop.validator`) when the payload matches, returns `None` when no candidate validates, validates successfully against `OrchestratorDecision` (import from `ralph_loop.orchestrator`), skips a leading tool-use envelope in favor of the verdict object.
    - _Validates: 2.1, 2.2, 2.3, 2.4, 2.5_
    - _Exercises: the infrastructure underpinning Property 16, Property 17, Property 18, Property 19_

  - [x] 3.3 Add five regression tests to `tests/test_validator.py::TestPersonaReviewCheck` covering scenarios 1.1–1.5
    - Each test stubs the Kiro invoker with `AsyncMock(spec=KiroInvoker)` returning a `KiroInvocationResult` whose `stdout` matches the scenario, then calls `_run_persona_review_check` and asserts `result.verdict` and `result.rationale` match the embedded payload. Model the scenarios on `test_verdict_wrapped_in_prose_is_extracted` in the same class.
    - Scenario 1.1 (markdown fence): `stdout = "```json\n{\"verdict\": \"pass\", \"rationale\": \"ok\"}\n```"` → expect `verdict="pass"`, `rationale="ok"`.
    - Scenario 1.2 (leading tool-use envelope): `stdout = '{"tool":"read_file","args":{"path":"x"}}\n{"verdict":"pass","rationale":"ok"}'` → expect `verdict="pass"`, `rationale="ok"`.
    - Scenario 1.3 (literal `}` inside rationale): `stdout = '{"verdict":"fail","rationale":"missing } in expression"}'` → expect `verdict="fail"`, `rationale="missing } in expression"`.
    - Scenario 1.4 (escaped quotes around a brace): `stdout = '{"verdict":"fail","rationale":"saw \\"{\\" unexpected"}'` → expect `verdict="fail"`, `rationale='saw "{" unexpected'`.
    - Scenario 1.5 (multiple objects, verdict not first): `stdout = '{"progress":1}\n{"tool":"read_file","args":{}}\n{"verdict":"pass","rationale":"ok"}'` → expect `verdict="pass"`, `rationale="ok"`.
    - _Validates: 2.1, 2.2, 2.3, 2.4, 2.5_
    - _Exercises: Property 16_

  - [x] 3.4 Add Property 17 plus five mirror regression tests to `tests/test_orchestrator.py`
    - Add **Property 17** (fix-checking, `OrchestratorDecision`) to `tests/test_orchestrator.py`, mirroring the Property 16 `@given` strategy but with `persona=st.text(min_size=1, max_size=20)` and `rationale=st.text(max_size=200)` and calling `extract_validating_object(stdout, OrchestratorDecision)` (import `OrchestratorDecision` and `extract_validating_object` directly). Property 17 MUST fail on unfixed code for the same reasons as Property 16; after Task 2.3 lands it MUST pass. Record via `updatePBTStatus` the same way Property 16 was recorded in Task 1.
    - Add five regression tests to `tests/test_orchestrator.py` exercising `Orchestrator._parse_decision` (or an Orchestrator end-to-end via the existing harness) with stdout from scenarios 1.1–1.5 adapted to `{"persona": "Writer", "rationale": "ok"}` shape. Each asserts the returned `OrchestratorDecision` matches the embedded payload.
    - _Validates: 2.1, 2.2, 2.3, 2.4, 2.5, 3.5_
    - _Exercises: Property 17_

- [x] 4. Full-suite verification
  - Run `python -m pytest tests/ -q` once and confirm: all 463 previously-passing tests still pass; every new test added in Tasks 1, 3.1, 3.2, 3.3, 3.4 passes; Property 16 (added in Task 1) now PASSES on fixed code (it failed on unfixed code — update its PBT status from `failed` to `passed`); Property 17 PASSES; Property 18 and Property 19 PASS.
  - Run `grepSearch` with the query `_extract_first_json_object` across the `tests/` tree (skipPruning: true) and confirm the only match is the pinned `_extract_first_json_object_naive` copy inside `tests/test_validator_properties.py`. No test imports the production helper by name (it no longer exists).
  - Run `grepSearch` with the query `_extract_first_json_object` across `ralph_loop/` and confirm zero matches — both production helpers were deleted.
  - If any test fails, fix the production code (not the test) and re-run; if any `grepSearch` result is unexpected, investigate before proceeding.
  - _Validates: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_
  - _Exercises: Property 16, Property 17, Property 18, Property 19_

- [-]* 5. Commit the fix
  - Stage only the files this bugfix touches: `ralph_loop/json_extract.py` (new), `ralph_loop/validator.py`, `ralph_loop/orchestrator.py`, `tests/test_json_extract.py` (new), `tests/test_validator.py`, `tests/test_validator_properties.py`, `tests/test_orchestrator.py`. Do not stage unrelated files.
  - Commit with a multi-line message that names the two real-world symptoms we observed: reviewer `TechCodeReviewer` on task `ch02-r01-python` burning `max_retries_per_task` on valid verdicts, and the `persona_review` check falsely marking fail when the reviewing persona wrapped JSON in a markdown fence or prepended a tool-use envelope. Reference scenarios 1.1–1.5 from design.md in the body. Do not include a `--no-verify` flag; let any configured pre-commit hooks run.
  - _Validates: none (commit only)_
