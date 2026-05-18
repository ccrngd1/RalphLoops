"""Context Window composition (R5.3, R6.1-R6.7, R14.5, R18.5, R18.6).

The :func:`compose_context` function assembles the single prompt string
handed to Claude Code CLI at the start of each iteration (R6.1). Sections are
ordered as the design requires (R6.2-R6.6, R14.5, R5.3); referenced
context files (R18.5) are inlined into the Task Spec section and
missing references are logged as warnings without aborting composition
(R18.6). Token overflow triggers a minimal truncation: the project
brief is reduced to a short summary while the full task spec, persona
prompt, and persona instructions are preserved (R6.7).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import structlog

from ralph_loop.models import ContextWindow, Persona, Task, TaskSpec
from ralph_loop.prompt_template import render_prompt

logger = logging.getLogger(__name__)

# Rough char->token ratio. The approximate token count on
# :class:`ContextWindow` is a budget estimate, not a precise count. The
# Ralph Loop does not have access to the target model's tokenizer at
# compose time, and the composed text is handed verbatim to Claude Code CLI
# which performs its own tokenization downstream. The heuristic is kept
# deliberately simple so its behavior is predictable in tests and so the
# truncation rule (R6.7) fires on text that really is too large.
_CHARS_PER_TOKEN = 4

# Number of leading characters kept from the project brief when the
# context window overflows. The truncation rule in R6.7 says the brief
# is reduced to a summary section; concretely we retain the first
# ``_BRIEF_SUMMARY_CHARS`` characters (roughly the first paragraph or
# two) plus an explicit truncation notice, preserving the full task
# spec and persona prompt/instructions untouched.
_BRIEF_SUMMARY_CHARS = 500

# The static framing prepended to every composed context window
# (R6.6). Content is deliberately short: Claude Code CLI is already told to
# operate on a single task, so this string exists to remind the agent
# that it is inside a loop iteration rather than a general chat.
LOOP_FRAMING = (
    "# Ralph Loop Iteration\n\n"
    "You are working on a single task under a single persona. Focus on "
    "the task below, apply the persona's role, and report your results. "
    "Respect the persona's tool and resource restrictions.\n"
)

# Notice inlined when the executing task was resumed from an
# interruption (R14.5). The Context Composer adds this only when the
# caller sets ``resumed_notice=True``; the caller consults the Task's
# ``resumed_from_interruption`` flag set by the Resumer.
RESUMED_NOTICE = (
    "## Resumed from interruption\n\n"
    "This task was previously interrupted. Inspect the current state "
    "of the task's artifacts before proceeding.\n"
)

# Deterministic footer appended after the truncated prefix of an
# oversized Context_File (R2.5). The template is formatted with the
# file's original UTF-8 byte size and the configured
# ``max_context_file_bytes`` cap so the marker is self-describing both
# in the composed prompt and in downstream log analysis.
TRUNCATION_MARKER_TEMPLATE = (
    "[truncated: {original_bytes} bytes, showing first {cap} bytes]"
)


def _approx_tokens(text: str) -> int:
    """Estimate token count for ``text`` using a flat char ratio."""
    return len(text) // _CHARS_PER_TOKEN


def _truncate_to_codepoint_boundary(data: bytes, cap: int) -> bytes:
    """Return at most ``cap`` bytes of ``data`` trimmed to a whole codepoint.

    UTF-8 codepoints are 1 to 4 bytes wide. A raw ``data[:cap]`` slice
    may land in the middle of a multi-byte sequence; handing such a
    partial codepoint to ``str.decode(..., errors="strict")`` would
    raise. This helper rewinds the slice to the nearest complete
    codepoint boundary so the retained prefix always decodes cleanly
    (R2.8), while dropping at most 3 bytes so the retained length stays
    in ``[max(0, cap - 3), cap]`` when the input exceeds the cap
    (R2.4).

    Properties (enforced by Property 21):

    * ``0 <= len(retained) <= cap``.
    * When ``len(data) > cap``: ``len(retained) >= cap - 3``.
    * ``retained.decode("utf-8")`` succeeds under ``errors="strict"``.
    * Idempotent: applying the function to its own output returns the
      output unchanged.
    """
    # Fast path: nothing to trim when the input already fits under the
    # cap. Returning the original ``bytes`` object keeps the
    # idempotence predicate cheap and obvious.
    if len(data) <= cap:
        return data

    prefix = data[:cap]
    # UTF-8 continuation bytes match the bit pattern ``10xxxxxx`` (the
    # top two bits are ``10``). A complete codepoint starts with a
    # leader byte whose top two bits are one of:
    #   0xxxxxxx  (1-byte ASCII)
    #   110xxxxx  (start of a 2-byte sequence)
    #   1110xxxx  (start of a 3-byte sequence)
    #   11110xxx  (start of a 4-byte sequence)
    # If ``prefix`` ends on a continuation byte, walk back until we
    # reach either a leader byte or the start of the buffer. Since a
    # codepoint is at most 4 bytes, this rewinds at most 3 bytes.
    i = len(prefix)
    while i > 0 and (prefix[i - 1] & 0xC0) == 0x80:
        i -= 1

    # At this point ``i == 0`` or ``prefix[i - 1]`` is a non-
    # continuation byte. If the byte at ``i - 1`` starts a multi-byte
    # sequence, decide whether that codepoint is complete inside
    # ``prefix``. If incomplete (fewer continuation bytes than the
    # leader expects), drop the partial codepoint. If complete,
    # re-include the full codepoint so the retained prefix ends on a
    # real codepoint boundary rather than on the leader alone.
    if i > 0:
        lead = prefix[i - 1]
        if lead & 0x80:  # top bit set, so this is a multi-byte leader
            if (lead & 0xE0) == 0xC0:
                expected_len: Optional[int] = 2
            elif (lead & 0xF0) == 0xE0:
                expected_len = 3
            elif (lead & 0xF8) == 0xF0:
                expected_len = 4
            else:
                # Malformed lead (e.g. 0b11111xxx or a stray
                # continuation-style high byte that the walk above
                # could not consume). Drop it unconditionally so the
                # retained prefix never contains an invalid leader.
                expected_len = None
            if expected_len is None:
                i -= 1
            else:
                actual_len = len(prefix) - (i - 1)
                if actual_len < expected_len:
                    # Partial codepoint at the tail: drop the leader.
                    i -= 1
                else:
                    # Complete codepoint: include every continuation
                    # byte the leader requires. For well-formed
                    # UTF-8 this equals ``len(prefix)``; for any
                    # malformed tail we clamp at ``expected_len`` so
                    # extra stray continuations are dropped.
                    i = (i - 1) + expected_len

    return prefix[:i]


def _inline_one_context_file(
    rel_path: str,
    abs_path: Path,
    *,
    max_file_bytes: int,
) -> str:
    """Return the Markdown block for one Context_File, truncating if needed.

    Reads ``abs_path`` as raw bytes so the byte-cap comparison matches
    the way Claude Code CLI measures its chunk limit (R2.4). Files at or under
    the cap are returned verbatim as ``"### File: <rel_path>\\n\\n<body>"``
    with no marker (R2.3). Files over the cap are head-truncated via
    :func:`_truncate_to_codepoint_boundary` to keep the retained prefix
    well-formed UTF-8 (R2.8), and a deterministic Truncation_Marker is
    appended on its own line so it is visibly distinct in the composed
    prompt (R2.5, R2.7). Each truncation emits a WARNING-level
    structured log record (Truncation_Event, R2.6) carrying the path,
    the original byte size, the retained byte size, and the configured
    cap so operators can correlate large-file truncations with
    downstream iteration outcomes.

    Decoding uses ``errors="replace"`` to match the behaviour of the
    existing :func:`inline_context_files` callers: a file that already
    contains malformed UTF-8 still renders (with replacement
    characters) rather than raising.
    """
    data = abs_path.read_bytes()
    original = len(data)
    if original <= max_file_bytes:
        body = data.decode("utf-8", errors="replace")
        return f"### File: {rel_path}\n\n{body}"

    trimmed = _truncate_to_codepoint_boundary(data, max_file_bytes)
    retained = len(trimmed)
    body = trimmed.decode("utf-8", errors="replace")
    marker = TRUNCATION_MARKER_TEMPLATE.format(
        original_bytes=original, cap=max_file_bytes
    )
    # Emit the Truncation_Event so operators can correlate large-file
    # truncations with subsequent validation/invocation outcomes
    # (R2.6, R2.9).
    structlog.get_logger().warning(
        "context_file_truncated",
        path=rel_path,
        original_bytes=original,
        retained_bytes=retained,
        cap_bytes=max_file_bytes,
    )
    # Marker sits on its own line after the body so it is visibly
    # distinct in the composed prompt (R2.7).
    return f"### File: {rel_path}\n\n{body}\n{marker}"


def inline_context_files(
    paths: list[str],
    *,
    base_dir: Optional[Path] = None,
    max_file_bytes: int = 65536,
) -> tuple[str, list[str]]:
    """Inline the contents of every path in ``paths`` (R18.5, R18.6).

    Returns a ``(inlined_text, missing_paths)`` tuple. Each existing
    file is emitted as a fenced-style ``### File: <path>`` block so the
    downstream persona can see which file each chunk came from. Missing
    or unreadable paths produce a warning log entry and are appended to
    ``missing_paths``; this function never raises for a missing
    reference (R18.6).

    ``base_dir`` scopes relative paths to the project root when the
    caller has one. When ``None``, paths resolve against the process
    working directory.

    ``max_file_bytes`` bounds the UTF-8 byte size of any single inlined
    file (R2.3, R2.4). Files at or under the cap are emitted verbatim;
    files over the cap are head-truncated on a UTF-8 codepoint boundary
    with a deterministic Truncation_Marker appended (R2.5, R2.7) and a
    WARNING-level Truncation_Event logged (R2.6). The default of
    65536 bytes matches :attr:`Config.max_context_file_bytes` so callers
    that do not pass a cap get the same behaviour as the loop.
    """
    parts: list[str] = []
    missing: list[str] = []
    for p in paths:
        candidate = Path(p) if base_dir is None else base_dir / p
        try:
            if candidate.is_file():
                parts.append(
                    _inline_one_context_file(
                        p, candidate, max_file_bytes=max_file_bytes
                    )
                )
            else:
                logger.warning("Context file not found: %s", p)
                missing.append(p)
        except OSError as e:
            logger.warning("Failed to read context file %s: %s", p, e)
            missing.append(p)
    return ("\n\n".join(parts), missing)


def _render_task_spec_section(spec: TaskSpec, inlined: str) -> str:
    """Render the spec body (+ inlined context files) as Markdown.

    The Task Spec lives in the composed prompt as Markdown so the agent
    sees a clear section hierarchy (R6.3). Each body field is emitted
    as a ``## <Heading>`` block; ``notes`` is omitted when absent. The
    inlined context files (R18.5) land in a dedicated trailing section
    so the agent can scroll past them when the spec itself is
    self-contained.
    """
    sections = [
        f"## Objective\n{spec.body.objective}",
        f"## Context References\n{spec.body.context_references}",
        f"## Instructions\n{spec.body.instructions}",
    ]
    if spec.body.notes:
        sections.append(f"## Notes\n{spec.body.notes}")
    if inlined:
        sections.append(f"## Inlined Context Files\n\n{inlined}")
    return "\n\n".join(sections)


def compose_context(
    *,
    task: Task,
    spec: TaskSpec,
    persona: Persona,
    brief: str,
    escalation_context: Optional[str] = None,
    resumed_notice: bool = False,
    max_tokens: int = 32_000,
    max_file_bytes: int = 65536,
    base_dir: Optional[Path] = None,
) -> ContextWindow:
    """Assemble the Context Window for a single iteration.

    Section order (R6.1-R6.6, R14.5, R5.3):

    1. Loop framing (R6.6).
    2. Resumed-from-interruption notice when applicable (R14.5).
    3. Project brief (R6.2).
    4. Task spec with referenced context files inlined (R6.3, R18.5).
    5. Persona prompt template, rendered with placeholders (R6.4).
    6. Persona instructions (R6.5), when present.
    7. Escalation context (R5.3), when present.

    ``max_file_bytes`` bounds the UTF-8 byte size of any single inlined
    Context_File (R2.3, R2.4). The cap is forwarded to
    :func:`inline_context_files`, which head-truncates over-cap files on
    a codepoint boundary and appends a deterministic Truncation_Marker
    (R2.5, R2.7). Per-file truncation runs before assembly and is
    independent of the whole-window token-budget fallback below
    (R2.10); the default of 65536 bytes matches
    :attr:`Config.max_context_file_bytes` so callers that omit the cap
    get the same behaviour as the loop.

    Token overflow (R6.7): when the composed text's approximate token
    count exceeds ``max_tokens``, the project brief is truncated to its
    first ``_BRIEF_SUMMARY_CHARS`` characters plus an explicit
    truncation marker. The task spec, persona prompt, persona
    instructions, and escalation context are preserved untouched.
    """
    context_files = spec.context_files or []
    inlined, _missing = inline_context_files(
        context_files,
        base_dir=base_dir,
        max_file_bytes=max_file_bytes,
    )
    task_spec_text = _render_task_spec_section(spec, inlined)

    rendered_prompt = render_prompt(
        persona.prompt_template,
        project_brief=brief,
        task_spec=task_spec_text,
        task_id=task.id,
        task_title=task.title,
        persona_name=persona.name,
    )

    def _assemble(brief_text: str) -> str:
        """Concatenate the seven sections in R6 order."""
        parts: list[str] = [LOOP_FRAMING]
        if resumed_notice:
            parts.append(RESUMED_NOTICE)
        parts.append(f"# Project Brief\n\n{brief_text}\n")
        parts.append(
            f"# Task Spec (id={task.id}, title={task.title})\n\n"
            f"{task_spec_text}\n"
        )
        parts.append(f"# Persona: {persona.name}\n\n{rendered_prompt}\n")
        if persona.instructions:
            parts.append(
                f"## Persona Instructions\n\n{persona.instructions}\n"
            )
        if escalation_context:
            parts.append(
                f"# Escalation Context\n\n{escalation_context}\n"
            )
        return "\n".join(parts)

    full = _assemble(brief)
    approx = _approx_tokens(full)
    if approx <= max_tokens:
        return ContextWindow(
            text=full, approx_tokens=approx, truncated=False
        )

    # Overflow: replace the brief with a summary and re-assemble.
    # Truncating only the brief preserves the full task spec and
    # persona prompt/instructions as required by R6.7.
    if len(brief) > _BRIEF_SUMMARY_CHARS:
        summary = (
            brief[:_BRIEF_SUMMARY_CHARS]
            + "\n\n[... project brief truncated ...]"
        )
    else:
        summary = brief
    truncated_text = _assemble(summary)
    truncated_approx = _approx_tokens(truncated_text)
    return ContextWindow(
        text=truncated_text,
        approx_tokens=truncated_approx,
        truncated=True,
    )
