"""Streamlit view for Member B risk outputs and published reference data.

Data preparation is independent of Streamlit/Folium. Member B's generated
``risk_scores.csv`` and ``well_scores.csv`` are read only when present and
validated against their writer's fields. Test/demo fixtures remain separate
from team outputs. No month-index-to-calendar mapping is inferred.
"""

from __future__ import annotations

import ast
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
    read_allocation_rows,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
DISCLAIMER = (
    "Risk indicators derived from published water-quality thresholds are not a CKDu diagnosis. "
    "Water-quality indicators do not establish that water caused CKDu."
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
EVALUATION_COLUMNS = ("metric", "scope", "value", "unit", "status", "observations", "details")
RISK_SCORE_REQUIRED = frozenset({
    "month_index", "ds_division", "wqi", "tier", "drivers", "source",
})
WELL_SCORE_REQUIRED = frozenset({
    "month_index", "month", "ds_division", "district", "ckdu_prevalence_class",
    "source", "well_id", "wqi", "tier",
})


@dataclass(frozen=True)
class Dataset:
    rows: tuple[dict[str, str], ...]
    status: str
    message: str
    source_label: str
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class DashboardData:
    synthetic_wells: Dataset
    divisions: Dataset
    reference_stats: Dataset
    team_risks: tuple[dict[str, str], ...]
    team_risk_status: str
    team_risk_source_label: str
    team_wells: tuple[dict[str, str], ...]
    team_well_status: str
    team_well_source_label: str
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
            columns = tuple(reader.fieldnames)
            rows = list(reader)
            if any(None in row or any(value is None for value in row.values()) for row in rows):
                return Dataset((), "incomplete", "CSV contains a row with missing/extra cells", source_label)
            return Dataset(tuple(rows), "available" if rows else "empty",
                           f"{len(rows)} rows" if rows else "No records", source_label, columns)
    except (OSError, UnicodeError, csv.Error) as exc:
        return Dataset((), "incomplete", f"Could not read CSV: {exc}", source_label)


def _validate_indexed_scores(dataset: Dataset, required: frozenset[str], *, wells: bool) -> Dataset:
    """Validate B's CSV row essentials without inventing missing score values."""
    if dataset.status in {"unavailable", "incomplete"}:
        return dataset
    headers = set(dataset.columns)
    missing = required - headers
    if missing:
        return Dataset((), "incomplete", f"Missing required columns: {sorted(missing)}", dataset.source_label)
    if dataset.status == "empty":
        return dataset
    validated: list[dict[str, str]] = []
    for number, row in enumerate(dataset.rows, start=2):
        try:
            index = int(row["month_index"])
            if index < 0:
                raise ValueError("month_index must be non-negative")
            if not row["ds_division"] or not row["tier"]:
                raise ValueError("division and tier must be present")
            if row["source"] not in {"real", "synthetic"}:
                raise ValueError("source must be 'real' or 'synthetic'")
            if row["wqi"].strip():
                value = float(row["wqi"])
                if not math.isfinite(value):
                    raise ValueError("wqi must be finite or blank")
            if wells:
                if not row["month"].strip() or not row["well_id"].strip():
                    raise ValueError("calendar month and well_id must be present")
                int(row["month"])
            else:
                _parse_list_cell(row["drivers"], f"row {number} drivers")
        except (TypeError, ValueError, SyntaxError) as exc:
            return Dataset((), "incomplete", f"Invalid score output at row {number}: {exc}", dataset.source_label)
        validated.append(row)
    return Dataset(tuple(validated), "available", f"{len(validated)} rows", dataset.source_label)


def _parse_list_cell(value: str, context: str) -> list[Any]:
    """Read pandas' list repr or JSON list used by Member B's CSV writer."""
    if isinstance(value, list):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = ast.literal_eval(value)
    else:
        raise ValueError(f"{context} must be a list")
    if not isinstance(parsed, list):
        raise ValueError(f"{context} must be a list")
    if any(not isinstance(item, str) for item in parsed):
        raise ValueError(f"{context} must contain strings")
    return parsed


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


def _load_team_risks(path: Path) -> Dataset:
    source = "Member B village risk output (logs/risk_scores.csv)"
    raw = _read_csv(path, source_label=source)
    return _validate_indexed_scores(raw, RISK_SCORE_REQUIRED, wells=False)


def _load_team_wells(path: Path) -> Dataset:
    source = "Member B per-well scores (logs/well_scores.csv)"
    raw = _read_csv(path, source_label=source)
    return _validate_indexed_scores(raw, WELL_SCORE_REQUIRED, wells=True)


def _dataset_status(dataset: Dataset) -> str:
    label = {
        "available": "Available",
        "empty": "Empty",
        "unavailable": "Unavailable",
        "incomplete": "Incomplete",
    }.get(dataset.status, "Unknown")
    return f"{label}: {dataset.message}"


def _load_team_allocations(path: Path) -> tuple[tuple[AllocationRow, ...], str]:
    if not path.is_file():
        return (), f"Unavailable: allocation file not found ({path})"
    try:
        records = tuple(read_allocation_rows(path))
    except (OSError, EvaluationInputError, ValueError) as exc:
        return (), f"Incomplete: {exc}"
    if not records:
        return (), "Empty: allocation file has no rows"
    return records, "Present but unverified: Member C output provenance is not confirmed"


def load_dashboard_data(
    *,
    synthetic_wells_path: str | Path = ROOT / "data/processed/synthetic_wells.csv",
    divisions_path: str | Path = ROOT / "data/raw/ckdu_03_sampling_design_ds_divisions.csv",
    reference_stats_path: str | Path = ROOT / "data/raw/ckdu_01_water_quality_area_stats.csv",
    risk_scores_path: str | Path = ROOT / "logs/risk_scores.csv",
    well_scores_path: str | Path = ROOT / "logs/well_scores.csv",
    allocations_path: str | Path = ROOT / "logs/allocations.csv",
    evaluation_path: str | Path = ROOT / "logs/evaluation_summary.csv",
    fixture_dir: str | Path = FIXTURES,
) -> DashboardData:
    """Load team outputs, reference data and a separate illustrative demo set."""
    fixture_dir = Path(fixture_dir)
    fixture_note = "Illustrative fixtures only; not observations, team outputs or scientific findings."
    try:
        demo_allocations = tuple(read_allocation_rows(fixture_dir / "allocation_outcomes.csv"))
        inputs = json.loads((fixture_dir / "allocation_inputs.json").read_text(encoding="utf-8"))
        # Fixture arithmetic is deliberately not attested as a real team result.
        demo_metrics = tuple(calculate_allocation_efficiency(
            demo_allocations, inputs.get("population_by_division"),
            {(item["ds_division"], item["month"]): item["risk_weight"]
             for item in inputs.get("risk_weights", [])},
        ))
        demo_risks = _json_fixture(fixture_dir / "risk_records.json", "records")
        demo_status = fixture_note if demo_allocations and demo_risks else "Demo fixtures unavailable or incomplete."
    except (OSError, json.JSONDecodeError, KeyError, TypeError, EvaluationInputError, ValueError):
        demo_risks, demo_allocations, demo_metrics = (), (), ()
        demo_status = "Demo fixtures unavailable or incomplete."
    risk_dataset = _load_team_risks(Path(risk_scores_path))
    well_dataset = _load_team_wells(Path(well_scores_path))
    allocations, allocation_status = _load_team_allocations(Path(allocations_path))
    return DashboardData(
        _read_csv(synthetic_wells_path, SYNTHETIC_COLUMNS,
                  "Synthetic observations (source=synthetic; not measured observations)"),
        _read_csv(divisions_path, DIVISION_COLUMNS,
                  "Published sampling design; coordinates, where present, are approximate division town points"),
        _read_csv(reference_stats_path, source_label="Real published reference summary (Cooray et al. 2019)"),
        risk_dataset.rows,
        _dataset_status(risk_dataset),
        risk_dataset.source_label,
        well_dataset.rows,
        _dataset_status(well_dataset),
        well_dataset.source_label,
        allocations,
        allocation_status,
        _read_csv(evaluation_path, EVALUATION_COLUMNS, "Member D evaluation summary"),
        demo_risks,
        demo_allocations,
        demo_metrics,
        demo_status,
    )


def available_months(data: DashboardData, *, demo: bool) -> tuple[int, ...]:
    """Return simulated month_index values for risk output only."""
    rows = data.demo_risks if demo else data.team_risks
    values: set[int] = set()
    for row in rows:
        raw = row.get("month_index")
        try:
            index = int(raw) if raw is not None else -1
        except (TypeError, ValueError):
            continue
        if index >= 0:
            values.add(index)
    return tuple(sorted(values))


def available_allocation_months(data: DashboardData, *, demo: bool) -> tuple[int, ...]:
    """Return allocation CSV calendar month values separately from month_index."""
    rows = data.demo_allocations if demo else data.team_allocations
    return tuple(sorted({row.month for row in rows}))


def allocation_rows_for_month(rows: Sequence[AllocationRow], month: int) -> tuple[AllocationRow, ...]:
    return tuple(row for row in rows if row.month == month)


def _metrics_for_month(
    dataset: Dataset, month: int, *, allocation_results_verified: bool = False,
) -> tuple[MetricResult, ...]:
    """Adapt evaluator CSV rows for one allocation calendar month.

    The evaluator's ``month=`` scope refers to allocation calendar month, not
    the simulation's ``month_index``. Invalid rows are not silently interpreted
    as successful values; the summary display reports them as incomplete.
    """
    if dataset.status not in {"available", "empty"}:
        return ()
    results = []
    for row in dataset.rows:
        scope = row.get("scope", "")
        if f"month={month}" not in scope.split(";"):
            continue
        try:
            status = row.get("status", "unknown")
            if status not in {"ok", "partial", "unavailable"}:
                continue
            raw_value = row.get("value", "")
            value = None if status == "unavailable" or raw_value == "" else float(raw_value)
            observations = int(row.get("observations", ""))
            if observations < 0:
                continue
        except (TypeError, ValueError):
            continue
        if value is not None and not math.isfinite(value):
            continue
        metric = row.get("metric", "")
        if _is_allocation_metric(metric) and not allocation_results_verified:
            value = None
            status = "unavailable"
        results.append(MetricResult(
            metric, scope, value, row.get("unit", ""),
            status, observations, {},
        ))
    return tuple(results)


ALLOCATION_METRICS = frozenset({"allocation_efficiency", "risk_weighted_population_served"})


def _is_allocation_metric(metric: str) -> bool:
    return metric in ALLOCATION_METRICS or metric.startswith("contract_net_minus_")


def prepare_evaluation_display(
    dataset: Dataset, *, allocation_results_verified: bool = False,
) -> tuple[str, tuple[dict[str, Any], ...]]:
    """Prepare evaluator rows for display without calculating new metrics.

    The dashboard has no provenance attestation for Member C's allocation
    output, so allocation values are withheld unless a future verified input
    supplies that evidence. Evaluator statuses and non-allocation values are
    otherwise shown as written.
    """
    if dataset.status == "unavailable":
        return f"Unavailable: {dataset.message}", ()
    if dataset.status == "incomplete":
        return f"Incomplete: {dataset.message}", ()
    if dataset.status == "empty" or not dataset.rows:
        return "Empty: evaluation summary has no metric rows.", ()

    output: list[dict[str, Any]] = []
    allowed_statuses = {"ok", "partial", "unavailable"}
    for index, row in enumerate(dataset.rows, start=2):
        metric = row.get("metric", "").strip()
        scope = row.get("scope", "").strip()
        status = row.get("status", "").strip()
        try:
            if not metric or not scope or status not in allowed_statuses:
                raise ValueError("metric, scope, and a recognized status are required")
            raw = row.get("value", "").strip()
            value: float | None
            if status == "unavailable" or not raw:
                value = None
            else:
                value = float(raw)
                if not math.isfinite(value):
                    raise ValueError("value must be finite")
            observations = int(row.get("observations", ""))
            if observations < 0:
                raise ValueError("observations must be non-negative")
        except (TypeError, ValueError):
            return (f"Incomplete: invalid evaluation summary row {index}.", ())

        reason = ""
        display_status = status
        if status != "unavailable" and value is None:
            display_status = "unavailable"
            reason = "No value was recorded."
        if _is_allocation_metric(metric) and not allocation_results_verified:
            value = None
            display_status = "unavailable"
            reason = "Allocation provenance and required inputs are not verified."
        details = row.get("details", "")
        if details:
            try:
                parsed_details = json.loads(details)
                if isinstance(parsed_details, dict):
                    reason = reason or str(parsed_details.get("reason", ""))
            except (TypeError, json.JSONDecodeError):
                return (f"Incomplete: invalid evaluation summary details at row {index}.", ())
        output.append({
            "Metric": metric,
            "Scope": scope,
            "Value": "Unavailable" if value is None else value,
            "Unit": row.get("unit", ""),
            "Status": display_status,
            "Observations": observations,
            "Details": reason,
        })
    return f"Available: {len(output)} evaluation metric rows from {dataset.source_label}.", tuple(output)


def _show_map(st: Any, divisions: Dataset, risk_records: Sequence[Mapping[str, Any]]) -> None:
    points = _valid_coordinates(divisions.rows)
    if not points:
        st.info("Map unavailable: no valid division coordinates are available. No locations are inferred.")
        return
    try:
        import folium
        from streamlit.components.v1 import html
    except ImportError as exc:
        st.info(f"Map rendering unavailable because Folium/Streamlit is not installed: {exc}")
        return
    st.caption("Map markers are approximate division town points, not well GPS locations.")
    tiers = {r.get("ds_division"): r.get("tier") for r in risk_records}
    color = {"Excellent": "#027a48", "Good": "#175cd3", "Poor": "#b42318",
             "Very Poor": "#e76f00", "Unsuitable": "#9b1c1c", "Unknown": "#667085"}
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


def _show_risks(st: Any, records: Sequence[Mapping[str, Any]], status: str,
                month_index: int | None) -> None:
    if month_index is None:
        st.info("Monthly risk records are unavailable; no simulated month index is present.")
        return
    selected = []
    for row in records:
        try:
            matches = int(row.get("month_index", -1)) == month_index
        except (TypeError, ValueError):
            matches = False
        if matches:
            selected.append(row)
    if not selected:
        st.info(f"No risk records are available for month_index {month_index}. {status}")
        return
    st.caption(status)
    st.caption("Hotspot status is not present in Member B risk-score outputs and is not inferred here.")
    st.caption("Unknown means no WQI score could be computed; it is not a safe tier.")
    display = []
    for row in selected:
        try:
            drivers = _parse_list_cell(row.get("drivers", "[]"), "drivers")
        except (ValueError, SyntaxError):
            drivers = "Unavailable (malformed drivers)"
        display.append({
            "Division": row.get("ds_division"), "Tier": row.get("tier", "Unknown"),
            "WQI": row.get("wqi") or "Unavailable", "Drivers": drivers,
            "Source": row.get("source", "unspecified"),
            "Share Poor or worse": row.get("share_poor_or_worse", "Unavailable"),
        })
    st.dataframe(display, use_container_width=True)


def _show_allocations(st: Any, rows: Sequence[AllocationRow], month: int | None,
                      metrics: Sequence[MetricResult] = (), label: str = "") -> None:
    if label:
        st.caption(label)
    if month is None:
        st.info("Allocation rows are unavailable; no allocation calendar month is present.")
    else:
        selected = allocation_rows_for_month(rows, month)
        if not selected:
            st.info(f"No allocation rows are available for calendar month {month}.")
        else:
            st.dataframe([{
                "Method": r.method, "Division": r.ds_division, "Resource": r.resource,
                "Round": r.round, "Winning bid": r.winning_bid,
            } for r in selected], use_container_width=True)
    month_results = ([m for m in metrics if f"month={month}" in m.scope.split(";")]
                     if month is not None else [])
    if not month_results:
        st.info("Efficiency comparisons unavailable: no matching verified evaluation rows are present.")
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
        risk_scores_path=logs_root / "risk_scores.csv", well_scores_path=logs_root / "well_scores.csv",
        allocations_path=logs_root / "allocations.csv", evaluation_path=logs_root / "evaluation_summary.csv",
    )
    view = st.radio("Data view", ("Team outputs", "Illustrative demo fixtures"), horizontal=True)
    demo = view.startswith("Illustrative")
    risk_months = available_months(data, demo=demo)
    selected_index = st.selectbox("Simulated month index", risk_months if risk_months else
                                  ["Unavailable — no risk month_index"], disabled=not risk_months)
    month_index = selected_index if isinstance(selected_index, int) else None
    allocation_months = available_allocation_months(data, demo=demo)
    selected_allocation_month = st.selectbox(
        "Allocation calendar month (separate from simulated month_index)",
        allocation_months if allocation_months else ["Unavailable — no allocation month"],
        disabled=not allocation_months,
    )
    allocation_month = selected_allocation_month if isinstance(selected_allocation_month, int) else None

    if demo:
        risks, allocations, metrics = data.demo_risks, data.demo_allocations, data.demo_metrics
        st.info(data.demo_status)
        risk_status = data.demo_status
        allocation_label = "Illustrative fixture allocation rows; not team outcomes or scientific results."
    else:
        risks, allocations = data.team_risks, data.team_allocations
        # Use the evaluator's allocation scope, but withhold numeric allocation
        # results while the loaded Member C rows remain unverified.
        metrics = _metrics_for_month(
            data.team_metrics, allocation_month or -1, allocation_results_verified=False,
        )
        risk_status = f"{data.team_risk_status}; {data.team_risk_source_label}"
        allocation_label = data.team_allocation_status

    if not demo:
        evaluation_status, evaluation_rows = prepare_evaluation_display(
            data.team_metrics, allocation_results_verified=False,
        )
        st.subheader("Evaluation summary")
        st.caption("Evaluator outputs are reported as recorded; allocation metrics stay unavailable until their inputs and provenance are verified.")
        st.caption(evaluation_status)
        if evaluation_rows:
            st.dataframe(list(evaluation_rows), use_container_width=True)
        else:
            st.info(evaluation_status)

    left, right = st.columns((3, 2))
    with left:
        st.subheader("Division risk map")
        map_risks = []
        for row in risks:
            try:
                if month_index is not None and int(row.get("month_index", -1)) == month_index:
                    map_risks.append(row)
            except (TypeError, ValueError):
                continue
        _show_map(st, data.divisions, map_risks)
    with right:
        st.subheader("Risk tiers and drivers")
        _show_risks(st, risks, risk_status, month_index)

    st.subheader("Monthly allocation file rows and comparisons")
    if not demo and data.team_allocation_status.startswith("Present but unverified"):
        st.warning("Allocation file is present, but its Member C provenance is unverified. Rows below are not presented as confirmed team outcomes.")
    _show_allocations(st, allocations, allocation_month, metrics, allocation_label)

    with st.expander("Member B per-well scores"):
        st.caption(data.team_well_status)
        st.caption(data.team_well_source_label)
        st.caption("Source labels are preserved per row; synthetic scores are not measured observations.")
        if data.team_wells.rows:
            selected_wells = []
            for row in data.team_wells.rows:
                try:
                    if month_index is not None and int(row["month_index"]) == month_index:
                        selected_wells.append(row)
                except (TypeError, ValueError):
                    continue
            if selected_wells:
                st.dataframe(selected_wells, use_container_width=True)
            else:
                st.info("No well-score rows for the selected simulated month index.")
        else:
            st.info(data.team_well_status)

    with st.expander("Synthetic well input observations (seasonal; not monthly risk tiers)"):
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
        st.caption("Published reference sampling-design coordinates are not treated as well locations.")


if __name__ == "__main__":
    main()
