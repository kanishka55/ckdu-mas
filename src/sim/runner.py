"""Lifecycle-safe, month-stepped SPADE simulation runner.

This runs the existing WellMonitorAgent, RiskScoringAgent, VillageAgents,
HotspotAgent, alert processor/local inbox, and single-round ResourceCoordinatorAgent.
All outputs and message logs are isolated under a fresh output root.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import spade
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour

from src.agents import common
from src.agents.alert_spade import AlertInboxAgent, AlertProcessingAgent
from src.agents.clock import ClockAgent
from src.agents.hotspot import HotspotAgent
from src.agents.resource_coordinator import ResourceCoordinatorAgent
from src.agents.risk_scoring import RiskScoringAgent
from src.agents.village import VillageAgent
from src.agents.well_monitor import WellMonitorAgent
from src.core import contract_net, msg_log, ontology, risk_tiers, wells, wqi
from src.core.hotspot_screening import ScreeningRule
from src.sim import load_data, risk_outputs
from src.sim.event_journal import append_event, register_run
from src.sim.evaluate import ALLOCATION_COLUMNS, evaluate

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "data/processed/synthetic_wells.csv"
DEFAULT_METHOD = ROOT / "data/raw/ckdu_02_wqi_method_and_thresholds.csv"
DEFAULT_DIVISIONS = ROOT / "data/raw/ckdu_03_sampling_design_ds_divisions.csv"
MANIFEST_NAME = "run_manifest.json"


@dataclass(frozen=True)
class AgentIdentityConfig:
    """Local parts used to derive per-run SPADE JIDs under the configured domain."""

    clock: str = "clock"
    well_monitor: str = "well_monitor"
    risk_scoring: str = "risk_scoring"
    hotspot: str = "hotspot"
    alert_processing: str = "alert_processing"
    alert_inbox: str = "alert_inbox"
    risk_observer_prefix: str = "risk_observer"
    coordinator_prefix: str = "coordinator_m"
    village_prefix: str = "village_"

    def __post_init__(self) -> None:
        values = (self.clock, self.well_monitor, self.risk_scoring, self.hotspot,
                  self.alert_processing, self.alert_inbox, self.risk_observer_prefix,
                  self.coordinator_prefix, self.village_prefix)
        if any(not isinstance(value, str) or not value.strip() or "@" in value for value in values):
            raise ValueError("agent identity local parts/prefixes must be non-empty and omit @domain")


@dataclass(frozen=True)
class SimulationConfig:
    """Configuration for an isolated run. Calendar labels are never inferred."""

    start_month_index: int
    end_month_index: int
    calendar_month_by_index: Mapping[int, int]
    output_dir: Path
    resource: str = "RO_unit"
    resource_pool: int = 1
    round_number: int = 1
    proposal_timeout_s: float = 5.0
    confirmation_timeout_s: float = 5.0
    stage_timeout_s: float = 45.0
    hotspot_stage_timeout_s: float = 5.0
    alert_receive_timeout_s: float = 0.2
    alert_stage_timeout_s: float = 5.0
    alert_acknowledgement_timeout_s: float = 2.0
    clock_send_timeout_s: float = 10.0
    domain: str = "localhost"
    embedded_server: bool = False
    password: str = "demo"
    run_id: str = "local"
    agent_identities: AgentIdentityConfig = AgentIdentityConfig()
    input_path: Path = DEFAULT_INPUT
    method_path: Path = DEFAULT_METHOD
    divisions_path: Path = DEFAULT_DIVISIONS
    hotspot_rule: ScreeningRule | None = None

    def __post_init__(self) -> None:
        for field_name, value in (("start_month_index", self.start_month_index),
                                  ("end_month_index", self.end_month_index),
                                  ("resource_pool", self.resource_pool),
                                  ("round_number", self.round_number)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.end_month_index < self.start_month_index:
            raise ValueError("end_month_index must be >= start_month_index")
        if self.resource not in ontology.RESOURCES:
            raise ValueError(f"resource must be one of {sorted(ontology.RESOURCES)}")
        for name in ("proposal_timeout_s", "confirmation_timeout_s", "stage_timeout_s",
                     "hotspot_stage_timeout_s",
                     "alert_receive_timeout_s", "alert_stage_timeout_s",
                     "alert_acknowledgement_timeout_s",
                     "clock_send_timeout_s"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be positive")
        expected = set(range(self.start_month_index, self.end_month_index + 1))
        if set(self.calendar_month_by_index) != expected:
            raise ValueError("calendar_month_by_index must explicitly map every configured month_index")
        for index, month in self.calendar_month_by_index.items():
            if isinstance(index, bool) or not isinstance(index, int):
                raise ValueError("calendar mapping keys must be integer month_index values")
            if isinstance(month, bool) or not isinstance(month, int) or month not in range(1, 13):
                raise ValueError(f"calendar month for month_index {index} must be 1..12")
        if not self.domain.strip() or not self.run_id.strip():
            raise ValueError("domain and run_id must be non-empty")
        if not isinstance(self.embedded_server, bool):
            raise ValueError("embedded_server must be bool")
        if not isinstance(self.output_dir, Path):
            object.__setattr__(self, "output_dir", Path(self.output_dir))


class RiskRecordCollectorAgent(Agent):
    """Run-local sink retaining the risk records sent by the existing scorer."""

    def __init__(self, *args, receive_timeout_s: float, **kwargs):
        super().__init__(*args, **kwargs)
        if receive_timeout_s <= 0:
            raise ValueError("receive_timeout_s must be positive")
        self.receive_timeout_s = receive_timeout_s
        self.records: list[dict[str, Any]] = []
        self.duplicate_keys: list[tuple[int, str]] = []

    class Collect(CyclicBehaviour):
        async def run(self):
            agent: RiskRecordCollectorAgent = self.agent
            got = await common.receive(self, timeout=agent.receive_timeout_s)
            if got is None:
                return
            _, performative, body = got
            if performative == ontology.INFORM and body["type"] == "risk_record":
                key = (body["month_index"], body["ds_division"])
                if any((row["month_index"], row["ds_division"]) == key for row in agent.records):
                    agent.duplicate_keys.append(key)
                agent.records.append(dict(body))

    async def setup(self):
        common.add_observed_behaviour(self, self.Collect())


def validate_output_dir(output_dir: str | Path) -> Path:
    """Reject production, source, fixture, and existing-result paths before any run writes."""
    root = Path(output_dir).expanduser().resolve()
    if root in {Path("/"), ROOT}:
        raise ValueError(f"refusing unsafe output directory: {root}")
    protected = (ROOT / "logs", ROOT / "data/raw", ROOT / "data/processed", ROOT / "tests",
                 ROOT / "src", ROOT / "dashboard", ROOT / "demo_output")
    for target in protected:
        try:
            root.relative_to(target.resolve())
        except ValueError:
            continue
        raise ValueError(f"refusing to write simulation output under protected path: {target}")
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"output directory is not empty; choose a fresh isolated path: {root}")
    return root


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not slug:
        raise ValueError(f"cannot form an agent identity from division {value!r}")
    return slug


def _jid(localpart: str, domain: str) -> str:
    return f"{localpart}@{domain}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json_exclusive(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def _write_confirmed_allocation_rows(
    output_root: Path,
    round_result_paths: Sequence[str],
    calendar_month_by_index: Mapping[int, int],
) -> Path:
    """Export confirmed Contract Net awards in the evaluator's existing row schema.

    This records outcomes from this synthetic run only. It does not create
    equal-split/first-come baselines or attest scientific/team provenance.
    """
    path = output_root / "logs" / "allocations.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for relative in round_result_paths:
        artifact = Path(relative)
        if artifact.is_absolute() or ".." in artifact.parts:
            raise ValueError("round result paths must remain inside the isolated run directory")
        try:
            result = json.loads((output_root / artifact).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot export allocation evidence from {relative}: {exc}") from exc
        if not isinstance(result, Mapping):
            raise ValueError(f"round result {relative} must contain a JSON object")
        month_index, resource, round_number = result.get("month_index"), result.get("resource"), result.get("round")
        if (isinstance(month_index, bool) or not isinstance(month_index, int)
                or month_index not in calendar_month_by_index
                or not isinstance(resource, str) or not resource
                or isinstance(round_number, bool) or not isinstance(round_number, int)):
            raise ValueError(f"round result {relative} has incomplete allocation identifiers")
        awardees = result.get("selected_awardees")
        if not isinstance(awardees, list):
            raise ValueError(f"round result {relative} has no selected_awardees list")
        for award in awardees:
            if not isinstance(award, Mapping):
                raise ValueError(f"round result {relative} contains a malformed award row")
            if award.get("status") != "confirmed":
                continue
            division, bid = award.get("ds_division"), award.get("bid")
            if not isinstance(division, str) or not division:
                raise ValueError(f"round result {relative} has a confirmed award without ds_division")
            if isinstance(bid, bool) or not isinstance(bid, (int, float)) or not math.isfinite(bid):
                raise ValueError(f"round result {relative} has a confirmed award with invalid bid")
            rows.append({"month": calendar_month_by_index[month_index], "resource": resource,
                         "ds_division": division, "round": round_number,
                         "winning_bid": bid, "method": "contract_net"})
    with path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=ALLOCATION_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _rewrite_owned_json(path: Path, value: Any) -> None:
    """Refresh the run manifest that this invocation reserved with exclusive creation."""
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def _append_monitor_event(path: Path, event_type: str, component: str, *,
                          identity: str | None = None, **details: Any) -> None:
    """Append one observed runner/lifecycle event to the isolated run journal."""
    append_event(path, event_type, component, identity=identity, **details)


async def _monitor_heartbeat(path: Path) -> None:
    """Provide process-level freshness evidence while the runner event loop remains alive."""
    while True:
        await asyncio.sleep(2.0)
        _append_monitor_event(path, "runner_heartbeat", "SimulationRunner")


async def _wait_for_month(
    month_index: int,
    divisions: Sequence[str],
    monitor: WellMonitorAgent,
    scorer: RiskScoringAgent,
    collector: RiskRecordCollectorAgent,
    villages: Sequence[VillageAgent],
    timeout_s: float,
) -> tuple[bool, str | None]:
    """Await actual monitor/scorer/collector/village state with a finite barrier timeout."""
    deadline = time.monotonic() + timeout_s
    village_by_name = {v.ds_division: v for v in villages}
    expected = set(divisions)
    while time.monotonic() < deadline:
        scorer_rows = [row for row in scorer.history if row.get("month_index") == month_index]
        collector_rows = [row for row in collector.records if row.get("month_index") == month_index]
        collector_divisions = {row["ds_division"] for row in collector_rows}
        villages_current = all(village_by_name[name].risk_month_index == month_index for name in expected)
        if (month_index in monitor.ticks_handled and len(scorer_rows) >= len(expected)
                and expected.issubset(collector_divisions) and villages_current):
            if len(scorer_rows) != len(expected) or len(collector_rows) != len(expected):
                return False, "duplicate_or_unexpected_risk_records_for_month"
            return True, None
        await asyncio.sleep(0.02)
    return False, "timed_out_waiting_for_monthly_risk_delivery_and_village_state"


async def _wait_for_hotspot_month(
    month_index: int,
    divisions: Sequence[str],
    hotspot: HotspotAgent,
    timeout_s: float,
) -> bool:
    """Wait for the hotspot subscriber to process this month's fan-out copies."""
    deadline = time.monotonic() + timeout_s
    expected = {(division, month_index) for division in divisions}
    while time.monotonic() < deadline:
        if expected.issubset(hotspot.results_by_key):
            return True
        await asyncio.sleep(0.02)
    return expected.issubset(hotspot.results_by_key)


