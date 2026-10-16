"""Evaluation calculations for CKDu MAS run logs.

This module reports indicator alignment, allocation comparisons, alert latency,
and Contract Net activity. It does not make causal or medical claims. Inputs
that need an unagreed cross-member mapping (reference classes, numeric risk
weights, or risk-input log events) are explicit function arguments; absent
interfaces produce an unavailable result, never an invented zero.

The allocation metric follows the project brief's shared definition:
``sum(risk_weight * population)`` over villages served for the same month and
resource. The numeric risk weight must be supplied by the caller because the
published B risk record has WQI/tier but no agreed scalar called ``risk``.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from src.core.msg_log import CSV_COLUMNS
from src.core.ontology import MESSAGE_PERFORMATIVES, PERFORMATIVES, validate, validate_message


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MESSAGES_PATH = ROOT / "logs" / "messages.csv"
DEFAULT_ALLOCATIONS_PATH = ROOT / "logs" / "allocations.csv"
DEFAULT_SUMMARY_PATH = ROOT / "logs" / "evaluation_summary.csv"
ALLOCATION_COLUMNS = (
    "month", "resource", "ds_division", "round", "winning_bid", "method"
)
ALLOCATION_METHODS = ("contract_net", "equal_split", "first_come")
RISK_RECORD_FIELDS = frozenset(
    {"ds_division", "month", "wqi", "tier", "drivers", "n_wells", "source"}
)


class EvaluationInputError(ValueError):
    """Raised when an input file contains malformed or incomplete records."""


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
    """Read and validate every row; malformed rows fail loudly, never drop."""
    rows = _read_exact_csv(path, CSV_COLUMNS)
    events: list[MessageEvent] = []
    for index, row in enumerate(rows, start=2):
        context = f"{path}:{index}"
        for field_name in ("sender", "receiver", "thread_id"):
            if not row[field_name]:
                raise EvaluationInputError(f"{context}: {field_name} is missing")
        performative = row["performative"]
        if performative and performative not in PERFORMATIVES:
            raise EvaluationInputError(f"{context}: unknown performative {performative!r}")
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise EvaluationInputError(f"{context}: payload is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise EvaluationInputError(f"{context}: payload must be a JSON object")
        event_type = payload.get("event_type")
        if event_type == "alert_issued":
            if performative != MESSAGE_PERFORMATIVES["alert"]:
                raise EvaluationInputError(f"{context}: alert_issued must use the mapped inform performative")
            try:
                validate_message("alert", payload)
            except ValueError as exc:
                raise EvaluationInputError(f"{context}: invalid alert_issued payload: {exc}") from exc
        elif event_type == "alert_suppressed":
            if performative or row["sender"] != row["receiver"]:
                raise EvaluationInputError(f"{context}: suppression audit rows must be self-addressed with empty performative")
            try:
                validate_message("alert_suppression", payload)
            except ValueError as exc:
                raise EvaluationInputError(f"{context}: invalid alert_suppressed payload: {exc}") from exc
        elif not performative:
            raise EvaluationInputError(f"{context}: empty performative is reserved for alert_suppressed audit rows")
        events.append(
            MessageEvent(
                _parse_timestamp(row["timestamp"], context),
                row["sender"],
                row["receiver"],
                performative,
                row["thread_id"],
                payload,
            )
        )
    return events


def read_allocation_rows(path: str | Path) -> list[AllocationRow]:
    """Read Member C's documented allocation CSV, rejecting malformed rows."""
    rows = _read_exact_csv(path, ALLOCATION_COLUMNS)
    parsed: list[AllocationRow] = []
    for index, row in enumerate(rows, start=2):
        context = f"{path}:{index}"
        try:
            month = int(row["month"])
            round_number = int(row["round"])
            winning_bid = float(row["winning_bid"])
        except (TypeError, ValueError) as exc:
            raise EvaluationInputError(f"{context}: month, round and winning_bid must be numeric") from exc
        if month < 1 or round_number < 1 or not math.isfinite(winning_bid):
            raise EvaluationInputError(f"{context}: invalid month, round, or winning_bid")
        if row["resource"] not in {"ro_unit", "bowser"}:
            raise EvaluationInputError(f"{context}: unsupported resource {row['resource']!r}")
        if row["method"] not in ALLOCATION_METHODS:
            raise EvaluationInputError(f"{context}: unsupported method {row['method']!r}")
        if not row["ds_division"]:
            raise EvaluationInputError(f"{context}: ds_division is missing")
        parsed.append(
            AllocationRow(month, row["resource"], row["ds_division"], round_number, winning_bid, row["method"])
        )
    return parsed


