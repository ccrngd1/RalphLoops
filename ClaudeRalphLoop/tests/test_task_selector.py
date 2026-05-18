"""Property-based tests for ``ralph_loop.task_selector.next_eligible_task``.

These tests exercise Property 1 from ``design.md``: for any
``list[Task]`` and any ``Config``, the task returned by
``next_eligible_task`` is either ``None`` (because no task in the list
satisfies the eligibility predicate) or a task that satisfies the
eligibility predicate AND carries the minimum ``priority`` among all
tasks that satisfy it.

Requirements validated: 1.2, 2.7, 2.8, 10.3.
"""

# Feature: ralph-loop, Property 1: Task Selector picks a minimum-priority eligible task

from __future__ import annotations

from hypothesis import given

from ralph_loop.models import Config, Task
from ralph_loop.task_selector import next_eligible_task

from tests.strategies import config_strategy, task_list_dag_strategy


def _is_eligible(task: Task, all_tasks: list[Task], config: Config) -> bool:
    """Replicate the eligibility predicate from design.md Property 1.

    This is a deliberately independent re-implementation of the
    predicate in ``ralph_loop.task_selector._is_eligible``. Keeping the
    two implementations separate is what lets the property test catch
    drift between the spec and the code: if someone changes the
    selector to allow ``in_progress`` tasks, the test fails here
    instead of silently agreeing with the bug.
    """

    if task.status not in {"pending", "failing"}:
        return False
    if task.retry_count >= config.max_retries_per_task:
        return False
    by_id = {t.id: t for t in all_tasks}
    for dep in task.depends_on or []:
        dep_task = by_id.get(dep)
        if dep_task is None or dep_task.status != "passing":
            return False
    return True


@given(tasks=task_list_dag_strategy(), config=config_strategy())
def test_task_selector_picks_minimum_priority_eligible_task(
    tasks: list[Task], config: Config
) -> None:
    """Validates: Requirements 1.2, 2.7, 2.8, 10.3.

    - R1.2 / R2.7: Orchestrator picks the lowest-priority-number
      non-passing task whose retry counter is below the limit and whose
      every ``depends_on`` references a ``passing`` task.
    - R2.8: A task is ineligible while any dependency is non-passing.
    - R10.3: A task whose ``retry_count`` has reached the per-task
      retry limit is excluded from selection.
    """

    eligible = [t for t in tasks if _is_eligible(t, tasks, config)]
    selected = next_eligible_task(tasks, config)

    if not eligible:
        # No task satisfies the predicate; selector must return None
        # (covers R10.3's "stuck" exclusion and R2.8's dependency
        # gating in the negative case).
        assert selected is None
        return

    # Some task is eligible; selector must return one, and it must be
    # both eligible and minimum-priority among the eligible set.
    assert selected is not None
    assert _is_eligible(selected, tasks, config)
    min_priority = min(t.priority for t in eligible)
    assert selected.priority == min_priority
