"""Unit tests for :class:`ralph_loop.tokens.TokenAccountant`.

Example-based coverage of the R12.1-R12.6 token-accounting rules. The
property-based tests for Property 27 (token record generation) and
Property 28 (cost computation) live in tasks 5.4 and 5.5 respectively.

Requirements validated: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6.
"""

from __future__ import annotations

import logging

import pytest

from ralph_loop.models import Config, LlmCallRecord, ModelPrice
from ralph_loop.tokens import TokenAccountant


def _config(pricing: dict[str, ModelPrice] | None = None) -> Config:
    """Build a minimal ``Config`` with optional ``model_pricing`` overrides."""

    return Config(
        fallback_persona="fallback",
        model_pricing=pricing or {},
    )


# -- Initial state ----------------------------------------------------------


def test_empty_accountant_has_zero_totals() -> None:
    """R12.5: a fresh accountant reports zero tokens and no cost or breakdowns."""
    accountant = TokenAccountant(_config())

    totals = accountant.totals()

    assert totals.total_input == 0
    assert totals.total_output == 0
    assert totals.total_combined == 0
    assert totals.total_estimated_cost is None
    assert totals.by_kind == {}


def test_empty_accountant_has_empty_call_list() -> None:
    """The accountant exposes an empty ``calls`` view before any records."""
    accountant = TokenAccountant(_config())

    assert accountant.calls == []


# -- Cost derivation from Model_Pricing ------------------------------------


def test_record_derives_cost_when_pricing_configured() -> None:
    """R12.3: when pricing is configured the accountant derives ``estimated_cost``.

    ``(input_tokens * input_price) + (output_tokens * output_price)`` is the
    formula the requirement prescribes, so 100*0.01 + 50*0.02 = 2.0.
    """
    accountant = TokenAccountant(
        _config({"gpt-test": ModelPrice(input_price_per_token=0.01, output_price_per_token=0.02)})
    )
    call = LlmCallRecord(
        kind="persona_execution",
        model="gpt-test",
        input_tokens=100,
        output_tokens=50,
    )

    accountant.record(call)

    recorded = accountant.calls[0]
    assert recorded.estimated_cost == pytest.approx(2.0)


def test_record_skips_cost_when_model_is_none() -> None:
    """R12.4: without a model identifier the accountant cannot look up pricing."""
    accountant = TokenAccountant(
        _config({"gpt-test": ModelPrice(input_price_per_token=0.01, output_price_per_token=0.02)})
    )
    call = LlmCallRecord(
        kind="persona_execution",
        model=None,
        input_tokens=100,
        output_tokens=50,
    )

    accountant.record(call)

    assert accountant.calls[0].estimated_cost is None


def test_record_skips_cost_when_pricing_unconfigured_for_model() -> None:
    """R12.4: when the model has no pricing entry, cost is omitted."""
    accountant = TokenAccountant(
        _config({"gpt-test": ModelPrice(input_price_per_token=0.01, output_price_per_token=0.02)})
    )
    call = LlmCallRecord(
        kind="persona_execution",
        model="some-other-model",
        input_tokens=100,
        output_tokens=50,
    )

    accountant.record(call)

    assert accountant.calls[0].estimated_cost is None


def test_record_preserves_precomputed_cost() -> None:
    """A caller-supplied ``estimated_cost`` is not recomputed even if pricing exists."""
    accountant = TokenAccountant(
        _config({"gpt-test": ModelPrice(input_price_per_token=0.01, output_price_per_token=0.02)})
    )
    call = LlmCallRecord(
        kind="persona_execution",
        model="gpt-test",
        input_tokens=100,
        output_tokens=50,
        estimated_cost=99.99,
    )

    accountant.record(call)

    assert accountant.calls[0].estimated_cost == pytest.approx(99.99)


def test_record_skips_cost_when_input_tokens_missing() -> None:
    """Partial token data (only output) is not enough to derive cost."""
    accountant = TokenAccountant(
        _config({"gpt-test": ModelPrice(input_price_per_token=0.01, output_price_per_token=0.02)})
    )
    call = LlmCallRecord(
        kind="persona_execution",
        model="gpt-test",
        input_tokens=None,
        output_tokens=50,
    )

    accountant.record(call)

    assert accountant.calls[0].estimated_cost is None


# -- Missing token data: warning behavior (R12.6) --------------------------


def test_record_warns_when_no_token_data(caplog: pytest.LogCaptureFixture) -> None:
    """R12.6: calls missing both token counts produce a warning identifying the kind."""
    accountant = TokenAccountant(_config())
    call = LlmCallRecord(
        kind="orchestrator_selection",
        model="gpt-test",
        input_tokens=None,
        output_tokens=None,
    )

    with caplog.at_level(logging.WARNING, logger="ralph_loop.tokens"):
        accountant.record(call)

    assert any(
        record.levelno == logging.WARNING and "orchestrator_selection" in record.getMessage()
        for record in caplog.records
    )


