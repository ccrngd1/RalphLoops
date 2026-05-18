"""Property-based tests for ``ralph_loop.budget.BudgetTracker`` (design Property 22).

These tests exercise Property 22 from ``design.md``: for any ``config``
and any sequence of iterations with controlled wall-clock advance, the
``BudgetTracker`` reports ``check_max_iterations()`` as ``True`` iff
``iteration_count >= config.max_iterations`` and reports
``check_wall_clock()`` as ``True`` iff
``wall_clock_timeout_ms > 0 and elapsed_ms >= wall_clock_timeout_ms``.
Once either check expires, it stays expired as long as time only
advances.

We drive elapsed time through an injected ``now`` callable so expiry is
fully deterministic: no real clock is consulted during these tests.

Requirements validated: 10.5, 1.7.
"""

# Feature: ralph-loop, Property 22: Wall-clock and iteration-cap termination

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from ralph_loop.budget import BudgetTracker
from ralph_loop.models import Config

from tests.strategies import budget_config_strategy


class _Clock:
    """Deterministic monotonic clock used to drive wall-clock expiry.

    ``BudgetTracker`` samples the clock at construction to record
    ``_start_time`` and again on every ``check_wall_clock`` call. By
    setting ``self.t`` directly, the test controls the elapsed time
    the tracker observes.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@given(
    config=budget_config_strategy(),
    iters=st.integers(min_value=0, max_value=30),
)
def test_max_iterations_expires_at_or_past_cap(config: Config, iters: int) -> None:
    """Validates Requirements 10.1, 1.7 (iteration-cap termination).

    ``check_max_iterations`` must return ``True`` iff the recorded
    iteration count is at or above ``config.max_iterations``. We record
    ``iters`` iterations and compare the predicate against the
    arithmetic threshold on the unmodified iteration count. Drawing
    ``iters`` up to ``30`` with ``max_iterations`` in ``[1, 20]``
    guarantees both the under-cap and at-or-past-cap branches are
    exercised routinely.
    """
    tracker = BudgetTracker(config)
    for _ in range(iters):
        tracker.record_iteration()

    assert tracker.check_max_iterations() == (iters >= config.max_iterations)
    # ``record_iteration`` must actually bump the counter.
    assert tracker.iteration_count == iters


@given(
    config=budget_config_strategy(),
    elapsed_ms=st.integers(min_value=0, max_value=120_000),
)
def test_wall_clock_expiry_matches_threshold(
    config: Config, elapsed_ms: int
) -> None:
    """Validates Requirements 10.5, 10.4 (wall-clock termination).

    With an injected deterministic clock advanced to ``elapsed_ms``:

    - ``wall_clock_timeout_ms == 0`` disables the check: the return
      value must be ``False`` regardless of how much time has elapsed
      (design convention documented in ``BudgetTracker.check_wall_clock``).
    - Otherwise the return value must equal
      ``elapsed_ms >= config.wall_clock_timeout_ms``.

    Drawing ``elapsed_ms`` up to ``120_000`` with
    ``wall_clock_timeout_ms`` capped at ``60_000`` guarantees both
    branches are exercised.
    """
    clock = _Clock()
    tracker = BudgetTracker(config, now=clock)
    clock.t = elapsed_ms / 1000.0  # advance to elapsed_ms milliseconds

    if config.wall_clock_timeout_ms == 0:
        assert tracker.check_wall_clock() is False
    else:
        assert tracker.check_wall_clock() == (
            elapsed_ms >= config.wall_clock_timeout_ms
        )


@given(
    config=budget_config_strategy(),
    first_elapsed_ms=st.integers(min_value=0, max_value=120_000),
    extra_ms=st.integers(min_value=0, max_value=120_000),
)
def test_wall_clock_expiry_is_monotonic_once_expired(
    config: Config, first_elapsed_ms: int, extra_ms: int
) -> None:
    """Once expired, wall-clock stays expired as time only advances.

    Validates the "stays expired" half of Property 22: if
    ``check_wall_clock`` first returns ``True`` at ``first_elapsed_ms``,
    any subsequent clock advance must still return ``True``. Time is
    strictly advanced (``extra_ms >= 0``), so we never move the clock
    backwards.
    """
    clock = _Clock()
    tracker = BudgetTracker(config, now=clock)

    clock.t = first_elapsed_ms / 1000.0
    first = tracker.check_wall_clock()

    clock.t = (first_elapsed_ms + extra_ms) / 1000.0
    second = tracker.check_wall_clock()

    if first:
        assert second is True


@given(
    config=budget_config_strategy(),
    iters=st.integers(min_value=0, max_value=30),
    extra_iters=st.integers(min_value=0, max_value=30),
)
def test_max_iterations_is_monotonic_once_expired(
    config: Config, iters: int, extra_iters: int
) -> None:
    """Once expired, iteration-cap stays expired as the counter only advances.

    Validates the "stays expired" half of Property 22 for the
    iteration-cap check: if ``check_max_iterations`` first returns
    ``True`` after ``iters`` iterations, recording any number of
    additional iterations must still return ``True``. The counter is
    monotonically non-decreasing (``record_iteration`` only increments),
    so we never artificially roll it back.
    """
    tracker = BudgetTracker(config)
    for _ in range(iters):
        tracker.record_iteration()
    first = tracker.check_max_iterations()

    for _ in range(extra_iters):
        tracker.record_iteration()
    second = tracker.check_max_iterations()

    if first:
        assert second is True
