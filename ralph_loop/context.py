"""Context Window composition (R5.3, R6.1-R6.7, R14.5, R18.5, R18.6).

The :func:`compose_context` function assembles the single prompt string
handed to Kiro CLI at the start of each iteration (R6.1). Sections are
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

from ralph_loop.models import ContextWindow, Persona, Task, TaskSpec
from ralph_loop.prompt_template import render_prompt

logger = logging.getLogger(__name__)

# Rough char->token ratio. The approximate token count on
# :class:`ContextWindow` is a budget estimate, not a precise count. The
# Ralph Loop does not have access to the target model's tokenizer at
# compose time, and the composed text is handed verbatim to Kiro CLI
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
# (R6.6). Content is deliberately short: Kiro CLI is already told to
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


def _approx_tokens(text: str) -> int:
    """Estimate token count for ``text`` using a flat char ratio."""
    return len(text) // _CHARS_PER_TOKEN


def inline_context_files(
    paths: list[str],
    *,
    base_dir: Optional[Path] = None,
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
    """
    parts: list[str] = []
    missing: list[str] = []
    for p in paths:
        candidate = Path(p) if base_dir is None else base_dir / p
        try:
            if candidate.is_file():
                contents = candidate.read_text(encoding="utf-8")
                parts.append(f"### File: {p}\n\n{contents}")
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

    Token overflow (R6.7): when the composed text's approximate token
    count exceeds ``max_tokens``, the project brief is truncated to its
    first ``_BRIEF_SUMMARY_CHARS`` characters plus an explicit
    truncation marker. The task spec, persona prompt, persona
    instructions, and escalation context are preserved untouched.
    """
    context_files = spec.context_files or []
    inlined, _missing = inline_context_files(context_files, base_dir=base_dir)
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
