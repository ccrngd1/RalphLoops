"""Property-based tests for the Orchestrator (Tasks 14.2, 14.4).

Two properties from ``design.md`` land here:

- Property 7 (persona-selection routing): for any ``(task, config,
  registry, llm_outcome)``, the combined escalation + orchestrator
  routing produces the expected selection path or marks the task stuck
  per the rule in ``design.md`` §Property 7.
- Property 8 (orchestrator prompt content): for any ``(task, spec,
  registry)``, the prompt built by
  :func:`ralph_loop.orchestrator.build_orchestrator_prompt` contains
  the task id, title, status, tags, retry counter, a task-spec summary,
  task creation metadata, every persona name and description, and a
  strict JSON-output instruction.

Property 7 validates the routing across the full LLM-outcome space by
mocking the Kiro invoker so each outcome maps deterministically to a
single :class:`ClaudeInvocationResult` (or raised exception).
"""

# Feature: ralph-loop, Property 7 & 8: Orchestrator selection routing and prompt content

from __future__ import annotations

import asyncio
import json
import string
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ralph_loop.escalation import EscalationHandler
from ralph_loop.claude_code import ClaudeCodeInvocationTimeout, ClaudeCodeInvoker
from ralph_loop.models import (
    Config,
    ClaudeInvocationResult,
    Persona,
    PersonaSelection,
    ShellCheckConfig,
    Task,
    TaskSpec,
    TaskSpecBody,
)
from ralph_loop.orchestrator import (
    STRICT_JSON_INSTRUCTION,
    Orchestrator,
    StuckTaskError,
    build_orchestrator_prompt,
)
from ralph_loop.persona_registry import PersonaRegistry


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Short, URL-safe name pool shared by every persona role. Drawing from a
# small fixed pool keeps the generator compact and ensures the
# "valid-known" outcome consistently lands on a persona that exists in
# the registry.
_PERSONA_POOL: tuple[str, ...] = (
    "Writer",
    "Reviewer",
    "Editor",
    "Planner",
    "Coder",
    "Tester",
    "Troubleshooter",
    "Fallback",
)

_TEXT_ALPHABET = string.ascii_letters + string.digits + " -_.,"
_ID_ALPHABET = string.ascii_letters + string.digits + "_-"

persona_name_strategy = st.sampled_from(_PERSONA_POOL)
short_text_strategy = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=30)
id_strategy = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=8)
status_strategy = st.sampled_from(
    ["pending", "in_progress", "passing", "failing", "stuck"]
)
tag_strategy = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=6)

llm_outcome_strategy = st.sampled_from(
    [
        "valid-known",
        "valid-hallucinated",
        "parse-error",
        "network-error",
        "timeout",
    ]
)


@st.composite
def persona_strategy(draw, *, name: Optional[str] = None) -> Persona:
    resolved_name = name if name is not None else draw(persona_name_strategy)
    return Persona(
        name=resolved_name,
        description=draw(short_text_strategy),
        prompt_template="You are {{persona_name}} on {{task_id}}.",
    )


@st.composite
def persona_registry_strategy(draw, *, min_size: int = 1) -> PersonaRegistry:
    """Draw a registry containing at least one persona.

    Names are drawn unique from ``_PERSONA_POOL``. Minimum size defaults
    to 1 so tests that need at least one persona (for the fallback or
    explicit target) never receive an empty registry.
    """
    names = draw(
        st.lists(
            persona_name_strategy,
            min_size=min_size,
            max_size=len(_PERSONA_POOL),
            unique=True,
        )
    )
    personas = [draw(persona_strategy(name=name)) for name in names]
    return PersonaRegistry({p.name: p for p in personas})


