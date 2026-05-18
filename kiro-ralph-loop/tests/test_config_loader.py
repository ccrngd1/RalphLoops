"""Unit tests for ``ConfigLoader`` (Task 7.1).

Covers:

- Defaults-only load (no config file, no overrides) when required
  paths exist under the project root.
- ``ralph.config.json`` values override model defaults (R15.1, R15.2).
- CLI overrides win over file values (R15.2).
- Malformed JSON raises ``ConfigLoadError`` (R15.1).
- Missing ``tasks_path`` / ``summary_path`` / ``personas_dir`` each
  raise ``ConfigLoadError`` with a descriptive message (R15.9).
- Absolute paths in the config are preserved (not re-rooted).

Property-based tests for merge precedence and required-file fail-fast
arrive in Tasks 7.2 and 7.3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralph_loop.config import ConfigLoadError, load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scaffold_required_paths(
    project_root: Path,
    *,
    tasks_name: str = "tasks.json",
    summary_name: str = "SUMMARY.md",
    personas_name: str = "personas",
) -> None:
    """Create the files and directory required by R15.9 under ``project_root``."""
    (project_root / tasks_name).write_text("[]", encoding="utf-8")
    (project_root / summary_name).write_text("# Project Brief\n", encoding="utf-8")
    (project_root / personas_name).mkdir(parents=True, exist_ok=True)


def _write_config_file(project_root: Path, data: dict) -> Path:
    path = project_root / "ralph.config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Defaults and overrides
# ---------------------------------------------------------------------------


def test_defaults_only_load_resolves_paths_relative_to_project_root(
    tmp_path: Path,
) -> None:
    """With no config file and no CLI overrides, the loader fills in defaults
    and resolves the default relative paths against the project root."""
    _scaffold_required_paths(tmp_path)

    cfg = load_config(
        project_root=tmp_path,
        cli_overrides={"fallback_persona": "Writer"},
    )

    assert cfg.fallback_persona == "Writer"
    # Paths are resolved to absolute paths under the project root.
    assert Path(cfg.tasks_path) == (tmp_path / "tasks.json").resolve()
    assert Path(cfg.summary_path) == (tmp_path / "SUMMARY.md").resolve()
    assert Path(cfg.personas_dir) == (tmp_path / "personas").resolve()
    # Remaining defaults are preserved.
    assert cfg.max_iterations == 50
    assert cfg.escalation_threshold == 3
    assert cfg.git_integration_enabled is True


def test_config_file_overrides_defaults(tmp_path: Path) -> None:
    _scaffold_required_paths(tmp_path)
    _write_config_file(
        tmp_path,
        {
            "fallback_persona": "OverrideWriter",
            "max_iterations": 100,
        },
    )

    cfg = load_config(project_root=tmp_path)

    assert cfg.fallback_persona == "OverrideWriter"
    assert cfg.max_iterations == 100
    # Untouched fields fall back to model defaults.
    assert cfg.escalation_threshold == 3


def test_cli_overrides_win_over_config_file(tmp_path: Path) -> None:
    _scaffold_required_paths(tmp_path)
    _write_config_file(
        tmp_path,
        {
            "fallback_persona": "FileWriter",
            "max_iterations": 100,
        },
    )

    cfg = load_config(
        project_root=tmp_path,
        cli_overrides={"max_iterations": 25},
    )

    # CLI wins for max_iterations.
    assert cfg.max_iterations == 25
    # File value for fallback_persona is preserved (no CLI override).
    assert cfg.fallback_persona == "FileWriter"


def test_cli_override_none_values_do_not_clobber_file_values(
    tmp_path: Path,
) -> None:
    """CLI callers pass ``None`` for flags the user did not supply; those
    entries must not overwrite file values."""
    _scaffold_required_paths(tmp_path)
    _write_config_file(
        tmp_path,
        {"fallback_persona": "FileWriter", "max_iterations": 77},
    )

    cfg = load_config(
        project_root=tmp_path,
        cli_overrides={"max_iterations": None, "fallback_persona": None},
    )

    assert cfg.fallback_persona == "FileWriter"
    assert cfg.max_iterations == 77


# ---------------------------------------------------------------------------
# Error paths (R15.1, R15.9)
# ---------------------------------------------------------------------------


def test_malformed_config_json_raises_config_load_error(tmp_path: Path) -> None:
    _scaffold_required_paths(tmp_path)
    (tmp_path / "ralph.config.json").write_text(
        "{ not valid json", encoding="utf-8"
    )

    with pytest.raises(ConfigLoadError) as excinfo:
        load_config(project_root=tmp_path)

    assert "ralph.config.json" in str(excinfo.value)


def test_config_file_as_json_array_raises_config_load_error(tmp_path: Path) -> None:
    _scaffold_required_paths(tmp_path)
    (tmp_path / "ralph.config.json").write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(ConfigLoadError) as excinfo:
        load_config(project_root=tmp_path)

    assert "JSON object" in str(excinfo.value)


def test_missing_tasks_path_raises_config_load_error(tmp_path: Path) -> None:
    _scaffold_required_paths(tmp_path)
    (tmp_path / "tasks.json").unlink()

    with pytest.raises(ConfigLoadError) as excinfo:
        load_config(
            project_root=tmp_path,
            cli_overrides={"fallback_persona": "Writer"},
        )

    message = str(excinfo.value)
    assert "tasks_path" in message
    assert "tasks.json" in message


def test_missing_summary_path_raises_config_load_error(tmp_path: Path) -> None:
    _scaffold_required_paths(tmp_path)
    (tmp_path / "SUMMARY.md").unlink()

    with pytest.raises(ConfigLoadError) as excinfo:
        load_config(
            project_root=tmp_path,
            cli_overrides={"fallback_persona": "Writer"},
        )

    message = str(excinfo.value)
    assert "summary_path" in message
    assert "SUMMARY.md" in message


def test_missing_personas_dir_raises_config_load_error(tmp_path: Path) -> None:
    _scaffold_required_paths(tmp_path)
    (tmp_path / "personas").rmdir()

    with pytest.raises(ConfigLoadError) as excinfo:
        load_config(
            project_root=tmp_path,
            cli_overrides={"fallback_persona": "Writer"},
        )

    message = str(excinfo.value)
    assert "personas_dir" in message
    assert "personas" in message


def test_missing_required_paths_are_all_reported_together(tmp_path: Path) -> None:
    """All three missing paths are surfaced in a single error so the user
    sees the full scope at once."""
    # Don't scaffold anything: every required path is missing.
    with pytest.raises(ConfigLoadError) as excinfo:
        load_config(
            project_root=tmp_path,
            cli_overrides={"fallback_persona": "Writer"},
        )

    message = str(excinfo.value)
    assert "tasks_path" in message
    assert "summary_path" in message
    assert "personas_dir" in message


def test_invalid_config_values_raise_config_load_error(tmp_path: Path) -> None:
    _scaffold_required_paths(tmp_path)
    _write_config_file(
        tmp_path,
        {"fallback_persona": "Writer", "max_iterations": 0},
    )

    with pytest.raises(ConfigLoadError) as excinfo:
        load_config(project_root=tmp_path)

    assert "Invalid config" in str(excinfo.value)


def test_missing_fallback_persona_raises_config_load_error(tmp_path: Path) -> None:
    """``fallback_persona`` is the only required field on ``Config``; when the
    user supplies nothing, the loader surfaces a validation error."""
    _scaffold_required_paths(tmp_path)

    with pytest.raises(ConfigLoadError) as excinfo:
        load_config(project_root=tmp_path)

    assert "fallback_persona" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_absolute_paths_in_config_are_not_re_rooted(tmp_path: Path) -> None:
    """R15.1/R15.2: when a config supplies absolute paths, the loader must
    treat those paths as authoritative and not re-root them under the
    project directory."""
    # Build a separate elsewhere/ tree that hosts the required files so
    # the loader resolves correctly.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _scaffold_required_paths(
        elsewhere,
        tasks_name="tasks.json",
        summary_name="SUMMARY.md",
        personas_name="personas",
    )

    project_root = tmp_path / "project"
    project_root.mkdir()

    abs_tasks = (elsewhere / "tasks.json").resolve()
    abs_summary = (elsewhere / "SUMMARY.md").resolve()
    abs_personas = (elsewhere / "personas").resolve()

    _write_config_file(
        project_root,
        {
            "fallback_persona": "Writer",
            "tasks_path": str(abs_tasks),
            "summary_path": str(abs_summary),
            "personas_dir": str(abs_personas),
        },
    )

    cfg = load_config(project_root=project_root)

    assert Path(cfg.tasks_path) == abs_tasks
    assert Path(cfg.summary_path) == abs_summary
    assert Path(cfg.personas_dir) == abs_personas


def test_explicit_config_path_overrides_default_location(tmp_path: Path) -> None:
    _scaffold_required_paths(tmp_path)
    explicit = tmp_path / "alt" / "custom.json"
    explicit.parent.mkdir()
    explicit.write_text(
        json.dumps({"fallback_persona": "Explicit", "max_iterations": 7}),
        encoding="utf-8",
    )

    cfg = load_config(project_root=tmp_path, config_path=explicit)

    assert cfg.fallback_persona == "Explicit"
    assert cfg.max_iterations == 7
