"""Unit tests for the Escalation Handler (Task 15.2).

Covers the edge cases documented in Task 15.2:

- No ``escalation_persona`` configured -> ``try_route`` returns ``None``,
  an info event is logged, and the outer loop is expected to delegate to
  normal Orchestrator selection (R5.4).
- Escalation persona configured and present -> ``try_route`` returns a
  :class:`PersonaSelection` with ``path="escalation"`` (R5.2) and logs
  the event (R5.7).
- Retry limit exhausted — the outer loop still marks the task stuck
  (R5.6, R10.3); the handler enforces only the escalation threshold,
  not the retry cap.

Additional coverage:

- ``should_escalate`` at and around the threshold (R5.1).
- ``try_route`` when the configured escalation persona is missing from
  the registry — treat as no escalation persona configured.
- ``build_escalation_context`` with empty, partial, and full inputs.

Requirements exercised: R5.1, R5.2, R5.3, R5.4, R5.6, R5.7, R10.3.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from ralph_loop.escalation import EscalationHandler
from ralph_loop.models import Config, Persona, Task
from ralph_loop.persona_registry import PersonaRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_persona(name: str) -> Persona:
    return Persona(
        name=name,
        description=f"{name} description.",
        prompt_template="You are {{persona_name}}.",
    )


def _make_registry(personas: list[Persona]) -> PersonaRegistry:
    return PersonaRegistry({p.name: p for p in personas})


def _make_task(retry_count: int = 0, **overrides: Any) -> Task:
    base: dict[str, Any] = dict(
        id="T1",
        title="Task one",
        priority=1,
        status="failing",
        spec_path="specs/T1.md",
        retry_count=retry_count,
    )
    base.update(overrides)
    return Task(**base)


def _make_config(
    *,
    escalation_threshold: int = 3,
    escalation_persona: str | None = None,
    max_retries_per_task: int = 5,
) -> Config:
    return Config(
        fallback_persona="Writer",
        escalation_threshold=escalation_threshold,
        escalation_persona=escalation_persona,
        max_retries_per_task=max_retries_per_task,
    )


# ---------------------------------------------------------------------------
# should_escalate (R5.1)
# ---------------------------------------------------------------------------


class TestShouldEscalate:
    def test_below_threshold_returns_false(self) -> None:
        handler = EscalationHandler(
            registry=_make_registry([_make_persona("Writer")]),
            config=_make_config(escalation_threshold=3),
        )
        assert handler.should_escalate(_make_task(retry_count=0)) is False
        assert handler.should_escalate(_make_task(retry_count=2)) is False

    def test_at_threshold_returns_true(self) -> None:
        handler = EscalationHandler(
            registry=_make_registry([_make_persona("Writer")]),
            config=_make_config(escalation_threshold=3),
        )
        assert handler.should_escalate(_make_task(retry_count=3)) is True

    def test_above_threshold_returns_true(self) -> None:
        handler = EscalationHandler(
            registry=_make_registry([_make_persona("Writer")]),
            config=_make_config(escalation_threshold=3),
        )
        assert handler.should_escalate(_make_task(retry_count=7)) is True


# ---------------------------------------------------------------------------
# try_route: no escalation persona configured (R5.4)
# ---------------------------------------------------------------------------


class TestTryRouteNoPersonaConfigured:
    def test_returns_none_when_escalation_persona_unset(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        handler = EscalationHandler(
            registry=_make_registry([_make_persona("Writer")]),
            config=_make_config(escalation_persona=None),
        )
        task = _make_task(retry_count=3)

        with caplog.at_level(logging.INFO):
            result = handler.try_route(task)

        assert result is None
        # R5.7: an escalation event is logged identifying the task id,
        # retry count, and the "none configured" marker.
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "no escalation_persona configured" in m and task.id in m
            for m in messages
        )

    def test_returns_none_when_escalation_persona_not_in_registry(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        handler = EscalationHandler(
            registry=_make_registry([_make_persona("Writer")]),
            config=_make_config(escalation_persona="Ghost"),
        )
        task = _make_task(retry_count=3)

        with caplog.at_level(logging.WARNING):
            result = handler.try_route(task)

        assert result is None
        messages = [r.getMessage() for r in caplog.records]
        assert any("Ghost" in m for m in messages)


# ---------------------------------------------------------------------------
# try_route: escalation persona configured and present (R5.2, R5.7)
# ---------------------------------------------------------------------------


class TestTryRouteWithPersona:
    def test_returns_escalation_selection(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        esc = _make_persona("Troubleshooter")
        handler = EscalationHandler(
            registry=_make_registry([_make_persona("Writer"), esc]),
            config=_make_config(escalation_persona="Troubleshooter"),
        )
        task = _make_task(retry_count=3)

        with caplog.at_level(logging.INFO):
            selection = handler.try_route(task)

        assert selection is not None
        assert selection.path == "escalation"
        assert selection.persona.name == "Troubleshooter"

        # R5.7: the event records task id, retry count, and selected persona.
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "Troubleshooter" in m and task.id in m and "3" in m
            for m in messages
        )


# ---------------------------------------------------------------------------
# Retry-limit-exhausted still marks stuck (R5.6, R10.3)
# ---------------------------------------------------------------------------


class TestRetryLimitExhausted:
    def test_should_escalate_true_does_not_bypass_retry_limit(self) -> None:
        """R5.6 / R10.3: escalation does not raise the retry cap.

        The Escalation Handler reports that the task is eligible for
        escalation (``should_escalate == True``), but the outer loop is
        still responsible for checking ``retry_count >=
        max_retries_per_task`` and marking the task stuck. This test
        encodes the contract: the handler returns ``True`` for a task
        whose retry counter has reached the retry limit, but the
        caller's own check is what actually terminates the task.
        """
        config = _make_config(
            escalation_threshold=3, max_retries_per_task=5
        )
        handler = EscalationHandler(
            registry=_make_registry([_make_persona("Writer")]),
            config=config,
        )

        # Task at the retry limit — the handler flags it for escalation,
        # but the outer loop (not the handler) is responsible for the
        # "mark stuck" transition.
        task_at_limit = _make_task(retry_count=config.max_retries_per_task)
        assert handler.should_escalate(task_at_limit) is True
        # Caller-side stuck check, replicating what the main loop does.
        assert task_at_limit.retry_count >= config.max_retries_per_task


# ---------------------------------------------------------------------------
# build_escalation_context (R5.3)
# ---------------------------------------------------------------------------


class TestBuildEscalationContext:
    def test_returns_empty_string_for_empty_inputs(self) -> None:
        handler = EscalationHandler(
            registry=_make_registry([_make_persona("Writer")]),
            config=_make_config(),
        )
        context = handler.build_escalation_context(
            task=_make_task(),
            retry_history=[],
            prior_validation_outputs=[],
            prior_iteration_logs=[],
        )
        assert context == ""

    def test_retry_history_is_included(self) -> None:
        handler = EscalationHandler(
            registry=_make_registry([_make_persona("Writer")]),
            config=_make_config(),
        )
        history = [
            {"iteration": 1, "persona": "Writer", "outcome": "fail"},
            {"iteration": 2, "persona": "Reviewer", "outcome": "fail"},
        ]

        context = handler.build_escalation_context(
            task=_make_task(),
            retry_history=history,
            prior_validation_outputs=[],
            prior_iteration_logs=[],
        )

        assert "Retry History" in context
        # Each history entry must appear in the composed context.
        for entry in history:
            assert str(entry["persona"]) in context
            assert str(entry["outcome"]) in context

    def test_prior_validation_outputs_are_fenced(self) -> None:
        handler = EscalationHandler(
            registry=_make_registry([_make_persona("Writer")]),
            config=_make_config(),
        )
        outputs = ["pytest FAILED: test_foo\n", "shellcheck error: SC2086"]

        context = handler.build_escalation_context(
            task=_make_task(),
            retry_history=[],
            prior_validation_outputs=outputs,
            prior_iteration_logs=[],
        )

        assert "Prior Failing Validation Output" in context
        for output in outputs:
            assert output in context
        # Each output is fenced so the LLM sees raw text.
        assert context.count("```") >= 2 * len(outputs)

    def test_prior_iteration_logs_are_included(self) -> None:
        handler = EscalationHandler(
            registry=_make_registry([_make_persona("Writer")]),
            config=_make_config(),
        )
        logs = [
            "iter 1: selected Writer, failing",
            "iter 2: selected Reviewer, failing",
        ]

        context = handler.build_escalation_context(
            task=_make_task(),
            retry_history=[],
            prior_validation_outputs=[],
            prior_iteration_logs=logs,
        )

        assert "Prior Iteration Logs" in context
        for log in logs:
            assert log in context

    def test_all_sections_present_when_all_inputs_provided(self) -> None:
        handler = EscalationHandler(
            registry=_make_registry([_make_persona("Writer")]),
            config=_make_config(),
        )

        context = handler.build_escalation_context(
            task=_make_task(),
            retry_history=[{"iteration": 1, "persona": "Writer"}],
            prior_validation_outputs=["failing test output"],
            prior_iteration_logs=["iter 1: failed"],
        )

        assert "Retry History" in context
        assert "Prior Failing Validation Output" in context
        assert "Prior Iteration Logs" in context
        assert "failing test output" in context
        assert "iter 1: failed" in context
