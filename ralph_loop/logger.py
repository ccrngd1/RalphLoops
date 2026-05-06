"""Structured logging and per-iteration observability (R11.1-R11.5, R12.2, R12.5).

The Logger subsystem has two responsibilities:

1. **Free-form structured logs.** Wire :mod:`structlog` with a JSON
   renderer so loop-wide informational / warning / error lines are
   emitted as one-line JSON documents (R11.5). The logger tees those
   lines to both the per-iteration log file and process stdout via a
   :class:`TeeStream` -- the design calls for identical content on
   both sinks so operators can follow the run live or review the file
   later (R11.5 again).

2. **Per-iteration entry accumulation.** :class:`IterationLogWriter`
   accumulates the structured fields of a single
   :class:`IterationLogEntry` as they become available during an
   iteration (selection, context, invocation, validation,
   task-creation, commit, outcome) and finalizes the entry as a
   single NDJSON line appended to the per-iteration log (R11.3,
   R12.2). The run summary is written once at shutdown by
   :func:`write_run_summary` (R11.4, R12.5).

The module does not own the loop. The CLI constructs a
:class:`Logger`, asks it for an :class:`IterationLogWriter` each
iteration, and calls :meth:`IterationLogWriter.finalize` at the end.
"""

from __future__ import annotations

import atexit
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional, TextIO

import structlog

from ralph_loop.models import (
    ContextSummary,
    GitCommitLog,
    IterationLogEntry,
    IterationOutcome,
    KiroInvocationLog,
    LlmCallRecord,
    RunSummary,
    SelectionPath,
    TaskCreationEventLog,
    ValidationLog,
    Verdict,
)


class TeeStream:
    """Write every string to each configured sink (R11.5).

    Used as the ``file`` target for stdlib :mod:`logging` and the
    stream consumed by :class:`structlog.WriteLoggerFactory`. Both
    sinks are flushed after every write so the file and stdout stay
    in sync even if the process is killed mid-iteration.
    """

    def __init__(self, *sinks: TextIO) -> None:
        if not sinks:
            raise ValueError("TeeStream requires at least one sink")
        self._sinks = sinks

    def write(self, msg: str) -> int:
        for sink in self._sinks:
            try:
                sink.write(msg)
                sink.flush()
            except Exception:  # noqa: BLE001 - logging must not break the loop
                # Failed sink writes are intentionally swallowed; the
                # alternative is to take the process down for a
                # filesystem hiccup during logging.
                pass
        return len(msg)

    def flush(self) -> None:
        for sink in self._sinks:
            try:
                sink.flush()
            except Exception:  # noqa: BLE001
                pass


def _safe_close(f: TextIO) -> None:
    """Close a file sink, swallowing errors (used for atexit cleanup)."""
    try:
        f.close()
    except Exception:  # noqa: BLE001
        pass


# Track file sinks opened by ``configure_logger`` so repeated calls in
# the same process (common in tests) replace and close the prior
# sink rather than leaking it. Also registered with :mod:`atexit` so
# the final sink is closed cleanly on interpreter exit.
_open_file_sinks: list[TextIO] = []


@atexit.register
def _close_tracked_file_sinks() -> None:
    for sink in _open_file_sinks:
        _safe_close(sink)
    _open_file_sinks.clear()


def configure_logger(
    *,
    log_file_path: Optional[Path] = None,
    stdout_sink: Optional[TextIO] = None,
) -> structlog.BoundLogger:
    """Configure structlog + stdlib logging for JSON output to file + stdout.

    Args:
        log_file_path: Destination file for the tee. When ``None``,
            only ``stdout_sink`` receives output. The parent directory
            is created if needed.
        stdout_sink: Sink that mirrors every line (defaults to
            :data:`sys.stdout`). Tests pass an :class:`io.StringIO` to
            capture output.

    Returns:
        A :class:`structlog.BoundLogger` bound to the configured
        processors. Callers can log directly against it or use the
        stdlib :mod:`logging` API -- both end up in the same sinks.
    """

    sink = stdout_sink if stdout_sink is not None else sys.stdout

    # Close any file sink from a prior ``configure_logger`` call so
    # repeated invocations in the same process (tests, re-init
    # scenarios) do not leak open file descriptors.
    while _open_file_sinks:
        _safe_close(_open_file_sinks.pop())

    if log_file_path is not None:
        log_file_path = Path(log_file_path)
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        # ``buffering=1`` gives us line-buffered writes, which keeps
        # the tee in step with stdout even during crashes.
        file_sink = log_file_path.open("a", encoding="utf-8", buffering=1)
        _open_file_sinks.append(file_sink)
        tee: TextIO = TeeStream(file_sink, sink)  # type: ignore[assignment]
    else:
        tee = TeeStream(sink)  # type: ignore[assignment]

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        logger_factory=structlog.WriteLoggerFactory(file=tee),  # type: ignore[arg-type]
        cache_logger_on_first_use=False,
    )

    # Mirror stdlib logging into the same tee so third-party libraries
    # that still use ``logging`` also surface on both sinks.
    root = logging.getLogger()
    # Remove any handlers attached by earlier ``configure_logger``
    # invocations (common in tests). Leaving them in place would
    # produce duplicate lines on the tee.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler = logging.StreamHandler(stream=tee)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    return structlog.get_logger()


