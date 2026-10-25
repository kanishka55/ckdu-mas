"""Evidence-gated evaluation for CKDu MAS logs.

The evaluator validates the current seven-column message log and typed
ontology. Metrics requiring unagreed mappings, verified allocations, complete
negotiation history, or an event-correlation contract remain unavailable.
Computed indicators are comparisons only, not medical or causal findings.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from src.core.msg_log import COLUMNS as MESSAGE_COLUMNS
from src.core.ontology import (
    ACCEPT_PROPOSAL, CFP, FAILURE, INFORM, PROPOSE, REFUSE, REJECT_PROPOSAL,
    SchemaError, decode, validate,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MESSAGES_PATH = ROOT / "logs" / "messages.csv"
DEFAULT_ALLOCATIONS_PATH = ROOT / "logs" / "allocations.csv"
DEFAULT_SUMMARY_PATH = ROOT / "logs" / "evaluation_summary.csv"
ALLOCATION_COLUMNS = ("month", "resource", "ds_division", "round", "winning_bid", "method")
ALLOCATION_METHODS = ("contract_net", "equal_split", "first_come")
NEGOTIATION_PERFORMATIVES = frozenset({CFP, PROPOSE, REFUSE, ACCEPT_PROPOSAL, REJECT_PROPOSAL, FAILURE})


class EvaluationInputError(ValueError):
    """Raised when a present input contains malformed or incomplete records."""


@dataclass(frozen=True)
class MetricResult:
    metric: str
    scope: str
    value: float | int | None
    unit: str
    status: str
    observations: int
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "scope": self.scope,
            "value": "" if self.value is None else self.value,
            "unit": self.unit,
            "status": self.status,
            "observations": self.observations,
            "details": json.dumps(self.details, sort_keys=True, separators=(",", ":")),
        }


@dataclass(frozen=True)
class MessageEvent:
    timestamp: datetime
    sender: str
    receiver: str
    performative: str
    thread_id: str
    payload: Mapping[str, Any]
    direction: str


@dataclass(frozen=True)
class AllocationRow:
    month: int
    resource: str
    ds_division: str
    round: int
    winning_bid: float
    method: str


def _availability_guidance(metric: str, reason: str) -> dict[str, str]:
    """Give a concise, metric-specific account of why evidence is unavailable."""
    if metric == "risk_classification_alignment":
        if "reference" in reason:
            return {"classification": "evidence", "missing_dependency": "sourced reference-tier mapping",
                    "required_action": "Supply an independently sourced, team-approved division-to-tier mapping."}
        return {"classification": "evidence", "missing_dependency": "valid risk records",
                "required_action": "Produce typed risk records for the evaluation period."}
    if metric.startswith("allocation") or metric.startswith("contract_net_minus_") or metric == "risk_weighted_population_served":
        if "definition" in reason and "weight" in reason:
            category, dependency, action = "configuration", "approved risk-weight definition", "Agree and document the risk-weight formula."
        elif "population" in reason or "risk-weight mapping" in reason or "numeric risk-weight mapping" in reason:
            category, dependency, action = "evidence", "population and division/month risk-weight inputs", "Supply sourced population and numeric risk-weight values for allocated divisions and months."
        elif "attested" in reason or "verified" in reason:
            category, dependency, action = "provenance", "allocation provenance", "Attest the origin and scope of the allocation rows."
        elif "method" in reason or "baseline" in reason or "allocation" in reason or "rows" in reason:
            category, dependency, action = "evidence", "comparable allocation rows and baselines", "Supply Contract Net, equal-split, and first-come rows for the same months and resources."
        else:
            category, dependency, action = "evidence", "allocation inputs", "Supply the required allocation evidence."
        return {"classification": category, "missing_dependency": dependency, "required_action": action}
    if metric == "alert_latency":
        if "selector" in reason or "correlation" in reason or "ambiguous" in reason:
            return {"classification": "configuration", "missing_dependency": "unambiguous risk-receipt to alert correlation",
                    "required_action": "Provide a receiver-specific risk selector and explicit alert-to-source correlation records; do not pair by timestamp proximity."}
        return {"classification": "evidence", "missing_dependency": "paired received-risk and sent-alert events",
                "required_action": "Record both endpoints with a usable correlation link and timezone-aware logger timestamps."}
    if metric.startswith("negotiation_"):
        return {"classification": "evidence", "missing_dependency": "complete consistently threaded round history",
                "required_action": "Persist complete CFP, response, award, and confirmation evidence for each evaluated round."}
    return {"classification": "evidence", "missing_dependency": "metric-specific inputs", "required_action": "Supply the documented evidence required by this metric."}


def _unavailable(metric: str, scope: str, unit: str, reason: str) -> MetricResult:
    details = {"reason": reason, **_availability_guidance(metric, reason)}
    return MetricResult(metric, scope, None, unit, "unavailable", 0, details)


def _finite_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EvaluationInputError(f"{context} must be a finite number")
    return float(value)


def _read_exact_csv(path: str | Path, expected_columns: Sequence[str]) -> list[dict[str, str]]:
    csv_path = Path(path)
    if not csv_path.is_file():
        raise FileNotFoundError(csv_path)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(expected_columns):
            raise EvaluationInputError(
                f"{csv_path}: expected columns {list(expected_columns)!r}; got {reader.fieldnames!r}"
            )
        rows: list[dict[str, str]] = []
        for line_number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise EvaluationInputError(f"{csv_path}:{line_number}: malformed column count")
            rows.append({key: value for key, value in row.items() if key is not None})
        return rows


def _parse_timestamp(value: str, context: str) -> datetime:
    if not value:
        raise EvaluationInputError(f"{context}: timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationInputError(f"{context}: invalid ISO timestamp {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvaluationInputError(f"{context}: timestamp must include a timezone")
    return parsed


def read_message_events(path: str | Path) -> list[MessageEvent]:
    """Read all rows under the current logger contract; never silently drop."""
    rows = _read_exact_csv(path, MESSAGE_COLUMNS)
    events: list[MessageEvent] = []
    for line_number, row in enumerate(rows, start=2):
        context = f"{path}:{line_number}"
        direction = row["direction"]
        if direction not in {"sent", "received"}:
            raise EvaluationInputError(f"{context}: direction must be 'sent' or 'received'")
        timestamp = _parse_timestamp(row["timestamp"], context)
        if not row["performative"]:
            raise EvaluationInputError(f"{context}: performative is missing")
        try:
            payload = decode(row["performative"], row["payload"])
        except SchemaError as exc:
            raise EvaluationInputError(f"{context}: invalid typed message: {exc}") from exc
        events.append(
            MessageEvent(
                timestamp, row["sender"], row["receiver"],
                row["performative"], row["thread_id"], payload, direction,
            )
        )
    return events


def read_allocation_rows(path: str | Path) -> list[AllocationRow]:
    """Read Member C's documented allocation CSV, rejecting malformed rows."""
    rows = _read_exact_csv(path, ALLOCATION_COLUMNS)
    parsed: list[AllocationRow] = []
    for line_number, row in enumerate(rows, start=2):
        context = f"{path}:{line_number}"
        try:
            month, round_number = int(row["month"]), int(row["round"])
            winning_bid = float(row["winning_bid"])
        except (TypeError, ValueError) as exc:
            raise EvaluationInputError(f"{context}: month, round and winning_bid must be numeric") from exc
        if month < 1 or round_number < 1 or not math.isfinite(winning_bid):
            raise EvaluationInputError(f"{context}: invalid month, round, or winning_bid")
        if row["resource"] not in {"RO_unit", "bowser"}:
            raise EvaluationInputError(f"{context}: unsupported resource {row['resource']!r}")
        if row["method"] not in ALLOCATION_METHODS:
            raise EvaluationInputError(f"{context}: unsupported method {row['method']!r}")
        if not row["ds_division"]:
            raise EvaluationInputError(f"{context}: ds_division is missing")
        parsed.append(AllocationRow(month, row["resource"], row["ds_division"], round_number, winning_bid, row["method"]))
    return parsed


