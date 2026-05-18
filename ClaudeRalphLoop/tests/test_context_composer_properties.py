"""Property-based tests for ``ralph_loop.context`` (design Properties 9,
10, 12, 21).

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

- Property 21 (Truncation is idempotent and byte-bounded): for any
  ``bytes`` payload and any positive ``cap``,
  ``_truncate_to_codepoint_boundary`` produces a prefix whose length
  is bounded by ``[max(0, cap - 3), cap]`` when the input exceeds the
  cap, is unchanged on a second application, and — when the input is
  itself well-formed UTF-8 — decodes cleanly under strict UTF-8. R2.8
  constrains only the cut point at the tail, so malformed interior
  bytes in the input pass through unchanged (real callers decode with
  ``errors="replace"``).

  Validates: Requirements 2.4, 2.8.

- Property 22 (Truncation marker invariant): for any ``bytes`` payload
  ``data`` of size ``N`` written to a tempfile and any positive
  ``cap``, the Markdown block returned by ``_inline_one_context_file``
  ends with the Truncation_Marker when ``N > cap`` and contains no
  ``"[truncated:"`` substring when ``N <= cap``. The marker's captured
  groups are ``(str(N), str(cap))``.

  Validates: Requirements 2.4, 2.5, 2.7.
"""

# Feature: resilient-invocation-and-context-truncation, Property 21: Truncation is idempotent and byte-bounded
# Feature: resilient-invocation-and-context-truncation, Property 22: Truncation marker invariant
# Feature: ralph-loop, Property 9 & 10 & 12: Context window content inclusion, truncation, and missing-reference warnings

from __future__ import annotations

import logging
import re
import string
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ralph_loop.context import (
    LOOP_FRAMING,
    RESUMED_NOTICE,
    TRUNCATION_MARKER_TEMPLATE,
    _inline_one_context_file,
    _truncate_to_codepoint_boundary,
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


# ---------------------------------------------------------------------------
# Property 21: Truncation is idempotent and byte-bounded
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(
    data=st.binary(max_size=2048),
    cap=st.integers(min_value=1, max_value=1024),
)
def test_truncate_to_codepoint_boundary_byte_bounded_and_idempotent(
    data: bytes, cap: int
) -> None:
    """Validates: Requirements 2.4, 2.8.

    For any ``bytes`` payload ``data`` and any positive ``cap``, the
    retained prefix produced by
    :func:`_truncate_to_codepoint_boundary` must satisfy three
    predicates jointly:

    1. **Byte bounds (R2.4).** ``0 <= len(retained) <= cap``, and when
       ``len(data) > cap`` the retained length is at least
       ``cap - 3`` because a UTF-8 codepoint is at most 4 bytes wide
       so the boundary rewind drops at most 3 bytes. When the input
       already fits under the cap, the function returns it unchanged.
    2. **Strict UTF-8 decode at the cut point (R2.8).** R2.8
       constrains only the *cut point* at the tail: when truncation
       cuts inside a multi-byte code point, the helper rewinds to the
       nearest complete code point boundary so the tail is not a
       partial code point. It does **not** promise to sanitize
       malformed byte sequences that live in the *interior* of the
       input — real callers decode the retained prefix with
       ``errors="replace"`` and a standalone continuation byte in the
       middle of the input is simply passed through. The property
       therefore enforces strict-UTF-8 decodability of ``retained``
       only when the input was well-formed UTF-8 to begin with and
       truncation actually occurred; when the input already contains
       malformed interior bytes, those bytes survive in ``retained``
       unchanged and strict decoding is not required.
    3. **Idempotence.** Applying the function to its own output
       returns the output unchanged, so repeated truncation of an
       already-truncated prefix is a no-op.
    """
    retained = _truncate_to_codepoint_boundary(data, cap)

    # 1. Byte bounds.
    assert 0 <= len(retained) <= cap
    if len(data) > cap:
        assert len(retained) >= cap - 3
    else:
        assert retained == data

    # 2. Strict UTF-8 decode at the tail: when truncation occurred AND
    # the original input was well-formed UTF-8, the retained prefix
    # must also be well-formed. R2.8 only constrains the cut-point's
    # well-formedness, so malformed interior bytes in the input are
    # preserved as-is (real callers decode with ``errors="replace"``).
    if len(data) > cap:
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            pass  # input already malformed; R2.8 does not constrain output
        else:
            retained.decode("utf-8", errors="strict")

    # 3. Idempotence.
    assert _truncate_to_codepoint_boundary(retained, cap) == retained


# ---------------------------------------------------------------------------
# Property 22: Truncation marker invariant
# ---------------------------------------------------------------------------


# Regex for the Truncation_Marker as it appears at the end of a Markdown
# block emitted by ``_inline_one_context_file``. The marker sits on its
# own line after the body (R2.7), so the pattern anchors on a leading
# newline and on end-of-string (``\Z``). The two captured groups are the
# original byte size and the configured cap, both rendered as decimal
# integers by ``str.format``.
TRUNCATION_MARKER_REGEX = (
    r"\n\[truncated: (\d+) bytes, showing first (\d+) bytes\]\Z"
)


@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    data=st.binary(max_size=4096),
    cap=st.integers(min_value=1, max_value=512),
)
def test_inline_one_context_file_marker_invariant(
    tmp_path: Path,
    data: bytes,
    cap: int,
) -> None:
    """Validates: Requirements 2.4, 2.5, 2.7.

    For any ``bytes`` payload ``data`` of size ``N`` written to a
    tempfile and any positive ``cap``, the Markdown block returned by
    :func:`_inline_one_context_file` satisfies the Truncation_Marker
    invariant in both directions:

    * **Over-cap branch (R2.4, R2.5, R2.7).** When ``N > cap``, the
      block ends with the Truncation_Marker on its own line,
      ``TRUNCATION_MARKER_REGEX`` matches at the tail, and the two
      captured groups equal ``(str(N), str(cap))`` — i.e. the marker
      faithfully records the original byte size and the configured
      cap.
    * **Under-or-at-cap branch (R2.3).** When ``N <= cap``, the block
      contains no ``"[truncated:"`` substring at all, so the marker
      cannot leak into files that were included verbatim.

    A fresh per-example subdirectory keeps the counter-example
    reproducible when Hypothesis reuses ``tmp_path`` across examples
    in the same pytest invocation.
    """
    base_dir = _fresh_subdir(tmp_path)
    path = base_dir / "ctx.md"
    path.write_bytes(data)
    n = len(data)

    block = _inline_one_context_file(
        "ctx.md", path, max_file_bytes=cap
    )

    if n > cap:
        match = re.search(TRUNCATION_MARKER_REGEX, block)
        assert match is not None, (
            f"marker missing from over-cap block (N={n}, cap={cap}): "
            f"{block!r}"
        )
        assert match.group(1) == str(n)
        assert match.group(2) == str(cap)
    else:
        assert "[truncated:" not in block