async def _wait_for_alert_month(month_index: int, divisions: Sequence[str],
                                alert_agent: AlertProcessingAgent, timeout_s: float) -> bool:
    expected = {(division, month_index) for division in divisions}
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if expected.issubset(alert_agent.decision_keys):
            return True
        await asyncio.sleep(0.02)
    return expected.issubset(alert_agent.decision_keys)


def _read_message_counts(path: Path) -> dict[str, Any]:
    by_direction: Counter[str] = Counter()
    by_performative: Counter[str] = Counter()
    rows = 0
    if path.is_file():
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != msg_log.COLUMNS:
                raise ValueError(f"message log schema mismatch: {reader.fieldnames!r}")
            for row in reader:
                rows += 1
                by_direction[row["direction"]] += 1
                by_performative[row["performative"]] += 1
    return {"logged_rows_including_sent_received_copies": rows,
            "by_direction": dict(sorted(by_direction.items())),
            "by_performative": dict(sorted(by_performative.items()))}


def _has_unavailable_stages(*evidence_sections: Any) -> bool:
    """Detect recorded partial, unavailable, or failed work without inspecting provenance text."""
    status_tokens = ("unavailable", "partial", "incomplete", "failed", "skipped", "not_configured")

    def contains(value: Any, key: Any = None) -> bool:
        if key == "failures" and isinstance(value, (list, tuple)):
            return bool(value)
        if key == "unavailable" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return value > 0
        if isinstance(value, str):
            return any(token in value.casefold() for token in status_tokens)
        if isinstance(value, Mapping):
            return any(contains(item, item_key) for item_key, item in value.items())
        if isinstance(value, (list, tuple)):
            return any(contains(item) for item in value)
        return False

    return any(contains(section) for section in evidence_sections)


