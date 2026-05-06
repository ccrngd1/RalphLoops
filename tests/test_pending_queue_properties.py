"""Property-based tests for :class:`ralph_loop.pending_queue.PendingQueueManager`
(design Properties 20 and 21).

Property 20 (Pending-queue round trip):

    For any pending-queue file containing a mix of entries that pass
    schema + persona-existence validation and entries that fail one or
    both, ``PendingQueueManager.process_on_startup`` admits exactly the
    passing entries into ``tasks.json`` (stamping ``admitted_run_id``
    while preserving original creation metadata and ``spilled_run_id``),
    discards every failing entry with a logged reason, truncates the
    pending-queue file to ``[]``, and logs correct ``loaded``,
    ``admitted``, and ``discarded`` counts.

    Validates: Requirements 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.9, 9.10.

Property 21 (Pending-queue invalid JSON produces a fatal exit):

    For any string ``s`` that is not valid JSON, when
    ``pending_tasks.json`` contains ``s``,
    ``PendingQueueManager.process_on_startup`` exits with a non-zero
    exit code and an error message identifying the file path and the
    parse error.

    Validates: Requirements 9.11.
"""

# Feature: ralph-loop, Property 20 & 21: Pending queue round trip and fail-fast on invalid JSON

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ralph_loop.models import Persona, Task
from ralph_loop.pending_queue import PendingQueueError, PendingQueueManager
from ralph_loop.persona_registry import PersonaRegistry

from tests.strategies import task_strategy


# ---------------------------------------------------------------------------
# Registry helper
# ---------------------------------------------------------------------------


# A fixed pool of persona names used by the mixed-entry strategy. The
# property tests build a registry from a subset of this pool and then
# generate entries whose ``target_persona`` is drawn either from the
# same pool (valid) or from a deliberately-unknown name (invalid),
# exercising the persona-existence branch of R9.4/R9.6 systematically.
_KNOWN_PERSONAS: tuple[str, ...] = ("Writer", "Editor", "Reviewer")
_UNKNOWN_PERSONA = "__not_registered__"


def _registry(names: tuple[str, ...]) -> PersonaRegistry:
    personas = {
        name: Persona(
            name=name,
            description=f"{name} persona",
            prompt_template="template",
        )
        for name in names
    }
    return PersonaRegistry(personas)


# ---------------------------------------------------------------------------
# Strategies for Property 20
# ---------------------------------------------------------------------------


@st.composite
def valid_pending_entry_strategy(draw) -> dict[str, Any]:
    """Generate a valid ``Task`` dict suitable for admission.

    The entry's ``target_persona`` is either absent (drawn as ``None``)
    or drawn from ``_KNOWN_PERSONAS`` so the registry populated by the
    test will always contain a match. ``spilled_run_id``,
    ``created_at_iteration``, and ``created_by_persona`` are drawn
    non-None so the property can assert they survive admission
    unchanged.
    """
    task = draw(task_strategy())
    # Rebuild with pinned provenance fields so Property 20 can assert
    # they round-trip unchanged. ``model_copy`` preserves every other
    # field on the generated task.
    target = draw(st.one_of(st.none(), st.sampled_from(_KNOWN_PERSONAS)))
    populated = task.model_copy(
        update={
            "target_persona": target,
            "spilled_run_id": draw(st.text(min_size=1, max_size=8)),
            "created_at_iteration": draw(st.integers(min_value=0, max_value=100)),
            "created_by_persona": draw(
                st.sampled_from(_KNOWN_PERSONAS + ("Legacy",))
            ),
            # Ensure admitted_run_id is cleared on input (the queue
            # should only ever hold un-admitted entries).
            "admitted_run_id": None,
        }
    )
    return populated.model_dump(mode="json")


