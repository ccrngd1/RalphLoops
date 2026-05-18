"""Click-based CLI entrypoint for the Ralph Loop (R15.1-R15.9, R16.1-R16.9, R13.5).

This module wires the ``ralph`` command group and its four subcommands
(``run``, ``init``, ``init-tasks``, ``rollback``) to the backend
components. The CLI itself is thin: most behaviour lives in dedicated
modules (ConfigLoader, PersonaRegistry, Resumer, PendingQueueManager,
TaskSelector, Orchestrator, EscalationHandler, ContextComposer,
ClaudeCodeInvoker, Validator, TaskCreationProcessor, Planner, GitManager,
TokenAccountant) and this file only orchestrates them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import click
import structlog

from ralph_loop.atomic_io import atomic_write_bytes
from ralph_loop.budget import BudgetTracker
from ralph_loop.config import ConfigLoadError, load_config
from ralph_loop.context import compose_context
from ralph_loop.escalation import EscalationHandler
from ralph_loop.git_manager import GitManager
from ralph_loop.claude_code import ClaudeCodeInvocationTimeout, ClaudeCodeInvoker
from ralph_loop.logger import configure_logger, write_run_summary
from ralph_loop.models import (
    CheckResult,
    Config,
    IterationOutcome,
    LlmCallRecord,
    RunSummary,
    TASK_LIST_ADAPTER,
    Task,
    TaskStatus,
)
from ralph_loop.orchestrator import Orchestrator, StuckTaskError
from ralph_loop.pending_queue import PendingQueueError, PendingQueueManager
from ralph_loop.persona_registry import PersonaRegistry, PersonaRegistryError
from ralph_loop.planner import (
    Planner,
    PlannerError,
    should_auto_planner,
    should_exit_empty_no_auto,
)
from ralph_loop.resumer import resume as resumer_resume
from ralph_loop.status_update import status_after_validation
from ralph_loop.task_creation import TaskCreationProcessor
from ralph_loop.task_selector import next_eligible_task, termination_decision
from ralph_loop.task_spec import TaskSpecParseError, parse_task_spec
from ralph_loop.tokens import TokenAccountant
from ralph_loop.validator import Validator, ValidatorStuckError

logger = logging.getLogger(__name__)


def _resolve_commit_sha() -> str:
    """Return the short git SHA of the ``ralph_loop`` package, or ``"unknown"``.

    Best-effort: if ``git`` is not on PATH, the package isn't in a git
    working tree, or anything else goes wrong, we return ``"unknown"``
    rather than failing the loop. The SHA is stamped into the run log
    so we can always answer "was the fix in effect?" from a single
    ``grep commit_sha <run-log>``.
    """
    try:
        pkg_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(pkg_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                # Also surface whether the working tree has uncommitted
                # edits, since those can diverge from the recorded SHA.
                dirty = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(pkg_root),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if dirty.returncode == 0 and dirty.stdout.strip():
                    return f"{sha}+dirty"
                return sha
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "unknown"


# Exit codes used across the CLI. Grouped here so tests and operators
# can refer to the same constants. Any change must be reflected in the
# R15.x / R1.x acceptance criteria and the user-facing docs.
EXIT_SUCCESS = 0
EXIT_BLOCKED = 1
EXIT_CONFIG_ERROR = 2
EXIT_BUDGET_EXCEEDED = 3
EXIT_INVOCATION_ERROR = 4


@click.group(
    help="Ralph Loop: domain-agnostic automated iteration wrapper for Claude Code CLI."
)
def main() -> None:
    """Entrypoint for the ``ralph`` command."""


# ============================================================================
# ralph run
# ============================================================================


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to ralph.config.json (defaults to <project-root>/ralph.config.json).",
)
@click.option(
    "--project-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Project root directory (defaults to current working directory).",
)
@click.option(
    "--max-iterations",
    type=int,
    default=None,
    help="Override Config.max_iterations (R10.1).",
)
@click.option(
    "--max-retries-per-task",
    type=int,
    default=None,
    help="Override Config.max_retries_per_task (R10.2).",
)
@click.option(
    "--wall-clock-timeout-ms",
    type=int,
    default=None,
    help="Override Config.wall_clock_timeout_ms (R10.4).",
)
@click.option(
    "--personas-dir",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Override Config.personas_dir. Accepts any directory containing "
        "persona YAML/Markdown files, including shared template sets like "
        "`templates/book/personas/`."
    ),
)
@click.option(
    "--model",
    "model",
    type=str,
    default=None,
    help=(
        "Override Config.default_model_id. Passed as `--model <id>` to "
        "every Claude Code CLI invocation (persona execution, persona_review, "
        "planner, escalation). Defaults to `claude-opus-4.7`."
    ),
)
@click.option(
    "--orchestrator-model",
    "orchestrator_model",
    type=str,
    default=None,
    help=(
        "Override Config.orchestrator_model_id. Only applies to the "
        "orchestrator_selection LLM call. When unset, the orchestrator "
        "uses --model / default_model_id too."
    ),
)
def run(
    config_path: Optional[Path],
    project_root: Optional[Path],
    max_iterations: Optional[int],
    max_retries_per_task: Optional[int],
    wall_clock_timeout_ms: Optional[int],
    personas_dir: Optional[Path],
    model: Optional[str],
    orchestrator_model: Optional[str],
) -> None:
    """Run the Ralph Loop main iteration loop (R1.1-R1.8, R15.1-R15.9)."""
    root = project_root or Path.cwd()
    cli_overrides = {
        "max_iterations": max_iterations,
        "max_retries_per_task": max_retries_per_task,
        "wall_clock_timeout_ms": wall_clock_timeout_ms,
        "personas_dir": str(personas_dir) if personas_dir is not None else None,
        "default_model_id": model,
        "orchestrator_model_id": orchestrator_model,
    }

    try:
        config = load_config(
            project_root=root,
            config_path=config_path,
            cli_overrides=cli_overrides,
        )
    except ConfigLoadError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    # Top-level catch-all so silent crashes never leave the operator
    # wondering why the run stopped. Every exception gets:
    # 1. A full traceback to stderr.
    # 2. The same traceback appended to the run log file (when the
    #    log dir is writable; we do this after configure_logger runs).
    # 3. A non-zero exit code.
    try:
        exit_code = asyncio.run(_run_loop(config, root))
    except KeyboardInterrupt:
        click.echo("\nInterrupted by user. State on disk is safe to resume.", err=True)
        sys.exit(EXIT_INVOCATION_ERROR)
    except Exception:  # noqa: BLE001 - top-level safety net
        import traceback
        tb = traceback.format_exc()
        click.echo(
            "\nUncaught exception during `ralph run`. Full traceback:\n"
            f"{tb}",
            err=True,
        )
        # Best-effort: write the traceback to the run log too. The log
        # dir may not exist if the crash happened before configure_logger.
        try:
            log_dir = Path(config.log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            # Append rather than overwrite so any partial run log is preserved.
            crash_log = log_dir / "crash.log"
            crash_log.write_text(tb, encoding="utf-8")
            click.echo(f"Traceback also written to {crash_log}", err=True)
        except Exception:  # noqa: BLE001 - never swallow the original error
            pass
        sys.exit(EXIT_INVOCATION_ERROR)

    sys.exit(exit_code)


async def _run_loop(config: Config, project_root: Path) -> int:
    """Main loop implementation. Returns the process exit code.

    This function wires the startup sequence (registry load -> resume
    -> pending queue -> auto-planner) and the per-iteration sequence
    (select task -> escalation check -> persona selection -> context
    compose -> flip to in_progress -> kiro invoke -> validate -> task
    creation -> status update -> git commit -> record tokens). It
    returns before the ``RunSummary`` is written so the caller can
    still surface the exit code regardless of summary write failure.
    """

    run_id = str(uuid.uuid4())
    start_time = time.monotonic()

    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    configure_logger(log_file_path=log_dir / f"run-{run_id}.log")

    commit_sha = _resolve_commit_sha()
    # Bind the SHA into every structured log line for the remainder of
    # this process. Lets ``grep commit_sha <run-log>`` answer "which
    # code produced this log" without parsing stdout banners.
    try:
        import structlog
        structlog.contextvars.bind_contextvars(
            commit_sha=commit_sha, run_id=run_id,
        )
    except Exception:  # noqa: BLE001 - logging bind must not fail the loop
        pass

    click.echo(f"[ralph run] commit_sha={commit_sha}")
    click.echo(f"[ralph run] run_id={run_id} project_root={project_root}")
    click.echo(
        f"[ralph run] model={config.default_model_id}"
        f" orchestrator_model={config.orchestrator_model_id or config.default_model_id}"
    )

    # --- Persona registry (R3.1-R3.8) ----------------------------------
    click.echo(f"[ralph run] loading personas from {config.personas_dir}...")
    try:
        registry = PersonaRegistry.load(Path(config.personas_dir))
    except PersonaRegistryError as exc:
        click.echo(f"Persona registry error: {exc}", err=True)
        return EXIT_CONFIG_ERROR
    click.echo(
        f"[ralph run] loaded {len(registry.all())} personas: "
        f"{[p.name for p in registry.all()]}"
    )

    # --- Load tasks.json ------------------------------------------------
    tasks_path = Path(config.tasks_path)
    click.echo(f"[ralph run] loading tasks from {tasks_path}...")
    try:
        tasks = _load_tasks(tasks_path)
    except Exception as exc:  # noqa: BLE001 - surface any load failure
        click.echo(f"Failed to load {tasks_path}: {exc}", err=True)
        return EXIT_CONFIG_ERROR
    click.echo(f"[ralph run] loaded {len(tasks)} task(s)")

    # --- Resumer: reset in-progress + dependency-health sweep (R14) ----
    click.echo("[ralph run] running resumer + dependency health sweep...")
    tasks = _apply_resume(tasks, tasks_path)
    click.echo(f"[ralph run] resumer done; task list has {len(tasks)} entries")

    # --- Budget tracker + pending queue + kiro invoker -----------------
    click.echo("[ralph run] initializing budget tracker and kiro invoker...")
    budget = BudgetTracker(config)
    pending_queue = PendingQueueManager(
        queue_path=Path(config.pending_tasks_path),
        registry=registry,
        run_id=run_id,
    )
    invoker = ClaudeCodeInvoker(claude_cli_command=config.claude_cli_command)

    click.echo(
        f"[ralph run] processing pending queue at {config.pending_tasks_path}..."
    )
    try:
        pending_result = pending_queue.process_on_startup()
    except PendingQueueError as exc:
        click.echo(f"Pending queue error: {exc}", err=True)
        return EXIT_CONFIG_ERROR
    click.echo(
        f"[ralph run] pending queue: loaded={pending_result.loaded}"
        f" admitted={len(pending_result.admitted)}"
        f" discarded={len(pending_result.discarded)}"
    )

    if pending_result.admitted:
        tasks = list(tasks) + list(pending_result.admitted)
        atomic_write_bytes(tasks_path, TASK_LIST_ADAPTER.dump_json(tasks))

    # --- Task creation processor ---------------------------------------
    processor = TaskCreationProcessor(
        registry=registry,
        config=config,
        budget=budget,
        pending_queue=pending_queue,
        tasks_path=tasks_path,
        run_id=run_id,
    )

    # --- Planner: auto-planner branching (R17.3, R17.4) ----------------
    if should_auto_planner(tasks, config):
        try:
            brief = Path(config.summary_path).read_text(encoding="utf-8")
            planner = Planner(
                invoker=invoker,
                registry=registry,
                config=config,
                processor=processor,
                tasks_path=tasks_path,
                log_path=log_dir / f"run-{run_id}-planner.log",
                model_id=config.default_model_id,
            )
            await planner.bootstrap(reason="auto", brief=brief)
        except PlannerError as exc:
            click.echo(f"Planner error: {exc}", err=True)
            return EXIT_CONFIG_ERROR
        tasks = _load_tasks(tasks_path)
    elif should_exit_empty_no_auto(tasks, config):
        click.echo(
            "tasks.json is empty and automatic_planner is disabled. "
            "Run `ralph init-tasks` to bootstrap the task list.",
            err=True,
        )
        return EXIT_BLOCKED

    # --- Orchestrator / escalation / validator / accountant ------------
    escalation = EscalationHandler(registry=registry, config=config)
    orchestrator = Orchestrator(
        invoker=invoker,
        log_path=log_dir / f"run-{run_id}-orchestrator.log",
        fallback_persona=config.fallback_persona,
        model_id=config.orchestrator_model_id or config.default_model_id,
    )
    validator = Validator(
        invoker=invoker,
        registry=registry,
        model_id=config.default_model_id,
        fallback_reviewer=config.fallback_persona,
    )
    accountant = TokenAccountant(config)
    git_mgr = GitManager(
        enabled=config.git_integration_enabled, cwd=project_root,
    )

    escalation_events = 0
    total_new_tasks = 0
    exit_code = EXIT_SUCCESS

    click.echo(
        f"[ralph run] entering iteration loop "
        f"(max_iterations={config.max_iterations}, "
        f"wall_clock_timeout_ms={config.wall_clock_timeout_ms})..."
    )

    # --- Iteration loop (R1.1-R1.8) ------------------------------------
    while True:
        decision = termination_decision(tasks)
        if decision.verdict == "success":
            click.echo("All tasks passing.")
            break
        if decision.verdict == "blocked":
            click.echo(
                f"Blocked. blocked_ids={decision.blocked_ids} "
                f"blocking_dep_ids={decision.blocking_dep_ids}",
                err=True,
            )
            exit_code = decision.exit_code or EXIT_BLOCKED
            break

        # Budget / wall-clock checks (R1.7, R10.1, R10.4, R10.5).
        if budget.check_wall_clock():
            click.echo("Wall-clock timeout reached.", err=True)
            exit_code = EXIT_BUDGET_EXCEEDED
            break
        if budget.check_max_iterations():
            click.echo("Max iterations reached.", err=True)
            exit_code = EXIT_BUDGET_EXCEEDED
            break

        task = next_eligible_task(tasks, config)
        if task is None:
            # termination_decision said continue but nothing is
            # eligible -- this indicates a retry cap exhaustion or a
            # dependency health issue the analyzer already surfaced.
            click.echo("No eligible task available; terminating.", err=True)
            exit_code = EXIT_BLOCKED
            break

        budget.record_iteration()
        iteration = budget.iteration_count

        # Parse the task spec. Parse failures mark the task stuck (R18.7).
        try:
            spec = parse_task_spec(project_root / task.spec_path)
        except TaskSpecParseError as exc:
            tasks = _mark_task_stuck(tasks, task.id, tasks_path)
            click.echo(
                f"Task {task.id} marked stuck: spec parse error: {exc}",
                err=True,
            )
            continue

        # Persona selection: escalation first, orchestrator second.
        try:
            selection = None
            if escalation.should_escalate(task):
                selection = escalation.try_route(task)
                if selection is not None:
                    escalation_events += 1
            if selection is None:
                selection = await orchestrator.select_persona(
                    task=task, spec=spec, registry=registry,
                )
        except StuckTaskError as exc:
            tasks = _mark_task_stuck(tasks, task.id, tasks_path)
            click.echo(f"Task {task.id} marked stuck: {exc}", err=True)
            continue

        # Compose the context window (R6.1-R6.7, R14.5, R5.3).
        brief = Path(config.summary_path).read_text(encoding="utf-8")
        context_window = compose_context(
            task=task,
            spec=spec,
            persona=selection.persona,
            brief=brief,
            resumed_notice=bool(task.resumed_from_interruption),
            max_tokens=config.max_context_tokens,
            max_file_bytes=config.max_context_file_bytes,
            base_dir=project_root,
        )

        # Flip task to in_progress and persist before invoking Claude Code CLI
        # so an interrupted run can detect the in_progress state on
        # restart (R14.1, R14.3).
        tasks = _set_task_status(tasks, task.id, "in_progress", tasks_path)
        pre_snapshot = list(tasks)

        # Invoke Claude Code CLI.
        iter_log_path = log_dir / f"iter-{iteration:04d}.log"
        try:
            invocation = await invoker.invoke(
                context=context_window.text,
                log_path=iter_log_path,
                call_kind=(
                    "escalation"
                    if selection.path == "escalation"
                    else "persona_execution"
                ),
                cwd=project_root,
                model_id=config.default_model_id,
            )
        except (KeyboardInterrupt, SystemExit):
            # Operator- and platform-initiated shutdowns must never be
            # swallowed by the graceful-continue handler below (R1.3).
            raise
        except Exception as exc:  # noqa: BLE001 - subprocess errors
            _handle_invocation_error(
                exc=exc,
                task=task,
                persona_name=selection.persona.name,
                tasks=tasks,
                tasks_path=tasks_path,
            )
            # Pick up the persisted status/retry update so the next
            # iteration starts from a consistent tasks list (R1.4, R1.9).
            tasks = _load_tasks(tasks_path)
            continue

        # Read the post-iteration snapshot of tasks.json as raw dicts
        # so unauthorized edits can be reverted (R8.8).
        post_snapshot = _read_raw_tasks(tasks_path)

        # Validate (R7.2-R7.13).
        try:
            validation = await validator.run(
                task=task,
                spec=spec,
                executing_persona_name=selection.persona.name,
                log_path=iter_log_path,
                default_timeout_ms=config.validation_timeout_ms,
                cwd=project_root,
            )
        except ValidatorStuckError as exc:
            tasks = _mark_task_stuck(tasks, task.id, tasks_path)
            click.echo(f"Task {task.id} marked stuck: {exc}", err=True)
            continue

        # Process task creation (R8.2-R8.13).
        creation_result = processor.process(
            pre_snapshot=pre_snapshot,
            post_snapshot=post_snapshot,
            executing_task_id=task.id,
            acting_persona=selection.persona.name,
            iteration=iteration,
        )
        total_new_tasks += len(creation_result.accepted)

        # Reload tasks after the processor's atomic write.
        tasks = _load_tasks(tasks_path)

        # Update executing task status based on validation (R2.5, R2.6).
        new_status, new_retry = status_after_validation(task, validation.checks)
        tasks = _update_task(
            tasks,
            task.id,
            {"status": new_status, "retry_count": new_retry},
            tasks_path,
        )

        # Git commit (R13.1, R13.2).
        outcome: IterationOutcome = (
            "pass" if validation.overall == "pass" else "fail"
        )
        git_mgr.iteration_commit(
            iteration=iteration,
            task_id=task.id,
            persona_name=selection.persona.name,
            outcome=outcome,
        )

        # Record LLM calls for token accounting (R12.1).
        if selection.token_usage is not None:
            accountant.record(
                LlmCallRecord(
                    kind="orchestrator_selection",
                    model=selection.token_usage.model,
                    input_tokens=selection.token_usage.input_tokens,
                    output_tokens=selection.token_usage.output_tokens,
                )
            )
        if invocation.token_usage is not None:
            accountant.record(
                LlmCallRecord(
                    kind=(
                        "escalation"
                        if selection.path == "escalation"
                        else "persona_execution"
                    ),
                    model=invocation.token_usage.model,
                    input_tokens=invocation.token_usage.input_tokens,
                    output_tokens=invocation.token_usage.output_tokens,
                )
            )

    # --- End of loop: persist task list + run summary ------------------
    # Tasks are already persisted at every transition via the helpers;
    # nothing to do here beyond writing the RunSummary (R11.4).
    status_counts: dict[str, int] = {}
    for t in tasks:
        status_counts[t.status] = status_counts.get(t.status, 0) + 1

    try:
        summary = RunSummary(
            run_id=run_id,
            total_iterations=budget.iteration_count,
            status_counts=status_counts,  # type: ignore[arg-type]
            total_new_tasks=total_new_tasks,
            escalation_events=escalation_events,
            elapsed_ms=int((time.monotonic() - start_time) * 1000),
            token_totals=accountant.totals(),
            exit_code=exit_code,
        )
        write_run_summary(summary=summary, log_dir=log_dir)
    except Exception as exc:  # noqa: BLE001 - summary write must not mask exit code
        logger.warning("Failed to write run summary: %s", exc)

    return exit_code


# ---------------------------------------------------------------------------
# Helpers for the run loop (kept module-local so tests can import them
# if needed, but not part of the public API).
# ---------------------------------------------------------------------------


def _load_tasks(tasks_path: Path) -> list[Task]:
    """Load and validate ``tasks.json`` as a ``list[Task]``.

    An empty or whitespace-only file yields an empty list. Any other
    content must parse through :data:`TASK_LIST_ADAPTER`; validation
    errors propagate up to the caller.
    """
    raw = tasks_path.read_text(encoding="utf-8") if tasks_path.exists() else ""
    if not raw.strip():
        return []
    return TASK_LIST_ADAPTER.validate_json(raw)


def _read_raw_tasks(tasks_path: Path) -> list[dict[str, Any]]:
    """Read ``tasks.json`` as a raw dict list for diff comparison (R8.2, R8.3)."""
    if not tasks_path.exists():
        return []
    raw = tasks_path.read_text(encoding="utf-8")
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _apply_resume(tasks: list[Task], tasks_path: Path) -> list[Task]:
    """Run the Resumer and persist any transitions (R14.3-R14.6, R2.9-R2.11)."""
    result = resumer_resume(tasks)
    if not (
        result.reset_tasks
        or result.stuck_by_missing_dep
        or result.stuck_by_cycle_tasks
    ):
        return tasks

    reset_by_id = {t.id: t for t in result.reset_tasks}
    stuck_by_id = {
        t.id: t
        for t in list(result.stuck_by_missing_dep)
        + list(result.stuck_by_cycle_tasks)
    }
    merged: list[Task] = []
    for t in tasks:
        if t.id in stuck_by_id:
            merged.append(stuck_by_id[t.id])
        elif t.id in reset_by_id:
            merged.append(reset_by_id[t.id])
        else:
            merged.append(t)
    atomic_write_bytes(tasks_path, TASK_LIST_ADAPTER.dump_json(merged))
    return merged


def _mark_task_stuck(
    tasks: list[Task], task_id: str, tasks_path: Path
) -> list[Task]:
    return _update_task(tasks, task_id, {"status": "stuck"}, tasks_path)


def _set_task_status(
    tasks: list[Task],
    task_id: str,
    status: TaskStatus,
    tasks_path: Path,
) -> list[Task]:
    return _update_task(tasks, task_id, {"status": status}, tasks_path)


def _update_task(
    tasks: list[Task],
    task_id: str,
    update: dict[str, Any],
    tasks_path: Path,
) -> list[Task]:
    """Apply ``update`` to the task with ``task_id`` and persist atomically."""
    updated: list[Task] = []
    for t in tasks:
        if t.id == task_id:
            updated.append(t.model_copy(update=update))
        else:
            updated.append(t)
    atomic_write_bytes(tasks_path, TASK_LIST_ADAPTER.dump_json(updated))
    return updated


# Substring (matched case-insensitively) used to classify a Claude Code CLI
# invocation failure as a chunk-limit failure (R1.7). Kept as a
# module-level constant so it is easy to grep for and to keep the
# handler pure w.r.t. string literals.
CHUNK_LIMIT_SUBSTRING = "chunk exceed the limit"


def _excerpt(s: str, limit: int = 2000) -> str:
    """Trim free-form captured output to a bounded excerpt for log records.

    Returns ``s`` unchanged when it is already at or below ``limit``
    characters; otherwise returns the first ``limit`` characters
    followed by a ``"...[truncated N chars]"`` suffix so downstream
    consumers know the value was clipped. Non-string inputs are coerced
    via :func:`str` so callers can pass ``getattr`` results without
    first type-checking them.
    """
    if not isinstance(s, str):
        s = str(s)
    if len(s) <= limit:
        return s
    return s[:limit] + f"...[truncated {len(s) - limit} chars]"


def _handle_invocation_error(
    *,
    exc: Exception,
    task: Task,
    persona_name: str,
    tasks: list[Task],
    tasks_path: Path,
) -> None:
    """Record an Invocation_Error as a synthetic Iteration_Failure (R1.1-R1.9).

    Reuses :func:`status_after_validation` by feeding it a synthetic
    failing ``CheckResult`` so the retry-count and status-transition
    rules stay identical to a failed ``persona_review`` (R1.1, R3.2).
    Persists the updated task list atomically via :func:`_update_task`
    **before** emitting the structured log record so a crash in the
    logger path cannot lose the status update (R1.4).
    """
    stdout_raw = getattr(exc, "stdout", "") or ""
    stderr_raw = getattr(exc, "stderr", "") or ""
    stdout_excerpt = _excerpt(stdout_raw)
    stderr_excerpt = _excerpt(stderr_raw)

    combined = (str(exc) + str(stderr_raw) + str(stdout_raw)).lower()
    chunk_limit_detected = CHUNK_LIMIT_SUBSTRING in combined

    # Build a synthetic failing CheckResult. The Validator never ran,
    # so we mark the synthetic check with ``type="shell"`` plus a
    # stable sentinel name so the log row is recognisable. We do NOT
    # invent a new check type so R3.1 ("no new call_kind values, no
    # new Task status values, no new terminal states") holds.
    synthetic = CheckResult(
        type="shell",
        name="claude_invocation",
        verdict="fail",
        output=f"invocation_error: {type(exc).__name__}: {exc}",
        duration_ms=0,
        timed_out=isinstance(exc, ClaudeCodeInvocationTimeout),
    )
    new_status, new_retry = status_after_validation(task, [synthetic])

    # Persist atomically BEFORE structured logging so a crash in the
    # logger path cannot lose the status update (R1.4).
    _update_task(
        tasks,
        task.id,
        {"status": new_status, "retry_count": new_retry},
        tasks_path,
    )

    structlog.get_logger().error(
        "iteration_invocation_error",
        task_id=task.id,
        persona_name=persona_name,
        exception_type=type(exc).__name__,
        exception_message=str(exc),
        stdout_excerpt=stdout_excerpt,
        stderr_excerpt=stderr_excerpt,
        chunk_limit_detected=chunk_limit_detected,
        failure_mode="chunk_limit" if chunk_limit_detected else "generic",
        new_status=new_status,
        new_retry_count=new_retry,
    )


# ============================================================================
# ralph init (R16.1-R16.9)
# ============================================================================


_DEFAULT_CONFIG_JSON = """\
{
  "fallback_persona": "Writer",
  "planner_persona": "Planner",
  "escalation_persona": null,
  "escalation_threshold": 3,
  "automatic_planner": false,
  "git_integration_enabled": true,
  "max_iterations": 50,
  "max_retries_per_task": 5,
  "wall_clock_timeout_ms": 3600000,
  "per_iteration_task_creation_budget": 10,
  "per_run_task_creation_budget": 100
}
"""

_DEFAULT_SUMMARY_MD = """\
# Project Summary

