"""Property-based tests for :mod:`ralph_loop.task_creation`.

Three design properties land here:

- **Property 17** (R8.4, R8.7, R8.12, R17.6): the new-entry validation
  pipeline rejects any invalid candidate and never spills it. Because
  :func:`validate_new_entry` is the pure helper underneath both the
  post-iteration Task_Creation_Processor and the Planner, we exercise
  it directly and assert that every rejection carries a non-empty
  reason and that ``(task, reason)`` never simultaneously hold
  non-None values.

- **Property 18** (R8.8): any modification or deletion of a task whose
  ``id != executing_task_id`` must be reverted. We build pre/post
  snapshot pairs directly (bypassing the existing composite
  :func:`pre_and_post_snapshot_strategy`) so the generator can pick
  the executing id deterministically and guarantee at least one
  unauthorized edit or delete per example.

- **Property 19** (R8.10, R8.11, R8.13, R9.8): the budget + spill
  invariant. A stateful ``RuleBasedStateMachine`` drives
  :func:`admit_or_spill_new_tasks` with a sequence of batches and
  occasional iteration boundaries, and the invariants assert that
  per-iteration and per-run admissions never exceed their caps and
  that every spilled task preserves its creation metadata when the
  processor stamps it on write.
"""

# Feature: ralph-loop, Property 17/18/19: New-entry validation, revert, and budget-spill invariant

from __future__ import annotations

import string
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from ralph_loop.budget import BudgetTracker
from ralph_loop.models import Config, Persona, Task
from ralph_loop.persona_registry import PersonaRegistry
from ralph_loop.snapshot_diff import diff_snapshots
from ralph_loop.task_creation import (
    admit_or_spill_new_tasks,
    revert_unauthorized_edits,
    validate_new_entry,
)

from tests.strategies import task_id_strategy, task_strategy


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


_KNOWN_PERSONAS: tuple[str, ...] = ("Writer", "Editor", "Reviewer")
_UNKNOWN_PERSONA = "__ghost__"


def _registry(names: tuple[str, ...] = _KNOWN_PERSONAS) -> PersonaRegistry:
    personas = {
        n: Persona(name=n, description=f"{n} persona", prompt_template="t")
        for n in names
    }
    return PersonaRegistry(personas)


def _dump(task: Task) -> dict[str, Any]:
    return task.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Property 17: New-entry validation pipeline
# ---------------------------------------------------------------------------


@st.composite
def _valid_entry_strategy(draw) -> dict[str, Any]:
    """Valid ``Task`` dict. ``target_persona`` is either absent or in
    the known pool so it cannot trip the persona check."""
    task = draw(task_strategy())
    target = draw(st.one_of(st.none(), st.sampled_from(_KNOWN_PERSONAS)))
    # ``creation_chain`` is drawn within the default depth cap of 5.
    chain = draw(
        st.one_of(
            st.none(),
            st.lists(task_id_strategy, min_size=0, max_size=5),
        )
    )
    return task.model_copy(
        update={"target_persona": target, "creation_chain": chain}
    ).model_dump(mode="json")


@st.composite
def _schema_invalid_entry_strategy(draw) -> dict[str, Any]:
    """Dict that fails :class:`Task` schema validation."""
    base = {
        "id": draw(task_id_strategy),
        "title": "t",
        "priority": 1,
        "status": "pending",
        "spec_path": "specs/x.md",
        "retry_count": 0,
    }
    mode = draw(
        st.sampled_from(
            ("missing_required", "wrong_type", "negative_retry", "bad_status")
        )
    )
    if mode == "missing_required":
        base.pop(
            draw(st.sampled_from(("title", "priority", "status", "spec_path")))
        )
    elif mode == "wrong_type":
        base["priority"] = "not-an-int"
    elif mode == "negative_retry":
        base["retry_count"] = -1
    else:  # bad_status
        base["status"] = "not-a-status"
    return base


@st.composite
def _unknown_persona_entry_strategy(draw) -> dict[str, Any]:
    """Schema-valid entry whose ``target_persona`` is not in the registry."""
    task = draw(task_strategy())
    return task.model_copy(
        update={"target_persona": _UNKNOWN_PERSONA}
    ).model_dump(mode="json")


