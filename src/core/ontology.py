"""Message ontology: the single source of truth for every message body exchanged between agents.

Every message body is a JSON object with a ``type`` field naming one of the schemas below.
Each type is only legal under specific FIPA performatives. Agents call ``encode`` before sending
and ``decode`` after receiving; both validate, so a schema mismatch fails at the sender instead of
silently corrupting a negotiation in week 9.

Pure module: no SPADE imports.

Wording rule for every human-readable string carried in a body (``reason``, ``message``): this is a
risk-indicator tool built on published water-quality research. It never diagnoses CKDu and never
claims water chemistry causes CKDu.

Schemas
-------
month_tick (inform)              Clock -> all
    month_index:int   simulated month counter, 0-based
    month:int         calendar month 1-12
    season:str        "Wet" | "Dry" (see src/core/wells.py MONTH_TO_SEASON)

well_readings (inform)           WellMonitor -> RiskScoring
    month_index:int, ds_division:str, source:str ("real" | "synthetic")
    readings:list     list of dicts, one per well, keys = wells.READING_COLUMNS

risk_record (inform)             RiskScoring -> Coordinator, Alert, Hotspot
    month_index:int, ds_division:str
    wqi:number|null   null when the WQI cannot be computed (missing weights or parameters)
    tier:str          risk tier label from the published WQI classes
    drivers:list      parameter names that contributed most to the score

well_risk (inform)               RiskScoring -> Hotspot  (per-well detail behind one risk_record)
    month_index:int, month:int (calendar 1-12), ds_division:str, district:str
    ckdu_prevalence_class:str  Cooray et al.'s class for the division (for the alignment check)
    source:str        "real" | "synthetic"
    wells:list        one dict per scored well: well_id:str, wqi:number|null, tier:str,
                      q:{parameter: qi} (quality rating per WQI parameter present, see
                      src/core/wqi.py). Wells rejected for unusable values are not included.
    Added by Member B (Function 2) for the HotspotAgent; additive, no existing type changed.

cfp (cfp)                        Coordinator -> Villages
    month_index:int, round:int
    resource:str      "RO_unit" | "bowser"
    units_available:int
    deadline_s:number seconds the coordinator will wait for proposals

propose (propose)                Village -> Coordinator
    month_index:int, round:int, ds_division:str
    bid:number        raw bid from src/core/bidding.bid; the coordinator normalises across villages
    risk_tier:str, population:int

refuse (refuse)                  Village -> Coordinator
    month_index:int, round:int, ds_division:str, reason:str

accept_proposal (accept-proposal)  Coordinator -> Village
    month_index:int, round:int, resource:str, units:int

reject_proposal (reject-proposal)  Coordinator -> Village
    month_index:int, round:int, reason:str

inform_done (inform)             Village -> Coordinator, after an accepted proposal
    month_index:int, round:int, ds_division:str, resource:str, units:int

failure (failure)                Village -> Coordinator, could not take the allocation
    month_index:int, round:int, ds_division:str, reason:str

alert (inform)                   Alert -> village health officer
    month_index:int, ds_division:str, tier:str, hotspot:bool, message:str
"""
from __future__ import annotations

import json
from typing import Any

# FIPA performatives used in this system
CFP = "cfp"
PROPOSE = "propose"
REFUSE = "refuse"
ACCEPT_PROPOSAL = "accept-proposal"
REJECT_PROPOSAL = "reject-proposal"
INFORM = "inform"
FAILURE = "failure"

PERFORMATIVES = frozenset(
    {CFP, PROPOSE, REFUSE, ACCEPT_PROPOSAL, REJECT_PROPOSAL, INFORM, FAILURE}
)

RESOURCES = frozenset({"RO_unit", "bowser"})
SEASONS = frozenset({"Wet", "Dry"})
SOURCES = frozenset({"real", "synthetic"})

_NUM = (int, float)

