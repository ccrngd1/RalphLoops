"""Property-based test for ``ralph_loop.snapshot_diff.diff_snapshots`` (Property 16).

This test exercises the diff rule declared in R8.2 and R8.3: for any
pre snapshot ``P`` (validated :class:`Task` list) and any post snapshot
``Q`` (raw dict list), the diff function classifies every id into
exactly one of ``created``, ``modified``, ``deleted``, or unchanged.

Property 16 (design.md):

    For any pre snapshot ``P`` and post snapshot ``Q`` of tasks.json,
    the diff function returns::

        created  = {t in Q | t.id not in ids(P)}
        modified = {t in Q | exists p in P where p.id == t.id and p != t}
        deleted  = {p in P | p.id not in ids(Q)}

Requirements validated: 8.2, 8.3.
"""

# Feature: ralph-loop, Property 16: Diff-based task-creation detection

from __future__ import annotations

from typing import Any

from hypothesis import given

from ralph_loop.models import Task
from ralph_loop.snapshot_diff import diff_snapshots

from tests.strategies import pre_and_post_snapshot_strategy


@given(snapshots=pre_and_post_snapshot_strategy())
def test_diff_partitions_ids_into_created_modified_deleted(
    snapshots: tuple[list[Task], list[dict[str, Any]]],
) -> None:
    """Validates: Requirements 8.2, 8.3.

    For any ``(pre, post)`` drawn by
    :func:`pre_and_post_snapshot_strategy` (where every post entry is a
    dict with a non-empty string id), the diff satisfies:

    1. ``deleted == {p in pre | p.id not in post_ids}``. The set of
       pre-only ids equals the set of ``deleted[*].id``.
    2. ``created == {q in post | q["id"] not in pre_ids}``. The set of
       post-only ids equals the set of ``created[*]["id"]``.
    3. ``modified`` contains one ``(pre_dump, post_dump)`` pair per
       common id whose JSON dumps differ; unchanged common ids do not
       appear.
    4. The four buckets (``created``, ``modified``, ``deleted``, and
       unchanged common ids) partition the union of pre and post ids.
    """

    pre, post = snapshots

    pre_ids = {t.id for t in pre}
    # The strategy guarantees every post entry is a dict with a
    # non-empty string id, so this comprehension is safe.
    post_ids = {entry["id"] for entry in post}

    diff = diff_snapshots(pre, post)

    # (1) Deleted = pre-only ids.
    deleted_ids = {t.id for t in diff.deleted}
    assert deleted_ids == pre_ids - post_ids

    # (2) Created = post-only ids. The strategy produces no malformed
    # entries, so every ``created`` dict has a usable string id.
    created_ids = {entry["id"] for entry in diff.created}
    assert created_ids == post_ids - pre_ids

    # (3) Modified entries are drawn from the common-id intersection and
    # each pair's pre dump differs from its post dump.
    common_ids = pre_ids & post_ids
    modified_ids = {pre_dump["id"] for pre_dump, _post in diff.modified}

    # Every modified id is common.
    assert modified_ids <= common_ids

    # Every modified pair actually differs.
    for pre_dump, post_dump in diff.modified:
        assert pre_dump != post_dump
        assert pre_dump["id"] == post_dump["id"]

    # The unchanged common ids have identical pre/post dumps.
    pre_dumps_by_id = {t.id: t.model_dump(mode="json") for t in pre}
    post_by_id = {entry["id"]: entry for entry in post}
    unchanged_ids = {
        tid for tid in common_ids if pre_dumps_by_id[tid] == post_by_id[tid]
    }
    assert modified_ids == common_ids - unchanged_ids

    # (4) Partition check: the four buckets are disjoint and cover the
    # union of pre and post ids.
    all_ids = pre_ids | post_ids
    assert created_ids | modified_ids | deleted_ids | unchanged_ids == all_ids
    assert created_ids.isdisjoint(deleted_ids)
    assert created_ids.isdisjoint(modified_ids)
    assert deleted_ids.isdisjoint(modified_ids)
    assert unchanged_ids.isdisjoint(
        created_ids | modified_ids | deleted_ids
    )
