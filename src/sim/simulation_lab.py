"""Input validation and isolated background-process control for the Streamlit Simulation Lab.

This module launches the existing SPADE runner. It does not implement simulation behavior.
"""
from __future__ import annotations

import csv
import contextlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from src.core import ontology, wells
from src.core.hotspot_screening import KNOWN_TIERS

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_INPUT = ROOT / "data/processed/synthetic_wells.csv"
INPUT_COLUMNS = tuple(column for column in wells.READING_COLUMNS if column != "month")
_PROCESSES: dict[str, subprocess.Popen[Any]] = {}
_PROCESS_LOG_HANDLES: dict[str, Any] = {}
_PROCESS_LOCK = threading.Lock()
HOTSPOT_TRIGGER_TIERS = tuple(sorted(KNOWN_TIERS - {"Unknown"}))


def resolve_hotspot_configuration(
    enabled: bool,
    trigger_tiers: tuple[str, ...] | list[str],
    consecutive_months: int,
) -> tuple[tuple[str, ...], int | None]:
    """Validate the optional UI selection and return runner settings.

    Disabled configuration always resolves to no rule, even if previous widget values
    remain in Streamlit session state. Enabled configuration requires user selections.
    """
    if not isinstance(enabled, bool):
        raise ValueError("hotspot configuration checkbox must be true or false")
    if not enabled:
        return (), None
    if not isinstance(trigger_tiers, (tuple, list)):
        raise ValueError("select at least one valid hotspot trigger tier")
    if any(not isinstance(tier, str) for tier in trigger_tiers):
        raise ValueError("select only valid hotspot trigger tiers")
    selected = tuple(dict.fromkeys(trigger_tiers))
    if not selected:
        raise ValueError("select at least one hotspot trigger tier")
    invalid = set(selected) - set(HOTSPOT_TRIGGER_TIERS)
    if invalid:
        raise ValueError(f"unsupported hotspot trigger tier(s): {sorted(invalid)}")
    if (isinstance(consecutive_months, bool) or not isinstance(consecutive_months, int)
            or consecutive_months < 2):
        raise ValueError("hotspot screening requires at least 2 consecutive month_index records")
    return selected, consecutive_months


@dataclass(frozen=True)
class InputPreview:
    path: Path | None
    row_count: int
    preview: tuple[dict[str, str], ...]
    source_label: str


