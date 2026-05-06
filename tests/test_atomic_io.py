"""Unit tests for ``ralph_loop.atomic_io``.

These tests pin the R14.2 atomic-write contract:

* The destination file contains either the old contents or the new contents
  at every observable moment (no partial writes).
* On Windows, ``PermissionError`` from ``os.replace`` is retried with
  exponential backoff up to five times; transient failures resolve
  transparently, and a persistent failure surfaces as ``AtomicWriteError``
  with the temp file cleaned up.

Tests inject a fake ``sleep`` function so exponential-backoff delays never
hit the real wall clock.

Requirements validated: R14.2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable
from unittest import mock

import pytest

from ralph_loop import atomic_io
from ralph_loop.atomic_io import AtomicWriteError, atomic_write_bytes, atomic_write_text


def _no_sleep(_seconds: float) -> None:
    """No-op replacement for ``time.sleep`` used in retry tests."""


def _recording_sleep() -> tuple[Callable[[float], None], list[float]]:
    """Return a (sleep_fn, captured_delays) pair for assertions."""

    captured: list[float] = []

    def _sleep(seconds: float) -> None:
        captured.append(seconds)

    return _sleep, captured


def test_atomic_write_bytes_creates_new_file(tmp_path: Path) -> None:
    """A write to a non-existent path produces a file with exactly that data."""

    target = tmp_path / "tasks.json"

    atomic_write_bytes(target, b'{"tasks": []}', sleep=_no_sleep)

    assert target.read_bytes() == b'{"tasks": []}'
    # The tmp sibling must not be left behind after a successful write.
    assert not (tmp_path / "tasks.json.tmp").exists()


def test_atomic_write_bytes_overwrites_existing_file(tmp_path: Path) -> None:
    """Overwriting an existing file replaces its contents atomically."""

    target = tmp_path / "tasks.json"
    target.write_bytes(b"old contents")

    atomic_write_bytes(target, b"new contents", sleep=_no_sleep)

    assert target.read_bytes() == b"new contents"
    assert not (tmp_path / "tasks.json.tmp").exists()


def test_atomic_write_bytes_keeps_old_file_on_temp_write_failure(
    tmp_path: Path,
) -> None:
    """If writing the temp file fails, the destination stays intact.

    Simulates an ``os.write`` failure mid-write. The destination must still
    contain the prior contents and the temp file must be cleaned up.
    """

    target = tmp_path / "tasks.json"
    target.write_bytes(b"old contents")

    with mock.patch.object(
        atomic_io.os, "write", side_effect=OSError("disk full")
    ):
        with pytest.raises(AtomicWriteError, match="Failed to write temp file"):
            atomic_write_bytes(target, b"new contents", sleep=_no_sleep)

    # Old contents are still visible to readers.
    assert target.read_bytes() == b"old contents"
    # Temp file is cleaned up on failure.
    assert not (tmp_path / "tasks.json.tmp").exists()


def test_atomic_write_bytes_retries_permission_error_then_succeeds(
    tmp_path: Path,
) -> None:
    """``PermissionError`` on the first N replace attempts retries and succeeds."""

    target = tmp_path / "tasks.json"
    target.write_bytes(b"old")

    real_replace = atomic_io.os.replace
    call_counter = {"count": 0}

    def flaky_replace(src: str, dst: str) -> None:
        call_counter["count"] += 1
        if call_counter["count"] <= 2:
            raise PermissionError("file in use by another process")
        real_replace(src, dst)

    sleep_fn, sleeps = _recording_sleep()

    with mock.patch.object(atomic_io.os, "replace", side_effect=flaky_replace):
        atomic_write_bytes(target, b"new", sleep=sleep_fn)

    # The real replace eventually swapped the file in.
    assert target.read_bytes() == b"new"
    # Two failures plus one success == three attempts total.
    assert call_counter["count"] == 3
    # Two retry delays matched the first two entries of the backoff schedule.
    assert sleeps == [0.05, 0.10]
    # No lingering temp file.
    assert not (tmp_path / "tasks.json.tmp").exists()


def test_atomic_write_bytes_gives_up_after_five_retries(tmp_path: Path) -> None:
    """Persistent ``PermissionError`` raises ``AtomicWriteError`` after 5 retries.

    The temp file must be cleaned up on final failure and the destination
    (when present) must remain untouched.
    """

    target = tmp_path / "tasks.json"
    target.write_bytes(b"old")

    replace_calls = {"count": 0}

    def always_locked(_src: str, _dst: str) -> None:
        replace_calls["count"] += 1
        raise PermissionError("file permanently locked")

    sleep_fn, sleeps = _recording_sleep()

    with mock.patch.object(atomic_io.os, "replace", side_effect=always_locked):
        with pytest.raises(AtomicWriteError, match="after 6 attempts"):
            atomic_write_bytes(target, b"new", sleep=sleep_fn)

    # One initial attempt plus five retries == six total.
    assert replace_calls["count"] == 6
    # Exponential backoff schedule from the spec.
    assert sleeps == [0.05, 0.10, 0.20, 0.40, 0.80]
    # The destination is unchanged; readers see the old file.
    assert target.read_bytes() == b"old"
    # The temp file is cleaned up on final failure.
    assert not (tmp_path / "tasks.json.tmp").exists()


def test_atomic_write_bytes_preserves_permission_error_as_cause(
    tmp_path: Path,
) -> None:
    """The underlying ``PermissionError`` is chained via ``__cause__``."""

    target = tmp_path / "tasks.json"

    def always_locked(_src: str, _dst: str) -> None:
        raise PermissionError("locked")

    with mock.patch.object(atomic_io.os, "replace", side_effect=always_locked):
        with pytest.raises(AtomicWriteError) as excinfo:
            atomic_write_bytes(target, b"data", sleep=_no_sleep)

    assert isinstance(excinfo.value.__cause__, PermissionError)


def test_atomic_write_text_encodes_utf8_by_default(tmp_path: Path) -> None:
    """``atomic_write_text`` encodes to utf-8 and writes the byte content."""

    target = tmp_path / "notes.txt"

    atomic_write_text(target, "héllo 🌍", sleep=_no_sleep)

    assert target.read_bytes() == "héllo 🌍".encode("utf-8")


def test_atomic_write_text_respects_custom_encoding(tmp_path: Path) -> None:
    """A non-default encoding is honored end-to-end."""

    target = tmp_path / "latin.txt"

    atomic_write_text(target, "café", encoding="latin-1", sleep=_no_sleep)

    assert target.read_bytes() == "café".encode("latin-1")


def test_atomic_write_bytes_accepts_string_path(tmp_path: Path) -> None:
    """``str`` paths are accepted and produce the same result as ``Path``."""

    target = tmp_path / "str_path.json"

    atomic_write_bytes(str(target), b"ok", sleep=_no_sleep)

    assert target.read_bytes() == b"ok"


def test_atomic_write_bytes_empty_payload(tmp_path: Path) -> None:
    """Writing zero bytes produces an empty file (not a missing file)."""

    target = tmp_path / "empty.bin"

    atomic_write_bytes(target, b"", sleep=_no_sleep)

    assert target.exists()
    assert target.read_bytes() == b""
