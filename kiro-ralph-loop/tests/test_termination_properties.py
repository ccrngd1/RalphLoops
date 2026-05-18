"""Property-based test for ``termination_decision`` (design Property 23).

This test exercises the three-way classification in
``ralph_loop.task_selector.termination_decision``: every ``list[Task]``
must resolve to exactly one of ``success``, ``blocked``, or
``continue`` per R1.6 and R1.8. We verify the verdict by replicating
the decision rules independently (so the property test catches drift
between the spec and the code) and then asserting the structural
invariants on the returned ``TerminationDecision``.

Requirements validated: 1.6, 1.8.
"""

# Feature: ralph-loop, Property 23: Completion and blocked-termination decisions

from __future__ import annotations

from hypothesis import given

from ralph_loop.models import Task
from ralph_loop.task_selector import termination_decision

from tests.strategies import task_list_dag_strategy


def _expected_verdict(tasks: list[Task]) -> str:
    """Independent replica of the termination-decision rules.

    Mirrors the acceptance criteria verbatim:

    - R1.6: every task passing (including the empty list) -> ``success``.
    - R1.8: every non-passing task is either ``stuck`` OR has at least
      one ``depends_on`` id whose referenced task is not ``passing``
      (missing referenced ids count as non-passing, matching R2.8) ->
      ``blocked``.
    - Otherwise at least one non-passing task can still be retried ->
      ``continue``.

    Keeping this logic independent from the implementation in
    ``task_selector.termination_decision`` is what lets the property
    test detect regressions in either direction: if someone loosened
    the ``blocked`` rule to include non-passing-with-passing-deps,
    the check on the final branch would fail here without silently
    agreeing with the bug.
    """
    non_passing = [t for t in tasks if t.status != "passing"]
    if not non_passing:
        return "success"

    by_id = {t.id: t for t in tasks}
    for task in non_passing:
        if task.status == "stuck":
            continue
        has_non_passing_dep = any(
            (by_id.get(dep_id) is None or by_id[dep_id].status != "passing")
            for dep_id in (task.depends_on or [])
        )
        if not has_non_passing_dep:
            # Non-passing, not stuck, and every dep is passing: still
            # retry-able, so the loop must continue (R1.8 falsified).
            return "continue"
    # Every non-passing task is either stuck or dep-blocked (R1.8).
    return "blocked"


@given(tasks=task_list_dag_strategy())
def test_termination_decision_matches_spec(tasks: list[Task]) -> None:
    """Validates: Requirements 1.6, 1.8.

    For any DAG-structured ``list[Task]``:

    - The verdict equals the independently-computed expected verdict,
      so the three cases (``success``, ``blocked``, ``continue``) are
      mutually exclusive and cover the input space (design P23).
    - ``success`` carries ``exit_code == 0`` and no blocked ids (R1.6).
    - ``blocked`` carries a non-zero ``exit_code`` AND the set of
      ``blocked_ids`` exactly equals the set of non-passing task ids
      (R1.8), AND every id in ``blocking_dep_ids`` comes from some
      task's ``depends_on`` list (either pointing at an existing task
      or at a missing reference).
    """
    expected = _expected_verdict(tasks)
    result = termination_decision(tasks)

    assert result.verdict == expected

    if result.verdict == "success":
        # R1.6: exit 0 and no blocked metadata when everything passes.
        assert result.exit_code == 0
        assert result.blocked_ids == []
        assert result.blocking_dep_ids == []
        return

    if result.verdict == "continue":
        # The loop is not terminating, so no exit code is set.
        assert result.exit_code is None
        return

    # verdict == "blocked"
    assert result.exit_code is not None and result.exit_code != 0

    # R1.8: blocked_ids must exactly enumerate the non-passing tasks.
    non_passing_ids = {t.id for t in tasks if t.status != "passing"}
    assert set(result.blocked_ids) == non_passing_ids
    # blocked_ids should not repeat ids; every reported id is a real
    # task id (we don't fabricate blocked ids out of dep strings).
    assert len(result.blocked_ids) == len(set(result.blocked_ids))

    # Every blocking dep id is either a task id in the list or a
    # string referenced in some task's depends_on. In practice the
    # implementation only draws from depends_on edges of non-passing,
    # non-stuck tasks, so any id there must be a string that appeared
    # in the union of task ids and declared dep references.
    task_ids = {t.id for t in tasks}
    declared_dep_refs: set[str] = set()
    for task in tasks:
        for dep_id in task.depends_on or []:
            declared_dep_refs.add(dep_id)
    allowed_blocking = task_ids | declared_dep_refs
    assert set(result.blocking_dep_ids) <= allowed_blocking
    # Reported blocking_dep_ids should also be de-duplicated.
    assert len(result.blocking_dep_ids) == len(set(result.blocking_dep_ids))
