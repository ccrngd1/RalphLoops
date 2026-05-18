"""Claude Code CLI invocation wrapper (R6.1, R11.2, R11.5, R12.1, R12.6).

This module spawns ``claude`` CLI as a subprocess, pipes the composed
context on stdin, and streams stdout/stderr concurrently to both the
per-iteration log file and process stdout (R11.5). Token usage is parsed
from JSON lines in the Claude Code CLI stdout via a dedicated Pydantic
model; when no valid token line is found we emit a warning and return
``token_usage=None`` (R12.6).

The ``call_kind`` passed by the caller comes from the selection path
that produced the persona for a persona iteration:

* ``selection_path == "escalation"`` -> ``call_kind = "escalation"``
* ``selection_path in {"explicit", "llm", "fallback"}`` ->
  ``call_kind = "persona_execution"``

Other call kinds (``"orchestrator_selection"``, ``"persona_review"``,
``"planner"``) are set by their respective callers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import sys
import time
from pathlib import Path
from typing import IO, Optional

from pydantic import BaseModel, ValidationError

from ralph_loop.models import (
    CallKind,
    ClaudeInvocationResult,
    TokenUsage,
)

logger = logging.getLogger(__name__)


class ClaudeTokenEnvelope(BaseModel):
    """Structured token envelope parsed from a Claude Code CLI JSON line (R12.6).

    This is an internal parser model; the outward-facing type is
    :class:`ralph_loop.models.TokenUsage`. Keeping this model separate
    lets the Claude Code CLI change its envelope shape without altering
    the domain :class:`TokenUsage` definition.
    """

    input_tokens: int
    output_tokens: int
    model: Optional[str] = None


class ClaudeCodeInvocationTimeout(Exception):
    """Raised when a Claude Code CLI invocation exceeds the caller's ``timeout_ms``."""


def _parse_token_line(line: str) -> Optional[TokenUsage]:
    """Return a :class:`TokenUsage` if ``line`` is valid JSON with token fields.

    The Claude Code CLI with --output-format json emits lines containing JSON
    objects. We scan for lines that parse as JSON and contain both
    'input_tokens' and 'output_tokens' fields. Malformed candidates are
    silently ignored so a single corrupt line does not mask a later good one.
    """

    try:
        parsed = json.loads(line.strip())
        if not isinstance(parsed, dict):
            return None
        if "input_tokens" not in parsed or "output_tokens" not in parsed:
            return None
        envelope = ClaudeTokenEnvelope.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError):
        return None
    return TokenUsage(
        input_tokens=envelope.input_tokens,
        output_tokens=envelope.output_tokens,
        model=envelope.model,
    )


async def _stream_and_tee(
    stream: asyncio.StreamReader,
    log_file: IO[str],
    stdout_sink: IO[str],
) -> str:
    """Read ``stream`` line-by-line to ``log_file`` and ``stdout_sink``.

    Returns the full captured text so the caller can parse the token
    envelope and return the final ``stdout``/``stderr`` on the
    :class:`ClaudeInvocationResult`. Lines are decoded as UTF-8 with
    ``errors="replace"`` so malformed bytes never interrupt the stream.
    """

    chunks: list[str] = []
    while True:
        raw = await stream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace")
        chunks.append(line)
        try:
            log_file.write(line)
            log_file.flush()
        except Exception:  # noqa: BLE001 - logging must never kill the loop
            logger.exception("Failed to tee Claude Code CLI output to log file")
        try:
            stdout_sink.write(line)
            stdout_sink.flush()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to tee Claude Code CLI output to stdout sink")
    return "".join(chunks)


