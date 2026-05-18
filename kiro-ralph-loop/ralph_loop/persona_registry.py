"""Persona registry loader and accessors (R3.1-R3.8).

The Persona Registry is the configured collection of available Personas
for a project. On Ralph Loop startup we walk the configured personas
directory, parse every YAML or Markdown-with-YAML-frontmatter file into
a :class:`~ralph_loop.models.Persona` via Pydantic validation, and build
an in-memory index keyed by persona name.

The loader is fail-fast: duplicate names and missing required fields
trigger :class:`PersonaRegistryError` with a descriptive message that
identifies the offending file so the outer CLI can exit non-zero (R3.5,
R3.6).

The registry exposes:

- :meth:`PersonaRegistry.get` - lookup by name (used by the Orchestrator
  when a task declares an explicit ``target_persona`` per R4.1).
- :meth:`PersonaRegistry.all` - all personas in deterministic name-sorted
  order (used by logging and tooling).
- :meth:`PersonaRegistry.describe_all_for_orchestrator` - name +
  description projections passed into the LLM-based persona-selection
  prompt (R3.8, R4.2).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import ValidationError

from ralph_loop.models import Persona, PersonaDescription


class PersonaRegistryError(Exception):
    """Raised when the persona registry fails to load.

    The Ralph Loop treats this as a fail-fast startup error (R3.5, R3.6):
    the CLI exits non-zero and logs the message, which always identifies
    the offending file and (when known) the offending field.
    """


# Frontmatter delimiter regex: matches a leading ``---\n<yaml>\n---\n<body>``
# block, accepting optional trailing whitespace on the delimiter lines and
# an optional trailing newline after the closing delimiter. The body is
# captured but unused by the Persona model.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _parse_persona_file(path: Path) -> dict:
    """Parse a persona file into the dict consumed by ``Persona(**data)``.

    Supports two formats (R3.2):

    1. Pure YAML when the file extension is ``.yaml`` or ``.yml``. The
       entire file is parsed with :func:`yaml.safe_load` and must be a
       YAML mapping at the top level.
    2. Markdown with YAML frontmatter for ``.md`` files. The frontmatter
       is delimited by ``---`` lines; only the frontmatter is used to
       construct the Persona, the body below the second ``---`` is
       ignored by the model but allowed for human-authored prose.

    Raises :class:`PersonaRegistryError` on read failure, YAML parse
    failure, missing frontmatter, or a non-mapping top-level value.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        raise PersonaRegistryError(
            f"Failed to read persona file {path}: {e}"
        ) from e

    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            raise PersonaRegistryError(
                f"Invalid YAML in persona file {path}: {e}"
            ) from e
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise PersonaRegistryError(
                f"Persona file {path} must contain a YAML mapping, "
                f"got {type(data).__name__}"
            )
        return data

    # Markdown with YAML frontmatter.
    m = _FRONTMATTER_RE.match(content)
    if m is None:
        raise PersonaRegistryError(
            f"Persona file {path} is not YAML and has no YAML frontmatter"
        )
    frontmatter_text = m.group(1)
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        raise PersonaRegistryError(
            f"Invalid YAML frontmatter in persona file {path}: {e}"
        ) from e
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise PersonaRegistryError(
            f"Persona file {path} frontmatter must be a YAML mapping, "
            f"got {type(data).__name__}"
        )
    return data


class PersonaRegistry:
    """In-memory index of personas loaded from a directory (R3.1, R3.4).

    Instances are constructed via :meth:`load`; the constructor takes an
    already-validated mapping so tests can inject a fixed registry.
    """

    def __init__(self, personas: dict[str, Persona]) -> None:
        self._personas: dict[str, Persona] = dict(personas)

    @classmethod
    def load(cls, directory: Path) -> "PersonaRegistry":
        """Load every persona file from ``directory`` (R3.1, R3.4).

        Supported extensions: ``.yaml``, ``.yml``, ``.md``. Files with
        other extensions (including dotfiles and editor temp files) are
        skipped silently so users can keep auxiliary files alongside
        their persona definitions without tripping the loader.

        Raises :class:`PersonaRegistryError` on:

        - A missing or non-directory personas path.
        - A read or YAML parse error on any persona file.
        - A :class:`pydantic.ValidationError` (R3.6) - the offending
          file and the list of invalid fields are embedded in the
          message.
        - A duplicate persona name across files (R3.5) - the message
          identifies the second file that declared the duplicate name.
        """
        if not directory.exists() or not directory.is_dir():
            raise PersonaRegistryError(
                f"Personas directory does not exist: {directory}"
            )

        personas: dict[str, Persona] = {}
        # Sorted iteration makes load order deterministic, which keeps
        # error messages (and downstream logs) stable run to run.
        files = sorted(directory.iterdir())
        for file in files:
            if not file.is_file():
                continue
            if file.suffix.lower() not in (".yaml", ".yml", ".md"):
                continue

            data = _parse_persona_file(file)
            try:
                persona = Persona(**data)
            except ValidationError as e:
                field_msgs = [
                    f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                    for err in e.errors()
                ]
                raise PersonaRegistryError(
                    f"Invalid persona definition in {file}:\n  "
                    + "\n  ".join(field_msgs)
                ) from e

            if persona.name in personas:
                raise PersonaRegistryError(
                    f"Duplicate persona name {persona.name!r}: "
                    f"also defined in {file}"
                )
            personas[persona.name] = persona

        return cls(personas)

    def get(self, name: str) -> Optional[Persona]:
        """Return the persona with the given name, or ``None`` if absent.

        Used by the Orchestrator when a task declares an explicit
        ``target_persona`` (R4.1, R4.9). A ``None`` return signals the
        caller to mark the task stuck.
        """
        return self._personas.get(name)

    def all(self) -> list[Persona]:
        """Return every persona in deterministic (name-sorted) order."""
        return [self._personas[name] for name in sorted(self._personas)]

    def describe_all_for_orchestrator(self) -> list[PersonaDescription]:
        """Return ``PersonaDescription`` projections for every persona (R3.8).

        The Orchestrator's LLM persona-selection prompt (R4.2) only
        needs each persona's name and description, so this is the
        reduced view it receives. Order matches :meth:`all` so the LLM
        sees a stable, deterministic list across calls.
        """
        return [
            PersonaDescription(name=p.name, description=p.description)
            for p in self.all()
        ]