@st.composite
def task_strategy(
    draw,
    *,
    target_persona: Optional[str] = None,
    retry_count: Optional[int] = None,
) -> Task:
    tid = draw(id_strategy)
    status = draw(status_strategy)
    tags = draw(st.one_of(st.none(), st.lists(tag_strategy, max_size=3)))
    resolved_retry = (
        retry_count if retry_count is not None else draw(st.integers(min_value=0, max_value=10))
    )
    # Creation metadata is optional across the Task universe; draw
    # sometimes so the prompt builder sees both the populated and the
    # "(no creation metadata)" branches.
    created_at_iteration = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=10)))
    created_by_persona = draw(st.one_of(st.none(), persona_name_strategy))
    creation_chain = draw(
        st.one_of(
            st.none(),
            st.lists(persona_name_strategy, min_size=1, max_size=3),
        )
    )
    return Task(
        id=tid,
        title=draw(short_text_strategy),
        priority=draw(st.integers(min_value=-5, max_value=20)),
        status=status,
        spec_path=f"specs/{tid}.md",
        retry_count=resolved_retry,
        target_persona=target_persona,
        tags=tags,
        created_at_iteration=created_at_iteration,
        created_by_persona=created_by_persona,
        creation_chain=creation_chain,
    )


@st.composite
def task_spec_strategy(draw) -> TaskSpec:
    tid = draw(id_strategy)
    return TaskSpec(
        id=tid,
        title=draw(short_text_strategy),
        validation=[
            ShellCheckConfig(type="shell", name="check", commands=["echo ok"])
        ],
        body=TaskSpecBody(
            objective=draw(short_text_strategy),
            context_references=draw(short_text_strategy),
            instructions=draw(short_text_strategy),
        ),
    )


# ---------------------------------------------------------------------------
# Property 8: Orchestrator prompt content
# ---------------------------------------------------------------------------


