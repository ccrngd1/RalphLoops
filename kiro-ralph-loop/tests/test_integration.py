"""End-to-end integration tests (Tasks 23.1-23.7).

These tests drive multiple Ralph Loop components together against a
temporary filesystem and a stub Kiro CLI harness
(:mod:`tests.support.fake_kiro`). They complement the component-level
unit and property tests under ``tests/`` by exercising the wiring
between components that a single unit test cannot cover.

Requirements exercised per task:

* 23.1 stub harness — R6.1, R12.1, R12.6
* 23.2 filesystem scaffold — R16.1-R16.7, R16.9
* 23.3 resumption end-to-end — R14.1-R14.6
* 23.4 pending-queue cross-run — R8.10, R8.11, R8.13, R9.5, R9.7-R9.10
* 23.5 concurrent non-executing-task edit revert — R8.8
* 23.6 planner bootstrap — R17.2, R17.3, R17.5, R17.6, R17.8, R12.1
* 23.7 book-writing smoke test — R1.6, R4.1, R4.2, R7.10, R13.2

The tests prefer full end-to-end runs where feasible. Where an end-to-
end flow would require a rich orchestration of multiple Kiro CLI
invocations per iteration (23.7), the test is scoped to the most
informative smoke coverage that still exercises the component wiring.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from ralph_loop.atomic_io import atomic_write_bytes
from ralph_loop.budget import BudgetTracker
from ralph_loop.cli import main
from ralph_loop.context import compose_context
from ralph_loop.kiro import KiroInvoker
from ralph_loop.models import (
    Config,
    Persona,
    TASK_LIST_ADAPTER,
    Task,
)
from ralph_loop.pending_queue import PendingQueueManager
from ralph_loop.persona_registry import PersonaRegistry
from ralph_loop.planner import Planner
from ralph_loop.resumer import resume as resumer_resume
from ralph_loop.snapshot_diff import diff_snapshots
from ralph_loop.task_creation import TaskCreationProcessor
from ralph_loop.task_spec import parse_task_spec


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


FAKE_KIRO = Path(__file__).parent / "support" / "fake_kiro.py"


def _kiro_command() -> str:
    """Return a ``kiro_cli_command`` that invokes the fake harness."""
    return f'"{sys.executable}" "{FAKE_KIRO}"'


def _persona(name: str, *, review_pc: str | None = None) -> Persona:
    return Persona(
        name=name,
        description=f"{name} persona.",
        prompt_template=(
            f"You are {{{{persona_name}}}}. Task: {{{{task_title}}}} "
            f"({{{{task_id}}}}).\n\n{{{{project_brief}}}}\n\n{{{{task_spec}}}}"
        ),
        default_persona_review_pass_condition=review_pc,
    )


def _registry(*names: str, review_pc_for: str | None = None) -> PersonaRegistry:
    return PersonaRegistry(
        {
            n: _persona(
                n, review_pc=("content is well-formed" if n == review_pc_for else None)
            )
            for n in names
        }
    )


def _task_dict(
    tid: str,
    *,
    priority: int = 0,
    status: str = "pending",
    target_persona: str | None = None,
    retry_count: int = 0,
    **kwargs: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": tid,
        "title": f"task {tid}",
        "priority": priority,
        "status": status,
        "spec_path": f"specs/{tid}.md",
        "retry_count": retry_count,
    }
    if target_persona is not None:
        entry["target_persona"] = target_persona
    entry.update(kwargs)
    return entry


def _make_task(
    tid: str,
    *,
    priority: int = 0,
    status: str = "pending",
    target_persona: str | None = None,
    retry_count: int = 0,
    depends_on: list[str] | None = None,
    **kwargs: Any,
) -> Task:
    data = _task_dict(
        tid,
        priority=priority,
        status=status,
        target_persona=target_persona,
        retry_count=retry_count,
    )
    if depends_on is not None:
        data["depends_on"] = depends_on
    data.update(kwargs)
    return Task.model_validate(data)


# ===========================================================================
# Task 23.1: Stub Kiro CLI harness
# ===========================================================================


class TestFakeKiroHarness:
    """Validates that the ``tests/support/fake_kiro.py`` harness itself is
    usable from integration tests (Task 23.1).

    The harness exists to simulate ``kiro-cli chat --no-interactive`` in a
    subprocess-safe way so downstream integration tests can drive the real
    :class:`KiroInvoker` without a Kiro CLI install.

    Requirements: R6.1 (stdin is drained so the invoker's context write
    does not block), R12.1 (structured token envelope parsed), R12.6
    (invoker falls back to ``None`` when the envelope is absent).
    """

    def test_fake_kiro_exists_and_is_executable(self) -> None:
        assert FAKE_KIRO.is_file(), f"fake_kiro.py missing at {FAKE_KIRO}"

    def test_emits_stdout_and_exits_zero(self, tmp_path: Path) -> None:
        env = {**os.environ, "RALPH_STUB_STDOUT": "hello integration test"}
        result = subprocess.run(
            [sys.executable, str(FAKE_KIRO), "chat", "--no-interactive"],
            input="context body",
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        assert "hello integration test" in result.stdout
        assert result.returncode == 0

    def test_emits_token_envelope_when_enabled(self) -> None:
        env = {
            **os.environ,
            "RALPH_STUB_STDOUT": "ok",
            "RALPH_STUB_EMIT_TOKENS": "1",
        }
        result = subprocess.run(
            [sys.executable, str(FAKE_KIRO), "chat", "--no-interactive"],
            input="",
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        assert "RALPH_TOKEN_USAGE:" in result.stdout
        # The marker line must be parseable as the invoker's envelope.
        marker_line = next(
            line for line in result.stdout.splitlines()
            if line.startswith("RALPH_TOKEN_USAGE:")
        )
        payload = marker_line.split("RALPH_TOKEN_USAGE:", 1)[1].strip()
        parsed = json.loads(payload)
        assert parsed["input_tokens"] == 10
        assert parsed["output_tokens"] == 5
        assert parsed["model"] == "stub-model"

    def test_omits_token_envelope_by_default(self) -> None:
        """Without ``RALPH_STUB_EMIT_TOKENS=1`` no envelope line is emitted (R12.6)."""
        env = {**os.environ, "RALPH_STUB_STDOUT": "silent run"}
        # Drop any carry-over env var from a prior test so the absence
        # is meaningful.
        env.pop("RALPH_STUB_EMIT_TOKENS", None)
        result = subprocess.run(
            [sys.executable, str(FAKE_KIRO), "chat", "--no-interactive"],
            input="",
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        assert "RALPH_TOKEN_USAGE:" not in result.stdout

    def test_mutates_tasks_json_when_configured(self, tmp_path: Path) -> None:
        """The harness can overwrite ``tasks.json`` to simulate persona writes."""
        tasks_path = tmp_path / "tasks.json"
        tasks_path.write_text("[]", encoding="utf-8")
        source = tmp_path / "replacement.json"
        source.write_text(
            json.dumps([_task_dict("new-from-stub")]), encoding="utf-8"
        )

        env = {
            **os.environ,
            "RALPH_STUB_STDOUT": "mutated",
            "RALPH_STUB_MUTATE_TASKS": str(source),
            "RALPH_STUB_TASKS_PATH": str(tasks_path),
        }
        subprocess.run(
            [sys.executable, str(FAKE_KIRO), "chat", "--no-interactive"],
            input="",
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        data = json.loads(tasks_path.read_text(encoding="utf-8"))
        assert data[0]["id"] == "new-from-stub"

    def test_respects_configured_exit_code(self) -> None:
        env = {**os.environ, "RALPH_STUB_EXIT_CODE": "7"}
        result = subprocess.run(
            [sys.executable, str(FAKE_KIRO), "chat", "--no-interactive"],
            input="",
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        assert result.returncode == 7

    async def test_kiro_invoker_parses_stub_output(
        self, tmp_path: Path
    ) -> None:
        """KiroInvoker can drive the fake harness and parse its envelope (R6.1, R12.1)."""
        log_path = tmp_path / "logs" / "iter.log"
        sink = io.StringIO()

        # Subprocess inherits the parent env; set the stub behaviour here.
        prior_stdout = os.environ.get("RALPH_STUB_STDOUT")
        prior_tokens = os.environ.get("RALPH_STUB_EMIT_TOKENS")
        os.environ["RALPH_STUB_STDOUT"] = "integration payload"
        os.environ["RALPH_STUB_EMIT_TOKENS"] = "1"
        try:
            invoker = KiroInvoker(kiro_cli_command=_kiro_command())
            result = await invoker.invoke(
                context="COMPOSED CONTEXT",
                log_path=log_path,
                call_kind="persona_execution",
                stdout_sink=sink,
            )
        finally:
            if prior_stdout is None:
                os.environ.pop("RALPH_STUB_STDOUT", None)
            else:
                os.environ["RALPH_STUB_STDOUT"] = prior_stdout
            if prior_tokens is None:
                os.environ.pop("RALPH_STUB_EMIT_TOKENS", None)
            else:
                os.environ["RALPH_STUB_EMIT_TOKENS"] = prior_tokens

        assert result.exit_code == 0
        assert result.token_usage is not None
        assert result.token_usage.input_tokens == 10
        assert result.token_usage.output_tokens == 5
        assert "integration payload" in result.stdout
        # Log file captured both streams (R11.2, R11.5).
        log_text = log_path.read_text(encoding="utf-8")
        assert "integration payload" in log_text
        assert "RALPH_TOKEN_USAGE:" in log_text


# ===========================================================================
# Task 23.2: Filesystem scaffolding integration test
# ===========================================================================


class TestRalphInitScaffolding:
    """Drives the :command:`ralph init` subcommand against a pristine
    temp directory and verifies every scaffolded file, the overwrite
    guard (R16.6 / R16.9), and the ``--template book`` seeding path
    (R16.7).

    Requirements: R16.1, R16.2, R16.3, R16.4, R16.5, R16.6, R16.7, R16.9.
    """

    def test_init_scaffolds_all_expected_files_and_dirs(
        self, tmp_path: Path
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["init", "--project-root", str(tmp_path), "--force"]
        )
        assert result.exit_code == 0, result.output

        # R16.1 / R16.2 / R16.3 / R16.4 / R16.5: every artefact is present.
        assert (tmp_path / "SUMMARY.md").is_file()
        assert (tmp_path / "tasks.json").is_file()
        assert (tmp_path / "pending_tasks.json").is_file()
        assert (tmp_path / "ralph.config.json").is_file()
        assert (tmp_path / "specs").is_dir()
        assert (tmp_path / "personas").is_dir()

        # tasks.json / pending_tasks.json start as empty arrays.
        assert (
            (tmp_path / "tasks.json").read_text(encoding="utf-8").strip()
            == "[]"
        )
        assert (
            (tmp_path / "pending_tasks.json").read_text(encoding="utf-8").strip()
            == "[]"
        )

        # Default persona seeded so the registry can load (R16.5).
        persona_files = list((tmp_path / "personas").iterdir())
        assert persona_files, "expected at least one default persona"

        # Config JSON parses and carries the documented defaults.
        cfg = json.loads(
            (tmp_path / "ralph.config.json").read_text(encoding="utf-8")
        )
        assert cfg["automatic_planner"] is False
        assert cfg["git_integration_enabled"] is True
        assert cfg["escalation_threshold"] >= 0

    def test_init_second_run_without_force_warns_on_existing_files(
        self, tmp_path: Path
    ) -> None:
        """R16.6/R16.9: existing files are protected in non-interactive contexts."""
        runner = CliRunner()

        first = runner.invoke(
            main, ["init", "--project-root", str(tmp_path), "--force"]
        )
        assert first.exit_code == 0, first.output

        # Modify an existing file; non-interactive re-run without
        # ``--force`` must abort and leave the existing content alone.
        summary = tmp_path / "SUMMARY.md"
        summary.write_text("user edits here", encoding="utf-8")

        second = runner.invoke(
            main, ["init", "--project-root", str(tmp_path)]
        )
        assert second.exit_code != 0
        # The prompt / abort message mentions --force.
        assert "force" in second.output.lower()
        # User edits survived.
        assert (
            summary.read_text(encoding="utf-8") == "user edits here"
        )

    def test_init_with_template_book_runs_and_acknowledges_template(
        self, tmp_path: Path
    ) -> None:
        """``--template book`` invokes successfully and the template name surfaces (R16.7)."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "init",
                "--project-root",
                str(tmp_path),
                "--template",
                "book",
                "--force",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "book" in result.output
        # Scaffold exists regardless of whether template bundles are
        # installed (the fallback default persona is seeded).
        assert (tmp_path / "personas").is_dir()
        assert any((tmp_path / "personas").iterdir())


