"""WQI -> risk tier per well, and well scores -> one risk record per village (DS division).

Pure module: no SPADE imports, no file I/O.

Tiers are the published WQI water-type classes (Cooray et al. 2019, Table 2 and Supplementary
Table S2), read from data/raw/ckdu_02_wqi_method_and_thresholds.csv. They describe water quality
against drinking-water standards. They are a risk indicator, not a CKDu diagnosis and not a claim
that the water causes CKDu.

Village rule (documented in docs/wqi_method.md):
- village WQI  = MEDIAN of the well WQIs that month; village tier = class of that median.
  One bad well among twenty does not move the median, so it does not relabel the whole village.
- share_poor_or_worse = fraction of wells in Poor, Very Poor or Unsuitable. This keeps the
  "one bad well" situation visible to the alert agent and the dashboard.
- drivers = parameters whose village-mean qi exceeds 100 (i.e. above their standard Si), ranked by
  mean contribution Wi * qi, at most N_DRIVERS. Ranking all parameters by contribution would list
  the heavily weighted ones (F, DOC) and pH (qi ~ 85-95 even when in range) for every village,
  which explains nothing. An empty list means no parameter is above its standard on average.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

import pandas as pd

from src.core import wqi as wqi_mod

CLASS_PREFIX = "WQI class: "
UNKNOWN_TIER = "Unknown"  # used only when no WQI could be computed (wqi is None)
POOR_OR_WORSE = frozenset({"Poor", "Very Poor", "Unsuitable"})
N_DRIVERS = 3


@dataclass(frozen=True)
class WqiClass:
    label: str
    lower: float  # inclusive
    upper: float  # exclusive


def _parse_range(item: str, text: str) -> tuple[float, float]:
    text = str(text).strip().replace("–", "-")
    if text.upper() == "TO_FILL":
        raise wqi_mod.ToFillError(f"{item!r} is TO_FILL in ckdu_02")
    if m := re.fullmatch(r"<\s*([\d.]+)", text):
        return 0.0, float(m[1])
    if m := re.fullmatch(r">\s*([\d.]+)", text):
        return float(m[1]), math.inf
    if m := re.fullmatch(r"([\d.]+)\s*-\s*([\d.]+)", text):
        return float(m[1]), float(m[2])
    raise ValueError(f"{item!r}: cannot parse class range {text!r}")


def classes_from_frame(df: pd.DataFrame) -> list[WqiClass]:
    """Read the ``WQI class:`` rows of ckdu_02. Ranges must be contiguous from 0 to infinity."""
    classes = []
    for item, value in zip(df["item"], df["value"]):
        item = str(item)
        if item.startswith(CLASS_PREFIX):
            lo, hi = _parse_range(item, value)
            classes.append(WqiClass(item[len(CLASS_PREFIX):].strip(), lo, hi))
    if not classes:
        raise ValueError("ckdu_02 has no 'WQI class:' rows")
    classes.sort(key=lambda c: c.lower)
    if classes[0].lower != 0:
        raise ValueError(f"lowest WQI class must start at 0, got {classes[0]}")
    for a, b in zip(classes, classes[1:]):
        if a.upper != b.lower:
            raise ValueError(f"WQI classes are not contiguous: {a.label} ends at {a.upper}, "
                             f"{b.label} starts at {b.lower}")
    if classes[-1].upper != math.inf:
        raise ValueError(f"highest WQI class must be open-ended, got {classes[-1]}")
    return classes


def tier_for_wqi(value: float | None, classes: list[WqiClass]) -> str:
    """Class label for a WQI value. Lower bound inclusive: 50 -> Good, 100 -> Poor."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return UNKNOWN_TIER
    if value < 0:
        raise ValueError(f"WQI cannot be negative, got {value}")
    for c in classes:
        if c.lower <= value < c.upper:
            return c.label
    raise ValueError(f"WQI {value} falls outside every class")  # unreachable if contiguous


def tier_order(classes: list[WqiClass]) -> list[str]:
    """Labels from best to worst water quality."""
    return [c.label for c in sorted(classes, key=lambda c: c.lower)]


