"""Unit tests for ``ralph_loop.dependency_analyzer.analyze_dependencies``.

These cover the five scenarios called out in the task description:

- No ``depends_on`` relationships (no-op pass-through).
- A task with a ``depends_on`` id that does not exist (R2.9).
- A simple two-node cycle (R2.10, R2.11).
- A three-node cycle (R2.10, R2.11).
- A self-loop (R2.10, R2.11).

The property-based coverage for the analyzer lives in a separate file
(task 4.2, Property 3). These tests pin concrete, small examples so the
behaviour is easy to read at a glance and so regressions surface with
immediate, minimal reproductions.
"""

from __future__ import annotations

from ralph_loop.dependency_analyzer import analyze_dependencies
from ralph_loop.models import Task


def _task(
    id_: str,
    *,
    status: str = "pending",
    depends_on: list[str] | None = None,
) -> Task:
    """Build a ``Task`` with sensible defaults for analyzer tests.

    Only the fields the analyzer cares about (``id``, ``status``,
    ``depends_on``) are parameterised; everything else gets filler
    values that satisfy the model's validators.
    """

    return Task(
        id=id_,
        title=f"task {id_}",
        priority=0,
        status=status,  # type: ignore[arg-type]
        spec_path=f"specs/{id_}.md",
        retry_count=0,
        depends_on=depends_on,
    )


def test_no_dependencies_leaves_tasks_unchanged() -> None:
    """Tasks without any ``depends_on`` are passed through as-is."""

    tasks = [_task("a"), _task("b"), _task("c")]
    result = analyze_dependencies(tasks)

    assert result.detected_cycles == []
    assert result.stuck_by_missing_dep == []
    assert result.stuck_by_cycle == []
    # No tasks were mutated; the updated list preserves identity.
    assert [t.id for t in result.updated_tasks] == ["a", "b", "c"]
    assert all(t.status == "pending" for t in result.updated_tasks)


def test_missing_dependency_marks_referrer_stuck() -> None:
    """R2.9: unknown dep ids force the referring task to ``"stuck"``."""

    a = _task("a", depends_on=["ghost"])  # 'ghost' is not in the list
    b = _task("b")
    result = analyze_dependencies([a, b])

    # No cycles, since the missing id is not a node in the graph.
    assert result.detected_cycles == []
    assert result.stuck_by_cycle == []

    # 'a' is reported as stuck by missing dep; 'b' is untouched.
    assert [t.id for t in result.stuck_by_missing_dep] == ["a"]
    updated_by_id = {t.id: t for t in result.updated_tasks}
    assert updated_by_id["a"].status == "stuck"
    assert updated_by_id["b"].status == "pending"

    # Purity: the original task instances must not be mutated.
    assert a.status == "pending"


def test_two_node_cycle_marks_both_participants_stuck() -> None:
    """R2.10 / R2.11: A -> B -> A produces one cycle of length 2."""

    a = _task("a", depends_on=["b"])
    b = _task("b", depends_on=["a"])
    result = analyze_dependencies([a, b])

    # Exactly one cycle; path contains both ids (order preserved per DFS).
    assert len(result.detected_cycles) == 1
    assert sorted(result.detected_cycles[0]) == ["a", "b"]

    # Both tasks are cycle participants and both are stuck.
    assert {t.id for t in result.stuck_by_cycle} == {"a", "b"}
    assert all(t.status == "stuck" for t in result.updated_tasks)
    # Neither is reported under missing-dep.
    assert result.stuck_by_missing_dep == []


def test_three_node_cycle_marks_all_participants_stuck() -> None:
    """R2.10 / R2.11: A -> B -> C -> A produces one cycle of length 3."""

    a = _task("a", depends_on=["b"])
    b = _task("b", depends_on=["c"])
    c = _task("c", depends_on=["a"])
    result = analyze_dependencies([a, b, c])

    assert len(result.detected_cycles) == 1
    assert sorted(result.detected_cycles[0]) == ["a", "b", "c"]
    assert {t.id for t in result.stuck_by_cycle} == {"a", "b", "c"}
    assert all(t.status == "stuck" for t in result.updated_tasks)


def test_self_loop_marks_task_stuck() -> None:
    """R2.11: A -> A produces a single-node cycle and marks A stuck."""

    a = _task("a", depends_on=["a"])
    b = _task("b")
    result = analyze_dependencies([a, b])

    # Self-loop surfaces as a cycle with exactly one id.
    assert result.detected_cycles == [["a"]]
    assert [t.id for t in result.stuck_by_cycle] == ["a"]

    updated_by_id = {t.id: t for t in result.updated_tasks}
    assert updated_by_id["a"].status == "stuck"
    assert updated_by_id["b"].status == "pending"


def test_cycle_and_missing_dep_reported_in_both_buckets() -> None:
    """A task with both a missing dep and a cycle edge is in both buckets."""

    # a -> b (known, forms cycle), a -> ghost (missing).
    a = _task("a", depends_on=["b", "ghost"])
    b = _task("b", depends_on=["a"])
    result = analyze_dependencies([a, b])

    assert [t.id for t in result.stuck_by_missing_dep] == ["a"]
    assert {t.id for t in result.stuck_by_cycle} == {"a", "b"}

    # The updated list contains each task at most once even though 'a'
    # qualifies under both buckets.
    assert [t.id for t in result.updated_tasks] == ["a", "b"]
    assert all(t.status == "stuck" for t in result.updated_tasks)


def test_already_stuck_task_is_not_re_copied_but_still_reported() -> None:
    """Idempotence: a task already marked stuck is kept as-is and reported."""

    a = _task("a", status="stuck", depends_on=["a"])  # self-loop, already stuck
    result = analyze_dependencies([a])

    assert result.detected_cycles == [["a"]]
    assert [t.id for t in result.stuck_by_cycle] == ["a"]
    # The returned task is the same instance (no unnecessary copy).
    assert result.updated_tasks[0] is a
    assert result.updated_tasks[0].status == "stuck"


def test_input_task_list_is_not_mutated() -> None:
    """Purity: the analyzer returns fresh instances; input state is preserved."""

    a = _task("a", depends_on=["b"])
    b = _task("b", depends_on=["a"])
    pre_status = [t.status for t in (a, b)]
    analyze_dependencies([a, b])
    assert [t.status for t in (a, b)] == pre_status
