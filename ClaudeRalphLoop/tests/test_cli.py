"""Unit tests for the ``ralph`` click CLI (Task 22.6).

These tests exercise the argument parsing and help rendering for each
subcommand using :class:`click.testing.CliRunner`. The heavy lifting of
the run loop is integration-tested separately in task 23.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from ralph_loop.cli import main


# ---------------------------------------------------------------------------
# Help rendering
# ---------------------------------------------------------------------------


def test_ralph_help_shows_description() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Ralph Loop" in result.output
    # All four subcommands are advertised.
    assert "run" in result.output
    assert "init" in result.output
    assert "init-tasks" in result.output
    assert "rollback" in result.output


@pytest.mark.parametrize(
    "subcommand",
    ["run", "init", "init-tasks", "rollback"],
)
def test_each_subcommand_has_help(subcommand: str) -> None:
    runner = CliRunner()
    result = runner.invoke(main, [subcommand, "--help"])
    assert result.exit_code == 0, result.output
    assert subcommand in result.output or "Usage" in result.output


# ---------------------------------------------------------------------------
# ralph init
# ---------------------------------------------------------------------------


def test_ralph_init_scaffolds_project(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["init", "--project-root", str(tmp_path), "--force"]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "SUMMARY.md").is_file()
    assert (tmp_path / "tasks.json").is_file()
    assert (tmp_path / "pending_tasks.json").is_file()
    assert (tmp_path / "ralph.config.json").is_file()
    assert (tmp_path / "specs").is_dir()
    assert (tmp_path / "personas").is_dir()
    # Default persona is seeded so the registry can load.
    assert any((tmp_path / "personas").iterdir())

    # tasks.json and pending_tasks.json are valid empty JSON arrays.
    assert (tmp_path / "tasks.json").read_text(encoding="utf-8").strip() == "[]"
    assert (
        (tmp_path / "pending_tasks.json").read_text(encoding="utf-8").strip()
        == "[]"
    )


def test_ralph_init_force_overwrites_existing(tmp_path: Path) -> None:
    (tmp_path / "SUMMARY.md").write_text("pre-existing", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        main, ["init", "--project-root", str(tmp_path), "--force"]
    )
    assert result.exit_code == 0, result.output
    assert (
        "pre-existing"
        not in (tmp_path / "SUMMARY.md").read_text(encoding="utf-8")
    )


def test_ralph_init_without_force_non_interactive_fails(tmp_path: Path) -> None:
    """Without --force, a non-interactive init over an existing scaffold must exit non-zero (R16.9)."""
    # Seed an existing file so the "already exists" branch fires.
    (tmp_path / "SUMMARY.md").write_text("prior", encoding="utf-8")
    runner = CliRunner()
    # CliRunner disconnects stdin by default, so isatty() is False.
    result = runner.invoke(main, ["init", "--project-root", str(tmp_path)])
    assert result.exit_code != 0
    assert "--force" in result.output or "force" in result.output


def test_ralph_init_with_template_note(tmp_path: Path) -> None:
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


# ---------------------------------------------------------------------------
# ralph rollback
# ---------------------------------------------------------------------------


def test_ralph_rollback_in_non_git_dir_exits_non_zero(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["rollback", "99", "--project-root", str(tmp_path)]
    )
    assert result.exit_code != 0


def test_ralph_rollback_requires_integer_argument() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["rollback", "not-a-number"])
    # Click's usage error surfaces a non-zero exit code.
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ralph run argument wiring
# ---------------------------------------------------------------------------


def test_ralph_run_missing_config_exits_non_zero(tmp_path: Path) -> None:
    """When the required files are missing, `ralph run` fails fast (R15.9)."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["run", "--project-root", str(tmp_path)]
    )
    assert result.exit_code != 0


def test_ralph_run_help_lists_overrides() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["run", "--help"])
    assert result.exit_code == 0
    assert "--config" in result.output
    assert "--max-iterations" in result.output
    assert "--max-retries-per-task" in result.output
    assert "--wall-clock-timeout-ms" in result.output
    assert "--personas-dir" in result.output


