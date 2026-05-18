"""Property-based test for ``ralph_loop.status_update.status_after_validation`` (Property 2).

This test exercises the status-update rule declared in R2.5 and R2.6:
for any ``Task`` and any non-empty ``list[CheckResult]``, the
``(status, retry_count)`` returned by ``status_after_validation`` is
fully determined by the aggregate verdict of the checks and the
caller's pre-validation ``retry_count``.

- R2.5: every check has ``verdict == "pass"`` -> status ``"passing"``,
  ``retry_count`` unchanged.
- R2.6: at least one check has ``verdict == "fail"`` -> status
  ``"failing"``, ``retry_count`` incremented by one.

Requirements validated: 2.5, 2.6.
"""

# Feature: ralph-loop, Property 2: Task status update is determined by validation results

from __future__ import annotations

from hypothesis import given

from ralph_loop.models import CheckResult, Task
from ralph_loop.status_update import status_after_validation

from tests.strategies import check_result_list_strategy, task_with_retry_count_strategy


@given(task=task_with_retry_count_strategy(), checks=check_result_list_strategy(min_size=1))
def test_status_update_is_determined_by_verdicts(
    task: Task, checks: list[CheckResult]
) -> None:
    """Validates: Requirements 2.5, 2.6.

    For any ``Task`` with a bounded ``retry_count`` and any non-empty
    ``list[CheckResult]``:

    - If every check has ``verdict == "pass"`` (R2.5), the returned
      status is ``"passing"`` and the returned ``retry_count`` equals
      the input ``task.retry_count``.
    - Otherwise at least one check has ``verdict == "fail"`` (R2.6),
      the returned status is ``"failing"``, and the returned
      ``retry_count`` equals ``task.retry_count + 1``.

    The test also implicitly asserts determinism: a single call is
    sufficient because the function is pure -- the return value is a
    deterministic function of the two inputs.
    """

    status, retry = status_after_validation(task, checks)

    all_pass = all(c.verdict == "pass" for c in checks)
    if all_pass:
        assert status == "passing"
        assert retry == task.retry_count
    else:
        assert status == "failing"
        assert retry == task.retry_count + 1