class ClaudeCodeInvoker:
    """Spawns Claude Code CLI as a subprocess and tees its output (R11.2, R11.5).

    The invoker is stateless; one instance can be reused across iterations
    of the Ralph Loop. The ``claude_cli_command`` argument is split with
    :func:`shlex.split` so callers can configure either a plain binary
    name (``"claude"``) or a full command with arguments
    (``"python /path/to/fake_claude.py --echo"``) — the latter is how the
    integration tests stub the real Claude Code CLI.
    """

    def __init__(self, claude_cli_command: str = "claude") -> None:
        argv = shlex.split(claude_cli_command)
        if not argv:
            raise ValueError("claude_cli_command must not be empty")
        self._argv_prefix = argv

    async def invoke(
        self,
        *,
        context: str,
        log_path: Path,
        call_kind: CallKind,
        timeout_ms: Optional[int] = None,
        cwd: Optional[Path] = None,
        stdout_sink: Optional[IO[str]] = None,
        model_id: Optional[str] = None,
    ) -> ClaudeInvocationResult:
        """Spawn Claude Code CLI, pipe ``context`` on stdin, tee output, return the result.

        Args:
            context: Composed prompt piped to the subprocess stdin (R6.1).
            log_path: Destination log file receiving both stdout and stderr
                line-by-line (R11.2, R11.5).
            call_kind: Classification used for logging and token
                accounting downstream (R12.1). The invoker does not decide
                ``call_kind`` itself; the caller maps the selection path
                (``"escalation"`` -> ``"escalation"``, otherwise
                ``"persona_execution"`` for persona iterations).
            timeout_ms: Optional overall timeout; on expiry the process
                is killed and :class:`ClaudeCodeInvocationTimeout` is raised.
            cwd: Optional working directory for the subprocess.
            stdout_sink: Optional writable text sink for the tee. Defaults
                to :data:`sys.stdout`. Tests pass an ``io.StringIO`` to
                capture both streams.
            model_id: Optional LLM model identifier. When set, the
                invoker appends ``--model <id>`` to the Claude Code CLI argv
                so the session uses the requested model. When omitted,
                Claude Code CLI's own default model selection applies.

        Returns:
            :class:`ClaudeInvocationResult` with ``exit_code``, ``stdout``,
            ``stderr``, optional ``token_usage`` (``None`` with a warning
            when the Claude Code CLI did not emit a parseable envelope, per
            R12.6), and ``duration_ms``.
        """

        sink = stdout_sink if stdout_sink is not None else sys.stdout
        argv = [
            *self._argv_prefix,
            "-p",
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
        ]
        if model_id is not None:
            argv.extend(["--model", model_id])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
        )
        # The subprocess module does guarantee these pipes are open when we
        # set PIPE above, but mypy/pyright still want the narrowing.
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None

        _streaming_tasks: list[asyncio.Task[str]] = []

        async def _run() -> tuple[str, str]:
            # Use append mode so repeated invocations targeting the same
            # per-iteration log path accumulate output rather than
            # overwrite. Higher layers own log-path selection.
            with log_path.open("a", encoding="utf-8") as log_file:
                # Write context then close stdin so Claude Code CLI sees EOF.
                try:
                    proc.stdin.write(context.encode("utf-8"))
                    await proc.stdin.drain()
                finally:
                    try:
                        proc.stdin.close()
                    except Exception:  # noqa: BLE001
                        pass

                stdout_task = asyncio.create_task(
                    _stream_and_tee(proc.stdout, log_file, sink)
                )
                stderr_task = asyncio.create_task(
                    _stream_and_tee(proc.stderr, log_file, sink)
                )
                _streaming_tasks.extend([stdout_task, stderr_task])
                stdout_text, stderr_text = await asyncio.gather(
                    stdout_task, stderr_task
                )
                await proc.wait()
                # Yield to flush any pending transport-close callbacks
                # scheduled by proc.wait() so pipe transports are released
                # before the event loop tears down.
                await asyncio.sleep(0)
                return stdout_text, stderr_text

        try:
            if timeout_ms is not None:
                stdout_text, stderr_text = await asyncio.wait_for(
                    _run(), timeout=timeout_ms / 1000.0
                )
            else:
                stdout_text, stderr_text = await _run()
        except asyncio.TimeoutError as exc:
            # Kill the child on timeout; wait() to reap.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            # Feed EOF and cancel/await the streaming tasks so the underlying
            # pipe transports (Unix-domain sockets) are released before the
            # event loop tears down. Without this, leaked transports generate
            # ResourceWarnings that corrupt the next test's cleanup.
            try:
                proc.stdout.feed_eof()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.stderr.feed_eof()
            except Exception:  # noqa: BLE001
                pass
            for task in _streaming_tasks:
                if not task.done():
                    task.cancel()
            if _streaming_tasks:
                await asyncio.gather(*_streaming_tasks, return_exceptions=True)
            # Yield to the event loop so any pending transport close callbacks
            # (scheduled by proc.wait()) are processed before we return.
            # Without this, the transports can remain open until GC fires
            # during a later test, generating spurious ResourceWarnings.
            await asyncio.sleep(0)
            raise ClaudeCodeInvocationTimeout(
                f"Claude Code CLI invocation (call_kind={call_kind!r}) exceeded "
                f"timeout_ms={timeout_ms}"
            ) from exc

        duration_ms = int((time.monotonic() - start) * 1000)

        token_usage: Optional[TokenUsage] = None
        for line in stdout_text.splitlines():
            parsed = _parse_token_line(line)
            if parsed is not None:
                token_usage = parsed  # Keep last match

        if token_usage is None:
            logger.warning(
                "Claude Code CLI call of kind %r reported no token usage "
                "(no valid JSON line with input_tokens and output_tokens found)",
                call_kind,
            )

        exit_code = proc.returncode if proc.returncode is not None else -1
        return ClaudeInvocationResult(
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            token_usage=token_usage,
            duration_ms=duration_ms,
        )
