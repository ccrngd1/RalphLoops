"""Unit and integration tests for :mod:`ralph_loop.logger` (Tasks 20.1, 20.2).

Covers:

- :class:`TeeStream` mirrors writes to every configured sink and
  swallows sink-level IO errors so logging cannot take the run
  process down.
- :func:`configure_logger` returns a bound structlog logger and wires
  both file and stdout sinks (R11.5).
- :class:`IterationLogWriter` accumulates per-iteration fields, emits
  a valid :class:`IterationLogEntry` on :meth:`finalize`, writes the
  NDJSON line to the log file, and mirrors the same line to the
  stdout sink (R11.3, R11.5, R12.2).
- :func:`write_run_summary` persists the :class:`RunSummary` as a
  pretty-printed JSON file in the configured log directory (R11.4,
  R12.5).

Task 20.2's dual-sink integration test is also fulfilled here via
:meth:`TestIterationLogWriter.test_finalize_writes_identical_line_to_file_and_stdout`;
it exercises the same code path a short loop would hit -- the only
interacting pieces are the writer and the two sinks.

Requirements exercised: R11.1, R11.2, R11.3, R11.4, R11.5, R12.2,
R12.5.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import structlog

from ralph_loop.logger import (
    IterationLogWriter,
    TeeStream,
    build_context_summary,
    build_claude_invocation_log,
    build_validation_log,
    configure_logger,
    write_run_summary,
)
from ralph_loop.models import (
    CheckResult,
    IterationLogEntry,
    KindTotals,
    LlmCallRecord,
    RunSummary,
    RunTokenTotals,
)


# ---------------------------------------------------------------------------
# TeeStream
# ---------------------------------------------------------------------------


class TestTeeStream:
    def test_write_reaches_every_sink(self) -> None:
        a = io.StringIO()
        b = io.StringIO()
        tee = TeeStream(a, b)
        tee.write("hello\n")
        assert a.getvalue() == "hello\n"
        assert b.getvalue() == "hello\n"

    def test_write_returns_len_msg(self) -> None:
        tee = TeeStream(io.StringIO())
        assert tee.write("abc") == 3

    def test_requires_at_least_one_sink(self) -> None:
        with pytest.raises(ValueError, match="at least one sink"):
            TeeStream()  # type: ignore[call-arg]

    def test_sink_exception_is_swallowed(self) -> None:
        class BrokenSink:
            def write(self, _: str) -> int:
                raise RuntimeError("kaboom")

            def flush(self) -> None:
                pass

        ok = io.StringIO()
        tee = TeeStream(BrokenSink(), ok)  # type: ignore[arg-type]
        # Must not raise; the good sink still receives the write.
        tee.write("ping\n")
        assert ok.getvalue() == "ping\n"


# ---------------------------------------------------------------------------
# configure_logger
# ---------------------------------------------------------------------------


class TestConfigureLogger:
    def test_returns_structlog_bound_logger(self, tmp_path: Path) -> None:
        sink = io.StringIO()
        logger = configure_logger(
            log_file_path=tmp_path / "logs" / "run.log",
            stdout_sink=sink,
        )
        assert isinstance(logger, structlog.stdlib.BoundLogger) or hasattr(
            logger, "bind"
        )

    def test_writes_json_line_to_stdout_sink(self, tmp_path: Path) -> None:
        sink = io.StringIO()
        logger = configure_logger(
            log_file_path=tmp_path / "logs" / "run.log",
            stdout_sink=sink,
        )
        logger.info("hello", task_id="T1")

        # The structlog JSON renderer emits a single JSON document per
        # line. Parsing it validates the payload and asserts its shape.
        content = sink.getvalue().strip()
        assert content, "structlog produced no output on stdout sink"
        parsed = json.loads(content)
        assert parsed["event"] == "hello"
        assert parsed["task_id"] == "T1"

    def test_writes_identical_json_line_to_file(self, tmp_path: Path) -> None:
        sink = io.StringIO()
        log_path = tmp_path / "logs" / "run.log"
        logger = configure_logger(log_file_path=log_path, stdout_sink=sink)
        logger.info("file-sink", run_id="r1")

        # File and sink should hold the same content.
        assert log_path.exists()
        file_content = log_path.read_text(encoding="utf-8")
        assert sink.getvalue() == file_content


# ---------------------------------------------------------------------------
# IterationLogWriter
# ---------------------------------------------------------------------------


def _full_iteration_fields() -> dict:
    """Return a dict with every mandatory field for IterationLogEntry."""
    return dict(
        start_time="2024-01-01T00:00:00Z",
        end_time="2024-01-01T00:00:05Z",
        task_id="T1",
        persona_name="Writer",
        selection_path="explicit",
        context_summary=build_context_summary(
            approx_tokens=100,
            truncated=False,
            resumed_from_interruption=False,
            escalation_enriched=False,
        ),
        claude_invocation=build_claude_invocation_log(
            exit_code=0,
            duration_ms=1000,
            stdout_path="logs/stdout.log",
            stderr_path="logs/stderr.log",
        ),
        validation=build_validation_log(
            overall="pass",
            checks=[
                CheckResult(
                    type="shell", name="build", verdict="pass",
                    output="ok", duration_ms=100,
                )
            ],
        ),
    )


class TestIterationLogWriter:
    def test_record_and_finalize_produces_valid_entry(
        self, tmp_path: Path
    ) -> None:
        sink = io.StringIO()
        log_path = tmp_path / "iter-0001.log"
        writer = IterationLogWriter(
            iteration=1, run_id="run-1", log_path=log_path, stdout_sink=sink,
        )

        writer.record(**_full_iteration_fields())
        entry = writer.finalize("pass")

        assert isinstance(entry, IterationLogEntry)
        assert entry.iteration == 1
        assert entry.run_id == "run-1"
        assert entry.outcome == "pass"
        assert entry.task_id == "T1"

    def test_append_llm_call_is_emitted_in_finalized_entry(
        self, tmp_path: Path
    ) -> None:
        sink = io.StringIO()
        log_path = tmp_path / "iter-0001.log"
        writer = IterationLogWriter(
            iteration=1, run_id="run-1", log_path=log_path, stdout_sink=sink,
        )
        writer.record(**_full_iteration_fields())
        writer.append_llm_call(
            LlmCallRecord(
                kind="persona_execution",
                model="m1",
                input_tokens=100,
                output_tokens=50,
            )
        )
        writer.append_llm_call(
            LlmCallRecord(
                kind="orchestrator_selection",
                model="m1",
                input_tokens=10,
                output_tokens=5,
            )
        )

        entry = writer.finalize("pass")
        assert len(entry.llm_calls) == 2
        kinds = [call.kind for call in entry.llm_calls]
        assert kinds == ["persona_execution", "orchestrator_selection"]

    def test_finalize_writes_identical_line_to_file_and_stdout(
        self, tmp_path: Path
    ) -> None:
        """R11.5 dual-sink assertion (Task 20.2): file and stdout see the same line."""

        sink = io.StringIO()
        log_path = tmp_path / "iter-0001.log"
        writer = IterationLogWriter(
            iteration=7, run_id="run-xyz", log_path=log_path, stdout_sink=sink,
        )
        writer.record(**_full_iteration_fields())
        writer.append_llm_call(
            LlmCallRecord(
                kind="persona_execution", model="m1",
                input_tokens=1, output_tokens=1,
            )
        )
        writer.finalize("pass")

        file_content = log_path.read_text(encoding="utf-8")
        sink_content = sink.getvalue()

        # Both sinks should carry exactly one NDJSON line with the
        # same content.
        assert file_content == sink_content
        assert file_content.endswith("\n")

        # And the line parses back to a valid entry.
        parsed = json.loads(file_content.strip())
        assert parsed["iteration"] == 7
        assert parsed["run_id"] == "run-xyz"
        assert parsed["outcome"] == "pass"
        assert len(parsed["llm_calls"]) == 1

    def test_finalize_appends_rather_than_overwrites(
        self, tmp_path: Path
    ) -> None:
        sink = io.StringIO()
        log_path = tmp_path / "iter-0001.log"

        for i in (1, 2):
            writer = IterationLogWriter(
                iteration=i, run_id="run-1", log_path=log_path,
                stdout_sink=sink,
            )
            writer.record(**_full_iteration_fields())
            writer.finalize("pass")

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        iterations = [json.loads(line)["iteration"] for line in lines]
        assert iterations == [1, 2]

    def test_missing_mandatory_field_raises_on_finalize(
        self, tmp_path: Path
    ) -> None:
        sink = io.StringIO()
        log_path = tmp_path / "iter-0001.log"
        writer = IterationLogWriter(
            iteration=1, run_id="run-1", log_path=log_path, stdout_sink=sink,
        )
        # Do not record any field -> Pydantic must reject the entry.
        with pytest.raises(Exception):
            writer.finalize("pass")


# ---------------------------------------------------------------------------
# write_run_summary (R11.4, R12.5)
# ---------------------------------------------------------------------------


class TestWriteRunSummary:
    def test_writes_valid_json_file(self, tmp_path: Path) -> None:
        summary = RunSummary(
            run_id="run-1",
            total_iterations=3,
            status_counts={
                "passing": 2, "failing": 0, "pending": 0,
                "in_progress": 0, "stuck": 1,
            },
            total_new_tasks=0,
            escalation_events=0,
            elapsed_ms=5_000,
            token_totals=RunTokenTotals(
                total_input=100, total_output=50, total_combined=150,
                total_estimated_cost=0.015,
                by_kind={
                    "persona_execution": KindTotals(
                        input=100, output=50, cost=0.015,
                    ),
                },
            ),
            exit_code=0,
        )

        path = write_run_summary(summary=summary, log_dir=tmp_path / "logs")

        assert path.exists()
        assert path.name.startswith("run-run-1-summary")
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert parsed["run_id"] == "run-1"
        assert parsed["total_iterations"] == 3
        assert parsed["exit_code"] == 0

    def test_creates_log_dir_if_missing(self, tmp_path: Path) -> None:
        summary = RunSummary(
            run_id="new",
            total_iterations=0,
            status_counts={
                "passing": 0, "failing": 0, "pending": 0,
                "in_progress": 0, "stuck": 0,
            },
            total_new_tasks=0,
            escalation_events=0,
            elapsed_ms=0,
            token_totals=RunTokenTotals(),
            exit_code=0,
        )
        target_dir = tmp_path / "does" / "not" / "exist"
        assert not target_dir.exists()
        path = write_run_summary(summary=summary, log_dir=target_dir)
        assert path.exists()
        assert path.parent == target_dir
