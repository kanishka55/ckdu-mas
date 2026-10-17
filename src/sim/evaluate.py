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


def _unavailable(metric: str, scope: str, unit: str, reason: str) -> MetricResult:
    return MetricResult(metric, scope, None, unit, "unavailable", 0, {"reason": reason})


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
        return MetricResult(metric, "all", None, "proportion", "unavailable", 0,
                            {"reason": "no risk records matched reference divisions",
                             "unmatched_divisions": sorted(set(unmatched))})
    return MetricResult(
        metric, "all", aligned / matched, "proportion", "partial" if unmatched else "ok", matched,
        {"aligned": aligned, "unmatched_records": len(unmatched),
         "unmatched_divisions": sorted(set(unmatched)),
         "interpretation": "alignment check only; not scientific validation or evidence of causation",
         "reference_mapping_source": reference_mapping_source},
    )


def calculate_allocation_efficiency(
    allocation_rows: Sequence[AllocationRow],
    population_by_village: Mapping[str, int] | None,
    risk_weight_by_village_month: Mapping[tuple[str, int], float] | None,
    *,
    allocation_results_verified: bool = False,
    risk_weight_definition: str | None = None,
) -> list[MetricResult]:
    """Calculate documented risk-weighted population only with explicit evidence.

    ``allocation_results_verified`` is a caller attestation that rows are actual
    team outputs, not fixtures. ``risk_weight_definition`` records the agreed
    scalar-risk definition. Both are mandatory; maps alone do not establish
    provenance or scientific meaning.
    """
    metric = "allocation_efficiency"
    reason = None
    if not allocation_rows:
        reason = "no allocation rows"
    elif not allocation_results_verified:
        reason = "allocation rows are not attested as actual team results"
    elif not risk_weight_definition:
        reason = "agreed numeric risk-weight definition is not supplied"
    elif population_by_village is None or risk_weight_by_village_month is None:
        reason = "population and numeric risk-weight mappings are required"
    if reason:
        return [_unavailable(metric, "all", "risk-weighted persons", reason)]

    groups: dict[tuple[int, str, str], set[str]] = defaultdict(set)
    for row in allocation_rows:
        groups[(row.month, row.resource, row.method)].add(row.ds_division)
    month_resources = sorted({(month, resource) for month, resource, _ in groups})
    results: list[MetricResult] = []
    for month, resource in month_resources:
        scope = f"month={month};resource={resource}"
        absent_methods = [method for method in ALLOCATION_METHODS
                          if not groups.get((month, resource, method))]
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
                scores[method] = None
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
    return results


def calculate_alert_latency(
    events: Sequence[MessageEvent],
    risk_input_selector: Callable[[MessageEvent], bool] | None,
) -> MetricResult:
    """Mean logged seconds for uniquely correlated risk receipt/alert issue pairs.

    Correlation requires matching ``thread_id``, ``ds_division`` and
    ``month_index``. The input must be a received typed risk_record and the
    endpoint a sent typed alert. No proximity or collection-time inference is
    used. Suppression/unavailable decisions are not ontology messages and are
    never counted.
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
        input_events = inputs.get(key, [])
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
                            {"reason": reason, "unmatched_alerts": unmatched,
                             "ambiguous_pairs": ambiguous, "suppression_events_counted": False})
    return MetricResult(
        metric, "all", sum(latencies) / len(latencies), "seconds",
        "partial" if unmatched or ambiguous else "ok", len(latencies),
        {"unmatched_alerts": unmatched, "ambiguous_pairs": ambiguous,
         "suppression_events_counted": False,
         "pairing": "exact thread_id, ds_division and month_index; received risk_record to sent alert",
         "timestamp_basis": "message logger UTC append timestamps"},
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
    risk_records: Sequence[Mapping[str, Any]] | None = None,
    allocation_results_verified: bool = False,
    risk_weight_definition: str | None = None,
    negotiation_history_complete: bool = False,
) -> list[MetricResult]:
    """Evaluate available evidence, write the documented CSV and print a table.

    Optional attestations are explicit integration seams. Missing source files
    yield unavailable metrics; malformed present files raise input errors.
    """
    messages_missing = not Path(messages_path).is_file()
    allocations_missing = not Path(allocations_path).is_file()
    events = [] if messages_missing else read_message_events(messages_path)
    allocations = [] if allocations_missing else read_allocation_rows(allocations_path)
    records = list(risk_records) if risk_records is not None else extract_risk_records(events)

    results = [calculate_risk_alignment(records, reference_tier_by_village)]
    if allocations_missing:
        results.append(_unavailable("allocation_efficiency", "all", "risk-weighted persons", "allocation log is missing"))
    else:
        try:
            results.extend(calculate_allocation_efficiency(
                allocations, population_by_village, risk_weight_by_village_month,
                allocation_results_verified=allocation_results_verified,
                risk_weight_definition=risk_weight_definition,
            ))
        except EvaluationInputError as exc:
            results.append(_unavailable("allocation_efficiency", "all", "risk-weighted persons", str(exc)))
    results.append(calculate_alert_latency(events, risk_input_selector))
    results.extend(calculate_negotiation_efficiency(events, history_complete=negotiation_history_complete))
    _write_summary(results, output_path)
    print_summary(results)
    return results


def main() -> None:
    evaluate()


if __name__ == "__main__":
    main()
