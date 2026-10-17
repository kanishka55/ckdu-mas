"""Well-reading record schema, validation and the real-vs-synthetic guard.

Pure module: no SPADE imports.

The readings frame published to Member B has exactly READING_COLUMNS. Every row carries
``source`` ("real" or "synthetic"); a frame that mixes the two is rejected unless the caller asks
for it explicitly, so real and synthetic data are never mixed silently.
"""
from __future__ import annotations

import pandas as pd

PARAMS = [
    "pH", "EC_uS_cm", "alkalinity_mg_L", "hardness_mg_L",
    "F_mg_L", "DOC_mg_L", "Cl_mg_L", "SO4_mg_L", "Fe_ug_L",
]
ID_COLUMNS = [
    "well_id", "ds_division", "district", "ckdu_prevalence_class",
    "month", "season", "source",
]
READING_COLUMNS = ID_COLUMNS + PARAMS

SOURCES = frozenset({"real", "synthetic"})
SEASONS = frozenset({"Wet", "Dry"})
PREVALENCE_CLASSES = frozenset({"High", "Moderate", "Mild", "Non-prevalence"})

# Modelling assumption, not a published mapping (see docs/data_assumptions.md).
# Dry-zone calendar: the Maha rains (second inter-monsoon + north-east monsoon) fall roughly
# October to January; February to September is treated as dry.
WET_MONTHS = frozenset({10, 11, 12, 1})
MONTH_TO_SEASON = {m: ("Wet" if m in WET_MONTHS else "Dry") for m in range(1, 13)}


class MixedSourceError(ValueError):
    """Raised when real and synthetic rows meet in one frame without explicit permission."""


def season_for_month(month: int) -> str:
    if month not in MONTH_TO_SEASON:
        raise ValueError(f"month must be 1-12, got {month!r}")
    return MONTH_TO_SEASON[month]


def check_sources(df: pd.DataFrame, allow_mixed: bool = False) -> None:
    """Raise if ``source`` is missing, holds an unknown label, or mixes labels without permission."""
    if "source" not in df.columns:
        raise ValueError("frame has no 'source' column; every row must be labelled real or synthetic")
    labels = set(df["source"].dropna().unique())
    if df["source"].isna().any():
        raise ValueError("some rows have no source label")
    unknown = labels - SOURCES
    if unknown:
        raise ValueError(f"unknown source labels {sorted(unknown)}; expected {sorted(SOURCES)}")
    if len(labels) > 1 and not allow_mixed:
        raise MixedSourceError(
            "frame mixes real and synthetic rows; split with real_only()/synthetic_only() "
            "or pass allow_mixed=True explicitly"
        )


def validate_readings(df: pd.DataFrame, allow_mixed: bool = False) -> pd.DataFrame:
    """Check a readings frame against READING_COLUMNS and return it in canonical column order."""
    missing = [c for c in READING_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"readings frame missing columns {missing}")
    check_sources(df, allow_mixed=allow_mixed)

    nulls = [c for c in ID_COLUMNS if df[c].isna().any()]
    if nulls:
        raise ValueError(f"identifier columns contain blanks: {nulls}")
    bad_months = df.loc[~df["month"].isin(range(1, 13)), "month"]
    if not bad_months.empty:
        raise ValueError(f"month must be 1-12, found {sorted(bad_months.unique())}")
    bad_seasons = set(df["season"].unique()) - SEASONS
    if bad_seasons:
        raise ValueError(f"unknown seasons {sorted(bad_seasons)}")
    expected = df["month"].map(MONTH_TO_SEASON)
    if (expected != df["season"]).any():
        raise ValueError("season does not match MONTH_TO_SEASON for some rows")
    bad_classes = set(df["ckdu_prevalence_class"].unique()) - PREVALENCE_CLASSES
    if bad_classes:
        raise ValueError(f"unknown prevalence classes {sorted(bad_classes)}")
    for p in PARAMS:
        if not pd.api.types.is_numeric_dtype(df[p]):
            raise ValueError(f"parameter column {p} is not numeric")
        if (df[p] < 0).any():
            raise ValueError(f"parameter column {p} has negative values")
    if df.duplicated(subset=["well_id", "ds_division", "month", "source"]).any():
        raise ValueError("duplicate (well_id, ds_division, month, source) rows")

    return df[READING_COLUMNS].reset_index(drop=True)


def real_only(df: pd.DataFrame) -> pd.DataFrame:
    check_sources(df, allow_mixed=True)
    return df[df["source"] == "real"].reset_index(drop=True)


def synthetic_only(df: pd.DataFrame) -> pd.DataFrame:
    check_sources(df, allow_mixed=True)
    return df[df["source"] == "synthetic"].reset_index(drop=True)


def month_for_index(month_index: int, start_month: int = 1) -> int:
    """Calendar month (1-12) for a 0-based simulated month counter starting at ``start_month``."""
    if month_index < 0:
        raise ValueError(f"month_index must be >= 0, got {month_index}")
    if start_month not in MONTH_TO_SEASON:
        raise ValueError(f"start_month must be 1-12, got {start_month}")
    return (start_month - 1 + month_index) % 12 + 1


def readings_for_month(monthly: pd.DataFrame, ds_division: str, month: int) -> tuple[str, list[dict]]:
    """One village's readings for one calendar month, ready for a ``well_readings`` message body.

    Returns (source, rows). Raises if the village has no rows that month or its rows mix sources,
    because one message carries exactly one source label.
    """
    season_for_month(month)
    sub = monthly[(monthly["ds_division"] == ds_division) & (monthly["month"] == month)]
    if sub.empty:
        raise ValueError(f"no readings for {ds_division!r} in month {month}")
    check_sources(sub)
    sub = sub.sort_values("well_id", kind="stable")[READING_COLUMNS]
    # .tolist() converts numpy scalars to plain Python types so the rows serialise to JSON
    rows = [dict(zip(READING_COLUMNS, values)) for values in sub.astype(object).values.tolist()]
    return str(sub["source"].iloc[0]), rows


def expand_to_months(seasonal: pd.DataFrame) -> pd.DataFrame:
    """Turn one reading per well per season into one reading per well per calendar month.

    Each month takes the well's reading for the season that month falls in (MONTH_TO_SEASON).
    Values are repeated, not interpolated: the published data has two seasons, not twelve months.
    """
    needed = [c for c in READING_COLUMNS if c != "month"]
    missing = [c for c in needed if c not in seasonal.columns]
    if missing:
        raise ValueError(f"seasonal frame missing columns {missing}")
    check_sources(seasonal, allow_mixed=True)

    months = pd.DataFrame(
        {"month": list(MONTH_TO_SEASON), "season": list(MONTH_TO_SEASON.values())}
    )
    out = seasonal.merge(months, on="season", how="inner")
    out = out.sort_values(["ds_division", "well_id", "month"], kind="stable")
    return validate_readings(out, allow_mixed=True)