@st.composite
def _deep_chain_entry_strategy(draw) -> dict[str, Any]:
    """Schema-valid entry whose ``creation_chain`` is deeper than the cap."""
    task = draw(task_strategy())
    # The test fixes the cap at 5, so generate a chain of at least 6 entries.
    chain = draw(
        st.lists(task_id_strategy, min_size=6, max_size=10)
    )
    return task.model_copy(
        update={"creation_chain": chain, "target_persona": None}
    ).model_dump(mode="json")


_candidate_entry_strategy = st.one_of(
    _valid_entry_strategy(),
    _schema_invalid_entry_strategy(),
    _unknown_persona_entry_strategy(),
    _deep_chain_entry_strategy(),
)


@given(entry=_candidate_entry_strategy)
def test_validate_new_entry_rejection_is_total(entry: dict[str, Any]) -> None:
    """Validates: Requirements 8.4, 8.7, 8.12, 17.6.

    For any candidate dict drawn from the mixed-shape strategy, the
    return shape is an *exclusive* disjunction:

    - ``(Task, None)`` on admission, or
    - ``(None, reason)`` on rejection where ``reason`` is non-empty.

    The "reject without spilling" half of Property 17 falls out of
    the test's structure: :func:`validate_new_entry` does not touch
    the pending queue at all, so any rejected entry cannot possibly
    be spilled from this code path.
    """
    registry = _registry()
    task, reason = validate_new_entry(
        entry, registry=registry, max_creation_chain_depth=5
    )

    # Exclusive disjunction on the return.
    assert (task is None) != (reason is None)

    if task is None:
        # Reason is the rejection signal; must be a non-empty string.
        assert isinstance(reason, str)
        assert reason

        # Derive the expected rejection cause from the entry and check
        # the returned reason names the right cause. This prevents the
        # function from silently misclassifying (e.g. reporting a
        # schema error when the real problem is an unknown persona).
        from pydantic import ValidationError

        try:
            candidate = Task.model_validate(entry)
            schema_ok = True
        except ValidationError:
            schema_ok = False

        if not schema_ok:
            assert "schema" in reason.lower()
        elif (
            candidate.target_persona is not None
            and registry.get(candidate.target_persona) is None
        ):
            assert candidate.target_persona in reason
        else:
            # Only remaining rejection cause in this strategy is chain depth.
            assert "creation_chain" in reason
    else:
        # Admission: no reason, and the task round-trips.
        assert reason is None
        # The returned Task is the validated form of the input entry.
        assert task.id == entry["id"]


# ---------------------------------------------------------------------------
# Property 18: Revert of non-executing task edits
# ---------------------------------------------------------------------------


@st.composite
def _pre_and_unauthorized_edits_strategy(
    draw,
) -> tuple[list[Task], list[dict[str, Any]], str]:
    """Build a (pre, post, executing_task_id) scenario with at least one
    unauthorized mod or delete.

    The strategy draws:

    1. A list of 2-6 pre-tasks with unique ids.
    2. An executing task id drawn from those ids.
    3. A per-non-executing-task action in ``{"keep", "modify", "delete"}``
       with ``"keep"`` excluded for at least one non-executing task so
       Property 18's precondition ("post modifies or deletes some
       non-executing task") always holds.

    Executing-task post state is always either ``"keep"`` or an
    authorized self-modification (status flip to ``"passing"``). That
    lets the property assert that the executing entry is never in
    ``reverted`` even when the persona edits its own row.
    """
    n = draw(st.integers(min_value=2, max_value=6))
    ids = draw(
        st.lists(task_id_strategy, min_size=n, max_size=n, unique=True)
    )

    pre_tasks = [draw(task_strategy(id_=tid)) for tid in ids]

    executing_id = draw(st.sampled_from(ids))
    non_executing_ids = [tid for tid in ids if tid != executing_id]

    # Draw one action per non-executing task. At least one must be
    # non-keep so the property's precondition is satisfied.
    action_strategy = st.sampled_from(("keep", "modify", "delete"))
    non_exec_actions = draw(
        st.lists(
            action_strategy,
            min_size=len(non_executing_ids),
            max_size=len(non_executing_ids),
        ).filter(lambda actions: any(a != "keep" for a in actions))
    )

    # Executing-task action: keep or authorized self-modify.
    exec_action = draw(st.sampled_from(("keep", "self_modify")))

    post: list[dict[str, Any]] = []
    actions_by_id = dict(zip(non_executing_ids, non_exec_actions))
    for pre_task in pre_tasks:
        if pre_task.id == executing_id:
            if exec_action == "keep":
                post.append(_dump(pre_task))
            else:
                dump = _dump(pre_task)
                dump["status"] = "passing"
                post.append(dump)
            continue

        action = actions_by_id[pre_task.id]
        if action == "keep":
            post.append(_dump(pre_task))
        elif action == "modify":
            dump = _dump(pre_task)
            # Bump priority so the modified dict differs from pre.
            dump["priority"] = pre_task.priority + draw(
                st.integers(min_value=1, max_value=5)
            )
            post.append(dump)
        # "delete": omit from post

    return pre_tasks, post, executing_id


