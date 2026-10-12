"""Water Quality Index (WQI) per well. Pure module: no SPADE imports, no file I/O.

The output is a RISK INDICATOR derived from published water-quality thresholds. It is not a CKDu
diagnosis and not a causal claim: the link between water chemistry and CKDu is still contested
(Nikagolla et al. 2020 state their data are insufficient to prove it).

Method: Cooray et al. 2019 (IJERPH 16:1698), Sec 2.3, Eq. 1-3:

    Wi  = wi / sum(wi)
    qi  = (Ci / Si) * 100
    WQI = sum(Wi * qi)

Weights ``wi`` and standards ``Si`` are read from data/raw/ckdu_02_wqi_method_and_thresholds.csv
(Supplementary Table S1), never hard-coded here. A ``TO_FILL`` or non-numeric value raises.

Missing parameters are never treated as zero. ``Wi`` is renormalised over the parameters that are
present and the used/missing parameters are recorded. When parameters are missing the result is a
reduced-parameter WQI: not directly comparable with the paper's 13-parameter values
(see docs/wqi_method.md).
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

# The 13 WQI parameters of Cooray et al. 2019, in Table S1 order.
WQI_PARAMS = [
    "pH", "TDS", "hardness", "alkalinity", "Ca", "Mg", "Na",
    "F", "Cl", "SO4", "NO3", "Fe", "DOC",
]

# WQI parameter -> (readings-frame column, factor converting that column to the unit of Si).
# The first 8 columns exist in src/core/wells.PARAMS. TDS, Ca, Mg, Na and NO3 are not in the data
# yet; their column names follow the same naming so they are picked up once Member A adds them.
COLUMNS: dict[str, tuple[str, float]] = {
    "pH": ("pH", 1.0),
    "TDS": ("TDS_mg_L", 1.0),
    "hardness": ("hardness_mg_L", 1.0),
    "alkalinity": ("alkalinity_mg_L", 1.0),
    "Ca": ("Ca_mg_L", 1.0),
    "Mg": ("Mg_mg_L", 1.0),
    "Na": ("Na_mg_L", 1.0),
    "F": ("F_mg_L", 1.0),
    "Cl": ("Cl_mg_L", 1.0),
    "SO4": ("SO4_mg_L", 1.0),
    "NO3": ("NO3_mg_L", 1.0),
    "Fe": ("Fe_ug_L", 0.001),  # data in ug/L, Si in mg/L
    "DOC": ("DOC_mg_L", 1.0),
}

WEIGHT_PREFIX = "Weight wi: "
STANDARD_PREFIX = "Standard Si: "


class ToFillError(ValueError):
    """A weight or standard needed by the WQI is still TO_FILL (or not a number) in ckdu_02."""


@dataclass(frozen=True)
class WqiMethod:
    weights: dict[str, float]    # wi per parameter
    standards: dict[str, float]  # Si per parameter, in the unit of the COLUMNS conversion


def _number(item: str, raw: str) -> float:
    text = str(raw).strip()
    if text.upper() == "TO_FILL":
        raise ToFillError(f"{item!r} is TO_FILL in ckdu_02; fill it from the source before scoring")
    try:
        value = float(text)
    except ValueError as exc:
        raise ToFillError(f"{item!r} has a non-numeric value {text!r} in ckdu_02") from exc
    if not math.isfinite(value) or value <= 0:
        raise ToFillError(f"{item!r} must be a positive number, got {text!r}")
    return value


def method_from_frame(df: pd.DataFrame) -> WqiMethod:
    """Build the WQI method from the ckdu_02 frame (src/sim/load_data.load_wqi_method).

    Every one of the 13 parameters needs a ``Weight wi:`` and a ``Standard Si:`` row.
    Any ``TO_FILL`` row of either kind raises, even for a parameter not listed in WQI_PARAMS.
    """
    weights: dict[str, float] = {}
    standards: dict[str, float] = {}
    for item, value in zip(df["item"], df["value"]):
        item = str(item)
        for prefix, found in ((WEIGHT_PREFIX, weights), (STANDARD_PREFIX, standards)):
            if item.startswith(prefix):
                param = item[len(prefix):].strip()
                if param in found:  # a second row would silently override the first
                    raise ValueError(f"ckdu_02 has more than one {item!r} row")
                found[param] = _number(item, value)

    for kind, found in (("Weight wi", weights), ("Standard Si", standards)):
        missing = [p for p in WQI_PARAMS if p not in found]
        if missing:
            raise ToFillError(f"ckdu_02 has no '{kind}' row for {missing}")
        unknown = sorted(set(found) - set(WQI_PARAMS))
        if unknown:
            raise ValueError(f"ckdu_02 '{kind}' rows for unknown parameters {unknown}")
    return WqiMethod(weights=weights, standards=standards)


def reject_invalid_rows(
    readings: pd.DataFrame, columns: list[str] | None = None
) -> tuple[pd.DataFrame, list[str]]:
    """Split off wells with an unusable concentration, so one typo does not void a whole village.

    A value is unusable when it is non-numeric, boolean, negative or not finite (inf).
    A blank (None/NaN) is NOT unusable; it is a missing parameter and ``score_frame`` renormalises.
    ``columns`` defaults to the WQI columns; columns absent from the frame are skipped.

    Returns (valid rows with those columns as float, one reason string per rejected row).
    """
    columns = [c for _, (c, _) in COLUMNS.items()] if columns is None else columns
    bad = pd.Series(False, index=readings.index)
    reasons: dict[object, list[str]] = {}
    converted = {}
    for col in [c for c in columns if c in readings.columns]:
        raw = readings[col]
        is_bool = raw.map(lambda v: isinstance(v, bool))
        num = pd.to_numeric(raw.where(~is_bool), errors="coerce")
        col_bad = is_bool | (num.isna() & raw.notna()) | (num < 0) | ~(num.isna() | num.abs().lt(math.inf))
        for idx in col_bad[col_bad].index:
            reasons.setdefault(idx, []).append(f"{col}={raw[idx]!r}")
        bad |= col_bad
        converted[col] = num.astype(float)
    valid = readings.assign(**converted)[~bad]
    well = readings["well_id"] if "well_id" in readings.columns else pd.Series(readings.index, index=readings.index)
    return valid, [f"{well[i]}: {', '.join(r)}" for i, r in reasons.items()]


def relative_weights(method: WqiMethod, params: list[str] | None = None) -> dict[str, float]:
    """Eq. 1: Wi = wi / sum(wi), over ``params`` (default: all 13)."""
    params = WQI_PARAMS if params is None else params
    total = sum(method.weights[p] for p in params)
    return {p: method.weights[p] / total for p in params}


def score_frame(readings: pd.DataFrame, method: WqiMethod) -> pd.DataFrame:
    """Score every row of a readings frame. Returns a new frame aligned to ``readings``' index.

    Columns:
      wqi             WQI over the parameters present in that row (NaN if none present)
      q_<param>       quality rating qi (Eq. 2), NaN when the parameter is missing
      c_<param>       contribution Wi * qi with Wi renormalised over the parameters present;
                      the c_ columns of a row sum to its wqi
      params_used     ";"-joined parameters that entered this row's WQI
      weight_covered  share of the full 13-parameter weight sum that was available (1.0 = full WQI)

    Raises ValueError on a non-numeric or negative concentration. Only a blank (NaN/None) counts as
    missing: a lab typo must not silently shrink the parameter set behind the score.
    """
    q = pd.DataFrame(index=readings.index)
    for p in WQI_PARAMS:
        col, factor = COLUMNS[p]
        if col in readings.columns:
            raw = readings[col]
            conc = pd.to_numeric(raw, errors="coerce")
            bad = raw[conc.isna() & raw.notna()]
            if not bad.empty:
                raise ValueError(f"{col} has non-numeric values {bad.tolist()[:5]}")
            if (conc < 0).any():
                raise ValueError(f"{col} has negative values {conc[conc < 0].tolist()[:5]}")
            conc = conc * factor
        else:
            conc = pd.Series(float("nan"), index=readings.index)
        q[p] = conc / method.standards[p] * 100.0

    present = q.notna()
    w = pd.Series(method.weights)[WQI_PARAMS]
    w_present = present.mul(w, axis=1)
    w_sum = w_present.sum(axis=1)
    rel = w_present.div(w_sum.where(w_sum > 0), axis=0)  # renormalised Wi per row
    contrib = (rel * q).where(present)

    out = pd.DataFrame(index=readings.index)
    out["wqi"] = contrib.sum(axis=1, min_count=1)
    for p in WQI_PARAMS:
        out[f"q_{p}"] = q[p]
    for p in WQI_PARAMS:
        out[f"c_{p}"] = contrib[p]
    out["params_used"] = present.apply(lambda r: ";".join(p for p in WQI_PARAMS if r[p]), axis=1)
    out["weight_covered"] = w_sum / w.sum()
    return out


def score_well(reading: Mapping[str, float], method: WqiMethod) -> dict:
    """Score one reading (column name -> value). Convenience wrapper around ``score_frame``.

    Returns {"wqi", "q", "contributions", "params_used", "params_missing", "weight_covered"}.
    Raises ValueError if none of the 13 parameters is present.
    """
    row = score_frame(pd.DataFrame([dict(reading)]), method).iloc[0]
    used = [p for p in row["params_used"].split(";") if p]
    if not used:
        raise ValueError("no WQI parameter present in the reading; cannot compute a WQI")
    return {
        "wqi": float(row["wqi"]),
        "q": {p: float(row[f"q_{p}"]) for p in used},
        "contributions": {p: float(row[f"c_{p}"]) for p in used},
        "params_used": used,
        "params_missing": [p for p in WQI_PARAMS if p not in used],
        "weight_covered": float(row["weight_covered"]),
    }
