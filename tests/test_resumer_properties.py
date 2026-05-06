"""Property-based test for ``ralph_loop.resumer.resume`` (design Property 4).

This test exercises the resume transition: any ``list[Task]`` passed to
``Resumer.resume`` must produce a result in which every task whose
pre-resume status was ``in_progress`` is rewritten to ``failing``
(R14.3), has its ``retry_count`` preserved (R14.4), and is flagged with
``resumed_from_interruption == True`` (R14.5). Tasks that did not have
status ``in_progress`` before the call are not touched by the reset
step, and the input list is never mutated.

The test verifies Property 4 against the ``reset_tasks`` bucket of
``ResumeResult`` because that is the publicly observable pre-dep-
analysis view of the transition: any ``in_progress`` task appears
there with status ``"failing"``, regardless of whether the subsequent
dependency sweep later rewrote the same task to ``"stuck"`` (R14.6 is
covered by the logged reset count, which the caller derives from
``reset_tasks``).

Requirements validated: 14.3, 14.4, 14.5, 14.6.
"""

# Feature: ralph-loop, Property 4: Resume transition preserves retry count and flags interrupted tasks

from __future__ import annotations

from hypothesis import given

from ralph_loop.models import Task
from ralph_loop.resumer import resume

from tests.strategies import task_list_dag_strategy


@given(tasks=task_list_dag_strategy())
def test_resume_preserves_retry_count_and_flags_interrupted_tasks(
    tasks: list[Task],
) -> None:
    """Validates: Requirements 14.3, 14.4, 14.5, 14.6.

    For any task list:

    - (a) every task whose pre-resume status was ``in_progress`` appears
      in ``result.reset_tasks`` with status ``"failing"`` (R14.3). The
      reset view is the post-reset-before-dep-analysis snapshot; it is
      what the caller uses to log the reset ids (R14.6). Any such task
      may later be re-labeled ``"stuck"`` by the dep sweep, but that is
      a separate concern covered by Property 3.
    - (b) every such task has ``retry_count`` equal to its pre-resume
      ``retry_count`` (R14.4). The Resumer must never touch the retry
      counter; the interruption was not the persona's fault.
    - (c) every such task has ``resumed_from_interruption == True`` so
      the Context Composer can emit the R14.5 "previously interrupted"
      notice on the next iteration.
    - Tasks whose pre-resume status was not ``in_progress`` are not
      present in ``reset_tasks`` at all; they pass through untouched.

    The test also asserts input purity: calling ``resume`` does not
    mutate any of the input ``Task`` instances (checked via a
    representative set of fields).
    """

    # Snapshot the pre-resume state so we can compare after the call.
    pre_interrupted = [t for t in tasks if t.status == "in_progress"]
    pre_state = [
        (t.id, t.status, t.retry_count, t.resumed_from_interruption)
        for t in tasks
    ]

    result = resume(tasks)

    reset_by_id = {t.id: t for t in result.reset_tasks}

    # (a), (b), (c): every pre-in_progress task is reset correctly.
    for pre in pre_interrupted:
        assert pre.id in reset_by_id, (
            f"Pre-in_progress task {pre.id!r} missing from reset_tasks"
        )
        reset = reset_by_id[pre.id]
        # (a) status transitioned to failing (R14.3).
        assert reset.status == "failing"
        # (b) retry_count preserved (R14.4).
        assert reset.retry_count == pre.retry_count
        # (c) resumed_from_interruption flag set (R14.5).
        assert reset.resumed_from_interruption is True

    # Tasks whose pre-resume status was not in_progress must not appear
    # in reset_tasks: those entries are passed through unchanged and the
    # Resumer has no reason to log them (R14.6 only covers reset ids).
    for t in tasks:
        if t.status != "in_progress":
            assert t.id not in reset_by_id, (
                f"Non-in_progress task {t.id!r} unexpectedly reset"
            )

    # Purity: the input Task instances are never mutated. We check the
    # representative fields that the Resumer rewrites.
    post_state = [
        (t.id, t.status, t.retry_count, t.resumed_from_interruption)
        for t in tasks
    ]
    assert pre_state == post_state
