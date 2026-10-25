"""Deterministic, pure local-demo allocation comparison.

This is an illustrative Contract Net-style ranking adapter, not Member C's
coordinator or a complete FIPA exchange. Its resource pool contains identical,
indivisible units, with at most one unit per DS division per month. The current
``src.core.bidding`` implementation imports the pandas-backed risk-tier module;
this small adapter repeats only its documented eligibility, ``WQI × population``
bid, and alphabetical tie-break so the engine remains dependency-free. Keep this
behavior aligned with ``src/core/bidding.py`` and review it with its owner.

The engine reports every division and never turns missing or Unknown risk into
safe/ineligible status. File output is a detailed illustrative report under a
configurable demo directory and is intentionally distinct from the evaluator's
team-allocation CSV schema.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.core.ontology import INFORM, RESOURCES, SchemaError, validate

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "demo_output"
PROVENANCE = "illustrative_local_demo"
ELIGIBLE_TIERS = frozenset({"Poor", "Very Poor", "Unsuitable"})
INELIGIBLE_TIERS = frozenset({"Excellent", "Good"})
METHODS = ("local_contract_net_style", "equal_split", "first_come")
DECISION_COLUMNS = (
    "provenance", "month_index", "resource", "resource_pool_size", "ds_division",
    "tier", "population", "method", "status", "units", "bid", "rank", "reason",
)

DecisionStatus = Literal["allocated", "not_allocated", "ineligible", "unavailable"]
ResultStatus = Literal["available", "partial", "unavailable"]


class AllocationInputError(ValueError):
    """Raised when the allocation request itself is malformed or ambiguous."""


@dataclass(frozen=True)
class AllocationDecision:
    provenance: str
    month_index: int
    resource: str
    resource_pool_size: int
    ds_division: str
    tier: str | None
    population: int | None
    method: str
    status: DecisionStatus
    units: int
    bid: float | None
    rank: int | None
    reason: str | None = None

    def as_row(self) -> dict[str, object]:
        return {
            "provenance": self.provenance,
            "month_index": self.month_index,
            "resource": self.resource,
            "resource_pool_size": self.resource_pool_size,
            "ds_division": self.ds_division,
            "tier": "" if self.tier is None else self.tier,
            "population": "" if self.population is None else self.population,
            "method": self.method,
            "status": self.status,
            "units": self.units,
            "bid": "" if self.bid is None else self.bid,
            "rank": "" if self.rank is None else self.rank,
            "reason": "" if self.reason is None else self.reason,
        }


@dataclass(frozen=True)
class LocalAllocationResult:
    status: ResultStatus
    month_index: int
    resource: str
    resource_pool_size: int
    arrival_order: tuple[str, ...]
    arrival_order_source: str
    decisions: tuple[AllocationDecision, ...]


@dataclass(frozen=True)
class _Candidate:
    division: str
    tier: str
    population: int
    bid: float


def allocate_local(
    risk_records: Sequence[Mapping[str, object]] | None,
    population_by_division: Mapping[str, object] | None,
    *,
    month_index: int,
    resource: str,
    resource_pool_size: int,
    arrival_order: Sequence[str] | None = None,
) -> LocalAllocationResult:
    """Allocate one resource pool and produce three comparable illustrative decisions.

    ``risk_records`` must contain at most one ontology-valid ``risk_record`` for
    each division at ``month_index``. Population is supplied separately because
    it is not a field in that ontology body. When ``arrival_order`` is omitted,
    alphabetical division order is the explicit provisional synthetic arrival
    order. The function does not calculate allocation-efficiency metrics.
    """
    _validate_request(month_index, resource, resource_pool_size)
    records = _records_for_month(risk_records, month_index)
    populations = _validate_population_mapping(population_by_division)
    divisions = sorted(set(records) | set(populations))
    if arrival_order is None:
        order = tuple(divisions)
        order_source = "provisional_alphabetical_synthetic_arrival_order"
        arrival_valid = True
    else:
        order_source = "caller_supplied_arrival_order"
        if not isinstance(arrival_order, Sequence) or isinstance(arrival_order, (str, bytes)):
            order = ()
            arrival_valid = False
        else:
            order = tuple(arrival_order)
            arrival_valid = all(isinstance(name, str) and name.strip() for name in order) and (
                len(order) == len(divisions)
                and len(set(order)) == len(order)
                and set(order) == set(divisions)
            )

    state: dict[str, tuple[str, int | None, float | None, str | None]] = {}
    candidates: list[_Candidate] = []
    for division in divisions:
        record = records.get(division)
        if record is None:
            state[division] = ("unavailable", populations.get(division), None, "risk_record_missing")
            continue
        tier = record["tier"]
        wqi = record["wqi"]
        population = populations.get(division)
        if tier == "Unknown":
            state[division] = ("unavailable", population, None, "risk_tier_unknown")
            continue
        if tier not in ELIGIBLE_TIERS | INELIGIBLE_TIERS:
            state[division] = ("unavailable", population, None, f"unsupported_risk_tier:{tier}")
            continue
        if wqi is None:
            state[division] = ("unavailable", population, None, "wqi_missing")
            continue
        if isinstance(wqi, bool) or not isinstance(wqi, (int, float)) or not math.isfinite(wqi) or wqi < 0:
            state[division] = ("unavailable", population, None, "wqi_invalid")
            continue
        if population is None:
            state[division] = ("unavailable", None, None, "population_missing")
            continue
        if isinstance(population, bool) or not isinstance(population, int) or population < 0:
            state[division] = ("unavailable", None, None, "population_invalid")
            continue
        if tier in INELIGIBLE_TIERS:
            state[division] = ("ineligible", population, None, "tier_does_not_meet_existing_need_rule")
            continue
        if population == 0:
            state[division] = ("ineligible", population, 0.0, "zero_population")
            continue
        bid = float(wqi) * population
        candidates.append(_Candidate(division, tier, population, bid))
        state[division] = ("candidate", population, bid, None)

    method_winners: dict[str, set[str]] = {}
    method_ranks: dict[str, dict[str, int]] = {}

    cnet_ranked = sorted(candidates, key=lambda candidate: (-candidate.bid, candidate.division))
    method_ranks["local_contract_net_style"] = {
        candidate.division: rank for rank, candidate in enumerate(cnet_ranked, start=1)
    }
    method_winners["local_contract_net_style"] = {
        candidate.division for candidate in cnet_ranked[:resource_pool_size]
    }

    equal_ranked = sorted(candidates, key=lambda candidate: candidate.division)
    method_ranks["equal_split"] = {
        candidate.division: rank for rank, candidate in enumerate(equal_ranked, start=1)
    }
    equal_winners: set[str] = set()
    if equal_ranked and resource_pool_size:
        quotient, remainder = divmod(resource_pool_size, len(equal_ranked))
        for rank, candidate in enumerate(equal_ranked):
            share = min(1, quotient + (1 if rank < remainder else 0))
            if share:
                equal_winners.add(candidate.division)
    method_winners["equal_split"] = equal_winners

    if arrival_valid:
        candidate_by_division = {candidate.division: candidate for candidate in candidates}
        first_ranked = [candidate_by_division[division] for division in order
                        if division in candidate_by_division]
        method_ranks["first_come"] = {
            candidate.division: rank for rank, candidate in enumerate(first_ranked, start=1)
        }
        method_winners["first_come"] = {
            candidate.division for candidate in first_ranked[:resource_pool_size]
        }
    else:
        method_ranks["first_come"] = {}
        method_winners["first_come"] = set()

    decisions: list[AllocationDecision] = []
    for method in METHODS:
        for division in divisions:
            base_status, population, bid, reason = state[division]
            rank = method_ranks[method].get(division)
            if base_status == "unavailable":
                status: DecisionStatus = "unavailable"
                units = 0
            elif base_status == "ineligible":
                status = "ineligible"
                units = 0
            elif method == "first_come" and not arrival_valid:
                status = "unavailable"
                units = 0
                reason = "arrival_order_must_list_each_division_exactly_once"
            elif division in method_winners[method]:
                status = "allocated"
                units = 1
            else:
                status = "not_allocated"
                units = 0
                reason = "resource_pool_exhausted" if resource_pool_size else "resource_pool_empty"
            record = records.get(division)
            decisions.append(AllocationDecision(
                PROVENANCE, month_index, resource, resource_pool_size, division,
                None if record is None else str(record["tier"]), population, method,
                status, units, bid, rank, reason,
            ))

    if not decisions or all(decision.status == "unavailable" for decision in decisions):
        result_status: ResultStatus = "unavailable"
    elif any(decision.status == "unavailable" for decision in decisions):
        result_status = "partial"
    else:
        result_status = "available"
    return LocalAllocationResult(
        result_status, month_index, resource, resource_pool_size, order, order_source,
        tuple(decisions),
    )


def write_allocation_report(
    result: LocalAllocationResult,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    """Write the detailed demo-only report; refuse production data/log directories."""
    directory = Path(output_dir).expanduser().resolve()
    _reject_production_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "allocation_decisions.csv"
    _reject_production_path(path.resolve())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DECISION_COLUMNS)
        writer.writeheader()
        writer.writerows(decision.as_row() for decision in result.decisions)
    return path


def _validate_request(month_index: int, resource: str, pool_size: int) -> None:
    if isinstance(month_index, bool) or not isinstance(month_index, int) or month_index < 0:
        raise AllocationInputError("month_index must be a non-negative integer")
    if not isinstance(resource, str) or resource not in RESOURCES:
        raise AllocationInputError(f"resource must be one of {sorted(RESOURCES)}")
    if isinstance(pool_size, bool) or not isinstance(pool_size, int) or pool_size < 0:
        raise AllocationInputError("resource_pool_size must be a non-negative integer")


def _records_for_month(
    risk_records: Sequence[Mapping[str, object]] | None,
    month_index: int,
) -> dict[str, Mapping[str, object]]:
    if risk_records is None:
        return {}
    if not isinstance(risk_records, Sequence) or isinstance(risk_records, (str, bytes)):
        raise AllocationInputError("risk_records must be a sequence of typed risk_record bodies")
    found: dict[str, Mapping[str, object]] = {}
    for offset, source in enumerate(risk_records):
        if not isinstance(source, Mapping):
            raise AllocationInputError(f"risk_records[{offset}] must be a mapping")
        record = dict(source)
        try:
            validate(INFORM, record)
        except SchemaError as exc:
            raise AllocationInputError(f"risk_records[{offset}] violates the ontology: {exc}") from exc
        if record["month_index"] != month_index:
            continue
        division = record["ds_division"]
        if not division.strip():
            raise AllocationInputError(f"risk_records[{offset}] has an empty ds_division")
        if division in found:
            raise AllocationInputError(
                f"duplicate risk record for division/month_index {(division, month_index)!r}"
            )
        found[division] = record
    return found


def _validate_population_mapping(
    population_by_division: Mapping[str, object] | None,
) -> dict[str, object]:
    if population_by_division is None:
        return {}
    if not isinstance(population_by_division, Mapping):
        raise AllocationInputError("population_by_division must be a mapping")
    for division in population_by_division:
        if not isinstance(division, str) or not division.strip():
            raise AllocationInputError("population mapping keys must be non-empty division names")
    return dict(population_by_division)


def _reject_production_path(path: Path) -> None:
    protected = (
        PROJECT_ROOT / "logs",
        PROJECT_ROOT / "data" / "raw",
        PROJECT_ROOT / "data" / "processed",
    )
    for root in protected:
        try:
            path.relative_to(root.resolve())
        except ValueError:
            continue
        raise ValueError(f"refusing to write demo allocation output inside protected path {root}")