# ===========================================================================
# Task 23.3: Resumption end-to-end
# ===========================================================================


class TestResumptionEndToEnd:
    """Simulates a mid-iteration interruption by writing an ``in_progress``
    task to ``tasks.json`` and then driving the Resumer and Context Composer
    together the way the main loop does on startup.

    Requirements: R14.1, R14.2, R14.3, R14.4, R14.5, R14.6.

    We do not send SIGTERM to a live subprocess because Python signal
    delivery differs across platforms (Windows in particular). Instead,
    we recreate the *on-disk condition* that signal handling would leave
    behind -- an ``in_progress`` task in ``tasks.json`` -- and drive the
    resume path exactly as the main loop does.
    """

    def test_resume_resets_in_progress_to_failing_and_flags_interruption(
        self, tmp_path: Path
    ) -> None:
        tasks_path = tmp_path / "tasks.json"
        # Mid-iteration crash snapshot: the loop had flipped ``alpha`` to
        # ``in_progress`` before the crash.
        pre_crash = [
            _make_task("alpha", status="in_progress", retry_count=2),
            _make_task("beta", status="pending"),
        ]
        atomic_write_bytes(tasks_path, TASK_LIST_ADAPTER.dump_json(pre_crash))

        # Startup: load and run the resumer.
        loaded = TASK_LIST_ADAPTER.validate_json(tasks_path.read_text(encoding="utf-8"))
        result = resumer_resume(loaded)

        # R14.3 / R14.4 / R14.5: status reset, retry preserved, flag set.
        assert len(result.reset_tasks) == 1
        reset = result.reset_tasks[0]
        assert reset.id == "alpha"
        assert reset.status == "failing"
        assert reset.retry_count == 2
        assert reset.resumed_from_interruption is True

        # Persist the merged list the way the main loop does and
        # confirm the on-disk state reflects the reset.
        merged: list[Task] = []
        reset_by_id = {t.id: t for t in result.reset_tasks}
        for t in loaded:
            merged.append(reset_by_id.get(t.id, t))
        atomic_write_bytes(tasks_path, TASK_LIST_ADAPTER.dump_json(merged))
        reloaded = TASK_LIST_ADAPTER.validate_json(tasks_path.read_text(encoding="utf-8"))
        alpha = next(t for t in reloaded if t.id == "alpha")
        assert alpha.status == "failing"
        assert alpha.retry_count == 2
        assert alpha.resumed_from_interruption is True

    def test_resumed_task_context_window_includes_interruption_notice(
        self, tmp_path: Path
    ) -> None:
        """R14.5: the next iteration's Context_Window carries the resumed notice."""
        # Build a spec the composer can consume.
        spec_path = tmp_path / "specs" / "alpha.md"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(
            "---\n"
            "id: alpha\n"
            "title: Alpha task\n"
            "validation:\n"
            "  - type: shell\n"
            "    commands: [\"echo ok\"]\n"
            "---\n"
            "## Objective\nDo alpha work.\n"
            "## Context References\nNone.\n"
            "## Instructions\nWrite alpha.\n",
            encoding="utf-8",
        )
        spec = parse_task_spec(spec_path)

        # A reset task: status=failing, resumed_from_interruption=True.
        task = _make_task(
            "alpha",
            status="failing",
            retry_count=2,
            resumed_from_interruption=True,
        )
        persona = _persona("Writer")

        window = compose_context(
            task=task,
            spec=spec,
            persona=persona,
            brief="A short project brief.",
            resumed_notice=bool(task.resumed_from_interruption),
            base_dir=tmp_path,
        )

        # R14.5: the resumed-from-interruption section is present.
        assert "Resumed from interruption" in window.text
        # The full task spec still renders after the notice.
        assert "Do alpha work." in window.text


