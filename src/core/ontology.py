"""Shared message vocabulary and provisional payload contracts.

The contracts below use fields explicitly named in the project task briefs. The
briefs do not define payload shapes for every message family; those entries are
marked provisional and must be reviewed by Members A, B, and C before the
ontology is treated as frozen. Message bodies exclude the SPADE performative;
the performative is supplied separately to :func:`validate`.

This module has no SPADE dependency so agents and tests can share the contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


PERFORMATIVES = frozenset(
    {
        "cfp",
        "propose",
        "refuse",
        "accept-proposal",
        "reject-proposal",
        "inform",
        "failure",
    }
)

# Message families and FIPA performatives are separate concepts. This mapping
# is intentionally explicit so an alert message can use the existing
# ``inform`` performative without adding a new FIPA performative. The mapping
# remains provisional pending team review.
MESSAGE_PERFORMATIVES = {"alert": "inform"}

# Each entry documents required fields and their expected Python/JSON value
# types. Empty provisional entries are intentionally not a claim that the
# corresponding protocol messages must have empty bodies; they prevent callers
# from silently inventing fields until the owners agree on the payload.
SCHEMAS: dict[str, dict[str, Any]] = {
    "cfp": {
        "required": {
            "thread": str,
            "month": int,
            "resource": str,
            "n_available": int,
            "round": int,
            "deadline_s": (int, float),
        },
        "optional": {},
        "provisional": False,
        "notes": "Fields follow Member C's published CFP example.",
    },
    "propose": {
        "required": {"bid": (int, float)},
        "optional": {},
        "provisional": True,
        "notes": "A bid is documented; its payload shape is not. `bid` is a provisional minimal shape.",
    },
    "refuse": {
        "required": {},
        "optional": {},
        "provisional": True,
        "notes": "Refusal is required; no refusal payload fields are documented.",
    },
    "accept-proposal": {
        "required": {},
        "optional": {},
        "provisional": True,
        "notes": "Performatives are documented; payload fields are not.",
    },
    "reject-proposal": {
        "required": {},
        "optional": {},
        "provisional": True,
        "notes": "Performatives are documented; payload fields are not.",
    },
    "inform": {
        "required": {},
        "optional": {},
        "provisional": True,
        "notes": "Confirmation is documented; its payload fields are not.",
    },
    "failure": {
        "required": {},
        "optional": {},
        "provisional": True,
        "notes": "Failure handling is documented; its payload fields are not.",
    },
    # These are message types, not FIPA performatives. They can be validated by
    # passing their schema key to validate; their performative mapping remains
    # an integration decision.
    "month_tick": {
        "required": {"month": int},
        "optional": {},
        "provisional": True,
        "notes": "Month number is explicit in the briefs; sender/recipient framing is not.",
    },
    "well_reading": {
        "required": {
            "well_id": str,
            "ds_division": str,
            "district": str,
            "ckdu_prevalence_class": str,
            "month": int,
            "season": str,
            "source": str,
            "pH": (int, float),
            "EC_uS_cm": (int, float),
            "alkalinity_mg_L": (int, float),
            "hardness_mg_L": (int, float),
            "F_mg_L": (int, float),
            "DOC_mg_L": (int, float),
            "Cl_mg_L": (int, float),
            "SO4_mg_L": (int, float),
            "Fe_ug_L": (int, float),
        },
        "optional": {},
        "provisional": True,
        "notes": "Uses Member A's stated reading columns; A's current CSV has no month field.",
    },
    "risk_record": {
        "required": {
            "ds_division": str,
            "month": int,
            "wqi": (int, float),
            "tier": str,
            "drivers": dict,
            "n_wells": int,
            "source": str,
        },
        "optional": {},
        "provisional": True,
        "notes": "Fields follow Member B's published risk_record example.",
    },
    "alert": {
        "required": {
            "event_type": str,
            "ds_division": str,
            "month": int,
            "tier": str,
            "text": str,
            "source": str,
            "hotspot": bool,
        },
        "optional": {},
        "provisional": True,
        "allowed_values": {
            "event_type": {"alert_issued"},
            "tier": {"Excellent", "Good", "Poor"},
            "source": {"real", "synthetic", "mixed"},
        },
        "notes": "Provisional alert payload; inform mapping is explicit in MESSAGE_PERFORMATIVES.",
    },
    "alert_suppression": {
        "required": {
            "event_type": str,
            "ds_division": str,
            "month": int,
            "tier": str,
            "source": str,
            "hotspot": bool,
            "reason": str,
        },
        "optional": {},
        "provisional": True,
        "allowed_values": {
            "event_type": {"alert_suppressed"},
            "tier": {"Excellent", "Good", "Poor"},
            "source": {"real", "synthetic", "mixed"},
            "reason": {"duplicate_same_state_same_month", "unchanged_state"},
        },
        "notes": "Internal audit record, not an alert message to the village health officer.",
    },
}


def validate_message(message_type: str, body: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` unless *body* matches a strict message-family schema."""
    _validate_schema(message_type, body)


def validate(performative: str, body: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` unless *body* matches the selected strict schema.

    The historical parameter name is retained for compatibility. It accepts
    either a FIPA performative or a message-family key in :data:`SCHEMAS`.
    Prefer :func:`validate_message` for message-family payloads. Bodies exclude
    a duplicate ``performative`` field.
    """
    _validate_schema(performative, body)


def _validate_schema(schema_name: str, body: Mapping[str, Any]) -> None:
    if schema_name not in SCHEMAS:
        raise ValueError(f"Unknown message schema/performative: {schema_name!r}")
    if not isinstance(body, Mapping):
        raise ValueError("Message body must be a JSON object (mapping)")

    schema = SCHEMAS[schema_name]
    required: dict[str, Any] = schema["required"]
    optional: dict[str, Any] = schema["optional"]
    missing = set(required) - set(body)
    unexpected = set(body) - set(required) - set(optional)
    problems: list[str] = []
    if missing:
        problems.append(f"missing fields: {', '.join(sorted(missing))}")
    if unexpected:
        problems.append(f"unexpected fields: {', '.join(sorted(unexpected))}")
    for field, expected in {**required, **optional}.items():
        if field not in body:
            continue
        # bool is an int subclass in Python, but is not a meaningful numeric
        # reading, month, bid, or deadline here.
        value = body[field]
        if (isinstance(value, bool) and expected is not bool) or not isinstance(value, expected):
            expected_names = (
                " or ".join(t.__name__ for t in expected)
                if isinstance(expected, tuple)
                else expected.__name__
            )
            problems.append(f"field {field!r} must be {expected_names}")
    if schema_name == "cfp" and not problems:
        if body["month"] < 1 or body["n_available"] < 0 or body["round"] < 1:
            problems.append("cfp month/round must be positive and n_available non-negative")
        if body["deadline_s"] <= 0:
            problems.append("cfp deadline_s must be positive")
        if body["resource"] not in {"ro_unit", "bowser"}:
            problems.append("cfp resource must be 'ro_unit' or 'bowser'")
    allowed_values = schema.get("allowed_values", {})
    for field, allowed in allowed_values.items():
        if field in body and body[field] not in allowed:
            problems.append(f"field {field!r} must be one of {sorted(allowed)}")
    if schema_name in {"alert", "alert_suppression"} and "month" in body and body["month"] < 1:
        problems.append("month must be positive")
    if problems:
        raise ValueError(f"Invalid {schema_name!r} body: " + "; ".join(problems))
