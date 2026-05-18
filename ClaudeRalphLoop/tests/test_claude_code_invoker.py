"""Integration tests for :class:`ralph_loop.claude_code.ClaudeCodeInvoker` (Task 13.2).

These tests stub the real ``claude -p`` binary with a small Python script
written to ``tmp_path`` via :data:`sys.executable`. The stub echoes a
configurable structured response that optionally includes a token-usage
envelope line, so we can assert that the :class:`ClaudeCodeInvoker`:

* Passes the composed context on stdin (R6.1).
* Captures ``stdout`` and ``stderr`` and tees both to the per-iteration
  log file and the provided stdout sink (R11.2, R11.5).
* Parses token usage from the structured envelope (R12.1) and warns and
  returns ``token_usage=None`` when the envelope is absent (R12.6).

Requirements exercised: R11.2, R11.5, R12.1, R12.6.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import textwrap
from pathlib import Path
from typing import Optional

import pytest

from ralph_loop.claude_code import (
    ClaudeCodeInvocationTimeout,
    ClaudeCodeInvoker,
)


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _write_stub(
    tmp_path: Path,
    *,
    stdout_lines: list[str],
    stderr_lines: list[str] = (),
    exit_code: int = 0,
    sleep_s: float = 0.0,
) -> Path:
    """Write a tiny Python script mimicking the Claude Code CLI contract.

    The stub:
      * reads ``stdin`` to EOF (so the invoker's ``context`` write is
        drained) and echoes it back with an ``INPUT:`` prefix so tests
        can assert the context actually reached the subprocess;
      * emits the configured ``stdout_lines`` and ``stderr_lines``;
      * optionally sleeps (used by the timeout test);
      * exits with the configured ``exit_code``.

    The arguments received (``-p --output-format json``) are also echoed so
    we can assert the invoker invoked the right subcommand.
    """

    stub_path = tmp_path / "fake_claude.py"
    script = textwrap.dedent(
        f"""
        import sys
        import time

        # Consume the whole stdin so the invoker's drain completes; echo a
        # stable prefix line so tests can assert the context was piped.
        data = sys.stdin.read()
        sys.stdout.write("ARGS:" + " ".join(sys.argv[1:]) + chr(10))
        sys.stdout.write("INPUT:" + data + chr(10))

        for line in {stdout_lines!r}:
            sys.stdout.write(line + chr(10))
            sys.stdout.flush()
        for line in {list(stderr_lines)!r}:
            sys.stderr.write(line + chr(10))
            sys.stderr.flush()
        if {sleep_s!r} > 0:
            time.sleep({sleep_s!r})
        sys.exit({exit_code!r})
        """
    ).strip()
    stub_path.write_text(script, encoding="utf-8")
    return stub_path


def _invoker_for(stub_path: Path) -> ClaudeCodeInvoker:
    """Build an invoker whose ``claude_cli_command`` runs the stub via ``sys.executable``."""
    # Quote both pieces so spaces in paths (common on Windows) survive shlex.
    return ClaudeCodeInvoker(claude_cli_command=f'"{sys.executable}" "{stub_path}"')


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "iter-0001.log"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_invoke_captures_stdin_stdout_and_parses_token_envelope(
    tmp_path: Path,
    log_path: Path,
) -> None:
    """Happy path: context reaches stdin, tokens parsed, stdout tee'd to sink + log.

    R6.1 (context on stdin), R11.2 (stdout/stderr captured in log),
    R11.5 (tee to stdout sink + log file), R12.1 (token envelope parsed).
    """

    token_line = '{"input_tokens": 123, "output_tokens": 45, "model": "m-1"}'
    stub = _write_stub(
        tmp_path,
        stdout_lines=["Hello from persona", token_line, "Goodbye"],
        stderr_lines=["warning on stderr"],
        exit_code=0,
    )
    invoker = _invoker_for(stub)
    sink = io.StringIO()

    result = await invoker.invoke(
        context="COMPOSED CONTEXT BODY",
        log_path=log_path,
        call_kind="persona_execution",
        stdout_sink=sink,
    )

    # Exit code and token parsing
    assert result.exit_code == 0
    assert result.token_usage is not None
    assert result.token_usage.input_tokens == 123
    assert result.token_usage.output_tokens == 45
    assert result.token_usage.model == "m-1"
    assert result.duration_ms >= 0

    # stdout contained the echoed argv and input prefix
    assert "ARGS:-p --output-format json" in result.stdout
    assert "INPUT:COMPOSED CONTEXT BODY" in result.stdout
    assert "Hello from persona" in result.stdout
    assert token_line in result.stdout

    # stderr captured
    assert "warning on stderr" in result.stderr

    # Log file saw both streams
    log_contents = log_path.read_text(encoding="utf-8")
    assert "INPUT:COMPOSED CONTEXT BODY" in log_contents
    assert "Hello from persona" in log_contents
    assert "warning on stderr" in log_contents
    assert token_line in log_contents

    # Stdout sink mirrored the same content
    sink_contents = sink.getvalue()
    assert "INPUT:COMPOSED CONTEXT BODY" in sink_contents
    assert "Hello from persona" in sink_contents
    assert "warning on stderr" in sink_contents


async def test_invoke_without_token_envelope_returns_none_and_warns(
    tmp_path: Path,
    log_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When no token envelope line is present, token_usage is None and a warning is logged (R12.6)."""

    stub = _write_stub(
        tmp_path,
        stdout_lines=["no token envelope here"],
        exit_code=0,
    )
    invoker = _invoker_for(stub)

    with caplog.at_level(logging.WARNING, logger="ralph_loop.kiro"):
        result = await invoker.invoke(
            context="ctx",
            log_path=log_path,
            call_kind="persona_execution",
            stdout_sink=io.StringIO(),
        )

    assert result.exit_code == 0
    assert result.token_usage is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("no token usage" in r.getMessage() for r in warnings)


