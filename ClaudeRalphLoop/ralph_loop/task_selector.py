"""Task selection logic for the Ralph Loop Orchestrator.

This module implements the pure eligibility-and-priority calculation the
Orchestrator uses to pick the next Task each iteration. It is intentionally
side-effect-free: no filesystem access, no logging, no mutation of inputs.
That purity lets Property 1 (design.md) exercise it with Hypothesis over
arbitrary `list[Task]` + `Config` values.

See requirements R1.2, R2.7, R2.8, R10.3 and design "Task Selector" section.
"""

from typing import Optional

from ralph_loop.models import Config, Task, TerminationDecision


_ELIGIBLE_STATUSES: frozenset[str] = frozenset({"pending", "failing"})


def _is_eligible(task: Task, tasks_by_id: dict[str, Task], config: Config) -> bool:
    """Return True iff ``task`` satisfies the eligibility predicate.

    Eligibility (R2.7, R2.8, R10.3):
    1. ``task.status`` is one of ``{"pending", "failing"}``.
    2. ``task.retry_count < config.max_retries_per_task``.
    3. Every id in ``task.depends_on`` references a task that exists in the
       same list AND whose ``status == "passing"``.

    A missing dependency id (no task in the list has that id) makes the task
    ineligible here. Surfacing that as ``stuck`` is the Dependency Analyzer's
    job (R2.9), not the Task Selector's.
    """
    if task.status not in _ELIGIBLE_STATUSES:
        return False
    if task.retry_count >= config.max_retries_per_task:
        return False
    for dep_id in task.depends_on or []:
        dep = tasks_by_id.get(dep_id)
        if dep is None or dep.status != "passing":
            return False
    return True


def next_eligible_task(tasks: list[Task], config: Config) -> Optional[Task]:
    """Return the minimum-priority eligible task, or ``None`` if none exist.

    Eligibility is defined in :func:`_is_eligible` and mirrors Property 1 in
    ``design.md``. Among eligible tasks, the one with the numerically lowest
    ``priority`` is returned (R2.7). Ties on priority are broken by input
    order: Python's ``sorted`` is stable, so the first eligible task in the
    caller's list wins when priorities match. This keeps selection
    deterministic without imposing an id-based ordering the spec does not
    require.

    The function is pure: it does not mutate ``tasks`` or ``config``. Callers
    that need to mark missing dependencies as ``stuck`` must run the
    Dependency Analyzer separately (R2.9).
    """
    tasks_by_id: dict[str, Task] = {t.id: t for t in tasks}
    eligible = [t for t in tasks if _is_eligible(t, tasks_by_id, config)]
    if not eligible:
        return None
    eligible.sort(key=lambda t: t.priority)
    return eligible[0]


def termination_decision(tasks: list[Task]) -> TerminationDecision:
    """Decide whether the Ralph Loop should terminate or keep iterating.

    The function inspects the current ``tasks`` list (no config is needed,
    the decision is purely status-driven) and returns one of three
    :class:`~ralph_loop.models.TerminationDecision` verdicts:

    1. ``"success"`` (exit 0) when every task has status ``"passing"``
       (R1.6). An empty list trivially satisfies this predicate: there are
       no non-passing tasks left to work on, so the loop should exit
       cleanly rather than continue forever.
    2. ``"blocked"`` (exit 1) when every non-passing task is either
       ``"stuck"`` OR has at least one ``depends_on`` id that references
       a task whose status is not ``"passing"`` (R1.8). The returned
       ``blocked_ids`` list the non-passing tasks; ``blocking_dep_ids`` is
       the union of non-passing dependency ids across those blocked
       tasks, preserving the caller's first-seen order so logs are
       deterministic. A dependency id that does not resolve to any task
       in the list still counts as non-passing here: the Orchestrator
       treats such tasks as ineligible (R2.8), and the Dependency
       Analyzer independently marks them stuck (R2.9).
    3. ``"continue"`` (no exit code) when at least one non-passing task
       is neither stuck nor dependency-blocked. That task can still be
       retried, so the loop should run another iteration.

    The function is pure and side-effect-free; it does not mutate
    ``tasks`` or perform any I/O. This is the primary target of design
    Property 23.
    """
    non_passing = [t for t in tasks if t.status != "passing"]
    if not non_passing:
        # R1.6: every task (possibly none) is passing -> success.
        return TerminationDecision(verdict="success", exit_code=0)

    by_id: dict[str, Task] = {t.id: t for t in tasks}

    blocked_ids: list[str] = []
    # Use dict to preserve insertion order while de-duplicating.
    blocking_dep_ids: dict[str, None] = {}
    for task in non_passing:
        if task.status == "stuck":
            # Stuck tasks are blocked by definition (R1.8); they have no
            # associated blocking dependency ids of their own.
            blocked_ids.append(task.id)
            continue
        non_passing_deps = [
            dep_id
            for dep_id in (task.depends_on or [])
            if (dep := by_id.get(dep_id)) is None or dep.status != "passing"
        ]
        if non_passing_deps:
            blocked_ids.append(task.id)
            for dep_id in non_passing_deps:
                blocking_dep_ids.setdefault(dep_id, None)
        else:
            # This task is non-passing, not stuck, and has no blocking
            # dependency, so it can still be retried this run. The loop
            # must continue (R1.8 is falsified).
            return TerminationDecision(verdict="continue", exit_code=None)

    # Every non-passing task was either stuck or dependency-blocked.
    return TerminationDecision(
        verdict="blocked",
        exit_code=1,
        blocked_ids=blocked_ids,
        blocking_dep_ids=list(blocking_dep_ids.keys()),
    )