@dataclass(frozen=True)
class SimulationSettings:
    start_month_index: int
    month_count: int
    first_calendar_month: int
    resource: str
    resource_pool: int
    round_number: int
    proposal_timeout_s: float
    confirmation_timeout_s: float
    stage_timeout_s: float
    hotspot_stage_timeout_s: float = 5.0
    alert_receive_timeout_s: float = 0.2
    alert_stage_timeout_s: float = 5.0
    alert_acknowledgement_timeout_s: float = 2.0
    hotspot_trigger_tiers: tuple[str, ...] = ()
    hotspot_consecutive_months: int | None = None

    def __post_init__(self) -> None:
        for name, value in (("start_month_index", self.start_month_index),
                            ("resource_pool", self.resource_pool),
                            ("round_number", self.round_number)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if isinstance(self.month_count, bool) or not isinstance(self.month_count, int) or self.month_count < 1:
            raise ValueError("month_count must be a positive integer")
        if isinstance(self.first_calendar_month, bool) or self.first_calendar_month not in range(1, 13):
            raise ValueError("first_calendar_month must be in 1..12")
        if self.resource not in ontology.RESOURCES:
            raise ValueError(f"resource must be one of {sorted(ontology.RESOURCES)}")
        for name in ("proposal_timeout_s", "confirmation_timeout_s", "stage_timeout_s",
                     "hotspot_stage_timeout_s", "alert_receive_timeout_s", "alert_stage_timeout_s",
                     "alert_acknowledgement_timeout_s"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be positive")
        if self.hotspot_trigger_tiers and self.hotspot_consecutive_months is None:
            raise ValueError("hotspot_consecutive_months is required when trigger tiers are configured")
        if not self.hotspot_trigger_tiers and self.hotspot_consecutive_months is not None:
            raise ValueError("hotspot trigger tiers are required when a screening window is configured")
        if self.hotspot_consecutive_months is not None and (
            isinstance(self.hotspot_consecutive_months, bool)
            or not isinstance(self.hotspot_consecutive_months, int)
            or self.hotspot_consecutive_months < 2
        ):
            raise ValueError("hotspot_consecutive_months must be at least 2")
        if "Unknown" in self.hotspot_trigger_tiers:
            raise ValueError("Unknown cannot be configured as a triggering hotspot tier")
        supported_tiers = {"Excellent", "Good", "Poor", "Very Poor", "Unsuitable"}
        if set(self.hotspot_trigger_tiers) - supported_tiers:
            raise ValueError("hotspot_trigger_tiers contains an unsupported risk tier")

    @property
    def end_month_index(self) -> int:
        return self.start_month_index + self.month_count - 1

    @property
    def calendar_months(self) -> tuple[int, ...]:
        # The user supplies the first label; the runner still receives every explicit label.
        return tuple((self.first_calendar_month - 1 + offset) % 12 + 1
                     for offset in range(self.month_count))


def validate_input_bytes(contents: bytes, *, preview_rows: int = 20) -> InputPreview:
    """Validate upload bytes against the actual seasonal input accepted by Member A's loader."""
    if not isinstance(contents, bytes) or not contents:
        raise ValueError("CSV upload is empty")
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8 encoded") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(INPUT_COLUMNS):
        raise ValueError(f"CSV columns must exactly match the project's seasonal input schema: {list(INPUT_COLUMNS)!r}")
    rows: list[dict[str, str]] = []
    for number, row in enumerate(reader, start=2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"CSV row {number} has missing or extra cells")
        rows.append({key: value for key, value in row.items()})
    if not rows:
        raise ValueError("CSV must contain at least one data row")

    # Reuse the same existing parsing, provenance guard, and seasonal expansion used by the runner.
    with tempfile.TemporaryDirectory() as temporary_root:
        temporary_path = Path(temporary_root) / "input.csv"
        temporary_path.write_bytes(contents)
        from src.sim import load_data
        monthly = load_data.load_monthly_readings(temporary_path)
    if monthly.empty:
        raise ValueError("input produced no monthly readings in the existing loader")
    return InputPreview(None, len(rows), tuple(rows[:max(0, preview_rows)]),
                        "Synthetic seasonal well readings (validated by runner input loader)")


def validate_input_path(path: str | Path = SAMPLE_INPUT, *, preview_rows: int = 20) -> InputPreview:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"input CSV not found: {target}")
    result = validate_input_bytes(target.read_bytes(), preview_rows=preview_rows)
    return InputPreview(target, result.row_count, result.preview,
                        "Built-in synthetic sample dataset" if target.resolve() == SAMPLE_INPUT.resolve()
                        else result.source_label)


def default_lab_root() -> Path:
    configured = os.environ.get("CKDU_SIMULATION_LAB_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(tempfile.gettempdir()) / "ckdu-mas-simulation-lab").resolve()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    with temp.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    temp.replace(path)


@contextlib.contextmanager
def _exclusive_launch_lock(path: Path):
    """Serialize simultaneous UI submissions on macOS/Linux and Windows."""
    with path.open("a+b") as stream:
        try:
            import fcntl
        except ImportError:
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _process_is_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _manifest_status(run_dir: Path) -> tuple[str | None, str | None]:
    manifest = _read_json(run_dir / "run_manifest.json")
    if manifest is None:
        return None, None
    status = manifest.get("status")
    reason = None
    failures = manifest.get("failures")
    if isinstance(failures, list) and failures:
        first = failures[0]
        if isinstance(first, dict):
            reason = str(first.get("error") or first.get("stage") or "") or None
    if status in {"completed", "completed_with_unavailable_stages"}:
        return str(status), None
    if status == "failed":
        return "failed", reason or "Simulation runner reported failure"
    if status == "running":
        return "running", None
    return None, reason


def _resolve_run_state(record: Mapping[str, Any], root: Path) -> dict[str, Any]:
    run_id = str(record.get("run_id", ""))
    run_dir = Path(str(record.get("run_dir", "")))
    pid = record.get("pid")
    with _PROCESS_LOCK:
        process = _PROCESSES.get(run_id)
        return_code = process.poll() if process is not None else None
        if return_code is not None:
            log_handle = _PROCESS_LOG_HANDLES.pop(run_id, None)
            if log_handle is not None:
                log_handle.close()
    manifest_state, manifest_error = _manifest_status(run_dir)
    if manifest_state == "completed":
        status, error = "completed", None
    elif manifest_state == "completed_with_unavailable_stages":
        status, error = "completed_with_unavailable_stages", None
    elif manifest_state == "failed":
        status, error = "failed", manifest_error
    elif return_code is not None:
        status = "failed"
        error = (f"Simulation process exited with code {return_code}"
                 if return_code != 0 else
                 "Simulation process exited without a completed run manifest")
    elif (manifest_state is None and record.get("status") == "queued"
          and (process is not None or _process_is_alive(pid))):
        status, error = "queued", None
    elif manifest_state == "running" and (process is not None or _process_is_alive(pid)):
        status, error = "running", None
    elif process is not None or _process_is_alive(pid):
        status, error = "running", None
    elif record.get("status") == "queued":
        status, error = "failed", "Runner process exited before writing a run manifest"
    else:
        status, error = str(record.get("status", "unknown")), record.get("error")
    resolved = dict(record)
    resolved.update(status=status, error=error, manifest_status=manifest_state)
    return resolved


def list_lab_runs(lab_root: str | Path) -> tuple[dict[str, Any], ...]:
    root = Path(lab_root).expanduser().resolve()
    controls = root / "control"
    if not controls.is_dir():
        return ()
    rows = []
    for path in controls.glob("*.json"):
        record = _read_json(path)
        if record is not None:
            rows.append(_resolve_run_state(record, root))
    return tuple(sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True))


def build_runner_command(settings: SimulationSettings, *, input_path: str | Path,
                         output_path: str | Path, run_id: str) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("run_id contains unsupported characters")
    args = [
        sys.executable, "-m", "src.sim.runner",
        "--start-month-index", str(settings.start_month_index),
        "--end-month-index", str(settings.end_month_index),
        "--calendar-months", ",".join(map(str, settings.calendar_months)),
        "--output-dir", str(Path(output_path).resolve()),
        "--resource", settings.resource,
        "--resource-pool", str(settings.resource_pool),
        "--round-number", str(settings.round_number),
        "--proposal-timeout", str(settings.proposal_timeout_s),
        "--confirmation-timeout", str(settings.confirmation_timeout_s),
        "--stage-timeout", str(settings.stage_timeout_s),
        "--hotspot-stage-timeout", str(settings.hotspot_stage_timeout_s),
        "--alert-receive-timeout", str(settings.alert_receive_timeout_s),
        "--alert-stage-timeout", str(settings.alert_stage_timeout_s),
        "--alert-acknowledgement-timeout", str(settings.alert_acknowledgement_timeout_s),
        "--input", str(Path(input_path).resolve()),
        "--run-id", run_id,
        "--embedded-server",
    ]
    for tier in settings.hotspot_trigger_tiers:
        args.extend(("--hotspot-trigger-tier", tier))
    if settings.hotspot_consecutive_months is not None:
        args.extend(("--hotspot-consecutive-months", str(settings.hotspot_consecutive_months)))
    return args


def launch_simulation(lab_root: str | Path, settings: SimulationSettings, *,
                      input_path: str | Path | None = None,
                      uploaded_csv: bytes | None = None,
                      popen_factory: Callable[..., Any] = subprocess.Popen) -> dict[str, Any]:
    """Reserve a new run path and launch the existing runner without blocking the caller."""
    if (input_path is None) == (uploaded_csv is None):
        raise ValueError("provide exactly one of input_path or uploaded_csv")
    root = Path(lab_root).expanduser().resolve()
    # Protect the configured root before creating any of its subdirectories.
    from src.sim.runner import validate_output_dir
    preflight = root / "runs" / f".preflight-{uuid.uuid4().hex}"
    validate_output_dir(preflight)
    root.mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(exist_ok=True)
    (root / "inputs").mkdir(exist_ok=True)
    (root / "control").mkdir(exist_ok=True)
    (root / "process_logs").mkdir(exist_ok=True)

    lock_path = root / ".launch.lock"
    with _exclusive_launch_lock(lock_path):
        for prior in list_lab_runs(root):
            if prior["status"] in {"queued", "running"}:
                raise RuntimeError(f"simulation {prior['run_id']} is already {prior['status']}; wait for it to finish")

        run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]
        run_dir = root / "runs" / run_id
        control_path = root / "control" / f"{run_id}.json"
        if run_dir.exists() or control_path.exists():
            raise FileExistsError(f"refusing to reuse simulation run path: {run_id}")

        if uploaded_csv is not None:
            input_preview = validate_input_bytes(uploaded_csv)
            selected_input = root / "inputs" / f"{run_id}.csv"
            with selected_input.open("xb") as stream:
                stream.write(uploaded_csv)
        else:
            selected_input = Path(input_path).expanduser().resolve()
            input_preview = validate_input_path(selected_input)

        # The runner performs this authoritative path protection again on startup.
        validate_output_dir(run_dir)
        log_path = root / "process_logs" / f"{run_id}.log"
        state: dict[str, Any] = {
            "run_id": run_id,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_dir": str(run_dir),
            "input_path": str(selected_input),
            "input_source": ("uploaded synthetic CSV" if uploaded_csv is not None
                             else input_preview.source_label),
            "input_record_count": input_preview.row_count,
            "input_columns": list(input_preview.preview[0]) if input_preview.preview else [],
            "process_log": str(log_path),
            "settings": {
                "start_month_index": settings.start_month_index,
                "end_month_index": settings.end_month_index,
                "calendar_months": list(settings.calendar_months),
                "resource": settings.resource,
                "resource_pool": settings.resource_pool,
                "round_number": settings.round_number,
            },
            "error": None,
        }
        _write_json_exclusive(control_path, state)
        command = build_runner_command(settings, input_path=selected_input,
                                       output_path=run_dir, run_id=run_id)
        try:
            log_handle = log_path.open("xb")
            process = popen_factory(command, cwd=ROOT, stdout=log_handle,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        except Exception as exc:
            if "log_handle" in locals():
                log_handle.close()
            state.update(status="failed", error=f"Could not start runner: {type(exc).__name__}: {exc}")
            _write_json_atomic(control_path, state)
            raise RuntimeError(state["error"]) from exc
        state.update(pid=process.pid, command=command)
        _write_json_atomic(control_path, state)
        with _PROCESS_LOCK:
            _PROCESSES[run_id] = process
            _PROCESS_LOG_HANDLES[run_id] = log_handle
        return _resolve_run_state(state, root)


def get_run_status(run_id: str, lab_root: str | Path) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        return None
    root = Path(lab_root).expanduser().resolve()
    record = _read_json(root / "control" / f"{run_id}.json")
    return _resolve_run_state(record, root) if record is not None else None


def read_process_log_tail(path: str | Path, *, max_bytes: int = 12000) -> str:
    """Return a bounded UTF-8-safe tail for a failed run's subprocess log."""
    target = Path(path)
    if not target.is_file():
        return "Process log is unavailable."
    try:
        with target.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            data = stream.read(max_bytes)
        return data.decode("utf-8", errors="replace") or "Process log is empty."
    except OSError as exc:
        return f"Could not read process log: {exc}"
