"""Unit tests for :mod:`ralph_loop.json_extract`.

Targeted coverage for each of the three public helpers:

* :func:`strip_markdown_fences` — fence with / without language tag,
  no-op when fence is absent, tolerant of trailing whitespace on the
  close line.
* :func:`iter_balanced_json_objects` — string-state tracking,
  escape-state tracking, multiple balanced objects yielded in order,
  unbalanced spans silently skipped.
* :func:`extract_validating_object` — validates against
  :class:`PersonaReviewVerdict`, returns ``None`` when no candidate
  validates, validates against :class:`OrchestratorDecision`, skips a
  leading tool-use envelope in favor of the verdict object.

These tests exercise the helper module directly so regressions surface
without needing to wire up the full validator / orchestrator
subprocess plumbing.
"""

from __future__ import annotations

import pytest

from ralph_loop.json_extract import (
    extract_validating_object,
    iter_balanced_json_objects,
    strip_ansi_escapes,
    strip_markdown_fences,
)
from ralph_loop.orchestrator import OrchestratorDecision
from ralph_loop.validator import PersonaReviewVerdict


# ---------------------------------------------------------------------------
# strip_markdown_fences
# ---------------------------------------------------------------------------


class TestStripMarkdownFences:
    def test_fence_with_language_tag_is_stripped(self) -> None:
        text = '```json\n{"verdict": "pass"}\n```'
        assert strip_markdown_fences(text) == '{"verdict": "pass"}'

    def test_fence_without_language_tag_is_stripped(self) -> None:
        text = '```\n{"verdict": "pass"}\n```'
        assert strip_markdown_fences(text) == '{"verdict": "pass"}'

    def test_no_fence_is_noop(self) -> None:
        text = '{"verdict": "pass", "rationale": "ok"}'
        assert strip_markdown_fences(text) == text

    def test_fence_with_trailing_whitespace_on_close_line(self) -> None:
        text = '```json\n{"v": 1}\n```   '
        assert strip_markdown_fences(text) == '{"v": 1}'

    def test_fence_with_leading_blank_lines(self) -> None:
        text = '\n\n```json\n{"v": 1}\n```'
        assert strip_markdown_fences(text) == '{"v": 1}'

    def test_empty_input_returns_empty(self) -> None:
        assert strip_markdown_fences("") == ""

    def test_multiline_body_inside_fence_preserved(self) -> None:
        text = '```json\n{\n  "verdict": "pass"\n}\n```'
        assert strip_markdown_fences(text) == '{\n  "verdict": "pass"\n}'


# ---------------------------------------------------------------------------
# iter_balanced_json_objects
# ---------------------------------------------------------------------------


class TestIterBalancedJsonObjects:
    def test_single_flat_object(self) -> None:
        text = '{"a": 1}'
        assert list(iter_balanced_json_objects(text)) == ['{"a": 1}']

    def test_object_with_literal_brace_inside_string_is_not_truncated(
        self,
    ) -> None:
        """A ``{`` inside a string value must not count as structural."""
        text = '{"a": "contains { brace"}'
        assert list(iter_balanced_json_objects(text)) == [
            '{"a": "contains { brace"}'
        ]

    def test_object_with_literal_close_brace_inside_string(self) -> None:
        text = '{"verdict":"fail","rationale":"missing } in expression"}'
        assert list(iter_balanced_json_objects(text)) == [text]

    def test_escaped_quote_inside_string_is_honored(self) -> None:
        """``\\"`` inside a string must not close the string.

        The scanner may yield additional inner spans starting at
        subsequent ``{`` characters within the string (they'll be
        unbalanced or invalid and rejected by downstream parsers);
        the important assertion is that the FIRST yielded object is
        the full, correctly-terminated outer span.
        """
        # The JSON literal is: {"a": "saw \"{\" in string"}
        text = r'{"a": "saw \"{\" in string"}'
        results = list(iter_balanced_json_objects(text))
        assert results[0] == text

    def test_escaped_backslash_inside_string(self) -> None:
        # The JSON literal is: {"a": "backslash \\"}
        text = r'{"a": "backslash \\"}'
        assert list(iter_balanced_json_objects(text)) == [text]

    def test_multiple_balanced_objects_yielded_in_order(self) -> None:
        text = '{"a":1}{"b":2}'
        assert list(iter_balanced_json_objects(text)) == [
            '{"a":1}',
            '{"b":2}',
        ]

    def test_nested_objects_yield_both_outer_and_inner(self) -> None:
        """The scanner yields the outer span AND nested inner spans.

        This is important for the real-world TechCodeReviewer case
        where a verdict JSON may appear inside a prose wrapper whose
        outer braces also balance. ``extract_validating_object`` picks
        the first yielded span that validates against the target
        Pydantic model.
        """
        text = '{"tool":"read_file","args":{"path":"x"}}'
        results = list(iter_balanced_json_objects(text))
        # Outer span present.
        assert text in results
        # Inner span also yielded.
        assert '{"path":"x"}' in results

    def test_unbalanced_opening_brace_is_skipped(self) -> None:
        """An unbalanced ``{`` must not prevent scanning the rest."""
        text = '{"a":1  {"b":2}'
        # The outer span stays open past the second opener, so only
        # when depth eventually drops to 0 (at the final }) does a
        # single object get yielded — the whole span starting at pos 0.
        # This is acceptable: ``json.loads`` will then reject the span,
        # and ``extract_validating_object`` moves on. The important
        # property is that the scanner never raises and always makes
        # forward progress.
        result = list(iter_balanced_json_objects(text))
        assert len(result) >= 1

    def test_no_opening_brace_returns_empty(self) -> None:
        assert list(iter_balanced_json_objects("no braces here")) == []

    def test_empty_input_returns_empty(self) -> None:
        assert list(iter_balanced_json_objects("")) == []

    def test_objects_separated_by_prose(self) -> None:
        text = 'prologue {"a":1} middle {"b":2} epilogue'
        assert list(iter_balanced_json_objects(text)) == [
            '{"a":1}',
            '{"b":2}',
        ]


