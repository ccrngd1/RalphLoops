"""Configurable stub of ``claude -p`` for integration tests (Task 23.1).

This script is invoked by integration tests as the ``claude_cli_command``
via ``sys.executable`` so no real Claude Code CLI binary is required. Behaviour
is controlled through environment variables set by the test before
spawning the :class:`ralph_loop.claude_code.ClaudeCodeInvoker`. The harness supports
the three things the integration suite needs:

* Emitting an arbitrary stdout payload (``RALPH_STUB_STDOUT``).
* Optionally appending a JSON line with token fields so the
  invoker can parse :class:`ralph_loop.models.TokenUsage` (R12.1).
* Optionally mutating ``tasks.json`` on disk so the Task Creation
  Processor, Planner, and resumption flows can observe changes that
  look like they came from a real persona session.

Per-invocation customisation is provided by a tiny counter file: when
``RALPH_STUB_COUNTER_FILE`` is set, the stub reads the integer inside
(creating the file at ``0`` on first call), writes back the incremented
value, and uses that index to pick a per-call override from the
``RALPH_STUB_CALL_OVERRIDES_FILE`` JSON map. The override is merged on
top of the baseline environment variables for the duration of the call
so a test can say "call #2 should mutate tasks.json with this payload,
call #3 should emit this stdout" without spawning multiple processes
with different envs.

The stub always drains stdin so the parent's ``context`` write never
stalls on a full pipe, and it never imports ``ralph_loop`` so it works
in a completely isolated subprocess environment.

Requirements exercised: R6.1 (stdin drained so context flows), R12.1
(structured token envelope), R12.6 (optional token envelope).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Counter helpers
# ---------------------------------------------------------------------------


def _read_counter(counter_path: str) -> int:
    """Return the current invocation index and advance it by one.

    On first invocation the file is created with ``"0"``; the returned
    index is always the *current* call's zero-based number. A missing
    or malformed counter file starts from zero so tests don't have to
    pre-populate it.
    """

    try:
        raw = open(counter_path, "r", encoding="utf-8").read().strip()
        idx = int(raw) if raw else 0
    except (OSError, ValueError):
        idx = 0
    try:
        with open(counter_path, "w", encoding="utf-8") as fh:
            fh.write(str(idx + 1))
    except OSError:
        # Counter writes are best-effort; a failure doesn't fail the call.
        pass
    return idx


def _call_overrides(overrides_path: str, index: int) -> dict[str, Any]:
    """Return the per-call override dict for ``index`` from the overrides JSON.

    The overrides file is a JSON object whose keys are stringified call
    indices (``"0"``, ``"1"``, ...) and whose values are dicts merged
    on top of the environment for this call. Missing entries yield an
    empty dict, so a test can configure only the calls it cares about.
    """

    try:
        with open(overrides_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(data, dict):
        return {}
    entry = data.get(str(index))
    if not isinstance(entry, dict):
        return {}
    return entry


def _resolve(key: str, overrides: dict[str, Any]) -> Optional[str]:
    """Return the effective value for ``key`` from overrides or env.

    Per-call overrides win over baseline environment so tests can
    configure a default behaviour and then tweak specific calls.
    """

    if key in overrides and overrides[key] is not None:
        value = overrides[key]
        return str(value) if not isinstance(value, str) else value
    return os.environ.get(key)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    # Drain stdin so the parent's context write never blocks on a full
    # pipe (the real Claude Code CLI also consumes its stdin).
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001 - stdin may be closed in some test paths
        pass

    # Load per-call overrides if the test supplied them.
    overrides: dict[str, Any] = {}
    counter_path = os.environ.get("RALPH_STUB_COUNTER_FILE")
    overrides_path = os.environ.get("RALPH_STUB_CALL_OVERRIDES_FILE")
    if counter_path and overrides_path:
        idx = _read_counter(counter_path)
        overrides = _call_overrides(overrides_path, idx)

    # Optional tasks.json mutation: copy the contents of the source
    # file over the target path. Paths are resolved per call so a test
    # can rewrite tasks.json once, then leave it alone on the next
    # iteration by omitting the override.
    mutate_path = _resolve("RALPH_STUB_MUTATE_TASKS", overrides)
    tasks_path = _resolve("RALPH_STUB_TASKS_PATH", overrides)
    if mutate_path and tasks_path:
        try:
            shutil.copyfile(mutate_path, tasks_path)
        except OSError as exc:
            # Surface the copy failure on stderr so the test can inspect
            # it; we do not treat this as fatal because exit code is
            # controlled separately.
            sys.stderr.write(f"fake_claude mutate failed: {exc}\n")

    # Emit the configured stdout payload.
    stdout_value = _resolve("RALPH_STUB_STDOUT", overrides)
    if stdout_value is None:
        stdout_value = "stub response\n"
    if not stdout_value.endswith("\n"):
        stdout_value += "\n"
    sys.stdout.write(stdout_value)

    # Optionally emit a token usage JSON line so the invoker can parse
    # ``TokenUsage`` (R12.1). When absent, the invoker logs a warning
    # and returns ``token_usage=None`` (R12.6).
    if _resolve("RALPH_STUB_EMIT_TOKENS", overrides) == "1":
        token_payload_raw = _resolve("RALPH_STUB_TOKEN_PAYLOAD", overrides)
        if token_payload_raw:
            payload = token_payload_raw
        else:
            payload = json.dumps(
                {"input_tokens": 10, "output_tokens": 5, "model": "stub-model"}
            )
        sys.stdout.write(f"{payload}\n")

    # ``persona_review`` verdict helpers. The validator spawns the
    # reviewing persona in its own Claude Code CLI session and parses a
    # ``{"verdict": "pass"|"fail"}`` object from stdout; emit one here
    # so integration tests that drive the full validator can stub a
    # pass or fail without building a second harness.
    verdict = _resolve("RALPH_STUB_EMIT_VERDICT", overrides)
    if verdict in ("pass", "fail"):
        sys.stdout.write(
            json.dumps({"verdict": verdict, "rationale": f"stub {verdict}"})
            + "\n"
        )

    # Orchestrator persona-selection decision helper: emit a JSON object
    # naming the chosen persona so the Orchestrator can parse it into an
    # ``OrchestratorDecision``.
    persona_decision = _resolve("RALPH_STUB_EMIT_PERSONA_DECISION", overrides)
    if persona_decision:
        sys.stdout.write(
            json.dumps(
                {"persona": persona_decision, "rationale": "stub rationale"}
            )
            + "\n"
        )

    # Optional stderr payload.
    stderr_value = _resolve("RALPH_STUB_STDERR", overrides)
    if stderr_value is not None:
        if not stderr_value.endswith("\n"):
            stderr_value += "\n"
        sys.stderr.write(stderr_value)

    sys.stdout.flush()
    sys.stderr.flush()

    exit_code_raw = _resolve("RALPH_STUB_EXIT_CODE", overrides) or "0"
    try:
        sys.exit(int(exit_code_raw))
    except ValueError:
        sys.exit(0)


if __name__ == "__main__":
    main()
