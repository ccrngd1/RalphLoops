"""Unit tests for :class:`ralph_loop.pending_queue.PendingQueueManager`
(Task 10.1).

These are plain pytest unit tests for ``process_on_startup`` and
``spill``. Their matching property tests (P20 and P21) live in
``tests/test_pending_queue_properties.py``.

Requirements exercised: 8.10, 8.11, 8.13, 9.1, 9.2, 9.3, 9.4, 9.5,
9.6, 9.7, 9.8, 9.9, 9.10, 9.11.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralph_loop.models import Persona, Task
from ralph_loop.pending_queue import PendingQueueError, PendingQueueManager
from ralph_loop.persona_registry import PersonaRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registry(*names: str) -> PersonaRegistry:
    """Build an in-memory registry with the listed persona names.

    Each persona gets a minimal valid definition so the registry is
    well-formed regardless of which name is asked for at lookup time.
    """
    personas = {
        name: Persona(
            name=name,
            description=f"{name} persona",
            prompt_template="template",
        )
        for name in names
    }
    return PersonaRegistry(personas)


def _task(
    id_: str = "t1",
    *,
    target_persona: str | None = None,
    spilled_run_id: str | None = None,
    created_at_iteration: int | None = None,
    created_by_persona: str | None = None,
) -> Task:
    """Build a minimal :class:`Task` with the listed overrides."""
    return Task(
        id=id_,
        title=f"title-{id_}",
        priority=1,
        status="pending",
        spec_path=f"specs/{id_}.md",
        retry_count=0,
        target_persona=target_persona,
        spilled_run_id=spilled_run_id,
        created_at_iteration=created_at_iteration,
        created_by_persona=created_by_persona,
    )


def _write_queue(path: Path, raw: str) -> None:
    path.write_text(raw, encoding="utf-8")


# ---------------------------------------------------------------------------
# process_on_startup - empty / missing file branches (R9.2)
# ---------------------------------------------------------------------------


class TestProcessOnStartupEmptyFile:
    def test_missing_file_returns_empty_result(self, tmp_path: Path) -> None:
        queue = tmp_path / "pending_tasks.json"
        manager = PendingQueueManager(queue, _registry(), run_id="r1")

        result = manager.process_on_startup()

        assert result.loaded == 0
        assert result.admitted == []
        assert result.discarded == []
        # The file must not be created when it was absent to begin with.
        assert not queue.exists()

    def test_zero_byte_file_returns_empty_result(self, tmp_path: Path) -> None:
        queue = tmp_path / "pending_tasks.json"
        queue.touch()
        manager = PendingQueueManager(queue, _registry(), run_id="r1")

        result = manager.process_on_startup()

        assert result.loaded == 0
        assert result.admitted == []

    def test_whitespace_only_file_returns_empty_result(
        self, tmp_path: Path
    ) -> None:
        queue = tmp_path / "pending_tasks.json"
        _write_queue(queue, "   \n\t  ")
        manager = PendingQueueManager(queue, _registry(), run_id="r1")

        result = manager.process_on_startup()

        assert result.loaded == 0
        assert result.admitted == []


# ---------------------------------------------------------------------------
# process_on_startup - happy path admission (R9.3, R9.4, R9.5, R9.7)
# ---------------------------------------------------------------------------


class TestProcessOnStartupAdmission:
    def test_valid_entry_without_target_persona_is_admitted(
        self, tmp_path: Path
    ) -> None:
        queue = tmp_path / "pending_tasks.json"
        task = _task(
            "a",
            spilled_run_id="run-prior",
            created_at_iteration=3,
            created_by_persona="Writer",
        )
        _write_queue(queue, json.dumps([task.model_dump(mode="json")]))

        manager = PendingQueueManager(queue, _registry(), run_id="run-new")
        result = manager.process_on_startup()

        assert result.loaded == 1
        assert result.discarded == []
        assert len(result.admitted) == 1
        admitted = result.admitted[0]
        # R9.7: admitted_run_id is stamped with the current run id,
        # and the original creation metadata + spilled_run_id are
        # preserved unchanged.
        assert admitted.admitted_run_id == "run-new"
        assert admitted.spilled_run_id == "run-prior"
        assert admitted.created_at_iteration == 3
        assert admitted.created_by_persona == "Writer"

    def test_valid_entry_with_known_persona_is_admitted(
        self, tmp_path: Path
    ) -> None:
        queue = tmp_path / "pending_tasks.json"
        task = _task("a", target_persona="Writer")
        _write_queue(queue, json.dumps([task.model_dump(mode="json")]))

        manager = PendingQueueManager(
            queue, _registry("Writer"), run_id="run-new"
        )
        result = manager.process_on_startup()

        assert len(result.admitted) == 1
        assert result.admitted[0].target_persona == "Writer"

    def test_file_truncated_to_empty_list_after_admission(
        self, tmp_path: Path
    ) -> None:
        queue = tmp_path / "pending_tasks.json"
        _write_queue(queue, json.dumps([_task("a").model_dump(mode="json")]))

        manager = PendingQueueManager(queue, _registry(), run_id="r")
        manager.process_on_startup()

        # R9.9: the file is atomically rewritten to ``[]``.
        assert queue.read_text(encoding="utf-8") == "[]"


# ---------------------------------------------------------------------------
# process_on_startup - discard branches (R9.6)
# ---------------------------------------------------------------------------


class TestProcessOnStartupDiscards:
    def test_entry_with_unknown_target_persona_is_discarded(
        self, tmp_path: Path
    ) -> None:
        queue = tmp_path / "pending_tasks.json"
        task = _task("a", target_persona="Ghost")
        _write_queue(queue, json.dumps([task.model_dump(mode="json")]))

        manager = PendingQueueManager(
            queue, _registry("Writer"), run_id="r"
        )
        result = manager.process_on_startup()

        assert result.admitted == []
        assert len(result.discarded) == 1
        assert "Ghost" in result.discarded[0].reason
        # File still gets truncated regardless of discards.
        assert queue.read_text(encoding="utf-8") == "[]"

    def test_entry_failing_schema_validation_is_discarded(
        self, tmp_path: Path
    ) -> None:
        queue = tmp_path / "pending_tasks.json"
        # Missing required fields (title, priority, status, spec_path).
        _write_queue(queue, json.dumps([{"id": "a"}]))

        manager = PendingQueueManager(queue, _registry(), run_id="r")
        result = manager.process_on_startup()

        assert result.admitted == []
        assert len(result.discarded) == 1
        reason = result.discarded[0].reason
        assert "Task schema validation failed" in reason

    def test_entry_with_negative_retry_count_is_discarded(
        self, tmp_path: Path
    ) -> None:
        queue = tmp_path / "pending_tasks.json"
        bad = _task("a").model_dump(mode="json")
        bad["retry_count"] = -1
        _write_queue(queue, json.dumps([bad]))

        manager = PendingQueueManager(queue, _registry(), run_id="r")
        result = manager.process_on_startup()

        assert result.admitted == []
        assert len(result.discarded) == 1

    def test_non_dict_entry_is_discarded(self, tmp_path: Path) -> None:
        queue = tmp_path / "pending_tasks.json"
        _write_queue(queue, json.dumps([42, "foo", None, ["nested"]]))

        manager = PendingQueueManager(queue, _registry(), run_id="r")
        result = manager.process_on_startup()

        assert result.loaded == 4
        assert result.admitted == []
        assert len(result.discarded) == 4
        # Every reason identifies the non-object type.
        assert all("not a JSON object" in d.reason for d in result.discarded)

    def test_mixed_valid_and_invalid_entries(self, tmp_path: Path) -> None:
        queue = tmp_path / "pending_tasks.json"
        entries = [
            _task("good").model_dump(mode="json"),
            {"id": "bad-schema"},  # missing required fields
            _task("also-good", target_persona="Writer").model_dump(
                mode="json"
            ),
            _task("bad-persona", target_persona="Ghost").model_dump(
                mode="json"
            ),
        ]
        _write_queue(queue, json.dumps(entries))

        manager = PendingQueueManager(
            queue, _registry("Writer"), run_id="r"
        )
        result = manager.process_on_startup()

        assert result.loaded == 4
        assert len(result.admitted) == 2
        assert len(result.discarded) == 2
        assert {t.id for t in result.admitted} == {"good", "also-good"}


# ---------------------------------------------------------------------------
# process_on_startup - fail-fast JSON parsing (R9.11)
# ---------------------------------------------------------------------------


class TestProcessOnStartupParseErrors:
    def test_invalid_json_raises_pending_queue_error_with_path(
        self, tmp_path: Path
    ) -> None:
        queue = tmp_path / "pending_tasks.json"
        _write_queue(queue, "{not valid json")

        manager = PendingQueueManager(queue, _registry(), run_id="r")

        with pytest.raises(PendingQueueError) as excinfo:
            manager.process_on_startup()

        # R9.11: the message must identify both the file path and the
        # underlying parse error.
        msg = str(excinfo.value)
        assert str(queue) in msg
        assert excinfo.value.__cause__ is not None
        assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)

    def test_json_root_not_a_list_raises_pending_queue_error(
        self, tmp_path: Path
    ) -> None:
        queue = tmp_path / "pending_tasks.json"
        _write_queue(queue, json.dumps({"not": "a list"}))

        manager = PendingQueueManager(queue, _registry(), run_id="r")

        with pytest.raises(PendingQueueError) as excinfo:
            manager.process_on_startup()

        msg = str(excinfo.value)
        assert str(queue) in msg
        assert "JSON array" in msg


# ---------------------------------------------------------------------------
# spill (R8.10, R8.11, R8.13)
# ---------------------------------------------------------------------------


class TestSpill:
    def test_spill_to_missing_file_creates_queue_with_one_entry(
        self, tmp_path: Path
    ) -> None:
        queue = tmp_path / "pending_tasks.json"
        task = _task(
            "a", created_at_iteration=5, created_by_persona="Writer"
        )
        manager = PendingQueueManager(queue, _registry(), run_id="run-1")

        manager.spill(task, "per_iteration_budget", "run-1")

        data = json.loads(queue.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert len(data) == 1
        # R8.13: creation metadata is preserved; spilled_run_id is
        # stamped with the supplied run id.
        assert data[0]["id"] == "a"
        assert data[0]["created_at_iteration"] == 5
        assert data[0]["created_by_persona"] == "Writer"
        assert data[0]["spilled_run_id"] == "run-1"

    def test_spill_appends_to_existing_queue(self, tmp_path: Path) -> None:
        queue = tmp_path / "pending_tasks.json"
        _write_queue(
            queue, json.dumps([_task("existing").model_dump(mode="json")])
        )
        manager = PendingQueueManager(queue, _registry(), run_id="run-2")

        manager.spill(_task("new"), "per_run_budget", "run-2")

        data = json.loads(queue.read_text(encoding="utf-8"))
        assert [e["id"] for e in data] == ["existing", "new"]

    def test_spill_preserves_creation_chain(self, tmp_path: Path) -> None:
        queue = tmp_path / "pending_tasks.json"
        task = Task(
            id="a",
            title="t",
            priority=1,
            status="pending",
            spec_path="specs/a.md",
            retry_count=0,
            created_at_iteration=2,
            created_by_persona="Reviewer",
            creation_chain=["root", "mid", "a"],
        )
        manager = PendingQueueManager(queue, _registry(), run_id="r")

        manager.spill(task, "per_iteration_budget", "r")

        data = json.loads(queue.read_text(encoding="utf-8"))
        assert data[0]["creation_chain"] == ["root", "mid", "a"]

    def test_spill_is_atomic_no_tmp_file_left_behind(
        self, tmp_path: Path
    ) -> None:
        queue = tmp_path / "pending_tasks.json"
        manager = PendingQueueManager(queue, _registry(), run_id="r")

        manager.spill(_task("a"), "per_iteration_budget", "r")

        # The atomic writer should have cleaned up the temp sidecar.
        assert not (tmp_path / "pending_tasks.json.tmp").exists()

    def test_spill_fails_fast_on_invalid_existing_queue_json(
        self, tmp_path: Path
    ) -> None:
        queue = tmp_path / "pending_tasks.json"
        _write_queue(queue, "{not valid")
        manager = PendingQueueManager(queue, _registry(), run_id="r")

        with pytest.raises(PendingQueueError):
            manager.spill(_task("a"), "per_iteration_budget", "r")