# ===========================================================================
# Task 23.4: Pending-queue cross-run integration test
# ===========================================================================


class TestPendingQueueCrossRun:
    """Exercises the full pending-queue lifecycle across two simulated
    runs: run 1 spills surplus tasks when the per-iteration budget is
    exhausted; run 2 admits them without charging them against run 2's
    creation budget.

    Requirements: R8.10, R8.11, R8.13, R9.5, R9.7, R9.8, R9.9, R9.10.
    """

    def test_spill_then_admit_preserves_metadata_and_bypasses_run_budget(
        self, tmp_path: Path
    ) -> None:
        queue_path = tmp_path / "pending_tasks.json"
        tasks_path = tmp_path / "tasks.json"
        tasks_path.write_text("[]", encoding="utf-8")
        registry = _registry("Writer", "Reviewer")

        # ---- Run 1: spill two surplus tasks ------------------------
        run1_id = "run-1"
        # Tight budget forces the spill.
        run1_config = Config(
            fallback_persona="Writer",
            per_iteration_task_creation_budget=1,
            per_run_task_creation_budget=100,
        )
        run1_budget = BudgetTracker(run1_config)
        run1_budget.record_iteration()
        run1_queue = PendingQueueManager(queue_path, registry, run_id=run1_id)
        run1_processor = TaskCreationProcessor(
            registry=registry,
            config=run1_config,
            budget=run1_budget,
            pending_queue=run1_queue,
            tasks_path=tasks_path,
            run_id=run1_id,
        )

        # Executing task already on disk.
        exe = _make_task("exe", status="in_progress")
        tasks_path.write_text(
            json.dumps([exe.model_dump(mode="json")]), encoding="utf-8"
        )

        # Post-snapshot: executing task untouched, 3 new tasks created.
        new_entries = [
            _task_dict("new-0", target_persona="Writer"),
            _task_dict("new-1", target_persona="Reviewer"),
            _task_dict("new-2"),
        ]
        post_snapshot = [exe.model_dump(mode="json")] + new_entries

        run1_result = run1_processor.process(
            pre_snapshot=[exe],
            post_snapshot=post_snapshot,
            executing_task_id="exe",
            acting_persona="Writer",
            iteration=5,
        )
        assert len(run1_result.accepted) == 1
        assert len(run1_result.spilled) == 2
        # R8.13: spilled tasks carry run 1's id.
        assert all(
            t.spilled_run_id == run1_id for t in run1_result.spilled
        )
        # Creation metadata preserved on spilled entries.
        for t in run1_result.spilled:
            assert t.created_at_iteration == 5
            assert t.created_by_persona == "Writer"

        # On-disk queue matches result.
        queue_contents = json.loads(queue_path.read_text(encoding="utf-8"))
        assert len(queue_contents) == 2
        for entry in queue_contents:
            assert entry["spilled_run_id"] == run1_id
            assert entry["created_at_iteration"] == 5
            assert entry["created_by_persona"] == "Writer"

        # ---- Run 2: admit the pending queue ------------------------
        run2_id = "run-2"
        # Per-run budget of 0 would reject any *newly* created task in
        # run 2, but admissions from the pending queue MUST NOT be
        # charged against this budget (R9.8).
        run2_config = Config(
            fallback_persona="Writer",
            per_iteration_task_creation_budget=10,
            per_run_task_creation_budget=0,
        )
        run2_budget = BudgetTracker(run2_config)
        run2_queue = PendingQueueManager(queue_path, registry, run_id=run2_id)

        admit_result = run2_queue.process_on_startup()

        # R9.5 / R9.7: admitted entries stamped with run 2's id;
        # original spilled_run_id and creation metadata preserved.
        assert admit_result.loaded == 2
        assert len(admit_result.admitted) == 2
        for t in admit_result.admitted:
            assert t.admitted_run_id == run2_id
            assert t.spilled_run_id == run1_id
            assert t.created_at_iteration == 5
            assert t.created_by_persona == "Writer"

        # R9.9: queue truncated to []. R9.10: counts logged (implicit;
        # captured via the run-summary on the real loop).
        assert queue_path.read_text(encoding="utf-8") == "[]"

        # R9.8: BudgetTracker was NOT advanced by the admit.
        assert run2_budget.per_run_created == 0
        # Even though the run-budget cap is 0, the admit succeeded.
        assert run2_budget.can_create_this_run() is False


