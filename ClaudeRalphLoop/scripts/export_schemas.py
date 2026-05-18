"""Export JSON Schemas from the Ralph Loop Pydantic models (Task 25.3).

Emits one schema per file under ``schemas/`` so editors (VS Code
YAML / JSON plugins, PyCharm, etc.) can validate ``tasks.json``,
Task_Spec frontmatter, persona files, and ``ralph.config.json`` against
the authoritative Pydantic models. Regenerate after any model change:

    python scripts/export_schemas.py

This script is intentionally small and self-contained: it imports
``ralph_loop.models`` and writes pretty-printed JSON with :func:`json.dumps`
so there are no runtime dependencies beyond what the package already
requires.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from ralph_loop.models import (
    TASK_LIST_ADAPTER,
    Config,
    Persona,
    Task,
    TaskSpec,
)


# Mapping of output filename (under the target ``schemas/`` directory)
# to a zero-argument callable that returns the JSON Schema dict. The
# indirection lets us use both ``Model.model_json_schema()`` and
# ``TypeAdapter(...).json_schema()`` without special-casing at the
# write site.
SCHEMA_EXPORTS: dict[str, Any] = {
    "task.schema.json": Task.model_json_schema,
    "tasks.schema.json": TASK_LIST_ADAPTER.json_schema,
    "persona.schema.json": Persona.model_json_schema,
    "task-spec.schema.json": TaskSpec.model_json_schema,
    "config.schema.json": Config.model_json_schema,
}


def export_schemas(output_dir: Path) -> list[Path]:
    """Write every schema in :data:`SCHEMA_EXPORTS` to ``output_dir``.

    The directory is created if it does not already exist. Each schema
    is written as pretty-printed JSON (two-space indent) with a
    trailing newline so the files round-trip cleanly through version
    control. Returns the list of written paths in insertion order.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, schema_fn in SCHEMA_EXPORTS.items():
        schema = schema_fn()
        path = output_dir / filename
        path.write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export JSON Schemas from the Ralph Loop Pydantic models."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "schemas",
        help="Target directory for the generated .schema.json files "
        "(default: <repo>/schemas).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:]) if argv is None else argv)
    written = export_schemas(args.output_dir)
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