def extract_risk_records(events: Iterable[MessageEvent]) -> list[dict[str, Any]]:
    """Extract B-shaped records from message payloads and deduplicate log ends.

    Identical send/receive log entries collapse to one village/month record.
    Conflicting records for that key are rejected because choosing one would
    silently alter the alignment denominator.
    """
    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for event in events:
        if not RISK_RECORD_FIELDS.issubset(event.payload):
            continue
        record = dict(event.payload)
        try:
            validate_message("risk_record", record)
        except ValueError as exc:
            raise EvaluationInputError(f"Invalid risk-record event: {exc}") from exc
        key = (record["ds_division"], record["month"])
        if key in unique and unique[key] != record:
            raise EvaluationInputError(f"conflicting risk records for division/month {key!r}")
        unique[key] = record
    return list(unique.values())


def calculate_risk_alignment(
    risk_records: Sequence[Mapping[str, Any]],
    reference_tier_by_village: Mapping[str, str] | None,
    *,
    reference_mapping_source: str = "caller-supplied mapping; provenance not verified",
) -> MetricResult:
    """Exact tier agreement against a caller-supplied reference mapping.

    ``reference_tier_by_village`` must be agreed from the published
    ``ckdu_03`` prevalence classes and relevant ``ckdu_04`` aggregate patterns.
    This function does not invent a mapping or treat agreement as causal
    validation. Unmatched divisions are reported as partial coverage.
    """
    metric = "risk_classification_alignment"
    if not risk_records:
        return _unavailable(metric, "all", "proportion", "no risk records")
    if not reference_tier_by_village:
        return _unavailable(metric, "all", "proportion", "reference tier mapping is not supplied")

    matched = 0
    aligned = 0
    unmatched: list[str] = []
    for record in risk_records:
        try:
            validate_message("risk_record", record)
        except ValueError as exc:
            raise EvaluationInputError(f"Invalid risk record: {exc}") from exc
        division = record["ds_division"]
        expected = reference_tier_by_village.get(division)
        if expected is None:
            unmatched.append(division)
            continue
        if expected not in {"Excellent", "Good", "Poor"}:
            raise EvaluationInputError(f"invalid reference tier for {division!r}: {expected!r}")
        matched += 1
        aligned += record["tier"] == expected
    if matched == 0:
        return MetricResult(
            metric, "all", None, "proportion", "unavailable", 0,
            {"reason": "no risk records matched reference divisions", "unmatched_divisions": sorted(set(unmatched))},
        )
    return MetricResult(
        metric,
        "all",
        aligned / matched,
        "proportion",
        "partial" if unmatched else "ok",
        matched,
        {"aligned": aligned, "unmatched_records": len(unmatched), "unmatched_divisions": sorted(set(unmatched)),
         "interpretation": "alignment check only; not causal validation",
         "reference_mapping_source": reference_mapping_source},
    )


def calculate_allocation_efficiency(
    allocation_rows: Sequence[AllocationRow],
    population_by_village: Mapping[str, int] | None,
    risk_weight_by_village_month: Mapping[tuple[str, int], float] | None,
) -> list[MetricResult]:
    """Compare three methods on risk-weighted population served.

    For each month/resource/method, each served division is counted once and
    scored as ``risk_weight_by_village_month[(division, month)] * population``.
    The caller supplies both inputs; this code does not equate WQI, tier, or bid
    with numeric risk. A missing baseline yields unavailable comparison rows,
    not a zero score.
    """
    metric = "allocation_efficiency"
    if not allocation_rows:
        return [_unavailable(metric, "all", "risk-weighted persons", "no allocation rows")]
    if population_by_village is None or risk_weight_by_village_month is None:
        return [_unavailable(metric, "all", "risk-weighted persons", "population and numeric risk-weight mappings are required")]

    groups: dict[tuple[int, str, str], set[str]] = defaultdict(set)
    for row in allocation_rows:
        groups[(row.month, row.resource, row.method)].add(row.ds_division)
    month_resources = sorted({(month, resource) for month, resource, _ in groups})
    results: list[MetricResult] = []
    for month, resource in month_resources:
        scores: dict[str, float | None] = {}
        served_counts: dict[str, int] = {}
        for method in ALLOCATION_METHODS:
            villages = groups.get((month, resource, method))
            if not villages:
                scores[method] = None
                served_counts[method] = 0
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
            served_counts[method] = len(villages)

        scope = f"month={month};resource={resource}"
        for method in ALLOCATION_METHODS:
            value = scores[method]
            results.append(
                MetricResult(
                    "risk_weighted_population_served",
                    f"{scope};method={method}",
                    value,
                    "risk-weighted persons",
                    "ok" if value is not None else "unavailable",
                    served_counts[method],
                    {} if value is not None else {"reason": "method has no allocation rows"},
                )
            )
        for baseline in ("equal_split", "first_come"):
            contract_value, baseline_value = scores["contract_net"], scores[baseline]
            available = contract_value is not None and baseline_value is not None
            results.append(
                MetricResult(
                    f"contract_net_minus_{baseline}",
                    scope,
                    contract_value - baseline_value if available else None,
                    "risk-weighted persons",
                    "ok" if available else "unavailable",
                    1 if available else 0,
                    {} if available else {"reason": f"missing {('contract_net' if contract_value is None else baseline)} baseline rows"},
                )
            )
    return results


