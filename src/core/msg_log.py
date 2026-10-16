"""Append-only, process-safe CSV logging for agent messages.

Agents should call :func:`log_message` for sent messages and
:func:`log_received_message` for received messages. Both event records use the
required six-column format; no direction column is added because the project
brief fixes the CSV schema exactly. Records are written once under an OS file
lock, so multiple threads/processes can share one log path.
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CSV_COLUMNS = ("timestamp", "sender", "receiver", "performative", "thread_id", "payload")
DEFAULT_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "messages.csv"


def _lock(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _csv_line(values: tuple[str, ...]) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerow(values)
    return buffer.getvalue().encode("utf-8")


def _write_all(fd: int, data: bytes) -> None:
    """Finish a locked append even if the OS reports a partial write."""
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("message log append made no progress")
        view = view[count:]


def log_message(
    sender: str,
    receiver: str,
    performative: str,
    thread_id: str,
    payload: Mapping[str, Any],
    log_path: str | os.PathLike[str] | None = None,
) -> None:
    """Append one sent-message event to the configured CSV log.

    ``log_path`` is injectable for tests and alternate runs. If omitted, the
    path is ``logs/messages.csv`` at the repository root. The payload must be
    a JSON-object mapping; validation against the ontology is the caller's
    responsibility so the logger remains usable during schema integration.
    """
    _append(sender, receiver, performative, thread_id, payload, log_path)


def log_received_message(
    sender: str,
    receiver: str,
    performative: str,
    thread_id: str,
    payload: Mapping[str, Any],
    log_path: str | os.PathLike[str] | None = None,
) -> None:
    """Append one received-message event using the same fixed CSV contract."""
    _append(sender, receiver, performative, thread_id, payload, log_path)


def _append(
    sender: str,
    receiver: str,
    performative: str,
    thread_id: str,
    payload: Mapping[str, Any],
    log_path: str | os.PathLike[str] | None,
) -> None:
    for name, value in (
        ("sender", sender),
        ("receiver", receiver),
        ("performative", performative),
        ("thread_id", thread_id),
    ):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping / JSON object")
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    path = Path(log_path) if log_path is not None else DEFAULT_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        _lock(fd)
        try:
            if os.fstat(fd).st_size == 0:
                _write_all(fd, _csv_line(CSV_COLUMNS))
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            row = (timestamp, sender, receiver, performative, thread_id, payload_text)
            data = _csv_line(row)
            _write_all(fd, data)
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
