"""Unit tests for ``ralph_loop.resumer.resume``.

These cover the concrete scenarios called out by the task description:

- Empty list passes through cleanly.
- A list with no ``in_progress`` tasks still runs the dependency
  analyzer but produces an empty ``reset_tasks`` bucket.
- A single ``in_progress`` task is rewritten to ``failing`` with
  ``retry_count`` preserved and ``resumed_from_interruption=True``
  (R14.3, R14.4, R14.5).
- Multiple ``in_progress`` tasks are all reset.
- An ``in_progress`` task with a missing ``depends_on`` target ends up
  ``stuck`` after the dependency sweep (R2.9).
- An ``in_progress`` task that participates in a cycle ends up
  ``stuck`` after the dependency sweep (R2.10, R2.11) and the detected
  cycle path is returned.
- The input task list is never mutated.

Property-based coverage for this module lives in task 4.4 (Property 4).
"""

from __future__ import annotations

from ralph_loop.models import Task
from ralph_loop.resumer import resume


def _task(
    id_: str,
    *,
    status: str = "pending",
    retry_count: int = 0,
    depends_on: list[str] | None = None,
    resumed_from_interruption: bool | None = None,
) -> Task:
    """Build a ``Task`` with sensible defaults for Resumer tests."""

    return Task(
        id=id_,
        title=f"task {id_}",
        priority=0,
        status=status,  # type: ignore[arg-type]
        spec_path=f"specs/{id_}.md",
        retry_count=retry_count,
        depends_on=depends_on,
        resumed_from_interruption=resumed_from_interruption,
    )


def test_empty_task_list_returns_empty_result() -> None:
    """An empty Task_List yields an empty ResumeResult."""

    result = resume([])

    assert result.reset_tasks == []
    assert result.stuck_by_missing_dep == []
    assert result.stuck_by_cycle_tasks == []
    assert result.detected_cycle == []


def test_no_in_progress_tasks_leaves_everything_alone() -> None:
    """Without any ``in_progress`` task, ``reset_tasks`` is empty."""

    a = _task("a", status="pending")
    b = _task("b", status="passing")
    c = _task("c", status="failing", retry_count=2)
    d = _task("d", status="stuck")

    result = resume([a, b, c, d])

    # No resets happened.
    assert result.reset_tasks == []
    # Dependency analyzer still ran but found nothing.
    assert result.stuck_by_missing_dep == []
    assert result.stuck_by_cycle_tasks == []
    assert result.detected_cycle == []


def test_single_in_progress_task_is_reset_without_incrementing_retry() -> None:
    """R14.3, R14.4, R14.5: reset to ``failing``, retry preserved, flag set."""

    a = _task("a", status="in_progress", retry_count=2)
    b = _task("b", status="passing")

    result = resume([a, b])

    assert len(result.reset_tasks) == 1
    reset = result.reset_tasks[0]
    assert reset.id == "a"
    assert reset.status == "failing"
    assert reset.retry_count == 2  # unchanged per R14.4
    assert reset.resumed_from_interruption is True  # R14.5

    # ``b`` is not in any stuck bucket and was not reset.
    assert result.stuck_by_missing_dep == []
    assert result.stuck_by_cycle_tasks == []


def test_multiple_in_progress_tasks_are_all_reset() -> None:
    """Every ``in_progress`` task is reset independently."""

    a = _task("a", status="in_progress", retry_count=0)
    b = _task("b", status="pending")
    c = _task("c", status="in_progress", retry_count=4)

    result = resume([a, b, c])

    ids = {t.id for t in result.reset_tasks}
    assert ids == {"a", "c"}

    reset_by_id = {t.id: t for t in result.reset_tasks}
    assert reset_by_id["a"].status == "failing"
    assert reset_by_id["a"].retry_count == 0
    assert reset_by_id["a"].resumed_from_interruption is True
    assert reset_by_id["c"].status == "failing"
    assert reset_by_id["c"].retry_count == 4
    assert reset_by_id["c"].resumed_from_interruption is True