async def test_invoke_propagates_non_zero_exit_code(
    tmp_path: Path,
    log_path: Path,
) -> None:
    """Non-zero child exit codes flow through unchanged on the result."""

    stub = _write_stub(
        tmp_path,
        stdout_lines=["running"],
        stderr_lines=["boom"],
        exit_code=7,
    )
    invoker = _invoker_for(stub)

    result = await invoker.invoke(
        context="ctx",
        log_path=log_path,
        call_kind="persona_execution",
        stdout_sink=io.StringIO(),
    )

    assert result.exit_code == 7
    assert "boom" in result.stderr


async def test_invoke_malformed_token_envelope_is_ignored(
    tmp_path: Path,
    log_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A line with invalid JSON does not crash; token_usage stays None (R12.6)."""

    stub = _write_stub(
        tmp_path,
        stdout_lines=["not-json-here"],
        exit_code=0,
    )
    invoker = _invoker_for(stub)

    with caplog.at_level(logging.WARNING, logger="ralph_loop.claude_code"):
        result = await invoker.invoke(
            context="ctx",
            log_path=log_path,
            call_kind="persona_execution",
            stdout_sink=io.StringIO(),
        )

    assert result.exit_code == 0
    assert result.token_usage is None
    assert any(
        "no token usage" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


async def test_invoke_timeout_raises_and_kills_subprocess(
    tmp_path: Path,
    log_path: Path,
) -> None:
    """A subprocess exceeding ``timeout_ms`` raises ClaudeCodeInvocationTimeout."""

    stub = _write_stub(
        tmp_path,
        stdout_lines=["sleeping"],
        sleep_s=2.0,
        exit_code=0,
    )
    invoker = _invoker_for(stub)

    with pytest.raises(ClaudeCodeInvocationTimeout):
        await invoker.invoke(
            context="ctx",
            log_path=log_path,
            call_kind="persona_execution",
            timeout_ms=200,
            stdout_sink=io.StringIO(),
        )
