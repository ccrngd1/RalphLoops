"""Task Creation Processor for post-iteration ``tasks.json`` diffs.

The Task Creation Processor runs once per iteration, after Claude Code CLI
exits. It compares the pre-iteration snapshot of ``tasks.json`` (a
validated :class:`~ralph_loop.models.Task` list the loop wrote before
invoking Kiro) to the post-iteration snapshot (the raw dict list read
back from disk), and applies the rules from Requirement 8:

- **Revert unauthorized edits (R8.8)**: every modification or deletion
  in the diff whose ``id`` is not the executing task's ``id`` is
  reverted to the pre-iteration state. Executing-task edits are
  permitted.
- **Validate new entries (R8.4, R8.7, R8.12)**: every ``created``
  entry is checked against the :class:`Task` Pydantic schema, the
  persona registry (when ``target_persona`` is set), and the
  configured ``max_creation_chain_depth``. Failures are *rejected*,
  not spilled: they never reach the pending queue (R8.4).
- **Budget admit/spill (R8.5, R8.10, R8.11, R8.13)**: the surviving
  valid entries are admitted up to the per-iteration and per-run
  caps; the surplus is appended to ``pending_tasks.json`` through
  :meth:`ralph_loop.pending_queue.PendingQueueManager.spill` with
  ``spilled_run_id`` stamped and original creation metadata preserved.
- **Cycle re-detection (R2.10, R2.11)**: once accepted tasks have
  been merged in, :func:`ralph_loop.dependency_analyzer.analyze_dependencies`
  re-runs over the merged list so any new cycle closes mark the
  participants ``stuck`` before the list is persisted.
- **Atomic persist**: the final merged list is written back via
  :func:`ralph_loop.atomic_io.atomic_write_bytes` so a crash during
  the write leaves either the old or the new file intact.

Three helper functions are exposed at module scope so the
property-based tests for Properties 17, 18, and 19 can exercise the
rules without having to plumb the full orchestrator through each
test case: :func:`validate_new_entry`, :func:`revert_unauthorized_edits`,
and :func:`admit_or_spill_new_tasks`. :class:`TaskCreationProcessor`
composes the three into the end-to-end pipeline wired into the loop.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from ralph_loop.atomic_io import atomic_write_bytes
from ralph_loop.budget import BudgetTracker
from ralph_loop.dependency_analyzer import analyze_dependencies
from ralph_loop.models import (
    Config,
    RejectedEntry,
    RevertedEntry,
    SnapshotDiff,
    SpillReason,
    TASK_LIST_ADAPTER,
    Task,
    TaskCreationResult,
)
from ralph_loop.pending_queue import PendingQueueManager
from ralph_loop.persona_registry import PersonaRegistry
from ralph_loop.snapshot_diff import diff_snapshots

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers (property-tested)
# ---------------------------------------------------------------------------


def validate_new_entry(
    entry: dict[str, Any],
    *,
    registry: PersonaRegistry,
    max_creation_chain_depth: int,
) -> tuple[Optional[Task], Optional[str]]:
    """Validate a single candidate new-task entry (R8.4, R8.7, R8.12).

    Three checks are applied, in the order the requirements specify:

    1. **Schema (R8.4)**: the raw dict must parse into a
       :class:`Task`. ``Task.model_validate`` handles required fields,
       types, and the ``retry_count >= 0`` invariant.
    2. **Target persona (R8.7)**: when ``target_persona`` is set, it
       must exist in the persona registry.
    3. **Creation chain depth (R8.12)**: a task's ``creation_chain``
       depth must not exceed ``max_creation_chain_depth``. An absent
       or empty chain always passes.

    Returns a ``(task, None)`` on success or ``(None, reason)`` when
    any check fails. The caller is responsible for logging the
    rejection and for *not* spilling the rejected entry to the
    pending queue (R8.4). ``reason`` is always a non-empty string on
    the failure branch so downstream tests can treat "non-None reason"
    as the rejection signal.
    """

    try:
        task = Task.model_validate(entry)
    except ValidationError as exc:
        return None, f"Task schema validation failed: {exc}"

    if (
        task.target_persona is not None
        and registry.get(task.target_persona) is None
    ):
        return None, (
            f"Target persona {task.target_persona!r} not in registry"
        )

    if task.creation_chain and len(task.creation_chain) > max_creation_chain_depth:
        return None, (
            f"creation_chain depth {len(task.creation_chain)} exceeds "
            f"max_creation_chain_depth {max_creation_chain_depth}: "
            f"{task.creation_chain}"
        )

    return task, None


def revert_unauthorized_edits(
    pre_tasks: list[Task],
    diff: SnapshotDiff,
    *,
    executing_task_id: str,
    iteration: int,
    acting_persona: str,
) -> tuple[list[Task], list[RevertedEntry]]:
    """Revert modifications or deletions of non-executing tasks (R8.8).

    The caller supplies the pre-iteration ``Task`` list and the diff
    produced by :func:`ralph_loop.snapshot_diff.diff_snapshots`. The
    returned merged list starts from ``pre_tasks`` (so every deleted
    entry is implicitly restored) and then applies *authorized*
    modifications to the executing task only. Every reverted entry is
    logged at WARNING and surfaced in the second return value so the
    loop can record the event in the iteration log.

    Semantics:

    - **Modified, id == executing_task_id**: authorized. The
      executing persona is allowed to edit its own task row
      (status flip, retry bump, ...). If the post edit itself fails
      schema validation the pre version is kept and a warning is
      logged -- the persona wrote garbage over its own row.
    - **Modified, id != executing_task_id**: reverted. The pre version
      stays in ``result``; a :class:`RevertedEntry` with
      ``reason="modified"`` is recorded.
    - **Deleted, any id**: reverted. The pre version is already in
      ``result`` (since we start from ``pre_tasks``); a
      :class:`RevertedEntry` with ``reason="deleted"`` is recorded.
      Executing-task self-deletion is still treated as a revert
      because the loop needs the row intact to update its status.

    The function is pure aside from logging: it never touches disk,
    the pending queue, or the registry.
    """

    # Seed ``result`` with the pre snapshot keyed by id. Python dicts
    # preserve insertion order, so downstream ``list(result.values())``
    # emits the merged list in the same order as ``pre_tasks``.
    pre_by_id: dict[str, Task] = {t.id: t for t in pre_tasks}
    result: dict[str, Task] = dict(pre_by_id)
    reverted: list[RevertedEntry] = []

    for pre_dump, post_dump in diff.modified:
        task_id = pre_dump["id"]
        if task_id == executing_task_id:
            # Authorized edit on the executing task. Replace the pre
            # version with the validated post version so status flips
            # and retry-count bumps survive. If the persona wrote a
            # malformed row, fall back to the pre version and log.
            try:
                post_task = Task.model_validate(post_dump)
                result[task_id] = post_task
            except ValidationError as exc:
                logger.warning(
                    "iteration=%d task=%s persona=%s: post-iteration edit "
                    "of executing task is invalid, keeping pre state (%s)",
                    iteration,
                    task_id,
                    acting_persona,
                    exc,
                )
        else:
            logger.warning(
                "iteration=%d task=%s persona=%s: reverting unauthorized "
                "modification of non-executing task",
                iteration,
                task_id,
                acting_persona,
            )
            reverted.append(
                RevertedEntry(task_id=task_id, reason="modified")
            )

    for pre_task in diff.deleted:
        logger.warning(
            "iteration=%d task=%s persona=%s: reverting %sdeletion "
            "of task",
            iteration,
            pre_task.id,
            acting_persona,
            "self-" if pre_task.id == executing_task_id else "unauthorized ",
        )
        reverted.append(
            RevertedEntry(task_id=pre_task.id, reason="deleted")
        )
        # The pre instance is already in ``result``; no further work.

    return list(result.values()), reverted


def admit_or_spill_new_tasks(
    valid_new_tasks: list[Task],
    *,
    budget: BudgetTracker,
    iteration: int,
    acting_persona: str,
) -> tuple[list[Task], list[tuple[Task, SpillReason]]]:
    """Admit up to the budgets, spill the surplus (R8.5, R8.10, R8.11).

    The decision is greedy and input-order preserving: for each
    candidate the tracker is consulted twice (per-iteration first,
    then per-run); the first failing check decides the spill reason.
    Admissions call :meth:`BudgetTracker.record_created`, which
    advances both counters. Spills do *not* advance the counters,
    which matches the BudgetTracker contract and the design's
    intention that spilled tasks re-enter the budget accounting only
    when (and if) a future run admits them from the pending queue
    (R9.8).

    The per-iteration check is applied before the per-run check so
    the spill reason reported to the pending queue matches the *first*
    binding constraint. This also matches the design's ordering in
    R8.10 (per-iteration) -> R8.11 (per-run).

    Accepted tasks are stamped with ``created_at_iteration = iteration``
    and ``created_by_persona = acting_persona`` (R8.5). Spilled tasks
    are returned unstamped; the caller is responsible for stamping
    them before writing to the pending queue so the metadata survives
    across runs (R8.13).
    """

    accepted: list[Task] = []
    spilled: list[tuple[Task, SpillReason]] = []

    for task in valid_new_tasks:
        if not budget.can_create_this_iteration():
            spilled.append((task, "per_iteration_budget"))
            continue
        if not budget.can_create_this_run():
            spilled.append((task, "per_run_budget"))
            continue

        stamped = task.model_copy(
            update={
                "created_at_iteration": iteration,
                "created_by_persona": acting_persona,
            }
        )
        accepted.append(stamped)
        budget.record_created(1)

    return accepted, spilled


# ---------------------------------------------------------------------------
# Processor orchestration
# ---------------------------------------------------------------------------


class TaskCreationProcessor:
    """End-to-end Task_Creation_Event pipeline (R8.2-R8.13, R2.10-R2.11).

    One instance is constructed per run and reused across iterations.
    The constructor captures the collaborators that are loop-scoped
    (registry, budget, pending queue, tasks path, run id); per-
    iteration state (executing task id, acting persona, iteration
    number) is passed explicitly to :meth:`process`.
    """

    def __init__(
        self,
        *,
        registry: PersonaRegistry,
        config: Config,
        budget: BudgetTracker,
        pending_queue: PendingQueueManager,
        tasks_path: Path,
        run_id: str,
    ) -> None:
        self._registry = registry
        self._config = config
        self._budget = budget
        self._pending_queue = pending_queue
        self._tasks_path = Path(tasks_path)
        self._run_id = run_id

    def process(
        self,
        *,
        pre_snapshot: list[Task],
        post_snapshot: list[dict[str, Any]],
        executing_task_id: str,
        acting_persona: str,
        iteration: int,
    ) -> TaskCreationResult:
        """Run the full pipeline and return the resulting
        :class:`TaskCreationResult`.

        Steps:

        1. Diff pre vs post (R8.2, R8.3).
        2. Revert unauthorized edits (R8.8).
        3. Validate each ``created`` entry (R8.4, R8.7, R8.12). Rejected
           entries are logged and excluded from any further processing
           -- they do NOT flow to the pending queue.
        4. Admit/spill the surviving valid entries (R8.5, R8.10,
           R8.11). Spilled entries are stamped with the current
           iteration + persona and then written to the pending queue
           with ``spilled_run_id = self._run_id`` (R8.13).
        5. Merge accepted entries into the reverted list and re-run
           dependency analysis (R2.10, R2.11). Any cycle the new
           entries close marks its participants ``stuck``.
        6. Persist the final list via the atomic writer.
        """

        diff = diff_snapshots(pre_snapshot, post_snapshot)

        # Step 1: revert unauthorized edits.
        merged_tasks, reverted = revert_unauthorized_edits(
            pre_snapshot,
            diff,
            executing_task_id=executing_task_id,
            iteration=iteration,
            acting_persona=acting_persona,
        )

        # Step 2: validate each new entry.
        rejected: list[RejectedEntry] = []
        valid_new: list[Task] = []
        for raw in diff.created:
            task, reason = validate_new_entry(
                raw,
                registry=self._registry,
                max_creation_chain_depth=self._config.max_creation_chain_depth,
            )
            if task is None:
                # Reason is always set on the None branch.
                assert reason is not None
                rejected.append(RejectedEntry(entry=raw, reason=reason))
                logger.warning(
                    "iteration=%d task=%s persona=%s: rejected new "
                    "task entry: %s",
                    iteration,
                    executing_task_id,
                    acting_persona,
                    reason,
                )
            else:
                valid_new.append(task)

        # Step 3: admit within budget, collect surplus for spill.
        accepted, spilled_pairs = admit_or_spill_new_tasks(
            valid_new,
            budget=self._budget,
            iteration=iteration,
            acting_persona=acting_persona,
        )

        # Step 4: write spilled entries to the pending queue. Stamp the
        # creation iteration + persona first so the metadata survives
        # across runs (R8.13). ``PendingQueueManager.spill`` also
        # stamps ``spilled_run_id`` internally.
        spilled: list[Task] = []
        for task, reason in spilled_pairs:
            stamped = task.model_copy(
                update={
                    "created_at_iteration": iteration,
                    "created_by_persona": acting_persona,
                }
            )
            self._pending_queue.spill(stamped, reason, self._run_id)
            spilled.append(
                stamped.model_copy(update={"spilled_run_id": self._run_id})
            )
            logger.info(
                "iteration=%d task=%s persona=%s: spilled task %s "
                "to pending queue (reason=%s)",
                iteration,
                executing_task_id,
                acting_persona,
                stamped.id,
                reason,
            )

        # Step 5: merge accepted entries into the reverted list.
        # ``merged_tasks`` from revert already contains the executing
        # task's authorized post-edit, so the order is:
        # [revert-kept pre + authorized executing edit, then accepted].
        merged_list = list(merged_tasks) + accepted

        # Step 6: re-run cycle detection (R2.10, R2.11). The analysis
        # returns ``updated_tasks`` with cycle participants marked
        # stuck; we persist that list, not ``merged_list``.
        analysis = analyze_dependencies(merged_list)
        cycle_stuck_ids = [t.id for t in analysis.stuck_by_cycle]

        # Step 7: persist the final state.
        dump = TASK_LIST_ADAPTER.dump_json(analysis.updated_tasks)
        atomic_write_bytes(self._tasks_path, dump)

        return TaskCreationResult(
            accepted=accepted,
            rejected=rejected,
            reverted=reverted,
            spilled=spilled,
            cycle_stuck=cycle_stuck_ids,
        )
