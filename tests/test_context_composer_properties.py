"""Property-based tests for ``ralph_loop.context`` (design Properties 9,
10, 12).

- Property 9 (Context window content inclusion): for any task / spec /
  persona / brief, the composed text includes the loop framing, the
  spec body, the rendered persona prompt, the persona instructions
  (when set), the escalation context (when present), and the resumed
  notice (when requested).

  Validates: Requirements 5.3, 6.2, 6.3, 6.4, 6.5, 6.6, 14.5.

- Property 10 (Context window truncation): for any combination that
  exceeds ``max_tokens``, the returned :class:`ContextWindow` has
  ``truncated=True``, the spec body / rendered persona prompt /
  persona instructions still appear in the output, and the project
  brief has been replaced by a summary.

  Validates: Requirements 6.7.

- Property 12 (Missing context-file warnings): for any mix of present
  and missing context files, ``inline_context_files`` records every
  missing path in its return value, logs a warning for each, never
  raises, and includes the contents of every present file in its
  output. ``compose_context`` likewise never aborts when a referenced
  context file is missing.

  Validates: Requirements 18.5, 18.6.
"""

# Feature: ralph-loop, Property 9 & 10 & 12: Context window content inclusion, truncation, and missing-reference warnings

from __future__ import annotations

import logging
import string
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ralph_loop.context import (
    LOOP_FRAMING,
    RESUMED_NOTICE,
    compose_context,
    inline_context_files,
)
from ralph_loop.models import (
    Persona,
    ShellCheckConfig,
    Task,
    TaskSpec,
    TaskSpecBody,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


# Marker tokens are short, distinctive ASCII strings drawn from a
# restricted alphabet so they survive Markdown/YAML assembly unchanged
# and so counterexamples print cleanly. The property tests assert that
# each marker appears in the composed output; restricting the alphabet
# (no ``{`` / ``}`` / ``#`` / ``\n``) guarantees the template renderer
# won't accidentally rewrite a marker into another placeholder.
_MARKER_ALPHABET = string.ascii_letters + string.digits + "_-"

_marker_strategy = st.text(
    alphabet=_MARKER_ALPHABET, min_size=4, max_size=12
)
"""Short, distinctive token that the test can look for in the composed
output. ``min_size=4`` keeps the marker long enough to be unlikely to
collide with framing prose, and ``max_size=12`` keeps counterexamples
readable."""


# Short alphabet used for the ``id`` / ``title`` / persona name fields.
# Same as the id alphabets elsewhere in the suite.
_id_strategy = st.text(alphabet=_MARKER_ALPHABET, min_size=1, max_size=8)


@st.composite
def _task_strategy(draw) -> Task:
    tid = draw(_id_strategy)
    return Task(
        id=tid,
        title=draw(_id_strategy),
        priority=draw(st.integers(min_value=-10, max_value=100)),
        status="pending",
        spec_path=f"specs/{tid}.md",
    )


@st.composite
def _spec_with_markers_strategy(draw) -> tuple[TaskSpec, dict[str, str]]:
    """Generate a spec whose body carries unique markers per section.

    The test uses the returned marker dict to assert that each section
    survives composition. The strategy draws one marker per field plus
    an optional notes marker, so both the with-notes and without-notes
    branches are exercised.
    """
    markers = {
        "objective": draw(_marker_strategy),
        "context_references": draw(_marker_strategy),
        "instructions": draw(_marker_strategy),
    }
    include_notes = draw(st.booleans())
    if include_notes:
        markers["notes"] = draw(_marker_strategy)
    tid = draw(_id_strategy)
    spec = TaskSpec(
        id=tid,
        title=draw(_id_strategy),
        validation=[ShellCheckConfig(type="shell", commands=["true"])],
        body=TaskSpecBody(
            objective=markers["objective"],
            context_references=markers["context_references"],
            instructions=markers["instructions"],
            notes=markers.get("notes"),
        ),
    )
    return spec, markers


@st.composite
def _persona_with_markers_strategy(draw) -> tuple[Persona, dict[str, str]]:
    """Generate a persona whose fields carry unique markers.

    ``prompt_template`` always contains ``{{persona_name}}`` so the
    test can verify the placeholder was rendered (R6.4). The
    instructions field is either a distinctive marker or ``None``.
    """
    markers = {
        "prompt_marker": draw(_marker_strategy),
        "name": draw(_id_strategy),
    }
    include_instructions = draw(st.booleans())
    if include_instructions:
        markers["instructions"] = draw(_marker_strategy)
    persona = Persona(
        name=markers["name"],
        description="description",
        prompt_template=(
            f"{markers['prompt_marker']} "
            "{{persona_name}} {{task_id}} {{task_title}}"
        ),
        instructions=markers.get("instructions"),
    )
    return persona, markers


# ---------------------------------------------------------------------------
# Property 9: Content inclusion
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    task=_task_strategy(),
    spec_and_markers=_spec_with_markers_strategy(),
    persona_and_markers=_persona_with_markers_strategy(),
    brief_marker=_marker_strategy,
    include_escalation=st.booleans(),
    escalation_marker=_marker_strategy,
    resumed_notice=st.booleans(),
)
def test_composed_window_contains_every_section(
    task: Task,
    spec_and_markers: tuple[TaskSpec, dict[str, str]],
    persona_and_markers: tuple[Persona, dict[str, str]],
    brief_marker: str,
    include_escalation: bool,
    escalation_marker: str,
    resumed_notice: bool,
) -> None:
    """Validates: Requirements 5.3, 6.2, 6.3, 6.4, 6.5, 6.6, 14.5.

    For every generated (task, spec, persona, brief, escalation?,
    resumed?) tuple, the composed text contains every input marker it
    ought to. A large token cap (1_000_000) keeps the composer on the
    untruncated branch so the property is really about *inclusion*
    rather than truncation fallback.
    """
    spec, spec_markers = spec_and_markers
    persona, persona_markers = persona_and_markers
    escalation_context = escalation_marker if include_escalation else None

    window = compose_context(
        task=task,
        spec=spec,
        persona=persona,
        brief=brief_marker,
        escalation_context=escalation_context,
        resumed_notice=resumed_notice,
        max_tokens=1_000_000,
    )

    # R6.6: loop framing present.
    assert "Ralph Loop Iteration" in window.text
    # R6.2: project brief present.
    assert brief_marker in window.text
    # R6.3: every spec body section with a marker survives.
    for value in spec_markers.values():
        assert value in window.text
    # R6.4: persona prompt template rendered with placeholders. The
    # ``prompt_marker`` appears verbatim and ``{{persona_name}}`` was
    # substituted for the persona's name.
    assert persona_markers["prompt_marker"] in window.text
    assert persona_markers["name"] in window.text
    # R6.5: persona instructions appear when declared.
    if "instructions" in persona_markers:
        assert persona_markers["instructions"] in window.text
    # R5.3: escalation context appears when supplied.
    if include_escalation:
        assert escalation_marker in window.text
    # R14.5: resumed notice appears when requested.
    if resumed_notice:
        assert "Resumed from interruption" in window.text
    else:
        assert "Resumed from interruption" not in window.text


