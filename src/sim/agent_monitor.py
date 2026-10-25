"""Read-only Agent Monitor data preparation for one explicitly selected simulation run.

This module reads the runner's event journal, manifest, seven-column message CSV, and
run-local artifacts. It never infers an active state from configured agent names.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.core import msg_log, ontology


@dataclass(frozen=True)
class AgentMonitorSnapshot:
    run_dir: Path
    source_status: str
    run_id: str
    run_status: str
    freshness: str
    last_activity: str | None
    agents: tuple[dict[str, Any], ...]
    lifecycle_events: tuple[dict[str, Any], ...]
    timeline_events: tuple[dict[str, Any], ...]
    message_status: str
    messages: tuple[dict[str, Any], ...]
    message_counts: tuple[dict[str, Any], ...]
    flows: tuple[dict[str, Any], ...]
    months: tuple[dict[str, Any], ...]
    rounds: tuple[dict[str, Any], ...]
    rounds_status: str
    hotspots: tuple[dict[str, Any], ...]
    hotspots_status: str
    alerts: tuple[dict[str, Any], ...]
    alerts_status: str
    errors: tuple[str, ...]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_event_journal(path: str | Path) -> tuple[tuple[dict[str, Any], ...], str, tuple[str, ...]]:
    """Read newline-delimited run events, retaining valid rows and reporting malformed lines."""
    target = Path(path)
    if not target.is_file():
        return (), "unavailable", (f"Event journal not found: {target}",)
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        with target.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict) or not isinstance(event.get("event_type"), str):
                        raise ValueError("expected object with event_type")
                    events.append(event)
                except (json.JSONDecodeError, ValueError) as exc:
                    errors.append(f"Malformed event journal line {number}: {exc}")
    except (OSError, UnicodeError) as exc:
        return (), "incomplete", (f"Could not read event journal: {exc}",)
    status = "incomplete" if errors else "available" if events else "empty"
    return tuple(events), status, tuple(errors)


def read_message_events(path: str | Path) -> tuple[tuple[dict[str, Any], ...], str, tuple[str, ...]]:
    """Read only the exact shared seven-column logger schema; flag malformed rows/payloads."""
    target = Path(path)
    if not target.is_file():
        return (), "unavailable", (f"Message log not found: {target}",)
    try:
        empty_file = target.stat().st_size == 0
    except OSError as exc:
        return (), "incomplete", (f"Could not stat message log: {exc}",)
    if empty_file:
        return (), "empty", ("Message log exists but has no records or header yet",)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        with target.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != msg_log.COLUMNS:
                return (), "incomplete", (
                    f"Unexpected message-log columns: {reader.fieldnames!r}; expected {msg_log.COLUMNS!r}",
                )
            for number, row in enumerate(reader, start=2):
                if None in row or any(value is None for value in row.values()):
                    errors.append(f"Malformed message-log row {number}: missing or extra CSV cells")
                    continue
                if row.get("direction") not in msg_log.DIRECTIONS:
                    errors.append(f"Malformed message-log row {number}: invalid direction")
                    continue
                item: dict[str, Any] = dict(row)
                item["event_type"] = "unknown"
                try:
                    body = ontology.decode(row["performative"], row["payload"])
                    item["event_type"] = body.get("type", "unknown")
                    item["month_index"] = body.get("month_index")
                    item["ds_division"] = body.get("ds_division")
                    item["payload_status"] = "valid"
                except (ontology.SchemaError, TypeError, ValueError) as exc:
                    item["payload_status"] = "malformed"
                    errors.append(f"Malformed message payload at row {number}: {exc}")
                rows.append(item)
    except (OSError, UnicodeError, csv.Error) as exc:
        return (), "incomplete", (f"Could not read message log: {exc}",)
    status = "incomplete" if errors else "available" if rows else "empty"
    return tuple(rows), status, tuple(errors)


def _read_json_list(path: Path, *, key: str | None = None) -> tuple[tuple[dict[str, Any], ...], str, str]:
    if not path.is_file():
        return (), "unavailable", f"File not found: {path}"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if key is not None:
            if not isinstance(parsed, dict):
                raise ValueError("expected JSON object")
            parsed = parsed.get(key)
        if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
            raise ValueError("expected a list of JSON objects")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return (), "incomplete", f"Malformed or unreadable {path.name}: {exc}"
    return tuple(parsed), "available" if parsed else "empty", f"{len(parsed)} records" if parsed else "No records"


def _events_from_manifest(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    rows = manifest.get("agent_lifecycle", [])
    if not isinstance(rows, list):
        return ()
    translated = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        event = row.get("event")
        translated.append({
            "event_type": "agent_" + str(event), "timestamp": row.get("at"),
            "component": row.get("component"), "identity": row.get("jid"),
        })
    return tuple(translated)


def derive_agent_states(events: Sequence[Mapping[str, Any]], messages: Sequence[Mapping[str, Any]],
                        *, run_status: str, run_fresh: bool,
                        configured_agents: Sequence[Mapping[str, str]] = (),
                        now: datetime | None = None, stale_after_s: float = 15.0) -> tuple[dict[str, Any], ...]:
    """Derive visible states only from recorded lifecycle and message events."""
    identities: dict[str, dict[str, Any]] = {}
    for configured in configured_agents:
        identity = configured.get("identity")
        if isinstance(identity, str) and identity:
            identities[identity] = {"Component": configured.get("component", "Unknown component"),
                                    "Identity": identity, "_events": []}
    for event in events:
        identity = event.get("identity")
        component = event.get("component")
        if not isinstance(identity, str) or not identity:
            continue
        record = identities.setdefault(identity, {"Component": component or "Unknown component",
                                                   "Identity": identity, "_events": []})
        # Operational events such as round_completed carry agent identities too;
        # only lifecycle events may determine lifecycle state.
        if str(event.get("event_type", "")).startswith("agent_"):
            record["_events"].append(event)
    output = []
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for identity, record in identities.items():
        ordered = sorted(
            enumerate(record["_events"]),
            key=lambda pair: (
                pair[1].get("event_seq", 0) if isinstance(pair[1].get("event_seq"), int) else 0,
                _parse_time(pair[1].get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
                pair[0],
            ),
        )
        agent_events = [row for _, row in ordered]
        latest = agent_events[-1] if agent_events else {}
        start_event = next((row for row in agent_events
                            if row.get("event_type") == "agent_started"), None)
        starting_event = next((row for row in reversed(agent_events)
                               if row.get("event_type") == "agent_starting"), None)
        heartbeat_events = [row for row in agent_events
                            if row.get("event_type") == "agent_heartbeat"]
        last_heartbeat = max(
            (_parse_time(row.get("timestamp")) for row in heartbeat_events if _parse_time(row.get("timestamp"))),
            default=None,
        )
        failures = [row for row in agent_events if row.get("event_type") in {
            "agent_start_failed", "agent_stop_failed", "agent_behavior_failed", "agent_work_failed",
        }]
        work_completed = [row for row in agent_events
                          if row.get("event_type") == "agent_work_completed"]
        stopped = next((row for row in reversed(agent_events)
                        if row.get("event_type") == "agent_stopped"), None)
        waiting_events = [row for row in agent_events if row.get("event_type") == "agent_waiting"]
        phase_events = [row for row in agent_events if row.get("event_type") == "agent_phase"]
        latest_waiting = waiting_events[-1] if waiting_events else None
        latest_phase = phase_events[-1] if phase_events else None
        phase_candidates = [row for row in agent_events
                            if row.get("event_type") in {"agent_waiting", "agent_phase"}]
        latest_behavior_phase = phase_candidates[-1] if phase_candidates else None

        state = "unknown"
        reason = "no lifecycle evidence"
        if failures:
            state = "failed"
            reason = str(failures[-1].get("reason") or failures[-1].get("outcome") or "failure recorded")
        elif work_completed:
            state = "completed"
            reason = str(work_completed[-1].get("outcome", "run work completion recorded"))
        elif stopped:
            state = "unknown"
            reason = "agent stopped, but successful work completion was not recorded"
        elif start_event is not None:
            heartbeat_age = ((observed_now - last_heartbeat).total_seconds()
                             if last_heartbeat is not None else None)
            heartbeat_fresh = heartbeat_age is not None and heartbeat_age <= stale_after_s
            waiting_is_current = (
                latest_waiting is not None
                and (latest_phase is None or
                     (latest_waiting.get("event_seq", 0) if isinstance(latest_waiting.get("event_seq"), int) else 0)
                     > (latest_phase.get("event_seq", 0) if isinstance(latest_phase.get("event_seq"), int) else 0))
            )
            if run_status == "running" and heartbeat_fresh and waiting_is_current:
                state = "waiting"
                reason = str(latest_waiting.get("phase", "workflow response"))
            elif run_status == "running" and heartbeat_fresh:
                state = "running"
                reason = "recent per-agent heartbeat"
            elif last_heartbeat is None:
                state = "unknown"
                reason = "agent startup observed; per-agent heartbeat unavailable"
            elif run_status != "running":
                state = "unknown"
                reason = "run is not active and this agent has no recorded work outcome"
            else:
                state = "stale"
                reason = "per-agent heartbeat is stale"
        elif starting_event is not None:
            age = (observed_now - (_parse_time(starting_event.get("timestamp")) or observed_now)).total_seconds()
            if run_status == "running" and run_fresh and age <= stale_after_s:
                state = "starting"
                reason = "startup requested; startup confirmation not recorded"
            elif age > stale_after_s:
                state = "stale"
                reason = "startup requested without recent confirmation"

        last_seen: datetime | None = last_heartbeat
        if last_seen is None:
            last_seen = _parse_time(latest.get("timestamp"))
        heartbeat_age = ((observed_now - last_heartbeat).total_seconds()
                         if last_heartbeat is not None else None)
        latest_failure = failures[-1] if failures else None
        sent = received = 0
        for message in messages:
            if message.get("direction") == "sent" and message.get("sender") == identity:
                sent += 1
            elif message.get("direction") == "received" and message.get("receiver") == identity:
                received += 1
        if state == "running (start observed)":
            last_age = (observed_now - last_seen).total_seconds() if last_seen else None
            if last_age is None or last_age > stale_after_s:
                state = "stale (no recent agent event)"
        record.pop("_events", None)
        output.append({"Component": record["Component"], "Identity": identity, "Status": state,
                       "Last observed": last_seen.isoformat() if last_seen else "Unavailable",
                       "Latest heartbeat": last_heartbeat.isoformat() if last_heartbeat else "Unavailable",
                       "Heartbeat age (s)": round(heartbeat_age, 2) if heartbeat_age is not None else None,
                       "Latest phase": (latest_behavior_phase.get("phase")
                                        or latest_behavior_phase.get("outcome")
                                        or latest_behavior_phase.get("event_type")
                                        if latest_behavior_phase else "Unavailable"),
                       "Failure details": (latest_failure.get("reason") or latest_failure.get("outcome")
                                           or latest_failure.get("event_type")
                                           if latest_failure else "No failure recorded"),
                       "Sent messages": sent, "Received messages": received,
                       "Status evidence": reason})
    return tuple(sorted(output, key=lambda row: (row["Component"], row["Identity"])))


def _month_progress(events: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    months: dict[int, dict[str, Any]] = {}
    for event in events:
        event_type = event.get("event_type")
        index = event.get("month_index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        item = months.setdefault(index, {"month_index": index, "status": "unknown"})
        if event_type == "month_started":
            item.update(status="running", calendar_month=event.get("calendar_month"))
        elif event_type == "month_stage":
            stage = event.get("stage")
            if isinstance(stage, str):
                item[stage] = event.get("status", "unknown")
        elif event_type == "month_completed":
            item["status"] = event.get("status", "completed")
    manifest_rows = manifest.get("month_status", [])
    if isinstance(manifest_rows, list):
        for row in manifest_rows:
            if isinstance(row, dict) and isinstance(row.get("month_index"), int):
                months.setdefault(row["month_index"], {"month_index": row["month_index"]}).update(row)
    return tuple(months[index] for index in sorted(months))


def _configured_identities(manifest: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Expose configured but unobserved agents as unknown, never as running."""
    config = manifest.get("configuration", {})
    if not isinstance(config, Mapping):
        return ()
    domain = config.get("domain")
    identities = config.get("agent_identities", {})
    if not isinstance(domain, str) or not isinstance(identities, Mapping):
        return ()
    output: list[dict[str, str]] = []
    base = (("ClockAgent", "clock"), ("WellMonitorAgent", "well_monitor"),
            ("RiskScoringAgent", "risk_scoring"), ("HotspotAgent", "hotspot"),
            ("AlertProcessingAgent", "alert_processing"), ("AlertInboxAgent", "alert_inbox"))
    for component, key in base:
        local = identities.get(key)
        if isinstance(local, str) and local:
            output.append({"component": component, "identity": f"{local}@{domain}"})
    villages = manifest.get("divisions", [])
    prefix = identities.get("village_prefix", "village_")
    if isinstance(prefix, str) and isinstance(villages, list):
        import re
        for division in villages:
            if not isinstance(division, str):
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", division.lower()).strip("_")
            if slug:
                output.append({"component": f"VillageAgent:{division}",
                               "identity": f"{prefix}{slug}@{domain}"})
    start, end = config.get("start_month_index"), config.get("end_month_index")
    coordinator_prefix = identities.get("coordinator_prefix", "coordinator_m")
    if (isinstance(start, int) and isinstance(end, int) and isinstance(coordinator_prefix, str)
            and 0 <= end - start <= 1000):
        output.extend({"component": f"ResourceCoordinatorAgent:month_index={index}",
                       "identity": f"{coordinator_prefix}{index}@{domain}"}
                      for index in range(start, end + 1))
    return tuple(output)


