"""Unit tests for :mod:`ralph_loop.task_creation` (Tasks 17.1, 17.3, 17.5, 17.7).

Example-based coverage of the three pure helpers plus the end-to-end
:class:`TaskCreationProcessor.process` orchestration. Property-based
coverage for Properties 17, 18, and 19 lives in
``tests/test_task_creation_properties.py``.

Requirements exercised: R8.2, R8.3, R8.4, R8.5, R8.6, R8.7, R8.8,
R8.10, R8.11, R8.12, R8.13, R2.10, R2.11, R9.8.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ralph_loop.budget import BudgetTracker
from ralph_loop.models import Config, Persona, Task
from ralph_loop.pending_queue import PendingQueueManager
from ralph_loop.persona_registry import PersonaRegistry
from ralph_loop.snapshot_diff import diff_snapshots
from ralph_loop.task_creation import (
    TaskCreationProcessor,
    admit_or_spill_new_tasks,
    revert_unauthorized_edits,
    validate_new_entry,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _persona(name: str) -> Persona:
    return Persona(
        name=name,
        description=f"{name} persona",
        prompt_template="template",
    )


def _registry(*names: str) -> PersonaRegistry:
    return PersonaRegistry({n: _persona(n) for n in names})


def _task(
    tid: str,
    *,
    priority: int = 0,
    status: str = "pending",
    retry_count: int = 0,
    target_persona: str | None = None,
    depends_on: list[str] | None = None,
    creation_chain: list[str] | None = None,
    created_at_iteration: int | None = None,
    created_by_persona: str | None = None,
) -> Task:
    return Task(
        id=tid,
        title=f"task {tid}",
        priority=priority,
        status=status,  # type: ignore[arg-type]
        spec_path=f"specs/{tid}.md",
        retry_count=retry_count,
        target_persona=target_persona,
        depends_on=depends_on,
        creation_chain=creation_chain,
        created_at_iteration=created_at_iteration,
        created_by_persona=created_by_persona,
    )


def _dump(task: Task) -> dict[str, Any]:
    return task.model_dump(mode="json")


def _config(
    *,
    per_iteration: int = 10,
    per_run: int = 100,
    max_creation_chain_depth: int = 5,
) -> Config:
    return Config(
        fallback_persona="Writer",
        per_iteration_task_creation_budget=per_iteration,
        per_run_task_creation_budget=per_run,
        max_creation_chain_depth=max_creation_chain_depth,
    )


# ---------------------------------------------------------------------------
# validate_new_entry (R8.4, R8.7, R8.12)
# ---------------------------------------------------------------------------


class TestValidateNewEntry:
    def test_valid_entry_returns_task_and_no_reason(self) -> None:
        entry = _dump(_task("new"))
        task, reason = validate_new_entry(
            entry, registry=_registry(), max_creation_chain_depth=5
        )
        assert task is not None
        assert task.id == "new"
        assert reason is None

    def test_valid_entry_with_known_persona_admitted(self) -> None:
        entry = _dump(_task("new", target_persona="Writer"))
        task, reason = validate_new_entry(
            entry,
            registry=_registry("Writer"),
            max_creation_chain_depth=5,
        )
        assert task is not None
        assert task.target_persona == "Writer"
        assert reason is None

    def test_missing_required_field_rejected(self) -> None:
        entry = {"id": "x"}  # missing title, priority, status, spec_path
        task, reason = validate_new_entry(
            entry, registry=_registry(), max_creation_chain_depth=5
        )
        assert task is None
        assert reason is not None
        assert "schema" in reason.lower()

    def test_unknown_target_persona_rejected(self) -> None:
        entry = _dump(_task("new", target_persona="Ghost"))
        task, reason = validate_new_entry(
            entry,
            registry=_registry("Writer"),
            max_creation_chain_depth=5,
        )
        assert task is None
        assert reason is not None
        assert "Ghost" in reason

    def test_absent_creation_chain_passes_any_depth(self) -> None:
        # No chain at all = implicitly depth 0.
        entry = _dump(_task("new"))
        task, reason = validate_new_entry(
            entry,
            registry=_registry(),
            max_creation_chain_depth=0,
        )
        assert task is not None
        assert reason is None

    def test_creation_chain_exactly_at_limit_passes(self) -> None:
        entry = _dump(_task("new", creation_chain=["a", "b", "c"]))
        task, reason = validate_new_entry(
            entry, registry=_registry(), max_creation_chain_depth=3
        )
        assert task is not None
        assert reason is None

    def test_creation_chain_exceeds_limit_rejected(self) -> None:
        entry = _dump(_task("new", creation_chain=["a", "b", "c", "d"]))
        task, reason = validate_new_entry(
            entry, registry=_registry(), max_creation_chain_depth=3
        )
        assert task is None
        assert reason is not None
        assert "creation_chain" in reason

    def test_negative_retry_count_rejected_by_schema(self) -> None:
        entry = _dump(_task("new"))
        entry["retry_count"] = -1
        task, reason = validate_new_entry(
            entry, registry=_registry(), max_creation_chain_depth=5
        )
        assert task is None
        assert reason is not None


# ---------------------------------------------------------------------------
# revert_unauthorized_edits (R8.8)
# ---------------------------------------------------------------------------


class TestRevertUnauthorizedEdits:
    def test_modification_of_executing_task_is_kept(self) -> None:
        pre_task = _task("exe", status="in_progress")
        post_dump = _dump(pre_task)
        post_dump["status"] = "passing"  # authorized self-edit
        post_dump["retry_count"] = 1

        diff = diff_snapshots([pre_task], [post_dump])

        merged, reverted = revert_unauthorized_edits(
            [pre_task], diff,
            executing_task_id="exe",
            iteration=1,
            acting_persona="Writer",
        )

        assert reverted == []
        assert [t.id for t in merged] == ["exe"]
        assert merged[0].status == "passing"
        assert merged[0].retry_count == 1

    def test_modification_of_other_task_is_reverted(self) -> None:
        exe = _task("exe", status="in_progress")
        other = _task("other", priority=0)

        other_post = _dump(other)
        other_post["priority"] = 99  # unauthorized edit

        diff = diff_snapshots([exe, other], [_dump(exe), other_post])

        merged, reverted = revert_unauthorized_edits(
            [exe, other], diff,
            executing_task_id="exe",
            iteration=1,
            acting_persona="Writer",
        )

        assert len(reverted) == 1
        assert reverted[0].task_id == "other"
        assert reverted[0].reason == "modified"

        merged_by_id = {t.id: t for t in merged}
        assert merged_by_id["other"].priority == 0  # reverted to pre

    def test_deletion_of_other_task_is_reverted(self) -> None:
        exe = _task("exe", status="in_progress")
        other = _task("other")

        # Post omits "other" -> delete.
        diff = diff_snapshots([exe, other], [_dump(exe)])

        merged, reverted = revert_unauthorized_edits(
            [exe, other], diff,
            executing_task_id="exe",
            iteration=5,
            acting_persona="Reviewer",
        )

        assert len(reverted) == 1
        assert reverted[0].task_id == "other"
        assert reverted[0].reason == "deleted"

        merged_by_id = {t.id: t for t in merged}
        assert "other" in merged_by_id
        # The pre instance is restored.
        assert merged_by_id["other"].id == "other"

    def test_self_deletion_is_also_reverted(self) -> None:
        exe = _task("exe", status="in_progress")

        # Persona deleted its own row.
        diff = diff_snapshots([exe], [])

        merged, reverted = revert_unauthorized_edits(
            [exe], diff,
            executing_task_id="exe",
            iteration=1,
            acting_persona="Writer",
        )

        assert len(reverted) == 1
        assert reverted[0].task_id == "exe"
        assert reverted[0].reason == "deleted"
        # Pre version restored.
        assert [t.id for t in merged] == ["exe"]

    def test_invalid_post_edit_of_executing_task_keeps_pre(self) -> None:
        exe = _task("exe", status="in_progress")
        broken_post = _dump(exe)
        broken_post["retry_count"] = -5  # fails Pydantic validation

        diff = diff_snapshots([exe], [broken_post])

        merged, reverted = revert_unauthorized_edits(
            [exe], diff,
            executing_task_id="exe",
            iteration=1,
            acting_persona="Writer",
        )

        assert reverted == []  # executing task is authorized, still not reverted
        assert [t.id for t in merged] == ["exe"]
        assert merged[0].retry_count == 0  # pre value kept


# ---------------------------------------------------------------------------
# admit_or_spill_new_tasks (R8.5, R8.10, R8.11)
# ---------------------------------------------------------------------------


class TestAdmitOrSpill:
    def test_all_within_budget_are_accepted(self) -> None:
        tasks = [_task(f"t{i}") for i in range(3)]
        tracker = BudgetTracker(_config(per_iteration=5, per_run=5))
        tracker.record_iteration()

        accepted, spilled = admit_or_spill_new_tasks(
            tasks, budget=tracker, iteration=1, acting_persona="Writer"
        )

        assert len(accepted) == 3
        assert spilled == []
        # R8.5: stamped with iteration and persona.
        for t in accepted:
            assert t.created_at_iteration == 1
            assert t.created_by_persona == "Writer"

    def test_per_iteration_budget_spills_surplus_with_correct_reason(self) -> None:
        tasks = [_task(f"t{i}") for i in range(4)]
        tracker = BudgetTracker(_config(per_iteration=2, per_run=100))
        tracker.record_iteration()

        accepted, spilled = admit_or_spill_new_tasks(
            tasks, budget=tracker, iteration=3, acting_persona="Reviewer"
        )

        assert len(accepted) == 2
        assert [t.id for t in accepted] == ["t0", "t1"]
        assert len(spilled) == 2
        assert all(reason == "per_iteration_budget" for _, reason in spilled)
        # Spilled tasks are returned unstamped here; stamping happens in
        # ``TaskCreationProcessor.process`` before persisting.
        assert spilled[0][0].id == "t2"

    def test_per_run_budget_spills_with_correct_reason(self) -> None:
        # Per-iteration budget ample, per-run cap is the bottleneck.
        tracker = BudgetTracker(_config(per_iteration=100, per_run=2))
        tracker.record_iteration()
        # Prime the tracker: one already admitted earlier this run.
        tracker.record_created(1)

        tasks = [_task(f"t{i}") for i in range(3)]
        accepted, spilled = admit_or_spill_new_tasks(
            tasks, budget=tracker, iteration=1, acting_persona="Writer"
        )

        assert len(accepted) == 1
        assert len(spilled) == 2
        assert all(reason == "per_run_budget" for _, reason in spilled)

    def test_per_iteration_check_runs_before_per_run_check(self) -> None:
        # Per-iteration = 1, per-run = 100. Second task spills under
        # per_iteration_budget, not per_run_budget.
        tracker = BudgetTracker(_config(per_iteration=1, per_run=100))
        tracker.record_iteration()

        tasks = [_task("a"), _task("b")]
        _, spilled = admit_or_spill_new_tasks(
            tasks, budget=tracker, iteration=1, acting_persona="Writer"
        )

        assert len(spilled) == 1
        assert spilled[0][1] == "per_iteration_budget"

    def test_empty_input_produces_empty_output(self) -> None:
        tracker = BudgetTracker(_config())
        tracker.record_iteration()

        accepted, spilled = admit_or_spill_new_tasks(
            [], budget=tracker, iteration=1, acting_persona="Writer"
        )

        assert accepted == []
        assert spilled == []


# ---------------------------------------------------------------------------
# TaskCreationProcessor.process end-to-end (R8.2-R8.13, R2.10-R2.11)
# ---------------------------------------------------------------------------


def _make_processor(
    tmp_path: Path,
    *,
    config: Config | None = None,
    registry: PersonaRegistry | None = None,
    run_id: str = "run-1",
) -> tuple[TaskCreationProcessor, BudgetTracker, PendingQueueManager, Path]:
    cfg = config or _config()
    reg = registry if registry is not None else _registry("Writer")
    budget = BudgetTracker(cfg)
    budget.record_iteration()  # prime so per-iteration budget is active
    queue_path = tmp_path / "pending_tasks.json"
    queue = PendingQueueManager(queue_path, reg, run_id=run_id)
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text("[]", encoding="utf-8")
    processor = TaskCreationProcessor(
        registry=reg,
        config=cfg,
        budget=budget,
        pending_queue=queue,
        tasks_path=tasks_path,
        run_id=run_id,
    )
    return processor, budget, queue, tasks_path


class TestProcess:
    def test_happy_path_admits_new_entries_and_persists(
        self, tmp_path: Path
    ) -> None:
        processor, _budget, _q, tasks_path = _make_processor(tmp_path)
        pre = [_task("exe", status="in_progress")]
        new_entry = _dump(_task("new"))
        post = [_dump(pre[0]), new_entry]

        result = processor.process(
            pre_snapshot=pre,
            post_snapshot=post,
            executing_task_id="exe",
            acting_persona="Writer",
            iteration=1,
        )

        assert len(result.accepted) == 1
        assert result.rejected == []
        assert result.reverted == []
        assert result.spilled == []
        assert result.cycle_stuck == []

        # Persisted list contains both the executing task and the new one.
        persisted = json.loads(tasks_path.read_text(encoding="utf-8"))
        assert {e["id"] for e in persisted} == {"exe", "new"}
        accepted_entry = next(e for e in persisted if e["id"] == "new")
        # R8.5: stamped metadata on persistence.
        assert accepted_entry["created_at_iteration"] == 1
        assert accepted_entry["created_by_persona"] == "Writer"

    def test_rejected_entries_are_not_spilled(self, tmp_path: Path) -> None:
        processor, _b, queue, tasks_path = _make_processor(tmp_path)
        pre = [_task("exe", status="in_progress")]
        # Two invalid entries: missing fields and unknown persona.
        post = [
            _dump(pre[0]),
            {"id": "bad-schema"},  # missing required fields
            _dump(_task("bad-persona", target_persona="Ghost")),
        ]

        result = processor.process(
            pre_snapshot=pre,
            post_snapshot=post,
            executing_task_id="exe",
            acting_persona="Writer",
            iteration=1,
        )

        assert result.accepted == []
        assert len(result.rejected) == 2
        # R8.4 / R8.7: rejected entries never reach the pending queue.
        queue_contents = json.loads(
            (tmp_path / "pending_tasks.json").read_text(encoding="utf-8")
        ) if (tmp_path / "pending_tasks.json").exists() else []
        assert queue_contents == []
        # Persisted list contains only the executing task.
        persisted = json.loads(tasks_path.read_text(encoding="utf-8"))
        assert {e["id"] for e in persisted} == {"exe"}

    def test_unauthorized_modification_is_reverted(
        self, tmp_path: Path
    ) -> None:
        processor, _b, _q, tasks_path = _make_processor(tmp_path)
        exe = _task("exe", status="in_progress")
        other = _task("other", priority=0)

        other_post = _dump(other)
        other_post["priority"] = 99

        result = processor.process(
            pre_snapshot=[exe, other],
            post_snapshot=[_dump(exe), other_post],
            executing_task_id="exe",
            acting_persona="Writer",
            iteration=7,
        )

        assert len(result.reverted) == 1
        assert result.reverted[0].task_id == "other"
        assert result.reverted[0].reason == "modified"

        persisted = json.loads(tasks_path.read_text(encoding="utf-8"))
        by_id = {e["id"]: e for e in persisted}
        assert by_id["other"]["priority"] == 0  # reverted

    def test_surplus_is_spilled_to_pending_queue(self, tmp_path: Path) -> None:
        # Budget = 1; create 3 new tasks -> 1 accepted, 2 spilled.
        cfg = _config(per_iteration=1, per_run=100)
        processor, _b, _q, tasks_path = _make_processor(
            tmp_path, config=cfg, run_id="run-xyz"
        )

        pre = [_task("exe", status="in_progress")]
        new_entries = [_dump(_task(f"new{i}")) for i in range(3)]
        post = [_dump(pre[0])] + new_entries

        result = processor.process(
            pre_snapshot=pre,
            post_snapshot=post,
            executing_task_id="exe",
            acting_persona="Writer",
            iteration=2,
        )

        assert len(result.accepted) == 1
        assert len(result.spilled) == 2
        # R8.13: spilled_run_id stamped in the result.
        assert all(t.spilled_run_id == "run-xyz" for t in result.spilled)
        # Creation metadata preserved on the spilled tasks.
        assert all(t.created_at_iteration == 2 for t in result.spilled)
        assert all(t.created_by_persona == "Writer" for t in result.spilled)

        # The pending queue file contains exactly the two spilled tasks.
        queue_data = json.loads(
            (tmp_path / "pending_tasks.json").read_text(encoding="utf-8")
        )
        assert len(queue_data) == 2
        assert all(e["spilled_run_id"] == "run-xyz" for e in queue_data)
        assert all(e["created_at_iteration"] == 2 for e in queue_data)
        assert all(e["created_by_persona"] == "Writer" for e in queue_data)

    def test_cycle_detection_marks_stuck_after_merge(
        self, tmp_path: Path
    ) -> None:
        # Pre list has only "exe". New entries "a" and "b" close a cycle
        # between themselves: a -> b, b -> a.
        processor, _b, _q, tasks_path = _make_processor(tmp_path)
        exe = _task("exe", status="in_progress")

        a = _dump(_task("a", depends_on=["b"]))
        b = _dump(_task("b", depends_on=["a"]))
        post = [_dump(exe), a, b]

        result = processor.process(
            pre_snapshot=[exe],
            post_snapshot=post,
            executing_task_id="exe",
            acting_persona="Writer",
            iteration=1,
        )

        assert len(result.accepted) == 2
        # Both new tasks participate in the cycle.
        assert set(result.cycle_stuck) == {"a", "b"}

        persisted = json.loads(tasks_path.read_text(encoding="utf-8"))
        by_id = {e["id"]: e for e in persisted}
        assert by_id["a"]["status"] == "stuck"
        assert by_id["b"]["status"] == "stuck"

    def test_executing_task_self_edit_persists(self, tmp_path: Path) -> None:
        processor, _b, _q, tasks_path = _make_processor(tmp_path)
        exe = _task("exe", status="in_progress")
        post_exe = _dump(exe)
        post_exe["status"] = "passing"

        processor.process(
            pre_snapshot=[exe],
            post_snapshot=[post_exe],
            executing_task_id="exe",
            acting_persona="Writer",
            iteration=1,
        )

        persisted = json.loads(tasks_path.read_text(encoding="utf-8"))
        assert persisted[0]["status"] == "passing"
