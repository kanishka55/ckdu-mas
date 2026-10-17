"""Load the data pack into typed pandas frames.

One function per file. Each fails loudly (FileNotFoundError / ValueError) on a missing,
empty or malformed file instead of returning an empty frame.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.core import wells

RAW = Path("data") / "raw"
PROCESSED = Path("data") / "processed"


def _read(path: str | Path, required: list[str]) -> pd.DataFrame:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"data file not found: {path}")
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError as exc:
        raise ValueError(f"{path} is empty") from exc
    if df.empty:
        raise ValueError(f"{path} has a header but no rows")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path} is missing columns {missing}")
    return df


def _numeric(df: pd.DataFrame, cols: list[str], path: str | Path) -> None:
    for c in cols:
        converted = pd.to_numeric(df[c], errors="coerce")
        bad = df.loc[converted.isna() & df[c].notna(), c]
        if not bad.empty:
            raise ValueError(f"{path}: column {c} has non-numeric values {bad.tolist()}")
        df[c] = converted.astype(float)


def load_area_stats(path: str | Path = RAW / "ckdu_01_water_quality_area_stats.csv") -> pd.DataFrame:
    """Cooray et al. 2019 Table 1: Min/Max/Avg per prevalence class x season. Real."""
    df = _read(path, ["ckdu_prevalence_class", "statistic", "season", *wells.PARAMS])
    _numeric(df, wells.PARAMS, path)
    bad = set(df["statistic"]) - {"Min", "Max", "Avg"}
    if bad:
        raise ValueError(f"{path}: unknown statistic labels {sorted(bad)}")
    return df


def load_wqi_method(path: str | Path = RAW / "ckdu_02_wqi_method_and_thresholds.csv") -> pd.DataFrame:
    """WQI method, weights and thresholds. ``value`` stays a string: it holds ranges and TO_FILL."""
    df = _read(path, ["item", "type", "value", "unit", "source", "notes"])
    df["value"] = df["value"].astype(str)
    return df


def load_ds_divisions(
    path: str | Path = RAW / "ckdu_03_sampling_design_ds_divisions.csv",
    require_complete: bool = True,
) -> pd.DataFrame:
    """The 11 DS divisions (one VillageAgent each) with population, coordinates and depot distance.

    With ``require_complete`` the frame must have no blanks in the columns agents rely on.
    """
    cols = [
        "ds_division", "district", "ckdu_prevalence_class", "samples_per_season",
        "population", "n_wells_modelled", "distance_to_depot_km", "lat", "lon", "source", "notes",
    ]
    df = _read(path, cols)
    _numeric(df, ["samples_per_season", "population", "n_wells_modelled",
                  "distance_to_depot_km", "lat", "lon"], path)
    if df["ds_division"].duplicated().any():
        raise ValueError(f"{path}: duplicate ds_division rows")
    filled = ["population", "n_wells_modelled", "distance_to_depot_km", "lat", "lon"]
    if require_complete:
        blanks = {c: df.loc[df[c].isna(), "ds_division"].tolist() for c in filled}
        blanks = {c: v for c, v in blanks.items() if v}
        if blanks:
            raise ValueError(f"{path}: blank values {blanks}")
    for c in ("samples_per_season", "population", "n_wells_modelled"):
        df[c] = df[c].astype("Int64")
    return df


def load_aggregate_stats(path: str | Path = RAW / "ckdu_04_ckdu_aggregate_stats.csv") -> pd.DataFrame:
    """Ranasinghe et al. 2019 district aggregates. ``value`` kept as string (some are ranges)."""
    df = _read(path, ["scope", "period", "metric", "value", "unit_or_note", "source"])
    df["value"] = df["value"].astype(str)
    return df


def load_validation_benchmarks(path: str | Path = RAW / "ckdu_05_validation_benchmarks.csv") -> pd.DataFrame:
    return _read(path, ["study", "location", "design", "finding", "source"])


def load_synthetic_wells(path: str | Path = PROCESSED / "synthetic_wells.csv") -> pd.DataFrame:
    """Seasonal synthetic readings (one row per well per season). Every row must be synthetic."""
    required = [c for c in wells.READING_COLUMNS if c != "month"]
    df = _read(path, required)
    _numeric(df, wells.PARAMS, path)
    wells.check_sources(df)
    if set(df["source"]) != {"synthetic"}:
        raise ValueError(f"{path}: expected only source='synthetic' rows")
    return df


def load_monthly_readings(path: str | Path = PROCESSED / "synthetic_wells.csv") -> pd.DataFrame:
    """Synthetic readings expanded to 12 calendar months: the frame published to Member B."""
    return wells.expand_to_months(load_synthetic_wells(path))
