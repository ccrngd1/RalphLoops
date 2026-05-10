"""Unit tests for :mod:`ralph_loop.validator` (Tasks 16.1-16.3).

Covers:

* Task 16.1 (shell, file_exists, timeouts)
* Task 16.2 (persona_review: resolved pass condition, self-review guard,
  missing pass condition stuck, unparseable verdict, timeout)
* Task 16.3 (``Validator.run`` aggregation: overall, timed_out_checks)

Requirements exercised: R7.2, R7.3, R7.4, R7.5, R7.6, R7.7, R7.8, R7.9,
R7.10, R7.11, R7.12, R7.13, R2.6.

All ``persona_review`` tests stub the Kiro invoker with an
``AsyncMock(spec=KiroInvoker)`` so the reviewing persona's "LLM call"
is deterministic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ralph_loop.kiro import KiroInvocationTimeout, KiroInvoker
from ralph_loop.models import (
    FileExistsCheckConfig,
    KiroInvocationResult,
    Persona,
    PersonaReviewCheckConfig,
    ShellCheckConfig,
    Task,
    TaskSpec,
    TaskSpecBody,
)
from ralph_loop.persona_registry import PersonaRegistry
from ralph_loop.validator import (
    PersonaReviewVerdict,
    Validator,
    ValidatorStuckError,
    _run_file_exists_check,
    _run_persona_review_check,
    _run_shell_check,
    aggregate_checks,
    resolve_pass_condition,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Portable "command exits 0" invocation: run the current Python binary
# with ``-c 'pass'`` so the tests behave identically on POSIX and Windows
# (where ``true``/``false`` are not built-in binaries).
def _cmd_exit_zero() -> str:
    return f'"{sys.executable}" -c "pass"'


def _cmd_exit_one() -> str:
    return f'"{sys.executable}" -c "import sys; sys.exit(1)"'


def _cmd_sleep(seconds: float) -> str:
    return f'"{sys.executable}" -c "import time; time.sleep({seconds})"'


def _make_persona(
    name: str,
    *,
    default_pass: str | None = None,
) -> Persona:
    return Persona(
        name=name,
        description=f"{name} persona.",
        prompt_template="You are {{persona_name}} on {{task_id}}.",
        default_persona_review_pass_condition=default_pass,
    )


def _make_registry(personas: list[Persona]) -> PersonaRegistry:
    return PersonaRegistry({p.name: p for p in personas})


def _make_task(**overrides: Any) -> Task:
    base: dict[str, Any] = dict(
        id="T1",
        title="Draft chapter",
        priority=1,
        status="in_progress",
        spec_path="specs/T1.md",
        retry_count=0,
    )
    base.update(overrides)
    return Task(**base)


def _make_spec(
    *,
    checks: list[Any] | None = None,
    context_files: list[str] | None = None,
) -> TaskSpec:
    return TaskSpec(
        id="T1",
        title="Draft chapter",
        validation=checks
        or [ShellCheckConfig(type="shell", name="noop", commands=[_cmd_exit_zero()])],
        context_files=context_files,
        body=TaskSpecBody(
            objective="Write chapter one.",
            context_references="outline v1",
            instructions="Keep it under 2000 words.",
        ),
    )


def _make_invocation_result(
    *,
    stdout: str = "",
    exit_code: int = 0,
) -> KiroInvocationResult:
    return KiroInvocationResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        token_usage=None,
        duration_ms=1,
    )


# ---------------------------------------------------------------------------
# Task 16.1: shell checks (R7.2, R7.5, R7.13)
# ---------------------------------------------------------------------------


class TestShellCheck:
    async def test_all_commands_exit_zero_passes(self) -> None:
        check = ShellCheckConfig(
            type="shell",
            name="suite",
            commands=[_cmd_exit_zero(), _cmd_exit_zero()],
        )
        result = await _run_shell_check(check, default_timeout_ms=5_000)
        assert result.verdict == "pass"
        assert result.type == "shell"
        assert result.name == "suite"
        assert result.timed_out is False

    async def test_any_non_zero_exit_fails(self) -> None:
        check = ShellCheckConfig(
            type="shell",
            name="suite",
            commands=[_cmd_exit_zero(), _cmd_exit_one()],
        )
        result = await _run_shell_check(check, default_timeout_ms=5_000)
        assert result.verdict == "fail"
        # Output records each command that ran (including the exit=1 line).
        assert "exit=1" in result.output

    async def test_first_command_non_zero_short_circuits(self) -> None:
        """Subsequent commands are not executed after a non-zero exit."""
        check = ShellCheckConfig(
            type="shell",
            name="short",
            commands=[_cmd_exit_one(), _cmd_exit_zero()],
        )
        result = await _run_shell_check(check, default_timeout_ms=5_000)
        assert result.verdict == "fail"
        # Only the failing command should appear in the captured output.
        assert result.output.count("exit=") == 1

    async def test_timeout_marks_check_timed_out(self) -> None:
        check = ShellCheckConfig(
            type="shell",
            name="slow",
            commands=[_cmd_sleep(5)],
            timeout_ms=100,
        )
        result = await _run_shell_check(check, default_timeout_ms=60_000)
        assert result.verdict == "fail"
        assert result.timed_out is True
        assert "timeout" in result.output.lower()

    async def test_default_name_when_unset(self) -> None:
        check = ShellCheckConfig(
            type="shell", commands=[_cmd_exit_zero()]
        )
        result = await _run_shell_check(check, default_timeout_ms=5_000)
        assert result.name == "shell"


# ---------------------------------------------------------------------------
# Task 16.1: file_exists checks (R7.4, R7.11)
# ---------------------------------------------------------------------------


class TestFileExistsCheck:
    async def test_all_paths_present_passes(self, tmp_path: Path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("x", encoding="utf-8")
        b.write_text("y", encoding="utf-8")

        check = FileExistsCheckConfig(
            type="file_exists",
            name="artifacts",
            paths=[str(a), str(b)],
        )
        result = await _run_file_exists_check(check)
        assert result.verdict == "pass"
        assert result.name == "artifacts"

    async def test_any_missing_path_fails(self, tmp_path: Path) -> None:
        a = tmp_path / "a.txt"
        a.write_text("x", encoding="utf-8")
        check = FileExistsCheckConfig(
            type="file_exists",
            name="artifacts",
            paths=[str(a), str(tmp_path / "missing.txt")],
        )
        result = await _run_file_exists_check(check)
        assert result.verdict == "fail"
        assert "missing.txt" in result.output

    async def test_relative_paths_resolve_against_cwd(self, tmp_path: Path) -> None:
        (tmp_path / "rel.txt").write_text("x", encoding="utf-8")
        check = FileExistsCheckConfig(
            type="file_exists", paths=["rel.txt"]
        )
        result = await _run_file_exists_check(check, cwd=tmp_path)
        assert result.verdict == "pass"

    async def test_default_name_when_unset(self) -> None:
        check = FileExistsCheckConfig(type="file_exists", paths=["README.md"])
        result = await _run_file_exists_check(check)
        # Name defaults; verdict depends on whether README exists, but name is stable.
        assert result.name == "file_exists"



# ---------------------------------------------------------------------------
# Task 16.2: persona_review (R7.3, R7.6-R7.10, R7.13)
# ---------------------------------------------------------------------------


class TestPersonaReviewCheck:
    async def test_valid_verdict_captures_rationale_and_condition(
        self, tmp_path: Path
    ) -> None:
        reviewer = _make_persona("Reviewer", default_pass="no critical issues")
        registry = _make_registry([_make_persona("Writer"), reviewer])
        check = PersonaReviewCheckConfig(
            type="persona_review",
            name="review",
            persona="Reviewer",
            pass_condition="all acceptance criteria met",
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=json.dumps(
                {"verdict": "pass", "rationale": "all criteria met"}
            )
        )

        result = await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=_make_spec(),
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
        )

        assert result.verdict == "pass"
        assert result.reviewing_persona == "Reviewer"
        # Spec override wins over the persona default (R7.6).
        assert result.resolved_pass_condition == "all acceptance criteria met"
        assert result.rationale == "all criteria met"
        assert result.type == "persona_review"
        # The invoker must be called with the persona_review call kind.
        invoker.invoke.assert_awaited_once()
        kwargs = invoker.invoke.call_args.kwargs
        assert kwargs["call_kind"] == "persona_review"

    async def test_uses_persona_default_when_spec_omits_condition(
        self, tmp_path: Path
    ) -> None:
        reviewer = _make_persona("Reviewer", default_pass="no critical issues")
        registry = _make_registry([_make_persona("Writer"), reviewer])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer", pass_condition=None
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=json.dumps({"verdict": "fail", "rationale": "typos"})
        )

        result = await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=_make_spec(),
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
        )

        # R7.7: the persona default is used when the spec omits a condition.
        assert result.resolved_pass_condition == "no critical issues"
        assert result.verdict == "fail"
        assert result.rationale == "typos"

    async def test_no_pass_condition_anywhere_raises_stuck(
        self, tmp_path: Path
    ) -> None:
        reviewer = _make_persona("Reviewer", default_pass=None)
        registry = _make_registry([_make_persona("Writer"), reviewer])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer", pass_condition=None
        )
        invoker = AsyncMock(spec=KiroInvoker)

        with pytest.raises(ValidatorStuckError) as excinfo:
            await _run_persona_review_check(
                check,
                task=_make_task(id="T-stuck"),
                spec=_make_spec(),
                executing_persona_name="Writer",
                registry=registry,
                invoker=invoker,
                log_path=tmp_path / "review.log",
                default_timeout_ms=60_000,
            )

        # R7.8: error identifies task id and reviewing persona.
        assert excinfo.value.task_id == "T-stuck"
        assert "Reviewer" in excinfo.value.reason
        # No LLM call when the task is stuck on resolution.
        invoker.invoke.assert_not_called()

    async def test_self_review_is_rejected_and_logged(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        writer = _make_persona("Writer", default_pass="no critical issues")
        registry = _make_registry([writer])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Writer"
        )
        invoker = AsyncMock(spec=KiroInvoker)

        with caplog.at_level(logging.ERROR, logger="ralph_loop.validator"):
            result = await _run_persona_review_check(
                check,
                task=_make_task(),
                spec=_make_spec(),
                executing_persona_name="Writer",  # same persona -> self-review
                registry=registry,
                invoker=invoker,
                log_path=tmp_path / "review.log",
                default_timeout_ms=60_000,
            )

        assert result.verdict == "fail"
        assert result.reviewing_persona == "Writer"
        # Recursion safety: the invoker must not be called for self-review.
        invoker.invoke.assert_not_called()
        messages = [r.getMessage() for r in caplog.records]
        assert any("Self-review" in m for m in messages)

    async def test_self_review_with_fallback_substitutes_reviewer(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """R7.9 + escalation recovery: when the reviewer matches the
        executing persona AND a distinct fallback_reviewer is
        configured, the validator swaps in the fallback so the task
        can still make progress out of the stuck state."""
        writer = _make_persona("Writer", default_pass="no critical issues")
        reviewer = _make_persona("Reviewer", default_pass="no critical issues")
        registry = _make_registry([writer, reviewer])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Writer"
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=json.dumps({"verdict": "pass", "rationale": "ok"})
        )

        with caplog.at_level(logging.WARNING, logger="ralph_loop.validator"):
            result = await _run_persona_review_check(
                check,
                task=_make_task(),
                spec=_make_spec(),
                executing_persona_name="Writer",
                registry=registry,
                invoker=invoker,
                log_path=tmp_path / "review.log",
                default_timeout_ms=60_000,
                fallback_reviewer="Reviewer",
            )

        assert result.verdict == "pass"
        # The invoker WAS called with the substitute reviewer.
        invoker.invoke.assert_awaited_once()
        # The fallback substitution is logged as a warning.
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "substituting fallback reviewer 'Reviewer'" in m for m in messages
        )

    async def test_self_review_with_matching_fallback_still_fails(
        self,
        tmp_path: Path,
    ) -> None:
        """When fallback_reviewer equals the executing persona too (i.e. the
        executing persona IS the fallback), no substitution is possible
        and the check must fail rather than recurse into self-review."""
        writer = _make_persona("Writer", default_pass="no critical issues")
        registry = _make_registry([writer])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Writer"
        )
        invoker = AsyncMock(spec=KiroInvoker)

        result = await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=_make_spec(),
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
            fallback_reviewer="Writer",  # same as executing, can't substitute
        )

        assert result.verdict == "fail"
        invoker.invoke.assert_not_called()

    async def test_context_files_are_inlined_into_review_prompt(
        self,
        tmp_path: Path,
    ) -> None:
        """Reviewer sees the actual file contents regardless of its cwd
        (fixes the 'No preface artifact was found' scenario)."""
        (tmp_path / "chapter02-preface.md").write_text(
            "Chapter 2 preface body.", encoding="utf-8"
        )
        writer = _make_persona("Writer", default_pass="present")
        reviewer = _make_persona("Reviewer", default_pass="present")
        registry = _make_registry([writer, reviewer])

        # Spec declares context_files that should be inlined.
        spec = _make_spec(
            context_files=["chapter02-preface.md"],
        )
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer",
            pass_condition="preface is present",
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=json.dumps({"verdict": "pass", "rationale": "file inlined"})
        )

        await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=spec,
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
            cwd=tmp_path,
        )

        # The prompt piped to Kiro CLI contained the file's contents.
        invoker.invoke.assert_awaited_once()
        passed_context = invoker.invoke.call_args.kwargs["context"]
        assert "Chapter 2 preface body." in passed_context
        assert "chapter02-preface.md" in passed_context
        # And the cwd was forwarded so the reviewer's own tools resolve
        # from the project root, not ralph-loop's source tree.
        assert invoker.invoke.call_args.kwargs["cwd"] == tmp_path

    async def test_missing_context_file_is_noted_not_fatal(
        self,
        tmp_path: Path,
    ) -> None:
        """A declared context_file that doesn't exist on disk is marked
        MISSING in the prompt but does not fail the check."""
        writer = _make_persona("Writer", default_pass="present")
        reviewer = _make_persona("Reviewer", default_pass="present")
        registry = _make_registry([writer, reviewer])
        spec = _make_spec(context_files=["does-not-exist.md"])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer",
            pass_condition="ok",
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=json.dumps({"verdict": "pass", "rationale": "noted"})
        )

        await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=spec,
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
            cwd=tmp_path,
        )

        passed_context = invoker.invoke.call_args.kwargs["context"]
        assert "MISSING" in passed_context
        assert "does-not-exist.md" in passed_context

    async def test_missing_reviewing_persona_raises_stuck(
        self, tmp_path: Path
    ) -> None:
        registry = _make_registry([_make_persona("Writer")])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Ghost"
        )
        invoker = AsyncMock(spec=KiroInvoker)

        with pytest.raises(ValidatorStuckError) as excinfo:
            await _run_persona_review_check(
                check,
                task=_make_task(id="T-ghost"),
                spec=_make_spec(),
                executing_persona_name="Writer",
                registry=registry,
                invoker=invoker,
                log_path=tmp_path / "review.log",
                default_timeout_ms=60_000,
            )
        assert "Ghost" in excinfo.value.reason
        invoker.invoke.assert_not_called()

    async def test_timeout_marks_timed_out(self, tmp_path: Path) -> None:
        reviewer = _make_persona("Reviewer", default_pass="no issues")
        registry = _make_registry([_make_persona("Writer"), reviewer])
        check = PersonaReviewCheckConfig(
            type="persona_review",
            name="slow-review",
            persona="Reviewer",
            timeout_ms=50,
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.side_effect = KiroInvocationTimeout("timeout")

        result = await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=_make_spec(),
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
        )
        assert result.verdict == "fail"
        assert result.timed_out is True
        assert result.reviewing_persona == "Reviewer"
        assert result.resolved_pass_condition == "no issues"

    async def test_unparseable_verdict_marks_fail(self, tmp_path: Path) -> None:
        reviewer = _make_persona("Reviewer", default_pass="no issues")
        registry = _make_registry([_make_persona("Writer"), reviewer])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer"
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout="I could not decide."
        )

        result = await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=_make_spec(),
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
        )
        assert result.verdict == "fail"
        assert result.reviewing_persona == "Reviewer"
        assert result.resolved_pass_condition == "no issues"
        assert result.timed_out is False

    async def test_verdict_wrapped_in_prose_is_extracted(
        self, tmp_path: Path
    ) -> None:
        reviewer = _make_persona("Reviewer", default_pass="no issues")
        registry = _make_registry([_make_persona("Writer"), reviewer])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer"
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=(
                "Here is my review:\n"
                '{"verdict": "pass", "rationale": "looks good"}\n'
                "Done."
            )
        )

        result = await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=_make_spec(),
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
        )
        assert result.verdict == "pass"
        assert result.rationale == "looks good"

    # ------------------------------------------------------------------
    # Bugfix regression tests for `persona-review-verdict-parsing`
    # (scenarios 1.1–1.5 from design.md).
    # ------------------------------------------------------------------

    async def test_verdict_in_markdown_fence_is_parsed(
        self, tmp_path: Path
    ) -> None:
        """Scenario 1.1: reviewer wraps verdict in ```json ... ``` fence."""
        reviewer = _make_persona("Reviewer", default_pass="no issues")
        registry = _make_registry([_make_persona("Writer"), reviewer])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer"
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout='```json\n{"verdict": "pass", "rationale": "ok"}\n```'
        )

        result = await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=_make_spec(),
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
        )
        assert result.verdict == "pass"
        assert result.rationale == "ok"

    async def test_leading_tool_use_envelope_is_skipped(
        self, tmp_path: Path
    ) -> None:
        """Scenario 1.2: tool-use JSON precedes the actual verdict."""
        reviewer = _make_persona("Reviewer", default_pass="no issues")
        registry = _make_registry([_make_persona("Writer"), reviewer])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer"
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=(
                '{"tool":"read_file","args":{"path":"x"}}\n'
                '{"verdict":"pass","rationale":"ok"}'
            )
        )

        result = await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=_make_spec(),
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
        )
        assert result.verdict == "pass"
        assert result.rationale == "ok"

    async def test_literal_close_brace_in_rationale_is_parsed(
        self, tmp_path: Path
    ) -> None:
        """Scenario 1.3: rationale string contains a literal ``}``."""
        reviewer = _make_persona("Reviewer", default_pass="no issues")
        registry = _make_registry([_make_persona("Writer"), reviewer])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer"
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout='{"verdict":"fail","rationale":"missing } in expression"}'
        )

        result = await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=_make_spec(),
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
        )
        assert result.verdict == "fail"
        assert result.rationale == "missing } in expression"

    async def test_escaped_quotes_around_brace_in_rationale(
        self, tmp_path: Path
    ) -> None:
        """Scenario 1.4: rationale contains escaped quotes around a brace."""
        reviewer = _make_persona("Reviewer", default_pass="no issues")
        registry = _make_registry([_make_persona("Writer"), reviewer])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer"
        )
        invoker = AsyncMock(spec=KiroInvoker)
        # The raw JSON source: {"verdict":"fail","rationale":"saw \"{\" unexpected"}
        invoker.invoke.return_value = _make_invocation_result(
            stdout=r'{"verdict":"fail","rationale":"saw \"{\" unexpected"}'
        )

        result = await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=_make_spec(),
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
        )
        assert result.verdict == "fail"
        assert result.rationale == 'saw "{" unexpected'

    async def test_multiple_objects_verdict_is_not_first(
        self, tmp_path: Path
    ) -> None:
        """Scenario 1.5: multiple JSON objects, verdict is not the first."""
        reviewer = _make_persona("Reviewer", default_pass="no issues")
        registry = _make_registry([_make_persona("Writer"), reviewer])
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer"
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=(
                '{"progress":1}\n'
                '{"tool":"read_file","args":{}}\n'
                '{"verdict":"pass","rationale":"ok"}'
            )
        )

        result = await _run_persona_review_check(
            check,
            task=_make_task(),
            spec=_make_spec(),
            executing_persona_name="Writer",
            registry=registry,
            invoker=invoker,
            log_path=tmp_path / "review.log",
            default_timeout_ms=60_000,
        )
        assert result.verdict == "pass"
        assert result.rationale == "ok"



# ---------------------------------------------------------------------------
# Pure helpers: aggregate_checks, resolve_pass_condition
# ---------------------------------------------------------------------------


class TestAggregateChecks:
    def test_empty_list_aggregates_to_pass(self) -> None:
        result = aggregate_checks([])
        assert result.overall == "pass"
        assert result.timed_out_checks == []

    def test_all_pass_aggregates_to_pass(self) -> None:
        from ralph_loop.models import CheckResult

        results = [
            CheckResult(
                type="shell", name="a", verdict="pass", output="", duration_ms=1
            ),
            CheckResult(
                type="file_exists",
                name="b",
                verdict="pass",
                output="",
                duration_ms=1,
            ),
        ]
        result = aggregate_checks(results)
        assert result.overall == "pass"
        assert result.timed_out_checks == []

    def test_any_fail_aggregates_to_fail(self) -> None:
        from ralph_loop.models import CheckResult

        results = [
            CheckResult(
                type="shell", name="a", verdict="pass", output="", duration_ms=1
            ),
            CheckResult(
                type="shell", name="b", verdict="fail", output="", duration_ms=1
            ),
        ]
        result = aggregate_checks(results)
        assert result.overall == "fail"
        assert result.timed_out_checks == []

    def test_timed_out_names_surface(self) -> None:
        from ralph_loop.models import CheckResult

        results = [
            CheckResult(
                type="shell",
                name="slow",
                verdict="fail",
                output="",
                duration_ms=1,
                timed_out=True,
            ),
            CheckResult(
                type="shell", name="ok", verdict="pass", output="", duration_ms=1
            ),
        ]
        result = aggregate_checks(results)
        assert result.overall == "fail"
        assert result.timed_out_checks == ["slow"]


class TestResolvePassCondition:
    def test_spec_override_wins(self) -> None:
        persona = _make_persona("Reviewer", default_pass="persona default")
        check = PersonaReviewCheckConfig(
            type="persona_review",
            persona="Reviewer",
            pass_condition="spec override",
        )
        assert resolve_pass_condition(check, persona) == "spec override"

    def test_persona_default_when_spec_omits(self) -> None:
        persona = _make_persona("Reviewer", default_pass="persona default")
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer"
        )
        assert resolve_pass_condition(check, persona) == "persona default"

    def test_none_when_both_absent(self) -> None:
        persona = _make_persona("Reviewer", default_pass=None)
        check = PersonaReviewCheckConfig(
            type="persona_review", persona="Reviewer"
        )
        assert resolve_pass_condition(check, persona) is None


# ---------------------------------------------------------------------------
# Task 16.3: Validator.run aggregation (R7.10, R7.11, R7.12, R2.6)
# ---------------------------------------------------------------------------


class TestValidatorRun:
    async def test_all_pass_yields_overall_pass(
        self, tmp_path: Path
    ) -> None:
        present = tmp_path / "artifact.txt"
        present.write_text("x", encoding="utf-8")
        spec = _make_spec(
            checks=[
                ShellCheckConfig(type="shell", name="s", commands=[_cmd_exit_zero()]),
                FileExistsCheckConfig(
                    type="file_exists", name="f", paths=[str(present)]
                ),
            ]
        )
        validator = Validator(
            invoker=AsyncMock(spec=KiroInvoker),
            registry=_make_registry([_make_persona("Writer")]),
        )
        result = await validator.run(
            task=_make_task(),
            spec=spec,
            executing_persona_name="Writer",
            log_path=tmp_path / "validation.log",
        )
        assert result.overall == "pass"
        assert result.timed_out_checks == []
        assert [c.name for c in result.checks] == ["s", "f"]

    async def test_any_fail_yields_overall_fail(
        self, tmp_path: Path
    ) -> None:
        spec = _make_spec(
            checks=[
                ShellCheckConfig(type="shell", name="ok", commands=[_cmd_exit_zero()]),
                ShellCheckConfig(type="shell", name="bad", commands=[_cmd_exit_one()]),
            ]
        )
        validator = Validator(
            invoker=AsyncMock(spec=KiroInvoker),
            registry=_make_registry([_make_persona("Writer")]),
        )
        result = await validator.run(
            task=_make_task(),
            spec=spec,
            executing_persona_name="Writer",
            log_path=tmp_path / "validation.log",
        )
        assert result.overall == "fail"
        assert result.timed_out_checks == []

    async def test_timed_out_check_populates_timed_out_checks(
        self, tmp_path: Path
    ) -> None:
        spec = _make_spec(
            checks=[
                ShellCheckConfig(
                    type="shell",
                    name="slow",
                    commands=[_cmd_sleep(5)],
                    timeout_ms=100,
                ),
            ]
        )
        validator = Validator(
            invoker=AsyncMock(spec=KiroInvoker),
            registry=_make_registry([_make_persona("Writer")]),
        )
        result = await validator.run(
            task=_make_task(),
            spec=spec,
            executing_persona_name="Writer",
            log_path=tmp_path / "validation.log",
        )
        assert result.overall == "fail"
        assert result.timed_out_checks == ["slow"]

    async def test_mixed_checks_run_in_spec_order(
        self, tmp_path: Path
    ) -> None:
        reviewer = _make_persona("Reviewer", default_pass="no issues")
        registry = _make_registry([_make_persona("Writer"), reviewer])
        spec = _make_spec(
            checks=[
                ShellCheckConfig(type="shell", name="s", commands=[_cmd_exit_zero()]),
                PersonaReviewCheckConfig(
                    type="persona_review", name="r", persona="Reviewer"
                ),
            ]
        )
        invoker = AsyncMock(spec=KiroInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=json.dumps({"verdict": "pass", "rationale": "clean"})
        )
        validator = Validator(invoker=invoker, registry=registry)
        result = await validator.run(
            task=_make_task(),
            spec=spec,
            executing_persona_name="Writer",
            log_path=tmp_path / "validation.log",
        )
        assert result.overall == "pass"
        assert [c.type for c in result.checks] == ["shell", "persona_review"]

    async def test_stuck_condition_propagates_from_persona_review(
        self, tmp_path: Path
    ) -> None:
        """R7.8: missing pass condition anywhere -> ValidatorStuckError bubbles up."""
        reviewer = _make_persona("Reviewer", default_pass=None)
        registry = _make_registry([_make_persona("Writer"), reviewer])
        spec = _make_spec(
            checks=[
                PersonaReviewCheckConfig(
                    type="persona_review",
                    persona="Reviewer",
                    pass_condition=None,
                )
            ]
        )
        validator = Validator(
            invoker=AsyncMock(spec=KiroInvoker), registry=registry
        )
        with pytest.raises(ValidatorStuckError):
            await validator.run(
                task=_make_task(),
                spec=spec,
                executing_persona_name="Writer",
                log_path=tmp_path / "validation.log",
            )