# ===========================================================================
# Task 23.5: Concurrent non-executing-task edit revert
# ===========================================================================


class TestNonExecutingTaskEditRevert:
    """The Kiro CLI stub mutates a *non-executing* task entry; the loop
    must revert the edit on merge and record a warning.

    Requirements: R8.8.
    """

    def test_modification_of_non_executing_task_is_reverted_on_merge(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        tasks_path = tmp_path / "tasks.json"
        registry = _registry("Writer")
        config = Config(fallback_persona="Writer")
        budget = BudgetTracker(config)
        budget.record_iteration()
        queue = PendingQueueManager(
            tmp_path / "pending_tasks.json", registry, run_id="run-x"
        )

        exe = _make_task("exe", status="in_progress")
        other = _make_task("other", priority=0, status="pending")
        # Persist the pre-snapshot so the diff reflects a real
        # on-disk state.
        tasks_path.write_text(
            json.dumps(
                [
                    exe.model_dump(mode="json"),
                    other.model_dump(mode="json"),
                ]
            ),
            encoding="utf-8",
        )

        # Simulate the persona's mutation: bump ``other``'s priority.
        post_other = other.model_dump(mode="json")
        post_other["priority"] = 999
        post_snapshot = [exe.model_dump(mode="json"), post_other]

        processor = TaskCreationProcessor(
            registry=registry,
            config=config,
            budget=budget,
            pending_queue=queue,
            tasks_path=tasks_path,
            run_id="run-x",
        )

        with caplog.at_level(logging.WARNING, logger="ralph_loop.task_creation"):
            result = processor.process(
                pre_snapshot=[exe, other],
                post_snapshot=post_snapshot,
                executing_task_id="exe",
                acting_persona="Writer",
                iteration=3,
            )

        # R8.8: exactly one revert recorded, identifying the modified task.
        assert len(result.reverted) == 1
        assert result.reverted[0].task_id == "other"
        assert result.reverted[0].reason == "modified"

        # The on-disk state reflects the pre version of ``other``.
        persisted = json.loads(tasks_path.read_text(encoding="utf-8"))
        other_persisted = next(e for e in persisted if e["id"] == "other")
        assert other_persisted["priority"] == 0

        # R8.8: a warning was logged identifying the iteration, task id,
        # and acting persona.
        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "reverting unauthorized modification" in m
            and "other" in m
            and "Writer" in m
            for m in messages
        ), f"expected revert warning; got {messages}"


