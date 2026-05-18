"""Property-based tests for :mod:`ralph_loop.git_manager` (Tasks 19.2, 19.3).

Two properties from ``design.md`` land here:

- Property 29 (iteration commit message format): for any (iteration,
  task id, persona name, outcome), :func:`build_commit_message`
  produces a string that (a) matches the exact R13.2 shape
  ``ralph: iter=<N> task=<id> persona=<name> outcome=<outcome>`` and
  (b) can be round-tripped through
  :func:`parse_iteration_from_message` to recover the original
  iteration number. This is what makes rollback-by-iteration work.

- Property 30 (rollback to unknown iteration): for any iteration
  number `n` for which the git log contains no matching
  ``Iteration_Commit``, :meth:`GitManager.rollback` returns a
  non-zero exit code (R13.6).

Requirements validated: 13.2, 13.6.
"""

# Feature: ralph-loop, Property 29 & 30: Iteration commit message format & rollback to unknown iteration

from __future__ import annotations

import shutil
import string
import subprocess
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from ralph_loop.git_manager import (
    GitManager,
    build_commit_message,
    parse_iteration_from_message,
)


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------


# The outcome alphabet is fixed by R13.2: the five values the loop can
# emit. Drawing from the literal keeps the generator tight.
_OUTCOME_POOL = ("pass", "fail", "stuck", "escalated", "timeout")

# ID / name alphabet kept URL-safe and without spaces; spaces would
# break the parseable field-separation rule in R13.2. The Ralph Loop
# generates ids and persona names from a similar alphabet so this
# mirrors production inputs.
_ID_ALPHABET = string.ascii_letters + string.digits + "_-"

iteration_strategy = st.integers(min_value=-100, max_value=10_000)
"""Iteration numbers. Negative values are unusual but not invalid per
the R13.2 grammar; keeping them in play exposes any formatter that
silently clamps to non-negative integers."""

task_id_strategy = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=10)
persona_name_strategy = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=10)
outcome_strategy = st.sampled_from(_OUTCOME_POOL)


# ---------------------------------------------------------------------------
# Property 29: commit message format round-trip
# ---------------------------------------------------------------------------


@given(
    iteration=iteration_strategy,
    task_id=task_id_strategy,
    persona_name=persona_name_strategy,
    outcome=outcome_strategy,
)
def test_commit_message_format_round_trips_iteration(
    iteration: int,
    task_id: str,
    persona_name: str,
    outcome: str,
) -> None:
    """Property 29: ``build_commit_message`` always produces an invertible message.

    Replicated shape: ``ralph: iter=<N> task=<id> persona=<name>
    outcome=<outcome>`` (R13.2). We verify three invariants:

    1. The message starts with ``ralph: `` and contains each of the
       four input fields verbatim in the expected order.
    2. :func:`parse_iteration_from_message` inverts the formatter and
       returns the original iteration number.
    3. The message has no trailing whitespace or newline that could
       upset ``git log --format=%s`` comparisons on Windows (where a
       trailing ``\\r`` is a common accident).
    """

    message = build_commit_message(
        iteration=iteration,
        task_id=task_id,
        persona_name=persona_name,
        outcome=outcome,
    )

    assert message.startswith("ralph: ")
    assert f"iter={iteration}" in message
    assert f"task={task_id}" in message
    assert f"persona={persona_name}" in message
    assert f"outcome={outcome}" in message
    # R13.2 ordering: iter, task, persona, outcome.
    i_pos = message.index("iter=")
    t_pos = message.index("task=")
    p_pos = message.index("persona=")
    o_pos = message.index("outcome=")
    assert i_pos < t_pos < p_pos < o_pos

    # No accidental surrounding whitespace.
    assert message == message.strip()

    # Round-trip the iteration.
    assert parse_iteration_from_message(message) == iteration


# ---------------------------------------------------------------------------
# Property 30: rollback to unknown iteration exits non-zero
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available on PATH",
)
@given(iteration=st.integers(min_value=-10_000, max_value=10_000))
@settings(max_examples=25, deadline=None)
def test_rollback_to_unknown_iteration_returns_nonzero(
    iteration: int, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Property 30: no matching commit -> non-zero return from rollback.

    We materialise a brand-new repo per example (via
    ``tmp_path_factory``) so each Hypothesis example is independent.
    The seed commit's message is fixed and does not match the R13.2
    ``ralph:`` prefix, so the ``git log --grep`` branch always comes
    back empty and :meth:`GitManager.rollback` must fall into the
    "unknown iteration" path (R13.6).
    """

    root = tmp_path_factory.mktemp("repo")
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=str(root), check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "ralph@example.com"],
        cwd=str(root), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Ralph"],
        cwd=str(root), check=True, capture_output=True,
    )
    (root / "README").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-A"], cwd=str(root), check=True, capture_output=True,
    )
    # Seed commit message deliberately has no ``ralph:`` prefix.
    subprocess.run(
        ["git", "commit", "-m", "initial seed not-a-ralph-commit"],
        cwd=str(root), check=True, capture_output=True,
    )

    mgr = GitManager(enabled=True, cwd=root)
    exit_code = mgr.rollback(iteration)
    assert exit_code != 0