def test_in_progress_with_missing_dep_becomes_stuck_after_analysis() -> None:
    """Reset runs first, then the analyzer marks the task ``stuck`` (R2.9)."""

    # 'ghost' does not exist in the list.
    a = _task("a", status="in_progress", retry_count=1, depends_on=["ghost"])

    result = resume([a])

    # The task still shows up in reset_tasks since it WAS in_progress.
    assert len(result.reset_tasks) == 1
    assert result.reset_tasks[0].id == "a"
    # reset_tasks reflects the post-reset view (before dep analysis),
    # so status is "failing" and the flag is set. Retry is preserved.
    assert result.reset_tasks[0].status == "failing"
    assert result.reset_tasks[0].retry_count == 1
    assert result.reset_tasks[0].resumed_from_interruption is True

    # The dep analyzer re-marked the task stuck; the updated entry
    # shows up under stuck_by_missing_dep with status "stuck" and the
    # interruption flag preserved (only status was updated).
    assert len(result.stuck_by_missing_dep) == 1
    stuck = result.stuck_by_missing_dep[0]
    assert stuck.id == "a"
    assert stuck.status == "stuck"
    assert stuck.retry_count == 1
    assert stuck.resumed_from_interruption is True

    assert result.stuck_by_cycle_tasks == []
    assert result.detected_cycle == []


def test_in_progress_in_cycle_becomes_stuck_and_cycle_reported() -> None:
    """An ``in_progress`` task in a cycle is reset then marked stuck (R2.10, R2.11)."""

    # a -> b -> a forms a 2-cycle. 'a' starts in_progress, 'b' pending.
    a = _task("a", status="in_progress", retry_count=0, depends_on=["b"])
    b = _task("b", status="pending", depends_on=["a"])

    result = resume([a, b])

    # ``a`` was reset first.
    reset_ids = [t.id for t in result.reset_tasks]
    assert reset_ids == ["a"]
    assert result.reset_tasks[0].resumed_from_interruption is True

    # Both tasks participate in the cycle and end up stuck.
    cycle_ids = {t.id for t in result.stuck_by_cycle_tasks}
    assert cycle_ids == {"a", "b"}

    # Updated cycle entries carry status "stuck"; the resumed flag
    # survives on ``a`` since analyze_dependencies only updates status.
    stuck_by_id = {t.id: t for t in result.stuck_by_cycle_tasks}
    assert stuck_by_id["a"].status == "stuck"
    assert stuck_by_id["a"].resumed_from_interruption is True
    assert stuck_by_id["b"].status == "stuck"
    assert stuck_by_id["b"].resumed_from_interruption is None

    # The first detected cycle path is reported; order follows the
    # analyzer's DFS recursion stack.
    assert sorted(result.detected_cycle) == ["a", "b"]


def test_self_loop_on_in_progress_task_detected_as_cycle() -> None:
    """A self-loop on an ``in_progress`` task surfaces as a single-id cycle."""

    a = _task("a", status="in_progress", retry_count=3, depends_on=["a"])

    result = resume([a])

    # Still counted as reset.
    assert [t.id for t in result.reset_tasks] == ["a"]
    # Cycle path is [a]; single participant.
    assert result.detected_cycle == ["a"]
    assert [t.id for t in result.stuck_by_cycle_tasks] == ["a"]
    assert result.stuck_by_cycle_tasks[0].status == "stuck"
    assert result.stuck_by_cycle_tasks[0].retry_count == 3


def test_input_task_list_is_not_mutated() -> None:
    """Purity: input Task instances are untouched."""

    a = _task("a", status="in_progress", retry_count=2)
    b = _task("b", status="in_progress", retry_count=0, depends_on=["b"])
    c = _task("c", status="pending", depends_on=["ghost"])

    pre = [
        (t.id, t.status, t.retry_count, t.resumed_from_interruption)
        for t in (a, b, c)
    ]

    resume([a, b, c])

    post = [
        (t.id, t.status, t.retry_count, t.resumed_from_interruption)
        for t in (a, b, c)
    ]
    assert pre == post


def test_reset_tasks_preserve_input_order_when_interleaved() -> None:
    """``reset_tasks`` follows the input order of the ``in_progress`` entries."""

    tasks = [
        _task("a", status="pending"),
        _task("b", status="in_progress"),
        _task("c", status="passing"),
        _task("d", status="in_progress"),
        _task("e", status="failing"),
    ]

    result = resume(tasks)

    assert [t.id for t in result.reset_tasks] == ["b", "d"]
