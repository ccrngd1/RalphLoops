"""Unit tests for the Persona Registry loader (Task 8.1).

These are plain pytest unit tests for the loader's happy-path and
fail-fast behaviors. The matching property test (P5) for deterministic
mapping, duplicate detection, and missing-field handling lands in task
8.2.

Requirements exercised: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.8.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_loop.models import Persona, PersonaDescription
from ralph_loop.persona_registry import PersonaRegistry, PersonaRegistryError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml_persona(
    dir: Path,
    filename: str,
    *,
    name: str = "Writer",
    description: str = "Drafts new prose from an outline.",
    prompt_template: str = "You are {{persona_name}} on {{task_id}}.",
    instructions: str | None = "Write clearly.",
    default_pass: str | None = None,
) -> Path:
    """Write a minimal YAML persona file and return its path."""
    lines = [
        f"name: {name}",
        f"description: {description}",
        # Use single-quoted YAML string to safely embed the braces.
        f"prompt_template: '{prompt_template}'",
    ]
    if instructions is not None:
        lines.append(f"instructions: '{instructions}'")
    if default_pass is not None:
        lines.append(f"default_persona_review_pass_condition: '{default_pass}'")
    path = dir / filename
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_md_persona(
    dir: Path,
    filename: str,
    *,
    name: str = "Reviewer",
    description: str = "Reviews drafts and emits a verdict.",
    prompt_template: str = "You are {{persona_name}}.",
    body: str = "## Reviewer\n\nDetailed reviewer guidance.",
) -> Path:
    """Write a Markdown-with-YAML-frontmatter persona file."""
    content = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"prompt_template: '{prompt_template}'\n"
        "---\n"
        f"{body}\n"
    )
    path = dir / filename
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Missing directory / empty directory (R3.1)
# ---------------------------------------------------------------------------


class TestLoadDirectoryPreconditions:
    def test_missing_directory_raises_persona_registry_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"

        with pytest.raises(PersonaRegistryError) as excinfo:
            PersonaRegistry.load(missing)

        assert str(missing) in str(excinfo.value)

    def test_path_is_a_file_raises_persona_registry_error(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "file.txt"
        not_a_dir.write_text("hello", encoding="utf-8")

        with pytest.raises(PersonaRegistryError):
            PersonaRegistry.load(not_a_dir)

    def test_empty_directory_yields_empty_registry(self, tmp_path: Path) -> None:
        registry = PersonaRegistry.load(tmp_path)

        assert registry.all() == []
        assert registry.describe_all_for_orchestrator() == []
        assert registry.get("anything") is None


# ---------------------------------------------------------------------------
# Happy path (R3.2, R3.4)
# ---------------------------------------------------------------------------


class TestLoadHappyPath:
    def test_single_yaml_persona_is_indexed_by_name(self, tmp_path: Path) -> None:
        _write_yaml_persona(tmp_path, "writer.yaml", name="Writer")

        registry = PersonaRegistry.load(tmp_path)

        writer = registry.get("Writer")
        assert isinstance(writer, Persona)
        assert writer.name == "Writer"
        assert registry.all() == [writer]

    def test_yml_extension_is_supported(self, tmp_path: Path) -> None:
        _write_yaml_persona(tmp_path, "writer.yml", name="Writer")

        registry = PersonaRegistry.load(tmp_path)

        assert registry.get("Writer") is not None

    def test_markdown_persona_with_frontmatter_is_parsed(self, tmp_path: Path) -> None:
        _write_md_persona(tmp_path, "reviewer.md", name="Reviewer")

        registry = PersonaRegistry.load(tmp_path)

        reviewer = registry.get("Reviewer")
        assert reviewer is not None
        assert reviewer.name == "Reviewer"
        assert reviewer.description.startswith("Reviews drafts")
        # The markdown body must not leak into the persona fields.
        assert "Detailed reviewer guidance" not in (reviewer.instructions or "")

    def test_multiple_personas_returned_in_sorted_order(self, tmp_path: Path) -> None:
        _write_yaml_persona(tmp_path, "zeta.yaml", name="Zeta")
        _write_yaml_persona(tmp_path, "alpha.yaml", name="Alpha")
        _write_md_persona(tmp_path, "mid.md", name="Mid")

        registry = PersonaRegistry.load(tmp_path)

        names = [p.name for p in registry.all()]
        assert names == ["Alpha", "Mid", "Zeta"]


# ---------------------------------------------------------------------------
# Fail-fast: duplicate names (R3.5)
# ---------------------------------------------------------------------------


class TestDuplicateNames:
    def test_duplicate_name_across_files_raises_and_names_file(
        self, tmp_path: Path
    ) -> None:
        _write_yaml_persona(tmp_path, "a-writer.yaml", name="Writer")
        second = _write_yaml_persona(tmp_path, "b-writer.yaml", name="Writer")

        with pytest.raises(PersonaRegistryError) as excinfo:
            PersonaRegistry.load(tmp_path)

        msg = str(excinfo.value)
        assert "Writer" in msg
        # The message must identify the second file, so operators can
        # delete or rename it without reading every persona file.
        assert str(second) in msg

    def test_duplicate_across_yaml_and_md_is_detected(self, tmp_path: Path) -> None:
        _write_yaml_persona(tmp_path, "writer.yaml", name="Writer")
        _write_md_persona(tmp_path, "writer.md", name="Writer")

        with pytest.raises(PersonaRegistryError) as excinfo:
            PersonaRegistry.load(tmp_path)

        assert "Writer" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Fail-fast: missing required fields (R3.6)
# ---------------------------------------------------------------------------


class TestMissingRequiredFields:
    def test_missing_description_raises_with_field_and_file(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.yaml"
        path.write_text(
            "name: Writer\nprompt_template: 'x'\n",
            encoding="utf-8",
        )

        with pytest.raises(PersonaRegistryError) as excinfo:
            PersonaRegistry.load(tmp_path)

        msg = str(excinfo.value)
        assert str(path) in msg
        assert "description" in msg

    def test_missing_name_raises_with_field_and_file(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.yaml"
        path.write_text(
            "description: 'Writes prose.'\nprompt_template: 'x'\n",
            encoding="utf-8",
        )

        with pytest.raises(PersonaRegistryError) as excinfo:
            PersonaRegistry.load(tmp_path)

        msg = str(excinfo.value)
        assert str(path) in msg
        assert "name" in msg

    def test_missing_prompt_template_raises_with_field_and_file(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "broken.yaml"
        path.write_text(
            "name: Writer\ndescription: 'Writes prose.'\n",
            encoding="utf-8",
        )

        with pytest.raises(PersonaRegistryError) as excinfo:
            PersonaRegistry.load(tmp_path)

        msg = str(excinfo.value)
        assert str(path) in msg
        assert "prompt_template" in msg

    def test_invalid_yaml_is_reported_with_file(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.yaml"
        path.write_text(
            # Intentional YAML error: unclosed flow mapping.
            "name: Writer\ndescription: {oops\n",
            encoding="utf-8",
        )

        with pytest.raises(PersonaRegistryError) as excinfo:
            PersonaRegistry.load(tmp_path)

        assert str(path) in str(excinfo.value)

    def test_md_file_without_frontmatter_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "body_only.md"
        path.write_text(
            "# No frontmatter here, just prose.\n",
            encoding="utf-8",
        )

        with pytest.raises(PersonaRegistryError) as excinfo:
            PersonaRegistry.load(tmp_path)

        assert str(path) in str(excinfo.value)


# ---------------------------------------------------------------------------
# Non-persona files are skipped (R3.1)
# ---------------------------------------------------------------------------


class TestSkippedFiles:
    def test_unsupported_extensions_are_skipped(self, tmp_path: Path) -> None:
        _write_yaml_persona(tmp_path, "writer.yaml", name="Writer")
        # These files must be silently ignored, not flagged as errors.
        (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
        (tmp_path / "README").write_text("also ignore", encoding="utf-8")
        (tmp_path / ".hidden").write_text("still ignored", encoding="utf-8")
        (tmp_path / "archive.zip").write_bytes(b"PK\x03\x04")

        registry = PersonaRegistry.load(tmp_path)

        assert [p.name for p in registry.all()] == ["Writer"]

    def test_subdirectories_are_skipped(self, tmp_path: Path) -> None:
        _write_yaml_persona(tmp_path, "writer.yaml", name="Writer")
        # A nested directory with a would-be persona file must not be
        # recursively loaded.
        nested = tmp_path / "nested"
        nested.mkdir()
        _write_yaml_persona(nested, "extra.yaml", name="Extra")

        registry = PersonaRegistry.load(tmp_path)

        assert [p.name for p in registry.all()] == ["Writer"]


# ---------------------------------------------------------------------------
# Orchestrator projection (R3.8)
# ---------------------------------------------------------------------------


class TestOrchestratorProjection:
    def test_describe_all_returns_persona_description_objects(
        self, tmp_path: Path
    ) -> None:
        _write_yaml_persona(
            tmp_path,
            "writer.yaml",
            name="Writer",
            description="Drafts prose.",
        )
        _write_yaml_persona(
            tmp_path,
            "reviewer.yaml",
            name="Reviewer",
            description="Reviews prose.",
        )

        registry = PersonaRegistry.load(tmp_path)
        descriptions = registry.describe_all_for_orchestrator()

        assert all(isinstance(d, PersonaDescription) for d in descriptions)
        assert [(d.name, d.description) for d in descriptions] == [
            ("Reviewer", "Reviews prose."),
            ("Writer", "Drafts prose."),
        ]

    def test_describe_all_is_empty_when_registry_is_empty(
        self, tmp_path: Path
    ) -> None:
        registry = PersonaRegistry.load(tmp_path)

        assert registry.describe_all_for_orchestrator() == []
