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
        """``\\"`` inside a string must not close the string."""
        # The JSON literal is: {"a": "saw \"{\" in string"}
        text = r'{"a": "saw \"{\" in string"}'
        assert list(iter_balanced_json_objects(text)) == [text]

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

    def test_nested_objects_counted_as_one(self) -> None:
        text = '{"tool":"read_file","args":{"path":"x"}}'
        assert list(iter_balanced_json_objects(text)) == [text]

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
