"""Unit tests for the graceful Invocation_Error handler in ``ralph_loop.cli``.

Covers :func:`_handle_invocation_error` (R1.1, R1.2, R1.4, R1.5, R1.7) and
:func:`_excerpt` (R1.2). Property 20 (Invocation_Error converges to
Iteration_Failure via the same rule as a failing check) is exercised
elsewhere in :mod:`tests.test_cli_properties`; this module focuses on
scripted unit scenarios.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from ralph_loop.atomic_io import atomic_write_bytes
from ralph_loop.cli import (
    CHUNK_LIMIT_SUBSTRING,
    _excerpt,
    _handle_invocation_error,
    _load_tasks,
)
from ralph_loop.claude_code import ClaudeCodeInvocationTimeout
from ralph_loop.models import TASK_LIST_ADAPTER, Task
from ralph_loop.status_update import status_after_validation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(**overrides: Any) -> Task:
    """Construct a ``Task`` with minimal required fields."""
    data: dict[str, Any] = {
        "id": "T-1",
        "title": "Example task",
        "priority": 1,
        "status": "pending",
        "spec_path": "specs/T-1.md",
        "retry_count": 0,
    }
    data.update(overrides)
    return Task(**data)


def _write_tasks(tasks_path: Path, tasks: list[Task]) -> None:
    """Persist ``tasks`` to ``tasks_path`` using the production serializer."""
    atomic_write_bytes(tasks_path, TASK_LIST_ADAPTER.dump_json(tasks))


def _invocation_error_event(logs: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the single ``iteration_invocation_error`` record from ``logs``."""
    matches = [r for r in logs if r.get("event") == "iteration_invocation_error"]
    assert len(matches) == 1, (
        f"expected exactly one iteration_invocation_error record; got {logs!r}"
    )
    return matches[0]


# ---------------------------------------------------------------------------
# chunk_limit detection
# ---------------------------------------------------------------------------