@st.composite
def schema_invalid_entry_strategy(draw) -> dict[str, Any]:
    """Generate a dict that fails :class:`Task` Pydantic validation.

    Hypothesis picks one of three distinct failure modes: missing a
    required field, a wrong-typed required field, or a negative
    ``retry_count``. Each mode exercises a different branch of
    ``Task.model_validate``.
    """
    mode = draw(st.sampled_from(("missing_required", "wrong_type", "negative_retry")))
    base = {
        "id": draw(st.text(alphabet="abcdef0123", min_size=1, max_size=4)),
        "title": "t",
        "priority": 1,
        "status": "pending",
        "spec_path": "specs/x.md",
        "retry_count": 0,
    }
    if mode == "missing_required":
        # Drop a required field.
        field = draw(
            st.sampled_from(("title", "priority", "status", "spec_path"))
        )
        base.pop(field)
    elif mode == "wrong_type":
        # Replace a typed field with a string the model will reject.
        base["priority"] = "not-an-int"
    else:  # negative_retry
        base["retry_count"] = -1
    return base


@st.composite
def unknown_persona_entry_strategy(draw) -> dict[str, Any]:
    """Generate a schema-valid entry whose ``target_persona`` is absent
    from the registry."""
    task = draw(task_strategy())
    populated = task.model_copy(
        update={
            "target_persona": _UNKNOWN_PERSONA,
            "admitted_run_id": None,
        }
    )
    return populated.model_dump(mode="json")


# Strategy for a single mixed entry: one of three buckets. Using
# ``one_of`` (rather than three separate generators) lets Hypothesis
# pick the distribution dynamically per draw, so the generator covers
# lists that are all-valid, all-invalid, and every mix in between
# without additional orchestration.
pending_queue_entry_strategy = st.one_of(
    valid_pending_entry_strategy(),
    schema_invalid_entry_strategy(),
    unknown_persona_entry_strategy(),
)


# ---------------------------------------------------------------------------
# Property 20: pending-queue round trip
# ---------------------------------------------------------------------------


