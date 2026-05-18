"""Unit tests for ``ralph_loop.context`` (Tasks 11.3 and 11.5).

Covers :func:`inline_context_files` (R18.5, R18.6) and
:func:`compose_context` (R5.3, R6.1-R6.7, R14.5). The matching property
tests for content inclusion (P9) and truncation (P10) land in
:mod:`tests.test_context_composer_properties`.

Requirements exercised: 5.3, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 14.5, 18.5, 18.6.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from ralph_loop.context import (
    LOOP_FRAMING,
    RESUMED_NOTICE,
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
# Fixtures
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "t-001",
    title: str = "Draft chapter",
    status: str = "pending",
    priority: int = 10,
) -> Task:
    return Task(
        id=id,
        title=title,
        priority=priority,
        status=status,  # type: ignore[arg-type]
        spec_path=f"specs/{id}.md",
    )


def _make_spec(
    *,
    id: str = "t-001",
    title: str = "Draft chapter",
    objective: str = "OBJECTIVE",
    context_references: str = "CTX_REF",
    instructions: str = "INSTR",
    notes: str | None = None,
    context_files: list[str] | None = None,
) -> TaskSpec:
    return TaskSpec(
        id=id,
        title=title,
        validation=[ShellCheckConfig(type="shell", commands=["true"])],
        context_files=context_files,
        body=TaskSpecBody(
            objective=objective,
            context_references=context_references,
            instructions=instructions,
            notes=notes,
        ),
    )


def _make_persona(
    *,
    name: str = "Writer",
    prompt_template: str = "PROMPT[{{persona_name}}]",
    instructions: str | None = "PERSONA_INSTR",
) -> Persona:
    return Persona(
        name=name,
        description=f"{name} description",
        prompt_template=prompt_template,
        instructions=instructions,
    )


# ---------------------------------------------------------------------------
# inline_context_files (R18.5, R18.6)
# ---------------------------------------------------------------------------


class TestInlineContextFiles:
    def test_empty_paths_returns_empty_text_and_empty_missing(
        self, tmp_path: Path
    ) -> None:
        text, missing = inline_context_files([], base_dir=tmp_path)

        assert text == ""
        assert missing == []

    def test_all_existing_files_are_inlined(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("CONTENT_A", encoding="utf-8")
        (tmp_path / "b.md").write_text("CONTENT_B", encoding="utf-8")

        text, missing = inline_context_files(
            ["a.md", "b.md"], base_dir=tmp_path
        )

        assert missing == []
        assert "CONTENT_A" in text
        assert "CONTENT_B" in text
        # Each file must be labeled so downstream consumers can
        # distinguish chunks.
        assert "### File: a.md" in text
        assert "### File: b.md" in text

    def test_missing_file_is_recorded_and_logged_without_raising(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (tmp_path / "present.md").write_text("PRESENT", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="ralph_loop.context"):
            text, missing = inline_context_files(
                ["present.md", "missing.md"],
                base_dir=tmp_path,
            )

        assert missing == ["missing.md"]
        assert "PRESENT" in text
        # A warning was emitted identifying the missing path.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("missing.md" in r.getMessage() for r in warnings)

    def test_directory_is_treated_as_missing(self, tmp_path: Path) -> None:
        (tmp_path / "subdir").mkdir()

        text, missing = inline_context_files(
            ["subdir"], base_dir=tmp_path
        )

        assert missing == ["subdir"]
        assert text == ""

    def test_no_base_dir_uses_cwd_semantics(self, tmp_path: Path) -> None:
        # A path that clearly doesn't exist on the system. Without
        # ``base_dir`` the function should still log + record missing
        # without raising.
        text, missing = inline_context_files(
            [str(tmp_path / "definitely-not-here.md")]
        )

        assert len(missing) == 1
        assert text == ""


# ---------------------------------------------------------------------------
# compose_context (R5.3, R6.1-R6.7, R14.5)
# ---------------------------------------------------------------------------


class TestComposeContext:
    def test_untruncated_contains_every_section_in_order(
        self, tmp_path: Path
    ) -> None:
        task = _make_task()
        spec = _make_spec()
        persona = _make_persona()
        brief = "PROJECT_BRIEF_CONTENT"

        window = compose_context(
            task=task,
            spec=spec,
            persona=persona,
            brief=brief,
            base_dir=tmp_path,
        )

        # Each required section is present and truncated=False.
        assert window.truncated is False
        assert LOOP_FRAMING.strip() in window.text  # R6.6
        assert "PROJECT_BRIEF_CONTENT" in window.text  # R6.2
        assert "OBJECTIVE" in window.text  # R6.3 (spec body)
        assert "INSTR" in window.text  # R6.3 (spec body)
        assert "PROMPT[Writer]" in window.text  # R6.4 (rendered prompt)
        assert "PERSONA_INSTR" in window.text  # R6.5 (persona instructions)
        # Section ordering: framing < brief < spec < persona.
        framing_pos = window.text.find("Ralph Loop Iteration")
        brief_pos = window.text.find("PROJECT_BRIEF_CONTENT")
        spec_pos = window.text.find("OBJECTIVE")
        persona_pos = window.text.find("PROMPT[Writer]")
        assert framing_pos < brief_pos < spec_pos < persona_pos

    def test_resumed_notice_included_when_requested(
        self, tmp_path: Path
    ) -> None:
        window = compose_context(
            task=_make_task(),
            spec=_make_spec(),
            persona=_make_persona(),
            brief="BRIEF",
            resumed_notice=True,
            base_dir=tmp_path,
        )

        assert RESUMED_NOTICE.strip() in window.text
        # Notice appears after framing but before brief (R14.5 ordering).
        framing_pos = window.text.find("Ralph Loop Iteration")
        notice_pos = window.text.find("Resumed from interruption")
        brief_pos = window.text.find("BRIEF")
        assert framing_pos < notice_pos < brief_pos

    def test_resumed_notice_omitted_by_default(self, tmp_path: Path) -> None:
        window = compose_context(
            task=_make_task(),
            spec=_make_spec(),
            persona=_make_persona(),
            brief="BRIEF",
            base_dir=tmp_path,
        )

        assert "Resumed from interruption" not in window.text

    def test_escalation_context_appended_when_present(
        self, tmp_path: Path
    ) -> None:
        window = compose_context(
            task=_make_task(),
            spec=_make_spec(),
            persona=_make_persona(),
            brief="BRIEF",
            escalation_context="ESCALATION_DETAILS",
            base_dir=tmp_path,
        )

        assert "Escalation Context" in window.text
        assert "ESCALATION_DETAILS" in window.text
        # Escalation context appears last.
        persona_pos = window.text.find("PROMPT[Writer]")
        escalation_pos = window.text.find("ESCALATION_DETAILS")
        assert persona_pos < escalation_pos

    def test_persona_without_instructions_omits_section(
        self, tmp_path: Path
    ) -> None:
        window = compose_context(
            task=_make_task(),
            spec=_make_spec(),
            persona=_make_persona(instructions=None),
            brief="BRIEF",
            base_dir=tmp_path,
        )

        assert "Persona Instructions" not in window.text

    def test_context_files_are_inlined_into_spec_section(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "ctx.md").write_text("CTX_CONTENT", encoding="utf-8")
        spec = _make_spec(context_files=["ctx.md"])

        window = compose_context(
            task=_make_task(),
            spec=spec,
            persona=_make_persona(),
            brief="BRIEF",
            base_dir=tmp_path,
        )

        assert "CTX_CONTENT" in window.text
        assert "### File: ctx.md" in window.text

    def test_missing_context_file_does_not_abort(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        spec = _make_spec(context_files=["absent.md"])

        with caplog.at_level(logging.WARNING, logger="ralph_loop.context"):
            window = compose_context(
                task=_make_task(),
                spec=spec,
                persona=_make_persona(),
                brief="BRIEF",
                base_dir=tmp_path,
            )

        # Composition succeeded; warning logged.
        assert "BRIEF" in window.text
        assert any(
            "absent.md" in r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING"
        )

    def test_truncation_preserves_spec_persona_and_flags(
        self, tmp_path: Path
    ) -> None:
        # A huge brief guarantees overflow against a small token cap.
        big_brief = "X" * 10_000
        task = _make_task()
        spec = _make_spec(
            objective="OBJ_MARKER",
            instructions="INSTR_MARKER",
        )
        persona = _make_persona(
            prompt_template="PROMPT_MARKER {{persona_name}}",
            instructions="PERSONA_INSTR_MARKER",
        )

        window = compose_context(
            task=task,
            spec=spec,
            persona=persona,
            brief=big_brief,
            max_tokens=100,
            base_dir=tmp_path,
        )

        assert window.truncated is True
        # R6.7: spec body + persona prompt + persona instructions survive.
        assert "OBJ_MARKER" in window.text
        assert "INSTR_MARKER" in window.text
        assert "PROMPT_MARKER Writer" in window.text
        assert "PERSONA_INSTR_MARKER" in window.text
        # Brief has been shrunk; truncation notice must be present.
        assert "[... project brief truncated ...]" in window.text
        # The full 10_000-char brief is no longer inlined verbatim.
        assert big_brief not in window.text

    def test_approx_tokens_matches_text_length_heuristic(
        self, tmp_path: Path
    ) -> None:
        window = compose_context(
            task=_make_task(),
            spec=_make_spec(),
            persona=_make_persona(),
            brief="BRIEF",
            base_dir=tmp_path,
        )

        # Heuristic is len(text) // 4.
        assert window.approx_tokens == len(window.text) // 4


# ---------------------------------------------------------------------------
# _truncate_to_codepoint_boundary (R2.4, R2.8) -- exercises Property 21
# ---------------------------------------------------------------------------


class TestTruncateToCodepointBoundary:
    """Unit tests for :func:`_truncate_to_codepoint_boundary`.

    The helper is the pure core of per-file context truncation
    (R2.4, R2.8). These tests pin down specific mid-codepoint cuts
    for each UTF-8 width (1-4 bytes) so the algorithm's correctness
    is observable from named examples, not only from the aggregate
    property test (Property 21).
    """

    def test_cap_zero_returns_empty_bytes(self) -> None:
        # cap=0 must yield b"" regardless of the input length so the
        # retained prefix is always <= cap (R2.4).
        assert _truncate_to_codepoint_boundary(b"hello", 0) == b""
        assert _truncate_to_codepoint_boundary(b"", 0) == b""
        assert _truncate_to_codepoint_boundary(b"\xc3\xa9", 0) == b""

    def test_cap_at_or_above_length_returns_data_unchanged(self) -> None:
        # Fast path: nothing to trim. Returned bytes must equal the
        # input verbatim, preserving Property 21's idempotence.
        data = b"hello world"
        assert _truncate_to_codepoint_boundary(data, len(data)) == data
        assert _truncate_to_codepoint_boundary(data, len(data) + 1) == data
        assert _truncate_to_codepoint_boundary(data, 10_000) == data
        assert _truncate_to_codepoint_boundary(b"", 5) == b""

    def test_ascii_over_cap_truncates_to_exactly_cap(self) -> None:
        # ASCII is 1 byte per codepoint so no rewind is ever needed;
        # the retained prefix length equals ``cap`` exactly.
        data = b"abcdefghij"
        retained = _truncate_to_codepoint_boundary(data, 4)
        assert retained == b"abcd"
        assert len(retained) == 4

    def test_pure_ascii_input_never_rewinds(self) -> None:
        # Sweep every cap in [0, len(data)] and verify the retained
        # length equals the cap (no rewind ever). This is the
        # positive-space complement to the multi-byte rewind tests.
        data = b"The quick brown fox jumps over the lazy dog."
        for cap in range(len(data) + 1):
            retained = _truncate_to_codepoint_boundary(data, cap)
            assert len(retained) == cap
            assert retained == data[:cap]

    def test_two_byte_codepoint_rewinds_one_byte(self) -> None:
        # "é" encodes as b"\xc3\xa9" (2 bytes). Cutting between the
        # lead byte and its continuation byte must rewind 1 byte so
        # the returned prefix decodes cleanly (R2.8).
        data = "é".encode("utf-8")
        assert data == b"\xc3\xa9"

        # cap=1 lands on the lead byte; the partial codepoint must be
        # dropped entirely, yielding b"".
        retained = _truncate_to_codepoint_boundary(data, 1)
        assert retained == b""
        retained.decode("utf-8")  # strict decode must succeed

        # "aé" has one ASCII byte before the two-byte codepoint. A
        # cap of 2 cuts after the lead byte of "é" and must rewind
        # back to just "a" (1 byte dropped).
        data2 = "aé".encode("utf-8")
        assert data2 == b"a\xc3\xa9"
        retained2 = _truncate_to_codepoint_boundary(data2, 2)
        assert retained2 == b"a"
        assert retained2.decode("utf-8") == "a"

    def test_three_byte_codepoint_rewinds_one_or_two_bytes(self) -> None:
        # "€" encodes as b"\xe2\x82\xac" (3 bytes). A mid-codepoint
        # cut must rewind 1 or 2 bytes depending on where it lands.
        data = "€".encode("utf-8")
        assert data == b"\xe2\x82\xac"

        # cap=1 lands on the lead byte alone (expected_len=3,
        # actual_len=1 < 3); drop it entirely.
        retained_1 = _truncate_to_codepoint_boundary(data, 1)
        assert retained_1 == b""
        retained_1.decode("utf-8")

        # cap=2 lands on the lead + one continuation (actual_len=2 < 3);
        # drop the whole codepoint, 2 bytes rewound.
        retained_2 = _truncate_to_codepoint_boundary(data, 2)
        assert retained_2 == b""
        retained_2.decode("utf-8")

        # "a€" = b"a\xe2\x82\xac"; cap=3 cuts after the second byte
        # of "€" and must rewind 2 bytes back to "a".
        data2 = "a€".encode("utf-8")
        assert data2 == b"a\xe2\x82\xac"
        retained2 = _truncate_to_codepoint_boundary(data2, 3)
        assert retained2 == b"a"
        assert retained2.decode("utf-8") == "a"

    def test_four_byte_codepoint_rewinds_up_to_three_bytes(self) -> None:
        # "🎉" encodes as b"\xf0\x9f\x8e\x89" (4 bytes). Cutting any
        # of the first three bytes must rewind up to 3 bytes so the
        # retained prefix decodes cleanly (R2.8).
        data = "🎉".encode("utf-8")
        assert data == b"\xf0\x9f\x8e\x89"

        for cap in (1, 2, 3):
            retained = _truncate_to_codepoint_boundary(data, cap)
            assert retained == b""
            retained.decode("utf-8")

        # cap=4 fits the whole codepoint (fast path).
        assert _truncate_to_codepoint_boundary(data, 4) == data

        # "a🎉" = b"a\xf0\x9f\x8e\x89"; cap=4 cuts after three bytes
        # of "🎉" and must rewind 3 bytes back to "a".
        data2 = "a🎉".encode("utf-8")
        assert data2 == b"a\xf0\x9f\x8e\x89"
        retained2 = _truncate_to_codepoint_boundary(data2, 4)
        assert retained2 == b"a"
        assert retained2.decode("utf-8") == "a"

    def test_pure_multi_byte_input_lands_on_codepoint_boundary(self) -> None:
        # A string of the same multi-byte codepoint repeated.
        # Sweeping every cap must yield a prefix that decodes strictly
        # and whose length is always a multiple of the codepoint's
        # byte width.
        #
        # Use "€" (3 bytes) so mid-codepoint cuts exercise both
        # single-byte and two-byte rewinds.
        text = "€" * 8
        data = text.encode("utf-8")
        assert len(data) == 24

        for cap in range(len(data) + 1):
            retained = _truncate_to_codepoint_boundary(data, cap)
            # Length is always a multiple of 3 (codepoint width).
            assert len(retained) % 3 == 0
            # Strict decode must succeed: no partial codepoint.
            retained.decode("utf-8")
            # Retained length never exceeds the cap.
            assert len(retained) <= cap

        # Same sweep with a 4-byte codepoint to cover the widest case.
        text4 = "🎉" * 5
        data4 = text4.encode("utf-8")
        assert len(data4) == 20
        for cap in range(len(data4) + 1):
            retained = _truncate_to_codepoint_boundary(data4, cap)
            assert len(retained) % 4 == 0
            retained.decode("utf-8")
            assert len(retained) <= cap

    def test_continuation_byte_at_position_zero_returns_empty(self) -> None:
        # A buffer that starts with a continuation byte is malformed
        # UTF-8; the helper must still return a well-formed empty
        # prefix rather than raise or emit the stray continuation.
        data = b"\x82\x82\x82"
        retained = _truncate_to_codepoint_boundary(data, 1)
        assert retained == b""
        retained.decode("utf-8")

        # Same result whatever the cap, as long as it is below the
        # length: every leader-less walk back exits at i == 0.
        for cap in (1, 2, 3):
            retained = _truncate_to_codepoint_boundary(data, cap)
            if cap >= len(data):
                # Fast path returns the (malformed) data unchanged.
                assert retained == data
            else:
                assert retained == b""

    def test_malformed_lead_byte_is_dropped(self) -> None:
        # A byte in 0xF8..0xFF has its top bit set (it survives the
        # continuation walk-back) but does not match any of the valid
        # 2/3/4-byte UTF-8 leader patterns (0xC0/0xE0/0xF0). The helper
        # must hit the malformed-lead fallback (``expected_len = None``)
        # and drop the byte unconditionally so the retained prefix
        # never contains an invalid leader (R2.8).
        for malformed_lead in (b"\xf8", b"\xfc", b"\xfe", b"\xff"):
            data = malformed_lead + b"\x82\x82"
            # cap=2 leaves prefix=<lead><cont>; the walk rewinds past
            # the continuation, lands on the malformed lead, and the
            # fallback path drops it.
            retained = _truncate_to_codepoint_boundary(data, 2)
            assert retained == b""
            retained.decode("utf-8")  # strict decode succeeds

            # cap=1 lands directly on the malformed lead with no
            # continuation walk; same fallback fires, same result.
            retained_1 = _truncate_to_codepoint_boundary(data, 1)
            assert retained_1 == b""
            retained_1.decode("utf-8")


# ---------------------------------------------------------------------------
# _inline_one_context_file (R2.3-R2.7) -- exercises Property 22
# ---------------------------------------------------------------------------


# Design's Truncation_Marker regex (Property 22). The marker is anchored
# to end-of-string (``\Z``) so the test also asserts the marker lands on
# its own line as the final line of the emitted Markdown block (R2.7).
TRUNCATION_MARKER_REGEX = (
    r"\n\[truncated: (\d+) bytes, showing first (\d+) bytes\]\Z"
)


class TestInlineOneContextFile:
    """Unit tests for :func:`_inline_one_context_file`.

    Exercises the three observable behaviours required by R2.3-R2.7:

    * Under-cap files emit the body verbatim with no marker (R2.3).
    * At-cap files emit the body verbatim with no marker: the boundary
      is inclusive per ``original <= max_file_bytes`` (R2.3, R2.4).
    * Over-cap files emit the truncated body followed by the
      deterministic Truncation_Marker on its own line (R2.4, R2.5,
      R2.7) and emit a WARNING-level structured Truncation_Event log
      record carrying ``path``, ``original_bytes``, ``retained_bytes``,
      and ``cap_bytes`` (R2.6).
    """

    def test_file_smaller_than_cap_is_verbatim_with_no_marker(
        self, tmp_path: Path
    ) -> None:
        # A small file must be inlined unchanged: no truncation, no
        # marker, and the Markdown header must identify the relative
        # path so downstream consumers can distinguish files (R2.3).
        path = tmp_path / "small.md"
        body = "hello world\nline 2\n"
        path.write_bytes(body.encode("utf-8"))

        with capture_logs() as logs:
            block = _inline_one_context_file(
                "small.md", path, max_file_bytes=1024
            )

        assert block == f"### File: small.md\n\n{body}"
        assert "[truncated:" not in block
        # No Truncation_Event was emitted because no truncation
        # occurred (R2.6 only fires on over-cap files).
        assert not any(
            r.get("event") == "context_file_truncated" for r in logs
        )

    def test_file_exactly_at_cap_is_verbatim_with_no_marker(
        self, tmp_path: Path
    ) -> None:
        # The cap boundary is INCLUSIVE: the spec says "whose UTF-8
        # encoded byte size is less than or equal to the cap" (R2.3).
        # A file whose size equals the cap exactly must therefore
        # emit the body verbatim with no marker.
        path = tmp_path / "boundary.md"
        body_bytes = b"a" * 64
        path.write_bytes(body_bytes)

        with capture_logs() as logs:
            block = _inline_one_context_file(
                "boundary.md", path, max_file_bytes=64
            )

        assert block == f"### File: boundary.md\n\n{body_bytes.decode()}"
        assert "[truncated:" not in block
        assert not any(
            r.get("event") == "context_file_truncated" for r in logs
        )

    def test_file_larger_than_cap_ends_with_marker_on_its_own_line(
        self, tmp_path: Path
    ) -> None:
        # An over-cap file must end with the marker on its own line
        # (R2.7) and the marker text must match the R2.5 template
        # with the original byte size and the configured cap
        # substituted. ASCII input means no codepoint rewind, so the
        # retained prefix is exactly ``cap`` bytes.
        path = tmp_path / "big.md"
        original_bytes = b"a" * 200
        cap = 64
        path.write_bytes(original_bytes)

        block = _inline_one_context_file(
            "big.md", path, max_file_bytes=cap
        )

        expected_marker = (
            f"\n[truncated: {len(original_bytes)} bytes, "
            f"showing first {cap} bytes]"
        )
        assert block.endswith(expected_marker)
        # Body preceding the marker is the truncated prefix inlined
        # under the file header. ASCII input means retained == cap.
        retained_body = "a" * cap
        assert block == (
            f"### File: big.md\n\n{retained_body}{expected_marker}"
        )

    def test_over_cap_marker_matches_design_regex(
        self, tmp_path: Path
    ) -> None:
        # Property 22's TRUNCATION_MARKER_REGEX must match the emitted
        # block and its captured groups must equal (original, cap).
        # We use a payload that exercises a mid-codepoint cut on a
        # 3-byte codepoint so the retained size can differ from the
        # cap; the marker is measured against the configured cap, not
        # against the retained bytes (R2.5).
        path = tmp_path / "utf8.md"
        # "€" is b"\xe2\x82\xac" (3 bytes). 10 copies = 30 bytes.
        original = ("€" * 10).encode("utf-8")
        assert len(original) == 30
        path.write_bytes(original)
        cap = 10  # mid-3rd-codepoint cut; boundary rewind drops 1 byte

        block = _inline_one_context_file(
            "utf8.md", path, max_file_bytes=cap
        )

        match = re.search(TRUNCATION_MARKER_REGEX, block)
        assert match is not None, (
            f"marker did not match design regex; block={block!r}"
        )
        assert int(match.group(1)) == len(original)
        assert int(match.group(2)) == cap

    def test_truncation_event_captured_with_required_keys(
        self, tmp_path: Path
    ) -> None:
        # R2.6: on truncation, emit a WARNING-level structured log
        # record ``event="context_file_truncated"`` carrying ``path``,
        # ``original_bytes``, ``retained_bytes``, and ``cap_bytes``.
        # ``structlog.testing.capture_logs`` records every structured
        # event emitted during the block regardless of the processor
        # chain configured by the application.
        path = tmp_path / "warn.md"
        original = b"x" * 500
        cap = 100
        path.write_bytes(original)

        with capture_logs() as logs:
            block = _inline_one_context_file(
                "warn.md", path, max_file_bytes=cap
            )

        events = [
            r for r in logs if r.get("event") == "context_file_truncated"
        ]
        assert len(events) == 1, (
            f"expected exactly one context_file_truncated log; got {logs!r}"
        )
        record = events[0]
        # WARNING level (R2.6).
        assert record.get("log_level") == "warning"
        # Required keys (R2.6).
        assert record["path"] == "warn.md"
        assert record["original_bytes"] == len(original)
        assert record["cap_bytes"] == cap
        # retained_bytes must match what actually ends up in the
        # emitted block's body (so operators can trust the log when
        # correlating with the composed prompt).
        assert record["retained_bytes"] == cap  # pure ASCII, no rewind
        # And the block itself still carries the marker.
        assert "[truncated:" in block
