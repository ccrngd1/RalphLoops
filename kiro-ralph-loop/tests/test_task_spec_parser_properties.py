"""Property-based tests for ``ralph_loop.task_spec.parse_task_spec``
(design Property 11).

Property 11 has two arms:

Round trip:
    For any valid Task Spec frontmatter (with optional fields drawn
    freely), serializing to Markdown+YAML and running ``parse_task_spec``
    returns a :class:`TaskSpec` whose scalar fields equal the inputs.

Invalid rejection:
    For any frontmatter that is a valid base dict minus exactly one
    required field (``id``, ``title``, ``validation``), the parser
    raises :class:`TaskSpecParseError` identifying that field.

Validates: Requirements 7.1, 18.1, 18.2, 18.3, 18.4, 18.7.
"""

# Feature: ralph-loop, Property 11: Task spec parse round trip and invalid-rejection

from __future__ import annotations

import string
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ralph_loop.models import (
    FileExistsCheckConfig,
    PersonaReviewCheckConfig,
    ShellCheckConfig,
)
from ralph_loop.task_spec import TaskSpecParseError, parse_task_spec


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


# Short URL-safe identifier alphabet (matches the alphabet used by the
# rest of the suite in ``tests/strategies.py``). Kept local so this
# file is self-contained and the shrinker produces readable
# counterexamples in id-typed fields.
_ID_ALPHABET = string.ascii_letters + string.digits + "_-"

# Alphabet for description-like strings: plain ASCII without YAML
# special characters. The YAML emitter is asked to always quote strings
# (``default_style='"'`` when used below) but keeping the alphabet
# simple means even unquoted ids round-trip cleanly.
_PROSE_ALPHABET = string.ascii_letters + string.digits + " -_.,"


_id_strategy = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=8)
_title_strategy = st.text(alphabet=_PROSE_ALPHABET, min_size=1, max_size=20)
_tag_strategy = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=8)
_path_strategy = st.text(alphabet=_ID_ALPHABET + "/.", min_size=1, max_size=20)
_command_strategy = st.text(alphabet=_PROSE_ALPHABET, min_size=1, max_size=20)
_pass_condition_strategy = st.text(
    alphabet=_PROSE_ALPHABET, min_size=1, max_size=30
)
_persona_name_strategy = st.sampled_from(
    ["Writer", "Editor", "Reviewer", "Coder", "Tester"]
)


@st.composite
def _shell_check_strategy(draw) -> dict[str, Any]:
    return {
        "type": "shell",
        "commands": draw(
            st.lists(_command_strategy, min_size=1, max_size=3)
        ),
    }


@st.composite
def _persona_review_check_strategy(draw) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "persona_review",
        "persona": draw(_persona_name_strategy),
    }
    if draw(st.booleans()):
        entry["pass_condition"] = draw(_pass_condition_strategy)
    return entry


@st.composite
def _file_exists_check_strategy(draw) -> dict[str, Any]:
    return {
        "type": "file_exists",
        "paths": draw(st.lists(_path_strategy, min_size=1, max_size=3)),
    }


_check_strategy = st.one_of(
    _shell_check_strategy(),
    _persona_review_check_strategy(),
    _file_exists_check_strategy(),
)


@st.composite
def valid_frontmatter_strategy(draw) -> dict[str, Any]:
    """Generate a dict that is a valid :class:`TaskSpec` frontmatter.

    The dict carries the three required fields (``id``, ``title``,
    ``validation``) plus a random mix of the optional fields listed in
    R18.2. Check configs are drawn via the discriminated-union
    strategies above so every supported check type is exercised.
    """
    fm: dict[str, Any] = {
        "id": draw(_id_strategy),
        "title": draw(_title_strategy),
        "validation": draw(
            st.lists(_check_strategy, min_size=1, max_size=3)
        ),
    }
    if draw(st.booleans()):
        fm["target_persona"] = draw(_persona_name_strategy)
    if draw(st.booleans()):
        fm["tags"] = draw(st.lists(_tag_strategy, min_size=0, max_size=3))
    if draw(st.booleans()):
        fm["depends_on"] = draw(
            st.lists(_id_strategy, min_size=0, max_size=3)
        )
    if draw(st.booleans()):
        fm["context_files"] = draw(
            st.lists(_path_strategy, min_size=0, max_size=3)
        )
    return fm


