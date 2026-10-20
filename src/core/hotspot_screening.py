"""Configurable, project-defined screening over typed village risk records.

This is a deterministic demo screening indicator, not a validated medical or
epidemiological hotspot model. A caller must explicitly configure which
published WQI tiers trigger the screen and how many consecutive simulated
months are required. No scientific cutoff is supplied by this module.

The shared ontology has no hotspot message type, so this module returns an
internal result. ``alert_hotspot`` is ``None`` when screening is unavailable;
callers must not turn missing evidence into ``False``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from src.core.ontology import INFORM, SchemaError, validate

ScreeningStatus = Literal["active", "inactive", "unavailable"]
KNOWN_TIERS = frozenset({"Excellent", "Good", "Poor", "Very Poor", "Unsuitable", "Unknown"})


@dataclass(frozen=True)
class ScreeningRule:
    """Explicit demo rule configuration; parameters are choices, not validated thresholds."""

    rule_id: str
    trigger_tiers: frozenset[str]
    consecutive_months: int

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise ValueError("rule_id must be non-empty")
        if not isinstance(self.trigger_tiers, frozenset):
            raise ValueError("trigger_tiers must be a frozenset of known tier labels")
        if not self.trigger_tiers:
            raise ValueError("trigger_tiers must contain at least one known tier")
        unknown = self.trigger_tiers - (KNOWN_TIERS - {"Unknown"})
        if unknown:
            raise ValueError(f"trigger_tiers contains unsupported or Unknown tiers: {sorted(unknown)}")
        if isinstance(self.consecutive_months, bool) or not isinstance(self.consecutive_months, int):
            raise ValueError("consecutive_months must be a positive integer")
        if self.consecutive_months < 1:
            raise ValueError("consecutive_months must be a positive integer")


@dataclass(frozen=True)
class ScreeningResult:
    """Internal result for one division and simulated month."""

    status: ScreeningStatus
    ds_division: str
    month_index: int
    rule_id: str | None
    evidence_month_indexes: tuple[int, ...] = ()
    reason: str | None = None

    @property
    def alert_hotspot(self) -> bool | None:
        """Adapt known screening states to AlertAgent's explicit bool-or-None input."""
        if self.status == "active":
            return True
        if self.status == "inactive":
            return False
        return None


def screen_risk_history(
    risk_records: Sequence[Mapping[str, object]],
    *,
    ds_division: str,
    month_index: int,
    rule: ScreeningRule | None,
) -> ScreeningResult:
    """Screen the latest ``consecutive_months`` records for one division.

    Each relevant simulated month must have exactly one valid typed ``risk_record``
    with a known non-``Unknown`` tier and a finite, non-null WQI. Missing months,
    duplicate records, malformed records, or insufficient history yield an
    explicit ``unavailable`` result. A complete window is ``active`` only if
    every tier is in the caller-configured ``trigger_tiers``; otherwise it is
    ``inactive``. This is a screening convenience for demos, not epidemiological
    evidence, and does not add a hotspot field to the shared ontology.
    """
    if not isinstance(ds_division, str) or not ds_division.strip():
        return _unavailable("", month_index, rule, "division_identifier_missing")
    if isinstance(month_index, bool) or not isinstance(month_index, int) or month_index < 0:
        return _unavailable(ds_division, -1, rule, "invalid_month_index")
    if rule is None:
        return _unavailable(ds_division, month_index, None, "screening_rule_not_configured")
    if not isinstance(risk_records, Sequence) or isinstance(risk_records, (str, bytes)):
        return _unavailable(ds_division, month_index, rule, "risk_records_must_be_a_sequence")

    by_month: dict[int, Mapping[str, object]] = {}
    try:
        for record in risk_records:
            if not isinstance(record, Mapping):
                raise SchemaError("risk record must be a mapping")
            body = dict(record)
            validate(INFORM, body)
            if body["ds_division"] != ds_division:
                continue
            index = body["month_index"]
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise SchemaError("month_index must be a non-negative integer")
            if index in by_month:
                return _unavailable(ds_division, month_index, rule, f"duplicate_record_for_month_index_{index}")
            by_month[index] = body
    except (SchemaError, TypeError, ValueError) as exc:
        return _unavailable(ds_division, month_index, rule, f"invalid_risk_record:{exc}")

    first_index = month_index - rule.consecutive_months + 1
    if first_index < 0:
        return _unavailable(ds_division, month_index, rule, "insufficient_consecutive_month_history")
    indexes = tuple(range(first_index, month_index + 1))
    missing = tuple(index for index in indexes if index not in by_month)
    if missing:
        return _unavailable(ds_division, month_index, rule, f"missing_month_indexes:{','.join(map(str, missing))}")

    for index in indexes:
        record = by_month[index]
        tier = record["tier"]
        wqi = record["wqi"]
        if tier == "Unknown":
            return _unavailable(ds_division, month_index, rule, f"unknown_risk_tier_at_month_index_{index}")
        if tier not in KNOWN_TIERS:
            return _unavailable(ds_division, month_index, rule, f"unsupported_risk_tier_at_month_index_{index}")
        if wqi is None:
            return _unavailable(ds_division, month_index, rule, f"missing_wqi_at_month_index_{index}")
        if isinstance(wqi, bool) or not isinstance(wqi, (int, float)) or not math.isfinite(wqi):
            return _unavailable(ds_division, month_index, rule, f"invalid_wqi_at_month_index_{index}")

    is_active = all(by_month[index]["tier"] in rule.trigger_tiers for index in indexes)
    return ScreeningResult(
        "active" if is_active else "inactive",
        ds_division,
        month_index,
        rule.rule_id,
        indexes,
    )


def _unavailable(
    ds_division: str,
    month_index: int,
    rule: ScreeningRule | None,
    reason: str,
) -> ScreeningResult:
    return ScreeningResult("unavailable", ds_division, month_index, rule.rule_id if rule else None,
                           reason=reason)
