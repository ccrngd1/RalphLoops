"""Property-based tests for ``ralph_loop.tokens.TokenAccountant``.

These tests exercise Properties 27 and 28 from ``design.md``:

- **Property 27** (task 5.4, R12.1, R12.2, R12.6): for any sequence of
  ``LlmCallRecord`` drawn across the five ``CallKind`` values, the
  ``TokenAccountant`` appends exactly one record per call (preserving
  ``kind``), and the aggregated ``RunTokenTotals`` sum only the token
  fields that are actually reported — records missing a token count
  contribute zero to that sum, implementing the "exclude from totals"
  rule in R12.6. The per-kind breakdown follows the same sums
  restricted to records of that kind (R12.5).

- **Property 28** (task 5.5, R12.3, R12.4, R12.5): for any sequence of
  fully-populated ``LlmCallRecord`` plus a randomized ``Model_Pricing``
  map, the run-level ``total_estimated_cost`` equals
  ``sum(input*in_price + output*out_price)`` over the records whose
  model has a pricing entry (R12.3), with unpriced models contributing
  nothing (R12.4). When no record is priced, ``total_estimated_cost``
  stays ``None`` rather than collapsing to zero, matching the
  "cost field absent" clause of R12.4.

The generators live in ``tests/strategies.py`` and are shared across
properties so shrunk counterexamples are consistent across tests.

Requirements validated: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6.
"""

# Feature: ralph-loop, Property 27 & 28: Token record generation and cost computation

from __future__ import annotations

from collections import defaultdict

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ralph_loop.models import CallKind, Config, LlmCallRecord
from ralph_loop.tokens import TokenAccountant

from tests.strategies import (
    llm_call_record_strategy,
    llm_call_record_strategy_with_tokens,
    pricing_map_strategy,
)


@given(
    calls=st.lists(llm_call_record_strategy(), min_size=0, max_size=30),
)
def test_every_recorded_call_appears_in_calls_list(
    calls: list[LlmCallRecord],
) -> None:
    """Validates: Requirements 12.1, 12.2, 12.6.

    R12.1/R12.2: every ``LlmCallRecord`` handed to
    ``TokenAccountant.record`` is retained in ``calls`` in insertion
    order with its ``kind`` preserved. One call in, one record out —
    the accountant never drops or merges records.

    R12.6: records whose token fields are absent are still retained in
    the ``calls`` list (so the per-iteration log shows the call
    happened) but contribute zero to the run-level totals. The
    expected totals therefore treat ``None`` tokens as ``0`` rather
    than as a failure signal.

    The per-kind aggregation is also checked here because R12.5
    mandates that totals are broken down by kind; the property is the
    same arithmetic restricted to records of each kind.
    """

    accountant = TokenAccountant(Config(fallback_persona="fallback"))
    for call in calls:
        accountant.record(call)

    # Every call is retained in insertion order with its ``kind`` preserved.
    assert len(accountant.calls) == len(calls)
    for recorded, original in zip(accountant.calls, calls):
        assert recorded.kind == original.kind

    totals = accountant.totals()

    # Run-level totals treat ``None`` tokens as ``0`` (R12.6: excluded).
    expected_input = sum(c.input_tokens or 0 for c in calls)
    expected_output = sum(c.output_tokens or 0 for c in calls)
    assert totals.total_input == expected_input
    assert totals.total_output == expected_output
    assert totals.total_combined == expected_input + expected_output

    # Per-kind breakdown (R12.5) is the same sum restricted to each kind.
    expected_by_kind_input: dict[CallKind, int] = defaultdict(int)
    expected_by_kind_output: dict[CallKind, int] = defaultdict(int)
    seen_kinds: set[CallKind] = set()
    for call in calls:
        seen_kinds.add(call.kind)
        expected_by_kind_input[call.kind] += call.input_tokens or 0
        expected_by_kind_output[call.kind] += call.output_tokens or 0

    # Every kind that appears at least once has an entry; kinds that
    # never appear have no entry (the accountant never fabricates empty
    # ``KindTotals`` for kinds it has not seen).
    assert set(totals.by_kind.keys()) == seen_kinds
    for kind in seen_kinds:
        assert totals.by_kind[kind].input == expected_by_kind_input[kind]
        assert totals.by_kind[kind].output == expected_by_kind_output[kind]


@given(
    calls=st.lists(llm_call_record_strategy_with_tokens(), min_size=0, max_size=30),
    pricing=pricing_map_strategy(),
)
def test_cost_matches_formula(
    calls: list[LlmCallRecord], pricing: dict
) -> None:
    """Validates: Requirements 12.3, 12.4, 12.5.

    R12.3: for every call whose model has a pricing entry, the
    derived per-call cost is
    ``input_tokens * input_price + output_tokens * output_price``. The
    run-level total (R12.5) is the sum of those per-call costs.

    R12.4: calls whose model has no pricing entry contribute nothing
    to the total. When every call is unpriced, the run-level
    ``total_estimated_cost`` is ``None`` rather than ``0.0`` — the
    field is documented as "absent" in that case, which Pydantic
    represents as ``None``.

    ``calls`` is drawn from ``llm_call_record_strategy_with_tokens``
    so every record has both token counts and a model id set. This
    keeps Property 28 focused on the cost-computation rule rather
    than rediscovering Property 27's missing-token behavior, which is
    already covered above.

    The per-kind cost breakdown is also checked because R12.5 mandates
    that totals are broken down by kind; the per-kind cost follows
    the same sum restricted to records of that kind.
    """

    config = Config(fallback_persona="fallback", model_pricing=pricing)
    accountant = TokenAccountant(config)
    for call in calls:
        accountant.record(call)

    totals = accountant.totals()

    # Compute the expected cost from the pricing map directly. Because
    # ``llm_call_record_strategy_with_tokens`` never pre-populates
    # ``estimated_cost``, the only source of derived cost is the
    # pricing map.
    expected_total: float = 0.0
    expected_by_kind_cost: dict[CallKind, float] = defaultdict(float)
    any_priced = False
    for call in calls:
        # The strategy guarantees these are not None, but an explicit
        # guard keeps the arithmetic honest if the generator changes.
        if call.model is None or call.input_tokens is None or call.output_tokens is None:
            continue
        price = pricing.get(call.model)
        if price is None:
            continue  # R12.4: unpriced model contributes nothing.
        per_call = (
            call.input_tokens * price.input_price_per_token
            + call.output_tokens * price.output_price_per_token
        )
        expected_total += per_call
        expected_by_kind_cost[call.kind] += per_call
        any_priced = True

    if any_priced:
        assert totals.total_estimated_cost == pytest.approx(expected_total)
    else:
        # R12.4: no priced record means the cost field stays absent.
        assert totals.total_estimated_cost is None

    # Per-kind cost (R12.5). Kinds that had at least one priced record
    # carry a numeric cost; kinds whose records were all unpriced carry
    # ``None`` so the output matches the top-level semantics.
    for kind, kind_totals in totals.by_kind.items():
        if kind in expected_by_kind_cost:
            assert kind_totals.cost == pytest.approx(expected_by_kind_cost[kind])
        else:
            assert kind_totals.cost is None
