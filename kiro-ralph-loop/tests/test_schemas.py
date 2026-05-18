"""Unit tests for the exported JSON Schemas (Task 25.3).

Verifies that ``scripts/export_schemas.py`` produces five JSON Schema
files (one per top-level Ralph Loop model) and that each file parses as
JSON and has a top-level shape consistent with a JSON Schema object.

Requirements exercised: 2.1, 2.2, 3.2, 15.3, 18.1.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ralph_loop.models import (
    TASK_LIST_ADAPTER,
    Config,
    Persona,
    Task,
    TaskSpec,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "export_schemas.py"
_COMMITTED_SCHEMAS_DIR = _REPO_ROOT / "schemas"


# Canonical list of schema filenames and the Pydantic callable that
# should produce them. The mapping mirrors ``SCHEMA_EXPORTS`` in
# ``scripts/export_schemas.py`` but is redefined here so the tests can
# detect accidental drift between the two.
_EXPECTED_SCHEMAS: dict[str, object] = {
    "task.schema.json": Task.model_json_schema,
    "tasks.schema.json": TASK_LIST_ADAPTER.json_schema,
    "persona.schema.json": Persona.model_json_schema,
    "task-spec.schema.json": TaskSpec.model_json_schema,
    "config.schema.json": Config.model_json_schema,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_export_module():
    """Import ``scripts/export_schemas.py`` without needing a package init."""
    spec = importlib.util.spec_from_file_location(
        "ralph_loop_export_schemas_under_test", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _looks_like_json_schema(schema: dict) -> bool:
    """Return True iff ``schema`` has one of the two top-level shapes
    we expect from a Pydantic JSON Schema export.

    Object-typed models emit ``{"type": "object", "properties": {...}}``.
    The tasks list schema emits ``{"type": "array", "items": {...}}``.
    We accept either a ``type`` or a ``properties`` key so the check is
    robust to future Pydantic changes that move the discriminator.
    """
    return ("type" in schema) or ("properties" in schema)


# ---------------------------------------------------------------------------
# Script-level tests (subprocess + temp output directory)
# ---------------------------------------------------------------------------


def test_export_script_writes_every_schema(tmp_path: Path) -> None:
    """Run the exporter against a temp dir and verify every schema file
    is created and parses as JSON.

    Using a temp directory guarantees we are testing the fresh output of
    the script, not the copy committed at ``schemas/``.
    """
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--output-dir", str(tmp_path)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"export_schemas.py exited {result.returncode}; "
        f"stderr={result.stderr!r}"
    )

    for filename in _EXPECTED_SCHEMAS:
        path = tmp_path / filename
        assert path.exists(), f"Expected {path} to be written"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert _looks_like_json_schema(data), (
            f"{filename} does not look like a JSON Schema: keys={list(data)}"
        )


# ---------------------------------------------------------------------------
# Function-level tests (import the module directly)
# ---------------------------------------------------------------------------


def test_export_function_writes_every_schema(tmp_path: Path) -> None:
    """Directly invoke :func:`export_schemas.export_schemas` and check
    the returned path list matches the expected filenames."""
    module = _load_export_module()
    written = module.export_schemas(tmp_path)
    written_names = [p.name for p in written]
    assert set(written_names) == set(_EXPECTED_SCHEMAS)
    for path in written:
        assert path.exists()
        json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("filename,schema_fn", list(_EXPECTED_SCHEMAS.items()))
def test_exported_schema_matches_pydantic(
    tmp_path: Path, filename: str, schema_fn
) -> None:
    """The script's output for each schema must match a fresh call to
    the underlying Pydantic schema function."""
    module = _load_export_module()
    module.export_schemas(tmp_path)

    on_disk = json.loads((tmp_path / filename).read_text(encoding="utf-8"))
    from_model = schema_fn()
    assert on_disk == from_model


# ---------------------------------------------------------------------------
# Committed-artifact tests
# ---------------------------------------------------------------------------


def test_committed_schemas_directory_is_present() -> None:
    """The repo must ship a ``schemas/`` directory with every expected
    file, so editors and downstream tooling can pick them up without
    re-running the exporter (R15.3)."""
    assert _COMMITTED_SCHEMAS_DIR.is_dir()
    for filename in _EXPECTED_SCHEMAS:
        path = _COMMITTED_SCHEMAS_DIR / filename
        assert path.exists(), f"Missing committed schema file: {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert _looks_like_json_schema(data)
