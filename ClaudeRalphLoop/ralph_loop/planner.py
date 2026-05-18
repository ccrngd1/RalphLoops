"""Planner bootstrap: generate the initial Task_List from the Project_Brief.

The :class:`Planner` is invoked either explicitly via the ``init-tasks``
subcommand (R17.2) or automatically at run start when ``tasks.json`` is
empty and the ``automatic_planner`` flag is enabled (R17.3). In both
modes the Planner:

1. Resolves the configured ``planner_persona`` from the registry
   (fail-fast if missing: R17.7).
2. Takes a pre-snapshot of ``tasks.json`` (always ``[]`` -- the Planner
   only runs when the Task_List is empty, per R17.3 and R17.2 wording).
3. Invokes the planner persona in a dedicated Claude Code CLI session whose
   Context_Window carries the Project_Brief + a planner-specific
   instruction to write new Task entries to ``tasks.json`` (R17.2,
   R17.5).
4. Reads the post-snapshot of ``tasks.json`` back and funnels it through
   :class:`TaskCreationProcessor.process` with ``pre_snapshot=[]`` so
   planner output goes through the identical schema / persona /
   creation-chain / budget pipeline as in-iteration task creation
   (R17.6, R8.4, R8.7, R8.12).

The module also exposes two pure branching helpers,
:func:`should_auto_planner` and :func:`should_exit_empty_no_auto`, that
encode the R17.3 / R17.4 / R17.7 decision table so the main loop and
Property 26 can share the exact same logic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal, Optional

from ralph_loop.claude_code import ClaudeCodeInvoker
from ralph_loop.models import Config, Task, TaskCreationResult
from ralph_loop.persona_registry import PersonaRegistry
from ralph_loop.task_creation import TaskCreationProcessor

logger = logging.getLogger(__name__)


# Sentinel task id used for planner-created entries. The processor
# records this id in log messages; it is never written to the Task_List
# because the planner only produces ``created`` entries (``pre`` is
# empty, so the executing-task revert branch is inert).
_PLANNER_TASK_ID = "__planner__"


class PlannerError(Exception):
    """Raised when the Planner cannot run (fatal startup failure).

    Corresponds to R17.7: the planner persona is missing from the
    configuration or from the registry, so neither ``init-tasks`` nor
    an auto-planner startup can proceed. The caller maps this to a
    non-zero exit code.
    """


def should_auto_planner(tasks: list[Task], config: Config) -> bool:
    """Return ``True`` iff auto-planner should run (R17.3).

    Auto-planner fires when ``tasks.json`` is empty *and*
    ``automatic_planner`` is enabled. A non-empty task list always
    skips planner invocation regardless of the flag.
    """

    return len(tasks) == 0 and bool(config.automatic_planner)


def should_exit_empty_no_auto(tasks: list[Task], config: Config) -> bool:
    """Return ``True`` iff the loop should exit for "empty + no auto" (R17.4).

    When ``tasks.json`` is empty and ``automatic_planner`` is disabled,
    the loop logs an informational message directing the operator to
    run ``init-tasks`` and exits non-zero without selecting a task.
    """

    return len(tasks) == 0 and not bool(config.automatic_planner)


class Planner:
    """Wire the planner persona's Claude Code CLI session to the task-creation pipeline."""

    def __init__(
        self,
        *,
        invoker: ClaudeCodeInvoker,
        registry: PersonaRegistry,
        config: Config,
        processor: TaskCreationProcessor,
        tasks_path: Path,
        log_path: Path,
        model_id: Optional[str] = None,
    ) -> None:
        self._invoker = invoker
        self._registry = registry
        self._config = config
        self._processor = processor
        self._tasks_path = Path(tasks_path)
        self._log_path = Path(log_path)
        self._model_id = model_id

    async def bootstrap(
        self,
        *,
        reason: Literal["init-tasks", "auto"],
        brief: str,
    ) -> TaskCreationResult:
        """Invoke the planner persona and funnel its output through the processor.

        Args:
            reason: Why the planner is running -- ``"init-tasks"`` when
                invoked by the subcommand (R17.2), ``"auto"`` when
                triggered by the empty-task-list + auto flag branch
                (R17.3). Used in log messages so operators can tell
                the two cases apart.
            brief: The contents of the Project_Brief that the planner
                persona should read (R17.2).

        Raises:
            PlannerError: When ``planner_persona`` is not configured
                or the configured name is absent from the registry
                (R17.7). The main loop turns this into a non-zero
                exit.

        Returns:
            The :class:`TaskCreationResult` produced by
            :meth:`TaskCreationProcessor.process` over the planner's
            post-snapshot. Callers read ``accepted``, ``rejected``,
            and ``spilled`` to log the counts per R17.8.
        """

        planner_name = self._config.planner_persona
        if not planner_name:
            logger.error(
                "planner: no planner_persona configured (reason=%s)", reason,
            )
            raise PlannerError(
                "No planner_persona configured; set Config.planner_persona "
                "or run `ralph init-tasks` only after configuring one."
            )

        planner = self._registry.get(planner_name)
        if planner is None:
            logger.error(
                "planner: planner_persona %r not found in registry (reason=%s)",
                planner_name, reason,
            )
            raise PlannerError(
                f"planner_persona {planner_name!r} not present in the "
                "persona registry"
            )

        logger.info(
            "planner: invoking persona=%r reason=%s tasks_path=%s",
            planner_name, reason, self._tasks_path,
        )

        prompt = _build_planner_prompt(
            planner_name=planner_name,
            brief=brief,
            tasks_path=self._tasks_path,
            specs_dir=Path(self._config.specs_dir),
            available_personas=self._registry.describe_all_for_orchestrator(),
        )

        # Run the Claude Code CLI session under the reserved ``planner`` call
        # kind so Token_Usage is attributed to the planner bucket in
        # RunTokenTotals (R12.1, R17.8).  Pass cwd so the agent's file
        # tools resolve paths relative to the project root.
        await self._invoker.invoke(
            context=prompt,
            log_path=self._log_path,
            call_kind="planner",
            cwd=self._tasks_path.parent,
            model_id=self._model_id,
        )

        # Read the post-snapshot. A missing or malformed file is not
        # fatal: we funnel whatever we see through the processor, which
        # will reject invalid entries via the standard pipeline (R17.6).
        post = _read_tasks_json(self._tasks_path)

        result = self._processor.process(
            pre_snapshot=[],
            post_snapshot=post,
            executing_task_id=_PLANNER_TASK_ID,
            acting_persona=planner_name,
            iteration=0,
        )

        if not result.accepted and not result.rejected:
            # R17.4 describes the "empty Task_List + no auto" branch, but
            # we can also land here if the planner persona produced no
            # output at all. Surface that case explicitly so the
            # operator can retry or inspect the planner prompt.
            logger.error(
                "planner: produced zero tasks (reason=%s, persona=%s)",
                reason, planner_name,
            )

        logger.info(
            "planner: completed reason=%s accepted=%d rejected=%d spilled=%d",
            reason,
            len(result.accepted),
            len(result.rejected),
            len(result.spilled),
        )

        return result