class TestChunkLimitDetection:
    """``chunk_limit_detected`` / ``failure_mode`` reflect the Kiro CLI marker.

    The handler lower-cases the concatenation of ``str(exc)``, ``exc.stderr``,
    and ``exc.stdout`` and tests for :data:`CHUNK_LIMIT_SUBSTRING`, so the
    substring can hide in any of the three sources (R1.7).
    """

    def test_chunk_limit_detected_in_str_exc_only(self, tmp_path: Path) -> None:
        # A bare ``RuntimeError`` carries the chunk-limit marker only in
        # its message; ``stdout`` / ``stderr`` attributes are absent.
        tasks_path = tmp_path / "tasks.json"
        task = _make_task()
        _write_tasks(tasks_path, [task])

        exc = RuntimeError("chunk exceed the limit")

        with capture_logs() as logs:
            _handle_invocation_error(
                exc=exc,
                task=task,
                persona_name="Writer",
                tasks=[task],
                tasks_path=tasks_path,
            )

        record = _invocation_error_event(logs)
        assert record["chunk_limit_detected"] is True
        assert record["failure_mode"] == "chunk_limit"

    def test_chunk_limit_detected_in_exc_stderr_only(
        self, tmp_path: Path
    ) -> None:
        # ``CalledProcessError.stderr`` is a standard attribute that the
        # handler must inspect even when the message alone does not carry
        # the marker.
        tasks_path = tmp_path / "tasks.json"
        task = _make_task()
        _write_tasks(tasks_path, [task])

        exc = subprocess.CalledProcessError(
            returncode=1,
            cmd="kiro",
            stderr="chunk exceed the limit",
            output="",
        )
        # Sanity-check: the exception message alone does NOT contain the
        # marker; we rely on the handler reading ``exc.stderr``.
        assert CHUNK_LIMIT_SUBSTRING not in str(exc).lower()

        with capture_logs() as logs:
            _handle_invocation_error(
                exc=exc,
                task=task,
                persona_name="Writer",
                tasks=[task],
                tasks_path=tasks_path,
            )

        record = _invocation_error_event(logs)
        assert record["chunk_limit_detected"] is True
        assert record["failure_mode"] == "chunk_limit"

    def test_chunk_limit_detected_in_exc_stdout_only(
        self, tmp_path: Path
    ) -> None:
        # ``CalledProcessError.stdout`` is an alias for ``.output``; the
        # handler must inspect it via ``getattr(exc, "stdout", ...)``.
        tasks_path = tmp_path / "tasks.json"
        task = _make_task()
        _write_tasks(tasks_path, [task])

        exc = subprocess.CalledProcessError(
            returncode=1,
            cmd="kiro",
            stderr="",
            output="chunk exceed the limit",
        )
        # Sanity-check: stderr is empty and the message lacks the marker.
        assert CHUNK_LIMIT_SUBSTRING not in str(exc).lower()
        assert exc.stdout == "chunk exceed the limit"

        with capture_logs() as logs:
            _handle_invocation_error(
                exc=exc,
                task=task,
                persona_name="Writer",
                tasks=[task],
                tasks_path=tasks_path,
            )

        record = _invocation_error_event(logs)
        assert record["chunk_limit_detected"] is True
        assert record["failure_mode"] == "chunk_limit"

    def test_case_insensitive_detection(self, tmp_path: Path) -> None:
        # The handler lower-cases its three sources before the membership
        # test, so an all-uppercase marker must also match (R1.7).
        tasks_path = tmp_path / "tasks.json"
        task = _make_task()
        _write_tasks(tasks_path, [task])

        exc = RuntimeError("CHUNK EXCEED THE LIMIT")

        with capture_logs() as logs:
            _handle_invocation_error(
                exc=exc,
                task=task,
                persona_name="Writer",
                tasks=[task],
                tasks_path=tasks_path,
            )

        record = _invocation_error_event(logs)
        assert record["chunk_limit_detected"] is True
        assert record["failure_mode"] == "chunk_limit"

    def test_no_chunk_limit_marker_falls_back_to_generic(
        self, tmp_path: Path
    ) -> None:
        # A generic error with no marker anywhere must be classified
        # ``failure_mode="generic"`` and ``chunk_limit_detected=False``.
        tasks_path = tmp_path / "tasks.json"
        task = _make_task()
        _write_tasks(tasks_path, [task])

        exc = RuntimeError("something else failed")

        with capture_logs() as logs:
            _handle_invocation_error(
                exc=exc,
                task=task,
                persona_name="Writer",
                tasks=[task],
                tasks_path=tasks_path,
            )

        record = _invocation_error_event(logs)
        assert record["chunk_limit_detected"] is False
        assert record["failure_mode"] == "generic"


# ---------------------------------------------------------------------------
# Synthetic CheckResult and ClaudeCodeInvocationTimeout handling
# ---------------------------------------------------------------------------