class IterationLogWriter:
    """Accumulate and persist one :class:`IterationLogEntry` (R11.3, R12.2).

    The loop calls :meth:`record` to stash individual fields (task
    id, persona, selection path, validation result, ...) as they
    become available, :meth:`append_llm_call` for every LLM call
    performed during the iteration (R12.2), and :meth:`finalize` when
    the iteration ends. ``finalize`` validates the accumulated fields
    through :class:`IterationLogEntry` (so a missing mandatory field
    surfaces immediately) and appends the JSON document to the log
    file while mirroring it to stdout (R11.5).
    """

    def __init__(
        self,
        *,
        iteration: int,
        run_id: str,
        log_path: Path,
        stdout_sink: Optional[TextIO] = None,
    ) -> None:
        self._log_path = Path(log_path)
        self._stdout_sink = stdout_sink if stdout_sink is not None else sys.stdout
        self._fields: dict[str, Any] = {
            "iteration": iteration,
            "run_id": run_id,
            "llm_calls": [],
        }

    # -- Field accumulators ----------------------------------------

    def record(self, **fields: Any) -> None:
        """Set one or more fields on the accumulating entry.

        Any key from :class:`IterationLogEntry` may be passed;
        ``llm_calls`` is appended-to via :meth:`append_llm_call`
        rather than replaced, so callers should not pass it here.
        Unknown keys are still stored -- Pydantic will reject them
        in :meth:`finalize` if the model is in strict mode, which
        keeps mistakes discoverable rather than silently lost.
        """

        self._fields.update(fields)

    def append_llm_call(self, record: LlmCallRecord) -> None:
        """Append a single :class:`LlmCallRecord` to the iteration (R12.2)."""

        self._fields.setdefault("llm_calls", []).append(record)

    # -- Terminal emit ---------------------------------------------

    def finalize(self, outcome: IterationOutcome) -> IterationLogEntry:
        """Build the entry and persist it as NDJSON (R11.3, R11.5).

        Raises :class:`pydantic.ValidationError` if a mandatory field
        was never recorded -- the loop should surface this as a bug
        rather than silently ship an incomplete log entry.
        """

        self._fields["outcome"] = outcome
        entry = IterationLogEntry.model_validate(self._fields)

        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        line = entry.model_dump_json() + "\n"

        # R11.5: identical lines to both sinks. We write to the file
        # first so a crash after the file write still surfaces the
        # entry on restart.
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
        try:
            self._stdout_sink.write(line)
            self._stdout_sink.flush()
        except Exception:  # noqa: BLE001
            pass

        return entry


def write_run_summary(
    *,
    summary: RunSummary,
    log_dir: Path,
) -> Path:
    """Persist the :class:`RunSummary` to disk (R11.4, R12.5).

    The summary file is named ``run-<run_id>-summary.json`` and lives
    alongside the per-iteration logs. Returns the path so the caller
    can log or print its location.
    """

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"run-{summary.run_id}-summary.json"
    path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    return path


# -- Convenience constructors for embedded sub-logs ----------------
#
# These small factories exist so the main loop can build the nested
# sub-models for the iteration log entry without importing half of
# ``models.py`` everywhere. They accept plain kwargs and return the
# validated Pydantic model; callers pass the result into
# :meth:`IterationLogWriter.record`.


def build_context_summary(
    *,
    approx_tokens: int,
    truncated: bool,
    resumed_from_interruption: bool,
    escalation_enriched: bool,
) -> ContextSummary:
    return ContextSummary(
        approx_tokens=approx_tokens,
        truncated=truncated,
        resumed_from_interruption=resumed_from_interruption,
        escalation_enriched=escalation_enriched,
    )


def build_kiro_invocation_log(
    *,
    exit_code: int,
    duration_ms: int,
    stdout_path: str,
    stderr_path: str,
) -> KiroInvocationLog:
    return KiroInvocationLog(
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def build_validation_log(
    *,
    overall: Verdict,
    checks: list,
) -> ValidationLog:
    return ValidationLog(overall=overall, checks=checks)


def build_task_creation_event_log(
    *,
    accepted_count: int,
    rejected_count: int,
    spilled_count: int,
    reverted_ids: list[str],
) -> TaskCreationEventLog:
    return TaskCreationEventLog(
        accepted_count=accepted_count,
        rejected_count=rejected_count,
        spilled_count=spilled_count,
        reverted_ids=reverted_ids,
    )


def build_git_commit_log(
    *,
    sha: Optional[str],
    skipped: bool,
    skip_reason: Optional[str],
) -> GitCommitLog:
    return GitCommitLog(sha=sha, skipped=skipped, skip_reason=skip_reason)


__all__ = [
    "IterationLogWriter",
    "TeeStream",
    "build_context_summary",
    "build_git_commit_log",
    "build_kiro_invocation_log",
    "build_task_creation_event_log",
    "build_validation_log",
    "configure_logger",
    "write_run_summary",
]
