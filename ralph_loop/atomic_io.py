"""Atomic filesystem I/O helpers for durable state writes (R14.2).

Pattern: write contents to ``<path>.tmp``, ``os.fsync`` the file descriptor,
close it, then ``os.replace(<tmp>, <path>)`` which is atomic on POSIX and
Windows. On Windows, ``os.replace`` can raise ``PermissionError`` if another
process briefly holds an open file handle to the destination; we retry with
exponential backoff (50ms, 100ms, 200ms, 400ms, 800ms; max 5 attempts).

This module is intentionally dependency-free: the pure filesystem contract
(old or new file visible at all times, never partial) is the single invariant
callers rely on. Higher layers (``tasks.json`` and ``pending_tasks.json``
writers) compose JSON serialization on top of these helpers.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional, Union

# Exponential backoff delays for Windows ``PermissionError`` retries (R14.2).
# The initial ``os.replace`` attempt is not counted here; these are the
# delays slept between retries, giving a total of 1 + 5 = 6 attempts in the
# worst case with a cumulative sleep budget of 1.55s before giving up.
_RETRY_DELAYS_S: tuple[float, ...] = (0.05, 0.10, 0.20, 0.40, 0.80)


class AtomicWriteError(OSError):
    """Raised when an atomic write cannot complete after all retries.

    Inherits from ``OSError`` so callers that already handle filesystem
    errors with a broad ``except OSError`` continue to work.
    """


def _cleanup_tmp(tmp_path: Path) -> None:
    """Best-effort removal of a leftover temp file; never raises."""

    try:
        if tmp_path.exists():
            tmp_path.unlink()
    except OSError:
        # Swallow secondary failures; the primary error is what matters.
        pass


def atomic_write_bytes(
    path: Union[str, Path],
    data: bytes,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Atomically write ``data`` to ``path``.

    Writes to ``<path>.tmp``, fsyncs, closes, then uses ``os.replace`` to swap
    the temp file into place. On Windows ``PermissionError``, retries up to
    five times with exponential backoff before giving up.

    Args:
        path: Destination path. Parent directory must already exist.
        data: Bytes to write.
        sleep: Injected sleep function (defaults to ``time.sleep``). Tests
            pass a no-op or recording stub to avoid real wall-clock delays.

    Raises:
        AtomicWriteError: If the initial write to the temp file fails or the
            retried ``os.replace`` never succeeds. The temp file is removed
            on failure (best effort).
    """

    path = Path(path)
    tmp_path = path.with_name(path.name + ".tmp")

    # Phase 1: write and fsync the temp file. Any failure here is fatal;
    # we clean up and raise, having never touched the destination.
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        _cleanup_tmp(tmp_path)
        raise AtomicWriteError(
            f"Failed to write temp file {tmp_path}: {exc}"
        ) from exc

    # Phase 2: atomic rename with Windows-aware retry. On POSIX the first
    # attempt always succeeds and the retry path is never exercised.
    last_error: Optional[PermissionError] = None
    for attempt in range(1 + len(_RETRY_DELAYS_S)):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt < len(_RETRY_DELAYS_S):
                sleep(_RETRY_DELAYS_S[attempt])
                continue
            # Fell through all retries.
            break

    # All attempts exhausted: clean up the temp file and surface the error.
    _cleanup_tmp(tmp_path)
    raise AtomicWriteError(
        f"Failed to atomically replace {path} after "
        f"{1 + len(_RETRY_DELAYS_S)} attempts: {last_error}"
    ) from last_error


def atomic_write_text(
    path: Union[str, Path],
    text: str,
    *,
    encoding: str = "utf-8",
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Atomically write ``text`` encoded as ``encoding`` (default utf-8).

    Thin convenience wrapper around :func:`atomic_write_bytes`.
    """

    atomic_write_bytes(path, text.encode(encoding), sleep=sleep)