def _build_planner_prompt(
    *,
    planner_name: str,
    brief: str,
    tasks_path: Path,
    specs_dir: Path,
    available_personas: list,
) -> str:
    """Compose the Context_Window for the planner invocation (R17.2, R17.5).

    The prompt explicitly calls out the Task schema fields the persona
    must supply so planner output is more likely to pass the
    downstream schema validation (R17.6), and it strongly prefers
    richer specs (objective + instructions prose, plus a
    ``persona_review`` check alongside ``file_exists``) so each task's
    pass condition exercises content quality rather than just "file is
    on disk".
    """

    # Render the available personas so the planner can pick concrete
    # names for ``target_persona`` and ``persona_review.persona``
    # instead of hallucinating roles that don't exist. The list comes
    # from the same ``PersonaDescription`` projection the Orchestrator
    # sees during LLM-based selection (R3.8, R4.2).
    if available_personas:
        persona_block = "\n".join(
            f"- {d.name}: {d.description}" for d in available_personas
        )
    else:
        persona_block = "(no personas loaded - register at least one)"

    return (
        f"You are the Planner persona {planner_name!r}.\n"
        "Read the Project_Brief below, break the work into tasks, then:\n"
        f"1. Create a spec file for each task under `{specs_dir}/`\n"
        f"2. Write the complete task list JSON array to `{tasks_path}`\n\n"
        "CRITICAL RULES:\n"
        "- You MUST write all files directly using your file create tool. "
        "Do NOT generate helper scripts, code snippets, or instructions.\n"
        "- Write the entire tasks.json in one create operation. There is "
        "no size limit - large files are fine.\n"
        "- Do NOT split writes into multiple operations or chunks.\n\n"
        "## Available personas\n\n"
        f"{persona_block}\n\n"
        "When a task produces reviewable content, route it to the best-"
        "matching persona via `target_persona` AND add a `persona_review` "
        "validation check that names a DIFFERENT persona as the reviewer.\n\n"
        "## Spec file format\n\n"
        "Each spec file is Markdown with YAML frontmatter. Place them at "
        f"`{specs_dir}/<task-id>.md`. Every spec MUST include a filled-in "
        "Objective, Context References, Instructions, and Notes section. "
        "Vague one-liners produce vague drafts; write concrete, "
        "actionable instructions that reference specific files, sections, "
        "or acceptance criteria.\n\n"
        "Every content-producing task MUST declare BOTH checks:\n"
        "1. `file_exists` - the output file is on disk (cheap gate).\n"
        "2. `persona_review` - a reviewer persona explicitly signs off on "
        "content quality. This is the real pass condition; without it a "
        "task passes as soon as an empty file appears.\n\n"
        "Example spec file:\n\n"
        "```markdown\n"
        "---\n"
        "id: ch02-r01-draft\n"
        "title: Draft recipe 2.1 Patient Message Response\n"
        "target_persona: Writer\n"
        "tags: [chapter02, recipe, draft]\n"
        "depends_on: [ch02-preface]\n"
        "validation:\n"
        "  - type: file_exists\n"
        "    name: output-file-exists\n"
        "    paths: [chapters/ch02/recipe-01.md]\n"
        "  - type: persona_review\n"
        "    name: editorial-review\n"
        "    persona: Reviewer\n"
        "    pass_condition: >-\n"
        "      Recipe includes scenario, prompt, expected output, and at\n"
        "      least one worked example. Prose matches the project voice.\n"
        "---\n\n"
        "## Objective\n"
        "Write recipe 2.1 covering how a clinician can draft a patient\n"
        "message reply using an LLM. The recipe belongs in chapter 2.\n\n"
        "## Context References\n"
        "- SUMMARY.md - project goals and audience\n"
        "- chapters/ch02/preface.md - chapter framing\n\n"
        "## Instructions\n"
        "1. Open `chapters/ch02/preface.md` to anchor on the chapter's\n"
        "   framing.\n"
        "2. Draft a 400-600 word recipe with these sections in order:\n"
        "   scenario, prompt, expected output, worked example, caveats.\n"
        "3. Save the draft to `chapters/ch02/recipe-01.md`.\n\n"
        "## Notes\n"
        "Defer to Writer voice and Style_Guide.md for tone.\n"
        "```\n\n"
        "Required frontmatter: id, title, validation (at least one entry).\n"
        "Validation types: file_exists (paths), shell (commands), "
        "persona_review (persona, pass_condition).\n"
        "A persona_review check's `persona` must appear in the Available "
        "personas list above, and must NOT equal the `target_persona` "
        "on the same spec (self-review is rejected at runtime).\n\n"
        "## tasks.json format\n\n"
        "Each task MUST supply: id (unique string matching spec id), title, "
        "priority (integer; lower runs first), status ('pending'), "
        "spec_path (relative path to the spec file, e.g. "
        f"`{specs_dir}/<task-id>.md`), retry_count (0).\n"
        "Optional fields: target_persona, depends_on, tags.\n\n"
        f"## Project_Brief\n\n{brief}\n"
    )


def _read_tasks_json(path: Path) -> list[dict[str, Any]]:
    """Read ``tasks.json`` as a raw dict list.

    Returns ``[]`` for missing, empty, non-JSON, or non-list contents.
    The downstream processor's schema validation is the single source
    of truth for entry-level rejection (R17.6).
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "planner: failed to read %s after planner run: %s", path, exc,
        )
        return []

    if not raw.strip():
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "planner: %s is not valid JSON after planner run: %s", path, exc,
        )
        return []

    if not isinstance(data, list):
        logger.warning(
            "planner: %s top-level value is %s, expected list",
            path, type(data).__name__,
        )
        return []

    # Filter to dict-only entries; the snapshot diff helper and the
    # Task Creation Processor both expect ``list[dict]``.
    return [entry for entry in data if isinstance(entry, dict)]
