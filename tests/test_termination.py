"""Unit tests for ``ralph_loop.task_selector.termination_decision``.

These are example-based smoke tests. The property-based test for
Property 23 (completion and blocked-termination decisions) is
implemented separately under task 3.4.

Requirements validated: 1.6 (all-passing -> success, exit 0) and
1.8 (every non-passing task stuck or dependency-blocked -> blocked,
non-zero exit).
"""

from __future__ import annotations

from ralph_loop.models import Task, TerminationDecision
from ralph_loop.task_selector import termination_decision


def _task(
    id_: str,
    status: str,
    *,
    depends_on: list[str] | None = None,
    priority: int = 0,
) -> Task:
    return Task(
        id=id_,
        title=f"task {id_}",
        priority=priority,
        status=status,  # type: ignore[arg-type]
        spec_path=f"specs/{id_}.md",
        depends_on=depends_on,
    )


def test_empty_list_is_success() -> None:
    """R1.6: An empty task list vacuously has every task passing."""
    result = termination_decision([])
    assert result == TerminationDecision(verdict="success", exit_code=0)


def test_all_passing_returns_success() -> None:
    """R1.6: all-passing -> success with exit code 0."""
    tasks = [_task("a", "passing"), _task("b", "passing")]
    result = termination_decision(tasks)
    assert result.verdict == "success"
    assert result.exit_code == 0
    assert result.blocked_ids == []
    assert result.blocking_dep_ids == []


def test_single_pending_no_deps_returns_continue() -> None:
    """A single actionable task must keep the loop running."""
    tasks = [_task("a", "pending")]
    result = termination_decision(tasks)
    assert result.verdict == "continue"
    assert result.exit_code is None


def test_single_failing_no_deps_returns_continue() -> None:
    """A failing task with no deps can still be retried."""
    tasks = [_task("a", "failing")]
    result = termination_decision(tasks)
    assert result.verdict == "continue"


def test_in_progress_no_deps_returns_continue() -> None:
    """An in_progress task is non-passing but has no block, so continue."""
    tasks = [_task("a", "in_progress")]
    result = termination_decision(tasks)
    assert result.verdict == "continue"


def test_all_stuck_returns_blocked() -> None:
    """R1.8: every non-passing task is stuck -> blocked."""
    tasks = [_task("a", "stuck"), _task("b", "stuck")]
    result = termination_decision(tasks)
    assert result.verdict == "blocked"
    assert result.exit_code is not None and result.exit_code != 0
    assert set(result.blocked_ids) == {"a", "b"}
    # Stuck tasks contribute no blocking dep ids of their own.
    assert result.blocking_dep_ids == []


def test_failing_task_with_non_passing_dep_is_blocked() -> None:
    """R1.8: a failing task whose dep is stuck counts as blocked."""
    tasks = [
        _task("a", "stuck"),
        _task("b", "failing", depends_on=["a"]),
    ]
    result = termination_decision(tasks)
    assert result.verdict == "blocked"
    assert result.exit_code is not None and result.exit_code != 0
    assert set(result.blocked_ids) == {"a", "b"}
    assert result.blocking_dep_ids == ["a"]


def test_mix_stuck_and_retry_able_returns_continue() -> None:
    """Some stuck + a still-retry-able failing task -> continue."""
    tasks = [
        _task("a", "stuck"),
        _task("b", "failing"),
    ]
    result = termination_decision(tasks)
    assert result.verdict == "continue"


def test_task_with_passing_dep_and_failing_status_returns_continue() -> None:
    """A failing task whose only dep is passing is still retry-able."""
    tasks = [
        _task("a", "passing"),
        _task("b", "failing", depends_on=["a"]),
    ]
    result = termination_decision(tasks)
    assert result.verdict == "continue"


def test_missing_dep_id_is_treated_as_blocking() -> None:
    """A depends_on id with no matching task is non-passing (R2.8 mirror)."""
    tasks = [_task("b", "failing", depends_on=["ghost"])]
    result = termination_decision(tasks)
    assert result.verdict == "blocked"
    assert result.blocked_ids == ["b"]
    assert result.blocking_dep_ids == ["ghost"]


def test_blocking_dep_ids_are_deduplicated_and_ordered() -> None:
    """Union of blocking deps preserves first-seen order, no dupes."""
    tasks = [
        _task("a", "stuck"),
        _task("b", "stuck"),
        _task("c", "failing", depends_on=["a", "b"]),
        _task("d", "failing", depends_on=["b", "a"]),
    ]
    result = termination_decision(tasks)
    assert result.verdict == "blocked"
    assert set(result.blocked_ids) == {"a", "b", "c", "d"}
    # a appears first (from c's deps), then b.
    assert result.blocking_dep_ids == ["a", "b"]