# ---------------------------------------------------------------------------
# Property 10: Truncation preserves spec, persona, and instructions
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    task=_task_strategy(),
    spec_and_markers=_spec_with_markers_strategy(),
    persona_and_markers=_persona_with_markers_strategy(),
    brief_padding=st.integers(min_value=2_000, max_value=10_000),
)
def test_truncation_preserves_spec_and_persona(
    task: Task,
    spec_and_markers: tuple[TaskSpec, dict[str, str]],
    persona_and_markers: tuple[Persona, dict[str, str]],
    brief_padding: int,
) -> None:
    """Validates: Requirements 6.7.

    When the composed text exceeds ``max_tokens``, the composer must
    return ``truncated=True`` and preserve the full task spec, the
    rendered persona prompt, and the persona instructions while
    truncating the project brief to its summary.

    The strategy forces overflow by building a long brief (``X`` x
    ``brief_padding``) and setting a tiny ``max_tokens``. The brief
    marker is placed at the *end* of the padded brief so the
    truncation-survival check is meaningful: under R6.7 truncation the
    marker at the end is dropped.
    """
    spec, spec_markers = spec_and_markers
    persona, persona_markers = persona_and_markers
    brief_tail_marker = "BRIEF_TAIL_MARKER_XYZ"
    brief = "X" * brief_padding + brief_tail_marker

    window = compose_context(
        task=task,
        spec=spec,
        persona=persona,
        brief=brief,
        max_tokens=50,
    )

    assert window.truncated is True
    # R6.7: spec body survives.
    for value in spec_markers.values():
        assert value in window.text
    # R6.7: rendered persona prompt survives.
    assert persona_markers["prompt_marker"] in window.text
    assert persona_markers["name"] in window.text
    # R6.7: persona instructions survive when present.
    if "instructions" in persona_markers:
        assert persona_markers["instructions"] in window.text
    # R6.7: the brief is replaced by a summary; the tail marker at the
    # end of the brief no longer appears verbatim because the
    # truncation keeps only the head of the brief.
    assert brief_tail_marker not in window.text
    # An explicit truncation notice is inlined.
    assert "[... project brief truncated ...]" in window.text


# ---------------------------------------------------------------------------
# Property 12: Missing context-file warnings
# ---------------------------------------------------------------------------


# Path alphabet: limit to filename-safe ASCII so tests materialize
# files cleanly across platforms.
_PATH_ALPHABET = string.ascii_letters + string.digits + "_-"