class TestSyntheticCheckResult:
    """The handler builds a synthetic failing ``CheckResult`` for the oracle."""

    def test_timeout_sets_timed_out_true_on_synthetic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When the underlying exception is ``ClaudeCodeInvocationTimeout``, the
        # synthetic ``CheckResult`` passed to ``status_after_validation``
        # must carry ``timed_out=True`` so downstream log fields and any
        # future oracle that looks at ``timed_out`` stay accurate (R1.7).
        tasks_path = tmp_path / "tasks.json"
        task = _make_task()
        _write_tasks(tasks_path, [task])

        captured: dict[str, Any] = {}

        def fake_status_after_validation(t: Task, checks: list[Any]) -> tuple[Any, int]:
            captured["task"] = t
            captured["checks"] = checks
            return ("failing", t.retry_count + 1)

        monkeypatch.setattr(
            "ralph_loop.cli.status_after_validation",
            fake_status_after_validation,
        )

        exc = ClaudeCodeInvocationTimeout("ran over the per-call budget")

        with capture_logs() as logs:
            _handle_invocation_error(
                exc=exc,
                task=task,
                persona_name="Writer",
                tasks=[task],
                tasks_path=tasks_path,
            )

        # Exactly one synthetic check was built and it carries
        # ``timed_out=True``.
        assert len(captured["checks"]) == 1
        synthetic = captured["checks"][0]
        assert synthetic.timed_out is True
        assert synthetic.verdict == "fail"
        assert synthetic.type == "shell"
        assert synthetic.name == "claude_invocation"

        # The emitted log record reflects the oracle output.
        record = _invocation_error_event(logs)
        assert record["new_status"] == "failing"
        assert record["new_retry_count"] == task.retry_count + 1

    def test_non_timeout_sets_timed_out_false_on_synthetic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A regular exception (not a timeout) must produce a synthetic
        # check with ``timed_out=False`` so the field does not drift from
        # the runtime reality of the invocation.
        tasks_path = tmp_path / "tasks.json"
        task = _make_task()
        _write_tasks(tasks_path, [task])

        captured: dict[str, Any] = {}

        def fake_status_after_validation(t: Task, checks: list[Any]) -> tuple[Any, int]:
            captured["checks"] = checks
            return ("failing", t.retry_count + 1)

        monkeypatch.setattr(
            "ralph_loop.cli.status_after_validation",
            fake_status_after_validation,
        )

        _handle_invocation_error(
            exc=RuntimeError("bad things"),
            task=task,
            persona_name="Writer",
            tasks=[task],
            tasks_path=tasks_path,
        )

        synthetic = captured["checks"][0]
        assert synthetic.timed_out is False


# ---------------------------------------------------------------------------
# Exceptions without ``stdout`` / ``stderr`` attributes
# ---------------------------------------------------------------------------


def test_runtime_error_without_std_attrs_yields_empty_excerpts(
    tmp_path: Path,
) -> None:
    # A plain ``RuntimeError`` has neither ``stdout`` nor ``stderr``
    # attributes. The handler uses ``getattr(..., default="")`` so no
    # ``AttributeError`` may escape and the log record carries empty
    # excerpts (R1.2).
    tasks_path = tmp_path / "tasks.json"
    task = _make_task()
    _write_tasks(tasks_path, [task])

    exc = RuntimeError("no attrs")
    # Confirm the standard library really does not add these attrs.
    assert not hasattr(exc, "stdout")
    assert not hasattr(exc, "stderr")

    with capture_logs() as logs:
        _handle_invocation_error(
            exc=exc,
            task=task,
            persona_name="Writer",
            tasks=[task],
            tasks_path=tasks_path,
        )

    record = _invocation_error_event(logs)
    assert record["stdout_excerpt"] == ""
    assert record["stderr_excerpt"] == ""
    assert record["exception_type"] == "RuntimeError"
    assert record["exception_message"] == "no attrs"


# ---------------------------------------------------------------------------
# Persistence ordering: disk update happens BEFORE the structured log
# ---------------------------------------------------------------------------


class _RaisingProcessorError(RuntimeError):
    """Sentinel raised by the test processor on its first invocation."""


