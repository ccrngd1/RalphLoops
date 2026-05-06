"""Property-based tests for :mod:`ralph_loop.validator` (Tasks 16.4-16.6).

Three design properties land here:

- Property 13: validation-check aggregation matches per-type pass rules
  (R7.5, R7.10, R7.11, R7.12).
- Property 14: persona_review pass-condition resolution
  (R7.6, R7.7, R7.8).
- Property 15: validation timeout handling produces a failing check
  result with ``timed_out=True`` (R7.13).

All three properties are exercised against pure helpers
(``aggregate_checks`` and ``resolve_pass_condition``) or against the
shell check runner with a mocked ``asyncio.create_subprocess_exec`` so
shrinking converges quickly without launching real subprocesses.
"""

# Feature: ralph-loop, Property 13/14/15: Validator aggregation, pass-condition resolution, timeout handling

from __future__ import annotations

import asyncio
import string
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ralph_loop.models import (
    CheckResult,
    Persona,
    PersonaReviewCheckConfig,
    ShellCheckConfig,
)
from ralph_loop.validator import (
    ValidatorStuckError,
    _run_shell_check,
    aggregate_checks,
    resolve_pass_condition,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_CHECK_TYPES = ("shell", "persona_review", "file_exists")
_VERDICTS = ("pass", "fail")

# Short URL-safe alphabet so shrunk counterexamples stay readable.
_NAME_ALPHABET = string.ascii_letters + string.digits + "_-"

_name_strategy = st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=8)


@st.composite
def _check_result_strategy(draw) -> CheckResult:
    """Generate a single :class:`CheckResult` for Property 13.

    Every field is drawn independently so the generator covers the
    full Cartesian product of (type, verdict, timed_out). Rationale /
    pass-condition / reviewing-persona are left unset because
    aggregation depends only on ``verdict`` and ``timed_out``.
    """
    return CheckResult(
        type=draw(st.sampled_from(_CHECK_TYPES)),  # type: ignore[arg-type]
        name=draw(_name_strategy),
        verdict=draw(st.sampled_from(_VERDICTS)),  # type: ignore[arg-type]
        output=draw(st.text(max_size=20)),
        duration_ms=draw(st.integers(min_value=0, max_value=10_000)),
        timed_out=draw(st.booleans()),
    )


# Pass-condition strategy for Property 14. ``None`` models "condition not
# set"; strings model explicit pass conditions. Keeping the string
# alphabet narrow avoids shrinker-sensitive unicode variants that would
# not change the resolution rule.
_pass_condition_strategy = st.one_of(
    st.none(),
    st.text(alphabet=string.ascii_letters + " ", min_size=1, max_size=20),
)


# ---------------------------------------------------------------------------
# Property 13: validation-check aggregation (R7.5, R7.10, R7.11, R7.12)
# ---------------------------------------------------------------------------


class TestProperty13Aggregation:
    """Validates: Requirements 7.5, 7.10, 7.11, 7.12.

    The aggregation rule exposed by :func:`aggregate_checks` must
    satisfy, for any list of per-check outcomes:

    * ``overall == "pass"`` iff every check in the list has
      ``verdict == "pass"`` (R7.12 / R2.6).
    * ``timed_out_checks`` is exactly the list of check names whose
      ``timed_out`` flag is set, preserving the order of the input
      list (R7.13 via R7.12's "capture output in the iteration log").

    The test exercises the rule on arbitrary :class:`CheckResult`
    lists, including the empty list (vacuous ``pass``).
    """

    @given(
        results=st.lists(_check_result_strategy(), min_size=0, max_size=8)
    )
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )
    def test_overall_pass_iff_all_pass(
        self, results: list[CheckResult]
    ) -> None:
        """``overall == "pass"`` iff every check passes."""
        aggregated = aggregate_checks(results)
        expected_all_pass = all(r.verdict == "pass" for r in results)
        assert (aggregated.overall == "pass") is expected_all_pass

    @given(
        results=st.lists(_check_result_strategy(), min_size=0, max_size=8)
    )
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )
    def test_timed_out_checks_matches_names(
        self, results: list[CheckResult]
    ) -> None:
        """``timed_out_checks`` is exactly the ordered list of timed-out check names."""
        aggregated = aggregate_checks(results)
        expected = [r.name for r in results if r.timed_out]
        assert aggregated.timed_out_checks == expected

    @given(
        results=st.lists(_check_result_strategy(), min_size=0, max_size=8)
    )
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )
    def test_checks_preserved_in_order(
        self, results: list[CheckResult]
    ) -> None:
        """``aggregated.checks`` preserves the input order exactly."""
        aggregated = aggregate_checks(results)
        assert [c.name for c in aggregated.checks] == [r.name for r in results]


