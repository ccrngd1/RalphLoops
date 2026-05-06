"""Configuration loading and merging (R15.1-R15.9).

ConfigLoader reads ``ralph.config.json``, merges file values over defaults
and CLI overrides on top. Resolves all paths to absolute paths relative
to the project root. Fail-fast when required files/directories are
missing at startup (R15.9).

The only REQUIRED filesystem entries are ``tasks_path`` (a file),
``summary_path`` (a file), and ``personas_dir`` (a directory). These are
bootstrap outputs of ``ralph init``; the CLI ``init`` subcommand creates
them, and ``ralph run`` fail-fasts if they're missing.

Note: ``ralph init`` bypasses ConfigLoader (it's creating the scaffold).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from ralph_loop.models import Config


class ConfigLoadError(Exception):
    """Raised when config cannot be loaded or when required files are missing."""


def load_config(
    *,
    project_root: Path,
    config_path: Optional[Path] = None,
    cli_overrides: Optional[dict[str, Any]] = None,
) -> Config:
    """Load, merge, and validate config.

    Merge order (later wins) per R15.2:

    1. Defaults (from the ``Config`` Pydantic model, R15.3-R15.7).
    2. ``ralph.config.json`` (when the file exists, R15.1).
    3. CLI overrides (when provided, R15.2).

    Paths in the resulting ``Config`` are resolved to absolute paths
    relative to ``project_root``.

    Required-file checks (R15.9):

    - ``tasks_path`` file must exist
    - ``summary_path`` file must exist
    - ``personas_dir`` directory must exist

    If any are missing, raise ``ConfigLoadError``.

    Args:
        project_root: The project root directory. Paths in config
            are resolved relative to this.
        config_path: Explicit config file path. Defaults to
            ``project_root / "ralph.config.json"``.
        cli_overrides: Dict of overrides from the CLI. These take
            highest precedence. ``None`` values are skipped so callers
            can pass unset CLI flags without clobbering file values.

    Returns:
        A validated ``Config`` instance with absolute paths.

    Raises:
        ConfigLoadError: On JSON parse error, Pydantic validation
            error, or missing required files.
    """
    resolved_config_path = config_path or (project_root / "ralph.config.json")

    # Step 1: read file values (if any) -- defaults come from the Config model itself.
    file_data: dict[str, Any] = {}
    if resolved_config_path.exists():
        try:
            raw = resolved_config_path.read_text(encoding="utf-8")
            file_data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ConfigLoadError(
                f"Failed to parse config file {resolved_config_path}: {e}"
            ) from e
        if not isinstance(file_data, dict):
            raise ConfigLoadError(
                f"Config file {resolved_config_path} must contain a JSON object, "
                f"got {type(file_data).__name__}"
            )

    # Step 2: merge file_data and cli_overrides. CLI wins. Skip ``None``
    # entries in cli_overrides so callers can pass unset flags through.
    merged: dict[str, Any] = dict(file_data)
    if cli_overrides:
        for k, v in cli_overrides.items():
            if v is not None:
                merged[k] = v

    # Step 3: validate through Config. Defaults fill in fields absent from
    # the merged dict.
    try:
        config = Config(**merged)
    except ValidationError as e:
        raise ConfigLoadError(f"Invalid config: {e}") from e

    # Step 4: resolve paths to absolute paths.
    config = _resolve_paths(config, project_root)

    # Step 5: required-file checks (R15.9).
    _check_required_files(config)

    return config


def _resolve_paths(config: Config, project_root: Path) -> Config:
    """Return a ``Config`` copy with path fields resolved to absolute paths.

    Absolute paths in the input are passed through unchanged (no
    ``project_root`` prefix is applied). Relative paths are resolved
    against ``project_root`` and normalized via ``Path.resolve()``.
    """

    def resolve(p: str) -> str:
        path = Path(p)
        if path.is_absolute():
            return str(path)
        return str((project_root / path).resolve())

    return config.model_copy(
        update={
            "tasks_path": resolve(config.tasks_path),
            "summary_path": resolve(config.summary_path),
            "personas_dir": resolve(config.personas_dir),
            "specs_dir": resolve(config.specs_dir),
            "pending_tasks_path": resolve(config.pending_tasks_path),
            "log_dir": resolve(config.log_dir),
        }
    )


def _check_required_files(config: Config) -> None:
    """Raise ``ConfigLoadError`` if required files/directories are missing (R15.9)."""
    missing: list[tuple[str, str]] = []
    if not Path(config.tasks_path).is_file():
        missing.append(("tasks_path", config.tasks_path))
    if not Path(config.summary_path).is_file():
        missing.append(("summary_path", config.summary_path))
    if not Path(config.personas_dir).is_dir():
        missing.append(("personas_dir", config.personas_dir))

    if missing:
        parts = "\n".join(f"  - {name}: {path}" for name, path in missing)
        raise ConfigLoadError(
            f"Required configuration paths are missing:\n{parts}"
        )
