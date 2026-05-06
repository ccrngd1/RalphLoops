"""Escalation handling for tasks that exceed the retry threshold (R5.1-R5.7, R10.3).

When a task's retry counter reaches the configured escalation threshold,
the Ralph Loop treats the next iteration for that task as *escalated*.
If an escalation persona is configured, the iteration routes straight to
that persona with a supplemental context (retry history, prior failing
validation outputs, prior iteration logs) so the persona can take a
different approach (R5.1, R5.2, R5.3). If no escalation persona is
configured, the loop keeps using normal Orchestrator persona selection
for the remainder of the task's retries (R5.4). In either case the
per-task retry limit still terminates the task as ``stuck`` once
exhausted (R5.6, R10.3) — escalation does not extend the retry budget,
it just changes who runs the next iteration.

This module provides the pure-logic parts of that flow:

* :meth:`EscalationHandler.should_escalate` — the threshold predicate
  (R5.1).
* :meth:`EscalationHandler.try_route` — resolve the escalation persona
  when configured (R5.2) or return ``None`` so the caller delegates to
  the Orchestrator (R5.4). Logs the escalation event either way (R5.7).
* :meth:`EscalationHandler.build_escalation_context` — compose retry
  history, prior failing validation outputs, and prior iteration logs
  into the supplemental context block (R5.3).

The caller (the main loop in ``cli.py``) is responsible for pairing
:meth:`try_route` with :meth:`build_escalation_context` and handing both
to the Context Composer.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ralph_loop.models import Config, PersonaSelection, Task
from ralph_loop.persona_registry import PersonaRegistry

logger = logging.getLogger(__name__)


class EscalationHandler:
    """Resolve escalation routing and assemble escalation context."""

    def __init__(self, *, registry: PersonaRegistry, config: Config) -> None:
        self._registry = registry
        self._config = config

    def should_escalate(self, task: Task) -> bool:
        """Return ``True`` iff ``task.retry_count >= escalation_threshold`` (R5.1).

        The threshold is ``config.escalation_threshold`` (default ``3``
        per R5.5). A task whose retry counter has reached or exceeded
        that value is eligible for escalation routing on the next
        iteration.
        """
        return task.retry_count >= self._config.escalation_threshold

    def try_route(self, task: Task) -> Optional[PersonaSelection]:
        """Return a ``path="escalation"`` selection when a persona is configured.

        Resolves the escalation persona (R5.2):

        - ``escalation_persona`` is unset -> log an info event (R5.7)
          and return ``None``, signaling the caller to fall back to
          normal Orchestrator selection (R5.4).
        - ``escalation_persona`` is set but not in the registry -> log
          a warning and return ``None`` so the iteration still makes
          progress rather than crashing on a config error.
        - ``escalation_persona`` is set and present -> log the
          escalation event (R5.7) and return the
          :class:`PersonaSelection` with ``path="escalation"``.

        The per-task retry limit (R5.6 / R10.3) is enforced by the outer
        loop, not here: escalation only changes who runs the next
        iteration, it does not bypass the retry cap.
        """
        name = self._config.escalation_persona
        if not name:
            logger.info(
                "escalation event: task %s retry %d; no escalation_persona configured",
                task.id,
                task.retry_count,
            )
            return None
        persona = self._registry.get(name)
        if persona is None:
            logger.warning(
                "escalation_persona %r is not in the persona registry; "
                "treating as no escalation persona configured",
                name,
            )
            return None
        logger.info(
            "escalation event: task %s retry %d -> persona %s",
            task.id,
            task.retry_count,
            name,
        )
        return PersonaSelection(persona=persona, path="escalation")

    def build_escalation_context(
        self,
        *,
        task: Task,
        retry_history: list[dict[str, Any]],
        prior_validation_outputs: list[str],
        prior_iteration_logs: list[str],
    ) -> str:
        """Compose the supplemental escalation context block (R5.3).

        Sections included (each omitted when the corresponding input is
        empty so an escalated task with no prior history doesn't
        produce a block of section headers followed by nothing):

        1. ``Retry History`` — a bulleted list of ``retry_history``
           entries, one line per retry, stringifying each dict via
           :class:`str`.
        2. ``Prior Failing Validation Output`` — each entry fenced in a
           triple-backtick block so the LLM sees raw output exactly as
           it was captured.
        3. ``Prior Iteration Logs`` — same fencing treatment.

        Returns an empty string when all three inputs are empty, which
        is the correct behavior for the very first escalation attempt
        after a retry-counter reset.
        """
        if not (retry_history or prior_validation_outputs or prior_iteration_logs):
            return ""

        parts: list[str] = [f"# Escalation Context for Task {task.id}\n\n"]

        if retry_history:
            parts.append("## Retry History\n\n")
            for entry in retry_history:
                parts.append(f"- {entry}\n")
            parts.append("\n")

        if prior_validation_outputs:
            parts.append("## Prior Failing Validation Output\n\n")
            for output in prior_validation_outputs:
                parts.append(f"```\n{output}\n```\n\n")

        if prior_iteration_logs:
            parts.append("## Prior Iteration Logs\n\n")
            for log_entry in prior_iteration_logs:
                parts.append(f"```\n{log_entry}\n```\n\n")

        return "".join(parts)