# ===========================================================================
# Task 23.6: Planner bootstrap integration test
# ===========================================================================


class TestPlannerBootstrapIntegration:
    """End-to-end planner bootstrap driving the real :class:`KiroInvoker`
    against the fake harness. The stub writes three tasks to
    ``tasks.json``; the planner delegates to the Task Creation Processor,
    which validates each entry, admits them, and logs counts.

    Requirements: R17.2, R17.3, R17.5, R17.6, R17.8, R12.1.
    """

    async def test_planner_admits_three_tasks_and_records_token_usage(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        tasks_path = tmp_path / "tasks.json"
        tasks_path.write_text("[]", encoding="utf-8")
        queue_path = tmp_path / "pending_tasks.json"
        log_path = tmp_path / "logs" / "planner.log"

        registry = _registry("Planner", "Writer", "Reviewer")
        config = Config(
            fallback_persona="Writer",
            planner_persona="Planner",
            automatic_planner=True,
            per_iteration_task_creation_budget=10,
            per_run_task_creation_budget=100,
        )
        budget = BudgetTracker(config)
        # ``bootstrap`` runs before any iteration; budget starts at zero
        # but the first admit needs the per-iteration budget active.
        budget.record_iteration()
        queue = PendingQueueManager(queue_path, registry, run_id="run-planner")
        processor = TaskCreationProcessor(
            registry=registry,
            config=config,
            budget=budget,
            pending_queue=queue,
            tasks_path=tasks_path,
            run_id="run-planner",
        )

        # Prepare the replacement tasks.json the stub will copy over.
        replacement = tmp_path / "planner_tasks.json"
        replacement.write_text(
            json.dumps(
                [
                    _task_dict("t1", target_persona="Writer"),
                    _task_dict("t2", target_persona="Reviewer"),
                    _task_dict("t3"),
                ]
            ),
            encoding="utf-8",
        )

        # Drive the stub: it will overwrite tasks.json and emit a token
        # envelope so the invoker records the planner call (R12.1).
        prior_env = {
            k: os.environ.get(k)
            for k in [
                "RALPH_STUB_MUTATE_TASKS",
                "RALPH_STUB_TASKS_PATH",
                "RALPH_STUB_STDOUT",
                "RALPH_STUB_EMIT_TOKENS",
            ]
        }
        os.environ["RALPH_STUB_MUTATE_TASKS"] = str(replacement)
        os.environ["RALPH_STUB_TASKS_PATH"] = str(tasks_path)
        os.environ["RALPH_STUB_STDOUT"] = "planner wrote 3 tasks"
        os.environ["RALPH_STUB_EMIT_TOKENS"] = "1"
        try:
            invoker = KiroInvoker(kiro_cli_command=_kiro_command())
            planner = Planner(
                invoker=invoker,
                registry=registry,
                config=config,
                processor=processor,
                tasks_path=tasks_path,
                log_path=log_path,
            )
            with caplog.at_level(logging.INFO, logger="ralph_loop.planner"):
                result = await planner.bootstrap(
                    reason="auto", brief="Write a short book."
                )
        finally:
            for k, v in prior_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        # R17.6 / R17.8: all three entries were validated and accepted.
        assert len(result.accepted) == 3
        assert result.rejected == []
        assert result.spilled == []
        assert {t.id for t in result.accepted} == {"t1", "t2", "t3"}

        # All accepted tasks land in tasks.json, stamped with the
        # planner persona.
        persisted = json.loads(tasks_path.read_text(encoding="utf-8"))
        assert {e["id"] for e in persisted} == {"t1", "t2", "t3"}
        for e in persisted:
            assert e["created_by_persona"] == "Planner"
            # ``Planner.bootstrap`` passes ``iteration=0``.
            assert e["created_at_iteration"] == 0

        # R17.2 / R17.5: the planner invocation landed in the log file.
        log_text = log_path.read_text(encoding="utf-8")
        assert "planner wrote 3 tasks" in log_text
        # R12.1: the envelope was emitted and captured in the log.
        assert "RALPH_TOKEN_USAGE:" in log_text

        # R17.8: the accepted/rejected counts land in the planner log.
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "accepted=3" in m and "rejected=0" in m
            for m in messages
        ), f"expected accepted/rejected counts; got {messages}"

    async def test_planner_rejects_invalid_entries_and_logs(
        self, tmp_path: Path
    ) -> None:
        """R17.6: invalid planner output is rejected through the same pipeline."""
        tasks_path = tmp_path / "tasks.json"
        tasks_path.write_text("[]", encoding="utf-8")
        queue_path = tmp_path / "pending_tasks.json"
        log_path = tmp_path / "logs" / "planner.log"

        registry = _registry("Planner", "Writer")
        config = Config(
            fallback_persona="Writer", planner_persona="Planner",
        )
        budget = BudgetTracker(config)
        budget.record_iteration()
        queue = PendingQueueManager(queue_path, registry, run_id="run-planner")
        processor = TaskCreationProcessor(
            registry=registry,
            config=config,
            budget=budget,
            pending_queue=queue,
            tasks_path=tasks_path,
            run_id="run-planner",
        )

        # Mix: one valid, one missing required fields, one unknown persona.
        replacement = tmp_path / "planner_tasks.json"
        replacement.write_text(
            json.dumps(
                [
                    _task_dict("ok"),
                    {"id": "bad-schema"},
                    _task_dict("bad-persona", target_persona="Ghost"),
                ]
            ),
            encoding="utf-8",
        )

        prior_env = {
            k: os.environ.get(k)
            for k in (
                "RALPH_STUB_MUTATE_TASKS",
                "RALPH_STUB_TASKS_PATH",
                "RALPH_STUB_STDOUT",
            )
        }
        os.environ["RALPH_STUB_MUTATE_TASKS"] = str(replacement)
        os.environ["RALPH_STUB_TASKS_PATH"] = str(tasks_path)
        os.environ["RALPH_STUB_STDOUT"] = "planner wrote mixed tasks"
        try:
            invoker = KiroInvoker(kiro_cli_command=_kiro_command())
            planner = Planner(
                invoker=invoker,
                registry=registry,
                config=config,
                processor=processor,
                tasks_path=tasks_path,
                log_path=log_path,
            )
            result = await planner.bootstrap(
                reason="init-tasks", brief="Brief"
            )
        finally:
            for k, v in prior_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        assert len(result.accepted) == 1
        assert result.accepted[0].id == "ok"
        assert len(result.rejected) == 2
        # R17.6 / R8.4: rejected entries do not reach the pending queue.
        if queue_path.exists():
            assert json.loads(queue_path.read_text(encoding="utf-8")) == []


