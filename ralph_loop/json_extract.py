"""Shared JSON extraction helpers for persona_review and orchestrator parsing.

This module consolidates the JSON-envelope parsing logic previously
duplicated in :mod:`ralph_loop.validator` (verdict parsing) and
:mod:`ralph_loop.orchestrator` (persona-selection decision parsing) per
the ``persona-review-verdict-parsing`` bugfix spec.

Public surface (three functions):

* :func:`strip_markdown_fences` — one-pass removal of a leading
  ``\u0060\u0060\u0060<lang>`` line and a matching trailing
  ``\u0060\u0060\u0060`` line. No-op when either fence is absent.
* :func:`iter_balanced_json_objects` — yields every top-level balanced
  ``{...}`` substring in order of appearance. Tracks JSON string state
  (``"..."``) and escape state (``\\"``, ``\\\\``) so that braces inside
  string values do not miscount depth. O(n) in input length; never
  raises; silently skips unbalanced spans.
* :func:`extract_validating_object` — single entry point used by both
  callers. Strips fences, iterates candidates, returns the first one
  that successfully validates against the given Pydantic v2 model, or
  ``None`` when no candidate validates.

Design intent: the naive "find first ``{``, count braces" extractor
that previously lived in both modules returned the first balanced
``{...}`` substring without regard to JSON string/escape state or
surrounding envelopes (markdown fences, tool-use JSON wrappers).
Valid verdicts were silently dropped when a reviewing persona produced
output wrapped in a fence, prepended a tool-use envelope, or emitted a
rationale containing literal ``{``/``}`` or escaped quotes. See
``.kiro/specs/persona-review-verdict-parsing/design.md`` for the full
analysis and the ``isBugCondition`` formal specification.

No regex; no new third-party dependencies; O(n) in input length; never
raises.
"""

from __future__ import annotations

import json
from typing import Iterator, Optional, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


def strip_markdown_fences(text: str) -> str:
    """Strip a surrounding markdown code fence if one is present.

    Recognises a leading line that starts with three backticks
    (optionally followed by a language tag such as ``json``) and a
    trailing line that is exactly three backticks (allowing trailing
    whitespace). Returns the content between them when both are found;
    otherwise returns ``text`` unchanged.

    No regex. Single split/join on ``"\n"``. O(n).
    """
    if not text:
        return text
    lines = text.split("\n")
    if not lines:
        return text

    # Find the first non-empty line; if it opens a fence, strip it.
    # We only strip when the VERY FIRST line (ignoring leading blank
    # lines) is a fence opener, to avoid accidentally stripping an
    # unrelated fence embedded deeper in the stream.
    first_idx = 0
    while first_idx < len(lines) and lines[first_idx].strip() == "":
        first_idx += 1
    if first_idx >= len(lines):
        return text

    first_line_stripped = lines[first_idx].strip()
    if not first_line_stripped.startswith("```"):
        return text

    # Find the last non-empty line; if it closes a fence, strip it.
    last_idx = len(lines) - 1
    while last_idx > first_idx and lines[last_idx].strip() == "":
        last_idx -= 1
    if last_idx <= first_idx:
        return text

    last_line_stripped = lines[last_idx].strip()
    if last_line_stripped != "```":
        return text

    inner = lines[first_idx + 1 : last_idx]
    return "\n".join(inner)


def iter_balanced_json_objects(text: str) -> Iterator[str]:
    """Yield every top-level balanced ``{...}`` substring in ``text``.

    Walks ``text`` once, tracking JSON string state and escape state:

    * ``in_string`` toggles on an unescaped ``"``.
    * ``escape_next`` is set on ``\\`` inside a string and consumes the
      next character (handling ``\\"`` and ``\\\\``).
    * Only unescaped, non-string ``{`` / ``}`` count as structural.

    When a top-level opening brace is balanced by a matching close, the
    ``text[start : close + 1]`` slice is yielded and the outer pointer
    advances past it. When a span is unbalanced (no matching close),
    the outer pointer advances by one past the unmatched opening brace
    and scanning continues; no exception is raised.

    O(n) in ``len(text)``; never raises.
    """
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue

        depth = 0
        in_string = False
        escape_next = False
        start = i
        j = i
        balanced = False
        while j < n:
            c = text[j]
            if escape_next:
                escape_next = False
            elif in_string:
                if c == "\\":
                    escape_next = True
                elif c == '"':
                    in_string = False
                # else: ordinary string content, ignore
            else:
                if c == '"':
                    in_string = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        balanced = True
                        break
            j += 1

        if balanced:
            yield text[start : j + 1]
            i = j + 1
        else:
            # Unbalanced opening brace: advance past it and keep scanning.
            i = start + 1


def extract_validating_object(text: str, model: type[T]) -> Optional[T]:
    """Return the first balanced ``{...}`` object that validates as ``model``.

    Preprocessing: :func:`strip_markdown_fences` removes a surrounding
    code fence if present. Iteration: :func:`iter_balanced_json_objects`
    yields every top-level balanced object in order. For each candidate
    the helper attempts ``json.loads`` + ``model.model_validate``;
    :class:`json.JSONDecodeError` and :class:`pydantic.ValidationError`
    are silently caught so a non-matching candidate (e.g. a leading
    tool-use envelope) doesn't short-circuit the scan. The first
    successful validation wins; ``None`` is returned if every candidate
    fails.

    The helper never raises and is pure with respect to its arguments.
    """
    stripped = strip_markdown_fences(text)
    for candidate in iter_balanced_json_objects(stripped):
        try:
            payload = json.loads(candidate)
            return model.model_validate(payload)
        except (json.JSONDecodeError, ValidationError):
            continue
    return None