def _has_complete_negotiation_history(
    output_root: Path,
    round_result_paths: Sequence[str],
    month_status: Sequence[Mapping[str, Any]],
    *,
    recipient_count: int,
    round_number: int,
) -> bool:
    """Attest complete round evidence before enabling evaluator negotiation metrics.

    The evaluator still validates message threads and sent/received event identities. This
    additional gate only confirms that every month with completed risk delivery has a persisted
    round result with one response per configured recipient and no recorded missing, malformed,
    unmatched, timed-out, or protocol-error evidence.
    """
    expected_months = {
        item.get("month_index") for item in month_status
        if item.get("risk_delivery") == "complete"
    }
    if not expected_months or not round_result_paths or len(round_result_paths) != len(expected_months):
        return False
    if (isinstance(recipient_count, bool) or not isinstance(recipient_count, int)
            or recipient_count < 1):
        return False

    seen_months: set[int] = set()
    for relative_path in round_result_paths:
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            return False
        try:
            result = json.loads((output_root / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(result, Mapping):
            return False
        month_index = result.get("month_index")
        if month_index not in expected_months or month_index in seen_months:
            return False
        if result.get("round") != round_number or not result.get("thread_id"):
            return False
        if result.get("state_history", [])[-1:] != ["DONE"]:
            return False
        for field in ("missing_recipients", "timed_out_recipients", "malformed_replies",
                      "unmatched_replies", "protocol_errors", "confirmation_timeouts"):
            if result.get(field) != []:
                return False

        proposals = result.get("proposals")
        refusals = result.get("refusals")
        ineligible = result.get("ineligible_proposals")
        if not all(isinstance(rows, list) for rows in (proposals, refusals, ineligible)):
            return False
        responders = [row.get("sender") for row in proposals + refusals + ineligible
                      if isinstance(row, Mapping)]
        if (len(responders) != len(proposals) + len(refusals) + len(ineligible)
                or any(not isinstance(sender, str) or not sender for sender in responders)
                or len(set(responders)) != recipient_count):
            return False

        awardees = result.get("selected_awardees")
        if not isinstance(awardees, list) or any(
            not isinstance(awardee, Mapping)
            or awardee.get("status") not in {"confirmed", "failed"}
            for awardee in awardees
        ):
            return False
        seen_months.add(month_index)

    return seen_months == expected_months


async def run_simulation(config: SimulationConfig) -> dict[str, Any]:
    """Run configured SPADE months; write every artifact only below a fresh output root."""
    output_root = validate_output_dir(config.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    log_path = output_root / "logs/messages.csv"
    event_path = output_root / "agent_events.jsonl"
    round_dir = output_root / "rounds"
    round_dir.mkdir(parents=True, exist_ok=True)
    with event_path.open("x", encoding="utf-8", newline="\n"):
        pass
    run_id = config.run_id
    register_run(event_path, run_id)
    message_log_path = log_path
    started_agents: list[tuple[str, Agent]] = []
    lifecycle: list[dict[str, Any]] = []
    months: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    risk_records: list[dict[str, Any]] = []
    hotspot_agent: HotspotAgent | None = None
    alert_agent: AlertProcessingAgent | None = None
    heartbeat_task: asyncio.Task | None = None
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "started_at": _utc_now(),
        "provenance": {
            "classification": "synthetic_simulation",
            "input_path": str(config.input_path),
            "source_required": "synthetic",
            "seasonal_readings_repeated_by_existing_well_expansion": True,
            "not_new_monthly_measurements": True,
            "reduced_parameter_wqi": True,
            "ckdu_diagnostic_or_causal_claim": False,
            "contract_net": "existing single-round SPADE coordinator; not a multi-round convergence implementation",
            "hotspot": "project-defined screening only; not a validated medical or epidemiological model",
            "alerts": "typed SPADE flow to run-local simulated inbox; not a real health-officer endpoint",
        },
        "configuration": {
            "start_month_index": config.start_month_index,
            "end_month_index": config.end_month_index,
            "calendar_month_by_index": dict(config.calendar_month_by_index),
            "resource": config.resource,
            "resource_pool": config.resource_pool,
            "round_number": config.round_number,
            "proposal_timeout_s": config.proposal_timeout_s,
            "confirmation_timeout_s": config.confirmation_timeout_s,
            "stage_timeout_s": config.stage_timeout_s,
            "hotspot_stage_timeout_s": config.hotspot_stage_timeout_s,
            "alert_receive_timeout_s": config.alert_receive_timeout_s,
            "alert_stage_timeout_s": config.alert_stage_timeout_s,
            "alert_acknowledgement_timeout_s": config.alert_acknowledgement_timeout_s,
            "domain": config.domain,
            "embedded_server": config.embedded_server,
            "output_dir": str(output_root),
            "agent_identities": {
                "clock": config.agent_identities.clock,
                "well_monitor": config.agent_identities.well_monitor,
                "risk_scoring": config.agent_identities.risk_scoring,
                "hotspot": config.agent_identities.hotspot,
                "alert_processing": config.agent_identities.alert_processing,
                "alert_inbox": config.agent_identities.alert_inbox,
                "risk_observer_prefix": config.agent_identities.risk_observer_prefix,
                "coordinator_prefix": config.agent_identities.coordinator_prefix,
                "village_prefix": config.agent_identities.village_prefix,
            },
            "hotspot_rule": None if config.hotspot_rule is None else {
                "rule_id": config.hotspot_rule.rule_id,
                "trigger_tiers": sorted(config.hotspot_rule.trigger_tiers),
                "consecutive_months": config.hotspot_rule.consecutive_months,
            },
        },
        "agent_lifecycle": lifecycle,
        "month_status": months,
        "round_result_paths": [],
        "stage_status": {},
        "message_counts": {},
        "failures": failures,
    }
    manifest_path = output_root / MANIFEST_NAME
    _write_json_exclusive(manifest_path, manifest)
    _append_monitor_event(event_path, "run_started", "SimulationRunner", run_id=run_id)
    heartbeat_task = asyncio.create_task(_monitor_heartbeat(event_path))

    def record_lifecycle(component: str, event: str, agent: Agent | None = None) -> None:
        at = _utc_now()
        identity = str(agent.jid) if agent is not None else None
        lifecycle.append({"component": component, "event": event, "at": at, **({"jid": identity} if identity else {})})
        event_names = {"started": "agent_started", "start_failed": "agent_start_failed",
                       "stopped": "agent_stopped", "stop_failed": "agent_stop_failed"}
        _append_monitor_event(event_path, event_names[event], component,
                              identity=identity, lifecycle_state=event)

    try:
        monthly = load_data.load_monthly_readings(config.input_path)
        if set(monthly["source"].astype(str)) != {"synthetic"}:
            raise ValueError("simulation input must contain only source='synthetic' readings")
        divisions_frame = load_data.load_ds_divisions(config.divisions_path)
        divisions = sorted(str(value) for value in divisions_frame["ds_division"].tolist())
        method_frame = load_data.load_wqi_method(config.method_path)
        method = wqi.method_from_frame(method_frame)
        classes = risk_tiers.classes_from_frame(method_frame)
        manifest["input_record_count"] = int(len(monthly))
        manifest["divisions"] = divisions

        village_jids: dict[str, str] = {}
        used_slugs: set[str] = set()
        for division in divisions:
            slug = _slug(division)
            if slug in used_slugs:
                raise ValueError(f"division names collide after JID normalization: {division!r}")
            used_slugs.add(slug)
            village_jids[division] = _jid(f"{config.agent_identities.village_prefix}{slug}", config.domain)
        clock_jid = _jid(config.agent_identities.clock, config.domain)
        monitor_jid = _jid(config.agent_identities.well_monitor, config.domain)
        scorer_jid = _jid(config.agent_identities.risk_scoring, config.domain)
        collector_jid = _jid(f"{config.agent_identities.risk_observer_prefix}_{_slug(run_id)}", config.domain)

        divisions_indexed = divisions_frame.set_index("ds_division")
        villages = [VillageAgent(
            village_jids[name], config.password, ds_division=name,
            population=int(divisions_indexed.loc[name, "population"]),
            distance_to_depot_km=float(divisions_indexed.loc[name, "distance_to_depot_km"]),
            local_wells=monthly[monthly["ds_division"] == name],
        ) for name in divisions]
        collector = RiskRecordCollectorAgent(collector_jid, config.password, receive_timeout_s=1.0)
        hotspot_jid = _jid(config.agent_identities.hotspot, config.domain)
        hotspot_agent = HotspotAgent(
            hotspot_jid, config.password, divisions=divisions, rule=config.hotspot_rule,
            output_path=output_root / "hotspot_screening.json", receive_timeout_s=0.25,
            result_recipient_jid=_jid(config.agent_identities.alert_processing, config.domain),
        )
        alert_processor_jid = _jid(config.agent_identities.alert_processing, config.domain)
        alert_inbox_jid = _jid(config.agent_identities.alert_inbox, config.domain)
        alert_agent = AlertProcessingAgent(
            alert_processor_jid, config.password, divisions=divisions,
            recipient_jid=alert_inbox_jid, risk_sender_jid=scorer_jid,
            hotspot_sender_jid=hotspot_jid,
            output_path=output_root / "alert_decisions.json",
            receive_timeout_s=config.alert_receive_timeout_s,
            acknowledgement_timeout_s=config.alert_acknowledgement_timeout_s,
        )
        alert_inbox = AlertInboxAgent(alert_inbox_jid, config.password,
                                      allowed_sender=alert_processor_jid,
                                      receive_timeout_s=config.alert_receive_timeout_s)
        scorer = RiskScoringAgent(
            scorer_jid, config.password,
            subscribers=[collector_jid, hotspot_jid, alert_processor_jid],
            hotspot_subscribers=[], village_jids=village_jids,
            method=method, classes=classes,
        )
        monitor = WellMonitorAgent(monitor_jid, config.password, monthly, divisions,
                                   recipients=[scorer_jid])
        clock = ClockAgent(clock_jid, config.password, recipients=[monitor_jid])
        agents: list[tuple[str, Agent]] = (
            [(f"VillageAgent:{name}", agent) for name, agent in zip(divisions, villages)]
            + [("AlertInboxAgent", alert_inbox), ("AlertProcessingAgent", alert_agent),
               ("RiskRecordCollector", collector), ("HotspotAgent", hotspot_agent),
               ("RiskScoringAgent", scorer),
               ("WellMonitorAgent", monitor), ("ClockAgent", clock)]
        )
        for _name, agent in agents:
            agent.message_log_path = message_log_path

        for name, agent in agents:
            agent.event_journal_path = event_path
            agent.run_id = run_id
            agent.monitor_component = name
            agent.monitor_behavior_failures = []
            _append_monitor_event(event_path, "agent_starting", name, identity=str(agent.jid))
            try:
                await agent.start(auto_register=True)
            except Exception:
                record_lifecycle(name, "start_failed", agent)
                raise
            started_agents.append((name, agent))
            record_lifecycle(name, "started", agent)
            common.add_agent_heartbeat(agent)

        manifest["stage_status"]["risk_scoring"] = "running"
        for month_index in range(config.start_month_index, config.end_month_index + 1):
            for _component, live_agent in agents:
                live_agent.monitor_month_index = month_index
            calendar_month = config.calendar_month_by_index[month_index]
            month_status: dict[str, Any] = {
                "month_index": month_index,
                "calendar_month": calendar_month,
                "status": "running",
                "clock_tick": "pending",
                "risk_delivery": "pending",
                "hotspot_screening": "unavailable" if config.hotspot_rule is None else "pending",
                "alert_decisions": "pending",
                "contract_net": "pending",
            }
            months.append(month_status)
            _append_monitor_event(event_path, "month_started", "SimulationRunner",
                                  month_index=month_index, calendar_month=calendar_month)
            await clock.publish_month(month_index, calendar_month, timeout_s=config.clock_send_timeout_s)
            month_status["clock_tick"] = "published"
            _append_monitor_event(event_path, "month_stage", "SimulationRunner",
                                  month_index=month_index, stage="clock_tick", status="published")
            risk_ready, reason = await _wait_for_month(
                month_index, divisions, monitor, scorer, collector, villages, config.stage_timeout_s,
            )
            month_records = [dict(row) for row in collector.records
                             if row["month_index"] == month_index]
            risk_records.extend(month_records)
            hotspot_received = False
            if risk_ready:
                hotspot_received = await _wait_for_hotspot_month(
                    month_index, divisions, hotspot_agent, config.hotspot_stage_timeout_s,
                )
            screens = hotspot_agent.finalize_month(month_index)
            await hotspot_agent.publish_month_results(month_index)
            for screen in screens:
                _append_monitor_event(event_path, "hotspot_result", "HotspotAgent",
                                      identity=hotspot_jid, month_index=month_index,
                                      ds_division=screen.ds_division, screening_status=screen.status,
                                      reason=screen.reason)
            alerts_ready = await _wait_for_alert_month(
                month_index, divisions, alert_agent, config.alert_stage_timeout_s,
            )
            if not alerts_ready:
                alert_agent.finalize_month(month_index)
            ack_deadline = time.monotonic() + config.alert_acknowledgement_timeout_s
            while alert_agent.pending_ack and time.monotonic() < ack_deadline:
                alert_agent.expire_confirmations()
                await asyncio.sleep(0.02)
            alert_agent.expire_confirmations(float("inf"))
            month_alerts = [row for row in alert_agent.records if row.get("month_index") == month_index]
            month_status["alert_decisions"] = "complete" if alerts_ready else "partially_unavailable"
            month_status["alert_delivery"] = {
                "requested": sum(row.get("delivery_status") in {"requested", "sent", "confirmed", "failed"}
                                  for row in month_alerts),
                "sent": sum(row.get("delivery_status") in {"sent", "confirmed"} for row in month_alerts),
                "confirmed_at_local_inbox": sum(row.get("delivery_status") == "confirmed" for row in month_alerts),
                "failed": sum(row.get("delivery_status") == "failed" for row in month_alerts),
                "suppressed": sum(row.get("status") == "suppressed" for row in month_alerts),
                "unavailable": sum(row.get("status") == "unavailable" for row in month_alerts),
            }
            month_status["hotspot_screening"] = (
                "complete" if hotspot_received and all(s.status != "unavailable" for s in screens)
                else "partially_unavailable"
            )
            _append_monitor_event(event_path, "month_stage", "SimulationRunner",
                                  month_index=month_index, stage="risk_delivery",
                                  status="complete" if risk_ready else "unavailable")
            _append_monitor_event(event_path, "month_stage", "SimulationRunner",
                                  month_index=month_index, stage="alert_decisions",
                                  status=month_status["alert_decisions"],
                                  delivery=month_status["alert_delivery"])
            _append_monitor_event(event_path, "month_stage", "SimulationRunner",
                                  month_index=month_index, stage="hotspot_screening",
                                  status=month_status["hotspot_screening"])
            for alert_row in month_alerts:
                _append_monitor_event(event_path, "alert_outcome", "AlertProcessingAgent",
                                      identity=alert_processor_jid, month_index=month_index,
                                      ds_division=alert_row.get("ds_division"),
                                      decision_status=alert_row.get("status"),
                                      delivery_status=alert_row.get("delivery_status"),
                                      reason=alert_row.get("reason"))
            if not risk_ready:
                month_status.update(status="incomplete", risk_delivery="unavailable", reason=reason,
                                    contract_net="skipped_missing_current_month_risk")
                _append_monitor_event(event_path, "month_completed", "SimulationRunner",
                                      month_index=month_index, status="incomplete",
                                      contract_net=month_status["contract_net"])
                failures.append({"stage": f"month_index={month_index}", "error": reason or "risk delivery failed"})
                continue

            month_status["risk_delivery"] = "complete"
            divisions_for_month = {record["ds_division"] for record in month_records}
            if divisions_for_month != set(divisions):
                month_status.update(status="incomplete", contract_net="skipped_missing_current_month_risk")
                _append_monitor_event(event_path, "month_completed", "SimulationRunner",
                                      month_index=month_index, status="incomplete",
                                      contract_net=month_status["contract_net"])
                continue

            coordinator_jid = _jid(f"{config.agent_identities.coordinator_prefix}{month_index}", config.domain)
            round_path = round_dir / f"month_index_{month_index:04d}_round_{config.round_number}.json"
            coordinator_config = contract_net.CoordinatorConfig(
                month_index=month_index, round_number=config.round_number,
                resource=config.resource, resource_pool=config.resource_pool,
                recipients=[contract_net.VillageRecipient(village_jids[name], name) for name in divisions],
                proposal_timeout_s=config.proposal_timeout_s,
                confirmation_timeout_s=config.confirmation_timeout_s,
                output_path=round_path,
                thread_id=f"ckdu-{_slug(run_id)}-month-{month_index}-round-{config.round_number}",
                provenance="synthetic-input SPADE simulation; single bounded Contract Net round",
            )
            coordinator = ResourceCoordinatorAgent(coordinator_jid, config.password, coordinator_config)
            coordinator.message_log_path = message_log_path
            name = f"ResourceCoordinatorAgent:month_index={month_index}:round={config.round_number}"
            coordinator.event_journal_path = event_path
            coordinator.run_id = run_id
            coordinator.monitor_component = name
            coordinator.monitor_behavior_failures = []
            _append_monitor_event(event_path, "agent_starting", name, identity=coordinator_jid)
            try:
                await coordinator.start(auto_register=True)
            except Exception:
                _append_monitor_event(event_path, "agent_start_failed", name,
                                      identity=str(coordinator.jid), lifecycle_state="start_failed")
                lifecycle.append({"component": name, "event": "start_failed", "at": _utc_now(),
                                  "jid": str(coordinator.jid)})
                raise
            started_agents.append((name, coordinator))
            _append_monitor_event(event_path, "agent_started", name, identity=str(coordinator.jid),
                                  lifecycle_state="started")
            lifecycle.append({"component": name, "event": "started", "at": _utc_now(),
                              "jid": str(coordinator.jid)})
            common.add_agent_heartbeat(coordinator)
            behaviour = next(iter(coordinator.behaviours))
            try:
                await asyncio.wait_for(
                    behaviour.join(),
                    timeout=config.proposal_timeout_s + config.confirmation_timeout_s + 15.0,
                )
                round_outcome = coordinator.round_result
                if round_outcome is None:
                    _append_monitor_event(event_path, "agent_work_failed", name,
                                          identity=str(coordinator.jid), month_index=month_index,
                                          round=config.round_number, outcome="unavailable_no_round_result")
                elif (coordinator.monitor_behavior_failures
                      or round_outcome.get("award_status") == "protocol_error"):
                    _append_monitor_event(event_path, "agent_work_failed", name,
                                          identity=str(coordinator.jid), month_index=month_index,
                                          round=config.round_number,
                                          outcome=round_outcome.get("award_status"))
                else:
                    _append_monitor_event(event_path, "agent_work_completed", name,
                                          identity=str(coordinator.jid), month_index=month_index,
                                          round=config.round_number,
                                          outcome=round_outcome.get("award_status"))
            finally:
                try:
                    await coordinator.stop()
                except Exception as exc:
                    failures.append({"stage": f"shutdown:{name}",
                                     "error": f"{type(exc).__name__}: {exc}"})
                    _append_monitor_event(event_path, "agent_stop_failed", name,
                                          identity=str(coordinator.jid), lifecycle_state="stop_failed")
                    lifecycle.append({"component": name, "event": "stop_failed", "at": _utc_now(),
                                      "jid": str(coordinator.jid)})
                else:
                    started_agents.remove((name, coordinator))
                    _append_monitor_event(event_path, "agent_stopped", name,
                                          identity=str(coordinator.jid), lifecycle_state="stopped")
                    lifecycle.append({"component": name, "event": "stopped", "at": _utc_now()})
            if coordinator.round_result is None:
                month_status.update(status="incomplete", contract_net="no_round_result")
                failures.append({"stage": name, "error": "coordinator completed without a round result"})
                _append_monitor_event(event_path, "round_completed", name,
                                      identity=str(coordinator.jid), month_index=month_index,
                                      round=config.round_number, award_status="unavailable",
                                      winner=None, reason="coordinator_completed_without_result")
            else:
                month_status["contract_net"] = coordinator.round_result["award_status"]
                month_status["status"] = "complete"
                manifest["round_result_paths"].append(str(round_path.relative_to(output_root)))
                _append_monitor_event(event_path, "round_completed", name,
                                      identity=str(coordinator.jid), month_index=month_index,
                                      round=config.round_number,
                                      award_status=coordinator.round_result.get("award_status"),
                                      winner=coordinator.round_result.get("winner"),
                                      result_path=str(round_path.relative_to(output_root)))
            _append_monitor_event(event_path, "month_completed", "SimulationRunner",
                                  month_index=month_index, status=month_status["status"],
                                  contract_net=month_status["contract_net"])

        manifest["stage_status"]["risk_scoring"] = (
            "complete" if risk_records else "unavailable_no_risk_records"
        )
        manifest["stage_status"]["hotspot_screening"] = (
            "unavailable_rule_not_configured" if config.hotspot_rule is None
            else "screening_outputs_recorded"
        )
        manifest["stage_status"]["hotspot_agent"] = (
            "processed_risk_records" if hotspot_agent and (
                any(hotspot_agent.risk_history.values()) or hotspot_agent.rejected_records
            ) else "started_no_risk_records_received"
        )
        manifest["stage_status"]["alert_decisions"] = (
            "processed_typed_risk_and_hotspot_messages" if alert_agent and alert_agent.records
            else "unavailable_no_paired_evidence"
        )
        manifest["stage_status"]["alert_transport"] = "local_simulated_inbox_not_real_health_officer"
        manifest["stage_status"]["contract_net"] = (
            "single_round_per_completed_month" if any(m["contract_net"] not in {
                "pending", "skipped_missing_current_month_risk", "no_round_result"} for m in months)
            else "unavailable_no_completed_rounds"
        )
        manifest["stage_status"]["agent_monitor"] = "event_journal_and_message_log_available"

        if scorer.history:
            risk_outputs.write_outputs(
                scorer.history,
                risk_path=output_root / "logs/risk_scores.csv",
                well_path=output_root / "logs/well_scores.csv",
            )
        else:
            manifest["stage_status"]["risk_score_files"] = "unavailable_no_scored_records"
        _write_json_exclusive(output_root / "risk_records.json", risk_records)
        if config.hotspot_rule is None:
            manifest["stage_status"]["hotspot_screening"] = "unavailable_rule_not_configured"
        allocation_path = _write_confirmed_allocation_rows(
            output_root, manifest["round_result_paths"], config.calendar_month_by_index,
        )
        population_by_division = {
            str(name): int(divisions_indexed.loc[name, "population"])
            for name in divisions
        }
        manifest["allocation_evidence"] = {
            "path": str(allocation_path.relative_to(output_root)),
            "status": "confirmed_contract_net_rows_only",
            "provenance": "synthetic local SPADE simulation; exported from persisted round artifacts",
            "baselines_supplied": [],
            "risk_weight_definition": None,
            "population_source": str(config.divisions_path),
            "team_output_attestation": False,
        }

        def alert_risk_receipt(event: Any) -> bool:
            return (event.payload.get("type") == "risk_record"
                    and event.receiver == alert_processor_jid)

        results = evaluate(
            messages_path=log_path,
            allocations_path=allocation_path,
            output_path=output_root / "logs/evaluation_summary.csv",
            risk_records=risk_records or None,
            population_by_village=population_by_division,
            risk_input_selector=alert_risk_receipt,
            alert_correlation_records=alert_agent.records if alert_agent else None,
            negotiation_history_complete=_has_complete_negotiation_history(
                output_root, manifest["round_result_paths"], months,
                recipient_count=len(divisions), round_number=config.round_number,
            ),
        )
        manifest["evaluation_status"] = {
            result.metric: {"status": result.status, "value": result.value,
                            "unit": result.unit, "reason": result.details.get("reason"),
                            "availability": dict(result.details)}
            for result in results
        }
        manifest["message_counts"] = _read_message_counts(log_path)
        manifest["status"] = (
            "completed_with_unavailable_stages"
            if _has_unavailable_stages(
                manifest.get("stage_status", {}), months,
                manifest.get("evaluation_status", {}), {"failures": failures},
            ) else "completed"
        )
        _append_monitor_event(event_path, "run_completed", "SimulationRunner",
                              status=manifest["status"])
    except Exception as exc:
        failures.append({"stage": "runner", "error": f"{type(exc).__name__}: {exc}"})
        manifest["status"] = "failed"
        _append_monitor_event(event_path, "run_failed", "SimulationRunner",
                              status="failed", reason=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        for name, agent in reversed(started_agents):
            if manifest.get("status") != "failed":
                failures_for_agent = getattr(agent, "monitor_behavior_failures", [])
                event_type = "agent_work_failed" if failures_for_agent else "agent_work_completed"
                _append_monitor_event(
                    event_path, event_type, name, identity=str(agent.jid),
                    outcome=("behavior_failed" if failures_for_agent
                             else "completed_with_unavailable_stages"
                             if manifest.get("status") == "completed_with_unavailable_stages"
                             else "completed"),
                    behavior_failures=failures_for_agent,
                )
            try:
                await agent.stop()
            except Exception as exc:
                failures.append({"stage": f"shutdown:{name}", "error": f"{type(exc).__name__}: {exc}"})
                _append_monitor_event(event_path, "agent_stop_failed", name,
                                      identity=str(agent.jid), lifecycle_state="stop_failed")
                lifecycle.append({"component": name, "event": "stop_failed", "at": _utc_now()})
            else:
                _append_monitor_event(event_path, "agent_stopped", name,
                                      identity=str(agent.jid), lifecycle_state="stopped")
                lifecycle.append({"component": name, "event": "stopped", "at": _utc_now()})
        if hotspot_agent is not None:
            try:
                hotspot_agent.write_output()
            except Exception as exc:
                failures.append({"stage": "hotspot_output", "error": f"{type(exc).__name__}: {exc}"})
        if alert_agent is not None:
            try:
                alert_agent.write_output()
            except Exception as exc:
                failures.append({"stage": "alert_output", "error": f"{type(exc).__name__}: {exc}"})
        manifest["completed_at"] = _utc_now()
        manifest["agent_lifecycle"] = lifecycle
        manifest["month_status"] = months
        manifest["failures"] = failures
        if log_path.is_file():
            try:
                manifest["message_counts"] = _read_message_counts(log_path)
            except Exception as exc:
                failures.append({"stage": "message_log_summary", "error": str(exc)})
        if not manifest.get("stage_status"):
            manifest["stage_status"] = {"runner": "failed_before_agent_start"}
        if not manifest.get("status") or manifest["status"] == "running":
            manifest["status"] = "failed" if failures else "incomplete"
        elif manifest["status"] != "failed":
            manifest["status"] = (
                "completed_with_unavailable_stages"
                if _has_unavailable_stages(
                    manifest.get("stage_status", {}), months,
                    manifest.get("evaluation_status", {}), {"failures": failures},
                ) else "completed"
            )
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        _append_monitor_event(event_path, "run_finished", "SimulationRunner",
                              status=manifest["status"])
        _rewrite_owned_json(manifest_path, manifest)
    return manifest


def _month_mapping(start: int, months: Sequence[int]) -> dict[int, int]:
    return dict(zip(range(start, start + len(months)), months))


def _parse_int_list(raw: str) -> list[int]:
    try:
        return [int(value.strip()) for value in raw.split(",") if value.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("months must be comma-separated integers") from exc


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run a fresh, isolated multi-month CKDu MAS simulation")
    parser.add_argument("--start-month-index", type=int, default=0)
    parser.add_argument("--end-month-index", type=int, required=True,
                        help="inclusive simulated month_index; provide one calendar label per index")
    parser.add_argument("--calendar-months", type=_parse_int_list, required=True,
                        help="explicit calendar month labels 1..12, comma-separated")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="fresh output root; must not be production logs/data/fixtures/demo_output")
    parser.add_argument("--resource", choices=sorted(ontology.RESOURCES), default="RO_unit")
    parser.add_argument("--resource-pool", type=int, default=1)
    parser.add_argument("--round-number", type=int, default=1)
    parser.add_argument("--proposal-timeout", type=float, default=5.0)
    parser.add_argument("--confirmation-timeout", type=float, default=5.0)
    parser.add_argument("--stage-timeout", type=float, default=45.0)
    parser.add_argument("--hotspot-stage-timeout", type=float, default=5.0,
                        help="seconds to await risk-record fan-out processing by HotspotAgent")
    parser.add_argument("--domain", default="localhost")
    parser.add_argument("--clock-id", default="clock")
    parser.add_argument("--well-monitor-id", default="well_monitor")
    parser.add_argument("--risk-scoring-id", default="risk_scoring")
    parser.add_argument("--hotspot-id", default="hotspot")
    parser.add_argument("--alert-processing-id", default="alert_processing")
    parser.add_argument("--alert-inbox-id", default="alert_inbox")
    parser.add_argument("--alert-receive-timeout", type=float, default=0.2)
    parser.add_argument("--alert-stage-timeout", type=float, default=5.0)
    parser.add_argument("--alert-acknowledgement-timeout", type=float, default=2.0)
    parser.add_argument("--risk-observer-prefix", default="risk_observer")
    parser.add_argument("--coordinator-prefix", default="coordinator_m")
    parser.add_argument("--village-prefix", default="village_")
    parser.add_argument("--password", default="demo", help="XMPP password; omit from saved manifests")
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--method", type=Path, default=DEFAULT_METHOD)
    parser.add_argument("--divisions", type=Path, default=DEFAULT_DIVISIONS)
    parser.add_argument("--hotspot-trigger-tier", action="append", default=None,
                        help="explicit provisional screening tier (repeat flag; no default rule)")
    parser.add_argument("--hotspot-consecutive-months", type=int)
    parser.add_argument("--embedded-server", action="store_true",
                        help="use SPADE's embedded XMPP server")
    args = parser.parse_args(argv)
    calendar = _month_mapping(args.start_month_index, args.calendar_months)
    if args.end_month_index != args.start_month_index + len(args.calendar_months) - 1:
        parser.error("calendar-months must contain exactly one value for each configured month_index")
    rule = None
    if args.hotspot_trigger_tier is not None or args.hotspot_consecutive_months is not None:
        if not args.hotspot_trigger_tier or args.hotspot_consecutive_months is None:
            parser.error("configure both --hotspot-trigger-tier and --hotspot-consecutive-months, or neither")
        rule = ScreeningRule("cli_configured_screening_v1", frozenset(args.hotspot_trigger_tier),
                             args.hotspot_consecutive_months)
    config = SimulationConfig(
        start_month_index=args.start_month_index, end_month_index=args.end_month_index,
        calendar_month_by_index=calendar, output_dir=args.output_dir,
        resource=args.resource, resource_pool=args.resource_pool, round_number=args.round_number,
        proposal_timeout_s=args.proposal_timeout, confirmation_timeout_s=args.confirmation_timeout,
        stage_timeout_s=args.stage_timeout, hotspot_stage_timeout_s=args.hotspot_stage_timeout,
        alert_stage_timeout_s=args.alert_stage_timeout,
        alert_receive_timeout_s=args.alert_receive_timeout,
        alert_acknowledgement_timeout_s=args.alert_acknowledgement_timeout,
        domain=args.domain,
        embedded_server=args.embedded_server, password=args.password,
        run_id=args.run_id, input_path=args.input, method_path=args.method,
        divisions_path=args.divisions, hotspot_rule=rule,
        agent_identities=AgentIdentityConfig(
            clock=args.clock_id, well_monitor=args.well_monitor_id,
            risk_scoring=args.risk_scoring_id, hotspot=args.hotspot_id,
            alert_processing=args.alert_processing_id, alert_inbox=args.alert_inbox_id,
            risk_observer_prefix=args.risk_observer_prefix,
            coordinator_prefix=args.coordinator_prefix, village_prefix=args.village_prefix,
        ),
    )
    spade.run(run_simulation(config), embedded_xmpp_server=config.embedded_server)


if __name__ == "__main__":
    main()