class _RaiseOnFirstCall:
    """Structlog processor that raises on first call, passes afterwards.

    Used to simulate a crash in the logger path so the ``_handle_invocation_error``
    persistence-ordering guarantee (R1.4) can be exercised: the atomic
    write to ``tasks.json`` must complete before the logger is invoked,
    so the disk state remains consistent even when logging fails.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self, logger: Any, method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            raise _RaisingProcessorError("boom from processor")
        return event_dict


@pytest.fixture
def _restore_structlog_config():
    """Snapshot the current structlog config and restore it on teardown."""
    # ``structlog`` does not expose a public API to snapshot the active
    # config, but ``reset_defaults`` rebuilds the library's standard
    # processor chain and logger factory. Earlier tests in the suite
    # rely on this default so resetting to it is safe.
    yield
    structlog.reset_defaults()


def test_tasks_json_updated_before_logger_is_invoked(
    tmp_path: Path, _restore_structlog_config: None
) -> None:
    # R1.4: the atomic write to ``tasks.json`` must land BEFORE the
    # structured log record is emitted, so a crash in the logger path
    # cannot cause the status/retry update to be lost.
    tasks_path = tmp_path / "tasks.json"
    task = _make_task(status="pending", retry_count=0)
    _write_tasks(tasks_path, [task])

    raising = _RaiseOnFirstCall()
    structlog.configure(
        processors=[raising],
        wrapper_class=structlog.BoundLogger,
        cache_logger_on_first_use=False,
    )

    exc = RuntimeError("logger must fail first, disk must already be updated")

    with pytest.raises(_RaisingProcessorError):
        _handle_invocation_error(
            exc=exc,
            task=task,
            persona_name="Writer",
            tasks=[task],
            tasks_path=tasks_path,
        )

    # The processor must have been invoked exactly once (the raise).
    assert raising.calls == 1

    # Despite the logger crashing, ``tasks.json`` on disk reflects the
    # new ``(status, retry_count)`` from ``status_after_validation``.
    reloaded = _load_tasks(tasks_path)
    oracle_status, oracle_retry = status_after_validation(
        task,
        [
            # Any failing check produces the same ``("failing", n+1)``
            # result via ``status_after_validation``; the handler
            # synthesises one such check internally.
            _synthetic_failing_check(),
        ],
    )
    assert len(reloaded) == 1
    assert reloaded[0].status == oracle_status
    assert reloaded[0].retry_count == oracle_retry


def _synthetic_failing_check():
    """Mirror the handler's synthetic check shape for oracle comparison."""
    from ralph_loop.models import CheckResult

    return CheckResult(
        type="shell",
        name="claude_invocation",
        verdict="fail",
        output="",
        duration_ms=0,
        timed_out=False,
    )


# ---------------------------------------------------------------------------
# _excerpt helper
# ---------------------------------------------------------------------------


class TestExcerpt:
    """``_excerpt`` round-trips short strings and truncates long ones."""

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "abc",
            "a" * 1999,
            "a" * 2000,
        ],
    )
    def test_short_or_boundary_strings_round_trip(self, value: str) -> None:
        # Inputs at or below the 2000-char limit are returned unchanged
        # (including the empty string and the inclusive boundary).
        assert _excerpt(value) == value

    def test_long_string_truncates_with_suffix(self) -> None:
        # One over the default limit must be truncated to the first
        # ``limit`` characters plus the ``"...[truncated N chars]"`` tail.
        value = "a" * 2001
        result = _excerpt(value)
        assert result.startswith("a" * 2000)
        assert result == "a" * 2000 + "...[truncated 1 chars]"

    def test_much_longer_string_truncates_with_correct_count(self) -> None:
        # The suffix reports the number of characters the caller did
        # NOT see (``len(s) - limit``).
        value = "b" * 3000
        result = _excerpt(value)
        assert result == "b" * 2000 + "...[truncated 1000 chars]"

    def test_custom_limit_is_honoured(self) -> None:
        # ``limit`` is a caller-provided parameter; short inputs round-trip
        # and long inputs truncate with the same marker shape.
        assert _excerpt("hello", limit=10) == "hello"
        assert _excerpt("x" * 15, limit=10) == "x" * 10 + "...[truncated 5 chars]"

    def test_non_str_input_is_stringified_first(self) -> None:
        # The handler passes ``getattr(exc, "stdout", "") or ""`` which
        # is always a string in practice, but ``_excerpt`` also accepts
        # non-string values via ``str()`` to be robust against exotic
        # exception types.
        assert _excerpt(12345) == "12345"
        # ``bytes`` renders via ``repr`` through ``str``.
        assert _excerpt(b"abc") == "b'abc'"

    def test_non_str_input_long_enough_to_truncate(self) -> None:
        # Long non-str input is stringified first, then truncated. An
        # integer with 2500 digits stringifies to a 2500-char string
        # and must truncate to the usual 2000-char prefix + suffix.
        value = int("9" * 2500)
        result = _excerpt(value)
        assert result.startswith("9" * 2000)
        assert result.endswith("...[truncated 500 chars]")
        assert len(result) == 2000 + len("...[truncated 500 chars]")


