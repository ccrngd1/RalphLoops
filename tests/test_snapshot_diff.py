"""Unit tests for ``ralph_loop.snapshot_diff.diff_snapshots``.

Example-based coverage of the R8.2 / R8.3 snapshot-diff rule. The
property-based test for Property 16 (diff-based task-creation
detection) lives in task 6.2.

Requirements validated: R8.2 (pre/post snapshot comparison) and R8.3
(newly added entries classified as created).
"""

from __future__ import annotations

from typing import Any

from ralph_loop.models import Task
from ralph_loop.snapshot_diff import diff_snapshots


def _task(
    tid: str,
    *,
    priority: int = 0,
    status: str = "pending",
    retry_count: int = 0,
) -> Task:
    """Build a minimal ``Task`` for diff tests."""

    return Task(
        id=tid,
        title=f"task {tid}",
        priority=priority,
        status=status,  # type: ignore[arg-type]
        spec_path=f"specs/{tid}.md",
        retry_count=retry_count,
    )


def _dump(task: Task) -> dict[str, Any]:
    """Return the JSON-mode model dump used for on-disk equality checks."""

    return task.model_dump(mode="json")


def test_empty_pre_and_post_returns_empty_diff() -> None:
    """No snapshots, no diff."""

    diff = diff_snapshots([], [])

    assert diff.created == []
    assert diff.modified == []
    assert diff.deleted == []


def test_identical_pre_and_post_returns_empty_diff() -> None:
    """Round-tripping an unchanged list must not report any diff."""

    pre = [_task("a"), _task("b", priority=1)]
    post = [_dump(t) for t in pre]

    diff = diff_snapshots(pre, post)

    assert diff.created == []
    assert diff.modified == []
    assert diff.deleted == []


def test_new_task_in_post_is_created() -> None:
    """R8.3: an id present only in the post snapshot is ``created``."""

    pre = [_task("a")]
    new_entry = _dump(_task("b"))
    post = [_dump(pre[0]), new_entry]

    diff = diff_snapshots(pre, post)

    assert diff.created == [new_entry]
    assert diff.modified == []
    assert diff.deleted == []


def test_removed_task_is_deleted() -> None:
    """An id present only in the pre snapshot is ``deleted``."""

    pre = [_task("a"), _task("b")]
    post = [_dump(pre[0])]

    diff = diff_snapshots(pre, post)

    assert diff.created == []
    assert diff.modified == []
    assert [t.id for t in diff.deleted] == ["b"]


def test_modified_task_returns_pair() -> None:
    """Same id, different content -> a ``(pre_dump, post_dump)`` pair."""

    pre_task = _task("a", status="pending")
    pre = [pre_task]
    post_entry = _dump(pre_task)
    post_entry["status"] = "in_progress"

    diff = diff_snapshots(pre, [post_entry])

    assert diff.created == []
    assert diff.deleted == []
    assert diff.modified == [(_dump(pre_task), post_entry)]


def test_complex_mix_created_deleted_modified_unchanged() -> None:
    """One of each bucket plus an unchanged task."""

    keep = _task("keep")
    modify = _task("mod", priority=0)
    delete = _task("del")
    pre = [keep, modify, delete]

    modify_post = _dump(modify)
    modify_post["priority"] = 9
    create_post = _dump(_task("new"))
    post = [_dump(keep), modify_post, create_post]

    diff = diff_snapshots(pre, post)

    assert diff.created == [create_post]
    assert diff.modified == [(_dump(modify), modify_post)]
    assert [t.id for t in diff.deleted] == ["del"]


def test_post_entry_without_id_routed_to_created() -> None:
    """Raw post entries missing a usable id flow through ``created``.

    The downstream new-entry validation pipeline (R8.4) is responsible
    for rejecting them; the diff function simply must not drop them.
    """

    pre = [_task("a")]
    bad_entries: list[dict[str, Any]] = [
        {"title": "no id at all"},           # no id key
        {"id": "", "title": "empty id"},     # empty string id
        {"id": 42, "title": "int id"},       # wrong type
    ]
    post = [_dump(pre[0]), *bad_entries]

    diff = diff_snapshots(pre, post)

    # Every malformed entry appears exactly once in ``created``.
    assert len(diff.created) == len(bad_entries)
    for e in bad_entries:
        assert e in diff.created
    assert diff.modified == []
    assert diff.deleted == []


def test_non_dict_post_entry_is_wrapped_into_created() -> None:
    """Non-dict JSON values survive the diff and end up in ``created``."""

    pre: list[Task] = []
    post = ["not a dict", 123]  # type: ignore[list-item]

    diff = diff_snapshots(pre, post)  # type: ignore[arg-type]

    assert len(diff.created) == 2
    assert {"_invalid_raw": "not a dict"} in diff.created
    assert {"_invalid_raw": 123} in diff.created
    assert diff.modified == []
    assert diff.deleted == []


def test_inputs_are_not_mutated() -> None:
    """Purity: caller state is untouched."""

    pre_task = _task("a")
    pre = [pre_task]
    post_entry = _dump(pre_task)
    post_entry["priority"] = 17
    post = [post_entry]

    pre_before = [_dump(t) for t in pre]
    post_before = [dict(e) for e in post]

    diff_snapshots(pre, post)

    assert [_dump(t) for t in pre] == pre_before
    assert post == post_before
