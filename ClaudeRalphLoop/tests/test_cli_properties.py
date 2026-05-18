"""Property-based tests for the ``ralph_loop.cli`` Invocation_Error handler.

This module hosts Property 20 from the
``resilient-invocation-and-context-truncation`` spec:

    Invocation_Error converges to Iteration_Failure via the same rule
    as a failing check.

The handler under test is :func:`ralph_loop.cli._handle_invocation_error`.
Its persisted ``(status, retry_count)`` pair is asserted against the
output of :func:`ralph_loop.status_update.status_after_validation` on the
same task and a synthetic failing :class:`CheckResult`. The oracle is
``status_after_validation`` itself, so the property exercises the claim
that the handler never invents a new transition rule.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from hypothesis import given, settings, strategies as st

from ralph_loop.atomic_io import atomic_write_bytes
from ralph_loop.cli import _handle_invocation_error, _load_tasks
from ralph_loop.claude_code import ClaudeCodeInvocationTimeout
from ralph_loop.models import TASK_LIST_ADAPTER, CheckResult, Task
from ralph_loop.status_update import status_after_validation


# ---------------------------------------------------------------------------
# Hypothesis strategy: random (task, exception) pair
# ---------------------------------------------------------------------------


# Alphabet restricted to letters and digits so shrunk counterexamples stay
# readable and so ``Task`` string fields (``id``, ``spec_path``) never trip
# on surrogate codepoints that break JSON round-tripping.
_ID_ALPHABET = st.characters(whitelist_categories=("Ll", "Lu", "Nd"))


@st.composite
def task_and_exception(draw):
    """Generate ``(Task, Exception)`` pairs for Property 20.

    The task carries a non-terminal status (``pending``, ``failing``,
    ``in_progress``) and an arbitrary ``retry_count`` in ``[0, 10]``.
    The exception is drawn from the four types that ``invoker.invoke``
    can raise in practice: a bare ``RuntimeError`` and ``ValueError``,
    the project-specific ``ClaudeCodeInvocationTimeout``, and the standard
    library ``subprocess.CalledProcessError`` (which also carries
    ``stdout`` / ``stderr`` attributes the handler inspects).
    """

    task = draw(
        st.builds(
            Task,
            id=st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=8),
            title=st.text(min_size=1, max_size=16),
            priority=st.integers(min_value=0, max_value=10),
            status=st.sampled_from(["pending", "failing", "in_progress"]),
            spec_path=st.just("specs/x.md"),
            retry_count=st.integers(min_value=0, max_value=10),
        )
    )
    msg = draw(st.text(max_size=200))
    stderr_text = draw(st.text(max_size=200))
    stdout_text = draw(st.text(max_size=200))
    exc_choice = draw(
        st.sampled_from(["runtime", "value", "timeout", "calledprocess"])
    )
    exc: Exception
    if exc_choice == "runtime":
        exc = RuntimeError(msg)
    elif exc_choice == "value":
        exc = ValueError(msg)
    elif exc_choice == "timeout":
        # ``ClaudeCodeInvocationTimeout`` is a plain ``Exception`` subclass; it
        # accepts any positional message string. Fall back to a literal
        # ``"timeout"`` so ``str(exc)`` is never empty.
        exc = ClaudeCodeInvocationTimeout(msg or "timeout")
    else:
        exc = subprocess.CalledProcessError(
            returncode=1,
            cmd="kiro",
            output=stdout_text,
            stderr=stderr_text,
        )
    return task, exc


# ---------------------------------------------------------------------------
# Property 20
# ---------------------------------------------------------------------------


# Feature: resilient-invocation-and-context-truncation, Property 20:
# Invocation_Error converges to Iteration_Failure via the same rule as a
# failing check. Validates: R1.1, R1.5, R3.2, R3.3.
@given(task_and_exception())
@settings(max_examples=200, deadline=None)
def test_property_20_handler_matches_status_after_validation(pair):
    """Handler's persisted ``(status, retry)`` equals the oracle output.

    The oracle is ``status_after_validation`` applied to the same task
    with a single synthetic failing ``CheckResult``. Any exception raised
    out of ``invoker.invoke`` must route through the same transition rule
    that governs a failing ``persona_review`` check, so the handler never
    invents a new rule (Property 20 / R3.2, R3.3).
    """

    task, exc = pair

    # Use ``tempfile.TemporaryDirectory`` rather than pytest's
    # function-scoped ``tmp_path`` fixture because Hypothesis warns
    # against mixing function-scoped fixtures with ``@given``: the same
    # directory would be reused across generated examples and stale
    # state could leak between cases.
    with tempfile.TemporaryDirectory() as d:
        tasks_path = Path(d) / "tasks.json"
        atomic_write_bytes(tasks_path, TASK_LIST_ADAPTER.dump_json([task]))

        # Oracle: any failing ``CheckResult`` yields the same
        # ``(status, retry)`` output from ``status_after_validation``.
        oracle_status, oracle_retry = status_after_validation(
            task,
            [
                CheckResult(
                    type="shell",
                    name="x",
                    verdict="fail",
                    output="",
                    duration_ms=0,
                )
            ],
        )

        _handle_invocation_error(
            exc=exc,
            task=task,
            persona_name="P",
            tasks=[task],
            tasks_path=tasks_path,
        )

        reloaded = _load_tasks(tasks_path)
        assert len(reloaded) == 1
        assert reloaded[0].id == task.id
        assert reloaded[0].status == oracle_status
        assert reloaded[0].retry_count == oracle_retry