# ===========================================================================
# Property 24: Loop exit code is termination-decision-driven, not
# Invocation_Error-driven.
#
# Scripted enumeration over four scenarios per the design's Testing Strategy
# section:
#   (a) all-pass sequence -> EXIT_SUCCESS
#   (b) handler + pass mix -> termination-decision exit code, never
#       EXIT_INVOCATION_ERROR
#   (c) repeated handler fires on the same task until retry_count ==
#       max_retries_per_task transitions it out of eligibility -> EXIT_BLOCKED
#   (d) handler fire on one task followed by a passing iteration on a
#       different task -> termination-decision exit code
#
# Each scenario asserts exit_code equals termination_decision(final_tasks)
# .exit_code (with the EXIT_BLOCKED fallback honoured when the loop exits
# via the "no eligible task" branch in _run_loop).
#
# **Validates: R1.3, R1.6, R3.1**
# ===========================================================================


class _ScriptedClaudeCodeInvoker:
    """Script-driven stand-in for :class:`ralph_loop.kiro.ClaudeCodeInvoker`.

    Per-task call scripts: each entry of ``raise_script[task_id]`` is the
    exception to raise on that call (None means "return a passing
    ClaudeInvocationResult"). Once a task's script is exhausted, all
    subsequent calls return passing. ``raise_every_call_for`` is a set of
    task ids for which every call raises ``RuntimeError("chunk exceed the
    limit")`` regardless of the per-call script.

    The executing task id is parsed from the composed context window's
    ``# Task Spec (id=<id>, title=...)`` header, mirroring the technique
    used by ``_StubClaudeCodeInvoker`` in :mod:`tests.test_cli`.
    """

    def __init__(
        self,
        *,
        raise_script: dict[str, list[BaseException | None]] | None = None,
        raise_every_call_for: set[str] | None = None,
    ) -> None:
        self._raise_script = {k: list(v) for k, v in (raise_script or {}).items()}
        self._raise_every_call_for = set(raise_every_call_for or set())
        self.calls_by_task_id: dict[str, int] = {}

    async def invoke(
        self,
        *,
        context: str,
        log_path: Path,
        call_kind: str,
        timeout_ms: int | None = None,
        cwd: Path | None = None,
        stdout_sink: Any = None,
        model_id: str | None = None,
    ) -> Any:
        import re

        from ralph_loop.models import ClaudeInvocationResult

        match = re.search(r"# Task Spec \(id=([^,]+),", context)
        task_id = match.group(1).strip() if match else "<unknown>"
        idx = self.calls_by_task_id.get(task_id, 0)
        self.calls_by_task_id[task_id] = idx + 1

        if task_id in self._raise_every_call_for:
            raise RuntimeError("chunk exceed the limit")

        script = self._raise_script.get(task_id, [])
        if idx < len(script) and script[idx] is not None:
            raise script[idx]  # type: ignore[misc]

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"scripted_invoker task_id={task_id}\n", encoding="utf-8"
        )
        return ClaudeInvocationResult(
            exit_code=0,
            stdout="",
            stderr="",
            token_usage=None,
            duration_ms=0,
        )


