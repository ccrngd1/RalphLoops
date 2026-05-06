"""Unit tests for ``ralph_loop.status_update.status_after_validation``.

Example-based coverage of the R2.5 / R2.6 status-update rule. The
property-based test for Property 2 (status-update determinism) lives in
task 4.6.

Requirements validated: R2.5 (all-pass -> ``"passing"``, retry preserved)
and R2.6 (any-fail -> ``"failing"``, retry incremented).
"""

from __future__ import annotations

from ralph_loop.models import CheckResult, Task
from ralph_loop.status_update import status_after_validation


def _task(retry_count: int = 0) -> Task:
    """Build a minimal ``Task`` with the requested retry counter."""

    return Task(
        id="a",
        title="task a",
        priority=0,
        status="in_progress",
        spec_path="specs/a.md",
        retry_count=retry_count,
    )


def _check(verdict: str, *, name: str = "c", type_: str = "shell") -> CheckResult:
    """Build a minimal ``CheckResult`` with a given verdict."""

    return CheckResult(
        type=type_,  # type: ignore[arg-type]
        name=name,
        verdict=verdict,  # type: ignore[arg-type]
        output="",
        duration_ms=0,
    )


def test_all_pass_returns_passing_with_retry_unchanged() -> None:
    """R2.5: every check passes -> status ``passing``, retry unchanged."""

    task = _task(retry_count=2)
    checks = [_check("pass"), _check("pass", name="d"), _check("pass", name="e")]

    status, retry = status_after_validation(task, checks)

    assert status == "passing"
    assert retry == 2


def test_single_pass_returns_passing_with_retry_unchanged() -> None:
    """A single passing check still yields ``passing``."""

    task = _task(retry_count=0)
    status, retry = status_after_validation(task, [_check("pass")])

    assert status == "passing"
    assert retry == 0


def test_one_fail_among_passes_returns_failing_and_increments() -> None:
    """R2.6: any fail flips the aggregate to ``failing`` and retry += 1."""

    task = _task(retry_count=1)
    checks = [_check("pass"), _check("fail", name="d"), _check("pass", name="e")]

    status, retry = status_after_validation(task, checks)

    assert status == "failing"
    assert retry == 2


def test_all_fail_returns_failing_and_increments() -> None:
    """R2.6: every check failing still increments by exactly one."""

    task = _task(retry_count=3)
    checks = [_check("fail"), _check("fail", name="d")]

    status, retry = status_after_validation(task, checks)

    assert status == "failing"
    assert retry == 4


def test_empty_checks_returns_failing_and_increments() -> None:
    """Empty-check safety default: treat as failure rather than silent pass."""

    task = _task(retry_count=0)

    status, retry = status_after_validation(task, [])

    assert status == "failing"
    assert retry == 1


def test_input_task_is_not_mutated() -> None:
    """Purity: the caller's ``Task`` instance is untouched."""

    task = _task(retry_count=2)
    pre = (task.status, task.retry_count)

    status_after_validation(task, [_check("fail")])

    assert (task.status, task.retry_count) == pre
