"""Read-only, selected-run detail data for the Simulation Lab dashboard.

All artifact paths are fixed relative to one validated run directory. Event-journal entries and
SPADE message rows remain separate evidence records; the message log has no unique message ID.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.core import ontology
from src.sim.agent_monitor import AgentMonitorSnapshot, _parse_time, load_agent_monitor, read_event_journal, read_message_events


EVALUATION_COLUMNS = ("metric", "scope", "value", "unit", "status", "observations", "details")
AGENT_RESPONSIBILITIES = {
    "ClockAgent": "Publishes configured month_tick messages.",
    "WellMonitorAgent": "Selects configured readings and sends well_readings messages.",
    "RiskScoringAgent": "Scores readings using the existing reduced-parameter WQI and risk tiers.",
    "VillageAgent": "Tracks current-month risk and responds to Contract Net CFP/award messages.",
    "ResourceCoordinatorAgent": "Runs one bounded Contract Net round and records its outcome.",
    "HotspotAgent": "Applies the configured project-defined provisional persistence screen.",
    "AlertProcessingAgent": "Pairs risk and hotspot evidence, makes alert decisions, and sends eligible alerts.",
    "AlertInboxAgent": "Receives alerts in the run-local simulated inbox and acknowledges receipt.",
    "RiskRecordCollector": "Collects typed risk_record messages for the run's audit artifacts.",
    "RiskRecordCollectorAgent": "Collects typed risk_record messages for the run's audit artifacts.",
}


@dataclass(frozen=True)
class ArtifactRows:
    rows: tuple[dict[str, Any], ...]
    status: str
    message: str


@dataclass(frozen=True)
class SimulationRunDetails:
    run_dir: Path
    manifest: Mapping[str, Any] | None
    manifest_status: str
    control: Mapping[str, Any] | None
    monitor: AgentMonitorSnapshot
    events: tuple[dict[str, Any], ...]
    event_status: str
    messages: tuple[dict[str, Any], ...]
    message_status: str
    risk_records: ArtifactRows
    risk_scores: ArtifactRows
    well_scores: ArtifactRows
    rounds: ArtifactRows
    hotspots: ArtifactRows
    alerts: ArtifactRows
    evaluation: ArtifactRows
    errors: tuple[str, ...]


def _read_json_object(path: Path) -> tuple[Mapping[str, Any] | None, str, str]:
    if not path.is_file():
        return None, "unavailable", f"{path.name} is not present yet."
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, "incomplete", f"{path.name} is unreadable or partially written: {exc}"
    if not isinstance(parsed, dict):
        return None, "incomplete", f"{path.name} must contain a JSON object."
    return parsed, "available", "Manifest loaded."


def _read_json_rows(path: Path) -> ArtifactRows:
    if not path.is_file():
        return ArtifactRows((), "unavailable", f"{path.name} is not present yet.")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return ArtifactRows((), "incomplete", f"{path.name} is unreadable or partially written: {exc}")
    if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
        return ArtifactRows((), "incomplete", f"{path.name} must contain a list of JSON objects.")
    return ArtifactRows(tuple(parsed), "available" if parsed else "empty",
                        f"{len(parsed)} records" if parsed else "No records are available.")


def _read_csv_rows(path: Path, *, expected_columns: Sequence[str] | None = None,
                   required_columns: Sequence[str] = ()) -> ArtifactRows:
    if not path.is_file():
        return ArtifactRows((), "unavailable", f"{path.name} is not present yet.")
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames:
                return ArtifactRows((), "incomplete", f"{path.name} has no complete CSV header.")
            if expected_columns is not None and reader.fieldnames != list(expected_columns):
                return ArtifactRows((), "incomplete",
                                    f"{path.name} columns do not match the expected schema.")
            missing = set(required_columns) - set(reader.fieldnames)
            if missing:
                return ArtifactRows((), "incomplete",
                                    f"{path.name} is missing required columns: {sorted(missing)}")
            rows = []
            for number, row in enumerate(reader, start=2):
                if None in row or any(value is None for value in row.values()):
                    return ArtifactRows(tuple(rows), "incomplete",
                                        f"{path.name} has an incomplete row at line {number}.")
                rows.append(dict(row))
    except (OSError, UnicodeError, csv.Error) as exc:
        return ArtifactRows((), "incomplete", f"{path.name} is unreadable or partially written: {exc}")
    return ArtifactRows(tuple(rows), "available" if rows else "empty",
                        f"{len(rows)} records" if rows else "No records are available.")


def _read_rounds(path: Path) -> ArtifactRows:
    if not path.is_dir():
        return ArtifactRows((), "unavailable", "rounds directory is not present yet.")
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for file in sorted(path.glob("*.json")):
        row, state, message = _read_json_object(file)
        if state != "available" or row is None:
            errors.append(f"{file.name}: {message}")
            continue
        if "month_index" not in row or "round" not in row:
            errors.append(f"{file.name}: required month_index/round fields are missing")
            continue
        row = dict(row)
        row["_artifact"] = file.name
        rows.append(row)
    if errors:
        return ArtifactRows(tuple(rows), "incomplete", "; ".join(errors))
    return ArtifactRows(tuple(rows), "available" if rows else "empty",
                        f"{len(rows)} round records" if rows else "No round results are available.")


def _decode_messages(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    decoded: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        try:
            body = ontology.decode(str(row.get("performative", "")), str(row.get("payload", "")))
            row["message_type"] = body.get("type", "unknown")
            row["month_index"] = body.get("month_index")
            row["round"] = body.get("round")
            row["decoded_payload"] = body
            row["payload_status"] = "valid"
        except (ontology.SchemaError, TypeError, ValueError) as exc:
            row["message_type"] = "unknown"
            row["decoded_payload"] = None
            row["payload_status"] = f"malformed: {exc}"
        row["evidence_source"] = "Seven-column SPADE message log"
        decoded.append(row)
    return tuple(decoded)


def _message_artifact_rows(messages: Sequence[Mapping[str, Any]], message_type: str) -> tuple[dict[str, Any], ...]:
    """Expose live typed message evidence without deduplicating logged send/receive rows."""
    return tuple({**dict(row.get("decoded_payload", {})),
                  "evidence_source": "typed message-log row; not deduplicated"}
                 for row in messages
                 if row.get("message_type") == message_type and isinstance(row.get("decoded_payload"), Mapping))


def _safe_control(path: str | Path | None, run_dir: Path) -> Mapping[str, Any] | None:
    if path is None:
        return None
    control_path = Path(path).expanduser().resolve()
    expected_root = run_dir.parent.parent.resolve()
    try:
        control_path.relative_to((expected_root / "control").resolve())
    except ValueError:
        return None
    try:
        value = json.loads(control_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or Path(str(value.get("run_dir", ""))).resolve() != run_dir.resolve():
        return None
    return value


def load_simulation_run_details(run_dir: str | Path, *, lab_root: str | Path | None = None,
                                control_path: str | Path | None = None) -> SimulationRunDetails:
    """Load only known artifacts under one selected run directory; partial writes stay explicit."""
    root = Path(run_dir).expanduser().resolve()
    errors: list[str] = []
    if lab_root is not None:
        runs_root = (Path(lab_root).expanduser().resolve() / "runs").resolve()
        try:
            root.relative_to(runs_root)
        except ValueError:
            raise ValueError("selected Simulation Lab run is outside the configured runs directory")
    manifest, manifest_status, manifest_message = _read_json_object(root / "run_manifest.json")
    if manifest_status != "available":
        errors.append(manifest_message)
    events, event_status, event_errors = read_event_journal(root / "agent_events.jsonl")
    messages, message_status, message_errors = read_message_events(root / "logs/messages.csv")
    errors.extend(event_errors)
    errors.extend(message_errors)
    message_rows = _decode_messages(messages)
    monitor = load_agent_monitor(root)
    risk_records = _read_json_rows(root / "risk_records.json")
    risk_scores = _read_csv_rows(root / "logs/risk_scores.csv", required_columns=(
        "month_index", "ds_division", "source", "wqi", "tier", "drivers"))
    well_scores = _read_csv_rows(root / "logs/well_scores.csv", required_columns=(
        "month_index", "month", "ds_division", "source", "well_id", "wqi", "tier"))
    rounds = _read_rounds(root / "rounds")
    hotspots = _read_json_rows(root / "hotspot_screening.json")
    alerts = _read_json_rows(root / "alert_decisions.json")
    evaluation = _read_csv_rows(root / "logs/evaluation_summary.csv", expected_columns=EVALUATION_COLUMNS)
    if risk_records.status in {"unavailable", "empty"}:
        logged_risks = _message_artifact_rows(message_rows, "risk_record")
        if logged_risks:
            risk_records = ArtifactRows(logged_risks, "available from message log",
                                       f"{len(logged_risks)} typed risk_record log rows; sent/received copies retained")
    if hotspots.status in {"unavailable", "empty"}:
        journal_hotspots = tuple({
            "month_index": event.get("month_index"), "ds_division": event.get("ds_division"),
            "status": event.get("screening_status", "unavailable"), "reason": event.get("reason"),
            "evidence_source": "runner-observed hotspot_result journal event",
        } for event in events if event.get("event_type") == "hotspot_result")
        message_hotspots = _message_artifact_rows(message_rows, "hotspot_result")
        fallback_hotspots = journal_hotspots or message_hotspots
        if fallback_hotspots:
            hotspots = ArtifactRows(fallback_hotspots, "available from run events/messages",
                                    f"{len(fallback_hotspots)} observed hotspot evidence rows")
    if alerts.status in {"unavailable", "empty"}:
        journal_alerts = tuple({
            "month_index": event.get("month_index"), "ds_division": event.get("ds_division"),
            "status": event.get("decision_status", "unavailable"),
            "delivery_status": event.get("delivery_status", "unavailable"),
            "reason": event.get("reason"),
            "evidence_source": "runner-observed alert_outcome journal event",
        } for event in events if event.get("event_type") == "alert_outcome")
        if journal_alerts:
            alerts = ArtifactRows(journal_alerts, "available from event journal",
                                  f"{len(journal_alerts)} runner-observed alert outcome rows")
    for name, artifact in (("risk records", risk_records), ("risk scores", risk_scores),
                           ("well scores", well_scores), ("rounds", rounds),
                           ("hotspots", hotspots), ("alerts", alerts),
                           ("evaluation", evaluation)):
        if artifact.status == "incomplete":
            errors.append(f"{name}: {artifact.message}")
    return SimulationRunDetails(
        root, manifest, manifest_status, _safe_control(control_path, root), monitor,
        tuple(dict(event, evidence_source="Agent event journal") for event in events), event_status,
        message_rows, message_status, risk_records, risk_scores, well_scores, rounds,
        hotspots, alerts, evaluation, tuple(errors),
    )


def severity_for_evidence(row: Mapping[str, Any]) -> str:
    """Presentation-only severity derived from explicit error/refusal/timeout event labels."""
    event = str(row.get("event_type", "")).casefold()
    performative = str(row.get("performative", "")).casefold()
    state = " ".join(str(row.get(key, "")) for key in
                      ("status", "outcome", "decision_status", "delivery_status", "reason")).casefold()
    if (any(token in event for token in ("failed", "failure", "error"))
            or performative == "failure" or any(token in state for token in ("failed", "failure", "error"))):
        return "error"
    if (any(token in event for token in ("timeout", "unavailable", "refus")) or performative == "refuse"
            or any(token in state for token in ("timeout", "unavailable", "refus", "incomplete"))):
        return "warning"
    return "info"


def combined_timeline(details: SimulationRunDetails) -> tuple[dict[str, Any], ...]:
    """Combine evidence for viewing without deduplicating journal and message records."""
    rows = [dict(event, event_type=event.get("event_type", "unknown"),
                 evidence_source="Agent event journal") for event in details.events]
    for message in details.messages:
        rows.append({
            "timestamp": message.get("timestamp"), "event_type": f"message:{message.get('performative', 'unknown')}",
            "component": "SPADE message log", "identity": message.get("sender"),
            "month_index": message.get("month_index"), "round": message.get("round"),
            "ds_division": message.get("ds_division"),
            "thread_id": message.get("thread_id"), "sender": message.get("sender"),
            "receiver": message.get("receiver"), "performative": message.get("performative"),
            "direction": message.get("direction"), "message_type": message.get("message_type"),
            "payload": message.get("payload"), "evidence_source": "Seven-column SPADE message log",
        })
    for row in rows:
        row["severity"] = severity_for_evidence(row)
        if row.get("evidence_source") == "Seven-column SPADE message log":
            row["explanation"] = (f"Logged {row.get('direction', 'unknown direction')} "
                                   f"{row.get('performative', 'unknown performative')} message "
                                   f"({row.get('message_type', 'unknown type')}) from "
                                   f"{row.get('sender', 'unknown sender')} to {row.get('receiver', 'unknown recipient')}.")
        else:
            row["explanation"] = (row.get("reason") or row.get("outcome") or row.get("phase")
                                   or row.get("status") or row.get("event_type", "Recorded run event"))
        row["_sort_time"] = _parse_time(row.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda row: (row["_sort_time"], row.get("event_seq", 0)))
    return tuple(rows)


def filter_evidence(rows: Sequence[Mapping[str, Any]], *, month: str = "All", agent: str = "All",
                    event_type: str = "All", severity: str = "All", query: str = "") -> tuple[dict[str, Any], ...]:
    """Filter event or message evidence without altering source rows."""
    needle = query.casefold().strip()
    output = []
    for row in rows:
        if month != "All" and str(row.get("month_index", "Unavailable")) != month:
            continue
        if agent != "All" and agent not in {str(row.get("identity", "")), str(row.get("component", "")),
                                           str(row.get("sender", "")), str(row.get("receiver", ""))}:
            continue
        if event_type != "All" and str(row.get("event_type", "")) != event_type:
            continue
        if severity != "All" and str(row.get("severity", severity_for_evidence(row))) != severity:
            continue
        if needle and needle not in json.dumps(dict(row), sort_keys=True, default=str).casefold():
            continue
        output.append(dict(row))
    return tuple(output)


def filter_messages(rows: Sequence[Mapping[str, Any]], *, query: str = "", month: str = "All",
                    sender: str = "All", receiver: str = "All", performative: str = "All",
                    direction: str = "All") -> tuple[dict[str, Any], ...]:
    needle = query.casefold().strip()
    output = []
    for row in rows:
        if month != "All" and str(row.get("month_index", "Unavailable")) != month:
            continue
        if sender != "All" and row.get("sender") != sender:
            continue
        if receiver != "All" and row.get("receiver") != receiver:
            continue
        if performative != "All" and row.get("performative") != performative:
            continue
        if direction != "All" and row.get("direction") != direction:
            continue
        if needle and needle not in json.dumps(dict(row), sort_keys=True, default=str).casefold():
            continue
        output.append(dict(row))
    return tuple(output)


def agent_execution_details(details: SimulationRunDetails) -> tuple[dict[str, Any], ...]:
    """Group observed journal events and message counts by identity, preserving source evidence."""
    status_by_identity = {str(row.get("Identity")): row for row in details.monitor.agents}
    identities = set(status_by_identity)
    identities.update(str(event["identity"]) for event in details.events
                      if isinstance(event.get("identity"), str) and event.get("identity"))
    rows = []
    for identity in sorted(identities):
        agent_status = status_by_identity.get(identity, {})
        events = [dict(event) for event in details.events if event.get("identity") == identity]
        messages = [dict(message) for message in details.messages
                    if identity in {message.get("sender"), message.get("receiver")}]
        timestamps = [parsed for parsed in (_parse_time(event.get("timestamp")) for event in events) if parsed]
        timestamps.extend(parsed for parsed in (_parse_time(message.get("timestamp")) for message in messages) if parsed)
        components = [event.get("component") for event in events if event.get("component")]
        component = str(components[0] if components else agent_status.get("Component", "Unknown component"))
        phases = [event for event in events if event.get("event_type") in {"agent_phase", "agent_waiting"}]
        rows.append({
            "component": component, "identity": identity,
            "responsibility": AGENT_RESPONSIBILITIES.get(component.split(":", 1)[0],
                "Responsibility is not described by the current event contract."),
            "status": agent_status.get("Status", "unknown"),
            "status_evidence": agent_status.get("Status evidence", "No per-agent lifecycle evidence."),
            "first_observed": min(timestamps).isoformat() if timestamps else None,
            "last_observed": max(timestamps).isoformat() if timestamps else None,
            "latest_heartbeat": agent_status.get("Latest heartbeat", "Unavailable"),
            "heartbeat_age_s": agent_status.get("Heartbeat age (s)"),
            "latest_phase": agent_status.get("Latest phase", "Unavailable"),
            "phase_history": tuple(phases),
            "sent_rows": sum(message.get("direction") == "sent" and message.get("sender") == identity
                             for message in messages),
            "received_rows": sum(message.get("direction") == "received" and message.get("receiver") == identity
                                 for message in messages),
            "events": tuple(events), "messages": tuple(messages),
            "work_events": tuple(event for event in events if event.get("event_type") in {
                "agent_work_started", "agent_work_completed", "agent_work_failed", "agent_behavior_failed",
            }),
            "failures": tuple(event for event in events if severity_for_evidence(event) == "error"),
        })
    return tuple(rows)


def _latest_evidence(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(enumerate(rows), key=lambda pair: (
        _parse_time(pair[1].get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
        pair[1].get("event_seq", 0) if isinstance(pair[1].get("event_seq"), int) else pair[0],
        pair[0],
    ))[1]


def _message_brief(row: Mapping[str, Any]) -> str:
    body = row.get("decoded_payload")
    if not isinstance(body, Mapping):
        return f"{row.get('performative', 'message')} · payload unavailable"
    fields = ("type", "month_index", "round", "ds_division", "tier", "wqi", "bid",
              "status", "screening_status", "delivery_status", "resource")
    summary = [f"{key}={body[key]}" for key in fields if body.get(key) is not None]
    return ", ".join(summary) or str(body.get("type", "typed message"))


def _event_brief(row: Mapping[str, Any]) -> str:
    keys = ("month_index", "screening_status", "decision_status", "delivery_status", "award_status",
            "tier", "wqi", "bid", "ds_division", "winner", "outcome", "status", "reason")
    details = [f"{key}={row[key]}" for key in keys if row.get(key) is not None]
    return f"{row.get('event_type', 'event')}" + (": " + ", ".join(details) if details else "")


def _artifact_result(component: str, details: SimulationRunDetails) -> tuple[str | None, str | None]:
    sources = {
        "RiskScoringAgent": (details.risk_scores, "logs/risk_scores.csv"),
        "RiskRecordCollector": (details.risk_records, "risk_records.json"),
        "RiskRecordCollectorAgent": (details.risk_records, "risk_records.json"),
        "ResourceCoordinatorAgent": (details.rounds, "rounds/*.json"),
        "HotspotAgent": (details.hotspots, "hotspot_screening.json"),
        "AlertProcessingAgent": (details.alerts, "alert_decisions.json"),
    }
    entry = sources.get(component)
    if entry is None or not entry[0].rows:
        return None, None
    artifact, path = entry
    row = _latest_evidence(artifact.rows)
    if row is None:
        return None, None
    result = _event_brief({"event_type": f"recorded {component} output", **dict(row)})
    return result, f"Run artifact: {path}"


def _component_label(value: Any) -> str:
    text = str(value or "Unknown component")
    return text.split(":", 1)[0]


def _agent_cards(details: SimulationRunDetails) -> tuple[dict[str, Any], ...]:
    cards = []
    for agent in agent_execution_details(details):
        identity = agent["identity"]
        related = [row for row in details.messages if identity in {row.get("sender"), row.get("receiver")}]
        incoming = _latest_evidence([row for row in related if row.get("direction") == "received"
                                     and row.get("receiver") == identity])
        outgoing = _latest_evidence([row for row in related if row.get("direction") == "sent"
                                     and row.get("sender") == identity])
        events = list(agent["events"])
        relevant_events = [row for row in events if row.get("event_type") not in {
            "agent_heartbeat", "agent_started", "agent_starting", "agent_stopped",
        }]
        latest_event = _latest_evidence(relevant_events)
        activity = agent.get("latest_phase")
        if activity in (None, "Unavailable", ""):
            activity = (_event_brief(latest_event) if latest_event else
                        (_message_brief(incoming) if incoming else "Waiting for input."))
        component_name = _component_label(agent["component"])
        artifact_result, artifact_destination = _artifact_result(component_name, details)
        result = _message_brief(outgoing) if outgoing else (
            artifact_result or (_event_brief(latest_event) if latest_event else "No result recorded yet."))
        output_to = str(outgoing.get("receiver")) if outgoing else (
            artifact_destination or "No output recipient/destination recorded.")
        cards.append({
            "name": component_name, "identity": identity,
            "status": agent["status"], "status_evidence": agent["status_evidence"],
            "activity": str(activity),
            "latest_input": _message_brief(incoming) if incoming else "Waiting for input.",
            "latest_result": result, "output_to": output_to,
            "sent_rows": agent["sent_rows"], "received_rows": agent["received_rows"],
            "component": agent["component"],
        })
    order = {name: index for index, name in enumerate((
        "ClockAgent", "WellMonitorAgent", "RiskScoringAgent", "VillageAgent",
        "RiskRecordCollector", "ResourceCoordinatorAgent", "HotspotAgent",
        "AlertProcessingAgent", "AlertInboxAgent",
    ))}
    return tuple(sorted(cards, key=lambda row: (order.get(row["name"], 99), row["identity"])))


def _collaboration_flow(details: SimulationRunDetails,
                        cards: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build observed SPADE edges from sent rows; keep runner sequencing explicitly separate."""
    identity_to_component = {str(card["identity"]): str(card["name"]) for card in cards}
    nodes = {str(card["name"]) for card in cards}
    runner_observed = bool(details.manifest) or any(
        event.get("component") == "SimulationRunner" for event in details.events)
    if runner_observed:
        nodes.add("SimulationRunner")
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for row in details.messages:
        if row.get("direction") != "sent":
            continue
        sender = str(row.get("sender", "Unknown sender"))
        receiver = str(row.get("receiver", "Unknown recipient"))
        source = identity_to_component.get(sender, sender)
        target = identity_to_component.get(receiver, receiver)
        nodes.update((source, target))
        edge = edges.setdefault((source, target), {"source": source, "target": target,
                                                    "sent_log_rows": 0, "message_types": set()})
        edge["sent_log_rows"] += 1
        edge["message_types"].add(str(row.get("message_type", "unknown")))
    actual = tuple({**edge, "message_types": tuple(sorted(edge["message_types"])),
                    "evidence": "sent row in seven-column SPADE message log"}
                   for _key, edge in sorted(edges.items()))

    # These dashed relationships describe runner-controlled sequencing only; they are not agent messages.
    has_month_evidence = bool(details.manifest and details.manifest.get("month_status")) or any(
        row.get("event_type") == "month_started" for row in details.events)
    scheduled = []
    if has_month_evidence and runner_observed:
        for component in sorted({str(card["name"]) for card in cards}):
            scheduled.append({"source": "SimulationRunner", "target": component,
                              "evidence": "agent startup/month scheduling is runner controlled; not a SPADE message"})
    control = details.control or {}
    input_label = control.get("input_source") or (
        details.manifest.get("provenance", {}).get("input_path")
        if details.manifest and isinstance(details.manifest.get("provenance"), Mapping) else None
    )
    if input_label and "WellMonitorAgent" in nodes:
        nodes.add("Input dataset")
        scheduled.append({"source": "Input dataset", "target": "WellMonitorAgent",
                          "evidence": "configured synthetic input; not an agent message"})
    return {"nodes": tuple(sorted(nodes)), "message_edges": actual,
            "runner_orchestration": tuple(scheduled),
            "input_label": str(input_label or "Input source unavailable"),
            "message_status": details.message_status}


