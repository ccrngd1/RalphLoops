"""Post-validation status update rule (R2.5, R2.6).

Given the current :class:`Task` and the :class:`CheckResult` list produced by
the Validator for that iteration, compute the next ``(status, retry_count)``
tuple. This is a pure function with no dependency on the Task Selector or any
I/O: the loop simply overwrites the task's status and retry counter with the
returned values and persists the task list.

The rule, lifted straight from the acceptance criteria and design doc:

- R2.5: when every :class:`CheckResult` has ``verdict == "pass"``, the task's
  next status is ``"passing"`` and ``retry_count`` is unchanged.
- R2.6: when at least one check fails, the task's next status is ``"failing"``
  and ``retry_count`` increments by one.

Empty-check handling
--------------------

Design Property 2 is stated over a *non-empty* ``list[CheckResult]``, which
leaves the empty case ambiguous. In practice every ``Task_Spec`` must declare
at least one validation check (R18.1 / ``TaskSpec.validation`` has
``min_length=1``), so the Validator never produces an empty list for a
well-formed spec. As a safety default we treat an empty list as a failure
(status ``"failing"``, ``retry_count + 1``) rather than silently promoting the
task to ``"passing"`` on no evidence. Callers that reach this branch have
almost certainly hit a bug elsewhere and should be re-tried.
"""

from __future__ import annotations

from ralph_loop.models import CheckResult, Task, TaskStatus


def status_after_validation(
    task: Task, checks: list[CheckResult]
) -> tuple[TaskStatus, int]:
    """Return the next ``(status, retry_count)`` for ``task``.

    Pure function; does not mutate ``task`` or ``checks``.

    Parameters
    ----------
    task:
        The task whose validation just completed. Only ``retry_count`` is
        read; other fields are ignored.
    checks:
        The :class:`CheckResult` list produced by the Validator for this
        iteration.

    Returns
    -------
    tuple[TaskStatus, int]
        ``("passing", task.retry_count)`` when ``checks`` is non-empty and
        every entry has ``verdict == "pass"`` (R2.5); otherwise
        ``("failing", task.retry_count + 1)`` (R2.6, plus the empty-list
        safety default documented above).
    """
    if checks and all(c.verdict == "pass" for c in checks):
        return ("passing", task.retry_count)
    return ("failing", task.retry_count + 1)