def calculate_alert_latency(
    events: Sequence[MessageEvent],
    risk_input_selector: Callable[[MessageEvent], bool] | None,
) -> MetricResult:
    """Mean seconds from selected risk-input events to issued alerts.

    Pairing uses ``(ds_division, month)`` and the earliest selected input event
    for that key. Only ``event_type=alert_issued`` counts as an alert; internal
    ``alert_suppressed`` events are excluded. The selector is required because
    the current shared schema does not define a consistently identifiable
    risk-input event marker or sent/received direction.
    """
    metric = "alert_latency"
    if not events:
        return _unavailable(metric, "all", "seconds", "no message events")
    if risk_input_selector is None:
        return _unavailable(metric, "all", "seconds", "risk-input event selector is not supplied")

    risk_inputs: dict[tuple[str, int], datetime] = {}
    issued: list[MessageEvent] = []
    selected_inputs: list[MessageEvent] = []
    for event in events:
        if risk_input_selector(event):
            selected_inputs.append(event)
            division, month = event.payload.get("ds_division"), event.payload.get("month")
            if not isinstance(division, str) or isinstance(month, bool) or not isinstance(month, int):
                raise EvaluationInputError("selected risk-input event requires ds_division and integer month")
            key = (division, month)
            risk_inputs[key] = min(risk_inputs.get(key, event.timestamp), event.timestamp)
        if event.payload.get("event_type") == "alert_issued":
            issued.append(event)

    if not issued:
        return _unavailable(metric, "all", "seconds", "no alert_issued events")
    # The fixed message CSV has no sent/received column. Duplicate rows with
    # the same endpoint, thread and payload can therefore be a sender/receiver
    # pair or two identical sends. Refuse to manufacture a latency from them.
    for collection_name, collection in (("risk input", selected_inputs), ("alert", issued)):
        signatures = [
            (event.sender, event.receiver, event.performative, event.thread_id,
             json.dumps(event.payload, sort_keys=True, separators=(",", ":")))
            for event in collection
        ]
        if len(signatures) != len(set(signatures)):
            return _unavailable(
                metric,
                "all",
                "seconds",
                f"duplicate {collection_name} rows have ambiguous sent/received direction",
            )
    latencies: list[float] = []
    unmatched: list[str] = []
    for event in issued:
        key = (event.payload["ds_division"], event.payload["month"])
        start = risk_inputs.get(key)
        if start is None:
            unmatched.append(f"{key[0]}:{key[1]}")
            continue
        elapsed = (event.timestamp - start).total_seconds()
        if elapsed < 0:
            raise EvaluationInputError(f"alert for {key!r} predates its selected risk input")
        latencies.append(elapsed)
    if not latencies:
        return MetricResult(
            metric, "all", None, "seconds", "unavailable", 0,
            {"reason": "no alert-issued events matched a risk input", "unmatched_alerts": len(unmatched),
             "unmatched_keys": unmatched},
        )
    return MetricResult(
        metric, "all", sum(latencies) / len(latencies), "seconds",
        "partial" if unmatched else "ok", len(latencies),
        {"unmatched_alerts": len(unmatched), "unmatched_keys": unmatched,
         "suppression_events_counted": False, "pairing": "earliest risk input per division/month",
         "statistic": "arithmetic mean of matched alert latencies"},
    )