def load_agent_monitor(run_dir: str | Path, *, stale_after_s: float = 15.0,
                       now: datetime | None = None) -> AgentMonitorSnapshot:
    """Read one explicitly selected multi-agent run; generated demo/team paths are not inferred."""
    root = Path(run_dir).expanduser()
    errors: list[str] = []
    try:
        parsed = json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("run_id"), str):
            raise ValueError("expected run manifest with run_id")
        manifest: Mapping[str, Any] = parsed
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return AgentMonitorSnapshot(root, "unavailable", "Unknown", "unknown", "unknown", None,
                                    (), (), (), "unavailable", (), (), (), (), (), "unavailable",
                                    (), "unavailable", (), "unavailable", (f"Run manifest unavailable: {exc}",))

    events, event_status, event_errors = read_event_journal(root / "agent_events.jsonl")
    errors.extend(event_errors)
    if event_status == "unavailable":
        events = _events_from_manifest(manifest)
        if events:
            event_status = "snapshot_from_manifest"
    messages, message_status, message_errors = read_message_events(root / "logs/messages.csv")
    errors.extend(message_errors)

    current = now or datetime.now(timezone.utc)
    runner_heartbeats = [_parse_time(event.get("timestamp")) for event in events
                         if event.get("event_type") in {"heartbeat", "runner_heartbeat"}]
    agent_heartbeats = [_parse_time(event.get("timestamp")) for event in events
                        if event.get("event_type") == "agent_heartbeat"]
    log_times = [_parse_time(row.get("timestamp")) for row in messages]
    times = [value for value in [*runner_heartbeats, *agent_heartbeats, *log_times]
             if value is not None]
    last_activity_dt = max(times) if times else None
    age = (current.astimezone(timezone.utc) - last_activity_dt).total_seconds() if last_activity_dt else None
    runner_heartbeat_dt = max((value for value in runner_heartbeats if value is not None), default=None)
    runner_heartbeat_age = ((current.astimezone(timezone.utc) - runner_heartbeat_dt).total_seconds()
                            if runner_heartbeat_dt is not None else None)
    run_status = str(manifest.get("status", "unknown"))
    if any(event.get("event_type") == "run_finished" for event in events):
        last_finished = next(event for event in reversed(events) if event.get("event_type") == "run_finished")
        run_status = str(last_finished.get("status", run_status))
    # Message traffic and agent heartbeats are activity evidence, but only the runner heartbeat
    # establishes that the orchestrator is still advancing/observing the run.
    fresh = runner_heartbeat_age is not None and runner_heartbeat_age <= stale_after_s
    if run_status == "running":
        freshness = "live" if fresh else "stale"
    elif run_status == "failed":
        freshness = "failed snapshot"
    elif run_status in {"completed", "completed_with_unavailable_stages"}:
        freshness = "completed snapshot"
    else:
        freshness = "unknown"

    counts: dict[str, dict[str, int]] = {}
    flows: dict[tuple[str, str], int] = {}
    for row in messages:
        sender, receiver = row.get("sender", ""), row.get("receiver", "")
        direction = row.get("direction")
        for identity in (sender, receiver):
            counts.setdefault(identity, {"sent": 0, "received": 0})
        if direction == "sent":
            counts[sender]["sent"] += 1
        elif direction == "received":
            counts[receiver]["received"] += 1
        flows[(sender, receiver)] = flows.get((sender, receiver), 0) + 1
    lifecycle = tuple(event for event in events
                      if str(event.get("event_type", "")).startswith("agent_")
                      and event.get("event_type") != "agent_heartbeat")
    timeline = tuple(sorted(
        events,
        key=lambda event: (
            _parse_time(event.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
            event.get("event_seq", 0) if isinstance(event.get("event_seq"), int) else 0,
        ),
    ))
    agents = derive_agent_states(events, messages, run_status=run_status, run_fresh=fresh,
                                 configured_agents=_configured_identities(manifest),
                                 now=current, stale_after_s=stale_after_s)
    rounds: tuple[dict[str, Any], ...] = ()
    rounds_status = "unavailable"
    rounds_message = "Round output directory not found"
    rounds_dir = root / "rounds"
    if rounds_dir.is_dir():
        values = []
        malformed_rounds = False
        for path in sorted(rounds_dir.glob("*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(row, dict) or "month_index" not in row or "award_status" not in row:
                    raise ValueError("round result requires month_index and award_status")
                values.append(row)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                malformed_rounds = True
                errors.append(f"Malformed round result {path.name}: {exc}")
        rounds = tuple(values)
        rounds_status = "incomplete" if malformed_rounds else "available" if rounds else "empty"
        rounds_message = (f"{len(rounds)} valid round records; some files are malformed" if malformed_rounds
                          else f"{len(rounds)} round records" if rounds else "No persisted round outcomes")
    else:
        rounds_status, rounds_message = "unavailable", "Round output directory not found"
    hotspots, hotspots_status, hotspots_message = _read_json_list(root / "hotspot_screening.json")
    if hotspots_status in {"unavailable", "empty"}:
        event_hotspots = tuple({
            "month_index": event.get("month_index"), "ds_division": event.get("ds_division"),
            "status": event.get("screening_status", "unavailable"),
            "reason": event.get("reason"), "provenance": "runner event journal",
        } for event in events if event.get("event_type") == "hotspot_result")
        if event_hotspots:
            hotspots, hotspots_status, hotspots_message = event_hotspots, "available", f"{len(event_hotspots)} journal events"
    alerts, alerts_status, alerts_message = _read_json_list(root / "alert_decisions.json")
    if alerts_status in {"unavailable", "empty"}:
        event_alerts = tuple({
            "month_index": event.get("month_index"), "ds_division": event.get("ds_division"),
            "status": event.get("decision_status", "unavailable"),
            "delivery_status": event.get("delivery_status", "unavailable"),
            "reason": event.get("reason"), "provenance": "runner event journal",
        } for event in events if event.get("event_type") == "alert_outcome")
        if event_alerts:
            alerts, alerts_status, alerts_message = event_alerts, "available", f"{len(event_alerts)} journal events"
    for label, status, message in (("hotspot", hotspots_status, hotspots_message),
                                   ("alerts", alerts_status, alerts_message)):
        if status in {"unavailable", "incomplete"}:
            errors.append(f"{label}: {message}")
    recent = tuple(sorted(messages, key=lambda row: _parse_time(row.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:30])
    count_rows = tuple({"Identity": identity, "Sent": value["sent"], "Received": value["received"]}
                       for identity, value in sorted(counts.items()))
    flow_rows = tuple({"Sender": sender, "Receiver": receiver, "Logged rows": total}
                      for (sender, receiver), total in sorted(flows.items()))
    return AgentMonitorSnapshot(
        root, "available", manifest["run_id"], run_status, freshness,
        last_activity_dt.isoformat() if last_activity_dt else None, agents, lifecycle, timeline,
        message_status, recent, count_rows, flow_rows,
        _month_progress(events, manifest), rounds, rounds_status,
        hotspots, f"{hotspots_status}: {hotspots_message}",
        alerts, f"{alerts_status}: {alerts_message}", tuple(errors),
    )
