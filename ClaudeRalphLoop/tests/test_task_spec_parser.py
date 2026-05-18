"""Unit tests for ``ralph_loop.task_spec.parse_task_spec`` (Task 11.1).

These are plain pytest unit tests for the Task Spec parser's
happy-path and failure-path behaviors. The matching property test
(P11) lands in :mod:`tests.test_task_spec_parser_properties`.

Requirements exercised: 7.1, 18.1, 18.2, 18.3, 18.4, 18.7.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_loop.models import (
    FileExistsCheckConfig,
    PersonaReviewCheckConfig,
    ShellCheckConfig,
    TaskSpec,
)
from ralph_loop.task_spec import TaskSpecParseError, parse_task_spec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_spec(
    path: Path,
    *,
    frontmatter: str,
    body: str = "",
) -> Path:
    """Materialize a spec file on disk and return its path."""
    content = f"---\n{frontmatter.strip()}\n---\n{body}"
    path.write_text(content, encoding="utf-8")
    return path


# A reusable minimal-valid frontmatter block. Tests mutate one field at
# a time to isolate which ValidationError message they're asserting on.
_MINIMAL_FRONTMATTER = """
id: t-001
title: Draft outline
validation:
  - type: shell
    commands:
      - "true"
""".strip()


# ---------------------------------------------------------------------------
# Happy-path parsing
# ---------------------------------------------------------------------------


class TestHappyPathParsing:
    def test_minimal_valid_spec_returns_taskspec(self, tmp_path: Path) -> None:
        path = _write_spec(
            tmp_path / "spec.md",
            frontmatter=_MINIMAL_FRONTMATTER,
            body="",
        )

        spec = parse_task_spec(path)

        assert isinstance(spec, TaskSpec)
        assert spec.id == "t-001"
        assert spec.title == "Draft outline"
        assert len(spec.validation) == 1
        assert isinstance(spec.validation[0], ShellCheckConfig)
        assert spec.validation[0].commands == ["true"]
        # Missing body sections default to empty strings.
        assert spec.body.objective == ""
        assert spec.body.context_references == ""
        assert spec.body.instructions == ""
        assert spec.body.notes is None

    def test_full_body_with_all_sections(self, tmp_path: Path) -> None:
        body = (
            "## Objective\n"
            "Write chapter one.\n"
            "\n"
            "## Context References\n"
            "- specs/outline.md\n"
            "\n"
            "## Instructions\n"
            "Follow the outline tightly.\n"
            "\n"
            "## Notes\n"
            "Keep voice consistent.\n"
        )
        path = _write_spec(
            tmp_path / "spec.md",
            frontmatter=_MINIMAL_FRONTMATTER,
            body=body,
        )

        spec = parse_task_spec(path)

        assert spec.body.objective == "Write chapter one."
        assert spec.body.context_references == "- specs/outline.md"
        assert spec.body.instructions == "Follow the outline tightly."
        assert spec.body.notes == "Keep voice consistent."

    def test_only_objective_section_leaves_others_empty(
        self, tmp_path: Path
    ) -> None:
        body = "## Objective\nWrite chapter one.\n"
        path = _write_spec(
            tmp_path / "spec.md",
            frontmatter=_MINIMAL_FRONTMATTER,
            body=body,
        )

        spec = parse_task_spec(path)

        assert spec.body.objective == "Write chapter one."
        assert spec.body.context_references == ""
        assert spec.body.instructions == ""
        assert spec.body.notes is None

    def test_optional_frontmatter_fields_are_preserved(
        self, tmp_path: Path
    ) -> None:
        frontmatter = """
id: t-002
title: Review chapter one
target_persona: Reviewer
tags:
  - quality
  - review
depends_on:
  - t-001
persona_fields:
  focus: clarity
context_files:
  - specs/outline.md
  - summary.md
validation:
  - type: persona_review
    persona: Editor
    pass_condition: no critical issues
""".strip()
        path = _write_spec(
            tmp_path / "spec.md",
            frontmatter=frontmatter,
            body="## Objective\nReview ch1.\n",
        )

        spec = parse_task_spec(path)

        assert spec.target_persona == "Reviewer"
        assert spec.tags == ["quality", "review"]
        assert spec.depends_on == ["t-001"]
        assert spec.persona_fields == {"focus": "clarity"}
        assert spec.context_files == ["specs/outline.md", "summary.md"]
        assert isinstance(spec.validation[0], PersonaReviewCheckConfig)
        assert spec.validation[0].persona == "Editor"
        assert spec.validation[0].pass_condition == "no critical issues"

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        path = _write_spec(
            tmp_path / "spec.md",
            frontmatter=_MINIMAL_FRONTMATTER,
        )

        spec = parse_task_spec(str(path))

        assert spec.id == "t-001"

    def test_file_exists_check_type_dispatches(self, tmp_path: Path) -> None:
        frontmatter = """
