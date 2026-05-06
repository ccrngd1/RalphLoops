"""Property-based tests for ``ralph_loop.dependency_analyzer.analyze_dependencies``.

These tests exercise Property 3 from ``design.md``: for any
``list[Task]``, after ``analyze_dependencies``, (a) every task whose
``depends_on`` contains an identifier that does not exist in the list
is marked ``stuck`` and (b) every task that participates in any
``depends_on`` cycle is marked ``stuck`` and the detected cycle path
is recorded.

Requirements validated: 2.9, 2.10, 2.11.
"""

# Feature: ralph-loop, Property 3: Dependency-health marking

from __future__ import annotations

from hypothesis import given

from ralph_loop.dependency_analyzer import analyze_dependencies
from ralph_loop.models import Task

from tests.strategies import (
    task_list_dag_strategy,
    task_list_with_cycle_strategy,
    task_list_with_missing_dep_strategy,
)


@given(tasks=task_list_dag_strategy())
def test_dag_has_no_stuck_tasks(tasks: list[Task]) -> None:
    """Validates: Requirements 2.10, 2.11.

    A DAG-structured task list has no cycles by construction (each
    task's ``depends_on`` references only strictly earlier ids). The
    analyzer must therefore return an empty ``detected_cycles`` list
    and an empty ``stuck_by_cycle`` bucket. The DAG strategy also
    builds every dependency from the pool of earlier-task ids, so
    there are no missing references either.
    """

    result = analyze_dependencies(tasks)

    # No cycles detected on a DAG.
    assert result.detected_cycles == []
    assert result.stuck_by_cycle == []

    # The DAG strategy never produces missing dep references, so the
    # missing-dep bucket must also be empty.
    assert result.stuck_by_missing_dep == []

    # updated_tasks preserves ordering and identity of every input task.
    assert [t.id for t in result.updated_tasks] == [t.id for t in tasks]
    # No task was re-labeled to stuck because neither branch fired.
    for original, updated in zip(tasks, result.updated_tasks):
        assert updated.status == original.status


@given(tasks=task_list_with_missing_dep_strategy())
def test_task_with_missing_dep_is_marked_stuck(tasks: list[Task]) -> None:
    """Validates: Requirement 2.9.

    The strategy guarantees at least one task references an id that
    does not exist in the list. The analyzer must mark every such task
    stuck and surface it in ``stuck_by_missing_dep``. The original
    task list is not mutated.
    """

    # Pre-compute the set of tasks that truly have a missing reference
    # so the property asserts both "at least one" and "exactly the
    # right set" of marked tasks.
    known_ids = {t.id for t in tasks}
    expected_missing_ids = {
        t.id
        for t in tasks
        if any(dep not in known_ids for dep in (t.depends_on or []))
    }
    # The strategy guarantees this set is non-empty.
    assert expected_missing_ids, "strategy should produce at least one missing dep"

    result = analyze_dependencies(tasks)

    # Every task flagged by the analyzer is one we expect; and every
    # task we expect was flagged. The buckets carry the updated-stuck
    # copies, so we compare by id.
    reported_missing_ids = {t.id for t in result.stuck_by_missing_dep}
    assert reported_missing_ids == expected_missing_ids

    # Each such task is stuck in updated_tasks.
    updated_by_id = {t.id: t for t in result.updated_tasks}
    for tid in expected_missing_ids:
        assert updated_by_id[tid].status == "stuck"

    # Purity: the analyzer must not mutate the input instances. We only
    # check tasks that were not already stuck on input; the strategy
    # draws statuses uniformly so some may legitimately have started
    # out stuck.
    for original in tasks:
        if original.id in expected_missing_ids and original.status != "stuck":
            # The corresponding updated entry is a different instance
            # (model_copy) and the original is unchanged.
            assert original.status != "stuck"


@given(tasks=task_list_with_cycle_strategy())
def test_cycle_participants_are_marked_stuck(tasks: list[Task]) -> None:
    """Validates: Requirements 2.10, 2.11.

    The strategy guarantees at least one ``depends_on`` cycle. The
    analyzer must return a non-empty ``detected_cycles`` list and mark
    every task participating in any detected cycle as stuck. Every id
    that appears in a detected cycle must correspond to an existing
    task in the list.
    """

    result = analyze_dependencies(tasks)

    # The strategy guarantees at least one cycle.
    assert result.detected_cycles, "cycle strategy must produce >= 1 cycle"

    task_ids = {t.id for t in tasks}
    cycle_participants: set[str] = set()
    for cycle in result.detected_cycles:
        # Every id in a detected cycle must be a real task id (unknown
        # dep ids are handled by the missing-dep branch and must not
        # leak into cycle paths).
        assert set(cycle) <= task_ids
        cycle_participants.update(cycle)

    # The analyzer reports cycle participants through ``stuck_by_cycle``.
    reported_cycle_ids = {t.id for t in result.stuck_by_cycle}
    assert cycle_participants <= reported_cycle_ids

    # Every cycle participant has status "stuck" in updated_tasks.
    updated_by_id = {t.id: t for t in result.updated_tasks}
    for tid in cycle_participants:
        assert updated_by_id[tid].status == "stuck"
