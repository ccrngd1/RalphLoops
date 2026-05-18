"""Budget and wall-clock safety controls (R10.1, R10.4-R10.7, R9.8, R1.7).

The :class:`BudgetTracker` is pure in-memory state carried for the duration
of a single ``ralph run`` invocation. It tracks four things:

* the number of iterations run so far (R10.1, R1.7),
* how many newly created tasks have been admitted *this iteration* (R10.6),
* how many newly created tasks have been admitted *in this run* (R10.7), and
* when the run started, so the wall-clock budget can be checked (R10.4, R10.5).

The Task Creation Processor calls :meth:`BudgetTracker.record_created` for
every task it admits *as a result of in-iteration creation*. Tasks admitted
from the pending queue MUST NOT pass through ``record_created`` because
R9.8 specifies that re-admitted pending tasks do not count against the
per-run creation budget of the current run.

The main loop calls :meth:`BudgetTracker.check_wall_clock` and
:meth:`BudgetTracker.check_max_iterations` each iteration to decide whether
to terminate, and :meth:`BudgetTracker.record_iteration` at the start of
each iteration to bump the iteration counter and reset the per-iteration
creation counter.
"""

from __future__ import annotations

import time
from typing import Callable

from ralph_loop.models import Config


class BudgetTracker:
    """Stateful per-run tracker for iteration, task-creation, and wall-clock budgets.

    Parameters
    ----------
    config:
        The resolved :class:`Config` whose ``max_iterations``,
        ``wall_clock_timeout_ms``, ``per_iteration_task_creation_budget``,
        and ``per_run_task_creation_budget`` fields define the budgets the
        tracker enforces.
    now:
        Injection point for the clock source. Defaults to
        :func:`time.monotonic`, which is immune to wall-clock adjustments
        and therefore the right choice for elapsed-time checks. Tests pass
        a deterministic callable so wall-clock expiry is reproducible.
    """

    def __init__(
        self,
        config: Config,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._iteration_count = 0
        self._per_iteration_created = 0
        self._per_run_created = 0
        self._now = now
        self._start_time = now()

    # -- Read-only counters -------------------------------------------------

    @property
    def iteration_count(self) -> int:
        """Number of iterations that have been recorded via ``record_iteration`` (R10.1)."""
        return self._iteration_count

    @property
    def per_iteration_created(self) -> int:
        """Tasks admitted this iteration (reset by ``record_iteration``, R10.6)."""
        return self._per_iteration_created

    @property
    def per_run_created(self) -> int:
        """Tasks admitted so far in this run (R10.7).

        Re-admitted pending-queue tasks are intentionally excluded per R9.8.
        """
        return self._per_run_created

    # -- Iteration-level transitions ---------------------------------------

    def record_iteration(self) -> None:
        """Bump the iteration counter and reset the per-iteration creation count.

        Intended to be called at the *start* of each iteration before any
        task-creation processing runs, so ``can_create_this_iteration`` is
        accurate for the new iteration.
        """
        self._iteration_count += 1
        self._per_iteration_created = 0

    def record_created(self, n: int = 1) -> None:
        """Record ``n`` newly admitted in-iteration-created tasks (R10.6, R10.7).

        Callers MUST NOT pass pending-queue admissions through here (R9.8).

        Raises
        ------
        ValueError
            If ``n`` is negative.
        """
        if n < 0:
            raise ValueError("n must be >= 0")
        self._per_iteration_created += n
        self._per_run_created += n

    # -- Budget-status queries ---------------------------------------------

    def can_create_this_iteration(self) -> bool:
        """Return ``True`` when at least one more task can be admitted this iteration (R10.6)."""
        return self._per_iteration_created < self._config.per_iteration_task_creation_budget

    def can_create_this_run(self) -> bool:
        """Return ``True`` when at least one more task can be admitted in this run (R10.7)."""
        return self._per_run_created < self._config.per_run_task_creation_budget

    def remaining_this_iteration(self) -> int:
        """How many more tasks can be admitted this iteration before spilling.

        Useful for the Task Creation Processor: it computes ``min(remaining,
        len(candidates))`` to decide how many to admit versus how many to
        spill to the pending queue for the per-iteration overflow case.
        """
        remaining = self._config.per_iteration_task_creation_budget - self._per_iteration_created
        return max(0, remaining)

    def remaining_this_run(self) -> int:
        """How many more tasks can be admitted in this run before spilling.

        The Task Creation Processor uses this to bound admissions by the
        per-run cap once the per-iteration cap has been honored (R8.11).
        """
        remaining = self._config.per_run_task_creation_budget - self._per_run_created
        return max(0, remaining)

    # -- Termination checks -------------------------------------------------

    def check_wall_clock(self) -> bool:
        """Return ``True`` when the wall-clock budget has *expired* (R10.4, R10.5).

        ``wall_clock_timeout_ms == 0`` disables the check: elapsed time can
        never trigger termination in that mode. This matches the design's
        convention that a zero timeout means "no wall-clock enforcement".
        """
        timeout_ms = self._config.wall_clock_timeout_ms
        if timeout_ms == 0:
            return False
        # Compare in seconds (the clock's native unit) rather than scaling
        # elapsed seconds up to milliseconds. The multiplication-by-1000
        # round-trip introduces float rounding error: for example,
        # ``(8011 / 1000.0) * 1000.0`` yields ``8010.999999999999``, which
        # would spuriously report "not yet expired" at the exact threshold.
        # Dividing the integer ``timeout_ms`` by 1000 is a single rounding
        # operation that matches the rounding already applied when the
        # caller advanced the clock, so equal elapsed_ms / timeout_ms
        # values compare equal as floats.
        elapsed_s = self._now() - self._start_time
        timeout_s = timeout_ms / 1000.0
        return elapsed_s >= timeout_s

    def check_max_iterations(self) -> bool:
        """Return ``True`` when the iteration cap has been reached (R10.1, R1.7)."""
        return self._iteration_count >= self._config.max_iterations