def _final_decisions(details: SimulationRunDetails) -> dict[str, Any]:
    risks = details.risk_scores.rows or details.risk_records.rows
    risk_status = details.risk_scores.status if details.risk_scores.rows else details.risk_records.status
    risk_rows = tuple({
        "month_index": row.get("month_index", "Unavailable"),
        "division": row.get("ds_division", "Unavailable"),
        "tier": row.get("tier", "Unavailable"), "wqi": row.get("wqi", "Unavailable"),
        "drivers": row.get("drivers", "Unavailable"),
        "evidence": row.get("evidence_source", "Risk-score artifact"),
    } for row in risks)
    allocations = []
    for round_row in details.rounds.rows:
        selected = round_row.get("selected_awardees", [])
        if isinstance(selected, list) and selected:
            for award in selected:
                if not isinstance(award, Mapping):
                    continue
                allocations.append({
                    "month_index": round_row.get("month_index"), "round": round_row.get("round"),
                    "division": award.get("ds_division", "Unavailable"),
                    "status": award.get("status", round_row.get("award_status", "Unavailable")),
                    "confirmed_winner": round_row.get("winner") == award.get("ds_division")
                                         and round_row.get("award_status") == "confirmed",
                    "evidence": round_row.get("_artifact", "round result artifact"),
                })
        elif round_row.get("winner"):
            allocations.append({
                "month_index": round_row.get("month_index"), "round": round_row.get("round"),
                "division": round_row.get("winner"),
                "status": round_row.get("award_status", "Unavailable"),
                "confirmed_winner": round_row.get("award_status") == "confirmed",
                "evidence": round_row.get("_artifact", "round result artifact"),
            })
    hotspot_rows = tuple({
        "month_index": row.get("month_index", "Unavailable"),
        "division": row.get("ds_division", "Unavailable"),
        "status": row.get("status", row.get("screening_status", "Unavailable")),
        "history": row.get("evidence_month_indexes", "Unavailable"),
        "reason": row.get("reason", "Unavailable"),
    } for row in details.hotspots.rows)
    alert_rows = tuple({
        "month_index": row.get("month_index", "Unavailable"),
        "division": row.get("ds_division", "Unavailable"),
        "decision": row.get("status", row.get("decision_status", "Unavailable")),
        "delivery": row.get("delivery_status", "Unavailable"),
        "reason": row.get("reason", "Unavailable"),
    } for row in details.alerts.rows)
    stage_status = details.manifest.get("stage_status", {}) if details.manifest else {}
    unavailable_stages = tuple((key, value) for key, value in stage_status.items()
                               if isinstance(value, str) and any(word in value.casefold()
                                  for word in ("unavailable", "partial", "failed", "incomplete", "skipped"))) \
        if isinstance(stage_status, Mapping) else ()
    failures = details.manifest.get("failures", []) if details.manifest else []
    unavailable_metrics = tuple({
        "metric": row.get("metric", "Unknown metric"),
        "status": row.get("status", "unavailable"),
        "details": row.get("details", "Required evidence unavailable."),
    } for row in details.evaluation.rows
      if str(row.get("status", "")).casefold() in {"unavailable", "partial"})
    return {
        "risks": risk_rows, "risk_status": risk_status,
        "allocations": tuple(allocations), "allocation_status": details.rounds.status,
        "hotspots": hotspot_rows, "hotspot_status": details.hotspots.status,
        "alerts": alert_rows, "alert_status": details.alerts.status,
        "unavailable_stages": unavailable_stages,
        "failures": tuple(failures) if isinstance(failures, list) else (),
        "evaluation_status": details.evaluation.status,
        "unavailable_metrics": unavailable_metrics,
    }


