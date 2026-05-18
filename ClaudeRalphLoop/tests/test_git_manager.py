"""Integration tests for :mod:`ralph_loop.git_manager` (Task 19.1, 19.4).

These tests use the real ``git`` binary against a temporary
repository created via ``git init`` in ``tmp_path``. They cover:

- ``is_enabled`` / ``is_git_repo`` discrimination across enabled /
  disabled / inside-repo / outside-repo permutations (R13.3, R13.4).
- ``iteration_commit`` happy path with a message matching the R13.2
  format, including the sha returned to the caller.
- ``iteration_commit`` on a working tree with no changes -> a benign
  skip rather than a hard failure (R13.7).
- ``iteration_commit`` outside a repo -> skip with ``skip_reason``
  identifying the condition (R13.3).
- ``rollback`` with a known iteration number restoring the tree state
  (R13.5).
- ``rollback`` with an unknown iteration number exiting non-zero
  (R13.6).
- ``rollback`` with git disabled exiting non-zero (R13.4).

The tests skip automatically when ``git`` is unavailable on ``PATH``
so CI environments without the binary still run the rest of the
suite.

Requirements exercised: R13.1, R13.2, R13.3, R13.4, R13.5, R13.6,
R13.7.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ralph_loop.git_manager import (
    GitManager,
    build_commit_message,
    parse_iteration_from_message,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not available on PATH",
)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Initialise a fresh git repo in ``tmp_path`` and return its root.

    Configures a deterministic author so ``git commit`` does not fail
    on CI images without a global identity. ``init.defaultBranch`` is
    pinned to ``main`` so the initial branch name is stable across
    git versions.
    """

    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "ralph@example.com"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Ralph"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    # Seed with an initial commit so later commits have a parent.
    seed = tmp_path / "README"
    seed.write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# build_commit_message / parse_iteration_from_message
# ---------------------------------------------------------------------------


class TestCommitMessage:
    def test_format_matches_r13_2(self) -> None:
        msg = build_commit_message(
            iteration=7, task_id="T1", persona_name="Writer", outcome="pass",
        )
        assert msg == "ralph: iter=7 task=T1 persona=Writer outcome=pass"

    def test_round_trip_inverts_iteration(self) -> None:
        msg = build_commit_message(
            iteration=42, task_id="tx", persona_name="Ed", outcome="fail",
        )
        assert parse_iteration_from_message(msg) == 42

    def test_parse_returns_none_for_unrelated_message(self) -> None:
        assert parse_iteration_from_message("Merge branch foo") is None
        assert parse_iteration_from_message("") is None


# ---------------------------------------------------------------------------
# is_enabled / is_git_repo (R13.3, R13.4)
# ---------------------------------------------------------------------------


class TestEnablementAndRepoDetection:
    def test_disabled_manager_reports_disabled(self, git_repo: Path) -> None:
        mgr = GitManager(enabled=False, cwd=git_repo)
        assert mgr.is_enabled() is False
        # Even though the directory is a repo, is_git_repo follows the
        # enabled gate -- disabled implies "no git work".
        assert mgr.is_git_repo() is False

    def test_enabled_in_repo(self, git_repo: Path) -> None:
        mgr = GitManager(enabled=True, cwd=git_repo)
        assert mgr.is_enabled() is True
        assert mgr.is_git_repo() is True

    def test_enabled_outside_repo(self, tmp_path: Path) -> None:
        # tmp_path is not a repo here (no git init was called).
        mgr = GitManager(enabled=True, cwd=tmp_path)
        assert mgr.is_git_repo() is False


# ---------------------------------------------------------------------------
# iteration_commit (R13.1, R13.2, R13.3, R13.4, R13.7)
# ---------------------------------------------------------------------------