@given(scenario=_pre_and_unauthorized_edits_strategy())
@settings(suppress_health_check=[HealthCheck.filter_too_much])
def test_unauthorized_edits_are_reverted(
    scenario: tuple[list[Task], list[dict[str, Any]], str],
) -> None:
    """Validates: Requirements 8.8.

    For any scenario where the post snapshot modifies or deletes at
    least one non-executing task:

    - Every non-executing modification or deletion appears in the
      ``reverted`` list with the matching reason.
    - The executing task never appears in ``reverted`` (even when it
      self-edits).
    - The merged list restores the pre state for every reverted task,
      keying by id.
    - The merged list preserves every pre id (no data loss from
      reverts).
    """
    pre_tasks, post, executing_id = scenario
    diff = diff_snapshots(pre_tasks, post)

    merged, reverted = revert_unauthorized_edits(
        pre_tasks,
        diff,
        executing_task_id=executing_id,
        iteration=1,
        acting_persona="Writer",
    )

    # Derive the expected set of reverted (id, reason) pairs from the
    # diff. Every modified-non-executing entry is reverted. Every
    # deleted entry (including self-deletion) is reverted.
    expected: list[tuple[str, str]] = []
    for pre_dump, _post_dump in diff.modified:
        if pre_dump["id"] != executing_id:
            expected.append((pre_dump["id"], "modified"))
    for pre_task in diff.deleted:
        expected.append((pre_task.id, "deleted"))

    actual = [(r.task_id, r.reason) for r in reverted]
    assert sorted(actual) == sorted(expected)

    # Executing task's self-modification never lands in the reverted bucket.
    assert all(r.task_id != executing_id or r.reason == "deleted" for r in reverted)

    # Merged list still contains every pre id (reverts restore, they
    # don't drop entries).
    merged_ids = {t.id for t in merged}
    assert merged_ids >= {t.id for t in pre_tasks}

    # For every reverted non-executing task, the merged state equals the pre state.
    pre_by_id = {t.id: t for t in pre_tasks}
    merged_by_id = {t.id: t for t in merged}
    for r in reverted:
        if r.task_id != executing_id:
            assert merged_by_id[r.task_id] == pre_by_id[r.task_id]


# ---------------------------------------------------------------------------
# Property 19: Stateful budget + spill invariant
# ---------------------------------------------------------------------------


_ALPHABET = string.ascii_lowercase + string.digits


def _state_task(tid: str, persona: str) -> Task:
    """Build a Task with enough pre-creation metadata for the stateful
    test to assert preservation after stamping."""
    return Task(
        id=tid,
        title=f"task {tid}",
        priority=0,
        status="pending",
        spec_path=f"specs/{tid}.md",
        retry_count=0,
        # Prepopulate a creation_chain so we can check it survives
        # admit/spill unchanged.
        creation_chain=[persona],
    )