def village_risk(scored: pd.DataFrame, classes: list[WqiClass]) -> dict:
    """Aggregate one village-month of well scores (output of ``wqi.score_frame``).

    Returns wqi (median, or None), tier, drivers, n_wells, n_scored, share_poor_or_worse,
    worst_tier, weight_covered (minimum over wells) and params_used (union).
    """
    n_wells = len(scored)
    ok = scored[scored["wqi"].notna()]
    if ok.empty:
        return {"wqi": None, "tier": UNKNOWN_TIER, "drivers": [], "n_wells": n_wells,
                "n_scored": 0, "share_poor_or_worse": None, "worst_tier": UNKNOWN_TIER,
                "weight_covered": 0.0, "params_used": []}

    # Rounded once here, so the WQI sent in risk_record is exactly the value the tier came from
    # (an unrounded 99.97 would otherwise be sent as 100.0 with tier "Good").
    median = round(float(ok["wqi"].median()), 1)
    well_tiers = ok["wqi"].map(lambda v: tier_for_wqi(float(v), classes))
    order = tier_order(classes)
    params = wqi_mod.WQI_PARAMS
    mean_q = ok[[f"q_{p}" for p in params]].mean(skipna=True).set_axis(params)
    contrib = ok[[f"c_{p}" for p in params]].mean(skipna=True).set_axis(params)
    exceeding = [p for p in params if mean_q[p] > 100]
    # sort by contribution, then by name, so ties are deterministic
    drivers = sorted(((p, contrib[p]) for p in exceeding), key=lambda kv: (-kv[1], kv[0]))
    drivers = drivers[:N_DRIVERS]
    used = sorted({p for s in ok["params_used"] for p in s.split(";") if p},
                  key=wqi_mod.WQI_PARAMS.index)
    return {
        "wqi": median,
        "tier": tier_for_wqi(median, classes),
        "drivers": [p for p, _ in drivers],
        "driver_contributions": {p: float(v) for p, v in drivers},
        "n_wells": n_wells,
        "n_scored": len(ok),
        "share_poor_or_worse": float(well_tiers.isin(POOR_OR_WORSE).mean()),
        "worst_tier": max(well_tiers, key=order.index),
        "weight_covered": float(ok["weight_covered"].min()),
        "params_used": used,
    }


def well_risk(month_index: int, readings: pd.DataFrame, scored: pd.DataFrame,
              classes: list[WqiClass]) -> dict:
    """The ``well_risk`` message body: per-well detail for one village-month (to the HotspotAgent).

    ``readings`` are the validated rows of one village and one month; ``scored`` is
    ``wqi.score_frame(readings)`` (same index). Identifiers come from the rows themselves.
    """
    first = readings.iloc[0]
    rows = []
    for idx, r in readings.iterrows():
        s = scored.loc[idx]
        value = None if pd.isna(s["wqi"]) else round(float(s["wqi"]), 1)
        q = {p: round(float(s[f"q_{p}"]), 1) for p in wqi_mod.WQI_PARAMS if not pd.isna(s[f"q_{p}"])}
        rows.append({"well_id": str(r["well_id"]), "wqi": value,
                     "tier": tier_for_wqi(value, classes), "q": q})
    return {
        "type": "well_risk",
        "month_index": int(month_index),
        "month": int(first["month"]),
        "ds_division": str(first["ds_division"]),
        "district": str(first["district"]),
        "ckdu_prevalence_class": str(first["ckdu_prevalence_class"]),
        "source": str(first["source"]),
        "wells": rows,
    }


def well_rows(body: dict) -> list[dict]:
    """Flatten a ``well_risk`` body to one row per well (q_<param> columns), e.g. for a CSV."""
    keys = ("month_index", "month", "ds_division", "district", "ckdu_prevalence_class", "source")
    return [{**{k: body[k] for k in keys}, "well_id": w["well_id"], "wqi": w["wqi"], "tier": w["tier"],
             **{f"q_{p}": v for p, v in w["q"].items()}} for w in body["wells"]]


def risk_record(month_index: int, ds_division: str, village: dict) -> dict:
    """The ``risk_record`` message body from src/core/ontology.py for one village-month."""
    wqi_value = village["wqi"]
    return {
        "type": "risk_record",
        "month_index": int(month_index),
        "ds_division": ds_division,
        "wqi": None if wqi_value is None else float(wqi_value),
        "tier": village["tier"],
        "drivers": list(village["drivers"]),
    }