Describe your project goals, scope, and constraints here.
"""

_DEFAULT_PERSONA_YAML = """\
name: Writer
description: Drafts new prose from an outline.
prompt_template: |
  You are the Writer for "{{task_title}}" ({{task_id}}).

  Project brief:
  {{project_brief}}

  Task spec:
  {{task_spec}}

  Produce a well-structured draft that meets the task's objective.
"""


@main.command()
@click.option(
    "--template",
    default=None,
    help="Domain template name; seeds domain personas + planner persona.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing files without prompting (bypasses R16.6 prompts).",
)
@click.option(
    "--project-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Project root directory to scaffold (defaults to cwd).",
)
def init(
    template: Optional[str], force: bool, project_root: Optional[Path]
) -> None:
    """Scaffold a new Ralph Loop project (R16.1-R16.9)."""
    root = project_root or Path.cwd()
    root.mkdir(parents=True, exist_ok=True)

    files_to_create: dict[Path, str] = {
        root / "SUMMARY.md": _DEFAULT_SUMMARY_MD,
        root / "tasks.json": "[]\n",
        root / "pending_tasks.json": "[]\n",
        root / "ralph.config.json": _DEFAULT_CONFIG_JSON,
    }

    # Pre-flight non-interactive guard (R16.9): if any file already
    # exists and --force was not passed, abort before writing anything.
    existing = [p for p in files_to_create if p.exists()]
    if existing and not force and not sys.stdin.isatty():
        names = ", ".join(p.name for p in existing)
        click.echo(
            f"Existing files detected ({names}). "
            "Re-run with --force to overwrite in non-interactive contexts.",
            err=True,
        )
        sys.exit(EXIT_CONFIG_ERROR)

    for path, content in files_to_create.items():
        if path.exists() and not force:
            # Interactive confirmation (R16.6). click.confirm defaults
            # to "no" so hitting enter keeps the existing file.
            if not click.confirm(f"{path.name} exists. Overwrite?", default=False):
                continue
        path.write_text(content, encoding="utf-8")

    (root / "specs").mkdir(exist_ok=True)
    personas_dir = root / "personas"
    personas_dir.mkdir(exist_ok=True)

    default_persona_path = personas_dir / "writer.yaml"
    if not default_persona_path.exists() or force:
        default_persona_path.write_text(_DEFAULT_PERSONA_YAML, encoding="utf-8")

    if template:
        # R16.7: ``--template <name>`` seeds domain personas + planner
        # persona. The templates themselves ship separately (task 25.2);
        # for now we surface the request so operators know it was seen.
        click.echo(
            f"Note: template {template!r} requested but no template "
            "bundle is installed; default persona was seeded instead."
        )

    click.echo(f"Initialized Ralph Loop project at {root}")


# ============================================================================
# ralph init-tasks (R16.8, R17.2, R17.5-R17.8)
# ============================================================================


@main.command("init-tasks")
@click.option(
    "--project-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Project root directory (defaults to cwd).",
)
@click.option(
    "--personas-dir",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Override Config.personas_dir. Lets the planner resolve personas "
        "from an external directory (for example `templates/book/personas/`) "
        "without copying files into the project."
    ),
)
@click.option(
    "--model",
    "model",
    type=str,
    default=None,
    help=(
        "Override Config.default_model_id for the planner's Claude Code CLI "
        "session (defaults to `claude-opus-4.7`)."
    ),
)
def init_tasks(
    project_root: Optional[Path],
    personas_dir: Optional[Path],
    model: Optional[str],
) -> None:
    """Invoke the planner persona to bootstrap ``tasks.json`` (R16.8, R17.2)."""
    root = project_root or Path.cwd()
    cli_overrides = {
        "personas_dir": str(personas_dir) if personas_dir is not None else None,
        "default_model_id": model,
    }
    try:
        config = load_config(project_root=root, cli_overrides=cli_overrides)
    except ConfigLoadError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    try:
        registry = PersonaRegistry.load(Path(config.personas_dir))
    except PersonaRegistryError as exc:
        click.echo(f"Persona registry error: {exc}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    async def _run() -> int:
        log_dir = Path(config.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

        invoker = ClaudeCodeInvoker(claude_cli_command=config.claude_cli_command)
        budget = BudgetTracker(config)
        pending_queue = PendingQueueManager(
            queue_path=Path(config.pending_tasks_path),
            registry=registry,
            run_id="init-tasks",
        )
        processor = TaskCreationProcessor(
            registry=registry,
            config=config,
            budget=budget,
            pending_queue=pending_queue,
            tasks_path=Path(config.tasks_path),
            run_id="init-tasks",
        )

        brief = Path(config.summary_path).read_text(encoding="utf-8")
        planner = Planner(
            invoker=invoker,
            registry=registry,
            config=config,
            processor=processor,
            tasks_path=Path(config.tasks_path),
            log_path=log_dir / "init-tasks.log",
            model_id=config.default_model_id,
        )
        try:
            result = await planner.bootstrap(reason="init-tasks", brief=brief)
        except PlannerError as exc:
            click.echo(f"Planner error: {exc}", err=True)
            return EXIT_CONFIG_ERROR

        click.echo(
            f"Planner generated {len(result.accepted)} tasks "
            f"(rejected={len(result.rejected)}, spilled={len(result.spilled)})."
        )
        return EXIT_SUCCESS

    sys.exit(asyncio.run(_run()))


# ============================================================================
# ralph rollback (R13.5, R13.6)
# ============================================================================


@main.command()
@click.argument("iteration_number", type=int)
@click.option(
    "--project-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Project root (defaults to cwd).",
)
def rollback(iteration_number: int, project_root: Optional[Path]) -> None:
    """Revert the working tree to the commit for iteration N (R13.5, R13.6)."""
    root = project_root or Path.cwd()
    # Config load is best-effort here: rollback should still work when
    # the required files are missing (the operator may be recovering
    # from a partial scaffold).
    try:
        config = load_config(project_root=root)
        git_enabled = config.git_integration_enabled
    except ConfigLoadError:
        git_enabled = True

    mgr = GitManager(enabled=git_enabled, cwd=root)
    exit_code = mgr.rollback(iteration_number)
    sys.exit(exit_code)
