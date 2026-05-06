"""Git Manager: per-iteration commits and rollback (R13.1-R13.7).

The Git Manager is a thin wrapper around the ``git`` binary invoked
via :mod:`subprocess`. It is intentionally small: the loop only needs
five commands (``rev-parse``, ``add``, ``commit``, ``log --grep``,
``checkout``) and adding a richer library (``GitPython``) would
outweigh the convenience it buys us.

Behaviours by requirement:

- **R13.1** Create an Iteration_Commit after every iteration.
- **R13.2** Commit message format
  ``ralph: iter=<N> task=<id> persona=<name> outcome=<outcome>``.
- **R13.3** Non-repo -> log warning, skip, continue.
- **R13.4** Disabled -> skip, log at startup.
- **R13.5** ``rollback(n)`` grepping the iteration tag in commit
  messages and ``git checkout <sha> -- .`` to restore the tree.
- **R13.6** Unknown iteration -> non-zero exit code.
- **R13.7** Commit or checkout errors are logged but never fatal for
  the run loop; the CLI ``rollback`` subcommand maps non-zero returns
  from :meth:`GitManager.rollback` to a non-zero process exit.

The public surface is :class:`GitManager` plus the pure
:func:`build_commit_message` helper, which Property 29 exercises
directly.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from ralph_loop.models import CommitResult

logger = logging.getLogger(__name__)


# Compiled regex for parsing iteration numbers back out of commit
# messages (R13.5). The anchored ``^`` keeps it strict; Property 29
# exercises this inversion end to end.
_COMMIT_MSG_RE = re.compile(
    r"^ralph: iter=(?P<iter>-?\d+) task=(?P<task>\S+) persona=(?P<persona>\S+) outcome=(?P<outcome>\S+)$"
)


def build_commit_message(
    *,
    iteration: int,
    task_id: str,
    persona_name: str,
    outcome: str,
) -> str:
    """Format the Iteration_Commit message (R13.2).

    The exact shape is
    ``ralph: iter=<N> task=<id> persona=<name> outcome=<outcome>``.
    No other fields, no trailing whitespace. Property 29 inverts this
    format to recover the iteration number for rollback, so the format
    must stay stable and the four fields must appear in the order
    above.
    """

    return (
        f"ralph: iter={iteration} task={task_id} "
        f"persona={persona_name} outcome={outcome}"
    )


def parse_iteration_from_message(message: str) -> Optional[int]:
    """Return the iteration number embedded in a commit message.

    Returns ``None`` when the message does not match the iteration
    commit format. Property 29 uses this to assert that every well-
    formed :func:`build_commit_message` output can be inverted.
    """

    # ``git log`` may include a trailing newline in its body output.
    m = _COMMIT_MSG_RE.match(message.strip())
    if m is None:
        return None
    try:
        return int(m.group("iter"))
    except ValueError:
        return None


class GitManager:
    """Synchronous wrapper over the ``git`` binary (R13.1-R13.7).

    One instance is constructed per run. Construction is cheap -- no
    subprocesses are spawned -- so the loop can build it before
    discovering whether the working directory is a git repository.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        cwd: Path,
        git_executable: str = "git",
    ) -> None:
        self._enabled = bool(enabled)
        self._cwd = Path(cwd)
        self._git = git_executable

    # -- Queries -----------------------------------------------------

    def is_enabled(self) -> bool:
        """Return ``True`` when git integration is configured as enabled."""
        return self._enabled

    def is_git_repo(self) -> bool:
        """Return ``True`` iff the working directory is inside a git repo.

        Uses ``git rev-parse --is-inside-work-tree`` (R13.3). Any
        failure (missing binary, non-zero exit, OSError) returns
        ``False`` so the caller can treat the absence of a repo as
        non-fatal (R13.3, R13.7).
        """

        if not self._enabled:
            return False
        try:
            result = subprocess.run(
                [self._git, "rev-parse", "--is-inside-work-tree"],
                cwd=str(self._cwd),
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, FileNotFoundError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    # -- Commits -----------------------------------------------------

    def iteration_commit(
        self,
        *,
        iteration: int,
        task_id: str,
        persona_name: str,
        outcome: str,
    ) -> CommitResult:
        """Stage and commit every working-tree change for one iteration (R13.1).

        The message is built by :func:`build_commit_message`. Skip
        reasons are surfaced in the returned :class:`CommitResult`
        rather than via exceptions so the loop can keep running on
        repo-less / disabled / commit-failure paths (R13.3, R13.4,
        R13.7).
        """

        if not self._enabled:
            logger.info(
                "git_manager: skipping iteration commit (disabled) "
                "iteration=%d task=%s",
                iteration, task_id,
            )
            return CommitResult(skipped=True, skip_reason="disabled")

        if not self.is_git_repo():
            logger.warning(
                "git_manager: skipping iteration commit (not a git repo) "
                "iteration=%d task=%s",
                iteration, task_id,
            )
            return CommitResult(skipped=True, skip_reason="not-a-repo")

        message = build_commit_message(
            iteration=iteration,
            task_id=task_id,
            persona_name=persona_name,
            outcome=outcome,
        )

        # Stage everything (R13.1). A failure here is unlikely but
        # handled symmetrically with the commit failure below.
        try:
            add_result = subprocess.run(
                [self._git, "add", "-A"],
                cwd=str(self._cwd),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            logger.warning(
                "git_manager: git add failed iteration=%d: %s",
                iteration, exc,
            )
            return CommitResult(
                skipped=True,
                skip_reason=f"git add failed: {exc}",
            )

        if add_result.returncode != 0:
            logger.warning(
                "git_manager: git add returned %d iteration=%d stderr=%s",
                add_result.returncode, iteration, add_result.stderr.strip(),
            )
            return CommitResult(
                skipped=True,
                skip_reason=f"git add exit {add_result.returncode}",
            )

        try:
            commit_result = subprocess.run(
                [self._git, "commit", "-m", message],
                cwd=str(self._cwd),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            logger.warning(
                "git_manager: git commit failed iteration=%d: %s",
                iteration, exc,
            )
            return CommitResult(
                skipped=True,
                skip_reason=f"git commit failed: {exc}",
            )

        if commit_result.returncode != 0:
            # "nothing to commit" is a benign no-change iteration, not
            # a fatal error. Map it to a skip so the loop can proceed.
            combined = (commit_result.stdout + commit_result.stderr).lower()
            if "nothing to commit" in combined:
                logger.info(
                    "git_manager: nothing to commit iteration=%d task=%s",
                    iteration, task_id,
                )
                return CommitResult(
                    skipped=True,
                    skip_reason="nothing to commit",
                )
            logger.warning(
                "git_manager: git commit returned %d iteration=%d "
                "stdout=%s stderr=%s",
                commit_result.returncode, iteration,
                commit_result.stdout.strip(), commit_result.stderr.strip(),
            )
            return CommitResult(
                skipped=True,
                skip_reason=f"git commit exit {commit_result.returncode}",
            )

        # Resolve the new HEAD sha so the caller can log / reference
        # the commit.
        try:
            sha_result = subprocess.run(
                [self._git, "rev-parse", "HEAD"],
                cwd=str(self._cwd),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            logger.warning(
                "git_manager: git rev-parse HEAD failed iteration=%d: %s",
                iteration, exc,
            )
            return CommitResult(sha=None, skipped=False)

        sha = sha_result.stdout.strip() if sha_result.returncode == 0 else None
        logger.info(
            "git_manager: committed iteration=%d task=%s sha=%s",
            iteration, task_id, sha,
        )
        return CommitResult(sha=sha, skipped=False)

    # -- Rollback ----------------------------------------------------

    def rollback(self, iteration: int) -> int:
        """Restore the working tree to the commit for iteration ``iteration``.

        Returns ``0`` on success and a non-zero integer on failure
        (missing commit -> ``1``, any other error -> ``2``) so the
        CLI ``rollback`` subcommand can propagate the exit code
        directly (R13.6).

        Mechanics (R13.5):

        1. ``git log --grep="ralph: iter=N "`` with the trailing
           space anchored so we don't match ``iter=10`` when looking
           for ``iter=1``.
        2. If no commits are found, log an error and return 1.
        3. Otherwise use the first (most recent) sha and run
           ``git checkout <sha> -- .`` to restore the tree without
           detaching HEAD. Checkout errors return 2 (R13.7).
        """

        if not self._enabled:
            logger.error(
                "git_manager: rollback called while git integration is "
                "disabled iteration=%d",
                iteration,
            )
            return 2

        if not self.is_git_repo():
            logger.error(
                "git_manager: rollback called outside a git repository "
                "iteration=%d",
                iteration,
            )
            return 2

        # The trailing space in the grep pattern anchors the iteration
        # number so we don't match a prefix (``iter=1`` matching
        # ``iter=10``). ``--format=%H`` emits only the commit sha so we
        # don't have to parse the human-readable body.
        grep_pattern = f"ralph: iter={iteration} "
        try:
            log_result = subprocess.run(
                [
                    self._git, "log",
                    "--grep", grep_pattern,
                    "--format=%H",
                ],
                cwd=str(self._cwd),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            logger.error(
                "git_manager: git log failed during rollback iteration=%d: %s",
                iteration, exc,
            )
            return 2

        if log_result.returncode != 0:
            logger.error(
                "git_manager: git log returned %d during rollback "
                "iteration=%d stderr=%s",
                log_result.returncode, iteration,
                log_result.stderr.strip(),
            )
            return 2

        shas = [line.strip() for line in log_result.stdout.splitlines() if line.strip()]
        if not shas:
            logger.error(
                "git_manager: no Iteration_Commit found for iteration=%d",
                iteration,
            )
            return 1

        # Multiple commits for the same iteration should be rare but can
        # happen if a run is resumed. Use the most-recent one.
        sha = shas[0]
        try:
            checkout_result = subprocess.run(
                [self._git, "checkout", sha, "--", "."],
                cwd=str(self._cwd),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            logger.error(
                "git_manager: git checkout failed during rollback "
                "iteration=%d sha=%s: %s",
                iteration, sha, exc,
            )
            return 2

        if checkout_result.returncode != 0:
            logger.error(
                "git_manager: git checkout returned %d during rollback "
                "iteration=%d sha=%s stderr=%s",
                checkout_result.returncode, iteration, sha,
                checkout_result.stderr.strip(),
            )
            return 2

        logger.info(
            "git_manager: rolled back iteration=%d to sha=%s",
            iteration, sha,
        )
        return 0