def test_ralph_run_personas_dir_override_is_honored(tmp_path: Path) -> None:
    """`--personas-dir` lets `ralph run` resolve personas from an external
    directory without copying them into the project (R15.2, R15.3)."""
    # Scaffold a project, then delete its personas dir so the only
    # available registry lives outside the project root.
    project = tmp_path / "project"
    project.mkdir()
    runner = CliRunner()
    init_result = runner.invoke(
        main, ["init", "--project-root", str(project), "--force"]
    )
    assert init_result.exit_code == 0, init_result.output

    # Remove the default personas dir under the project.
    shutil.rmtree(project / "personas")
    assert not (project / "personas").exists()

    # Create a sibling personas directory with a valid persona file.
    external = tmp_path / "shared-personas"
    external.mkdir()
    (external / "writer.yaml").write_text(
        "name: Writer\n"
        "description: External persona.\n"
        "prompt_template: \"{{persona_name}} {{task_id}}\"\n",
        encoding="utf-8",
    )

    # Without the flag, `run` must fail because personas_dir is missing.
    fail = runner.invoke(
        main, ["run", "--project-root", str(project)]
    )
    assert fail.exit_code != 0
    assert "Required configuration paths" in fail.output

    # With the flag, the loader accepts the external directory. The run
    # still exits non-zero later (no real Kiro CLI is installed) but the
    # config error path must be gone: the error is now downstream.
    ok = runner.invoke(
        main,
        [
            "run",
            "--project-root",
            str(project),
            "--personas-dir",
            str(external),
        ],
    )
    # Config load succeeded: the R15.9 fail-fast message must not appear.
    assert "Required configuration paths" not in ok.output


# ---------------------------------------------------------------------------
# ralph init-tasks
# ---------------------------------------------------------------------------


def test_ralph_init_tasks_without_scaffold_fails(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["init-tasks", "--project-root", str(tmp_path)]
    )
    # Missing tasks.json / summary.md / personas -> config error.
    assert result.exit_code != 0


def test_ralph_init_tasks_help_lists_personas_dir() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["init-tasks", "--help"])
    assert result.exit_code == 0
    assert "--personas-dir" in result.output


def test_ralph_init_tasks_personas_dir_override_accepted(tmp_path: Path) -> None:
    """`init-tasks` accepts `--personas-dir` and loads from the external path."""
    project = tmp_path / "project"
    project.mkdir()
    runner = CliRunner()
    init_result = runner.invoke(
        main, ["init", "--project-root", str(project), "--force"]
    )
    assert init_result.exit_code == 0, init_result.output
    shutil.rmtree(project / "personas")

    external = tmp_path / "shared-personas"
    external.mkdir()
    (external / "planner.yaml").write_text(
        "name: Writer\n"
        "description: External persona.\n"
        "prompt_template: \"{{persona_name}} {{task_id}}\"\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        main,
        [
            "init-tasks",
            "--project-root",
            str(project),
            "--personas-dir",
            str(external),
        ],
    )
    # Config + registry load succeeded. A real Kiro CLI is not installed
    # so the command exits non-zero downstream; the important signal is
    # that the R15.9 personas-missing fail-fast path did NOT fire.
    assert "Required configuration paths" not in result.output



# ---------------------------------------------------------------------------
# Commit SHA resolution (diagnostics)
# ---------------------------------------------------------------------------


class TestResolveCommitSha:
    """``_resolve_commit_sha`` stamps the run log with the current git SHA so
    operators can tell pre-fix dumps from post-fix dumps without relying on
    wall-clock timestamps. The helper must never raise; ``"unknown"`` is the
    safe fallback when git isn't available or the package isn't in a git
    working tree.
    """

    def test_returns_string(self) -> None:
        from ralph_loop.cli import _resolve_commit_sha

        sha = _resolve_commit_sha()
        assert isinstance(sha, str)
        assert sha

    def test_is_sha_or_unknown(self) -> None:
        """Return value should be ``unknown``, a short SHA, or ``<sha>+dirty``."""
        from ralph_loop.cli import _resolve_commit_sha

        sha = _resolve_commit_sha()
        if sha == "unknown":
            return
        core = sha.removesuffix("+dirty")
        # Short git SHA is 7+ hex chars by default; ``--short`` on modern
        # git can return 7-10 depending on repo collisions.
        assert 4 <= len(core) <= 40
        assert all(c in "0123456789abcdef" for c in core)

    def test_does_not_raise_when_git_missing(
        self, monkeypatch: Any
    ) -> None:
        """When subprocess can't find git, the helper returns ``unknown``."""
        import subprocess as _subproc

        from ralph_loop import cli as cli_mod

        def fake_run(*_args: Any, **_kwargs: Any) -> None:
            raise FileNotFoundError("git not on PATH")

        monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
        assert cli_mod._resolve_commit_sha() == "unknown"