@given(
    registry=persona_registry_strategy(min_size=1),
    task=task_strategy(),
    spec=task_spec_strategy(),
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_8_orchestrator_prompt_content(
    registry: PersonaRegistry, task: Task, spec: TaskSpec
) -> None:
    """Validates: Requirements 4.3, 4.4.

    The Orchestrator prompt must contain every piece of context the LLM
    needs to make an informed decision:

    - Task identity: id, title, status, tags, retry counter (R4.3).
    - Task-spec summary (R4.3).
    - Task creation metadata (R4.3).
    - Every persona name and description (R3.8, R4.2).
    - A strict structured-output instruction (R4.4).
    """
    descriptions = registry.describe_all_for_orchestrator()
    prompt = build_orchestrator_prompt(task, spec, descriptions)

    # Task identity (R4.3)
    assert task.id in prompt
    assert task.title in prompt
    assert task.status in prompt
    assert f"retry_count: {task.retry_count}" in prompt

    # Tags (R4.3): rendered as a repr list. Each individual tag must
    # appear somewhere in the prompt; the list delimiter doesn't matter
    # for the property.
    for tag in task.tags or []:
        assert tag in prompt

    # Task-spec summary (R4.3)
    assert spec.body.objective in prompt
    assert spec.body.instructions in prompt

    # Creation metadata (R4.3)
    if (
        task.created_at_iteration is None
        and task.created_by_persona is None
        and not task.creation_chain
    ):
        assert "(no creation metadata)" in prompt
    else:
        if task.created_at_iteration is not None:
            assert f"created_at_iteration: {task.created_at_iteration}" in prompt
        if task.created_by_persona is not None:
            assert task.created_by_persona in prompt
        if task.creation_chain:
            for ancestor in task.creation_chain:
                assert ancestor in prompt

    # Persona names and descriptions (R3.8, R4.2)
    for d in descriptions:
        assert d.name in prompt
        assert d.description in prompt

    # Strict JSON-output instruction (R4.4)
    assert STRICT_JSON_INSTRUCTION in prompt


# ---------------------------------------------------------------------------
# Property 7: Persona selection routing
# ---------------------------------------------------------------------------


def _make_invocation_result_for_outcome(
    outcome: str, registry_names: list[str]
) -> tuple[
    Optional[ClaudeInvocationResult], Optional[BaseException], Optional[str]
]:
    """Build the mock ``ClaudeCodeInvoker.invoke`` return value for a given outcome.

    Returns a tuple ``(result, exception, expected_llm_name)`` where
    exactly one of ``result`` or ``exception`` is set. ``expected_llm_name``
    is the persona name the LLM names in its JSON response (for
    ``"valid-known"`` or ``"valid-hallucinated"``); ``None`` otherwise.
    """
    if outcome == "valid-known":
        name = registry_names[0]
        payload = json.dumps({"persona": name, "rationale": "picked"})
        return (
            ClaudeInvocationResult(
                exit_code=0,
                stdout=payload,
                stderr="",
                token_usage=None,
                duration_ms=1,
            ),
            None,
            name,
        )
    if outcome == "valid-hallucinated":
        # A name guaranteed not to exist in the registry.
        name = "__not_in_registry__"
        payload = json.dumps({"persona": name, "rationale": "pick me"})
        return (
            ClaudeInvocationResult(
                exit_code=0,
                stdout=payload,
                stderr="",
                token_usage=None,
                duration_ms=1,
            ),
            None,
            name,
        )
    if outcome == "parse-error":
        return (
            ClaudeInvocationResult(
                exit_code=0,
                stdout="Sorry, I cannot decide.",
                stderr="",
                token_usage=None,
                duration_ms=1,
            ),
            None,
            None,
        )
    if outcome == "network-error":
        return (None, RuntimeError("network down"), None)
    if outcome == "timeout":
        return (None, ClaudeCodeInvocationTimeout("took too long"), None)
    raise AssertionError(f"unknown outcome {outcome!r}")


def _route(
    *,
    task: Task,
    spec: TaskSpec,
    registry: PersonaRegistry,
    config: Config,
    escalation: EscalationHandler,
    orchestrator: Orchestrator,
) -> tuple[str, Optional[PersonaSelection], Optional[StuckTaskError]]:
    """Run the combined escalation + orchestrator routing.

    Returns ``(outcome_kind, selection, error)`` where ``outcome_kind``
    is one of ``"selection"`` (selection returned) or ``"stuck"`` (the
    task was marked stuck). Exactly one of ``selection`` / ``error`` is
    non-``None``.
    """
    # Escalation path (R5.1, R5.2, R5.4) takes priority when the task is
    # eligible. When no escalation persona is configured or present,
    # ``try_route`` returns ``None`` and we fall back to normal
    # Orchestrator selection.
    if escalation.should_escalate(task):
        esc_selection = escalation.try_route(task)
        if esc_selection is not None:
            return ("selection", esc_selection, None)

    try:
        selection = asyncio.run(
            orchestrator.select_persona(
                task=task, spec=spec, registry=registry
            )
        )
    except StuckTaskError as e:
        return ("stuck", None, e)
    return ("selection", selection, None)


@given(
    registry=persona_registry_strategy(min_size=2),
    llm_outcome=llm_outcome_strategy,
    # Whether the task declares an explicit target_persona, and whether
    # that target is present in the registry.
    explicit_target=st.sampled_from(["absent", "present", "missing"]),
    # Whether the config wires an escalation persona, and whether that
    # persona is present in the registry.
    escalation_persona_state=st.sampled_from(
        ["unset", "present", "missing"]
    ),
    # Whether the task's retry counter sits at or above the threshold.
    retry_counter_state=st.sampled_from(["below", "at", "above"]),
    # Whether the fallback persona is present in the registry. Property
    # 7's "fallback" case presumes the configured fallback exists, which
    # matches the happy-path configuration.
    spec=task_spec_strategy(),
    extra_retry=st.integers(min_value=0, max_value=3),
    escalation_threshold=st.integers(min_value=0, max_value=5),
)
@settings(
    max_examples=150,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_property_7_persona_selection_routing(
    registry: PersonaRegistry,
    llm_outcome: str,
    explicit_target: str,
    escalation_persona_state: str,
    retry_counter_state: str,
    spec: TaskSpec,
    extra_retry: int,
    escalation_threshold: int,
    tmp_path_factory,
) -> None:
    """Validates: Requirements 2.3, 4.1, 4.2, 4.5, 4.7, 4.8, 4.9, 5.1, 5.2, 5.4.

    For any combination of (task explicit-target state, escalation
    configuration, retry-counter state, LLM outcome), the combined
    escalation + orchestrator routing produces the expected path or
    marks the task stuck, matching the rule in ``design.md`` §Property 7.
    """
    names = list(registry._personas.keys())  # type: ignore[attr-defined]

    # --- Build the task with the right explicit-target state ---
    if explicit_target == "absent":
        target = None
    elif explicit_target == "present":
        target = names[0]
    else:  # "missing"
        target = "__not_in_registry_target__"

    # --- Build the task with a retry count relative to threshold ---
    if retry_counter_state == "below":
        retry_count = max(0, escalation_threshold - 1)
    elif retry_counter_state == "at":
        retry_count = escalation_threshold
    else:  # "above"
        retry_count = escalation_threshold + 1 + extra_retry

    task = Task(
        id="T",
        title="T",
        priority=1,
        status="failing",
        spec_path="specs/T.md",
        retry_count=retry_count,
        target_persona=target,
    )

    # --- Build the config ---
    fallback_name = names[-1]  # distinct from names[0] since min_size=2
    if escalation_persona_state == "unset":
        escalation_persona = None
    elif escalation_persona_state == "present":
        # Pick any name from the registry (may equal fallback; that's OK).
        escalation_persona = names[min(1, len(names) - 1)]
    else:  # "missing"
        escalation_persona = "__not_in_registry_escalation__"

    config = Config(
        fallback_persona=fallback_name,
        escalation_persona=escalation_persona,
        escalation_threshold=escalation_threshold,
        max_retries_per_task=100,  # pushed high so the retry cap
        # doesn't interact with the routing logic; retry-cap handling
        # is covered by the Escalation Handler unit tests (Task 15.2).
    )

    # --- Wire up the Kiro invoker per the chosen LLM outcome ---
    invoker = AsyncMock(spec=ClaudeCodeInvoker)
    result, exception, llm_named = _make_invocation_result_for_outcome(
        llm_outcome, names
    )
    if exception is not None:
        invoker.invoke.side_effect = exception
    else:
        invoker.invoke.return_value = result

    log_path = tmp_path_factory.mktemp("orch") / "llm.log"
    orchestrator = Orchestrator(
        invoker=invoker,
        log_path=log_path,
        fallback_persona=fallback_name,
    )
    escalation = EscalationHandler(registry=registry, config=config)

    kind, selection, error = _route(
        task=task,
        spec=spec,
        registry=registry,
        config=config,
        escalation=escalation,
        orchestrator=orchestrator,
    )

    # --- Compute the expected outcome per design.md Property 7 -----

    # Escalation first: path == "escalation" iff should_escalate AND an
    # escalation persona is configured AND present in the registry
    # (R5.1, R5.2).
    escalates = retry_count >= escalation_threshold
    escalation_resolves = escalates and escalation_persona_state == "present"

    if escalation_resolves:
        assert kind == "selection"
        assert selection is not None
        assert selection.path == "escalation"
        return

    # Explicit target (R4.1, R4.9)
    if explicit_target == "present":
        assert kind == "selection"
        assert selection is not None
        assert selection.path == "explicit"
        assert selection.persona.name == target
        return
    if explicit_target == "missing":
        assert kind == "stuck"
        assert error is not None
        assert error.task_id == task.id
        return

    # LLM-based selection (R4.2, R4.5, R4.7, R4.8)
    if llm_outcome == "valid-known":
        assert kind == "selection"
        assert selection is not None
        assert selection.path == "llm"
        assert selection.persona.name == llm_named
        return
    if llm_outcome == "valid-hallucinated":
        assert kind == "stuck"
        assert error is not None
        return
    # parse-error, network-error, timeout -> fallback (R4.8)
    assert kind == "selection"
    assert selection is not None
    assert selection.path == "fallback"
    assert selection.persona.name == fallback_name
