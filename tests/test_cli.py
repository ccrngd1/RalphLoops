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