id: t-003
title: Build artifacts
validation:
  - type: file_exists
    paths:
      - dist/out.html
""".strip()
        path = _write_spec(
            tmp_path / "spec.md",
            frontmatter=frontmatter,
        )

        spec = parse_task_spec(path)

        assert isinstance(spec.validation[0], FileExistsCheckConfig)
        assert spec.validation[0].paths == ["dist/out.html"]


# ---------------------------------------------------------------------------
# Frontmatter / delimiter errors
# ---------------------------------------------------------------------------


class TestFrontmatterErrors:
    def test_missing_frontmatter_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.md"
        path.write_text("just a body, no frontmatter\n", encoding="utf-8")

        with pytest.raises(TaskSpecParseError) as excinfo:
            parse_task_spec(path)

        assert "missing YAML frontmatter" in str(excinfo.value)

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.md"
        path.write_text(
            "---\nid: t-001\ntitle: [unterminated\n---\n",
            encoding="utf-8",
        )

        with pytest.raises(TaskSpecParseError) as excinfo:
            parse_task_spec(path)

        assert "Invalid YAML" in str(excinfo.value)

    def test_non_mapping_frontmatter_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.md"
        # A YAML sequence rather than a mapping.
        path.write_text("---\n- a\n- b\n---\n", encoding="utf-8")

        with pytest.raises(TaskSpecParseError) as excinfo:
            parse_task_spec(path)

        assert "mapping" in str(excinfo.value)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.md"

        with pytest.raises(TaskSpecParseError) as excinfo:
            parse_task_spec(missing)

        assert "Failed to read" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Schema-validation errors (R18.7)
# ---------------------------------------------------------------------------


class TestSchemaErrors:
    def test_missing_id_identifies_id_field(self, tmp_path: Path) -> None:
        frontmatter = """
title: no id here
validation:
  - type: shell
    commands: ["true"]
""".strip()
        path = _write_spec(tmp_path / "spec.md", frontmatter=frontmatter)

        with pytest.raises(TaskSpecParseError) as excinfo:
            parse_task_spec(path)

        assert excinfo.value.field == "id"
        assert excinfo.value.task_id is None

    def test_missing_title_identifies_title_field(
        self, tmp_path: Path
    ) -> None:
        frontmatter = """
id: t-001
validation:
  - type: shell
    commands: ["true"]
""".strip()
        path = _write_spec(tmp_path / "spec.md", frontmatter=frontmatter)

        with pytest.raises(TaskSpecParseError) as excinfo:
            parse_task_spec(path)

        assert excinfo.value.field == "title"
        # Task id is available because it was present in the frontmatter.
        assert excinfo.value.task_id == "t-001"

    def test_missing_validation_identifies_validation_field(
        self, tmp_path: Path
    ) -> None:
        frontmatter = """
id: t-001
title: needs validation
""".strip()
        path = _write_spec(tmp_path / "spec.md", frontmatter=frontmatter)

        with pytest.raises(TaskSpecParseError) as excinfo:
            parse_task_spec(path)

        assert excinfo.value.field == "validation"

    def test_empty_validation_list_identifies_validation_field(
        self, tmp_path: Path
    ) -> None:
        frontmatter = """
id: t-001
title: empty checks
validation: []
""".strip()
        path = _write_spec(tmp_path / "spec.md", frontmatter=frontmatter)

        with pytest.raises(TaskSpecParseError) as excinfo:
            parse_task_spec(path)

        assert excinfo.value.field == "validation"

    def test_invalid_check_type_discriminator_is_reported(
        self, tmp_path: Path
    ) -> None:
        frontmatter = """
id: t-001
title: bad discriminator
validation:
  - type: not_a_real_check_type
    commands: ["true"]
""".strip()
        path = _write_spec(tmp_path / "spec.md", frontmatter=frontmatter)

        with pytest.raises(TaskSpecParseError) as excinfo:
            parse_task_spec(path)

        # The Pydantic error path for a bad discriminator includes the
        # ``validation`` key and the tagged-union location. We only
        # assert that the field path references ``validation``.
        assert excinfo.value.field is not None
        assert "validation" in excinfo.value.field

    def test_persona_review_missing_persona_is_reported(
        self, tmp_path: Path
    ) -> None:
        frontmatter = """
id: t-001
title: review without persona
validation:
  - type: persona_review
    pass_condition: no issues
""".strip()
        path = _write_spec(tmp_path / "spec.md", frontmatter=frontmatter)

        with pytest.raises(TaskSpecParseError) as excinfo:
            parse_task_spec(path)

        assert excinfo.value.field is not None
        assert "validation" in excinfo.value.field
