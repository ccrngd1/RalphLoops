"""Pending-task queue manager (R9.1-R9.11, R8.10, R8.11, R8.13).

The Pending Task Queue (``pending_tasks.json``) stores Tasks that were
spilled from a prior run when the per-iteration or per-run
Task_Creation_Budget was exhausted. At startup the Ralph_Loop re-admits
the valid entries back into ``tasks.json`` so surplus work is never
silently lost between runs (R9.1-R9.10).

Two operations live here:

- :meth:`PendingQueueManager.process_on_startup` loads and validates
  every entry, admits the valid ones (stamping ``admitted_run_id`` while
  preserving ``spilled_run_id`` and original creation metadata),
  discards invalid entries with a logged reason, atomically truncates
  the file to ``[]``, and returns a :class:`PendingQueueResult`.
- :meth:`PendingQueueManager.spill` appends a spilled task to the queue
  using the atomic writer so interrupted writes never leave a partial
  file on disk.

Unparseable JSON in the queue file is a fatal startup error (R9.11):
the manager raises :class:`PendingQueueError` carrying the file path
and the underlying :class:`json.JSONDecodeError` message so the CLI
exits non-zero with an operator-friendly diagnostic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ralph_loop.atomic_io import atomic_write_bytes
from ralph_loop.models import (
    DiscardedEntry,
    PendingQueueResult,
    SpillReason,
    Task,
)
from ralph_loop.persona_registry import PersonaRegistry

logger = logging.getLogger(__name__)


class PendingQueueError(Exception):
    """Raised on a fatal pending-queue condition (R9.11).

    The Ralph_Loop treats this as a fail-fast startup error: the CLI
    exits non-zero and logs the message, which always identifies the
    file path and the underlying parse error so the operator can fix
    the file before re-running.
    """


class PendingQueueManager:
    """Load, admit, and append entries in ``pending_tasks.json``.

    Instances are parameterized by:

    - ``queue_path``: path to the pending-queue file (defaults to
      ``pending_tasks.json`` per R15.5, but the manager works with any
      path supplied by the config loader).
    - ``registry``: the loaded :class:`PersonaRegistry`; used to
      validate ``target_persona`` references on admitted entries
      (R9.4).
    - ``run_id``: the current run identifier; stamped onto every
      admitted entry as ``admitted_run_id`` (R9.7) and onto every
      spilled entry as ``spilled_run_id`` (R8.13).
    """

    def __init__(
        self, queue_path: Path, registry: PersonaRegistry, run_id: str
    ) -> None:
        self._queue_path = Path(queue_path)
        self._registry = registry
        self._run_id = run_id

    def process_on_startup(self) -> PendingQueueResult:
        """Load and admit pending-queue entries (R9.1-R9.10).

        Semantics:

        - If the file does not exist or is empty, return
          ``PendingQueueResult(loaded=0, admitted=[], discarded=[])``
          without touching disk (R9.2).
        - Parse the file as JSON; raise :class:`PendingQueueError` on
          any :class:`json.JSONDecodeError` (R9.11) or when the
          top-level value is not a list (the queue is defined as a
          JSON array per the data-model section of ``design.md``).
        - For every entry:
          - Reject entries that are not JSON objects.
          - Validate the dict against the :class:`Task` Pydantic
            schema (R9.3).
          - If ``target_persona`` is set, verify it exists in the
            registry (R9.4).
          - Admit entries that pass both checks by stamping
            ``admitted_run_id = self._run_id`` via
            ``model_copy(update=...)`` (R9.5, R9.7). Original
            ``spilled_run_id``, ``created_at_iteration``, and
            ``created_by_persona`` fields are preserved because
            ``model_copy`` replaces only the updated keys.
          - Discard everything else with a reason string (R9.6).
        - After processing, atomically rewrite the file to ``[]``
          (R9.9) and log ``loaded``, ``admitted``, and ``discarded``
          counts (R9.10).
        """

        if not self._queue_path.exists() or self._queue_path.stat().st_size == 0:
            return PendingQueueResult(loaded=0, admitted=[], discarded=[])

        try:
            raw_content = self._queue_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PendingQueueError(
                f"Failed to read pending queue file {self._queue_path}: {exc}"
            ) from exc

        # Treat pure whitespace content as an empty queue. Windows
        # editors that "touch" a file sometimes leave a trailing
        # newline; stripping avoids tripping R9.11 on that benign case.
        if not raw_content.strip():
            return PendingQueueResult(loaded=0, admitted=[], discarded=[])

        try:
            raw_entries = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            # R9.11: fail fast with a message that names the file and
            # quotes the underlying parse error.
            raise PendingQueueError(
                f"Failed to parse pending queue file {self._queue_path}: "
                f"{exc.msg} (line {exc.lineno} column {exc.colno})"
            ) from exc

        if not isinstance(raw_entries, list):
            raise PendingQueueError(
                f"Pending queue file {self._queue_path} must contain a JSON "
                f"array, got {type(raw_entries).__name__}"
            )

        loaded = len(raw_entries)
        admitted: list[Task] = []
        discarded: list[DiscardedEntry] = []

        for raw in raw_entries:
            # Every entry must be a JSON object. Discarding non-dict
            # entries (ints, strings, lists, nulls) satisfies R9.6 by
            # flagging them with a descriptive reason rather than
            # failing the whole load.
            if not isinstance(raw, dict):
                discarded.append(
                    DiscardedEntry(
                        raw_entry={"_invalid_raw": raw},
                        reason=(
                            "Entry is not a JSON object "
                            f"(got {type(raw).__name__})"
                        ),
                    )
                )
                continue

            # Schema validation (R9.3).
            try:
                task = Task.model_validate(raw)
            except ValidationError as exc:
                discarded.append(
                    DiscardedEntry(
                        raw_entry=raw,
                        reason=f"Task schema validation failed: {exc}",
                    )
                )
                continue

            # Persona-existence validation (R9.4).
            if (
                task.target_persona is not None
                and self._registry.get(task.target_persona) is None
            ):
                discarded.append(
                    DiscardedEntry(
                        raw_entry=raw,
                        reason=(
                            f"Target persona {task.target_persona!r} not in "
                            "registry"
                        ),
                    )
                )
                continue

            # Admit: stamp admitted_run_id while preserving every other
            # field on the Task (R9.5, R9.7). model_copy with update
            # returns a new instance with only the listed keys replaced.
            admitted_task = task.model_copy(
                update={"admitted_run_id": self._run_id}
            )
            admitted.append(admitted_task)

        # Truncate the file (R9.9). We use the atomic writer so a
        # crash between writes can't leave the queue half-written.
        atomic_write_bytes(self._queue_path, b"[]")

        logger.info(
            "pending queue processed: loaded=%d admitted=%d discarded=%d",
            loaded,
            len(admitted),
            len(discarded),
        )
        # Log each discard reason at WARNING so operators can see why
        # entries were dropped without having to re-read the file.
        for entry in discarded:
            logger.warning(
                "pending queue discarded entry: %s", entry.reason
            )

        return PendingQueueResult(
            loaded=loaded, admitted=admitted, discarded=discarded
        )

    def spill(self, task: Task, reason: SpillReason, run_id: str) -> None:
        """Append a spilled task to the pending queue (R8.10, R8.11, R8.13).

        The Task Creation Processor calls this when a budget was
        exceeded during an iteration. The task is stamped with
        ``spilled_run_id = run_id`` while every other field
        (``created_at_iteration``, ``created_by_persona``,
        ``creation_chain``) is preserved so a later run can re-admit
        the task with its full provenance intact (R9.7).

        ``reason`` is a :data:`SpillReason` literal reported in logs
        by the processor; it is not persisted in the queue file
        because the cause of the spill is a per-run concern.

        The write is atomic (:func:`atomic_write_bytes`), so a crash
        during the write will leave either the old queue or the new
        queue on disk, never a partial file (R14.2).
        """

        # Read the current queue. We don't use the atomic writer for
        # the read because readers get a consistent view from the
        # filesystem regardless.
        entries: list[Any]
        if self._queue_path.exists():
            try:
                raw = self._queue_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise PendingQueueError(
                    f"Failed to read pending queue file {self._queue_path}: "
                    f"{exc}"
                ) from exc

            if raw:
                try:
                    entries = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise PendingQueueError(
                        f"Failed to parse pending queue file "
                        f"{self._queue_path}: {exc.msg} (line {exc.lineno} "
                        f"column {exc.colno})"
                    ) from exc
                if not isinstance(entries, list):
                    raise PendingQueueError(
                        f"Pending queue file {self._queue_path} must contain "
                        f"a JSON array, got {type(entries).__name__}"
                    )
            else:
                entries = []
        else:
            entries = []

        spilled_task = task.model_copy(update={"spilled_run_id": run_id})
        entries.append(spilled_task.model_dump(mode="json"))

        atomic_write_bytes(
            self._queue_path, json.dumps(entries).encode("utf-8")
        )

        logger.info(
            "pending queue spilled task %s (reason=%s, run_id=%s)",
            task.id,
            reason,
            run_id,
        )
