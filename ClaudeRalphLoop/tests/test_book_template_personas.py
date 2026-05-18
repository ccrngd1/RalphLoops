"""Unit tests for the shipped book-authoring persona template (Task 25.2).

Validates that ``templates/book/personas/`` contains six personas that
parse cleanly through :class:`PersonaRegistry.load`, that the
reviewer-style personas carry a ``default_persona_review_pass_condition``
(R7.7), and that each persona's prompt template renders correctly via
:func:`ralph_loop.prompt_template.render_prompt` (R3.7, R6.4).

Requirements exercised: 3.2, 3.7, 7.7, 16.7, 17.1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_loop.models import Persona
from ralph_loop.persona_registry import PersonaRegistry
from ralph_loop.prompt_template import render_prompt


# Resolve the template path relative to the repo root so the test does
# not depend on pytest's cwd.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BOOK_PERSONAS_DIR = _REPO_ROOT / "templates" / "book" / "personas"


# The canonical list of personas the book template ships with. Keep
# this explicit (rather than derived from the directory listing) so an
# accidental deletion or rename is caught by the test.
_EXPECTED_NAMES = {
    "Writer",
    "Reviewer",
    "Editor",
    "FactChecker",
    "Outline",
    "Planner",
}

# The reviewer-style personas MUST supply a default pass condition so a
# ``persona_review`` check that omits ``pass_condition`` can still be
# resolved at runtime without marking the task stuck (R7.7, R7.8).
_REVIEWER_NAMES = {"Reviewer", "Editor", "FactChecker"}

# The generator / non-reviewer personas intentionally omit the default
# pass condition; they never sit on the validator's review path.
_NON_REVIEWER_NAMES = {"Writer", "Outline", "Planner"}


@pytest.fixture(scope="module")
def registry() -> PersonaRegistry:
    """Load the book persona template once per test module."""
    return PersonaRegistry.load(_BOOK_PERSONAS_DIR)


def test_all_expected_personas_loaded(registry: PersonaRegistry) -> None:
    """Every persona named in the book template must load."""
    loaded_names = {p.name for p in registry.all()}
    assert loaded_names == _EXPECTED_NAMES


@pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
def test_persona_validates_as_pydantic_model(
    registry: PersonaRegistry, name: str
) -> None:
    """Each persona must parse as a valid :class:`Persona`.

    :meth:`PersonaRegistry.load` already calls ``Persona(**data)``
    internally, but we re-round-trip through ``model_dump`` /
    ``model_validate`` to catch any non-canonical fields that slipped
    through a lenient loader.
    """
    persona = registry.get(name)
    assert persona is not None
    assert isinstance(persona, Persona)

    roundtripped = Persona.model_validate(persona.model_dump())
    assert roundtripped == persona

    # Basic field hygiene.
    assert persona.description.strip()
    assert persona.prompt_template.strip()


@pytest.mark.parametrize("name", sorted(_REVIEWER_NAMES))
def test_reviewer_personas_have_default_pass_condition(
    registry: PersonaRegistry, name: str
) -> None:
    """Reviewer-style personas ship with a default pass condition (R7.7)."""
    persona = registry.get(name)
    assert persona is not None
    assert persona.default_persona_review_pass_condition is not None
    assert persona.default_persona_review_pass_condition.strip()


@pytest.mark.parametrize("name", sorted(_NON_REVIEWER_NAMES))
def test_non_reviewer_personas_have_no_default_pass_condition(
    registry: PersonaRegistry, name: str
) -> None:
    """Generator personas (Writer, Outline, Planner) intentionally omit
    ``default_persona_review_pass_condition`` because they never run on
    the validator's ``persona_review`` path. Documenting that choice
    here catches accidental additions during future template edits.
    """
    persona = registry.get(name)
    assert persona is not None
    assert persona.default_persona_review_pass_condition is None


@pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
def test_prompt_template_renders_all_supported_placeholders(
    registry: PersonaRegistry, name: str
) -> None:
    """Each persona's prompt template must substitute the five
    supported placeholders (R3.7).

    The template must reference ``{{persona_name}}``, ``{{task_id}}``,
    ``{{task_title}}``, ``{{project_brief}}``, and ``{{task_spec}}`` at
    least once; after rendering, no ``{{...}}`` marker from that set
    may remain.
    """
    persona = registry.get(name)
    assert persona is not None

    for placeholder in (
        "{{persona_name}}",
        "{{task_id}}",
        "{{task_title}}",
        "{{project_brief}}",
        "{{task_spec}}",
    ):
        assert placeholder in persona.prompt_template, (
            f"Persona {name} must reference {placeholder} in its prompt"
        )

    rendered = render_prompt(
        persona.prompt_template,
        project_brief="PROJECT_BRIEF_VALUE",
        task_spec="TASK_SPEC_VALUE",
        task_id="task-123",
        task_title="Example Task",
        persona_name=persona.name,
    )

    # All five substitutions took effect.
    assert "PROJECT_BRIEF_VALUE" in rendered
    assert "TASK_SPEC_VALUE" in rendered
    assert "task-123" in rendered
    assert "Example Task" in rendered
    assert persona.name in rendered

    # No leftover supported-placeholder markers.
    for placeholder in (
        "{{persona_name}}",
        "{{task_id}}",
        "{{task_title}}",
        "{{project_brief}}",
        "{{task_spec}}",
    ):
        assert placeholder not in rendered