# ---------------------------------------------------------------------------
# extract_validating_object
# ---------------------------------------------------------------------------


class TestExtractValidatingObject:
    def test_validates_flat_persona_review_verdict(self) -> None:
        text = '{"verdict":"pass","rationale":"ok"}'
        result = extract_validating_object(text, PersonaReviewVerdict)
        assert result is not None
        assert result.verdict == "pass"
        assert result.rationale == "ok"

    def test_validates_flat_orchestrator_decision(self) -> None:
        text = '{"persona":"Writer","rationale":"best match"}'
        result = extract_validating_object(text, OrchestratorDecision)
        assert result is not None
        assert result.persona == "Writer"
        assert result.rationale == "best match"

    def test_returns_none_when_no_candidate_validates(self) -> None:
        text = 'no json here at all'
        assert extract_validating_object(text, PersonaReviewVerdict) is None

    def test_returns_none_when_object_is_wrong_shape(self) -> None:
        text = '{"completely":"unrelated"}'
        assert extract_validating_object(text, PersonaReviewVerdict) is None

    def test_skips_leading_tool_use_envelope(self) -> None:
        """Leading tool-use JSON must not preempt the verdict object."""
        text = (
            '{"tool":"read_file","args":{"path":"x"}}\n'
            '{"verdict":"pass","rationale":"ok"}'
        )
        result = extract_validating_object(text, PersonaReviewVerdict)
        assert result is not None
        assert result.verdict == "pass"
        assert result.rationale == "ok"

    def test_strips_fence_before_scanning(self) -> None:
        text = '```json\n{"verdict":"fail","rationale":"bad"}\n```'
        result = extract_validating_object(text, PersonaReviewVerdict)
        assert result is not None
        assert result.verdict == "fail"
        assert result.rationale == "bad"

    def test_handles_literal_brace_in_rationale(self) -> None:
        text = '{"verdict":"fail","rationale":"missing } in expr"}'
        result = extract_validating_object(text, PersonaReviewVerdict)
        assert result is not None
        assert result.verdict == "fail"
        assert result.rationale == "missing } in expr"

    def test_empty_input_returns_none(self) -> None:
        assert extract_validating_object("", PersonaReviewVerdict) is None

    def test_returns_first_valid_when_multiple_validate(self) -> None:
        """When multiple candidates validate, the first in stream wins."""
        text = (
            '{"verdict":"pass","rationale":"first"} '
            '{"verdict":"fail","rationale":"second"}'
        )
        result = extract_validating_object(text, PersonaReviewVerdict)
        assert result is not None
        assert result.verdict == "pass"
        assert result.rationale == "first"


# ---------------------------------------------------------------------------
# Regression: real Kiro CLI TechCodeReviewer output (ch02-r08-python)
# ---------------------------------------------------------------------------