# type -> (allowed performatives, {field: accepted python types})
SCHEMAS: dict[str, tuple[frozenset[str], dict[str, tuple[type, ...]]]] = {
    "month_tick": (
        frozenset({INFORM}),
        {"month_index": (int,), "month": (int,), "season": (str,)},
    ),
    "well_readings": (
        frozenset({INFORM}),
        {"month_index": (int,), "ds_division": (str,), "source": (str,), "readings": (list,)},
    ),
    "risk_record": (
        frozenset({INFORM}),
        {
            "month_index": (int,),
            "ds_division": (str,),
            "wqi": _NUM + (type(None),),
            "tier": (str,),
            "drivers": (list,),
        },
    ),
    "well_risk": (
        frozenset({INFORM}),
        {
            "month_index": (int,),
            "month": (int,),
            "ds_division": (str,),
            "district": (str,),
            "ckdu_prevalence_class": (str,),
            "source": (str,),
            "wells": (list,),
        },
    ),
    "cfp": (
        frozenset({CFP}),
        {
            "month_index": (int,),
            "round": (int,),
            "resource": (str,),
            "units_available": (int,),
            "deadline_s": _NUM,
        },
    ),
    "propose": (
        frozenset({PROPOSE}),
        {
            "month_index": (int,),
            "round": (int,),
            "ds_division": (str,),
            "bid": _NUM,
            "risk_tier": (str,),
            "population": (int,),
        },
    ),
    "refuse": (
        frozenset({REFUSE}),
        {"month_index": (int,), "round": (int,), "ds_division": (str,), "reason": (str,)},
    ),
    "accept_proposal": (
        frozenset({ACCEPT_PROPOSAL}),
        {"month_index": (int,), "round": (int,), "resource": (str,), "units": (int,)},
    ),
    "reject_proposal": (
        frozenset({REJECT_PROPOSAL}),
        {"month_index": (int,), "round": (int,), "reason": (str,)},
    ),
    "inform_done": (
        frozenset({INFORM}),
        {
            "month_index": (int,),
            "round": (int,),
            "ds_division": (str,),
            "resource": (str,),
            "units": (int,),
        },
    ),
    "failure": (
        frozenset({FAILURE}),
        {"month_index": (int,), "round": (int,), "ds_division": (str,), "reason": (str,)},
    ),
    "alert": (
        frozenset({INFORM}),
        {
            "month_index": (int,),
            "ds_division": (str,),
            "tier": (str,),
            "hotspot": (bool,),
            "message": (str,),
        },
    ),
}


class SchemaError(ValueError):
    """Raised when a message body does not match its documented schema."""


def validate(performative: str, body: dict[str, Any]) -> None:
    """Raise SchemaError if ``body`` is not a legal message for ``performative``."""
    if performative not in PERFORMATIVES:
        raise SchemaError(f"unknown performative {performative!r}")
    if not isinstance(body, dict):
        raise SchemaError(f"body must be a dict, got {type(body).__name__}")
    msg_type = body.get("type")
    if msg_type not in SCHEMAS:
        raise SchemaError(f"unknown message type {msg_type!r}")
    allowed, fields = SCHEMAS[msg_type]
    if performative not in allowed:
        raise SchemaError(f"type {msg_type!r} is not allowed with performative {performative!r}")

    present = set(body) - {"type"}
    missing = set(fields) - present
    extra = present - set(fields)
    if missing:
        raise SchemaError(f"{msg_type}: missing fields {sorted(missing)}")
    if extra:
        raise SchemaError(f"{msg_type}: unexpected fields {sorted(extra)}")

    for name, types in fields.items():
        value = body[name]
        # bool is a subclass of int; reject it wherever a number is expected
        if isinstance(value, bool) and bool not in types:
            raise SchemaError(f"{msg_type}.{name}: expected {_names(types)}, got bool")
        if not isinstance(value, types):
            raise SchemaError(
                f"{msg_type}.{name}: expected {_names(types)}, got {type(value).__name__}"
            )

    _check_values(msg_type, body)


def _check_values(msg_type: str, body: dict[str, Any]) -> None:
    if "resource" in body and body["resource"] not in RESOURCES:
        raise SchemaError(f"{msg_type}.resource must be one of {sorted(RESOURCES)}")
    if "season" in body and body["season"] not in SEASONS:
        raise SchemaError(f"{msg_type}.season must be one of {sorted(SEASONS)}")
    if "source" in body and body["source"] not in SOURCES:
        raise SchemaError(f"{msg_type}.source must be one of {sorted(SOURCES)}")
    if "month" in body and not 1 <= body["month"] <= 12:
        raise SchemaError(f"{msg_type}.month must be 1-12")
    for name in ("month_index", "round", "units_available", "units", "population"):
        if name in body and body[name] < 0:
            raise SchemaError(f"{msg_type}.{name} must be >= 0")
    if "deadline_s" in body and body["deadline_s"] <= 0:
        raise SchemaError(f"{msg_type}.deadline_s must be > 0")


def _names(types: tuple[type, ...]) -> str:
    return "|".join("null" if t is type(None) else t.__name__ for t in types)


def encode(performative: str, body: dict[str, Any]) -> str:
    """Validate and serialise a body to the JSON string sent as the message body."""
    validate(performative, body)
    return json.dumps(body, sort_keys=True)


def decode(performative: str, text: str) -> dict[str, Any]:
    """Parse and validate a received message body."""
    try:
        body = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"body is not valid JSON: {exc}") from exc
    validate(performative, body)
    return body