# ---------------------------------------------------------------------------
# Property 14: persona_review pass-condition resolution (R7.6, R7.7, R7.8)
# ---------------------------------------------------------------------------


def _persona_with_default(default: Optional[str]) -> Persona:
    return Persona(
        name="Reviewer",
        description="Reviews drafts.",
        prompt_template="You are {{persona_name}}.",
        default_persona_review_pass_condition=default,
    )


def _check_with_condition(
    spec_condition: Optional[str],
) -> PersonaReviewCheckConfig:
    return PersonaReviewCheckConfig(
        type="persona_review",
        name="review",
        persona="Reviewer",
        pass_condition=spec_condition,
    )


class TestProperty14ResolvePassCondition:
    """Validates: Requirements 7.6, 7.7, 7.8.

    The resolution rule exposed by :func:`resolve_pass_condition`:

    * Spec-level ``pass_condition`` wins when set (R7.6).
    * Otherwise the reviewing persona's
      ``default_persona_review_pass_condition`` is used (R7.7).
    * When both are ``None``, the resolver returns ``None``; the caller
      (``_run_persona_review_check``) is responsible for raising
      :class:`ValidatorStuckError` (R7.8).

    The three branches of the rule are exercised exhaustively across
    the ``(Optional[str], Optional[str])`` product via Hypothesis.
    """

    @given(
        spec_condition=_pass_condition_strategy,
        persona_default=_pass_condition_strategy,
    )
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )
    def test_resolution_matches_rule(
        self,
        spec_condition: Optional[str],
        persona_default: Optional[str],
    ) -> None:
        """Resolution is (spec_cond) if not None else (persona_default)."""
        check = _check_with_condition(spec_condition)
        persona = _persona_with_default(persona_default)
        resolved = resolve_pass_condition(check, persona)

        if spec_condition is not None:
            assert resolved == spec_condition
        elif persona_default is not None:
            assert resolved == persona_default
        else:
            assert resolved is None

    @given(
        spec_condition=st.text(
            alphabet=string.ascii_letters + " ", min_size=1, max_size=20
        ),
        persona_default=_pass_condition_strategy,
    )
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )
    def test_spec_override_always_wins_when_present(
        self, spec_condition: str, persona_default: Optional[str]
    ) -> None:
        """R7.6: a non-None spec condition always wins, regardless of the persona default."""
        check = _check_with_condition(spec_condition)
        persona = _persona_with_default(persona_default)
        assert resolve_pass_condition(check, persona) == spec_condition

    @given(
        persona_default=st.text(
            alphabet=string.ascii_letters + " ", min_size=1, max_size=20
        ),
    )
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )
    def test_persona_default_used_when_spec_omits(
        self, persona_default: str
    ) -> None:
        """R7.7: the persona default is used when the spec omits a condition."""
        check = _check_with_condition(None)
        persona = _persona_with_default(persona_default)
        assert resolve_pass_condition(check, persona) == persona_default

    def test_none_when_both_absent_signals_stuck(self) -> None:
        """R7.8: both missing -> resolver returns ``None`` so the caller can raise stuck."""
        check = _check_with_condition(None)
        persona = _persona_with_default(None)
        assert resolve_pass_condition(check, persona) is None