# ---------------------------------------------------------------------------
# Integration: graceful Invocation_Error continues the loop (Task 5.1)
# ---------------------------------------------------------------------------


class _StubClaudeCodeInvoker:
    """Script-driven stand-in for :class:`ralph_loop.kiro.ClaudeCodeInvoker`.

    Tracks how many times ``invoke`` has been called for each task so
    the integration test can assert per-task call counts. Detects the
    executing task by scanning the composed context for the
    ``"# Task Spec (id=<task_id>, title=..."`` header that
    :func:`compose_context` emits. The first invocation for a task id
    in ``raise_on_first_for`` raises the mapped exception; every other
    invocation returns a passing :class:`ClaudeInvocationResult`.
    """

    def __init__(
        self,
        *,
        raise_on_first_for: dict[str, BaseException],
    ) -> None:
        # Stored keyword-only so tests can inspect the contract easily.
        self._raise_on_first_for = dict(raise_on_first_for)
        self.calls_by_task_id: dict[str, int] = {}

    async def invoke(
        self,
        *,
        context: str,
        log_path: "Path",
        call_kind: str,
        timeout_ms: "int | None" = None,
        cwd: "Path | None" = None,
        stdout_sink: Any = None,
        model_id: "str | None" = None,
    ) -> Any:
        from ralph_loop.models import ClaudeInvocationResult

        # Parse the executing task id from the composed context. The
        # context composer renders ``# Task Spec (id=<id>, title=...)``
        # in every composed window, so a simple regex suffices.
        import re

        match = re.search(r"# Task Spec \(id=([^,]+),", context)
        task_id = match.group(1).strip() if match else "<unknown>"
        self.calls_by_task_id[task_id] = (
            self.calls_by_task_id.get(task_id, 0) + 1
        )

        # First call for a scripted task raises; subsequent calls pass.
        if (
            self.calls_by_task_id[task_id] == 1
            and task_id in self._raise_on_first_for
        ):
            raise self._raise_on_first_for[task_id]

        # Write a minimal line to the iteration log so the on-disk
        # layout matches what the real invoker would produce.
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"stub_invoker task_id={task_id}\n", encoding="utf-8"
        )
        return ClaudeInvocationResult(
            exit_code=0,
            stdout="",
            stderr="",
            token_usage=None,
            duration_ms=0,
        )


class _StubValidator:
    """Stand-in for :class:`ralph_loop.validator.Validator`.

    Returns ``overall="pass"`` for every call and records the sequence
    of ``task_id`` values it saw so the test can assert Validator was
    never called on task A (R1.9).
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls_by_task_id: dict[str, int] = {}

    async def run(
        self,
        *,
        task: Any,
        spec: Any,
        executing_persona_name: str,
        log_path: "Path",
        default_timeout_ms: int = 5 * 60 * 1000,
        cwd: "Path | None" = None,
    ) -> Any:
        from ralph_loop.models import CheckResult, ValidationResult

        self.calls_by_task_id[task.id] = (
            self.calls_by_task_id.get(task.id, 0) + 1
        )
        return ValidationResult(
            overall="pass",
            checks=[
                CheckResult(
                    type="shell",
                    name="stub_validator_pass",
                    verdict="pass",
                    output="ok",
                    duration_ms=0,
                )
            ],
            timed_out_checks=[],
        )


class _StubGitManager:
    """Stand-in for :class:`ralph_loop.git_manager.GitManager`.

    Records every ``iteration_commit`` call so the test can assert
    commits were skipped for the iteration where the handler fired
    (R1.9).
    """

    def __init__(self, *, enabled: bool, cwd: "Path") -> None:
        self.enabled = enabled
        self.cwd = cwd
        self.iteration_commit_calls: list[tuple[int, str, str, str]] = []

    def iteration_commit(
        self,
        *,
        iteration: int,
        task_id: str,
        persona_name: str,
        outcome: str,
    ) -> Any:
        from ralph_loop.models import CommitResult

        self.iteration_commit_calls.append(
            (iteration, task_id, persona_name, outcome)
        )
        return CommitResult(sha=None, skipped=True, skip_reason="stub")


def _write_min_spec(path: Path, task_id: str) -> None:
    """Write a minimal valid Task_Spec file.

    One ``shell`` check is declared so ``TaskSpec.validation`` is
    non-empty. The stub Validator short-circuits the check anyway.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"id: {task_id}\n"
        f"title: Task {task_id}\n"
        "validation:\n"
        "  - type: shell\n"
        "    name: stub-check\n"
        '    commands: ["true"]\n'
        "---\n"
        "## Objective\n"
        f"Do the work for {task_id}.\n\n"
        "## Context References\n"
        "None.\n\n"
        "## Instructions\n"
        "Produce the artefact.\n",
        encoding="utf-8",
    )