def extract_risk_records(events: Iterable[MessageEvent]) -> list[dict[str, Any]]:
    """Extract and deduplicate typed risk records across sent/received copies."""
    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for event in events:
        if event.payload.get("type") != "risk_record":
            continue
        record = dict(event.payload)
        try:
            validate(INFORM, record)
        except SchemaError as exc:
            raise EvaluationInputError(f"Invalid risk-record event: {exc}") from exc
        key = (record["ds_division"], record["month_index"])
        if key in unique and unique[key] != record:
            raise EvaluationInputError(f"conflicting risk records for division/month_index {key!r}")
        unique[key] = record
    return list(unique.values())


def calculate_risk_alignment(
    risk_records: Sequence[Mapping[str, Any]],
    reference_tier_by_village: Mapping[str, str] | None,
    *,
    reference_mapping_source: str = "caller-supplied mapping; provenance not verified",
) -> MetricResult:
    """Compare tiers only when the caller explicitly supplies a mapping."""
    metric = "risk_classification_alignment"
    if not risk_records:
        return _unavailable(metric, "all", "proportion", "no risk records")
    if not reference_tier_by_village:
        return _unavailable(metric, "all", "proportion", "reference-to-tier mapping is not supplied")
    matched = aligned = 0
    unmatched: list[str] = []
    for source_record in risk_records:
        record = dict(source_record)
        try:
            validate(INFORM, record)
        except SchemaError as exc:
            raise EvaluationInputError(f"Invalid risk record: {exc}") from exc
        division = record["ds_division"]
        expected = reference_tier_by_village.get(division)
        if expected is None:
            unmatched.append(division)
            continue
        if not isinstance(expected, str) or not expected:
            raise EvaluationInputError(f"invalid reference tier for {division!r}: {expected!r}")
        matched += 1
        aligned += record["tier"] == expected
    if not matched:
        reason = "no risk records matched reference divisions"
        return MetricResult(metric, "all", None, "proportion", "unavailable", 0,
                            {"reason": reason, **_availability_guidance(metric, reason),
                             "unmatched_divisions": sorted(set(unmatched))})
    details: dict[str, Any] = {
        "aligned": aligned, "unmatched_records": len(unmatched),
        "unmatched_divisions": sorted(set(unmatched)),
        "interpretation": "alignment check only; not scientific validation or evidence of causation",
        "reference_mapping_source": reference_mapping_source,
    }
    if unmatched:
        reason = "reference-tier mapping does not cover every evaluated division"
        details.update({"reason": reason, **_availability_guidance(metric, reason),
                       "missing_dependency": "reference-tier labels for: " + ", ".join(sorted(set(unmatched))),
                       "required_action": "Supply sourced reference-tier labels for all unmatched divisions before interpreting full-coverage alignment."})
    return MetricResult(metric, "all", aligned / matched, "proportion",
                        "partial" if unmatched else "ok", matched, details)