def build_execution_view(details: SimulationRunDetails, *, run_status: str | None = None) -> dict[str, Any]:
    """Prepare the compact Simulation Lab execution screen entirely from recorded evidence."""
    manifest = details.manifest or {}
    month_rows = manifest.get("month_status", [])
    month_rows = [row for row in month_rows if isinstance(row, Mapping)] if isinstance(month_rows, list) else []
    config = manifest.get("configuration", {})
    config = config if isinstance(config, Mapping) else {}
    total = None
    start, end = config.get("start_month_index"), config.get("end_month_index")
    if isinstance(start, int) and isinstance(end, int) and end >= start:
        total = end - start + 1
    elif month_rows:
        total = len(month_rows)
    finished = sum(row.get("status") in {"complete", "completed", "incomplete", "failed"}
                   for row in month_rows)
    progress = finished / total if total else None
    cards = _agent_cards(details)
    resolved_status = run_status or str(manifest.get("status", "unknown"))
    return {
        "run_id": manifest.get("run_id", (details.control or {}).get("run_id", "Unknown")),
        "run_status": resolved_status, "completed_months": finished,
        "total_months": total, "latest_month_index": max((row.get("month_index") for row in month_rows
            if isinstance(row.get("month_index"), int)), default=None),
        "progress": min(1.0, max(0.0, progress)) if progress is not None else None,
        "agents": cards, "flow": _collaboration_flow(details, cards),
        "decisions": _final_decisions(details),
    }
