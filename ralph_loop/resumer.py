"""Run resumption and in-progress task recovery (R14.3-R14.6, R2.9-R2.11).

The Resumer is the pure-function component that runs at startup to bring
the on-disk Task_List back into a safe, loop-ready state before the
first iteration is scheduled. Two concerns overlap here:

1. **In-progress recovery** (R14.3, R14.4, R14.5, R14.6): a prior run
   may have been killed or crashed while a task was marked
   ``"in_progress"``. That status is the loop's signal that work was
   actively happening; on resume it is not a valid terminal state, so
   every ``"in_progress"`` task is rewritten to ``"failing"`` without
   incrementing its ``retry_count`` (the interruption was not the
   persona's fault, per R14.4) and is tagged with
   ``resumed_from_interruption=True`` so the Context Composer can add
   the R14.5 "previously interrupted" notice on the next iteration.

2. **Dependency health sweep** (R2.9, R2.10, R2.11): the loop also runs
   the dependency analyzer at startup so any missing ``depends_on``
   target or cycle participant gets re-marked ``"stuck"`` before task
   selection begins. This is the same analyzer that the Task Creation
   Processor reuses after every Task_Creation_Event, which keeps the
   "stuck" marking rule identical in both entry points.

The two steps run in order: reset first, analyze second. Running
analysis on the post-reset list means an ``in_progress`` task that also
happens to sit in a cycle or have a missing dep will surface as
``"stuck"`` in the final output. That is the intended behaviour: a
stuck task cannot be selected anyway, so the resume flag is effectively
informational while "stuck" remains the dominant status.

The module is a pure function: it performs no I/O, does no logging, and
never mutates the input list. Fresh ``Task`` instances are produced via
``task.model_copy(update=...)``. Input order is preserved throughout.
Logging of the reset count and ids (R14.6) is the caller's
responsibility and is wired up in the main run loop.
"""

from __future__ import annotations

from ralph_loop.dependency_analyzer import analyze_dependencies
from ralph_loop.models import ResumeResult, Task


def resume(tasks: list[Task]) -> ResumeResult:
    """Run startup recovery over a Task_List snapshot.

    The sequence mirrors the design's "Resumption on startup" sequence
    diagram:

    1. Copy every ``"in_progress"`` task to ``"failing"`` with
       ``retry_count`` unchanged and ``resumed_from_interruption=True``
       (R14.3, R14.4, R14.5). Tasks in any other status pass through
       unchanged, and the list order is preserved.
    2. Hand the post-reset list to
       :func:`ralph_loop.dependency_analyzer.analyze_dependencies` so
       that missing-dep referrers (R2.9) and cycle participants (R2.10,
       R2.11) are re-labeled ``"stuck"``. A task that was reset in step
       1 and sits in a cycle becomes stuck here; the
       ``resumed_from_interruption`` flag set in step 1 is preserved
       because ``analyze_dependencies`` only updates ``status``.
    3. Package the results as a :class:`ResumeResult`. The
       ``detected_cycle`` field is the singular first detected cycle
       (empty list when no cycle exists); the analyzer returns every
       cycle it finds via ``detected_cycles``, and the Resumer picks
       the first one for logging to match the ``ResumeResult`` shape
       documented in the design.

    Args:
        tasks: The Task_List loaded from ``tasks.json``. Not mutated.

    Returns:
        A :class:`ResumeResult` containing the reset tasks, the
        dependency-health stuck buckets, and the first detected cycle
        path.
    """

    # Step 1: reset in_progress -> failing without touching retry_count.
    # Build the post-reset list and remember which tasks were reset so
    # we can return them in ``reset_tasks``. The copy uses
    # ``model_copy(update=...)`` so the input Task instances stay
    # untouched (purity requirement - see Property 4 in design.md).
    post_reset: list[Task] = []
    reset_tasks: list[Task] = []
    for task in tasks:
        if task.status == "in_progress":
            reset = task.model_copy(
                update={
                    "status": "failing",
                    "resumed_from_interruption": True,
                }
            )
            post_reset.append(reset)
            reset_tasks.append(reset)
        else:
            post_reset.append(task)

    # Step 2: dependency health sweep on the post-reset list. The
    # analyzer is pure and preserves list order, so downstream code can
    # rely on ``updated_tasks`` aligning with the input ordering.
    analysis = analyze_dependencies(post_reset)

    # Step 3: pick the first detected cycle (if any) for logging. The
    # design's ``ResumeResult.detected_cycle`` is the singular
    # first-detected cycle; the analyzer returns every cycle it finds,
    # so taking the head matches the contract.
    detected_cycle: list[str] = (
        list(analysis.detected_cycles[0]) if analysis.detected_cycles else []
    )

    return ResumeResult(
        reset_tasks=reset_tasks,
        stuck_by_missing_dep=list(analysis.stuck_by_missing_dep),
        stuck_by_cycle_tasks=list(analysis.stuck_by_cycle),
        detected_cycle=detected_cycle,
    )