async def test_graceful_invocation_error_continues_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1.3, R1.6, R1.9, R3.1: an Invocation_Error on one task must not
    kill the loop.

    Walks the three-task scenario from the design's Testing Strategy
    section: tasks A, B, C all ``status=pending`` and ``priority=1``.
    The stub ClaudeCodeInvoker raises ``RuntimeError("chunk exceed the limit")``
    on A's first invocation and returns a passing ClaudeInvocationResult
    on B and C. With ``max_retries_per_task=1`` the retry cap is
    exhausted by A's first failure, so the loop proceeds through B and
    C and exits with ``EXIT_BLOCKED`` via the termination-decision path
    rather than ``EXIT_INVOCATION_ERROR``. Asserts the per-task
    call counts, the single ``iteration_invocation_error`` log record,
    and the final on-disk ``tasks.json`` state.
    """
    import json

    from structlog.testing import capture_logs

    from ralph_loop import cli as cli_mod
    from ralph_loop.cli import EXIT_BLOCKED, EXIT_INVOCATION_ERROR, EXIT_SUCCESS
    from ralph_loop.config import load_config

    # ------------------------------------------------------------------
    # 1. Scaffold a minimal project root the loader will accept (R15.9).
    # ------------------------------------------------------------------
    project = tmp_path / "project"
    project.mkdir()

    # SUMMARY.md (required per R15.9).
    (project / "SUMMARY.md").write_text(
        "# Project\n\nMinimal brief for the integration test.\n",
        encoding="utf-8",
    )

    # Minimal persona registry: a single Writer persona that every
    # task targets via ``target_persona``. Explicit routing short-
    # circuits the Orchestrator (R4.1) so no LLM call is made during
    # persona selection.
    personas_dir = project / "personas"
    personas_dir.mkdir()
    (personas_dir / "writer.yaml").write_text(
        "name: Writer\n"
        "description: Drafts prose.\n"
        'prompt_template: "{{persona_name}} {{task_id}} {{task_title}}"\n',
        encoding="utf-8",
    )

    # Three task specs (A, B, C). Each declares one shell check so the
    # Pydantic validation floor (R18.1) is met; the stub Validator
    # short-circuits it.
    specs_dir = project / "specs"
    for tid in ("A", "B", "C"):
        _write_min_spec(specs_dir / f"{tid}.md", tid)

    # tasks.json: A, B, C all pending, priority 1, routed explicitly
    # to Writer so the Orchestrator takes the ``path="explicit"``
    # branch with no LLM call.
    tasks_payload = [
        {
            "id": tid,
            "title": f"Task {tid}",
            "priority": 1,
            "status": "pending",
            "spec_path": f"specs/{tid}.md",
            "retry_count": 0,
            "target_persona": "Writer",
        }
        for tid in ("A", "B", "C")
    ]
    (project / "tasks.json").write_text(
        json.dumps(tasks_payload), encoding="utf-8"
    )

    # Pending queue is empty on startup.
    (project / "pending_tasks.json").write_text("[]", encoding="utf-8")

    # ralph.config.json: set ``max_retries_per_task=1`` so A's single
    # failure exhausts its retry budget. That keeps the invoker call
    # count on A at exactly 1 and drives the loop toward
    # ``EXIT_BLOCKED`` via the termination-decision path. Disable git
    # integration so no real subprocess is spawned.
    config_payload = {
        "fallback_persona": "Writer",
        "max_iterations": 10,
        "max_retries_per_task": 1,
        "wall_clock_timeout_ms": 600_000,
        "git_integration_enabled": False,
        "automatic_planner": False,
    }
    (project / "ralph.config.json").write_text(
        json.dumps(config_payload), encoding="utf-8"
    )

    # ------------------------------------------------------------------
    # 2. Build stubs. The ClaudeCodeInvoker stub raises on A's first call,
    #    passes on B and C. The Validator stub returns ``overall="pass"``
    #    for any call. The GitManager stub records calls.
    # ------------------------------------------------------------------
    stub_invoker = _StubClaudeCodeInvoker(
        raise_on_first_for={
            "A": RuntimeError("chunk exceed the limit"),
        }
    )
    stub_validator = _StubValidator()
    stub_git = _StubGitManager(enabled=False, cwd=project)

    # The loop constructs ``ClaudeCodeInvoker(claude_cli_command=...)`` inline
    # at module top. Monkeypatch the import binding in ``ralph_loop.cli``
    # so the loop uses our stub. We return the pre-built stub instance
    # from the factory to preserve the call-count state across
    # re-constructions (the loop may build multiple ``ClaudeCodeInvoker``
    # instances across its component wiring).
    monkeypatch.setattr(
        cli_mod, "ClaudeCodeInvoker", lambda *args, **kwargs: stub_invoker
    )
    monkeypatch.setattr(
        cli_mod, "Validator", lambda *args, **kwargs: stub_validator
    )
    monkeypatch.setattr(
        cli_mod, "GitManager", lambda *args, **kwargs: stub_git
    )

    # ``_run_loop`` calls ``configure_logger`` at startup, which
    # overwrites structlog's active processor chain and therefore
    # neutralises ``structlog.testing.capture_logs``. Replace it with
    # a no-op so the capture list receives the handler's
    # ``iteration_invocation_error`` record (R1.2).
    monkeypatch.setattr(
        cli_mod, "configure_logger", lambda *args, **kwargs: None
    )

    # ------------------------------------------------------------------
    # 3. Run the loop end-to-end and capture structlog events.
    # ------------------------------------------------------------------
    config = load_config(project_root=project)

    with capture_logs() as logs:
        exit_code = await cli_mod._run_loop(config, project)

    # ------------------------------------------------------------------
    # 4. Assertions (Task 5.1).
    # ------------------------------------------------------------------

    # Exit code is termination-decision-driven, never the per-iteration
    # invocation-error code (R1.3, R1.6, Property 24).
    assert exit_code != EXIT_INVOCATION_ERROR
    assert exit_code in (EXIT_SUCCESS, EXIT_BLOCKED)

    # Final on-disk tasks.json: A failing/retry=1, B and C passing.
    reloaded = json.loads(
        (project / "tasks.json").read_text(encoding="utf-8")
    )
    by_id = {t["id"]: t for t in reloaded}
    assert by_id["A"]["status"] == "failing"
    assert by_id["A"]["retry_count"] == 1
    assert by_id["B"]["status"] == "passing"
    assert by_id["C"]["status"] == "passing"

    # Exactly one ``iteration_invocation_error`` record for A with
    # the chunk-limit marker set (R1.7).
    err_records = [
        r for r in logs if r.get("event") == "iteration_invocation_error"
    ]
    assert len(err_records) == 1, (
        f"expected exactly one iteration_invocation_error; got {err_records!r}"
    )
    record = err_records[0]
    assert record["task_id"] == "A"
    assert record["chunk_limit_detected"] is True
    assert record["failure_mode"] == "chunk_limit"

    # Validator was never called on A (handler skips validation, R1.9).
    assert stub_validator.calls_by_task_id.get("A", 0) == 0
    # B and C both validated at least once.
    assert stub_validator.calls_by_task_id.get("B", 0) >= 1
    assert stub_validator.calls_by_task_id.get("C", 0) >= 1

    # Git manager iteration-commit was never called for A's iteration
    # (handler skips the commit, R1.9).
    assert all(
        task_id != "A"
        for (_iter, task_id, _persona, _outcome) in stub_git.iteration_commit_calls
    ), (
        f"iteration_commit unexpectedly called for A: "
        f"{stub_git.iteration_commit_calls!r}"
    )

    # Invoker was called exactly once on A (no same-iteration retry,
    # and the retry cap exhaustion prevents A from being picked in
    # any later iteration).
    assert stub_invoker.calls_by_task_id.get("A") == 1, (
        f"expected exactly one invoke call on A; got "
        f"{stub_invoker.calls_by_task_id!r}"
    )