class _PassingValidator:
    """Stand-in for :class:`ralph_loop.validator.Validator` that always passes."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls_by_task_id: dict[str, int] = {}

    async def run(
        self,
        *,
        task: Any,
        spec: Any,
        executing_persona_name: str,
        log_path: Path,
        default_timeout_ms: int = 5 * 60 * 1000,
        cwd: Path | None = None,
    ) -> Any:
        from ralph_loop.models import CheckResult, ValidationResult

        self.calls_by_task_id[task.id] = (
            self.calls_by_task_id.get(task.id, 0) + 1
        )
        return ValidationResult(
            overall="pass",
            checks=[
                CheckResult(
                    type="shell",
                    name="stub_pass",
                    verdict="pass",
                    output="ok",
                    duration_ms=0,
                )
            ],
            timed_out_checks=[],
        )


class _NoopGitManager:
    """Stand-in for :class:`ralph_loop.git_manager.GitManager`."""

    def __init__(self, *, enabled: bool, cwd: Path) -> None:
        self.enabled = enabled
        self.cwd = cwd
        self.iteration_commit_calls: list[tuple[int, str, str, str]] = []

    def iteration_commit(
        self,
        *,
        iteration: int,
        task_id: str,
        persona_name: str,
        outcome: str,
    ) -> Any:
        from ralph_loop.models import CommitResult

        self.iteration_commit_calls.append(
            (iteration, task_id, persona_name, outcome)
        )
        return CommitResult(sha=None, skipped=True, skip_reason="stub")


def _write_min_spec(path: Path, task_id: str) -> None:
    """Mirror of ``tests.test_cli._write_min_spec`` -- minimal valid Task_Spec.

    Duplicated rather than imported so the harness stays localised to this
    module per the implementation strategy.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"id: {task_id}\n"
        f"title: Task {task_id}\n"
        "validation:\n"
        "  - type: shell\n"
        "    name: stub-check\n"
        '    commands: ["true"]\n'
        "---\n"
        "## Objective\n"
        f"Do the work for {task_id}.\n\n"
        "## Context References\n"
        "None.\n\n"
        "## Instructions\n"
        "Produce the artefact.\n",
        encoding="utf-8",
    )


def _build_scripted_project(
    project: Path, task_ids: list[str], *, max_retries: int
) -> None:
    """Scaffold a minimal project root the loader will accept.

    All tasks are routed explicitly to a single ``Writer`` persona so the
    Orchestrator takes the no-LLM ``path="explicit"`` branch.
    Git integration is disabled and the auto-planner is off.
    """
    project.mkdir(parents=True, exist_ok=True)
    (project / "SUMMARY.md").write_text(
        "# Project\n\nMinimal brief for the Property 24 harness.\n",
        encoding="utf-8",
    )
    personas_dir = project / "personas"
    personas_dir.mkdir(exist_ok=True)
    (personas_dir / "writer.yaml").write_text(
        "name: Writer\n"
        "description: Drafts prose.\n"
        'prompt_template: "{{persona_name}} {{task_id}} {{task_title}}"\n',
        encoding="utf-8",
    )

    specs_dir = project / "specs"
    for tid in task_ids:
        _write_min_spec(specs_dir / f"{tid}.md", tid)

    tasks_payload = [
        {
            "id": tid,
            "title": f"Task {tid}",
            "priority": 1,
            "status": "pending",
            "spec_path": f"specs/{tid}.md",
            "retry_count": 0,
            "target_persona": "Writer",
        }
        for tid in task_ids
    ]
    import json as _json

    (project / "tasks.json").write_text(
        _json.dumps(tasks_payload), encoding="utf-8"
    )
    (project / "pending_tasks.json").write_text("[]", encoding="utf-8")
    (project / "ralph.config.json").write_text(
        _json.dumps(
            {
                "fallback_persona": "Writer",
                "max_iterations": 10,
                "max_retries_per_task": max_retries,
                "wall_clock_timeout_ms": 600_000,
                "git_integration_enabled": False,
                "automatic_planner": False,
            }
        ),
        encoding="utf-8",
    )


