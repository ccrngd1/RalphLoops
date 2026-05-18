"""Shared JSON extraction helpers for persona_review and orchestrator parsing.

This module consolidates the JSON-envelope parsing logic previously
duplicated in :mod:`ralph_loop.validator` (verdict parsing) and
:mod:`ralph_loop.orchestrator` (persona-selection decision parsing) per
the ``persona-review-verdict-parsing`` bugfix spec.

Public surface (three functions):

* :func:`strip_ansi_escapes` — removes ANSI CSI sequences
  (``\x1b[...letter``) and two-char escapes (``\x1b=``, ``\x1bD``, etc.)
  from ``text``. Claude Code CLI embeds color/style codes in its stdout even
  when a persona is asked to emit strict JSON (observed in real
  TechCodeReviewer output where italics markers landed inside the
  ``rationale`` string). Stripping them before extraction makes the
  parser robust to terminal-targeted output.
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


# Characters that terminate an ANSI CSI sequence (``\x1b[...<final>``).
# Any byte in 0x40-0x7E can be a final byte; we accept the printable
# range conservatively. See ECMA-48 §5.4.
_CSI_FINAL_BYTES = frozenset(chr(c) for c in range(0x40, 0x7F))


def strip_ansi_escapes(text: str) -> str:
    """Remove ANSI escape sequences from ``text``.

    Handles the two ESC-introducer sequence families that Claude Code CLI
    emits:

    * **CSI sequences** of the form ``ESC [ <params> <final>`` where
      ``<final>`` is a byte in the range ``0x40-0x7E`` (e.g. ``m`` for
      SGR color/style, ``l`` / ``h`` for private modes like ``?25l``).
    * **Two-character ESC sequences** of the form ``ESC <X>`` where
      ``<X>`` is a printable byte outside ``[`` (e.g. ``ESC =``,
      ``ESC D``). These are stripped as two characters.

    We deliberately do NOT strip the 8-bit CSI introducer ``\\x9b``:
    in practice Claude Code CLI uses the 7-bit ``ESC[`` form, and attempting
    to strip ``\\x9b`` can consume legitimate payload bytes when the
    byte occurs for an unrelated reason (e.g. inside Latin-1 / mojibake
    output). If a real 8-bit CSI stream shows up in production we
    revisit.

    Why: the reviewing persona is instructed to emit strict JSON, but
    the Claude Code CLI renders its output through a terminal-styling layer
    that injects color/style codes **inside** the JSON's string values
    (observed ``\\x1b[3mto\\x1b[23mdecimal_safe`` landing in a
    ``rationale`` string). Raw control characters are not valid inside
    JSON string literals per RFC 8259 §7, so ``json.loads`` rejects
    them with ``Invalid control character``. Pre-stripping all ANSI
    escapes eliminates the failure mode without changing the verdict's
    semantic content.

    O(n) in ``len(text)``; no regex; never raises.
    """
    if not text:
        return text
    # Fast path: no ESC -> nothing to strip. Avoids walking large
    # stdouts (e.g. tool-use output) one char at a time on the happy
    # path where the persona produced clean JSON.
    if "\x1b" not in text:
        return text

    out: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c == "\x1b":
            if i + 1 >= n:
                # Trailing stray ESC: drop it.
                i += 1
                continue
            nxt = text[i + 1]
            if nxt == "[":
                # CSI: ESC [ <params> <final>. Bail out if the
                # sequence isn't terminated by a final byte within a
                # reasonable lookahead, to avoid consuming unrelated
                # payload. 64 bytes is far more than any real CSI
                # sequence (longest legitimate: ~20 chars).
                j = i + 2
                limit = min(n, i + 64)
                while j < limit and text[j] not in _CSI_FINAL_BYTES:
                    j += 1
                if j < limit:
                    # Found a final byte, skip the whole sequence.
                    i = j + 1
                    continue
                # No final byte in range: treat the ESC as literal and
                # keep scanning. Preserves the original character to
                # avoid corrupting payloads that happen to contain
                # ``\\x1b`` for unrelated reasons.
                out.append(c)
                i += 1
                continue
            # Two-char ESC sequence (ESC <X>): drop both.
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


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
    """Yield every balanced ``{...}`` substring in ``text`` (nested included).

    Walks ``text``, tracking JSON string state and escape state:

    * ``in_string`` toggles on an unescaped ``"``.
    * ``escape_next`` is set on ``\\`` inside a string and consumes the
      next character (handling ``\\"`` and ``\\\\``).
    * Only unescaped, non-string ``{`` / ``}`` count as structural.

    When a top-level opening brace is balanced by a matching close, the
    ``text[start : close + 1]`` slice is yielded. After yielding, the
    scanner advances by **one character** past the opener rather than
    past the whole span; this guarantees that nested objects are also
    yielded, so a caller scanning for a verdict embedded inside prose
    that happens to contain outer braces (e.g.
    ``"{\\n{\\"verdict\\": \\"pass\\", ...}\\n}"``) still surfaces the
    inner verdict. Callers that only care about the first VALIDATING
    object (e.g. :func:`extract_validating_object`) short-circuit on
    the first Pydantic success, so the extra yields are cheap.

    When a span is unbalanced (no matching close), the outer pointer
    advances by one past the unmatched opening brace and scanning
    continues; no exception is raised.

    O(n²) worst case if every character is ``{`` followed by matching
    ``}``; O(n) on typical input where balanced spans are rare.
    Never raises.
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
            # Advance by ONE past the opener so we also visit nested
            # opening braces. Without this, an outer balanced span
            # (e.g. the whole of ``{ ...inner... }``) masks the inner
            # object from the scanner and the caller never sees it.
            i = start + 1
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
    # Strip ANSI escape sequences first. Claude Code CLI injects color / style
    # codes into the reviewing persona's stdout even when the persona
    # emits clean JSON; raw control characters inside a JSON string
    # literal are rejected by ``json.loads`` per RFC 8259 §7.
    stripped = strip_ansi_escapes(text)
    stripped = strip_markdown_fences(stripped)
    for candidate in iter_balanced_json_objects(stripped):
        try:
            payload = json.loads(candidate)
            return model.model_validate(payload)
        except (json.JSONDecodeError, ValidationError):
            continue
    return None