def calculate_allocation_efficiency(
    allocation_rows: Sequence[AllocationRow],
    population_by_village: Mapping[str, int] | None,
    risk_weight_by_village_month: Mapping[tuple[str, int], float] | None,
    *,
    allocation_results_verified: bool = False,
    risk_weight_definition: str | None = None,
    evaluated_month_resources: Sequence[tuple[int, str]] | None = None,
) -> list[MetricResult]:
    """Calculate documented risk-weighted population only with explicit evidence.

    ``allocation_results_verified`` is a caller attestation that rows are actual
    team outputs, not fixtures. ``risk_weight_definition`` records the agreed
    scalar-risk definition. Both are mandatory; maps alone do not establish
    provenance or scientific meaning.

    ``evaluated_month_resources`` is a further caller attestation: every method was
    computed for each listed (month, resource). A method with no rows there then
    served nobody (0) instead of being missing, which is the normal case for durable
    units once every village with a need holds one. With it, run totals per method
    are also reported (scope ``all;resource=...``). Without it, behaviour is unchanged.
    """
    evaluated = set(evaluated_month_resources or ())
    metric = "allocation_efficiency"
    groups: dict[tuple[int, str, str], set[str]] = defaultdict(set)
    for row in allocation_rows:
        groups[(row.month, row.resource, row.method)].add(row.ds_division)
    missing: list[str] = []
    if not allocation_rows:
        missing.append("allocation rows")
        reason = "no allocation rows"
    else:
        reason = "allocation rows are not attested as actual team results"
        if not allocation_results_verified:
            missing.append("allocation provenance attestation")
        if not risk_weight_definition:
            missing.append("approved numeric risk-weight definition")
        if population_by_village is None:
            missing.append("division population mapping")
        if risk_weight_by_village_month is None:
            missing.append("division/month risk-weight mapping")
        missing_methods = sorted({
            method for month, resource, _method in groups
            for method in ALLOCATION_METHODS
            if not groups.get((month, resource, method)) and (month, resource) not in evaluated
        })
        if missing_methods:
            missing.append("allocation baseline method rows: " + ", ".join(missing_methods))
        if allocation_results_verified and not missing_methods and risk_weight_definition \
                and population_by_village is not None and risk_weight_by_village_month is not None:
            reason = ""
        elif not allocation_results_verified:
            reason = "allocation rows are not attested as actual team results"
        elif not risk_weight_definition:
            reason = "agreed numeric risk-weight definition is not supplied"
        elif population_by_village is None or risk_weight_by_village_month is None:
            reason = "population and numeric risk-weight mappings are required"
        elif missing_methods:
            reason = "required allocation method rows are missing: " + ", ".join(missing_methods)
    blocking_dependencies = [item for item in missing
                             if not item.startswith("allocation baseline method rows:")]
    if blocking_dependencies:
        details = _availability_guidance(metric, reason)
        details["missing_dependencies"] = missing
        details["missing_dependency"] = "; ".join(missing)
        details["required_action"] = (
            "Supply or approve: " + "; ".join(missing) + "."
        )
        return [MetricResult(metric, "all", None, "risk-weighted persons", "unavailable", 0,
                             {"reason": reason, **details})]

    month_resources = sorted({(month, resource) for month, resource, _ in groups} | evaluated)
    results: list[MetricResult] = []
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for month, resource in month_resources:
        scope = f"month={month};resource={resource}"
        absent_methods = [method for method in ALLOCATION_METHODS
                          if not groups.get((month, resource, method))
                          and (month, resource) not in evaluated]
        if absent_methods:
            reason = f"required allocation method rows are missing: {', '.join(absent_methods)}"
            for method in ALLOCATION_METHODS:
                results.append(_unavailable("risk_weighted_population_served",
                                             f"{scope};method={method}",
                                             "risk-weighted persons", reason))
            for baseline in ("equal_split", "first_come"):
                results.append(_unavailable(f"contract_net_minus_{baseline}", scope,
                                             "risk-weighted persons", reason))
            continue
        scores: dict[str, float | None] = {}
        served: dict[str, int] = {}
        for method in ALLOCATION_METHODS:
            villages = groups.get((month, resource, method))
            served[method] = len(villages or ())
            if not villages:
                scores[method] = 0.0 if (month, resource) in evaluated else None
                continue
            score = 0.0
            for village in villages:
                if village not in population_by_village:
                    raise EvaluationInputError(f"missing population for allocated division {village!r}")
                if (village, month) not in risk_weight_by_village_month:
                    raise EvaluationInputError(f"missing numeric risk weight for {(village, month)!r}")
                population = population_by_village[village]
                if isinstance(population, bool) or not isinstance(population, int) or population < 0:
                    raise EvaluationInputError(f"population for {village!r} must be a non-negative integer")
                risk_weight = _finite_number(risk_weight_by_village_month[(village, month)], f"risk weight for {(village, month)!r}")
                score += risk_weight * population
            scores[method] = score
        for method in ALLOCATION_METHODS:
            value = scores[method]
            if value is not None and (month, resource) in evaluated:
                totals[(resource, method)] += value
            results.append(MetricResult(
                "risk_weighted_population_served", f"{scope};method={method}", value,
                "risk-weighted persons", "ok" if value is not None else "unavailable",
                served[method], {"risk_weight_definition": risk_weight_definition} if value is not None
                else {"reason": "method has no allocation rows"},
            ))
        for baseline in ("equal_split", "first_come"):
            cn, base = scores["contract_net"], scores[baseline]
            available = cn is not None and base is not None
            results.append(MetricResult(
                f"contract_net_minus_{baseline}", scope, cn - base if available else None,
                "risk-weighted persons", "ok" if available else "unavailable", 1 if available else 0,
                {"risk_weight_definition": risk_weight_definition} if available else
                {"reason": f"missing {('contract_net' if cn is None else baseline)} baseline rows"},
            ))
    for resource in sorted({resource for _month, resource in evaluated}):
        scope = f"all;resource={resource}"
        months_counted = sum(1 for _month, r in evaluated if r == resource)
        details = {"risk_weight_definition": risk_weight_definition,
                   "aggregation": "sum over evaluated months"}
        for method in ALLOCATION_METHODS:
            results.append(MetricResult(
                "risk_weighted_population_served", f"{scope};method={method}",
                totals[(resource, method)], "risk-weighted persons", "ok", months_counted, details))
        for baseline in ("equal_split", "first_come"):
            results.append(MetricResult(
                f"contract_net_minus_{baseline}", scope,
                totals[(resource, "contract_net")] - totals[(resource, baseline)],
                "risk-weighted persons", "ok", months_counted, details))
    return results