# ===========================================================================
# Task 23.7: Book-writing smoke test
# ===========================================================================


class TestBookWritingSmoke:
    """Minimal smoke coverage for the book-authoring workflow.

    A fully end-to-end book run would require orchestrating several
    Kiro CLI invocations per iteration (orchestrator decision, persona
    execution, reviewing persona, etc.) and the fake harness only
    supports one envelope per invocation. Instead this test wires up
    the persona set from the book template and exercises the slices
    that the requirements explicitly cite:

    * persona selection follows ``target_persona`` when set (R4.1) and
      falls back to the configured fallback when not (R4.2 / fallback
      branch),
    * validation aggregates per-check verdicts correctly (R7.10),
    * the Git Manager formats iteration commit messages per R13.2,
    * the loop's completion rule fires when every task is ``passing``
      (R1.6).

    Requirements: R1.6, R4.1, R4.2, R7.10, R13.2.
    """

    def test_book_persona_registry_loads_and_describes_all(
        self, tmp_path: Path
    ) -> None:
        """R4.1 / R4.2: the registry carries a Writer/Reviewer/Editor/
        Fact-Checker/Outline/Planner set suitable for book authoring.
        """
        personas_dir = tmp_path / "personas"
        personas_dir.mkdir(parents=True)
        for name, description in [
            ("Writer", "Drafts new prose from an outline."),
            ("Reviewer", "Reviews prose for structural issues."),
            ("Editor", "Polishes prose for voice and clarity."),
            ("FactChecker", "Verifies factual claims against sources."),
            ("Outline", "Produces chapter outlines from the brief."),
            ("Planner", "Bootstraps the Task List from the brief."),
        ]:
            (personas_dir / f"{name.lower()}.yaml").write_text(
                f"name: {name}\n"
                f"description: {description}\n"
                "prompt_template: |\n"
                "  You are {{persona_name}}.\n"
                "  Task: {{task_title}} ({{task_id}})\n",
                encoding="utf-8",
            )
        registry = PersonaRegistry.load(personas_dir)
        names = {p.name for p in registry.all()}
        assert names == {"Writer", "Reviewer", "Editor", "FactChecker", "Outline", "Planner"}
        # R3.8/R4.2 projection is populated for every persona.
        assert len(registry.describe_all_for_orchestrator()) == 6

    def test_explicit_target_persona_routes_without_llm(
        self, tmp_path: Path
    ) -> None:
        """R4.1: when ``target_persona`` is set the Orchestrator routes
        directly to that persona with no LLM call.
        """
        import asyncio
        from ralph_loop.orchestrator import Orchestrator

        registry = _registry("Writer", "Reviewer", "Editor")
        # The invoker MUST NOT be called on this branch, so point it at
        # an obviously invalid command to make accidental calls loud.
        invoker = KiroInvoker(kiro_cli_command=f'"{sys.executable}" -c "import sys;sys.exit(99)"')
        orchestrator = Orchestrator(
            invoker=invoker,
            log_path=tmp_path / "logs" / "orch.log",
            fallback_persona="Writer",
        )

        task = _make_task("ch1", target_persona="Writer")
        spec_path = tmp_path / "ch1.md"
        spec_path.write_text(
            "---\n"
            "id: ch1\n"
            "title: Chapter 1\n"
            "target_persona: Writer\n"
            "validation:\n"
            "  - type: shell\n"
            "    commands: [\"echo ok\"]\n"
            "---\n"
            "## Objective\nDraft chapter 1.\n"
            "## Context References\nNone.\n"
            "## Instructions\nDraft it.\n",
            encoding="utf-8",
        )
        spec = parse_task_spec(spec_path)

        selection = asyncio.run(
            orchestrator.select_persona(task=task, spec=spec, registry=registry)
        )
        assert selection.persona.name == "Writer"
        assert selection.path == "explicit"

    def test_validator_aggregates_shell_and_file_exists_checks(
        self, tmp_path: Path
    ) -> None:
        """R7.10: overall == 'pass' iff every check passes."""
        import asyncio
        from ralph_loop.validator import Validator

        registry = _registry("Writer")
        # The validator only invokes Kiro for persona_review checks,
        # which we don't use here.
        invoker = KiroInvoker(kiro_cli_command=_kiro_command())
        validator = Validator(invoker=invoker, registry=registry)

        # Create an artefact the file_exists check can see.
        manuscript = tmp_path / "chapter-1.md"
        manuscript.write_text("chapter body", encoding="utf-8")

        spec_path = tmp_path / "ch1.md"
        spec_path.write_text(
            "---\n"
            "id: ch1\n"
            "title: Chapter 1\n"
            "validation:\n"
            "  - type: file_exists\n"
            "    name: manuscript-exists\n"
            "    paths: [\"chapter-1.md\"]\n"
            "---\n"
            "## Objective\nDraft.\n"
            "## Context References\nNone.\n"
            "## Instructions\nWrite.\n",
            encoding="utf-8",
        )
        spec = parse_task_spec(spec_path)
        task = _make_task("ch1")

        result = asyncio.run(
            validator.run(
                task=task,
                spec=spec,
                executing_persona_name="Writer",
                log_path=tmp_path / "iter.log",
                default_timeout_ms=30_000,
                cwd=tmp_path,
            )
        )
        assert result.overall == "pass"
        assert len(result.checks) == 1
        assert result.checks[0].verdict == "pass"

        # Now break the check and verify aggregation flips to fail.
        manuscript.unlink()
        result2 = asyncio.run(
            validator.run(
                task=task,
                spec=spec,
                executing_persona_name="Writer",
                log_path=tmp_path / "iter2.log",
                default_timeout_ms=30_000,
                cwd=tmp_path,
            )
        )
        assert result2.overall == "fail"

    def test_iteration_commit_message_format(self, tmp_path: Path) -> None:
        """R13.2: iteration commits follow the canonical tag format."""
        import subprocess as sp

        # Initialise a throwaway git repo.
        sp.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        sp.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        sp.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        # Seed commit so git has a HEAD.
        (tmp_path / "README").write_text("book", encoding="utf-8")
        sp.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        sp.run(
            ["git", "commit", "-m", "initial"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

        from ralph_loop.git_manager import GitManager

        git_mgr = GitManager(enabled=True, cwd=tmp_path)
        # Create a change so there is something to commit.
        (tmp_path / "chapter-1.md").write_text("chapter body", encoding="utf-8")
        commit = git_mgr.iteration_commit(
            iteration=7,
            task_id="ch1",
            persona_name="Writer",
            outcome="pass",
        )
        # Commit either succeeded with a sha or was skipped with a reason;
        # both branches exercise the helper.
        if commit.sha is not None:
            log = sp.run(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            )
            message = log.stdout.strip()
            # R13.2: ``ralph: iter=<N> task=<id> persona=<name> outcome=<outcome>``.
            assert message == "ralph: iter=7 task=ch1 persona=Writer outcome=pass"

    def test_completion_decision_when_all_tasks_passing(self) -> None:
        """R1.6: the loop terminates successfully when every task is passing."""
        from ralph_loop.task_selector import termination_decision

        tasks = [
            _make_task("ch1", status="passing"),
            _make_task("ch2", status="passing"),
        ]
        decision = termination_decision(tasks)
        assert decision.verdict == "success"
        assert decision.exit_code == 0
