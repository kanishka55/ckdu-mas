"""Pure configuration, validation, selection, and serialization for one Contract Net round.

This module deliberately has no SPADE dependency. The coordinator agent owns transport and
timeouts; this module defines the round identity and deterministic proposal ranking.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.core import bidding, ontology


@dataclass(frozen=True)
class VillageRecipient:
    jid: str
    ds_division: str


@dataclass(frozen=True)
class CoordinatorConfig:
    month_index: int
    round_number: int
    resource: str
    resource_pool: int
    recipients: Sequence[VillageRecipient]
    proposal_timeout_s: float
    confirmation_timeout_s: float
    output_path: Path
    thread_id: str | None = None
    provenance: str = "SPADE Contract Net round; input provenance supplied by caller"

    def __post_init__(self) -> None:
        for field_name, value in (("month_index", self.month_index),
                                  ("round_number", self.round_number),
                                  ("resource_pool", self.resource_pool)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.resource not in ontology.RESOURCES:
            raise ValueError(f"resource must be one of {sorted(ontology.RESOURCES)}")
        for field_name, value in (("proposal_timeout_s", self.proposal_timeout_s),
                                  ("confirmation_timeout_s", self.confirmation_timeout_s)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field_name} must be a finite positive number")
        if not self.recipients:
            raise ValueError("at least one village recipient is required")
        jids = [r.jid for r in self.recipients]
        divisions = [r.ds_division for r in self.recipients]
        if any(not isinstance(x, str) or not x.strip() for x in (*jids, *divisions)):
            raise ValueError("recipient JIDs and divisions must be non-empty strings")
        if len(set(jids)) != len(jids) or len(set(divisions)) != len(divisions):
            raise ValueError("recipient JIDs and divisions must be unique")
        if self.thread_id is not None and (not isinstance(self.thread_id, str) or not self.thread_id):
            raise ValueError("thread_id must be a non-empty string when supplied")
        if not isinstance(self.output_path, Path):
            object.__setattr__(self, "output_path", Path(self.output_path))
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise ValueError("provenance must be a non-empty string")

    @property
    def correlation_id(self) -> str:
        return self.thread_id or f"contract-net-{uuid.uuid4().hex}"

    def cfp_body(self) -> dict[str, Any]:
        body = {
            "type": "cfp", "month_index": self.month_index, "round": self.round_number,
            "resource": self.resource, "units_available": self.resource_pool,
            "deadline_s": float(self.proposal_timeout_s),
        }
        ontology.validate(ontology.CFP, body)
        return body


def valid_proposal(body: Mapping[str, Any], config: CoordinatorConfig,
                   expected_division: str) -> tuple[bool, str | None]:
    """Return whether an ontology-valid proposal is current and eligible to rank."""
    try:
        ontology.validate(ontology.PROPOSE, dict(body))
    except (ontology.SchemaError, TypeError, ValueError) as exc:
        return False, f"invalid proposal schema: {exc}"
    if body["month_index"] != config.month_index or body["round"] != config.round_number:
        return False, "proposal month_index or round does not match this CFP"
    if body["ds_division"] != expected_division:
        return False, "proposal division does not match recipient configuration"
    tier = body["risk_tier"]
    if tier == "Unknown" or not bidding.has_need(tier):
        return False, f"risk tier {tier!r} is not eligible for a risk-based award"
    bid = body["bid"]
    if isinstance(bid, bool) or not math.isfinite(float(bid)) or bid <= 0:
        return False, "proposal bid must be finite and greater than zero"
    population = body["population"]
    if isinstance(population, bool) or population <= 0:
        return False, "proposal population must be greater than zero"
    return True, None


def select_awardees(proposals: Mapping[str, Mapping[str, Any]], pool: int) -> list[tuple[str, float]]:
    """Rank raw proposal bids using the shared normalization and alphabetical tie-break.

    The provisional unit policy is at most one resource unit per village per round.
    """
    if not isinstance(pool, int) or isinstance(pool, bool) or pool < 0:
        raise ValueError("pool must be a non-negative integer")
    raw = {division: float(body["bid"]) for division, body in proposals.items()}
    normalized = bidding.normalise_bids(raw)
    ranked = bidding.rank_bids(normalized)
    return ranked[:pool]


def write_round_result(path: Path, result: Mapping[str, Any]) -> None:
    """Write one JSON round result without replacing an existing artifact."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