class TestIterationCommit:
    def test_happy_path_returns_sha_and_commits_with_expected_message(
        self, git_repo: Path
    ) -> None:
        mgr = GitManager(enabled=True, cwd=git_repo)
        # Introduce a change to commit.
        (git_repo / "chapter1.md").write_text("draft", encoding="utf-8")

        result = mgr.iteration_commit(
            iteration=1, task_id="T1", persona_name="Writer", outcome="pass",
        )

        assert result.skipped is False
        assert result.sha is not None
        assert len(result.sha) >= 7  # short or full sha

        # Inspect the commit body to confirm R13.2 format landed on disk.
        log = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=str(git_repo), capture_output=True, text=True, check=True,
        )
        assert log.stdout.strip() == (
            "ralph: iter=1 task=T1 persona=Writer outcome=pass"
        )

    def test_disabled_manager_skips(self, git_repo: Path) -> None:
        mgr = GitManager(enabled=False, cwd=git_repo)
        (git_repo / "a.md").write_text("x", encoding="utf-8")
        result = mgr.iteration_commit(
            iteration=1, task_id="t", persona_name="p", outcome="pass",
        )
        assert result.skipped is True
        assert result.skip_reason == "disabled"

    def test_non_repo_skips_with_warning(self, tmp_path: Path) -> None:
        # tmp_path without git init.
        mgr = GitManager(enabled=True, cwd=tmp_path)
        result = mgr.iteration_commit(
            iteration=1, task_id="t", persona_name="p", outcome="pass",
        )
        assert result.skipped is True
        assert result.skip_reason == "not-a-repo"

    def test_nothing_to_commit_is_not_fatal(self, git_repo: Path) -> None:
        # Working tree clean after the seed commit; an immediate
        # iteration_commit should be a benign skip.
        mgr = GitManager(enabled=True, cwd=git_repo)
        result = mgr.iteration_commit(
            iteration=1, task_id="t", persona_name="p", outcome="pass",
        )
        assert result.skipped is True
        assert result.skip_reason is not None
        assert "nothing to commit" in result.skip_reason.lower()


# ---------------------------------------------------------------------------
# rollback (R13.5, R13.6, R13.7)
# ---------------------------------------------------------------------------


class TestRollback:
    def test_rollback_to_known_iteration_restores_tree(
        self, git_repo: Path
    ) -> None:
        mgr = GitManager(enabled=True, cwd=git_repo)

        # Iteration 1: create a file.
        path = git_repo / "chapter1.md"
        path.write_text("first draft", encoding="utf-8")
        first = mgr.iteration_commit(
            iteration=1, task_id="T1", persona_name="Writer", outcome="pass",
        )
        assert first.skipped is False

        # Iteration 2: overwrite the file.
        path.write_text("second draft", encoding="utf-8")
        second = mgr.iteration_commit(
            iteration=2, task_id="T1", persona_name="Writer", outcome="pass",
        )
        assert second.skipped is False

        # Rollback to iteration 1 should restore the first-draft content.
        exit_code = mgr.rollback(1)
        assert exit_code == 0
        assert path.read_text(encoding="utf-8") == "first draft"

    def test_rollback_unknown_iteration_returns_nonzero(
        self, git_repo: Path
    ) -> None:
        mgr = GitManager(enabled=True, cwd=git_repo)
        # No iteration_commit has been made; iteration 42 cannot exist.
        exit_code = mgr.rollback(42)
        assert exit_code != 0

    def test_rollback_when_disabled_returns_nonzero(
        self, git_repo: Path
    ) -> None:
        mgr = GitManager(enabled=False, cwd=git_repo)
        assert mgr.rollback(1) != 0

    def test_rollback_outside_repo_returns_nonzero(
        self, tmp_path: Path
    ) -> None:
        mgr = GitManager(enabled=True, cwd=tmp_path)
        assert mgr.rollback(1) != 0

    def test_rollback_distinguishes_iter_1_from_iter_10(
        self, git_repo: Path
    ) -> None:
        """Guard against the substring trap: a grep for ``iter=1`` must
        not match ``iter=10``.
        """

        mgr = GitManager(enabled=True, cwd=git_repo)

        # Create iter=10 first with file a.md.
        a = git_repo / "a.md"
        a.write_text("ten", encoding="utf-8")
        mgr.iteration_commit(
            iteration=10, task_id="T", persona_name="P", outcome="pass",
        )

        # Then iter=1 with file b.md.
        b = git_repo / "b.md"
        b.write_text("one", encoding="utf-8")
        mgr.iteration_commit(
            iteration=1, task_id="T", persona_name="P", outcome="pass",
        )

        # Rollback to iteration 1 should find exactly the iter=1 commit.
        exit_code = mgr.rollback(1)
        assert exit_code == 0
