"""Unit tests for the Orchestrator (Task 14.1 / 14.3).

Covers the prompt builder and the ``Orchestrator.select_persona`` paths:

- Explicit target present -> ``path="explicit"``.
- Explicit target missing -> :class:`StuckTaskError`.
- Valid LLM decision -> ``path="llm"`` with rationale populated.
- Hallucinated persona name -> :class:`StuckTaskError`.
- Timeout -> ``path="fallback"``.
- Non-JSON stdout -> ``path="fallback"``.
- Non-zero exit -> ``path="fallback"``.

Requirements exercised: R2.3, R2.4, R4.1-R4.10, R3.8, R12.1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ralph_loop.claude_code import ClaudeCodeInvocationTimeout, ClaudeCodeInvoker
from ralph_loop.models import (
    ClaudeInvocationResult,
    Persona,
    PersonaReviewCheckConfig,
    ShellCheckConfig,
    Task,
    TaskSpec,
    TaskSpecBody,
    TokenUsage,
)
from ralph_loop.orchestrator import (
    STRICT_JSON_INSTRUCTION,
    Orchestrator,
    OrchestratorDecision,
    StuckTaskError,
    build_orchestrator_prompt,
)
from ralph_loop.persona_registry import PersonaRegistry


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_persona(name: str, description: str = "") -> Persona:
    return Persona(
        name=name,
        description=description or f"{name} description.",
        prompt_template=f"You are {{{{persona_name}}}} on {{{{task_id}}}}.",
    )


def _make_registry(personas: list[Persona]) -> PersonaRegistry:
    return PersonaRegistry({p.name: p for p in personas})


def _make_task(**overrides: Any) -> Task:
    base: dict[str, Any] = dict(
        id="T1",
        title="Write chapter",
        priority=1,
        status="pending",
        spec_path="specs/T1.md",
        retry_count=0,
    )
    base.update(overrides)
    return Task(**base)


def _make_spec(**overrides: Any) -> TaskSpec:
    base: dict[str, Any] = dict(
        id="T1",
        title="Write chapter",
        validation=[
            ShellCheckConfig(type="shell", name="build", commands=["echo ok"])
        ],
        body=TaskSpecBody(
            objective="Draft the opening chapter.",
            context_references="Outline v1",
            instructions="Keep it under 2000 words.",
        ),
    )
    base.update(overrides)
    return TaskSpec(**base)


def _make_invocation_result(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    token_usage: TokenUsage | None = None,
    duration_ms: int = 1,
) -> ClaudeInvocationResult:
    return ClaudeInvocationResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        token_usage=token_usage,
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# Prompt builder (Task 14.1; R4.2, R4.3, R4.4, R3.8)
# ---------------------------------------------------------------------------


class TestBuildOrchestratorPrompt:
    def test_prompt_contains_task_identity_fields(self) -> None:
        task = _make_task(
            id="T-alpha",
            title="Draft outline",
            status="failing",
            tags=["writing", "bootstrap"],
            retry_count=2,
        )
        spec = _make_spec(id="T-alpha", title="Draft outline")
        registry = _make_registry(
            [_make_persona("Writer"), _make_persona("Reviewer")]
        )

        prompt = build_orchestrator_prompt(
            task, spec, registry.describe_all_for_orchestrator()
        )

        assert "T-alpha" in prompt
        assert "Draft outline" in prompt
        assert "failing" in prompt
        assert "retry_count: 2" in prompt
        # Tags rendered as a Python-style list so they're greppable
        # without any tricky quoting on the LLM side.
        assert "writing" in prompt
        assert "bootstrap" in prompt

    def test_prompt_contains_persona_names_and_descriptions(self) -> None:
        task = _make_task()
        spec = _make_spec()
        registry = _make_registry(
            [
                _make_persona("Writer", description="Drafts new prose."),
                _make_persona("Reviewer", description="Reviews drafts."),
            ]
        )

        prompt = build_orchestrator_prompt(
            task, spec, registry.describe_all_for_orchestrator()
        )

        assert "Writer" in prompt
        assert "Drafts new prose." in prompt
        assert "Reviewer" in prompt
        assert "Reviews drafts." in prompt

    def test_prompt_contains_strict_json_instruction(self) -> None:
        task = _make_task()
        spec = _make_spec()
        registry = _make_registry([_make_persona("Writer")])

        prompt = build_orchestrator_prompt(
            task, spec, registry.describe_all_for_orchestrator()
        )

        assert STRICT_JSON_INSTRUCTION in prompt

    def test_prompt_includes_task_spec_objective_and_instructions(self) -> None:
        task = _make_task()
        spec = _make_spec(
            body=TaskSpecBody(
                objective="Write a fact-checked intro.",
                context_references="(none)",
                instructions="Use the house style guide.",
            )
        )
        registry = _make_registry([_make_persona("Writer")])

        prompt = build_orchestrator_prompt(
            task, spec, registry.describe_all_for_orchestrator()
        )

        assert "Write a fact-checked intro." in prompt
        assert "Use the house style guide." in prompt

    def test_prompt_surfaces_creation_metadata_when_present(self) -> None:
        task = _make_task(
            created_at_iteration=4,
            created_by_persona="Outline",
            creation_chain=["Planner", "Outline"],
        )
        spec = _make_spec()
        registry = _make_registry([_make_persona("Writer")])

        prompt = build_orchestrator_prompt(
            task, spec, registry.describe_all_for_orchestrator()
        )

        assert "created_at_iteration: 4" in prompt
        assert "created_by_persona: Outline" in prompt
        assert "creation_chain" in prompt
        assert "Planner" in prompt

    def test_prompt_indicates_no_creation_metadata_when_absent(self) -> None:
        task = _make_task()
        spec = _make_spec()
        registry = _make_registry([_make_persona("Writer")])

        prompt = build_orchestrator_prompt(
            task, spec, registry.describe_all_for_orchestrator()
        )

        assert "(no creation metadata)" in prompt


# ---------------------------------------------------------------------------
# Orchestrator.select_persona: explicit target (R4.1, R4.9)
# ---------------------------------------------------------------------------


class TestSelectPersonaExplicit:
    async def test_explicit_target_present_returns_explicit_path(
        self, tmp_path: Path
    ) -> None:
        writer = _make_persona("Writer")
        registry = _make_registry([writer, _make_persona("Reviewer")])
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Reviewer",
        )
        task = _make_task(target_persona="Writer")
        spec = _make_spec()

        selection = await orch.select_persona(
            task=task, spec=spec, registry=registry
        )

        assert selection.path == "explicit"
        assert selection.persona.name == "Writer"
        # No LLM call should happen on the explicit path.
        invoker.invoke.assert_not_called()

    async def test_explicit_target_missing_raises_stuck_without_llm(
        self, tmp_path: Path
    ) -> None:
        registry = _make_registry([_make_persona("Writer")])
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Writer",
        )
        task = _make_task(target_persona="Ghost")
        spec = _make_spec()

        with pytest.raises(StuckTaskError) as excinfo:
            await orch.select_persona(task=task, spec=spec, registry=registry)

        assert excinfo.value.task_id == task.id
        assert "Ghost" in excinfo.value.reason
        invoker.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# Orchestrator.select_persona: LLM path (R4.2, R4.5, R4.6, R4.7, R4.8, R12.1)
# ---------------------------------------------------------------------------


class TestSelectPersonaLlm:
    async def test_valid_decision_returns_llm_path_with_rationale(
        self, tmp_path: Path
    ) -> None:
        writer = _make_persona("Writer")
        reviewer = _make_persona("Reviewer")
        registry = _make_registry([writer, reviewer])
        raw = json.dumps({"persona": "Reviewer", "rationale": "Review pass"})
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=raw,
            token_usage=TokenUsage(input_tokens=10, output_tokens=4, model="m1"),
        )
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Writer",
        )
        task = _make_task()
        spec = _make_spec()

        selection = await orch.select_persona(
            task=task, spec=spec, registry=registry
        )

        assert selection.path == "llm"
        assert selection.persona.name == "Reviewer"
        assert selection.rationale == "Review pass"
        assert selection.llm_decision_raw == raw
        assert selection.token_usage is not None
        assert selection.token_usage.input_tokens == 10
        invoker.invoke.assert_awaited_once()
        # The Orchestrator must classify the call as orchestrator_selection
        # for the Token Accountant (R12.1).
        kwargs = invoker.invoke.call_args.kwargs
        assert kwargs["call_kind"] == "orchestrator_selection"

    async def test_hallucinated_persona_raises_stuck(
        self, tmp_path: Path
    ) -> None:
        registry = _make_registry(
            [_make_persona("Writer"), _make_persona("Reviewer")]
        )
        raw = json.dumps({"persona": "Imaginary", "rationale": "nope"})
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.return_value = _make_invocation_result(stdout=raw)
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Writer",
        )

        with pytest.raises(StuckTaskError) as excinfo:
            await orch.select_persona(
                task=_make_task(), spec=_make_spec(), registry=registry
            )

        assert "Imaginary" in excinfo.value.reason

    async def test_timeout_falls_back(self, tmp_path: Path) -> None:
        registry = _make_registry(
            [_make_persona("Writer"), _make_persona("Reviewer")]
        )
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.side_effect = ClaudeCodeInvocationTimeout("timed out")
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Writer",
        )

        selection = await orch.select_persona(
            task=_make_task(), spec=_make_spec(), registry=registry
        )

        assert selection.path == "fallback"
        assert selection.persona.name == "Writer"
        assert selection.rationale is not None
        assert "timeout" in selection.rationale

    async def test_network_error_falls_back(self, tmp_path: Path) -> None:
        registry = _make_registry(
            [_make_persona("Writer"), _make_persona("Reviewer")]
        )
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.side_effect = RuntimeError("connection refused")
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Writer",
        )

        selection = await orch.select_persona(
            task=_make_task(), spec=_make_spec(), registry=registry
        )

        assert selection.path == "fallback"
        assert selection.persona.name == "Writer"
        assert selection.rationale is not None
        assert "connection refused" in selection.rationale

    async def test_non_json_stdout_falls_back(self, tmp_path: Path) -> None:
        registry = _make_registry(
            [_make_persona("Writer"), _make_persona("Reviewer")]
        )
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout="Sorry, I don't know."
        )
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Writer",
        )

        selection = await orch.select_persona(
            task=_make_task(), spec=_make_spec(), registry=registry
        )

        assert selection.path == "fallback"
        assert selection.persona.name == "Writer"
        assert selection.llm_decision_raw == "Sorry, I don't know."

    async def test_json_wrapped_in_prose_is_extracted(
        self, tmp_path: Path
    ) -> None:
        """LLM sometimes wraps JSON in prose; the parser extracts it."""
        registry = _make_registry(
            [_make_persona("Writer"), _make_persona("Reviewer")]
        )
        raw = (
            "Here is my decision:\n"
            '{"persona": "Writer", "rationale": "clear match"}'
        )
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.return_value = _make_invocation_result(stdout=raw)
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Reviewer",
        )

        selection = await orch.select_persona(
            task=_make_task(), spec=_make_spec(), registry=registry
        )

        assert selection.path == "llm"
        assert selection.persona.name == "Writer"

    async def test_non_zero_exit_falls_back(self, tmp_path: Path) -> None:
        registry = _make_registry(
            [_make_persona("Writer"), _make_persona("Reviewer")]
        )
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout='{"persona": "Writer", "rationale": "x"}', exit_code=2
        )
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Reviewer",
        )

        selection = await orch.select_persona(
            task=_make_task(), spec=_make_spec(), registry=registry
        )

        assert selection.path == "fallback"
        assert selection.persona.name == "Reviewer"
        assert selection.rationale is not None
        assert "exit" in selection.rationale

    async def test_fallback_persona_missing_raises_stuck(
        self, tmp_path: Path
    ) -> None:
        """When the invocation fails AND the configured fallback persona is
        itself missing from the registry, the orchestrator raises rather
        than returning a broken selection."""
        registry = _make_registry([_make_persona("Writer")])
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.side_effect = RuntimeError("network")
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="DoesNotExist",
        )

        with pytest.raises(StuckTaskError):
            await orch.select_persona(
                task=_make_task(), spec=_make_spec(), registry=registry
            )


# ---------------------------------------------------------------------------
# Decision model (small sanity check on OrchestratorDecision validation)
# ---------------------------------------------------------------------------


class TestOrchestratorDecision:
    def test_requires_persona_and_rationale(self) -> None:
        # Both fields are required; omitting either raises.
        with pytest.raises(Exception):
            OrchestratorDecision.model_validate({"persona": "Writer"})
        with pytest.raises(Exception):
            OrchestratorDecision.model_validate({"rationale": "r"})

    def test_valid_object_parses(self) -> None:
        decision = OrchestratorDecision.model_validate(
            {"persona": "Writer", "rationale": "matches well"}
        )
        assert decision.persona == "Writer"
        assert decision.rationale == "matches well"


# ---------------------------------------------------------------------------
# Bugfix coverage for `persona-review-verdict-parsing`
#
# Property 17: fix-checking for OrchestratorDecision — any legitimate
# envelope (fence, leading tool-use JSON, embedded braces, escaped
# quotes in rationale, prose wrapping) MUST still yield the embedded
# decision after the fix. Plus five regression tests mirroring the
# validator scenarios 1.1–1.5.
# ---------------------------------------------------------------------------


import string

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ralph_loop.json_extract import extract_validating_object


class TestProperty17OrchestratorDecisionRecovery:
    """Validates: bugfix requirements 2.1, 2.2, 2.3, 2.4, 2.5, 3.5.

    Fix-checking property mirroring Property 16 for
    :class:`OrchestratorDecision`. Given any valid decision payload
    wrapped in any combination of markdown fence, leading tool-use
    JSON envelope, and surrounding prose, the fixed pipeline MUST
    recover it.

    Expected to fail on unfixed code and pass after the migration of
    ``Orchestrator._parse_decision``.
    """

    @given(
        persona=st.text(
            alphabet=string.ascii_letters + string.digits + "_-",
            min_size=1,
            max_size=20,
        ),
        rationale=st.text(
            alphabet=st.characters(
                blacklist_categories=("Cs",),
                blacklist_characters="\x00",
            ),
            max_size=200,
        ),
        use_fence=st.booleans(),
        lang_tag=st.sampled_from(["", "json", "JSON"]),
        prepend_tool_use=st.booleans(),
        leading_prose=st.text(max_size=50),
        trailing_prose=st.text(max_size=50),
    )
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )
    def test_decision_recovered_from_any_legitimate_envelope(
        self,
        persona: str,
        rationale: str,
        use_fence: bool,
        lang_tag: str,
        prepend_tool_use: bool,
        leading_prose: str,
        trailing_prose: str,
    ) -> None:
        payload = {"persona": persona, "rationale": rationale}
        body = json.dumps(payload)

        segments: list[str] = []
        if leading_prose:
            segments.append(leading_prose)
        if prepend_tool_use:
            segments.append('{"tool":"read_file","args":{"path":"x"}}')
        if use_fence:
            segments.append(f"```{lang_tag}\n{body}\n```")
        else:
            segments.append(body)
        if trailing_prose:
            segments.append(trailing_prose)
        raw = "\n".join(segments)

        recovered = extract_validating_object(raw, OrchestratorDecision)
        assert recovered is not None, (
            f"extract_validating_object returned None for raw={raw!r}"
        )
        assert recovered.persona == persona
        assert recovered.rationale == rationale


# ---------------------------------------------------------------------------
# Regression tests: scenarios 1.1–1.5 against Orchestrator.select_persona
# ---------------------------------------------------------------------------


class TestOrchestratorVerdictParsingRegression:
    """Mirrors ``TestPersonaReviewCheck`` scenarios 1.1–1.5 for the
    orchestrator's decision-parsing path. Each test stubs the Kiro
    invoker with stdout matching the scenario and asserts the fixed
    pipeline recovers the expected :class:`OrchestratorDecision`.
    """

    async def test_decision_in_markdown_fence_is_parsed(
        self, tmp_path: Path
    ) -> None:
        """Scenario 1.1: LLM wraps decision in ```json ... ``` fence."""
        registry = _make_registry([_make_persona("Writer")])
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout='```json\n{"persona": "Writer", "rationale": "ok"}\n```'
        )
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Writer",
        )

        selection = await orch.select_persona(
            task=_make_task(), spec=_make_spec(), registry=registry
        )

        assert selection.path == "llm"
        assert selection.persona.name == "Writer"
        assert selection.rationale == "ok"

    async def test_leading_tool_use_envelope_is_skipped(
        self, tmp_path: Path
    ) -> None:
        """Scenario 1.2: tool-use JSON precedes the decision."""
        registry = _make_registry([_make_persona("Writer")])
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=(
                '{"tool":"read_file","args":{"path":"x"}}\n'
                '{"persona":"Writer","rationale":"ok"}'
            )
        )
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Writer",
        )

        selection = await orch.select_persona(
            task=_make_task(), spec=_make_spec(), registry=registry
        )

        assert selection.path == "llm"
        assert selection.persona.name == "Writer"
        assert selection.rationale == "ok"

    async def test_literal_close_brace_in_rationale_is_parsed(
        self, tmp_path: Path
    ) -> None:
        """Scenario 1.3: rationale string contains a literal ``}``."""
        registry = _make_registry([_make_persona("Writer")])
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout='{"persona":"Writer","rationale":"missing } in expression"}'
        )
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Writer",
        )

        selection = await orch.select_persona(
            task=_make_task(), spec=_make_spec(), registry=registry
        )

        assert selection.path == "llm"
        assert selection.persona.name == "Writer"
        assert selection.rationale == "missing } in expression"

    async def test_escaped_quotes_around_brace_in_rationale(
        self, tmp_path: Path
    ) -> None:
        """Scenario 1.4: rationale contains escaped quotes around a brace."""
        registry = _make_registry([_make_persona("Writer")])
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=r'{"persona":"Writer","rationale":"saw \"{\" unexpected"}'
        )
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Writer",
        )

        selection = await orch.select_persona(
            task=_make_task(), spec=_make_spec(), registry=registry
        )

        assert selection.path == "llm"
        assert selection.persona.name == "Writer"
        assert selection.rationale == 'saw "{" unexpected'

    async def test_multiple_objects_decision_is_not_first(
        self, tmp_path: Path
    ) -> None:
        """Scenario 1.5: multiple JSON objects, decision is not the first."""
        registry = _make_registry([_make_persona("Writer")])
        invoker = AsyncMock(spec=ClaudeCodeInvoker)
        invoker.invoke.return_value = _make_invocation_result(
            stdout=(
                '{"progress":1}\n'
                '{"tool":"read_file","args":{}}\n'
                '{"persona":"Writer","rationale":"ok"}'
            )
        )
        orch = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "orch.log",
            fallback_persona="Writer",
        )

        selection = await orch.select_persona(
            task=_make_task(), spec=_make_spec(), registry=registry
        )

        assert selection.path == "llm"
        assert selection.persona.name == "Writer"
        assert selection.rationale == "ok"