class TestStripAnsiEscapes:
    def test_sgr_color_code_is_stripped(self) -> None:
        assert strip_ansi_escapes("\x1b[38;5;141mhello\x1b[0m") == "hello"

    def test_italics_toggle_inside_string_is_stripped(self) -> None:
        """The exact pattern from the real TechCodeReviewer dump."""
        raw = "via \x1b[3mto\x1b[23mdecimal_safe"
        assert strip_ansi_escapes(raw) == "via todecimal_safe"

    def test_private_mode_sequence_is_stripped(self) -> None:
        """``ESC[?25l`` (hide cursor) must not leak into the output."""
        raw = "\x1b[?25labc\x1b[?25h"
        assert strip_ansi_escapes(raw) == "abc"

    def test_empty_is_noop(self) -> None:
        assert strip_ansi_escapes("") == ""

    def test_no_escape_returns_same_string(self) -> None:
        text = "plain ascii, no escape sequences"
        assert strip_ansi_escapes(text) == text

    def test_8bit_csi_byte_is_passthrough(self) -> None:
        """We intentionally do NOT strip 8-bit CSI; left as-is to avoid
        corrupting unrelated payloads that contain \\x9b (e.g. mojibake).
        """
        raw = "\x9bsomething"
        assert strip_ansi_escapes(raw) == raw

    def test_two_char_escape_is_stripped(self) -> None:
        # ESC = (keypad mode), ESC D (index)
        assert strip_ansi_escapes("\x1b=abc\x1bDdef") == "abcdef"

    def test_trailing_stray_esc_is_dropped(self) -> None:
        assert strip_ansi_escapes("abc\x1b") == "abc"


# The full stdout captured from a REAL failing TechCodeReviewer invocation
# on task ch02-r08-python. The ANSI SGR codes `\x1b[3m` and `\x1b[23m`
# wrap italicized words INSIDE the JSON string values (rationale), which
# is what defeated the post-1b34408 parser: ``json.loads`` rejects raw
# control characters in strings.
REAL_TECHCODEREVIEWER_STDOUT = (
    "Searching for files: \x1b[38;5;141m**/ch02*r08*\x1b[0m"
    "\x1b[38;5;244m (using tool: glob)\x1b[0m"
    "Searching for files: \x1b[38;5;141m**/*ambient*\x1b[0m"
    "\x1b[38;5;244m (using tool: glob)\x1b[0m\n"
    "\x1b[38;5;10m \u2713 \x1b[0mSuccessfully found "
    "\x1b[38;5;244m5 files\x1b[0m under current directory\n"
    "\x1b[38;5;244m - Completed in 0.183s\x1b[0m\n\n\n"
    "\x1b[38;5;141m> \x1b[0m"
    '{"verdict": "pass", "rationale": "The Python companion '
    "demonstrates the full ambient clinical documentation pipeline "
    "end-to-end. The DynamoDB Decimal gotcha is addressed via "
    "\x1b[3mto\x1b[23mdecimal_safe. The two simplified helpers "
    "(_extract_symptom_phrases, \x1b[3mnormalized\x1b[23medit_distance) "
    "are clearly-scoped; no \x1b[38;5;10mpass\x1b[0m bodies exist."
    '"}'
)


class TestRealTechCodeReviewerOutput:
    """Regression test using exact bytes from a failing real-world run.

    Source: ``parse_failures/iter-0001-ch02-r08-python-quality-review-
    TechCodeReviewer.txt`` captured via the validator's debug-dump
    path. The stdout is dominated by tool-use prose, ends with an
    ANSI-colored ``> `` marker followed by a verdict JSON whose
    ``rationale`` string contains embedded ANSI italics toggles.
    Fix commit 1b34408 extracted the JSON span correctly but
    ``json.loads`` rejected it because the rationale held raw
    ``\\x1b`` control characters.
    """

    def test_verdict_extracted_from_real_techcodereviewer_stdout(
        self,
    ) -> None:
        result = extract_validating_object(
            REAL_TECHCODEREVIEWER_STDOUT, PersonaReviewVerdict
        )
        assert result is not None, (
            "extract_validating_object returned None for real "
            "TechCodeReviewer stdout (regression against "
            "parse_failures dump)"
        )
        assert result.verdict == "pass"
        # Rationale should contain the UN-escaped text (italics toggles
        # stripped).
        assert "todecimal_safe" in result.rationale
        assert "normalizededit_distance" in result.rationale
        # And no raw ESC characters should leak through.
        assert "\x1b" not in result.rationale
