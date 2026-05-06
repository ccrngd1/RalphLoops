"""Pure diff function for ``tasks.json`` pre/post snapshots (R8.2, R8.3).

After each iteration the Ralph Loop captures two snapshots of
``tasks.json``:

- ``pre``: the validated :class:`~ralph_loop.models.Task` list written
  to disk before invoking Kiro CLI (tasks.json has already been flipped
  to mark the selected task ``in_progress``).
- ``post``: the raw JSON-decoded list read back after Kiro CLI exits.
  Entries in the post list may be invalid against the ``Task`` schema
  (the persona may have written a malformed entry); the diff function
  must therefore accept raw dicts and defer schema validation to the
  downstream Task Creation Processor (R8.4, R8.7, R8.12).

:func:`diff_snapshots` keys both lists by ``id`` and classifies every
entry into one of three buckets, matching design Property 16:

- ``created``: every post entry whose ``id`` is absent from the pre
  snapshot. Raw dicts are preserved so the processor can feed them into
  the new-entry validation pipeline without re-parsing (R8.3, R8.4).
  Post entries without a usable string ``id`` cannot be mapped into
  either snapshot index, so they are also routed to ``created`` where
  the downstream validation pipeline will reject them.
- ``modified``: ``(pre_dump, post_dump)`` pairs where the same ``id``
  exists in both snapshots but the persisted JSON dump differs. These
  drive the revert path for non-executing tasks (R8.8) and, for the
  executing task, let the loop detect persona-side edits to the
  in-progress entry.
- ``deleted``: pre :class:`Task` instances whose ``id`` is absent from
  the post snapshot. The processor restores these verbatim during
  revert (R8.8).

The function is pure: it performs no I/O, does not mutate its inputs,
and returns a fresh :class:`SnapshotDiff`.
"""

from __future__ import annotations

from typing import Any

from ralph_loop.models import SnapshotDiff, Task


def diff_snapshots(
    pre: list[Task],
    post: list[dict[str, Any]],
) -> SnapshotDiff:
    """Return the ``created`` / ``modified`` / ``deleted`` diff (R8.2, R8.3).

    Parameters
    ----------
    pre:
        The pre-iteration snapshot: already-validated ``Task`` instances.
        Callers guarantee ``pre`` has unique ids because it was loaded
        through the atomic writer + ``TASK_LIST_ADAPTER``.
    post:
        The post-iteration snapshot as raw JSON-decoded dicts. Entries
        may be missing fields, have wrong types, or carry ids that
        collide with pre entries. The diff function does not validate
        against the ``Task`` schema -- that is the Task Creation
        Processor's job (R8.4).

    Returns
    -------
    SnapshotDiff
        A new :class:`SnapshotDiff` whose ``created`` / ``modified`` /
        ``deleted`` buckets satisfy design Property 16.

    Notes
    -----
    - Modified entries are detected by comparing
      ``pre_task.model_dump(mode="json")`` to the raw post dict. This
      mirrors how the task list is serialized to disk, so a round-trip
      through ``tasks.json`` (no persona edits) yields an empty
      ``modified`` bucket.
    - If the post list contains two dicts with the same ``id``, the
      last-seen entry wins when populating the post index. This is
      consistent with how ``json.load`` + dict-keyed diffs behave and
      keeps the function deterministic for any given input order.
    """
    # Index pre by id. Task.id has ``min_length=1`` so every entry has a
    # usable key. JSON dumps are used for the equality check against raw
    # post dicts so the compare stays symmetric with on-disk state.
    pre_index: dict[str, Task] = {t.id: t for t in pre}
    pre_dumps: dict[str, dict[str, Any]] = {
        t.id: t.model_dump(mode="json") for t in pre
    }

    # Index post by id, collecting entries without a usable string id
    # into ``created`` directly. A "usable" id is a non-empty string:
    # anything else (missing key, non-string, empty string) cannot map
    # to a pre entry and must be routed to the new-entry validation
    # pipeline so it is rejected with a descriptive reason (R8.4).
    post_index: dict[str, dict[str, Any]] = {}
    created_without_id: list[dict[str, Any]] = []
    for entry in post:
        if not isinstance(entry, dict):
            # Non-dict entries (e.g. a bare string in tasks.json) cannot
            # be mapped at all. Wrap them so downstream validation sees a
            # dict-shaped value with the malformed payload.
            created_without_id.append({"_invalid_raw": entry})
            continue
        raw_id = entry.get("id")
        if isinstance(raw_id, str) and raw_id:
            post_index[raw_id] = entry
        else:
            created_without_id.append(entry)

    pre_ids = set(pre_index.keys())
    post_ids = set(post_index.keys())

    # Iterate pre / post lists (not the id sets) so output order is
    # deterministic and reflects the caller's input ordering. This keeps
    # logs and counterexamples readable.
    created: list[dict[str, Any]] = [
        entry for entry in post
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and entry["id"]
        and entry["id"] not in pre_ids
    ]
    created.extend(created_without_id)

    deleted: list[Task] = [t for t in pre if t.id not in post_ids]

    modified: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for t in pre:
        if t.id in post_ids:
            post_dump = post_index[t.id]
            pre_dump = pre_dumps[t.id]
            if pre_dump != post_dump:
                modified.append((pre_dump, post_dump))

    return SnapshotDiff(created=created, modified=modified, deleted=deleted)