def calculate_negotiation_efficiency(events: Sequence[MessageEvent]) -> list[MetricResult]:
    """Count CFP rounds and logged event rows per negotiation month.

    Duplicate CFP rows (for example sent and received log entries) count as one
    round via unique ``(thread_id, round)``; message counts are explicitly row
    counts because the six-column logger has no sent/received direction field.
    """
    metric_rounds = "negotiation_rounds"
    metric_messages = "negotiation_logged_event_rows"
    if not events:
        return [
            _unavailable(metric_rounds, "all", "rounds", "no message events"),
            _unavailable(metric_messages, "all", "logged event rows", "no message events"),
        ]
    cfps = [event for event in events if event.performative == "cfp"]
    if not cfps:
        return [
            _unavailable(metric_rounds, "all", "rounds", "no cfp events"),
            _unavailable(metric_messages, "all", "logged event rows", "no cfp events"),
        ]

    thread_month: dict[str, int] = {}
    rounds_by_month: dict[int, set[tuple[str, int]]] = defaultdict(set)
    for event in cfps:
        try:
            validate("cfp", event.payload)
        except ValueError as exc:
            raise EvaluationInputError(f"invalid CFP payload: {exc}") from exc
        month, round_number = event.payload["month"], event.payload["round"]
        if event.thread_id in thread_month and thread_month[event.thread_id] != month:
            raise EvaluationInputError(f"negotiation thread {event.thread_id!r} spans multiple months")
        thread_month[event.thread_id] = month
        rounds_by_month[month].add((event.thread_id, round_number))

    rows_by_month: dict[int, int] = defaultdict(int)
    unassociated = 0
    for event in events:
        month = thread_month.get(event.thread_id)
        if month is None:
            unassociated += 1
        else:
            rows_by_month[month] += 1

    results: list[MetricResult] = []
    months = sorted(rounds_by_month)
    for month in months:
        results.append(
            MetricResult(
                metric_rounds, f"month={month}", len(rounds_by_month[month]), "rounds", "ok",
                len(rounds_by_month[month]), {"definition": "unique cfp thread_id/round pairs"},
            )
        )
        results.append(
            MetricResult(
                metric_messages, f"month={month}", rows_by_month[month], "logged event rows", "ok",
                rows_by_month[month], {"unassociated_rows": unassociated,
                                       "counting_note": "sent and received rows cannot be distinguished by the current logger"},
            )
        )
    return results


def extract_logged_risk_records(events: Sequence[MessageEvent]) -> list[dict[str, Any]]:
    """Public wrapper for extracting deduplicated risk records from history."""
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
    """Print a compact human-readable table without converting missing to 0."""
    headers = ("METRIC", "SCOPE", "VALUE", "UNIT", "STATUS", "N")
    rows = [
        (r.metric, r.scope, "—" if r.value is None else f"{r.value:.4g}" if isinstance(r.value, float) else str(r.value),
         r.unit, r.status, str(r.observations))
        for r in results
    ]
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
) -> list[MetricResult]:
    """Run available metrics, write summary CSV, and print a table.

    Missing source files make dependent metrics unavailable. Present but
    malformed files raise :class:`EvaluationInputError`; they are never
    silently skipped. Explicit maps/selectors are integration seams for
    unresolved team definitions.
    """
    messages_missing = not Path(messages_path).is_file()
    allocations_missing = not Path(allocations_path).is_file()
    events = [] if messages_missing else read_message_events(messages_path)
    allocations = [] if allocations_missing else read_allocation_rows(allocations_path)
    records = list(risk_records) if risk_records is not None else extract_risk_records(events)

    results: list[MetricResult] = []
    if messages_missing and risk_records is None:
        results.append(_unavailable("risk_classification_alignment", "all", "proportion", "message log is missing"))
    else:
        results.append(calculate_risk_alignment(records, reference_tier_by_village))

    if allocations_missing:
        results.append(_unavailable("allocation_efficiency", "all", "risk-weighted persons", "allocation log is missing"))
    else:
        try:
            results.extend(calculate_allocation_efficiency(
                allocations, population_by_village, risk_weight_by_village_month
            ))
        except EvaluationInputError as exc:
            results.append(_unavailable("allocation_efficiency", "all", "risk-weighted persons", str(exc)))

    if messages_missing:
        results.append(_unavailable("alert_latency", "all", "seconds", "message log is missing"))
        results.extend(calculate_negotiation_efficiency([]))
    else:
        results.append(calculate_alert_latency(events, risk_input_selector))
        results.extend(calculate_negotiation_efficiency(events))

    _write_summary(results, output_path)
    print_summary(results)
    return results


def main() -> None:
    evaluate()


if __name__ == "__main__":
    main()
