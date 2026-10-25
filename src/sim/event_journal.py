"""Append-only, run-local event journal shared by the runner and SPADE agents.

The simulation is hosted in one Python process. A process-local lock serializes writes from its
async tasks; this journal is operational telemetry, separate from the seven-column message CSV.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_SEQUENCES: dict[Path, int] = {}
_RUN_IDS: dict[Path, str] = {}


def register_run(path: str | Path, run_id: str) -> None:
    """Associate a fresh run journal path with its manifest run identifier."""
    target = Path(path)
    resolved = target.resolve()
    with _LOCK:
        _RUN_IDS[resolved] = run_id
        if target.is_file() and target.stat().st_size == 0:
            _SEQUENCES[resolved] = 0


def append_event(path: str | Path, event_type: str, component: str, *,
                 identity: str | None = None, run_id: str | None = None,
                 **details: Any) -> dict[str, Any]:
    """Append one sequenced UTC event. Callers supply only event-specific fields in details."""
    target = Path(path)
    resolved = target.resolve()
    reserved = {"timestamp", "event_seq", "event_type", "component", "identity", "run_id"}
    if reserved.intersection(details):
        raise ValueError(f"event details cannot override reserved fields: {sorted(reserved.intersection(details))}")
    with _LOCK:
        if resolved not in _SEQUENCES:
            sequence = 0
            if target.is_file():
                # Continue correctly if a reader/tool opened an existing journal in this process.
                with target.open(encoding="utf-8") as existing:
                    for line in existing:
                        try:
                            row = json.loads(line)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if isinstance(row, dict) and isinstance(row.get("event_seq"), int):
                            sequence = max(sequence, row["event_seq"])
            _SEQUENCES[resolved] = sequence
        _SEQUENCES[resolved] += 1
        row: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "event_seq": _SEQUENCES[resolved],
            "event_type": event_type,
            "component": component,
        }
        selected_run_id = run_id or _RUN_IDS.get(resolved)
        if selected_run_id is not None:
            row["run_id"] = selected_run_id
        if identity is not None:
            row["identity"] = identity
        row.update(details)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()
        return row
