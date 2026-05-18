"""Orchestrator: LLM-based persona selection (R2.3-R2.4, R4.1-R4.10, R12.1).

The Orchestrator owns two responsibilities:

1. Turn a (task, spec, registry) triple into a persona selection by
   taking one of four paths:

   - ``"explicit"`` when the task declares a ``target_persona`` that
     exists in the registry (R4.1).
   - ``"llm"`` when the task has no explicit target and the LLM returns
     a valid decision naming a persona that exists in the registry
     (R4.2, R4.5).
   - ``"fallback"`` when the LLM call fails (network error, timeout, or
     an unparseable response) (R4.8). One shot only, no retry: the next
     iteration gets a fresh attempt.
   - raise :class:`StuckTaskError` when the task would be stuck by an
     orchestrator decision (R4.7, R4.9); the outer loop catches the
     exception and marks the task stuck.

2. Build the structured prompt handed to the LLM for LLM-based selection
   (R4.2, R4.3, R4.4). The prompt carries the task context (id, title,
   status, tags, retry counter, spec summary, creation metadata) plus
   every registered persona's name and description and a strict
   JSON-output instruction.

Token usage reported by the Claude Code CLI invocation is attached to the
:class:`PersonaSelection` so the Token Accountant can record the
``orchestrator_selection`` call kind (R12.1).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from ralph_loop.json_extract import extract_validating_object
from ralph_loop.claude_code import ClaudeCodeInvocationTimeout, ClaudeCodeInvoker
from ralph_loop.models import (
    PersonaDescription,
    PersonaSelection,
    Task,
    TaskSpec,
    TokenUsage,
)
from ralph_loop.persona_registry import PersonaRegistry

logger = logging.getLogger(__name__)


class OrchestratorDecision(BaseModel):
    """LLM-returned structured decision (R4.4).

    The Orchestrator instructs the LLM to return a strict JSON object of
    the form ``{"persona": "<name>", "rationale": "<brief text>"}``.
    Pydantic validates the shape; parse failures are handled by the
    fallback path (R4.8).
    """

    persona: str
    rationale: str


class StuckTaskError(Exception):
    """Raised when the orchestrator determines the current task must be stuck.

    This covers the two "mark task stuck" paths in R4.7 and R4.9:

    - R4.7: the LLM returned a persona name that does not exist in the
      registry (hallucination).
    - R4.9: the task declared a ``target_persona`` that does not exist
      in the registry.

    Carries the ``task_id`` and ``reason`` so the main loop can log the
    stuck event and persist the status transition without having to
    re-parse the exception message.
    """

    def __init__(self, task_id: str, reason: str) -> None:
        super().__init__(f"Task {task_id!r} marked stuck: {reason}")
        self.task_id = task_id
        self.reason = reason


# Strict JSON-output instruction used both in the prompt body and in
# tests that assert the instruction is present (Property 8).
STRICT_JSON_INSTRUCTION = (
    'Return STRICTLY a single JSON object of the form '
    '{"persona": "<name>", "rationale": "<brief text>"}. '
    "Do not include any additional text, prose, or code fences outside "
    "the JSON object."
)


def build_orchestrator_prompt(
    task: Task,
    spec: TaskSpec,
    descriptions: list[PersonaDescription],
) -> str:
    """Build the LLM prompt for persona selection (R4.2, R4.3, R4.4, R3.8).

    The prompt assembles, in order:

    1. A one-line role statement and the strict JSON-output instruction
       (R4.4).
    2. A Task section with ``id``, ``title``, current ``status``,
       ``tags``, and ``retry_count`` (R4.3).
    3. A Task Spec Summary distilled from the spec body's ``objective``
       and ``instructions`` (R4.3 "task-spec summary").
    4. A Creation Metadata section surfacing ``created_at_iteration``,
       ``created_by_persona``, and the ``creation_chain`` when present
       (R4.3 "task creation metadata").
    5. An Available Personas section listing every registered persona's
       name and description (R3.8, R4.2).

    The returned string is handed verbatim to the Claude Code CLI as the
    Orchestrator's LLM input.
    """
    persona_block = "\n".join(
        f"- {d.name}: {d.description}" for d in descriptions
    )

    # Condense the spec body's objective and instructions into a short
    # summary. The Orchestrator doesn't need the full prose — that lands
    # in the per-iteration Context_Window via the Context Composer.
    spec_summary = (
        f"Objective: {spec.body.objective}\n"
        f"Instructions: {spec.body.instructions}"
    )

    creation_lines: list[str] = []
    if task.created_at_iteration is not None:
        creation_lines.append(f"created_at_iteration: {task.created_at_iteration}")
    if task.created_by_persona is not None:
        creation_lines.append(f"created_by_persona: {task.created_by_persona}")
    if task.creation_chain:
        creation_lines.append(f"creation_chain: {task.creation_chain}")
    creation_block = (
        "\n".join(creation_lines) if creation_lines else "(no creation metadata)"
    )

    tags_display = task.tags if task.tags is not None else []

    return (
        "You are the Ralph Loop Orchestrator. Select the single "
        "best-matching persona for the following task by evaluating every "
        "persona's description against the task.\n"
        f"{STRICT_JSON_INSTRUCTION}\n"
        "\n## Task\n"
        f"id: {task.id}\n"
        f"title: {task.title}\n"
        f"status: {task.status}\n"
        f"tags: {tags_display}\n"
        f"retry_count: {task.retry_count}\n"
        "\n## Task Spec Summary\n"
        f"{spec_summary}\n"
        "\n## Creation Metadata\n"
        f"{creation_block}\n"
        "\n## Available Personas\n"
        f"{persona_block}\n"
    )


class Orchestrator:
    """LLM-backed persona selector (R4.1-R4.10).

    The orchestrator is stateless; one instance is reused across
    iterations. It owns the Kiro invoker, a log path for the LLM
    subprocess output, and the configured ``fallback_persona`` name.
    """

    def __init__(
        self,
        *,
        invoker: ClaudeCodeInvoker,
        log_path: Path,
        fallback_persona: str,
        timeout_ms: Optional[int] = None,
        model_id: Optional[str] = None,
    ) -> None:
        self._invoker = invoker
        self._log_path = log_path
        self._fallback_persona = fallback_persona
        self._timeout_ms = timeout_ms
        self._model_id = model_id

    async def select_persona(
        self,
        *,
        task: Task,
        spec: TaskSpec,
        registry: PersonaRegistry,
    ) -> PersonaSelection:
        """Select a persona for ``task`` per R4.1-R4.10.

        Path resolution:

        1. Explicit target (``task.target_persona``):
           - Present in the registry -> ``path="explicit"`` (R4.1).
           - Missing from the registry -> raise
             :class:`StuckTaskError` with a reason identifying the
             missing persona name (R4.9). No LLM call is made.
        2. Otherwise, a single structured LLM call (R4.2, R4.3, R4.4):
           - Valid decision naming a registered persona -> ``path="llm"``
             with ``rationale`` and ``llm_decision_raw`` populated
             (R4.5, R4.6).
           - LLM returned a persona name that isn't in the registry
             (hallucination) -> raise :class:`StuckTaskError` (R4.7).
           - Network error, timeout, non-zero exit, or unparseable
             response -> fallback persona with a logged warning (R4.8).

        Token usage reported by the Claude Code CLI invocation is attached to
        the :class:`PersonaSelection` (R12.1).
        """

        # --- Explicit target (R4.1, R4.9) -----------------------------
        if task.target_persona:
            persona = registry.get(task.target_persona)
            if persona is None:
                raise StuckTaskError(
                    task.id,
                    f"target persona {task.target_persona!r} "
                    "not in persona registry",
                )
            return PersonaSelection(persona=persona, path="explicit")

        # --- LLM-based selection (R4.2, R4.3, R4.4) -------------------
        descriptions = registry.describe_all_for_orchestrator()
        prompt = build_orchestrator_prompt(task, spec, descriptions)

        try:
            result = await self._invoker.invoke(
                context=prompt,
                log_path=self._log_path,
                call_kind="orchestrator_selection",
                timeout_ms=self._timeout_ms,
                model_id=self._model_id,
            )
        except ClaudeCodeInvocationTimeout as e:
            return self._fallback(registry, f"timeout: {e}")
        except Exception as e:  # noqa: BLE001 -- subprocess/network errors
            return self._fallback(registry, f"invocation error: {e}")

        if result.exit_code != 0:
            return self._fallback(
                registry,
                f"non-zero exit: {result.exit_code}",
                token_usage=result.token_usage,
            )

        decision_raw = result.stdout.strip()
        decision = self._parse_decision(decision_raw)
        if decision is None:
            return self._fallback(
                registry,
                "could not parse LLM decision",
                raw=decision_raw,
                token_usage=result.token_usage,
            )

        # --- Persona existence check (R4.5, R4.7) ---------------------
        persona = registry.get(decision.persona)
        if persona is None:
            raise StuckTaskError(
                task.id,
                f"LLM hallucinated persona name {decision.persona!r}; "
                "not in persona registry",
            )

        logger.info(
            "orchestrator selected persona %r for task %s (rationale: %s)",
            decision.persona,
            task.id,
            decision.rationale,
        )
        return PersonaSelection(
            persona=persona,
            path="llm",
            rationale=decision.rationale,
            llm_decision_raw=decision_raw,
            token_usage=result.token_usage,
        )

    def _parse_decision(self, raw: str) -> Optional[OrchestratorDecision]:
        """Best-effort JSON parse of the LLM response.

        Delegates to :func:`ralph_loop.json_extract.extract_validating_object`
        which strips markdown fences, scans every balanced ``{...}``
        object in ``raw`` (honoring JSON string/escape state), and
        returns the first one that validates as an
        :class:`OrchestratorDecision`. Empty input short-circuits to
        ``None``; when no candidate validates the caller takes the
        fallback path (R4.8).
        """
        if not raw:
            return None
        return extract_validating_object(raw, OrchestratorDecision)

    def _fallback(
        self,
        registry: PersonaRegistry,
        reason: str,
        *,
        raw: Optional[str] = None,
        token_usage: Optional[TokenUsage] = None,
    ) -> PersonaSelection:
        """Return a ``path="fallback"`` selection and log a warning (R4.8).

        Raises :class:`StuckTaskError` if the configured fallback
        persona is itself missing from the registry — that is a
        configuration error with no safe recovery.
        """
        persona = registry.get(self._fallback_persona)
        if persona is None:
            raise StuckTaskError(
                "__fallback__",
                f"configured fallback persona {self._fallback_persona!r} "
                "not in persona registry",
            )
        logger.warning(
            "orchestrator falling back to %r (reason: %s)",
            self._fallback_persona,
            reason,
        )
        return PersonaSelection(
            persona=persona,
            path="fallback",
            rationale=reason,
            llm_decision_raw=raw,
            token_usage=token_usage,
        )