# ---------------------------------------------------------------------------
# Property 15: validation timeout handling (R7.13)
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Minimal async stand-in for :class:`asyncio.subprocess.Process`.

    Provides just the two methods that :func:`_run_shell_check` calls:

    * ``communicate()`` — sleeps for ``sleep_s`` seconds then returns
      ``(stdout, stderr)``. The sleep is the mechanism by which the
      test triggers the per-check timeout.
    * ``kill()`` — records that the process was killed. We don't need
      to do anything destructive here because the fake never spawns a
      real OS process.
    * ``wait()`` — returns immediately so the timeout handler's cleanup
      path doesn't hang.
    """

    def __init__(self, *, sleep_s: float, exit_code: int = 0) -> None:
        self._sleep_s = sleep_s
        self.returncode: Optional[int] = None
        self._exit_code = exit_code
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        # This sleep is what the outer ``asyncio.wait_for`` cancels when
        # the timeout expires. We use ``asyncio.sleep`` directly so the
        # Property 15 patch of ``asyncio.sleep`` controls the delay.
        await asyncio.sleep(self._sleep_s)
        self.returncode = self._exit_code
        return (b"", b"")

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -9  # killed
        return self.returncode


class TestProperty15TimeoutHandling:
    """Validates: Requirements 7.13.

    When a validation check exceeds ``validation_timeout_ms``, the
    Validator must terminate the check and produce a failing
    :class:`CheckResult` with ``timed_out=True``. This test drives the
    shell-check runner with a mocked ``asyncio.create_subprocess_exec``
    so the command "runs" for a generated duration without spawning a
    real subprocess; ``asyncio.wait_for`` decides whether the check
    completes or times out based on the generated ``timeout_ms`` and
    ``sleep_s``.
    """

    @given(
        timeout_ms=st.integers(min_value=1, max_value=50),
        sleep_multiplier=st.floats(
            min_value=2.0,
            max_value=10.0,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    @settings(
        max_examples=30,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )
    def test_timeout_produces_failing_check_with_timed_out_flag(
        self, timeout_ms: int, sleep_multiplier: float
    ) -> None:
        """A slow "subprocess" beyond ``timeout_ms`` fails with ``timed_out=True``.

        We assert the three properties of the timeout branch:

        1. ``verdict == "fail"`` (R7.13 via R7.12).
        2. ``timed_out is True``.
        3. The fake process was ``kill()``-ed before the runner returned,
           so the timeout handler actually terminates the check (not
           just records the timeout).

        ``sleep_multiplier >= 2`` guarantees the generated
        ``sleep_s = (timeout_ms * multiplier) / 1000`` is well past the
        timeout, so the outcome is deterministic regardless of the
        scheduling jitter on the Hypothesis worker.
        """
        sleep_s = (timeout_ms * sleep_multiplier) / 1000.0
        fake = _FakeProcess(sleep_s=sleep_s)

        async def fake_create_subprocess_exec(*args, **kwargs):
            return fake

        check = ShellCheckConfig(
            type="shell",
            name="slow",
            commands=["fake-cmd"],
            timeout_ms=timeout_ms,
        )

        with patch(
            "ralph_loop.validator.asyncio.create_subprocess_exec",
            new=fake_create_subprocess_exec,
        ):
            result = asyncio.run(
                _run_shell_check(check, default_timeout_ms=60_000)
            )

        assert result.verdict == "fail"
        assert result.timed_out is True
        assert fake.killed is True
        assert "timeout" in result.output.lower()

    @given(
        timeout_ms=st.integers(min_value=200, max_value=500),
    )
    @settings(
        max_examples=10,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )
    def test_fast_check_does_not_time_out(self, timeout_ms: int) -> None:
        """A subprocess that completes well within ``timeout_ms`` is not marked timed out.

        Counterpart of the first property: the timeout branch is reached
        only when the check actually exceeds ``timeout_ms``. Using a
        ``sleep_s`` of ``0`` guarantees the fake completes before
        ``wait_for`` can time out, so ``timed_out is False`` and the
        verdict is ``"pass"``.
        """
        fake = _FakeProcess(sleep_s=0.0, exit_code=0)

        async def fake_create_subprocess_exec(*args, **kwargs):
            return fake

        check = ShellCheckConfig(
            type="shell", name="fast", commands=["fake-cmd"], timeout_ms=timeout_ms
        )

        with patch(
            "ralph_loop.validator.asyncio.create_subprocess_exec",
            new=fake_create_subprocess_exec,
        ):
            result = asyncio.run(
                _run_shell_check(check, default_timeout_ms=60_000)
            )

        assert result.verdict == "pass"
        assert result.timed_out is False
        assert fake.killed is False