def test_record_still_appends_call_with_no_token_data() -> None:
    """R12.6: the call is excluded from totals but remains visible in ``calls``."""
    accountant = TokenAccountant(_config())
    call = LlmCallRecord(
        kind="planner",
        input_tokens=None,
        output_tokens=None,
    )

    accountant.record(call)

    assert len(accountant.calls) == 1
    assert accountant.calls[0].kind == "planner"


def test_record_does_not_warn_when_token_data_is_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Records with either token count present are not reported as missing data."""
    accountant = TokenAccountant(_config())
    call = LlmCallRecord(
        kind="persona_execution",
        model="gpt-test",
        input_tokens=10,
        output_tokens=0,
    )

    with caplog.at_level(logging.WARNING, logger="ralph_loop.tokens"):
        accountant.record(call)

    assert not any(
        record.levelno == logging.WARNING
        and "reported no token usage" in record.getMessage()
        for record in caplog.records
    )


# -- Aggregation (R12.5) ----------------------------------------------------


def test_totals_excludes_calls_without_token_data() -> None:
    """R12.6: a call with no token data contributes nothing to the totals."""
    accountant = TokenAccountant(_config())
    accountant.record(LlmCallRecord(kind="persona_execution", input_tokens=10, output_tokens=20))
    accountant.record(LlmCallRecord(kind="persona_execution"))

    totals = accountant.totals()

    assert totals.total_input == 10
    assert totals.total_output == 20
    assert totals.total_combined == 30


def test_totals_aggregates_same_kind_into_one_entry() -> None:
    """R12.5: multiple calls of the same kind roll up into a single ``KindTotals``."""
    accountant = TokenAccountant(_config())
    accountant.record(LlmCallRecord(kind="persona_execution", input_tokens=10, output_tokens=20))
    accountant.record(LlmCallRecord(kind="persona_execution", input_tokens=5, output_tokens=15))

    totals = accountant.totals()

    assert list(totals.by_kind.keys()) == ["persona_execution"]
    assert totals.by_kind["persona_execution"].input == 15
    assert totals.by_kind["persona_execution"].output == 35


def test_totals_separates_different_kinds() -> None:
    """R12.5: distinct call kinds keep separate ``KindTotals`` entries."""
    accountant = TokenAccountant(_config())
    accountant.record(LlmCallRecord(kind="persona_execution", input_tokens=10, output_tokens=20))
    accountant.record(LlmCallRecord(kind="orchestrator_selection", input_tokens=3, output_tokens=7))
    accountant.record(LlmCallRecord(kind="persona_review", input_tokens=4, output_tokens=8))

    totals = accountant.totals()

    assert set(totals.by_kind.keys()) == {"persona_execution", "orchestrator_selection", "persona_review"}
    assert totals.by_kind["persona_execution"].input == 10
    assert totals.by_kind["orchestrator_selection"].input == 3
    assert totals.by_kind["persona_review"].input == 4


def test_totals_sums_estimated_cost_across_priced_calls() -> None:
    """R12.5: run-level ``total_estimated_cost`` is the sum over priced records."""
    pricing = {"gpt-test": ModelPrice(input_price_per_token=0.01, output_price_per_token=0.02)}
    accountant = TokenAccountant(_config(pricing))
    accountant.record(
        LlmCallRecord(kind="persona_execution", model="gpt-test", input_tokens=100, output_tokens=50)
    )
    accountant.record(
        LlmCallRecord(kind="persona_execution", model="gpt-test", input_tokens=10, output_tokens=5)
    )

    totals = accountant.totals()

    # (100*0.01 + 50*0.02) + (10*0.01 + 5*0.02) = 2.0 + 0.2 = 2.2
    assert totals.total_estimated_cost == pytest.approx(2.2)
    assert totals.by_kind["persona_execution"].cost == pytest.approx(2.2)


def test_totals_cost_stays_none_when_no_pricing_configured() -> None:
    """R12.4: without any priced records, cost totals remain ``None`` (omitted)."""
    accountant = TokenAccountant(_config())
    accountant.record(
        LlmCallRecord(kind="persona_execution", model="no-price-model", input_tokens=100, output_tokens=50)
    )

    totals = accountant.totals()

    assert totals.total_estimated_cost is None
    assert totals.by_kind["persona_execution"].cost is None


def test_totals_total_combined_is_input_plus_output() -> None:
    """R12.5: ``total_combined`` equals ``total_input + total_output``."""
    accountant = TokenAccountant(_config())
    accountant.record(LlmCallRecord(kind="persona_execution", input_tokens=7, output_tokens=13))
    accountant.record(LlmCallRecord(kind="planner", input_tokens=1, output_tokens=2))

    totals = accountant.totals()

    assert totals.total_input == 8
    assert totals.total_output == 15
    assert totals.total_combined == 23


def test_calls_property_returns_independent_list() -> None:
    """Mutating the list returned by ``calls`` does not affect the accountant."""
    accountant = TokenAccountant(_config())
    accountant.record(LlmCallRecord(kind="persona_execution", input_tokens=1, output_tokens=1))

    snapshot = accountant.calls
    snapshot.clear()

    assert len(accountant.calls) == 1