class BudgetSpillStateMachine(RuleBasedStateMachine):
    """Stateful model of admit/spill over a sequence of batches.

    The machine uses a fixed :class:`Config` (per-iteration cap 3,
    per-run cap 8) so the search space is small and the shrinker
    converges quickly on minimal counterexamples. One
    :class:`BudgetTracker` backs the whole run; ``record_iteration``
    resets the per-iteration counter (R10.6).

    Rules:

    - :meth:`new_iteration` bumps the iteration and resets the
      per-iteration cap.
    - :meth:`new_batch` calls :func:`admit_or_spill_new_tasks` with a
      batch of 1-5 fresh tasks and accumulates the accepted / spilled
      results in shadow totals.

    Invariants:

    - ``per_iteration_created <= Bi`` (R10.6 / R8.10).
    - ``per_run_created <= Br`` (R10.7 / R8.11).
    - Every spilled task from the machine preserves its
      ``creation_chain`` and id (R8.13 inputs survive the call).
    """

    _PER_ITERATION = 3
    _PER_RUN = 8

    def __init__(self) -> None:
        super().__init__()
        self._config = Config(
            fallback_persona="Writer",
            per_iteration_task_creation_budget=self._PER_ITERATION,
            per_run_task_creation_budget=self._PER_RUN,
        )
        self._tracker = BudgetTracker(self._config)
        self._tracker.record_iteration()
        self._iteration = 1
        self._persona = "Writer"
        # Shadow totals used by the invariants to cross-check the tracker.
        self._shadow_per_iter = 0
        self._shadow_per_run = 0
        # Seen ids so repeated batches don't accidentally reuse the same
        # id (which would be legal at the helper level but confuses
        # preservation checks).
        self._next_id = 0
        # Spilled tasks recorded for preservation invariants.
        self._spilled_observed: list[Task] = []
        # Accepted tasks recorded for stamping invariants.
        self._accepted_observed: list[Task] = []

    def _fresh_ids(self, n: int) -> list[str]:
        ids = [f"t{self._next_id + i}" for i in range(n)]
        self._next_id += n
        return ids

    @rule()
    def new_iteration(self) -> None:
        """Start a new iteration: reset per-iteration counter."""
        self._tracker.record_iteration()
        self._iteration += 1
        self._shadow_per_iter = 0

    @rule(count=st.integers(min_value=1, max_value=5))
    def new_batch(self, count: int) -> None:
        """Submit ``count`` fresh tasks to ``admit_or_spill_new_tasks``."""
        ids = self._fresh_ids(count)
        tasks = [_state_task(tid, self._persona) for tid in ids]

        accepted, spilled_pairs = admit_or_spill_new_tasks(
            tasks,
            budget=self._tracker,
            iteration=self._iteration,
            acting_persona=self._persona,
        )

        # Update shadow totals using the actual admission result, not
        # the batch size, so the invariants observe the same counts
        # the tracker advanced internally.
        self._shadow_per_iter += len(accepted)
        self._shadow_per_run += len(accepted)

        # Record observations for invariant checks. The helper returns
        # spilled tasks unstamped (stamping happens in the processor
        # before persisting), so ``task`` here is the as-submitted one.
        for task in accepted:
            self._accepted_observed.append(task)
        for task, _reason in spilled_pairs:
            self._spilled_observed.append(task)

    @invariant()
    def tracker_totals_match_shadow(self) -> None:
        """Tracker counters must match the shadow totals on every step."""
        assert self._tracker.per_iteration_created == self._shadow_per_iter
        assert self._tracker.per_run_created == self._shadow_per_run

    @invariant()
    def per_iteration_cap_respected(self) -> None:
        """R8.10 / R10.6: accepted per iteration <= per-iteration cap."""
        assert self._shadow_per_iter <= self._PER_ITERATION

    @invariant()
    def per_run_cap_respected(self) -> None:
        """R8.11 / R10.7: accepted per run <= per-run cap."""
        assert self._shadow_per_run <= self._PER_RUN

    @invariant()
    def spilled_preserve_creation_metadata(self) -> None:
        """R8.13: spilled tasks preserve their input creation_chain and id.

        ``admit_or_spill_new_tasks`` does not mutate inputs; it passes
        spilled tasks through untouched. The persona name we seeded
        onto ``creation_chain`` must survive unchanged on every
        spilled task.
        """
        for task in self._spilled_observed:
            assert task.creation_chain == [self._persona]
            # id must remain a string we seeded.
            assert task.id.startswith("t")

    @invariant()
    def accepted_are_stamped(self) -> None:
        """R8.5: accepted tasks carry the iteration and persona stamp."""
        for task in self._accepted_observed:
            assert task.created_at_iteration is not None
            assert task.created_at_iteration >= 1
            assert task.created_by_persona == self._persona


# pytest runner for the state machine. ``TestCase`` unwraps into a
# plain pytest test so the property integrates into the existing
# ``python -m pytest`` workflow without extra configuration.
TestBudgetSpillInvariant = BudgetSpillStateMachine.TestCase
TestBudgetSpillInvariant.settings = settings(
    max_examples=50,
    stateful_step_count=15,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)