@given(
    entries=st.lists(pending_queue_entry_strategy, min_size=0, max_size=8),
    run_id=st.text(alphabet="abcdef0123", min_size=1, max_size=8),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_pending_queue_round_trip(
    tmp_path_factory: pytest.TempPathFactory,
    entries: list[dict[str, Any]],
    run_id: str,
) -> None:
    """Validates: Requirements 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.9, 9.10.

    For a list of mixed valid/invalid pending-queue entries:

    - ``loaded`` equals the input length (R9.10).
    - Every entry that passes schema validation AND whose
      ``target_persona`` is either absent or present in the registry
      is admitted (R9.3, R9.4, R9.5). Admitted tasks have
      ``admitted_run_id == run_id`` and preserve the original
      ``spilled_run_id`` and ``created_*`` provenance fields (R9.7).
    - Every other entry is discarded with a non-empty reason (R9.6).
    - ``len(admitted) + len(discarded) == loaded``.
    - The pending-queue file is truncated to exactly ``[]`` (R9.9).
    """
    # Fresh temp dir per draw so the file-system state never leaks
    # between Hypothesis examples.
    tmp_path = tmp_path_factory.mktemp("pending_queue")
    queue = tmp_path / "pending_tasks.json"
    queue.write_text(json.dumps(entries), encoding="utf-8")

    registry = _registry(_KNOWN_PERSONAS)
    manager = PendingQueueManager(queue, registry, run_id=run_id)

    result = manager.process_on_startup()

    # R9.10: loaded count equals the input length.
    assert result.loaded == len(entries)
    # Partition invariant: every entry lands in exactly one bucket.
    assert len(result.admitted) + len(result.discarded) == result.loaded

    # Re-derive the expected partition from the inputs. An entry
    # admits iff it parses as a Task AND either has no target_persona
    # or its target_persona is in the registry.
    expected_admitted_ids: list[str] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        try:
            task = Task.model_validate(raw)
        except Exception:
            continue
        if (
            task.target_persona is not None
            and registry.get(task.target_persona) is None
        ):
            continue
        expected_admitted_ids.append(task.id)

    # Order is preserved across partition: the admitted list is the
    # subsequence of inputs that passed both checks.
    actual_admitted_ids = [t.id for t in result.admitted]
    assert actual_admitted_ids == expected_admitted_ids

    # R9.7: every admitted task carries the current run id and
    # preserves its spilled_run_id + creation metadata from the input.
    # We re-validate the input (skipping invalid ones) to cross-check.
    admitted_index = 0
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        try:
            input_task = Task.model_validate(raw)
        except Exception:
            continue
        if (
            input_task.target_persona is not None
            and registry.get(input_task.target_persona) is None
        ):
            continue
        admitted = result.admitted[admitted_index]
        admitted_index += 1
        assert admitted.admitted_run_id == run_id
        assert admitted.spilled_run_id == input_task.spilled_run_id
        assert admitted.created_at_iteration == input_task.created_at_iteration
        assert admitted.created_by_persona == input_task.created_by_persona

    # R9.6: every discarded entry has a non-empty reason string.
    for entry in result.discarded:
        assert entry.reason
        assert isinstance(entry.reason, str)

    # R9.9: the queue file is atomically truncated to ``[]``.
    assert queue.read_text(encoding="utf-8") == "[]"


# ---------------------------------------------------------------------------
# Strategies for Property 21
# ---------------------------------------------------------------------------


def _is_valid_json(s: str) -> bool:
    try:
        json.loads(s)
    except json.JSONDecodeError:
        return False
    return True


# Text that almost certainly fails ``json.loads``. We filter out two
# classes of strings so Property 21's invariant is exercised only on
# genuinely-unparseable inputs:
#
# 1. Strings that parse as valid JSON (``""``, bare numbers, ``null``,
#    ``true``, ``{}``, and so on).
# 2. Strings that are empty or whitespace-only. ``process_on_startup``
#    gracefully treats an empty-or-whitespace queue file as an empty
#    queue (see the ``test_whitespace_only_file_returns_empty_result``
#    unit test) so that Windows editors that "touch" the file with a
#    trailing newline don't trip R9.11 on that benign case. R9.11
#    targets content that is actually garbage, not whitespace.
#
# The filter keeps shrunk counterexamples short and maps directly to
# the R9.11 surface area.
invalid_json_strategy = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),  # exclude surrogates
        blacklist_characters="\x00",   # exclude the null byte
    ),
    min_size=1,
    max_size=40,
).filter(lambda s: s.strip() != "" and not _is_valid_json(s))


# ---------------------------------------------------------------------------
# Property 21: invalid JSON fails fast
# ---------------------------------------------------------------------------


@given(raw=invalid_json_strategy)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_pending_queue_invalid_json_raises_pending_queue_error(
    tmp_path_factory: pytest.TempPathFactory,
    raw: str,
) -> None:
    """Validates: Requirements 9.11.

    For any non-JSON string ``raw``, writing it to the pending-queue
    file and calling ``process_on_startup`` raises
    :class:`PendingQueueError`. The raised message identifies the
    file path, and the exception chains from the underlying
    :class:`json.JSONDecodeError` so downstream handlers can report
    the parse error to the operator.
    """
    tmp_path = tmp_path_factory.mktemp("pending_queue_invalid")
    queue = tmp_path / "pending_tasks.json"
    queue.write_text(raw, encoding="utf-8")

    manager = PendingQueueManager(
        queue, _registry(_KNOWN_PERSONAS), run_id="r"
    )

    with pytest.raises(PendingQueueError) as excinfo:
        manager.process_on_startup()

    # The error message must identify the queue file so operators
    # know which file to edit or delete to unblock the run.
    assert str(queue) in str(excinfo.value)
    # The exception chains from the underlying JSONDecodeError so
    # callers can drill into the parse error if needed.
    assert excinfo.value.__cause__ is not None
    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)
