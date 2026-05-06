"""Kiro CLI invocation wrapper (R6.1, R11.2, R11.5, R12.1, R12.6).

This module spawns ``kiro-cli chat --no-interactive`` as a subprocess,
pipes the composed context on stdin, and streams stdout/stderr
concurrently to both the per-iteration log file and process stdout
(R11.5). Token usage is parsed from a structured envelope line in the
Kiro CLI stdout via a dedicated Pydantic model; when the envelope is
absent we emit a warning and return ``token_usage=None`` (R12.6).

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
    KiroInvocationResult,
    TokenUsage,
)

logger = logging.getLogger(__name__)


# Marker line emitted by Kiro CLI (or a stub for tests) carrying the token
# envelope as JSON. Any stdout line containing this marker is parsed; the
# first successful parse wins.
TOKEN_MARKER = "RALPH_TOKEN_USAGE:"


class KiroTokenEnvelope(BaseModel):
    """Structured token envelope parsed from a Kiro CLI marker line (R12.6).

    This is an internal parser model; the outward-facing type is
    :class:`ralph_loop.models.TokenUsage`. Keeping this model separate
    lets the Kiro CLI change its envelope shape without altering the
    domain :class:`TokenUsage` definition.
    """

    input_tokens: int
    output_tokens: int
    model: Optional[str] = None


class KiroInvocationTimeout(Exception):
    """Raised when a Kiro CLI invocation exceeds the caller's ``timeout_ms``."""


def _parse_token_line(line: str) -> Optional[TokenUsage]:
    """Return a :class:`TokenUsage` if ``line`` carries the token envelope.

    Any line containing :data:`TOKEN_MARKER` is treated as a candidate; the
    remainder after the marker must be valid JSON that validates against
    :class:`KiroTokenEnvelope`. Malformed candidates are silently ignored so
    a single corrupt line does not mask a later good one.
    """

    idx = line.find(TOKEN_MARKER)
    if idx < 0:
        return None
    payload = line[idx + len(TOKEN_MARKER) :].strip()
    try:
        parsed = json.loads(payload)
        envelope = KiroTokenEnvelope.model_validate(parsed)
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
    :class:`KiroInvocationResult`. Lines are decoded as UTF-8 with
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
            logger.exception("Failed to tee Kiro CLI output to log file")
        try:
            stdout_sink.write(line)
            stdout_sink.flush()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to tee Kiro CLI output to stdout sink")
    return "".join(chunks)


class KiroInvoker:
    """Spawns Kiro CLI as a subprocess and tees its output (R11.2, R11.5).

    The invoker is stateless; one instance can be reused across iterations
    of the Ralph Loop. The ``kiro_cli_command`` argument is split with
    :func:`shlex.split` so callers can configure either a plain binary
    name (``"kiro-cli"``) or a full command with arguments
    (``"python /path/to/fake_kiro.py --echo"``) — the latter is how the
    integration tests stub the real Kiro CLI.
    """

    def __init__(self, kiro_cli_command: str = "kiro-cli") -> None:
        argv = shlex.split(kiro_cli_command)
        if not argv:
            raise ValueError("kiro_cli_command must not be empty")
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
    ) -> KiroInvocationResult:
        """Spawn Kiro CLI, pipe ``context`` on stdin, tee output, return the result.

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
                is killed and :class:`KiroInvocationTimeout` is raised.
            cwd: Optional working directory for the subprocess.
            stdout_sink: Optional writable text sink for the tee. Defaults
                to :data:`sys.stdout`. Tests pass an ``io.StringIO`` to
                capture both streams.
            model_id: Optional LLM model identifier. When set, the
                invoker appends ``--model <id>`` to the Kiro CLI argv
                so the session uses the requested model. When omitted,
                Kiro CLI's own default model selection applies.

        Returns:
            :class:`KiroInvocationResult` with ``exit_code``, ``stdout``,
            ``stderr``, optional ``token_usage`` (``None`` with a warning
            when the Kiro CLI did not emit a parseable envelope, per
            R12.6), and ``duration_ms``.
        """

        sink = stdout_sink if stdout_sink is not None else sys.stdout
        argv = [*self._argv_prefix, "chat", "--no-interactive", "--trust-all-tools"]
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

        async def _run() -> tuple[str, str]:
            # Use append mode so repeated invocations targeting the same
            # per-iteration log path accumulate output rather than
            # overwrite. Higher layers own log-path selection.
            with log_path.open("a", encoding="utf-8") as log_file:
                # Write context then close stdin so Kiro CLI sees EOF.
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
                stdout_text, stderr_text = await asyncio.gather(
                    stdout_task, stderr_task
                )
                await proc.wait()
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
            raise KiroInvocationTimeout(
                f"Kiro CLI invocation (call_kind={call_kind!r}) exceeded "
                f"timeout_ms={timeout_ms}"
            ) from exc

        duration_ms = int((time.monotonic() - start) * 1000)

        token_usage: Optional[TokenUsage] = None
        for line in stdout_text.splitlines():
            parsed = _parse_token_line(line)
            if parsed is not None:
                token_usage = parsed
                break

        if token_usage is None:
            logger.warning(
                "Kiro CLI call of kind %r reported no token usage "
                "(marker %r not found or unparseable)",
                call_kind,
                TOKEN_MARKER,
            )

        exit_code = proc.returncode if proc.returncode is not None else -1
        return KiroInvocationResult(
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            token_usage=token_usage,
            duration_ms=duration_ms,
        )
