"""Unit tests for :mod:`ralph_loop.planner` (Task 18.1).

Covers:

- The pure branching helpers :func:`should_auto_planner` and
  :func:`should_exit_empty_no_auto` (R17.3, R17.4).
- :meth:`Planner.bootstrap` fatal exits when no planner persona is
  configured or the configured name is missing from the registry
  (R17.7).
- :meth:`Planner.bootstrap` happy path: invokes Kiro with call kind
  ``"planner"``, reads the post-snapshot back from disk, and delegates
  to :class:`TaskCreationProcessor.process` with ``pre_snapshot=[]``.
- Rejected planner entries flow through the same pipeline as in-
  iteration creation (R17.6).

Requirements exercised: R17.1, R17.2, R17.3, R17.4, R17.5, R17.6,
R17.7, R17.8, R12.1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ralph_loop.budget import BudgetTracker
from ralph_loop.models import (
    Config,
    ClaudeInvocationResult,
    Persona,
    Task,
    TokenUsage,
)
from ralph_loop.pending_queue import PendingQueueManager
from ralph_loop.persona_registry import PersonaRegistry
from ralph_loop.planner import (
    Planner,
    PlannerError,
    should_auto_planner,
    should_exit_empty_no_auto,
)
from ralph_loop.task_creation import TaskCreationProcessor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persona(name: str) -> Persona:
    return Persona(
        name=name,
        description=f"{name} persona.",
        prompt_template="prompt",
    )


def _registry(*names: str) -> PersonaRegistry:
    return PersonaRegistry({n: _persona(n) for n in names})


def _task_entry(
    tid: str,
    *,
    priority: int = 0,
    status: str = "pending",
    target_persona: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": tid,
        "title": f"task {tid}",
        "priority": priority,
        "status": status,
        "spec_path": f"specs/{tid}.md",
        "retry_count": 0,
    }
    if target_persona is not None:
        entry["target_persona"] = target_persona
    return entry


def _make_processor(
    tmp_path: Path,
    *,
    registry: PersonaRegistry,
    config: Config,
    tasks_path: Path,
    run_id: str = "run-1",
) -> TaskCreationProcessor:
    budget = BudgetTracker(config)
    budget.record_iteration()
    queue_path = tmp_path / "pending_tasks.json"
    queue = PendingQueueManager(queue_path, registry, run_id=run_id)
    return TaskCreationProcessor(
        registry=registry,
        config=config,
        budget=budget,
        pending_queue=queue,
        tasks_path=tasks_path,
        run_id=run_id,
    )


def _make_planner(
    tmp_path: Path,
    *,
    registry: PersonaRegistry,
    config: Config,
    invoker_side_effect: list[dict[str, Any]] | None = None,
) -> tuple[Planner, Path, AsyncMock]:
    """Build a Planner wired to a mocked Kiro invoker that, when invoked,
    writes the supplied entries to ``tasks.json`` before returning.
    """

    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text("[]", encoding="utf-8")
    log_path = tmp_path / "logs" / "planner.log"

    processor = _make_processor(
        tmp_path,
        registry=registry,
        config=config,
        tasks_path=tasks_path,
    )

    invoker = AsyncMock()

    async def _invoke(*, context: str, log_path: Path, call_kind: str, **kwargs) -> ClaudeInvocationResult:  # noqa: ARG001
        # Mimic the planner persona writing to tasks.json.
        if invoker_side_effect is not None:
            tasks_path.write_text(
                json.dumps(invoker_side_effect), encoding="utf-8"
            )
        return ClaudeInvocationResult(
            exit_code=0,
            stdout="",
            stderr="",
            token_usage=TokenUsage(input_tokens=10, output_tokens=5, model="m1"),
            duration_ms=1,
        )

    invoker.invoke.side_effect = _invoke

    planner = Planner(
        invoker=invoker,
        registry=registry,
        config=config,
        processor=processor,
        tasks_path=tasks_path,
        log_path=log_path,
    )
    return planner, tasks_path, invoker


# ---------------------------------------------------------------------------
# Pure helpers (R17.3, R17.4)
# ---------------------------------------------------------------------------


class TestShouldAutoPlanner:
    def test_empty_and_auto_true(self) -> None:
        config = Config(fallback_persona="Writer", automatic_planner=True)
        assert should_auto_planner([], config) is True

    def test_empty_and_auto_false(self) -> None:
        config = Config(fallback_persona="Writer", automatic_planner=False)
        assert should_auto_planner([], config) is False

    def test_non_empty_and_auto_true(self) -> None:
        config = Config(fallback_persona="Writer", automatic_planner=True)
        tasks = [
            Task(
                id="t",
                title="t",
                priority=0,
                status="pending",
                spec_path="specs/t.md",
                retry_count=0,
            )
        ]
        assert should_auto_planner(tasks, config) is False

    def test_non_empty_and_auto_false(self) -> None:
        config = Config(fallback_persona="Writer", automatic_planner=False)
        tasks = [
            Task(
                id="t",
                title="t",
                priority=0,
                status="pending",
                spec_path="specs/t.md",
                retry_count=0,
            )
        ]
        assert should_auto_planner(tasks, config) is False


class TestShouldExitEmptyNoAuto:
    def test_empty_and_auto_false(self) -> None:
        config = Config(fallback_persona="Writer", automatic_planner=False)
        assert should_exit_empty_no_auto([], config) is True

    def test_empty_and_auto_true(self) -> None:
        config = Config(fallback_persona="Writer", automatic_planner=True)
        assert should_exit_empty_no_auto([], config) is False

    def test_non_empty(self) -> None:
        config = Config(fallback_persona="Writer", automatic_planner=False)
        tasks = [
            Task(
                id="t",
                title="t",
                priority=0,
                status="pending",
                spec_path="specs/t.md",
                retry_count=0,
            )
        ]
        assert should_exit_empty_no_auto(tasks, config) is False


# ---------------------------------------------------------------------------
# Planner.bootstrap fatal paths (R17.7)
# ---------------------------------------------------------------------------


class TestPlannerBootstrapFatal:
    async def test_no_planner_persona_configured_raises(
        self, tmp_path: Path
    ) -> None:
        config = Config(fallback_persona="Writer", planner_persona=None)
        registry = _registry("Writer")
        planner, _, invoker = _make_planner(
            tmp_path, registry=registry, config=config,
        )

        with pytest.raises(PlannerError, match="planner_persona"):
            await planner.bootstrap(reason="init-tasks", brief="Project brief")

        # Invoker never called when persona is missing.
        invoker.invoke.assert_not_called()

    async def test_planner_persona_missing_from_registry_raises(
        self, tmp_path: Path
    ) -> None:
        config = Config(fallback_persona="Writer", planner_persona="Ghost")
        registry = _registry("Writer")  # no Ghost
        planner, _, invoker = _make_planner(
            tmp_path, registry=registry, config=config,
        )

        with pytest.raises(PlannerError, match="Ghost"):
            await planner.bootstrap(reason="auto", brief="brief")

        invoker.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# _build_planner_prompt (R17.2, R17.5, R7.3, R7.6)
# ---------------------------------------------------------------------------


class TestBuildPlannerPrompt:
    """The planner prompt must guide the LLM toward richer specs.

    Specifically it must:
    - List the available personas by name + description so the
      planner doesn't invent role names (R3.8 mirror).
    - Require a ``persona_review`` check alongside ``file_exists`` so
      tasks exercise content quality instead of just file presence
      (R7.3, R7.6).
    - Warn about self-review (R7.9 recursion guard).
    """

    def test_prompt_lists_available_personas(self) -> None:
        from ralph_loop.models import PersonaDescription
        from ralph_loop.planner import _build_planner_prompt

        prompt = _build_planner_prompt(
            planner_name="Planner",
            brief="Project brief content",
            tasks_path=Path("tasks.json"),
            specs_dir=Path("specs"),
            available_personas=[
                PersonaDescription(name="Writer", description="Drafts prose."),
                PersonaDescription(
                    name="Reviewer", description="Reviews drafts."
                ),
            ],
        )

        assert "Writer: Drafts prose." in prompt
        assert "Reviewer: Reviews drafts." in prompt

    def test_prompt_mandates_persona_review_check(self) -> None:
        from ralph_loop.planner import _build_planner_prompt

        prompt = _build_planner_prompt(
            planner_name="Planner",
            brief="brief",
            tasks_path=Path("tasks.json"),
            specs_dir=Path("specs"),
            available_personas=[],
        )

        # The prompt must explicitly call out the two-check pattern.
        assert "persona_review" in prompt
        assert "file_exists" in prompt
        # And warn about self-review so the planner doesn't route
        # ``target_persona=Writer`` alongside ``persona_review: Writer``.
        assert "self-review" in prompt.lower()

    def test_prompt_includes_spec_body_guidance(self) -> None:
        """The planner should produce Objective/Instructions/Notes prose,
        not one-line placeholders."""
        from ralph_loop.planner import _build_planner_prompt

        prompt = _build_planner_prompt(
            planner_name="Planner",
            brief="brief",
            tasks_path=Path("tasks.json"),
            specs_dir=Path("specs"),
            available_personas=[],
        )

        assert "Objective" in prompt
        assert "Instructions" in prompt
        # The prompt explicitly discourages thin specs.
        assert "Vague one-liners" in prompt or "vague one-liners" in prompt


# ---------------------------------------------------------------------------
# Planner.bootstrap happy path (R17.2, R17.5, R17.6, R17.8, R12.1)
# ---------------------------------------------------------------------------


class TestPlannerBootstrapHappyPath:
    async def test_valid_planner_output_is_admitted(
        self, tmp_path: Path
    ) -> None:
        config = Config(
            fallback_persona="Writer",
            planner_persona="Planner",
            per_iteration_task_creation_budget=10,
            per_run_task_creation_budget=100,
        )
        registry = _registry("Planner", "Writer")
        side_effect = [
            _task_entry("t1", target_persona="Writer"),
            _task_entry("t2"),
            _task_entry("t3", target_persona="Writer"),
        ]
        planner, tasks_path, invoker = _make_planner(
            tmp_path,
            registry=registry,
            config=config,
            invoker_side_effect=side_effect,
        )

        result = await planner.bootstrap(
            reason="init-tasks", brief="Project brief"
        )

        assert len(result.accepted) == 3
        assert result.rejected == []
        assert result.spilled == []

        # R12.1: invoker was called with call_kind="planner"
        invoker.invoke.assert_awaited_once()
        call_kwargs = invoker.invoke.call_args.kwargs
        assert call_kwargs["call_kind"] == "planner"
        assert "Project brief" in call_kwargs["context"]

        # Persisted task list contains the three accepted tasks, stamped
        # with planner metadata.
        persisted = json.loads(tasks_path.read_text(encoding="utf-8"))
        assert {e["id"] for e in persisted} == {"t1", "t2", "t3"}
        for e in persisted:
            assert e["created_by_persona"] == "Planner"
            assert e["created_at_iteration"] == 0

    async def test_invalid_planner_entries_rejected_not_spilled(
        self, tmp_path: Path
    ) -> None:
        config = Config(
            fallback_persona="Writer", planner_persona="Planner",
        )
        registry = _registry("Planner", "Writer")
        # Mix: one valid, one missing required field, one unknown persona.
        side_effect = [
            _task_entry("ok"),
            {"id": "bad-schema"},  # missing title, priority, etc
            _task_entry("bad-persona", target_persona="Ghost"),
        ]
        planner, tasks_path, _ = _make_planner(
            tmp_path,
            registry=registry,
            config=config,
            invoker_side_effect=side_effect,
        )

        result = await planner.bootstrap(reason="auto", brief="brief")

        assert len(result.accepted) == 1
        assert result.accepted[0].id == "ok"
        assert len(result.rejected) == 2
        # R8.4 / R17.6: rejected entries do not flow to the pending queue.
        queue_path = tmp_path / "pending_tasks.json"
        if queue_path.exists():
            queue_contents = json.loads(queue_path.read_text(encoding="utf-8"))
            assert queue_contents == []

    async def test_empty_planner_output_returns_empty_result(
        self, tmp_path: Path
    ) -> None:
        config = Config(
            fallback_persona="Writer", planner_persona="Planner",
        )
        registry = _registry("Planner")
        # Planner writes nothing -> file stays [].
        planner, tasks_path, _ = _make_planner(
            tmp_path, registry=registry, config=config,
            invoker_side_effect=None,
        )

        result = await planner.bootstrap(reason="auto", brief="brief")

        assert result.accepted == []
        assert result.rejected == []
        assert result.spilled == []

    async def test_post_snapshot_non_json_is_handled(
        self, tmp_path: Path
    ) -> None:
        config = Config(
            fallback_persona="Writer", planner_persona="Planner",
        )
        registry = _registry("Planner")
        tasks_path = tmp_path / "tasks.json"
        tasks_path.write_text("[]", encoding="utf-8")
        log_path = tmp_path / "logs" / "planner.log"

        processor = _make_processor(
            tmp_path, registry=registry, config=config, tasks_path=tasks_path,
        )

        invoker = AsyncMock()

        async def _invoke(**_: Any) -> ClaudeInvocationResult:
            # Planner corrupts tasks.json.
            tasks_path.write_text("not json!", encoding="utf-8")
            return ClaudeInvocationResult(
                exit_code=0, stdout="", stderr="",
                token_usage=None, duration_ms=1,
            )

        invoker.invoke.side_effect = _invoke

        planner = Planner(
            invoker=invoker,
            registry=registry,
            config=config,
            processor=processor,
            tasks_path=tasks_path,
            log_path=log_path,
        )

        # The non-JSON file is treated as "no tasks produced"; no error.
        result = await planner.bootstrap(reason="auto", brief="brief")
        assert result.accepted == []
        assert result.rejected == []
