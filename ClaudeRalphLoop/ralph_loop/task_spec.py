"""Task Spec file parser (R18.1-R18.4, R18.7, R7.1).

Parses Markdown files with YAML frontmatter into :class:`TaskSpec` objects.
On validation error, raises :class:`TaskSpecParseError` carrying enough
information for the caller to "mark the task as stuck" and log an error
identifying the task identifier and the invalid field (R18.7, R7.1).

The parser is intentionally permissive about the *body* of the spec (the
prose sections that follow the frontmatter). Body sections are optional;
a missing section becomes an empty string on :class:`TaskSpecBody` so a
spec with only a frontmatter validates cleanly. Frontmatter fields, by
contrast, are strictly validated through Pydantic: any missing required
field (``id``, ``title``, ``validation``) or invalid discriminator in a
check entry produces a ``TaskSpecParseError`` with the offending field
path.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Union

import yaml
from pydantic import ValidationError

from ralph_loop.models import TaskSpec, TaskSpecBody


class TaskSpecParseError(Exception):
    """Raised when a task spec file cannot be parsed or is invalid.

    ``task_id`` is the identifier pulled from the frontmatter when
    available, so callers can mark the task stuck by id even when the
    parse failure prevented full model construction (R18.7).
    ``field`` is the ``loc`` path of the first Pydantic
    :class:`ValidationError` entry, so callers can log the offending
    field alongside the task identifier (R18.7).
    """

    def __init__(
        self,
        message: str,
        *,
        task_id: str | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.field = field


# A Task Spec file begins with a YAML frontmatter block delimited by
# ``---`` on its own line, followed by the Markdown body (R18.1). The
# regex is anchored at the start of the document; ``re.DOTALL`` lets the
# middle group span newlines so multi-line frontmatter matches in one
# shot. The trailing ``\n?`` tolerates files that omit the newline after
# the closing delimiter.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


# Canonical section names on :class:`TaskSpecBody` plus the tolerated
# spelling variations. Markdown section headers are matched
# case-insensitively with whitespace collapsed to ``_`` so both
# ``## Context References`` and ``## context_references`` land in the
# same bucket.
_BODY_SECTIONS: dict[str, str] = {
    "objective": "objective",
    "context_references": "context_references",
    "context_reference": "context_references",
    "instructions": "instructions",
    "notes": "notes",
}


def _parse_body(body_text: str) -> TaskSpecBody:
    """Split the Markdown body into the four :class:`TaskSpecBody` sections.

    Sections are identified by ``## <Name>`` headers (R18.3). Lines
    before the first recognized header are dropped, lines under a
    recognized header accumulate into that section, and lines under an
    unrecognized header are ignored. Missing sections default to the
    empty string (``notes`` defaults to ``None`` since the field is
    optional on :class:`TaskSpecBody`).
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            header = stripped[3:].strip().lower().replace(" ", "_")
            current = _BODY_SECTIONS.get(header)
            if current is not None:
                sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)

    return TaskSpecBody(
        objective="\n".join(sections.get("objective", [])).strip(),
        context_references="\n".join(
            sections.get("context_references", [])
        ).strip(),
        instructions="\n".join(sections.get("instructions", [])).strip(),
        notes=(
            "\n".join(sections["notes"]).strip()
            if "notes" in sections
            else None
        ),
    )


def parse_task_spec(path: Union[str, Path]) -> TaskSpec:
    """Parse a Task Spec file at ``path`` into a :class:`TaskSpec`.

    Raises :class:`TaskSpecParseError` on any failure: missing
    frontmatter delimiters, malformed YAML, non-mapping frontmatter,
    schema-validation failures on any of the required frontmatter fields
    (R18.1), or invalid validation-check discriminators (R18.7).

    Successful parses return a :class:`TaskSpec` with the frontmatter
    fields and the parsed :class:`TaskSpecBody` populated. Missing body
    sections become empty strings (R18.3 tolerates a sparse body).
    """
    path = Path(path)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        raise TaskSpecParseError(
            f"Failed to read task spec {path}: {e}"
        ) from e

    m = _FRONTMATTER_RE.match(content)
    if m is None:
        raise TaskSpecParseError(
            f"Task spec {path} is missing YAML frontmatter",
        )

    frontmatter_text = m.group(1)
    body_text = m.group(2)

    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        raise TaskSpecParseError(
            f"Invalid YAML frontmatter in {path}: {e}",
        ) from e

    if not isinstance(frontmatter, dict):
        raise TaskSpecParseError(
            f"Frontmatter in {path} must be a YAML mapping, got "
            f"{type(frontmatter).__name__}",
        )

    # Extract the task id eagerly for error reporting so callers can
    # mark the task stuck even when body/schema validation fails (R18.7).
    maybe_id_val = frontmatter.get("id")
    maybe_id = maybe_id_val if isinstance(maybe_id_val, str) else None

    body = _parse_body(body_text)

    try:
        spec = TaskSpec(**frontmatter, body=body)
    except ValidationError as e:
        errors = e.errors()
        first_err = errors[0] if errors else None
        field = (
            ".".join(str(part) for part in first_err["loc"])
            if first_err
            else None
        )
        msg = f"Invalid task spec {path}"
        if field:
            msg += f": field {field!r}"
        msg += f": {e}"
        raise TaskSpecParseError(
            msg,
            task_id=maybe_id,
            field=field,
        ) from e

    return spec
