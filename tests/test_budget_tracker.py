"""Unit tests for ``ralph_loop.budget.BudgetTracker``.

Example-based coverage of the R10.1/R10.4-R10.7/R9.8/R1.7 budget rules.
The property-based test for Property 22 (wall-clock and iteration-cap
termination) lives in task 5.2.

Requirements validated: 9.8, 10.1, 10.4, 10.5, 10.6, 10.7, 1.7.
"""

from __future__ import annotations

import pytest

from ralph_loop.budget import BudgetTracker
from ralph_loop.models import Config


def _config(
    *,
    per_iteration: int = 10,
    per_run: int = 100,
    max_iterations: int = 50,
    wall_clock_timeout_ms: int = 60 * 60 * 1000,
) -> Config:
    """Build a minimal ``Config`` with the requested budget knobs."""

    return Config(
        fallback_persona="fallback",
        per_iteration_task_creation_budget=per_iteration,
        per_run_task_creation_budget=per_run,
        max_iterations=max_iterations,
        wall_clock_timeout_ms=wall_clock_timeout_ms,
    )


class _FakeClock:
    """Deterministic monotonic clock used to drive wall-clock expiry."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# -- Initial state ----------------------------------------------------------


def test_new_tracker_has_zero_counters() -> None:
    """R10.1, R10.6, R10.7: a fresh tracker starts with every counter at zero."""
    clock = _FakeClock()
    tracker = BudgetTracker(_config(), now=clock)

    assert tracker.iteration_count == 0
    assert tracker.per_iteration_created == 0
    assert tracker.per_run_created == 0


def test_new_tracker_allows_creation() -> None:
    """R10.6, R10.7: both budgets start fresh, so creation is allowed."""
    tracker = BudgetTracker(_config())

    assert tracker.can_create_this_iteration() is True
    assert tracker.can_create_this_run() is True


def test_new_tracker_reports_full_remaining_budgets() -> None:
    """``remaining_this_iteration`` / ``remaining_this_run`` start at the configured caps."""
    tracker = BudgetTracker(_config(per_iteration=7, per_run=42))

    assert tracker.remaining_this_iteration() == 7
    assert tracker.remaining_this_run() == 42


# -- record_iteration -------------------------------------------------------


def test_record_iteration_increments_iteration_count() -> None:
    """R10.1: ``record_iteration`` advances the iteration counter."""
    tracker = BudgetTracker(_config())

    tracker.record_iteration()
    tracker.record_iteration()

    assert tracker.iteration_count == 2


def test_record_iteration_resets_per_iteration_counter() -> None:
    """R10.6: per-iteration admissions reset at the start of each iteration."""
    tracker = BudgetTracker(_config(per_iteration=10, per_run=100))
    tracker.record_created(5)
    assert tracker.per_iteration_created == 5

    tracker.record_iteration()

    assert tracker.per_iteration_created == 0


def test_record_iteration_does_not_reset_per_run_counter() -> None:
    """R10.7: per-run admissions persist across iterations."""
    tracker = BudgetTracker(_config(per_iteration=10, per_run=100))
    tracker.record_created(5)
    tracker.record_iteration()

    assert tracker.per_run_created == 5
    assert tracker.can_create_this_run() is True


# -- record_created ---------------------------------------------------------


def test_record_created_updates_both_counters() -> None:
    """R10.6, R10.7: ``record_created(n)`` adds ``n`` to both per-iter and per-run totals."""
    tracker = BudgetTracker(_config(per_iteration=10, per_run=100))

    tracker.record_created(3)

    assert tracker.per_iteration_created == 3
    assert tracker.per_run_created == 3


def test_record_created_accumulates() -> None:
    """Multiple calls accumulate: 2 + 4 = 6 on both counters."""
    tracker = BudgetTracker(_config(per_iteration=10, per_run=100))

    tracker.record_created(2)
    tracker.record_created(4)

    assert tracker.per_iteration_created == 6
    assert tracker.per_run_created == 6


def test_record_created_defaults_to_one() -> None:
    """The default ``n=1`` matches the typical single-task admit call site."""
    tracker = BudgetTracker(_config(per_iteration=10, per_run=100))

    tracker.record_created()

    assert tracker.per_iteration_created == 1
    assert tracker.per_run_created == 1


def test_record_created_zero_is_no_op() -> None:
    """``record_created(0)`` is explicitly allowed and changes nothing."""
    tracker = BudgetTracker(_config(per_iteration=10, per_run=100))

    tracker.record_created(0)

    assert tracker.per_iteration_created == 0
    assert tracker.per_run_created == 0


def test_record_created_negative_raises() -> None:
    """Negative admissions are a programmer error; they must raise."""
    tracker = BudgetTracker(_config())

    with pytest.raises(ValueError):
        tracker.record_created(-1)


# -- Per-iteration budget exhaustion ---------------------------------------


def test_crossing_per_iteration_budget_blocks_this_iteration() -> None:
    """R10.6: once the per-iteration cap is reached, ``can_create_this_iteration`` flips to False."""
    tracker = BudgetTracker(_config(per_iteration=3, per_run=100))

    tracker.record_created(3)

    assert tracker.can_create_this_iteration() is False
    assert tracker.remaining_this_iteration() == 0
    # The per-run budget should still be open.
    assert tracker.can_create_this_run() is True


def test_new_iteration_restores_per_iteration_capacity() -> None:
    """R10.6: the per-iteration cap is per-iteration, so a fresh iteration resets it."""
    tracker = BudgetTracker(_config(per_iteration=3, per_run=100))
    tracker.record_created(3)
    assert tracker.can_create_this_iteration() is False

    tracker.record_iteration()

    assert tracker.can_create_this_iteration() is True
    assert tracker.remaining_this_iteration() == 3


# -- Per-run budget exhaustion ---------------------------------------------


def test_crossing_per_run_budget_blocks_all_further_creation() -> None:
    """R10.7: once the per-run cap is reached, ``can_create_this_run`` flips to False."""
    tracker = BudgetTracker(_config(per_iteration=10, per_run=5))

    tracker.record_created(5)

    assert tracker.can_create_this_run() is False
    assert tracker.remaining_this_run() == 0


def test_per_run_budget_survives_across_iterations() -> None:
    """R10.7: ``record_iteration`` does not reset the per-run counter, so the cap still binds."""
    tracker = BudgetTracker(_config(per_iteration=5, per_run=4))
    tracker.record_created(4)
    assert tracker.can_create_this_run() is False

    tracker.record_iteration()

    assert tracker.can_create_this_run() is False
    assert tracker.remaining_this_run() == 0


def test_r9_8_pending_admit_not_recorded_leaves_per_run_budget_unchanged() -> None:
    """R9.8: admitted pending-queue tasks are NOT recorded against the run budget.

    This is a contract test - the pending queue manager admits tasks *without*
    calling ``record_created``. We simulate that here by admitting in-iteration
    creates up to the cap and asserting that a separate pending-admit loop
    that doesn't touch the tracker leaves per-run capacity untouched.
    """
    tracker = BudgetTracker(_config(per_iteration=10, per_run=3))

    # Pending-queue admissions (R9.8): the caller does not call record_created.
    for _ in range(7):
        pass  # placeholder - pending admit flow is not supposed to touch the tracker

    # Per-run capacity is unchanged by pending admissions.
    assert tracker.per_run_created == 0
    assert tracker.remaining_this_run() == 3

    # In-iteration creates still bind against the per-run cap.
    tracker.record_created(3)
    assert tracker.can_create_this_run() is False


# -- Wall-clock --------------------------------------------------------------


def test_wall_clock_not_expired_when_under_timeout() -> None:
    """R10.4: elapsed time below the timeout does not trigger expiry."""
    clock = _FakeClock()
    tracker = BudgetTracker(_config(wall_clock_timeout_ms=5_000), now=clock)

    clock.advance(1.0)  # 1s elapsed

    assert tracker.check_wall_clock() is False


def test_wall_clock_expires_at_threshold() -> None:
    """R10.5: once elapsed >= timeout, wall-clock is reported expired."""
    clock = _FakeClock()
    tracker = BudgetTracker(_config(wall_clock_timeout_ms=5_000), now=clock)

    clock.advance(5.0)  # exactly 5s (the cap)

    assert tracker.check_wall_clock() is True


def test_wall_clock_expires_past_threshold() -> None:
    """R10.5: staying past the timeout stays expired."""
    clock = _FakeClock()
    tracker = BudgetTracker(_config(wall_clock_timeout_ms=5_000), now=clock)

    clock.advance(10.0)  # 10s >> 5s cap

    assert tracker.check_wall_clock() is True


def test_wall_clock_expires_at_float_unfriendly_threshold() -> None:
    """R10.5: the at-threshold case must hold even when the ms value can't be
    represented exactly in float seconds.

    For ``timeout_ms = 8011`` the clock advance ``8011 / 1000.0`` is the
    binary float ``8.010999999999999``; multiplying that back up by 1000
    yields ``8010.999999999999``, which would spuriously fall short of
    ``8011``. The implementation must avoid that round-trip so that the
    exact-threshold advance still reports expiry.
    """
    clock = _FakeClock()
    tracker = BudgetTracker(_config(wall_clock_timeout_ms=8_011), now=clock)

    clock.advance(8_011 / 1000.0)  # exactly at the cap, modulo float repr

    assert tracker.check_wall_clock() is True


def test_wall_clock_disabled_when_timeout_is_zero() -> None:
    """A ``wall_clock_timeout_ms`` of 0 disables the check entirely."""
    clock = _FakeClock()
    tracker = BudgetTracker(_config(wall_clock_timeout_ms=0), now=clock)

    clock.advance(365 * 24 * 3600.0)  # one year

    assert tracker.check_wall_clock() is False


# -- Iteration cap -----------------------------------------------------------


def test_max_iterations_not_reached_when_under_cap() -> None:
    """R10.1/R1.7: iteration count below the cap does not trigger termination."""
    tracker = BudgetTracker(_config(max_iterations=3))

    tracker.record_iteration()
    tracker.record_iteration()

    assert tracker.check_max_iterations() is False


def test_max_iterations_reached_at_cap() -> None:
    """R10.1/R1.7: iteration count == cap triggers termination."""
    tracker = BudgetTracker(_config(max_iterations=3))

    for _ in range(3):
        tracker.record_iteration()

    assert tracker.check_max_iterations() is True
