"""Unit tests for Pydantic model validation edges (Task 2.5).

Covers:
- JSON round-trip for ``Task``, ``TaskSpec``, ``Persona``, ``Config``
  (via ``model_dump_json`` / ``model_validate_json``) and for the
  top-level ``tasks.json`` list through ``TASK_LIST_ADAPTER``.
- ``ValidationError`` on missing required fields, bad enum values,
  negative ``retry_count``, empty or mistyped check-type
  discriminators, persona and config required fields, and negative
  ``ModelPrice`` values.

These are plain pytest unit tests; property-based tests for specific
universal properties come in later tasks.

Requirements exercised: 2.2, 3.6, 7.1, 18.1, 18.7.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ralph_loop.models import (
    Config,
    FileExistsCheckConfig,
    ModelPrice,
    Persona,
    PersonaReviewCheckConfig,
    ShellCheckConfig,
    TASK_LIST_ADAPTER,
    Task,
    TaskSpec,
    TaskSpecBody,
    ToolRestrictions,
)


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _make_task(**overrides) -> Task:
    data: dict = {
        "id": "T-1",
        "title": "Draft intro",
        "priority": 1,
        "status": "pending",
        "spec_path": "specs/T-1.md",
        "retry_count": 0,
    }
    data.update(overrides)
    return Task(**data)


def _make_task_spec(**overrides) -> TaskSpec:
    body = TaskSpecBody(
        objective="Draft an introduction.",
        context_references="SUMMARY.md",
        instructions="Write 400 words of prose.",
        notes=None,
    )
    data: dict = {
        "id": "T-1",
        "title": "Draft intro",
        "target_persona": "Writer",
        "tags": ["draft"],
        "depends_on": None,
        "persona_fields": {"chapter": "01"},
        "validation": [
            ShellCheckConfig(type="shell", name="lint", commands=["true"]),
            FileExistsCheckConfig(
                type="file_exists", name="draft-present", paths=["drafts/intro.md"]
            ),
            PersonaReviewCheckConfig(
                type="persona_review",
                name="editorial",
                persona="Editor",
                pass_condition="no critical issues",
            ),
        ],
        "context_files": ["drafts/intro.md"],
        "body": body,
    }
    data.update(overrides)
    return TaskSpec(**data)


def _make_persona(**overrides) -> Persona:
    data: dict = {
        "name": "Writer",
        "description": "Drafts new prose from an outline.",
        "prompt_template": (
            "You are {{persona_name}} working on task {{task_id}}: {{task_title}}.\n"
            "Project brief:\n{{project_brief}}\n\nTask spec:\n{{task_spec}}"
        ),
        "instructions": "Write clearly and concisely.",
        "tool_restrictions": ToolRestrictions(allow=["fs_write"], disallow=["shell"]),
        "default_persona_review_pass_condition": "no critical issues",
    }
    data.update(overrides)
    return Persona(**data)


def _make_config(**overrides) -> Config:
    data: dict = {
        "fallback_persona": "Writer",
        "escalation_persona": "Editor",
        "planner_persona": "Planner",
        "model_pricing": {
            "kiro-default": ModelPrice(
                input_price_per_token=0.000_001,
                output_price_per_token=0.000_003,
            ),
        },
    }
    data.update(overrides)
    return Config(**data)


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    """``model_dump_json`` -> ``model_validate_json`` is an identity map."""

    def test_task_round_trip_preserves_all_fields(self) -> None:
        original = _make_task(
            priority=5,
            status="failing",
            retry_count=2,
            target_persona="Writer",
            depends_on=["T-0"],
            tags=["draft", "chapter-1"],
            created_at_iteration=3,
            created_by_persona="Planner",
            creation_chain=["Planner"],
            spilled_run_id="run-A",
            admitted_run_id="run-B",
            resumed_from_interruption=True,
        )

        restored = Task.model_validate_json(original.model_dump_json())

        assert restored == original

    def test_task_spec_round_trip_preserves_discriminated_checks(self) -> None:
        original = _make_task_spec()

        restored = TaskSpec.model_validate_json(original.model_dump_json())

        assert restored == original
        # The discriminated union must re-hydrate to the right concrete types.
        assert isinstance(restored.validation[0], ShellCheckConfig)
        assert isinstance(restored.validation[1], FileExistsCheckConfig)
        assert isinstance(restored.validation[2], PersonaReviewCheckConfig)

    def test_persona_round_trip_preserves_tool_restrictions(self) -> None:
        original = _make_persona()

        restored = Persona.model_validate_json(original.model_dump_json())

        assert restored == original
        assert restored.tool_restrictions is not None
        assert restored.tool_restrictions.allow == ["fs_write"]
        assert restored.tool_restrictions.disallow == ["shell"]

    def test_config_round_trip_preserves_pricing_map(self) -> None:
        original = _make_config()

        restored = Config.model_validate_json(original.model_dump_json())

        assert restored == original
        assert "kiro-default" in restored.model_pricing
        assert restored.model_pricing["kiro-default"].input_price_per_token == pytest.approx(
            0.000_001
        )

    def test_task_list_adapter_round_trip_preserves_all_tasks(self) -> None:
        tasks = [
            _make_task(id="T-1", priority=1, status="pending"),
            _make_task(id="T-2", priority=2, status="passing", retry_count=1),
            _make_task(
                id="T-3",
                priority=3,
                status="stuck",
                depends_on=["T-1", "T-2"],
                target_persona="Editor",
            ),
        ]

        raw = TASK_LIST_ADAPTER.dump_json(tasks)
        restored = TASK_LIST_ADAPTER.validate_json(raw)

        assert restored == tasks


# ---------------------------------------------------------------------------
# Task validation errors (R2.2)
# ---------------------------------------------------------------------------


class TestTaskValidation:
    def test_missing_id_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Task(
                # no ``id`` field on purpose
                title="Draft intro",
                priority=1,
                status="pending",
                spec_path="specs/T-1.md",
            )

        errors = excinfo.value.errors()
        assert any(err["loc"] == ("id",) for err in errors)

    def test_empty_id_raises_validation_error(self) -> None:
        # ``min_length=1`` on ``id`` protects against blank identifiers.
        with pytest.raises(ValidationError):
            _make_task(id="")

    def test_bad_status_enum_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _make_task(status="unknown")

        errors = excinfo.value.errors()
        assert any(err["loc"] == ("status",) for err in errors)

    def test_negative_retry_count_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _make_task(retry_count=-1)

        errors = excinfo.value.errors()
        assert any(err["loc"] == ("retry_count",) for err in errors)

    def test_negative_created_at_iteration_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            _make_task(created_at_iteration=-5)


# ---------------------------------------------------------------------------
# TaskSpec validation errors (R7.1, R18.1, R18.7)
# ---------------------------------------------------------------------------


class TestTaskSpecValidation:
    def test_empty_validation_list_raises_validation_error(self) -> None:
        # R18.1: ``validation`` must contain at least one check.
        with pytest.raises(ValidationError) as excinfo:
            _make_task_spec(validation=[])

        errors = excinfo.value.errors()
        assert any(err["loc"][:1] == ("validation",) for err in errors)

    def test_missing_validation_field_raises_validation_error(self) -> None:
        # R18.1: ``validation`` is required.
        body = TaskSpecBody(
            objective="o", context_references="c", instructions="i"
        )
        with pytest.raises(ValidationError) as excinfo:
            TaskSpec(id="T-1", title="t", body=body)

        errors = excinfo.value.errors()
        assert any(err["loc"] == ("validation",) for err in errors)

    def test_bogus_check_type_discriminator_raises_validation_error(self) -> None:
        # R7.1 + R18.7: the discriminated union must reject unknown ``type`` values.
        with pytest.raises(ValidationError) as excinfo:
            _make_task_spec(
                validation=[{"type": "bogus", "name": "n", "commands": ["true"]}]
            )

        errors = excinfo.value.errors()
        # Pydantic reports a discriminator error under the validation entry; at
        # minimum the error surface must point at the ``validation`` list.
        assert any(err["loc"] and err["loc"][0] == "validation" for err in errors)
        # And the bogus tag should appear in the error message surface.
        assert any("bogus" in str(err).lower() or "tag" in err["type"] for err in errors)

    def test_shell_check_requires_at_least_one_command(self) -> None:
        with pytest.raises(ValidationError):
            _make_task_spec(
                validation=[{"type": "shell", "name": "n", "commands": []}]
            )

    def test_file_exists_check_requires_at_least_one_path(self) -> None:
        with pytest.raises(ValidationError):
            _make_task_spec(
                validation=[{"type": "file_exists", "name": "n", "paths": []}]
            )

    def test_persona_review_check_requires_persona(self) -> None:
        with pytest.raises(ValidationError):
            _make_task_spec(
                validation=[
                    {
                        "type": "persona_review",
                        "name": "editorial",
                        "pass_condition": "ok",
                        # missing ``persona``
                    }
                ]
            )

    def test_missing_body_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            TaskSpec(
                id="T-1",
                title="t",
                validation=[ShellCheckConfig(type="shell", commands=["true"])],
                # no ``body``
            )

        errors = excinfo.value.errors()
        assert any(err["loc"] == ("body",) for err in errors)


# ---------------------------------------------------------------------------
# Persona validation errors (R3.6)
# ---------------------------------------------------------------------------


class TestPersonaValidation:
    def test_missing_description_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Persona(
                name="Writer",
                # no ``description`` field on purpose
                prompt_template="{{persona_name}}",
            )

        errors = excinfo.value.errors()
        assert any(err["loc"] == ("description",) for err in errors)

    def test_missing_name_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Persona(
                description="Drafts prose.",
                prompt_template="{{persona_name}}",
            )

        errors = excinfo.value.errors()
        assert any(err["loc"] == ("name",) for err in errors)

    def test_missing_prompt_template_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Persona(
                name="Writer",
                description="Drafts prose.",
            )

        errors = excinfo.value.errors()
        assert any(err["loc"] == ("prompt_template",) for err in errors)


# ---------------------------------------------------------------------------
# Config validation errors
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_missing_fallback_persona_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Config()  # ``fallback_persona`` is the only required field.

        errors = excinfo.value.errors()
        assert any(err["loc"] == ("fallback_persona",) for err in errors)

    def test_defaults_populate_when_only_fallback_persona_is_set(self) -> None:
        cfg = Config(fallback_persona="Writer")

        assert cfg.tasks_path == "tasks.json"
        assert cfg.summary_path == "SUMMARY.md"
        assert cfg.personas_dir == "personas/"
        assert cfg.pending_tasks_path == "pending_tasks.json"
        assert cfg.escalation_threshold == 3
        assert cfg.max_iterations == 50
        assert cfg.max_retries_per_task == 5
        assert cfg.git_integration_enabled is True
        assert cfg.automatic_planner is False
        assert cfg.model_pricing == {}

    def test_negative_escalation_threshold_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            Config(fallback_persona="Writer", escalation_threshold=-1)

    def test_zero_max_iterations_raises_validation_error(self) -> None:
        # ``max_iterations`` requires ``ge=1``; 0 is rejected.
        with pytest.raises(ValidationError):
            Config(fallback_persona="Writer", max_iterations=0)


# ---------------------------------------------------------------------------
# ModelPrice validation
# ---------------------------------------------------------------------------


class TestModelPriceValidation:
    def test_negative_input_price_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            ModelPrice(
                input_price_per_token=-0.0001,
                output_price_per_token=0.0001,
            )

        errors = excinfo.value.errors()
        assert any(err["loc"] == ("input_price_per_token",) for err in errors)

    def test_negative_output_price_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            ModelPrice(
                input_price_per_token=0.0001,
                output_price_per_token=-0.0001,
            )

        errors = excinfo.value.errors()
        assert any(err["loc"] == ("output_price_per_token",) for err in errors)

    def test_zero_prices_accepted(self) -> None:
        # ``ge=0`` so free-tier / unbilled models round-trip without error.
        price = ModelPrice(input_price_per_token=0.0, output_price_per_token=0.0)

        restored = ModelPrice.model_validate_json(price.model_dump_json())
        assert restored == price
