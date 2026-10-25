"""Append-only message log at logs/messages.csv. Feeds the evaluation metrics.

Columns: timestamp, sender, receiver, performative, thread_id, payload, direction

``direction`` is "sent" or "received". Both ends are logged so alert latency and negotiation timing
can be measured; without it a sent row and its received row would be indistinguishable.

Several agent processes write to the same file, so every write takes an exclusive lock file
(``messages.csv.lock``) created atomically with O_EXCL, writes one row, flushes, and releases.

Pure module: no SPADE imports. Agents pass plain strings.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COLUMNS = ["timestamp", "sender", "receiver", "performative", "thread_id", "payload", "direction"]
DIRECTIONS = frozenset({"sent", "received"})
DEFAULT_PATH = Path("logs") / "messages.csv"

LOCK_TIMEOUT_S = 10.0
LOCK_STALE_S = 30.0


class LogLockTimeout(TimeoutError):
    """Raised when the log lock cannot be taken within LOCK_TIMEOUT_S."""


def _acquire(lock: Path) -> None:
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return
        except (FileExistsError, PermissionError):
            # PermissionError: on Windows the lock is mid-delete by another process; retry.
            # A crashed writer can leave the lock behind; break it once it is clearly stale.
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                    lock.unlink(missing_ok=True)
            except OSError:
                pass  # lock vanished or is mid-delete; just retry
            if time.monotonic() > deadline:
                raise LogLockTimeout(f"could not lock {lock} within {LOCK_TIMEOUT_S}s")
            time.sleep(0.005)


def log_message(
    sender: str,
    receiver: str,
    performative: str,
    thread_id: str | None,
    payload: str | dict[str, Any],
    direction: str,
    path: str | Path = DEFAULT_PATH,
) -> None:
    """Append one message row. ``payload`` may be the JSON body string or a dict."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {sorted(DIRECTIONS)}, got {direction!r}")
    if not isinstance(payload, str):
        payload = json.dumps(payload, sort_keys=True)

    row = [
        datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        sender,
        receiver,
        performative,
        thread_id or "",
        payload,
        direction,
    ]
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(row)
    line = buf.getvalue()

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    _acquire(lock)
    try:
        new_file = not path.exists() or path.stat().st_size == 0
        with open(path, "a", encoding="utf-8", newline="") as fh:
            if new_file:
                fh.write(",".join(COLUMNS) + "\n")
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        lock.unlink(missing_ok=True)
