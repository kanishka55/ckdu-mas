"""Streamlit dashboard for published reference data and MAS outputs.

Data preparation is deliberately independent of Streamlit/Folium so missing
inputs and source labels can be tested without starting a server. Dashboard
fixtures are illustrative mechanics only; they are not observed or validated
CKDu findings.
"""

from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.sim.evaluate import (
    AllocationRow,
    EvaluationInputError,
    MetricResult,
    calculate_allocation_efficiency,
    extract_logged_risk_records,
    read_allocation_rows,
    read_message_events,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
DISCLAIMER = (
    "Risk indicators from published water-quality research; not a CKDu diagnosis. "
    "Synthetic data is clearly labelled. Water-quality indicators do not establish causation."
)
SYNTHETIC_COLUMNS = (
    "well_id", "ds_division", "district", "ckdu_prevalence_class", "season", "source",
    "pH", "EC_uS_cm", "alkalinity_mg_L", "hardness_mg_L", "F_mg_L", "DOC_mg_L",
    "Cl_mg_L", "SO4_mg_L", "Fe_ug_L",
)
DIVISION_COLUMNS = (
    "ds_division", "district", "ckdu_prevalence_class", "samples_per_season", "population",
    "n_wells_modelled", "distance_to_depot_km", "lat", "lon", "source", "notes",
)


@dataclass(frozen=True)
class Dataset:
    rows: tuple[dict[str, str], ...]
    status: str
    message: str
    source_label: str


@dataclass(frozen=True)
class DashboardData:
    synthetic_wells: Dataset
    divisions: Dataset
    reference_stats: Dataset
    team_risks: tuple[dict[str, Any], ...]
    team_risk_status: str
    team_allocations: tuple[AllocationRow, ...]
    team_allocation_status: str
    team_metrics: Dataset
    demo_risks: tuple[dict[str, Any], ...]
    demo_allocations: tuple[AllocationRow, ...]
    demo_metrics: tuple[MetricResult, ...]
    demo_status: str


def _read_csv(path: str | Path, expected_columns: Sequence[str] | None = None,
              source_label: str = "") -> Dataset:
    csv_path = Path(path)
    if not csv_path.is_file():
        return Dataset((), "unavailable", f"File not found: {csv_path}", source_label)
    try:
        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return Dataset((), "incomplete", "CSV has no header", source_label)
            if expected_columns is not None and reader.fieldnames != list(expected_columns):
                return Dataset((), "incomplete", f"Unexpected columns: {reader.fieldnames!r}", source_label)
            rows = list(reader)
            if any(None in row or any(value is None for value in row.values()) for row in rows):
                return Dataset((), "incomplete", "CSV contains a row with missing/extra cells", source_label)
            return Dataset(tuple(rows), "available" if rows else "empty",
                           f"{len(rows)} rows" if rows else "No records", source_label)
    except (OSError, UnicodeError, csv.Error) as exc:
        return Dataset((), "incomplete", f"Could not read CSV: {exc}", source_label)


def _json_fixture(path: Path, key: str) -> tuple[dict[str, Any], ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload[key]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return ()
        return tuple(rows)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return ()


def _valid_coordinates(rows: Sequence[Mapping[str, str]]) -> tuple[dict[str, Any], ...]:
    points = []
    for row in rows:
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            points.append({**row, "lat_value": lat, "lon_value": lon})
    return tuple(points)


def _load_team_risks(path: Path) -> tuple[tuple[dict[str, Any], ...], str]:
    if not path.is_file():
        return (), f"Unavailable: team message log not found ({path})"
    try:
        records = extract_logged_risk_records(read_message_events(path))
    except (OSError, EvaluationInputError, ValueError) as exc:
        return (), f"Incomplete: {exc}"
    return tuple(records), "Available" if records else "No risk records in message log"


def _load_team_allocations(path: Path) -> tuple[tuple[AllocationRow, ...], str]:
    if not path.is_file():
        return (), f"Unavailable: allocation log not found ({path})"
    try:
        records = read_allocation_rows(path)
    except (OSError, EvaluationInputError, ValueError) as exc:
        return (), f"Incomplete: {exc}"
    return tuple(records), "Available" if records else "No allocation records"


def load_dashboard_data(
    *,
    synthetic_wells_path: str | Path = ROOT / "data/processed/synthetic_wells.csv",
    divisions_path: str | Path = ROOT / "data/raw/ckdu_03_sampling_design_ds_divisions.csv",
    reference_stats_path: str | Path = ROOT / "data/raw/ckdu_01_water_quality_area_stats.csv",
    messages_path: str | Path = ROOT / "logs/messages.csv",
    allocations_path: str | Path = ROOT / "logs/allocations.csv",
    evaluation_path: str | Path = ROOT / "logs/evaluation_summary.csv",
    fixture_dir: str | Path = FIXTURES,
) -> DashboardData:
    """Load team outputs, reference data and a separate deterministic demo set."""
    fixture_dir = Path(fixture_dir)
    fixture_note = "Illustrative fixtures; not observations or scientific validation."
    try:
        demo_allocations = tuple(read_allocation_rows(fixture_dir / "allocation_outcomes.csv"))
        inputs = json.loads((fixture_dir / "allocation_inputs.json").read_text(encoding="utf-8"))
        demo_metrics = tuple(calculate_allocation_efficiency(
            demo_allocations, inputs["population_by_division"],
            {(item["ds_division"], item["month"]): item["risk_weight"]
             for item in inputs["risk_weights"]},
        ))
        demo_risks = _json_fixture(fixture_dir / "risk_records.json", "records")
        demo_status = fixture_note if demo_allocations and demo_risks else "Demo fixtures unavailable or incomplete."
    except (OSError, json.JSONDecodeError, KeyError, TypeError, EvaluationInputError, ValueError):
        demo_risks, demo_allocations, demo_metrics = (), (), ()
        demo_status = "Demo fixtures unavailable or incomplete."
    return DashboardData(
        _read_csv(synthetic_wells_path, SYNTHETIC_COLUMNS, "Synthetic observations (source=synthetic)"),
        _read_csv(divisions_path, DIVISION_COLUMNS, "Published sampling design; coordinates/population may be blank"),
        _read_csv(reference_stats_path, source_label="Real published reference summary (Cooray et al. 2019)"),
        *_load_team_risks(Path(messages_path)),
        *_load_team_allocations(Path(allocations_path)),
        _read_csv(evaluation_path, source_label="Team evaluation output"),
        demo_risks,
        demo_allocations,
        demo_metrics,
        demo_status,
    )


def available_months(data: DashboardData, *, demo: bool) -> tuple[int, ...]:
    if demo:
        rows = (*data.demo_risks, *data.demo_allocations)
    else:
        rows = (*data.team_risks, *data.team_allocations)
    months = {
        row.month if isinstance(row, AllocationRow) else row.get("month")
        for row in rows
    }
    return tuple(sorted(month for month in months if isinstance(month, int) and not isinstance(month, bool)))


def allocation_rows_for_month(rows: Sequence[AllocationRow], month: int) -> tuple[AllocationRow, ...]:
    return tuple(row for row in rows if row.month == month)


def _metrics_for_month(dataset: Dataset, month: int) -> tuple[MetricResult, ...]:
    """Adapt the evaluator's CSV output for display without recalculating it."""
    if dataset.status not in {"available", "empty"}:
        return ()
    results = []
    for row in dataset.rows:
        scope = row.get("scope", "")
        if f"month={month}" not in scope:
            continue
        try:
            raw_value = row.get("value", "")
            value = None if raw_value == "" else float(raw_value)
            observations = int(row.get("observations", "0"))
        except (TypeError, ValueError):
            continue
        results.append(MetricResult(
            row.get("metric", ""), scope, value, row.get("unit", ""),
            row.get("status", "unknown"), observations, {},
        ))
    return tuple(results)


def _show_map(st: Any, divisions: Dataset, risk_records: Sequence[Mapping[str, Any]]) -> None:
    points = _valid_coordinates(divisions.rows)
    if not points:
        st.info("Map unavailable: the division dataset has no valid latitude/longitude values. No coordinates are inferred.")
        return
    try:
        import folium
        from streamlit.components.v1 import html
    except ImportError as exc:
        st.info(f"Map rendering unavailable because Folium/Streamlit is not installed: {exc}")
        return
    tiers = {r.get("ds_division"): r.get("tier") for r in risk_records}
    color = {"Poor": "#b42318", "Good": "#175cd3", "Excellent": "#027a48"}
    center = (sum(p["lat_value"] for p in points) / len(points),
              sum(p["lon_value"] for p in points) / len(points))
    map_view = folium.Map(location=center, zoom_start=8)
    for point in points:
        division = point.get("ds_division", "Unknown division")
        tier = tiers.get(division)
        folium.CircleMarker(
            location=(point["lat_value"], point["lon_value"]), radius=7,
            color=color.get(tier, "#667085"), fill=True, fill_opacity=0.8,
            tooltip=f"{division} · risk tier: {tier or 'unavailable'}",
        ).add_to(map_view)
    html(map_view._repr_html_(), height=500, scrolling=True)


def _show_risks(st: Any, records: Sequence[Mapping[str, Any]], status: str, month: int | None) -> None:
    if month is None:
        st.info("Monthly risk records are unavailable for this view.")
        return
    selected = [r for r in records if r.get("month") == month]
    if not selected:
        st.info(f"No risk records are available for month {month}. {status}")
        return
    st.caption(status)
    st.dataframe([{
        "Division": r.get("ds_division"), "Tier": r.get("tier"),
        "Drivers": json.dumps(r.get("drivers", {}), sort_keys=True),
        "Source": r.get("source", "unspecified"),
    } for r in selected], use_container_width=True)


def _show_allocations(st: Any, rows: Sequence[AllocationRow], month: int | None,
                      metrics: Sequence[MetricResult] = (), label: str = "") -> None:
    if month is None:
        st.info("Monthly allocation outcomes are unavailable for this view.")
        return
    selected = allocation_rows_for_month(rows, month)
    if not selected:
        st.info(f"No allocation outcomes are available for month {month}.")
        return
    if label:
        st.caption(label)
    st.dataframe([{
        "Method": r.method, "Division": r.ds_division, "Resource": r.resource,
        "Round": r.round, "Winning bid": r.winning_bid,
    } for r in selected], use_container_width=True)
    month_results = [m for m in metrics if f"month={month}" in m.scope]
    if not month_results:
        st.info("Efficiency comparisons unavailable: agreed population and risk-weight inputs are not present.")
        return
    st.dataframe([{
        "Metric": m.metric, "Value": m.value if m.value is not None else "Unavailable",
        "Unit": m.unit, "Status": m.status,
    } for m in month_results], use_container_width=True)


def main() -> None:
    try:
        import streamlit as st
    except ImportError as exc:
        raise SystemExit("Streamlit is required to launch this dashboard; see project dependency instructions.") from exc

    st.set_page_config(page_title="CKDu Well-Water Risk Indicators", layout="wide")
    st.title("CKDu Well-Water Risk-Indicator Support")
    st.warning(DISCLAIMER)
    data_root = Path(os.environ.get("CKDU_DATA_ROOT", ROOT / "data"))
    logs_root = Path(os.environ.get("CKDU_LOGS_DIR", ROOT / "logs"))
    data = load_dashboard_data(
        synthetic_wells_path=data_root / "processed/synthetic_wells.csv",
        divisions_path=data_root / "raw/ckdu_03_sampling_design_ds_divisions.csv",
        reference_stats_path=data_root / "raw/ckdu_01_water_quality_area_stats.csv",
        messages_path=logs_root / "messages.csv", allocations_path=logs_root / "allocations.csv",
        evaluation_path=logs_root / "evaluation_summary.csv",
    )
    view = st.radio("Data view", ("Team outputs", "Illustrative demo fixtures"), horizontal=True)
    demo = view.startswith("Illustrative")
    months = available_months(data, demo=demo)
    month = st.selectbox("Month", months if months else ["Unavailable — no monthly records"], disabled=not months)
    month_value = month if isinstance(month, int) else None

    if demo:
        risks, allocations, metrics = data.demo_risks, data.demo_allocations, data.demo_metrics
        st.info(data.demo_status)
        risk_status = data.demo_status
        allocation_label = data.demo_status
    else:
        risks, allocations = data.team_risks, data.team_allocations
        metrics = _metrics_for_month(data.team_metrics, month_value) if month_value is not None else ()
        risk_status = data.team_risk_status
        allocation_label = (
            f"Allocation log: {data.team_allocation_status}; "
            f"evaluation summary: {data.team_metrics.status} ({data.team_metrics.source_label})."
        )

    left, right = st.columns((3, 2))
    with left:
        st.subheader("Division risk map")
        map_risks = [r for r in risks if month_value is not None and r.get("month") == month_value]
        _show_map(st, data.divisions, map_risks)
    with right:
        st.subheader("Risk tiers and drivers")
        _show_risks(st, risks, risk_status, month_value)

    st.subheader("Monthly allocation outcomes and comparisons")
    _show_allocations(st, allocations, month_value, metrics, allocation_label)

    with st.expander("Synthetic well observations (seasonal; not monthly risk tiers)"):
        st.caption(f"{data.synthetic_wells.source_label}. {data.synthetic_wells.message}.")
        if data.synthetic_wells.rows:
            seasons = sorted({r["season"] for r in data.synthetic_wells.rows})
            season = st.selectbox("Season", seasons)
            st.dataframe([r for r in data.synthetic_wells.rows if r["season"] == season], use_container_width=True)
        else:
            st.info(data.synthetic_wells.message)
    with st.expander("Real published reference data"):
        st.caption(data.reference_stats.source_label)
        if data.reference_stats.rows:
            st.dataframe(data.reference_stats.rows, use_container_width=True)
        else:
            st.info(data.reference_stats.message)
        st.caption("The reference sampling design contains no division coordinates in the current CSV.")


if __name__ == "__main__":
    main()