def _write_spec_file(
    tmp_path: Path, frontmatter: dict[str, Any], body: str = ""
) -> Path:
    """Serialize ``frontmatter`` as YAML and write ``---``-delimited file."""
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=True)
    path = tmp_path / "spec.md"
    path.write_text(
        f"---\n{yaml_text}---\n{body}",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Round-trip property
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(frontmatter=valid_frontmatter_strategy())
def test_valid_frontmatter_round_trips(
    tmp_path: Path, frontmatter: dict[str, Any]
) -> None:
    """Validates: Requirements 7.1, 18.1, 18.2, 18.3, 18.4.

    For any dict that would validate as a :class:`TaskSpec`
    frontmatter, serializing it through ``yaml.safe_dump`` into a
    ``---``-delimited Markdown file and parsing it back with
    ``parse_task_spec`` yields a :class:`TaskSpec` whose scalar fields
    match the inputs and whose validation-check discriminators match
    the input ``type`` tags.
    """
    path = _write_spec_file(tmp_path, frontmatter)

    spec = parse_task_spec(path)

    # Required scalars survive the round trip (R18.1).
    assert spec.id == frontmatter["id"]
    assert spec.title == frontmatter["title"]

    # Optional scalars survive the round trip (R18.2). ``None`` vs
    # missing key is unobservable on the parsed side because absent
    # fields default to ``None``.
    assert spec.target_persona == frontmatter.get("target_persona")
    assert spec.tags == frontmatter.get("tags")
    assert spec.depends_on == frontmatter.get("depends_on")
    assert spec.context_files == frontmatter.get("context_files")

    # Validation-check discriminators dispatch to the right model
    # (R7.1, R18.4). We compare the check ``type`` tag against the
    # model class for each entry.
    type_to_model = {
        "shell": ShellCheckConfig,
        "persona_review": PersonaReviewCheckConfig,
        "file_exists": FileExistsCheckConfig,
    }
    assert len(spec.validation) == len(frontmatter["validation"])
    for parsed, original in zip(spec.validation, frontmatter["validation"]):
        assert isinstance(parsed, type_to_model[original["type"]])


# ---------------------------------------------------------------------------
# Invalid-rejection property
# ---------------------------------------------------------------------------


_REQUIRED_FIELDS = ("id", "title", "validation")


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    frontmatter=valid_frontmatter_strategy(),
    field_to_drop=st.sampled_from(_REQUIRED_FIELDS),
)
def test_missing_required_field_is_rejected(
    tmp_path: Path,
    frontmatter: dict[str, Any],
    field_to_drop: str,
) -> None:
    """Validates: Requirements 18.1, 18.7.

    For any otherwise-valid frontmatter minus exactly one required
    field, the parser raises :class:`TaskSpecParseError` with
    ``field`` naming the dropped field so the caller can mark the
    task stuck and log the invalid field identifier (R18.7).
    """
    mutated = {
        key: value
        for key, value in frontmatter.items()
        if key != field_to_drop
    }
    path = _write_spec_file(tmp_path, mutated)

    with pytest.raises(TaskSpecParseError) as excinfo:
        parse_task_spec(path)

    assert excinfo.value.field == field_to_drop

    # When ``id`` survives the drop, the parser should report it as
    # ``task_id`` for logging. When ``id`` is the dropped field the
    # task_id attribute is expected to be ``None``.
    if field_to_drop == "id":
        assert excinfo.value.task_id is None
    else:
        assert excinfo.value.task_id == frontmatter["id"]
