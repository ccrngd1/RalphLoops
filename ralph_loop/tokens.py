"""Token usage recording and cost aggregation (R12.1-R12.6).

The :class:`TokenAccountant` is a stateful per-run accumulator. Every call
site that interacts with an LLM - Persona execution iterations, Orchestrator
persona-selection calls, ``persona_review`` validation checks, Planner
invocations, and Escalation-persona invocations - produces exactly one
:class:`~ralph_loop.models.LlmCallRecord` and hands it to
:meth:`TokenAccountant.record` (R12.1, R12.2).

The accountant performs two responsibilities:

* Derive ``estimated_cost`` from configured ``Model_Pricing`` when the call
  reports both input and output tokens and the record does not already
  carry a pre-computed cost (R12.3, R12.4).
* Aggregate the records into a :class:`~ralph_loop.models.RunTokenTotals`
  object for the end-of-run summary log (R12.5), with per-kind
  breakdowns and calls without token data excluded from totals with a
  logged warning (R12.6).
"""

from __future__ import annotations

import logging
from typing import Optional

from ralph_loop.models import (
    CallKind,
    Config,
    KindTotals,
    LlmCallRecord,
    RunTokenTotals,
)

logger = logging.getLogger(__name__)


class TokenAccountant:
    """Stateful accumulator of :class:`LlmCallRecord` with cost derivation.

    Parameters
    ----------
    config:
        The resolved :class:`~ralph_loop.models.Config`. Only the
        ``model_pricing`` map is read; all other fields are ignored. The
        accountant holds a reference to the config so that pricing
        lookups always reflect the current run's configuration without
        any copying.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._calls: list[LlmCallRecord] = []

    @property
    def calls(self) -> list[LlmCallRecord]:
        """Snapshot of recorded calls in insertion order.

        A fresh list is returned on every access to prevent callers from
        mutating internal state. The returned ``LlmCallRecord`` instances
        reflect any ``estimated_cost`` derivation performed by
        :meth:`record`.
        """
        return list(self._calls)

    def record(self, call: LlmCallRecord) -> None:
        """Append ``call`` to the run's record, deriving cost when possible.

        Behavior follows the R12 acceptance criteria:

        * If both ``input_tokens`` and ``output_tokens`` are ``None``, the
          call has no token data. A warning is logged identifying the
          call kind (R12.6) and the record is appended verbatim so the
          call is still visible in the per-iteration log.
        * Otherwise, if ``estimated_cost`` is not already set and the
          record has a ``model`` plus both token counts, pricing is
          looked up in ``config.model_pricing``. When pricing is present
          a derived cost is stored via ``model_copy`` (R12.3); when
          pricing is absent the record is appended with ``estimated_cost``
          left unset (R12.4).
        * Records whose ``estimated_cost`` is already supplied by the
          caller are preserved as-is.
        """
        # R12.6: a call with no token data is excluded from totals but still
        # persisted so the per-iteration log shows that the call happened.
        if call.input_tokens is None and call.output_tokens is None:
            logger.warning(
                "LLM call of kind %r reported no token usage; "
                "excluded from run token totals",
                call.kind,
            )
            self._calls.append(call)
            return

        # R12.3/R12.4: derive cost only when we have everything we need and
        # the record does not already carry a pre-computed cost. This keeps
        # the accountant idempotent if a caller has already priced a call
        # upstream (e.g. the Kiro CLI envelope carried a native cost).
        if (
            call.estimated_cost is None
            and call.model is not None
            and call.input_tokens is not None
            and call.output_tokens is not None
        ):
            price = self._config.model_pricing.get(call.model)
            if price is not None:
                cost = (
                    call.input_tokens * price.input_price_per_token
                    + call.output_tokens * price.output_price_per_token
                )
                call = call.model_copy(update={"estimated_cost": cost})

        self._calls.append(call)

    def totals(self) -> RunTokenTotals:
        """Aggregate all recorded calls into a :class:`RunTokenTotals` (R12.5).

        Missing ``input_tokens`` or ``output_tokens`` on an individual
        record contributes zero to the corresponding sum (R12.6: calls
        without token data are excluded from totals). ``estimated_cost``
        is summed only across records that carry a value, so the
        top-level ``total_estimated_cost`` and the per-kind
        ``KindTotals.cost`` remain ``None`` when no priced records exist
        (R12.4).
        """
        total_input = 0
        total_output = 0
        total_cost: Optional[float] = None
        by_kind: dict[CallKind, KindTotals] = {}

        for call in self._calls:
            if call.input_tokens is not None:
                total_input += call.input_tokens
            if call.output_tokens is not None:
                total_output += call.output_tokens

            if call.estimated_cost is not None:
                total_cost = (total_cost or 0.0) + call.estimated_cost

            kind_totals = by_kind.get(call.kind)
            if kind_totals is None:
                kind_totals = KindTotals()
                by_kind[call.kind] = kind_totals

            if call.input_tokens is not None:
                kind_totals.input += call.input_tokens
            if call.output_tokens is not None:
                kind_totals.output += call.output_tokens
            if call.estimated_cost is not None:
                kind_totals.cost = (kind_totals.cost or 0.0) + call.estimated_cost

        return RunTokenTotals(
            total_input=total_input,
            total_output=total_output,
            total_combined=total_input + total_output,
            total_estimated_cost=total_cost,
            by_kind=by_kind,
        )