async def _drive_run_loop(
    *,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    invoker: _ScriptedClaudeCodeInvoker,
    validator: _PassingValidator,
    git: _NoopGitManager,
) -> tuple[int, list[Task]]:
    """Wire the stubs into ``cli`` via monkeypatch and run ``_run_loop``.

    Returns ``(exit_code, final_tasks)`` where ``final_tasks`` is the
    reloaded ``tasks.json`` after the loop returns. ``configure_logger``
    is neutralised so Property 20-style log records can still be
    captured by ``structlog.testing.capture_logs`` in callers if needed
    (unused here -- the property is purely about the exit code).
    """
    from ralph_loop import cli as cli_mod
    from ralph_loop.config import load_config

    monkeypatch.setattr(cli_mod, "ClaudeCodeInvoker", lambda *a, **kw: invoker)
    monkeypatch.setattr(cli_mod, "Validator", lambda *a, **kw: validator)
    monkeypatch.setattr(cli_mod, "GitManager", lambda *a, **kw: git)
    monkeypatch.setattr(cli_mod, "configure_logger", lambda *a, **kw: None)

    config = load_config(project_root=project)
    exit_code = await cli_mod._run_loop(config, project)
    final_tasks = _load_tasks(project / "tasks.json")
    return exit_code, final_tasks


def _termination_oracle(final_tasks: list[Task]) -> int:
    """Mirror the loop's exit-code rule: termination_decision first, with
    the ``next_eligible_task is None`` fallback to ``EXIT_BLOCKED``.

    Both branches are observable to the harness; for any non-budget exit,
    the loop returns either ``decision.exit_code`` (success / blocked) or
    ``EXIT_BLOCKED`` when ``decision.verdict == "continue"`` but the
    selector finds nothing eligible. This helper returns the same value
    in both cases so the four scenarios can use a single oracle.
    """
    from ralph_loop.cli import EXIT_BLOCKED
    from ralph_loop.task_selector import termination_decision

    decision = termination_decision(final_tasks)
    if decision.verdict == "success":
        return decision.exit_code or 0
    if decision.verdict == "blocked":
        return decision.exit_code or EXIT_BLOCKED
    # ``continue`` with no eligible task: the loop falls through the
    # ``if task is None`` branch and returns EXIT_BLOCKED.
    return EXIT_BLOCKED