_filename_strategy = st.text(
    alphabet=_PATH_ALPHABET, min_size=1, max_size=12
).map(lambda s: f"{s}.md")


@st.composite
def _context_files_layout_strategy(
    draw,
) -> tuple[list[str], set[str], dict[str, str]]:
    """Generate a (paths, present_set, contents_by_filename) triple.

    ``paths`` is the input to :func:`inline_context_files` in request
    order (duplicates are possible because real specs could repeat a
    reference). ``present_set`` lists the filenames the test will
    actually create on disk before calling the function. The caller
    materializes files by looking up ``contents_by_filename[name]``.
    """
    filenames = draw(
        st.lists(_filename_strategy, min_size=0, max_size=6, unique=True)
    )
    # Decide presence per filename so both the all-present and
    # all-missing edge cases are reachable.
    present = {
        name for name in filenames if draw(st.booleans())
    }
    # Generate distinctive contents per filename. The content marker is
    # prefixed so the test can assert presence in the inlined output
    # without worrying about other strings in the composed window.
    contents = {
        name: f"CTX_MARKER_{name}_{draw(_marker_strategy)}"
        for name in filenames
    }
    # Allow duplicate entries by optionally repeating filenames.
    paths = list(filenames)
    if filenames and draw(st.booleans()):
        repeat = draw(st.sampled_from(filenames))
        paths.append(repeat)
    return paths, present, contents


def _fresh_subdir(tmp_path: Path) -> Path:
    """Return a never-before-used subdirectory under ``tmp_path``.

    ``tmp_path`` is function-scoped but Hypothesis re-runs the test
    function body many times per outer pytest invocation. Without a
    fresh directory per example, files created by earlier examples
    leak into later ones and break presence-vs-missing assumptions.
    Counter-based naming keeps the scheme deterministic so shrunk
    counterexamples are reproducible.
    """
    idx = 0
    while True:
        candidate = tmp_path / f"ex_{idx:05d}"
        if not candidate.exists():
            candidate.mkdir()
            return candidate
        idx += 1


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(layout=_context_files_layout_strategy())
def test_inline_context_files_warns_on_missing_and_never_aborts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    layout: tuple[list[str], set[str], dict[str, str]],
) -> None:
    """Validates: Requirements 18.5, 18.6.

    For any mix of present and missing filenames,
    :func:`inline_context_files` records each *missing* path in the
    returned ``missing`` list, logs a warning for each, never raises,
    and inlines the contents of every present file.
    """
    paths, present, contents = layout
    base_dir = _fresh_subdir(tmp_path)
    for name in present:
        (base_dir / name).write_text(contents[name], encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="ralph_loop.context"):
        caplog.clear()
        text, missing = inline_context_files(paths, base_dir=base_dir)

    # Every present file's contents must appear verbatim.
    for name in set(paths) & present:
        assert contents[name] in text
    # Every missing path in the input is reported in ``missing`` at
    # least once.
    expected_missing = [p for p in paths if p not in present]
    assert missing == expected_missing
    # A warning was emitted for each missing path; duplicates are
    # logged per occurrence.
    warning_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING"
    ]
    for p in expected_missing:
        assert any(p in msg for msg in warning_msgs)


@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(layout=_context_files_layout_strategy())
def test_compose_context_does_not_abort_on_missing_context_files(
    tmp_path: Path,
    layout: tuple[list[str], set[str], dict[str, str]],
) -> None:
    """Validates: Requirements 18.5, 18.6.

    Composition proceeds normally even when a referenced context file
    is missing. The composed window still includes the brief, spec
    body, and rendered persona prompt; no exception is raised.
    """
    paths, present, contents = layout
    base_dir = _fresh_subdir(tmp_path)
    for name in present:
        (base_dir / name).write_text(contents[name], encoding="utf-8")

    task = Task(
        id="t-001",
        title="title",
        priority=0,
        status="pending",
        spec_path="specs/t-001.md",
    )
    spec = TaskSpec(
        id="t-001",
        title="title",
        validation=[ShellCheckConfig(type="shell", commands=["true"])],
        context_files=paths or None,
        body=TaskSpecBody(
            objective="OBJ_MARKER",
            context_references="CTX_REFS",
            instructions="INSTR_MARKER",
        ),
    )
    persona = Persona(
        name="Writer",
        description="writer desc",
        prompt_template="PROMPT_MARKER {{persona_name}}",
    )

    window = compose_context(
        task=task,
        spec=spec,
        persona=persona,
        brief="BRIEF_MARKER",
        base_dir=base_dir,
        max_tokens=1_000_000,
    )

    assert "BRIEF_MARKER" in window.text
    assert "OBJ_MARKER" in window.text
    assert "INSTR_MARKER" in window.text
    assert "PROMPT_MARKER Writer" in window.text
    # Present files were inlined; missing ones silently omitted.
    for name in set(paths) & present:
        assert contents[name] in window.text