def calculate_alert_latency(
    events: Sequence[MessageEvent],
    risk_input_selector: Callable[[MessageEvent], bool] | None,
    *,
    alert_correlation_by_thread: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> MetricResult:
    """Mean logged seconds for uniquely correlated risk receipt/alert issue pairs.

    By default correlation requires matching ``thread_id``, ``ds_division``
    and ``month_index``. When supplied, ``alert_correlation_by_thread`` is an
    explicit bridge from each alert thread to its source risk thread and
    division/month, sourced from AlertProcessingAgent decision records. The
    input must be a received typed risk_record and the endpoint a sent typed
    alert. No proximity or collection-time inference is used.
    """
    metric = "alert_latency"
    if not events:
        return _unavailable(metric, "all", "seconds", "no message events")
    if risk_input_selector is None:
        return _unavailable(metric, "all", "seconds", "risk-input event selector is not supplied")
    inputs: dict[tuple[str, str, int], list[MessageEvent]] = defaultdict(list)
    alerts: dict[tuple[str, str, int], list[MessageEvent]] = defaultdict(list)
    for event in events:
        if event.payload.get("type") == "alert" and event.direction == "sent":
            key = (event.thread_id, event.payload["ds_division"], event.payload["month_index"])
            alerts[key].append(event)
        if risk_input_selector(event):
            if event.payload.get("type") != "risk_record":
                raise EvaluationInputError("selected risk input must be a typed risk_record")
            # The logger records both endpoints. Only the input arrival at the
            # receiving agent is the latency start; its sent copy is ignored.
            if event.direction != "received":
                continue
            key = (event.thread_id, event.payload["ds_division"], event.payload["month_index"])
            inputs[key].append(event)
    if not alerts:
        return _unavailable(metric, "all", "seconds", "no sent alert-issued events")
    latencies: list[float] = []
    unmatched = ambiguous = 0
    for key, alert_events in alerts.items():
        input_key = key
        if alert_correlation_by_thread is not None:
            links = alert_correlation_by_thread.get(key[0], ())
            if len(links) != 1:
                ambiguous += int(len(links) > 1)
                unmatched += int(not links)
                continue
            link = links[0]
            source_thread = link.get("source_thread_id")
            division = link.get("ds_division")
            month = link.get("month_index")
            if (not isinstance(source_thread, str) or not source_thread
                    or division != key[1] or month != key[2]):
                ambiguous += 1
                continue
            input_key = (source_thread, division, month)
        input_events = inputs.get(input_key, [])
        if len(alert_events) != 1 or len(input_events) > 1:
            ambiguous += 1
            continue
        if not input_events:
            unmatched += 1
            continue
        elapsed = (alert_events[0].timestamp - input_events[0].timestamp).total_seconds()
        if elapsed < 0:
            raise EvaluationInputError(f"alert for correlation key {key!r} predates risk receipt")
        latencies.append(elapsed)
    if not latencies:
        reason = "no uniquely correlated risk receipt and sent alert" if not ambiguous else "ambiguous correlation keys"
        return MetricResult(metric, "all", None, "seconds", "unavailable", 0,
                            {"reason": reason, **_availability_guidance(metric, reason), "unmatched_alerts": unmatched,
                             "ambiguous_pairs": ambiguous, "suppression_events_counted": False})
    details: dict[str, Any] = {
        "unmatched_alerts": unmatched, "ambiguous_pairs": ambiguous,
        "suppression_events_counted": False,
        "pairing": ("alert_decisions source_thread_id + exact ds_division/month_index bridge; received risk_record to sent alert"
                    if alert_correlation_by_thread is not None else
                    "exact thread_id, ds_division and month_index; received risk_record to sent alert"),
        "timestamp_basis": "message logger UTC append timestamps",
    }
    if unmatched or ambiguous:
        reason = "some alert pairs were unmatched or ambiguous"
        details.update({"reason": reason, **_availability_guidance(metric, reason),
                        "missing_dependency": "unique risk receipt and alert pairs for every issued alert",
                        "required_action": "Resolve missing or duplicate source links and rerun evaluation; ambiguous pairs are not counted."})
    return MetricResult(
        metric, "all", sum(latencies) / len(latencies), "seconds",
        "partial" if unmatched or ambiguous else "ok", len(latencies),
        details,
    )


def calculate_negotiation_efficiency(
    events: Sequence[MessageEvent],
    *,
    history_complete: bool = False,
) -> list[MetricResult]:
    """Count negotiation rounds/messages only after caller confirms full history."""
    rounds_metric = "negotiation_rounds"
    messages_metric = "negotiation_logged_event_rows"
    if not events:
        reason = "no message events"
    elif not history_complete:
        reason = "complete consistently threaded negotiation history is not confirmed"
    else:
        reason = ""
    if reason:
        return [_unavailable(rounds_metric, "all", "rounds", reason),
                _unavailable(messages_metric, "all", "unique messages", reason)]

    negotiation = [event for event in events if event.performative in NEGOTIATION_PERFORMATIVES
                   or event.payload.get("type") == "inform_done"]
    cfps = [event for event in negotiation if event.performative == CFP]
    if not cfps:
        reason = "no cfp events"
        return [_unavailable(rounds_metric, "all", "rounds", reason),
                _unavailable(messages_metric, "all", "unique messages", reason)]

    thread_month: dict[str, int] = {}
    rounds_by_month: dict[int, set[tuple[str, int]]] = defaultdict(set)
    for event in cfps:
        if not event.thread_id:
            return [_unavailable(rounds_metric, "all", "rounds", "CFP thread_id is missing"),
                    _unavailable(messages_metric, "all", "unique messages", "CFP thread_id is missing")]
        month, round_number = event.payload["month_index"], event.payload["round"]
        if event.thread_id in thread_month and thread_month[event.thread_id] != month:
            raise EvaluationInputError(f"negotiation thread {event.thread_id!r} spans multiple month_index values")
        thread_month[event.thread_id] = month
        rounds_by_month[month].add((event.thread_id, round_number))

    directions_by_message: dict[int, dict[tuple[str, str, str, str, str], Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    month_for_thread: dict[str, int] = {}
    for event in negotiation:
        if event.thread_id not in thread_month:
            return [_unavailable(rounds_metric, "all", "rounds",
                                 "negotiation events are not consistently associated with a CFP thread"),
                    _unavailable(messages_metric, "all", "unique messages",
                                 "negotiation events are not consistently associated with a CFP thread")]
        expected_month = thread_month[event.thread_id]
        event_month = event.payload.get("month_index")
        if event_month != expected_month:
            raise EvaluationInputError(f"negotiation event thread {event.thread_id!r} has inconsistent month_index")
        # The logger has no message ID. Use its stable endpoint fields plus the
        # thread, performative, and canonical typed body. Sender/receiver are
        # oriented identically in the sent and received log copies, while
        # broadcast sends to different receivers remain distinct messages.
        key = (event.thread_id, event.sender, event.receiver, event.performative,
               json.dumps(event.payload, sort_keys=True, separators=(",", ":")))
        directions_by_message[expected_month][key][event.direction] += 1
        month_for_thread[event.thread_id] = expected_month

    results: list[MetricResult] = []
    for month in sorted(rounds_by_month):
        identities = directions_by_message[month]
        if any(count > 1 for directions in identities.values() for count in directions.values()):
            return [_unavailable(rounds_metric, "all", "rounds", "duplicate same-direction message identity is ambiguous"),
                    _unavailable(messages_metric, "all", "unique messages", "duplicate same-direction message identity is ambiguous")]
        # One sent row, one received row, or one of either direction represent
        # one unique message. More than one row in the same direction cannot
        # be disambiguated without a message ID, so the metric is unavailable.
        unique_messages = len(identities)
        round_count = len(rounds_by_month[month])
        results.extend([
            MetricResult(rounds_metric, f"month_index={month}", round_count, "rounds", "ok", round_count,
                         {"definition": "unique thread_id and round values from CFP events"}),
            MetricResult(messages_metric, f"month_index={month}", unique_messages, "unique messages", "ok", unique_messages,
                         {"counting_note": "sent/received rows with identical thread, oriented sender/receiver, performative and typed payload count once; repeated same-direction identities are unavailable"}),
        ])
    return results


def extract_logged_risk_records(events: Sequence[MessageEvent]) -> list[dict[str, Any]]:
    """Public wrapper for extracting deduplicated typed risk records."""
    return extract_risk_records(events)


def _write_summary(results: Sequence[MetricResult], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ("metric", "scope", "value", "unit", "status", "observations", "details")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(result.as_row() for result in results)


def print_summary(results: Sequence[MetricResult]) -> None:
    """Print a readable table without replacing missing values with zero."""
    headers = ("METRIC", "SCOPE", "VALUE", "UNIT", "STATUS", "N")
    rows = [(r.metric, r.scope, "—" if r.value is None else
             f"{r.value:.4g}" if isinstance(r.value, float) else str(r.value),
             r.unit, r.status, str(r.observations)) for r in results]
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))] if rows else [len(h) for h in headers]
    print(" | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(row[i].ljust(widths[i]) for i in range(len(headers))))


def evaluate(
    *,
    messages_path: str | Path = DEFAULT_MESSAGES_PATH,
    allocations_path: str | Path = DEFAULT_ALLOCATIONS_PATH,
    output_path: str | Path = DEFAULT_SUMMARY_PATH,
    reference_tier_by_village: Mapping[str, str] | None = None,
    population_by_village: Mapping[str, int] | None = None,
    risk_weight_by_village_month: Mapping[tuple[str, int], float] | None = None,
    risk_input_selector: Callable[[MessageEvent], bool] | None = None,
    alert_correlation_records: Sequence[Mapping[str, Any]] | None = None,
    risk_records: Sequence[Mapping[str, Any]] | None = None,
    allocation_results_verified: bool = False,
    risk_weight_definition: str | None = None,
    negotiation_history_complete: bool = False,
    evaluated_month_resources: Sequence[tuple[int, str]] | None = None,
) -> list[MetricResult]:
    """Evaluate available evidence, write the documented CSV and print a table.

    Optional attestations are explicit integration seams. Missing source files
    yield unavailable metrics; malformed present files raise input errors.
    """
    messages_missing = not Path(messages_path).is_file()
    allocations_missing = not Path(allocations_path).is_file()
    events = [] if messages_missing else read_message_events(messages_path)
    allocation_input_invalid = False
    if allocations_missing:
        allocations = []
    else:
        try:
            allocations = read_allocation_rows(allocations_path)
        except EvaluationInputError:
            allocations = []
            allocation_input_invalid = True
    records = list(risk_records) if risk_records is not None else extract_risk_records(events)

    results = [calculate_risk_alignment(records, reference_tier_by_village)]
    if allocations_missing:
        results.append(_unavailable("allocation_efficiency", "all", "risk-weighted persons", "allocation log is missing"))
    elif allocation_input_invalid:
        reason = "allocation input or supporting values failed validation"
        results.append(MetricResult(
            "allocation_efficiency", "all", None, "risk-weighted persons", "unavailable", 0,
            {"reason": reason, "classification": "implementation",
             "missing_dependency": "valid allocation schema and complete finite supporting values",
             "required_action": "Correct allocation columns/types and provide valid population and risk-weight inputs."},
        ))
    else:
        try:
            results.extend(calculate_allocation_efficiency(
                allocations, population_by_village, risk_weight_by_village_month,
                allocation_results_verified=allocation_results_verified,
                risk_weight_definition=risk_weight_definition,
                evaluated_month_resources=evaluated_month_resources,
            ))
        except EvaluationInputError:
            reason = "allocation input or supporting values failed validation"
            results.append(MetricResult(
                "allocation_efficiency", "all", None, "risk-weighted persons", "unavailable", 0,
                {"reason": reason, "classification": "implementation",
                 "missing_dependency": "valid allocation schema and complete finite supporting values",
                 "required_action": "Correct allocation columns/types and provide valid population and risk-weight inputs."},
            ))
    correlations: dict[str, list[Mapping[str, Any]]] | None = None
    if alert_correlation_records is not None:
        correlations = defaultdict(list)
        for record in alert_correlation_records:
            if record.get("status") != "emitted":
                continue
            alert_thread = record.get("alert_thread_id")
            source_thread = record.get("source_thread_id")
            division, month = record.get("ds_division"), record.get("month_index")
            if (not isinstance(alert_thread, str) or not alert_thread
                    or not isinstance(source_thread, str) or not source_thread
                    or not isinstance(division, str) or not division
                    or isinstance(month, bool) or not isinstance(month, int)):
                continue
            correlations[alert_thread].append({"source_thread_id": source_thread,
                                               "ds_division": division,
                                               "month_index": month})
    results.append(calculate_alert_latency(
        events, risk_input_selector, alert_correlation_by_thread=correlations,
    ))
    results.extend(calculate_negotiation_efficiency(events, history_complete=negotiation_history_complete))
    _write_summary(results, output_path)
    print_summary(results)
    return results


def main() -> None:
    evaluate()


if __name__ == "__main__":
    main()