class TestProperty24LoopExitCode:
    """Property 24: the loop's final exit code is termination-decision-
    driven (or budget-driven), never ``EXIT_INVOCATION_ERROR``.

    Each scenario builds a minimal in-memory ``_run_loop`` driver with
    scripted invoker / validator / git stubs, runs the loop end-to-end,
    and asserts the returned exit code equals the
    ``termination_decision`` oracle.
    """

    async def test_scenario_a_all_pass_returns_exit_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # (a) one pending task, every invocation passes -> EXIT_SUCCESS.
        # The oracle ``termination_decision([passing])`` returns
        # ``verdict="success"`` and ``exit_code=0``.
        from ralph_loop.cli import EXIT_INVOCATION_ERROR, EXIT_SUCCESS

        project = tmp_path / "scenario_a"
        _build_scripted_project(project, ["A"], max_retries=1)

        invoker = _ScriptedClaudeCodeInvoker()
        validator = _PassingValidator()
        git = _NoopGitManager(enabled=False, cwd=project)

        exit_code, final = await _drive_run_loop(
            project=project,
            monkeypatch=monkeypatch,
            invoker=invoker,
            validator=validator,
            git=git,
        )

        assert exit_code != EXIT_INVOCATION_ERROR
        assert exit_code == EXIT_SUCCESS
        assert exit_code == _termination_oracle(final)
        assert {t.id: t.status for t in final} == {"A": "passing"}

    async def test_scenario_b_handler_then_pass_mix_returns_termination_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # (b) two pending tasks A, B. A's first invocation raises a
        # chunk-limit RuntimeError; subsequent A calls and all B calls
        # pass. With max_retries=2, A retries once and passes, so the
        # final state is all-passing and the loop returns EXIT_SUCCESS
        # via termination_decision -- never EXIT_INVOCATION_ERROR (R1.6).
        from ralph_loop.cli import EXIT_INVOCATION_ERROR

        project = tmp_path / "scenario_b"
        _build_scripted_project(project, ["A", "B"], max_retries=2)

        invoker = _ScriptedClaudeCodeInvoker(
            raise_script={
                "A": [RuntimeError("chunk exceed the limit"), None],
            }
        )
        validator = _PassingValidator()
        git = _NoopGitManager(enabled=False, cwd=project)

        exit_code, final = await _drive_run_loop(
            project=project,
            monkeypatch=monkeypatch,
            invoker=invoker,
            validator=validator,
            git=git,
        )

        assert exit_code != EXIT_INVOCATION_ERROR
        assert exit_code == _termination_oracle(final)
        # A retried once and passed; B passed.
        by_id = {t.id: t for t in final}
        assert by_id["A"].status == "passing"
        assert by_id["B"].status == "passing"

    async def test_scenario_c_retry_cap_exhaustion_returns_exit_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # (c) one pending task A whose every invocation raises a
        # chunk-limit RuntimeError. With max_retries=2, A's retry_count
        # increments on each handler fire (R1.5 / Property 20) until it
        # reaches the cap. ``next_eligible_task`` then returns ``None``
        # while the only remaining non-passing task is "failing" with a
        # full retry budget, so ``termination_decision`` reports
        # ``"continue"`` and the loop exits via the EXIT_BLOCKED fallback.
        # The crucial invariant: never EXIT_INVOCATION_ERROR (R1.3, R1.6).
        from ralph_loop.cli import EXIT_BLOCKED, EXIT_INVOCATION_ERROR

        project = tmp_path / "scenario_c"
        _build_scripted_project(project, ["A"], max_retries=2)

        invoker = _ScriptedClaudeCodeInvoker(raise_every_call_for={"A"})
        validator = _PassingValidator()
        git = _NoopGitManager(enabled=False, cwd=project)

        exit_code, final = await _drive_run_loop(
            project=project,
            monkeypatch=monkeypatch,
            invoker=invoker,
            validator=validator,
            git=git,
        )

        assert exit_code != EXIT_INVOCATION_ERROR
        assert exit_code == EXIT_BLOCKED
        assert exit_code == _termination_oracle(final)
        # A is left in "failing" with retry_count == max_retries; the
        # selector excluded it (retry cap exhausted), the loop exited.
        only = final[0]
        assert only.id == "A"
        assert only.status == "failing"
        assert only.retry_count == 2

    async def test_scenario_d_handler_then_pass_on_different_task_returns_termination_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # (d) two pending tasks A, B. A's first (and only) invocation
        # raises a chunk-limit RuntimeError. With max_retries=1, A's
        # retry budget is exhausted after one failure and the selector
        # excludes it. B then runs and passes. Final state: A failing
        # (retry_count=1), B passing -- not all-passing, so the
        # termination decision is ``"continue"`` (B's only non-passing
        # peer is A, which has no eligible retries left) and the loop
        # exits via the EXIT_BLOCKED fallback. Per R1.6 the exit code
        # equals the termination-decision oracle and is never
        # EXIT_INVOCATION_ERROR.
        from ralph_loop.cli import EXIT_INVOCATION_ERROR

        project = tmp_path / "scenario_d"
        _build_scripted_project(project, ["A", "B"], max_retries=1)

        invoker = _ScriptedClaudeCodeInvoker(
            raise_script={
                "A": [RuntimeError("chunk exceed the limit")],
            }
        )
        validator = _PassingValidator()
        git = _NoopGitManager(enabled=False, cwd=project)

        exit_code, final = await _drive_run_loop(
            project=project,
            monkeypatch=monkeypatch,
            invoker=invoker,
            validator=validator,
            git=git,
        )

        assert exit_code != EXIT_INVOCATION_ERROR
        assert exit_code == _termination_oracle(final)
        by_id = {t.id: t for t in final}
        assert by_id["A"].status == "failing"
        assert by_id["A"].retry_count == 1
        assert by_id["B"].status == "passing"
        # A was invoked exactly once -- retry cap exhaustion prevents
        # any further selection.
        assert invoker.calls_by_task_id.get("A") == 1
