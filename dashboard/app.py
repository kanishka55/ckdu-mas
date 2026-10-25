"""Streamlit view for Member B risk outputs and published reference data.

Data preparation is independent of Streamlit/Folium. Member B's generated
``risk_scores.csv`` and ``well_scores.csv`` are read only when present and
validated against their writer's fields. Test/demo fixtures remain separate
from team outputs. No month-index-to-calendar mapping is inferred.
"""

from __future__ import annotations

import ast
import csv
import html
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.sim.evaluate import (
    AllocationRow,
    EvaluationInputError,
    MetricResult,
    calculate_allocation_efficiency,
    read_allocation_rows,
)
from src.sim.agent_monitor import AgentMonitorSnapshot, load_agent_monitor
from src.sim.simulation_lab import (
    HOTSPOT_TRIGGER_TIERS,
    SAMPLE_INPUT,
    SimulationSettings,
    default_lab_root,
    get_run_status,
    launch_simulation,
    list_lab_runs,
    read_process_log_tail,
    resolve_hotspot_configuration,
    validate_input_bytes,
    validate_input_path,
)
from src.sim.run_details import (
    SimulationRunDetails,
    agent_execution_details,
    build_execution_view,
    combined_timeline,
    filter_evidence,
    filter_messages,
    load_simulation_run_details,
)
from src.core.ontology import RESOURCES

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
DEMO_ALLOCATION_COLUMNS = (
    "provenance", "month_index", "resource", "resource_pool_size", "ds_division",
    "tier", "population", "method", "status", "units", "bid", "rank", "reason",
)
DEMO_HOTSPOT_COLUMNS = (
    "provenance", "month_index", "ds_division", "rule_id", "status",
    "evidence_month_indexes", "reason",
)
DEMO_ALERT_COLUMNS = (
    "provenance", "month_index", "ds_division", "tier", "hotspot_status",
    "status", "reason", "message", "payload",
)


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
    team_wells: Dataset
    team_well_status: str
    team_well_source_label: str
    team_allocations: tuple[AllocationRow, ...]
    team_allocation_status: str
    team_metrics: Dataset
    demo_risks: tuple[dict[str, Any], ...]
    demo_allocations: tuple[AllocationRow, ...]
    demo_metrics: tuple[MetricResult, ...]
    demo_status: str


@dataclass(frozen=True)
class LocalDemoData:
    """Generated local-demo artifacts, loaded independently of team output paths."""

    root: Path
    manifest: Mapping[str, Any] | None
    manifest_status: str
    divisions: Dataset
    risks: Dataset
    hotspots: Dataset
    allocations: Dataset
    alerts: Dataset
    metrics: Dataset


def load_local_demo_data(output_root: str | Path) -> LocalDemoData:
    """Load only generated demo artifacts beneath the explicitly selected root."""
    root = Path(output_root).expanduser()
    manifest: Mapping[str, Any] | None = None
    try:
        parsed = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
        if not isinstance(parsed, dict) or parsed.get("provenance") != "illustrative_local_demo":
            raise ValueError("manifest provenance is not illustrative_local_demo")
        manifest = parsed
        manifest_status = "Available: illustrative local-demo provenance manifest"
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        manifest_status = f"Unavailable or incomplete manifest: {exc}"
    risks_raw = _read_csv(root / "logs/risk_scores.csv", source_label="Synthetic local-demo risk scores")
    risks = _validate_indexed_scores(risks_raw, RISK_SCORE_REQUIRED, wells=False)
    return LocalDemoData(
        root=root,
        manifest=manifest,
        manifest_status=manifest_status,
        divisions=_read_csv(root / "division_points.csv", DIVISION_COLUMNS,
                            "Published division coordinates copied into the local demo; approximate town points"),
        risks=risks,
        hotspots=_read_csv(root / "hotspot_screening.csv", DEMO_HOTSPOT_COLUMNS,
                            "Illustrative project-defined hotspot screening"),
        allocations=_read_csv(root / "allocation_decisions.csv", DEMO_ALLOCATION_COLUMNS,
                              "Illustrative local allocation; not Member C or verified team output"),
        alerts=_read_csv(root / "alerts.csv", DEMO_ALERT_COLUMNS,
                         "Illustrative local alert decisions; only emitted alerts are logged"),
        metrics=_read_csv(root / "logs/evaluation_summary.csv", EVALUATION_COLUMNS,
                          "Evidence-gated local demo evaluator summary"),
    )


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
        well_dataset,
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
        classification = ""
        missing_dependency = ""
        required_action = ""
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
                    classification = str(parsed_details.get("classification", ""))
                    missing_dependency = str(parsed_details.get("missing_dependency", ""))
                    required_action = str(parsed_details.get("required_action", ""))
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
            "Availability type": classification or ("evidence" if display_status == "unavailable" else ""),
            "Missing dependency": missing_dependency,
            "Required action": required_action,
        })
    return f"Available: {len(output)} evaluation metric rows from {dataset.source_label}.", tuple(output)


def _render_evaluation_summary(
    st: Any, dataset: Dataset, *, allocation_results_verified: bool = False,
    context: str = "",
) -> None:
    """Render the evaluation status once, or the available summary rows."""
    status, rows = prepare_evaluation_display(
        dataset, allocation_results_verified=allocation_results_verified,
    )
    st.subheader("Evaluation summary")
    if context:
        st.caption(context)
    if rows:
        st.caption(status)
        st.dataframe(list(rows), use_container_width=True)
    else:
        st.info(status)


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


def _show_demo_rows(st: Any, rows: Sequence[Mapping[str, Any]], empty_message: str) -> None:
    """Render demo records or an explicit empty state via ordinary Streamlit calls."""
    if rows:
        st.dataframe(list(rows), use_container_width=True)
    else:
        st.info(empty_message)


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


def _render_local_demo(st: Any, output_root: Path) -> None:
    """Render generated artifacts without loading or relabeling team outputs."""
    demo = load_local_demo_data(output_root)
    st.subheader("Generated local demo")
    st.warning("Synthetic and illustrative simulation outputs; not verified team results or measured observations.")
    st.caption(demo.manifest_status)
    if demo.manifest is not None:
        stages = demo.manifest.get("stages", {})
        st.caption(f"Risk-scoring stage: {stages.get('risk_scoring', 'status unavailable')}")
        st.caption("The input has seasonal readings expanded by the documented rule; repeated rows are not new monthly measurements.")
        st.caption("Reduced-parameter WQI is not directly comparable with a full 13-parameter WQI.")
        st.caption(str(demo.manifest.get("policies", {}).get("hotspot", {}).get(
            "status", "Hotspot rule provenance unavailable.")))
        st.caption(str(demo.manifest.get("policies", {}).get("allocation", {}).get(
            "implementation", "Allocation implementation provenance unavailable.")))

    months = sorted({int(row["month_index"]) for row in demo.risks.rows
                     if row.get("month_index", "").isdigit()})
    selected = st.selectbox("Simulated month_index", months if months else
                            ["Unavailable — no risk month_index"], disabled=not months)
    month_index = selected if isinstance(selected, int) else None
    selected_risks = [row for row in demo.risks.rows
                      if month_index is not None and row.get("month_index") == str(month_index)]
    left, right = st.columns((3, 2))
    with left:
        st.subheader("Division risk map")
        _show_map(st, demo.divisions, selected_risks)
        st.caption("Map coordinates are approximate division town points, not well GPS locations.")
    with right:
        st.subheader("Risk tiers and drivers")
        _show_risks(st, demo.risks.rows, f"{demo.risks.source_label}: {demo.risks.message}", month_index)

    st.subheader("Project-defined hotspot screening")
    st.caption("This illustrative screening indicator is not validated medical or epidemiological evidence. Missing/Unknown input remains unavailable.")
    if demo.hotspots.status == "available" and month_index is not None:
        rows = [row for row in demo.hotspots.rows if row.get("month_index") == str(month_index)]
        _show_demo_rows(st, rows, "No hotspot rows for the selected simulated month_index.")
    else:
        st.info(f"{demo.hotspots.status.title()}: {demo.hotspots.message}")

    st.subheader("Illustrative local allocation decisions")
    st.caption(f"{demo.allocations.source_label}. These rows are not Member C outputs and do not establish verified allocation efficiency.")
    if demo.allocations.status == "available" and month_index is not None:
        rows = [row for row in demo.allocations.rows if row.get("month_index") == str(month_index)]
        _show_demo_rows(st, rows, "No allocation rows for the selected simulated month_index.")
    else:
        st.info(f"{demo.allocations.status.title()}: {demo.allocations.message}")

    st.subheader("Alert decisions")
    st.caption("Only emitted decisions are sent alerts. Suppressed and unavailable outcomes are internal decisions.")
    if demo.alerts.status == "available" and month_index is not None:
        rows = [row for row in demo.alerts.rows if row.get("month_index") == str(month_index)]
        _show_demo_rows(st, rows, "No alert decision rows for the selected simulated month_index.")
    else:
        st.info(f"{demo.alerts.status.title()}: {demo.alerts.message}")

    _render_evaluation_summary(
        st, demo.metrics, allocation_results_verified=False,
        context="Allocation-comparison metrics remain unavailable for illustrative local allocation results.",
    )


def _render_agent_monitor_snapshot(st: Any, snapshot: AgentMonitorSnapshot) -> None:
    st.subheader("Live Agent Monitor")
    st.caption(f"Run: {snapshot.run_id} · {snapshot.run_dir}")
    st.caption(f"Source: explicitly selected SPADE simulation run · {snapshot.freshness} · run status: {snapshot.run_status}")
    if snapshot.freshness == "live":
        st.success("The runner heartbeat is fresh. Each agent's state is derived separately from its own events.")
    elif snapshot.freshness == "stale":
        st.warning("Run manifest still says running, but no recent heartbeat or message evidence exists. Agent activity is stale/unknown.")
    elif snapshot.freshness == "failed snapshot":
        st.error("This run failed. The monitor is showing its persisted failure snapshot.")
    elif snapshot.freshness == "completed snapshot":
        st.info("This run is complete; this view is a persisted snapshot, not live agent activity.")
    else:
        st.warning("Run freshness is unknown; no active state is inferred.")
    st.caption(f"Last observed run activity: {snapshot.last_activity or 'Unavailable'}")
    st.caption("An agent is running or waiting only with a recent heartbeat from that agent identity. Heartbeats show event-loop liveness, not domain correctness or external XMPP health.")

    st.markdown("#### Observed agents")
    if snapshot.agents:
        st.dataframe(list(snapshot.agents), use_container_width=True)
    else:
        st.info("No agent lifecycle identities have been recorded for this run.")
    with st.expander("Lifecycle events"):
        if snapshot.lifecycle_events:
            st.dataframe([{
                "Time": row.get("timestamp", "Unavailable"),
                "Component": row.get("component", "Unknown"),
                "Identity": row.get("identity", "Unavailable"),
                "Event": row.get("event_type", "Unknown"),
                "State": row.get("lifecycle_state", "Unavailable"),
            } for row in snapshot.lifecycle_events], use_container_width=True)
        else:
            st.info("No lifecycle events are available.")

    st.markdown("#### Message activity")
    st.caption(f"Seven-column message log: {snapshot.message_status}. Sent/received counts come from the logger's direction field.")
    if snapshot.message_counts:
        st.dataframe(list(snapshot.message_counts), use_container_width=True)
    else:
        st.info("No directional message counts are available; an empty log is not evidence of successful communication.")
    with st.expander("Message flows and recent messages"):
        if snapshot.flows:
            st.dataframe(list(snapshot.flows), use_container_width=True)
        else:
            st.info("No sender/receiver flows are present in this run log.")
        if snapshot.messages:
            st.dataframe([{
                "Timestamp": row.get("timestamp"), "Sender": row.get("sender"),
                "Receiver": row.get("receiver"), "Direction": row.get("direction"),
                "Performative": row.get("performative"), "Message type": row.get("event_type"),
                "month_index": row.get("month_index"), "Division": row.get("ds_division"),
                "Payload validation": row.get("payload_status"),
            } for row in snapshot.messages], use_container_width=True)
        else:
            st.info("No recent messages are available.")

    st.markdown("#### Month progression and Contract Net")
    if snapshot.months:
        st.dataframe(list(snapshot.months), use_container_width=True)
    else:
        st.info("No month progression events are available yet.")
    if snapshot.rounds:
        st.dataframe([{
            "month_index": row.get("month_index"), "Round": row.get("round"),
            "Resource": row.get("resource"), "Award status": row.get("award_status"),
            "Winner": row.get("winner") if row.get("award_status") == "confirmed" else "Not confirmed",
        } for row in snapshot.rounds], use_container_width=True)
    else:
        st.info(f"Contract Net outcomes {snapshot.rounds_status}: no persisted round result is available.")

    st.markdown("#### Hotspot screening")
    st.caption("Project-defined provisional screening only; this is not validated epidemiological evidence. Unknown/unavailable is not inactive.")
    if snapshot.hotspots:
        st.dataframe([{
            "month_index": row.get("month_index"), "Division": row.get("ds_division"),
            "Status": row.get("status", "Unavailable"), "Rule": row.get("rule_id"),
            "Reason": row.get("reason"),
        } for row in snapshot.hotspots], use_container_width=True)
    else:
        st.info(f"Hotspot outputs {snapshot.hotspots_status}; no screening result is available.")

    st.markdown("#### Alert decisions and transport")
    st.caption("Confirmed means received by the run-local simulated inbox only, not an external health officer.")
    if snapshot.alerts:
        st.dataframe([{
            "month_index": row.get("month_index"), "Division": row.get("ds_division"),
            "Tier": row.get("tier", "Unavailable"), "Hotspot": row.get("hotspot_status", "Unavailable"),
            "Decision": row.get("status", "Unavailable"),
            "Transport": row.get("delivery_status", "Unavailable"),
            "Reason": row.get("reason", row.get("delivery_reason", "")),
        } for row in snapshot.alerts], use_container_width=True)
    else:
        st.info(f"Alert outcomes {snapshot.alerts_status}; no decision record is available.")

    evaluation = _read_csv(
        snapshot.run_dir / "logs" / "evaluation_summary.csv",
        EVALUATION_COLUMNS,
        "Persisted evaluator summary for this selected SPADE run",
    )
    _render_evaluation_summary(
        st, evaluation, allocation_results_verified=False,
        context=("Allocation comparisons remain unavailable unless provenance, required inputs, "
                 "baselines, and the risk-weight definition are verified."),
    )
    if snapshot.errors:
        with st.expander("Incomplete monitor data"):
            for error in snapshot.errors:
                st.warning(error)


def _render_agent_monitor(st: Any, run_dir: Path) -> None:
    snapshot = load_agent_monitor(run_dir)
    _render_agent_monitor_snapshot(st, snapshot)


def _lab_run_history_rows(lab_root: Path) -> tuple[dict[str, Any], ...]:
    """Create a compact history table from lab control records and each run's manifest."""
    output = []
    for control in list_lab_runs(lab_root):
        run_dir = Path(control["run_dir"]).expanduser().resolve()
        try:
            run_dir.relative_to((lab_root.expanduser().resolve() / "runs").resolve())
        except ValueError:
            continue
        manifest: Mapping[str, Any] = {}
        try:
            parsed = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                manifest = parsed
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        config = manifest.get("configuration", {})
        if not isinstance(config, Mapping):
            config = {}
        settings = control.get("settings", {})
        if not isinstance(settings, Mapping):
            settings = {}
        start = config.get("start_month_index", settings.get("start_month_index"))
        end = config.get("end_month_index", settings.get("end_month_index"))
        month_range = f"{start}–{end}" if start is not None and end is not None else "Unavailable"
        provenance = manifest.get("provenance", {})
        provenance_label = (provenance.get("classification", "synthetic / provenance unavailable")
                            if isinstance(provenance, Mapping) else "synthetic / provenance unavailable")
        output.append({
            "Run ID": control.get("run_id", "Unknown"),
            "Status": _display_run_status(control.get("status", "unknown")),
            "Started": manifest.get("started_at", control.get("created_at", "Unavailable")),
            "Completed": manifest.get("completed_at", "Unavailable"),
            "Input dataset / provenance": (
                f"{control.get('input_source', 'Input unavailable')} · {provenance_label}"),
            "Input records": control.get("input_record_count", manifest.get("input_record_count", "Unavailable")),
            "month_index range": month_range,
            "Resource": config.get("resource", settings.get("resource", "Unavailable")),
            "Pool": config.get("resource_pool", settings.get("resource_pool", "Unavailable")),
            "_run_dir": run_dir,
            "_control_path": lab_root.expanduser().resolve() / "control" / f"{control.get('run_id')}.json",
        })
    return tuple(output)


def _display_run_status(status: Any) -> str:
    return {
        "completed_with_unavailable_stages": "completed with unavailable stages",
    }.get(str(status), str(status).replace("_", " "))


def _selected_run_id(selection: Any, rows: Sequence[Mapping[str, Any]]) -> str | None:
    """Extract the selected dataframe row across Streamlit's selection result wrappers."""
    selected = getattr(getattr(selection, "selection", None), "rows", None)
    if selected is None and isinstance(selection, Mapping):
        selected = selection.get("selection", {}).get("rows", [])
    if not selected:
        return None
    try:
        index = int(selected[0])
        return str(rows[index].get("Run ID")) if 0 <= index < len(rows) else None
    except (TypeError, ValueError, IndexError):
        return None


def _artifact_display(rows: Sequence[Mapping[str, Any]], *, limit: int = 100) -> list[dict[str, Any]]:
    """Convert JSON/CSV artifact records to Streamlit-safe display rows."""
    return [dict(row) for row in rows[:limit]]


def _render_page_table(st: Any, label: str, rows: Sequence[Mapping[str, Any]], *, key: str,
                       page_size: int = 100) -> None:
    total = len(rows)
    if not total:
        st.info(f"{label}: no matching records are available.")
        return
    pages = max(1, (total + page_size - 1) // page_size)
    page = 1
    if pages > 1:
        page = int(st.number_input(f"{label} page (of {pages})", min_value=1, max_value=pages,
                                   value=1, step=1, key=f"{key}_page"))
    start = (page - 1) * page_size
    st.caption(f"Showing {start + 1}–{min(start + page_size, total)} of {total} records.")
    st.dataframe(list(rows[start:start + page_size]), use_container_width=True, hide_index=True)


def _format_run_duration(manifest: Mapping[str, Any]) -> str:
    def parse(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    started, ended = parse(manifest.get("started_at")), parse(manifest.get("completed_at"))
    if started is None:
        return "Unavailable until the start timestamp is recorded"
    if ended is None:
        if manifest.get("status") != "running":
            return "Unavailable until a completion timestamp is recorded"
        ended = datetime.now(timezone.utc)
        running = True
    else:
        running = False
    seconds = max(0.0, (ended - started).total_seconds())
    return f"{seconds:.1f} seconds" + (" elapsed (running)" if running else "")


def _pretty_event_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    fields = ("timestamp", "event_seq", "evidence_source", "component", "identity", "event_type",
              "month_index", "month", "ds_division", "round", "thread_id", "phase", "stage",
              "state", "outcome", "status", "screening_status", "award_status",
              "decision_status", "delivery_status", "severity", "explanation",
              "reason", "error", "behavior_failures", "performative", "direction",
              "sender", "receiver", "resource")
    return [{field: row.get(field, "Unavailable") for field in fields} for row in rows]


def _render_agent_execution(st: Any, details: SimulationRunDetails) -> None:
    st.subheader("Agent-by-agent execution")
    st.caption("Lifecycle and work status come from recorded per-agent events. Runner-observed stage summaries are shown separately. A heartbeat is liveness evidence, not proof of correct behavior.")
    agents = agent_execution_details(details)
    if not agents:
        st.info("No agent identities were observed in this run. No agent activity is inferred from class or configuration names.")
        return
    for agent in agents:
        title = f"{agent['component']} · {agent['identity']} · {agent['status']}"
        with st.expander(title):
            st.write(agent["responsibility"])
            st.caption(f"Status evidence: {agent['status_evidence']}")
            st.dataframe([{
                "First observed": agent["first_observed"] or "Unavailable",
                "Last observed": agent["last_observed"] or "Unavailable",
                "Latest heartbeat": agent["latest_heartbeat"],
                "Heartbeat age (s)": agent["heartbeat_age_s"] if agent["heartbeat_age_s"] is not None else "Unavailable",
                "Latest phase": agent["latest_phase"],
                "Sent log rows": agent["sent_rows"], "Received log rows": agent["received_rows"],
            }], use_container_width=True, hide_index=True)
            phases = agent["phase_history"]
            st.markdown("**Phase history**")
            if phases:
                st.dataframe(_pretty_event_rows(phases), use_container_width=True, hide_index=True)
            else:
                st.info("No explicit phase evidence is recorded for this identity.")
            st.markdown("**Work, decisions, outputs, and failures**")
            if agent["events"]:
                st.dataframe(_pretty_event_rows(agent["events"]), use_container_width=True, hide_index=True)
            else:
                st.info("No agent event records are available.")
            if agent["failures"]:
                st.error("Failure evidence is present; inspect the event details above.")
            st.markdown("**Messages involving this identity**")
            if agent["messages"]:
                st.dataframe([{
                    "timestamp": row.get("timestamp"), "sender": row.get("sender"),
                    "receiver": row.get("receiver"), "performative": row.get("performative"),
                    "thread_id": row.get("thread_id"), "direction": row.get("direction"),
                    "type": row.get("message_type"), "month_index": row.get("month_index"),
                    "round": row.get("round"),
                } for row in agent["messages"]], use_container_width=True, hide_index=True)
            else:
                st.info("No message-log rows involve this identity.")


def _render_run_timeline(st: Any, details: SimulationRunDetails) -> None:
    st.subheader("Chronological simulation timeline")
    st.caption("Journal and message-log records are independent evidence sources. They are not merged or deduplicated; event severity is a display label derived from explicit event/performative names.")
    rows = combined_timeline(details)
    month_values = sorted({str(row.get("month_index")) for row in rows if row.get("month_index") is not None})
    agent_values = sorted({str(value) for row in rows for value in
                           (row.get("identity"), row.get("component"), row.get("sender"), row.get("receiver"))
                           if value})
    type_values = sorted({str(row.get("event_type", "unknown")) for row in rows})
    c1, c2, c3, c4 = st.columns(4)
    month = c1.selectbox("Timeline month_index", ("All", *month_values), key="run_detail_timeline_month")
    agent = c2.selectbox("Timeline agent/component", ("All", *agent_values), key="run_detail_timeline_agent")
    event_type = c3.selectbox("Timeline event type", ("All", *type_values), key="run_detail_timeline_type")
    severity = c4.selectbox("Timeline severity", ("All", "info", "warning", "error"), key="run_detail_timeline_severity")
    query = st.text_input("Search timeline evidence", key="run_detail_timeline_query")
    filtered = filter_evidence(rows, month=month, agent=agent, event_type=event_type,
                               severity=severity, query=query)
    _render_page_table(st, "Timeline", _pretty_event_rows(filtered), key="run_detail_timeline")


def _render_message_inspector(st: Any, details: SimulationRunDetails) -> None:
    st.subheader("SPADE message inspector")
    st.caption(f"Message log status: {details.message_status}. The logger has no stable per-message ID. Sent and received copies and repeated rows are preserved as recorded; no ambiguous rows are deduplicated.")
    if not details.messages:
        st.info("No valid seven-column SPADE message rows are available. Incomplete or missing logs are not reconstructed.")
        return
    months = sorted({str(row.get("month_index")) for row in details.messages if row.get("month_index") is not None})
    senders = sorted({str(row.get("sender", "")) for row in details.messages})
    receivers = sorted({str(row.get("receiver", "")) for row in details.messages})
    performatives = sorted({str(row.get("performative", "")) for row in details.messages})
    rounds = sorted({str(row.get("round")) for row in details.messages if row.get("round") is not None})
    threads = sorted({str(row.get("thread_id", "")) for row in details.messages if row.get("thread_id")})
    a, b, c, d = st.columns(4)
    month = a.selectbox("Message month_index", ("All", *months), key="run_detail_message_month")
    sender = b.selectbox("Sender", ("All", *senders), key="run_detail_message_sender")
    receiver = c.selectbox("Recipient", ("All", *receivers), key="run_detail_message_receiver")
    performative = d.selectbox("Performative", ("All", *performatives), key="run_detail_message_performative")
    e, f = st.columns(2)
    round_filter = e.selectbox("Message round", ("All", *rounds), key="run_detail_message_round")
    thread_filter = f.selectbox("Message thread", ("All", *threads), key="run_detail_message_thread")
    direction = st.selectbox("Direction", ("All", "sent", "received"), key="run_detail_message_direction")
    query = st.text_input("Search message fields and payload", key="run_detail_message_query")
    filtered = filter_messages(details.messages, query=query, month=month, sender=sender,
                               receiver=receiver, performative=performative, direction=direction)
    filtered = tuple(row for row in filtered
                     if (round_filter == "All" or str(row.get("round")) == round_filter)
                     and (thread_filter == "All" or row.get("thread_id") == thread_filter))
    display = [{
        "timestamp": row.get("timestamp"), "sender": row.get("sender"),
        "receiver": row.get("receiver"), "performative": row.get("performative"),
        "thread_id": row.get("thread_id"), "message_type": row.get("message_type"),
        "month_index": row.get("month_index"), "round": row.get("round"),
        "direction": row.get("direction"), "payload_status": row.get("payload_status"),
    } for row in filtered]
    _render_page_table(st, "Messages", display, key="run_detail_messages")
    if not filtered:
        return
    index = st.selectbox("Open recorded message", range(len(filtered)),
                         format_func=lambda i: (f"{filtered[i].get('timestamp')} · "
                                                f"{filtered[i].get('sender')} → {filtered[i].get('receiver')} · "
                                                f"{filtered[i].get('performative')} · {filtered[i].get('thread_id')}"),
                         key="run_detail_selected_message")
    chosen = filtered[int(index)]
    st.markdown("**Full recorded payload**")
    st.code(str(chosen.get("payload", "")), language="json")
    if chosen.get("decoded_payload") is not None:
        st.json(chosen["decoded_payload"])
    else:
        st.warning(f"Payload could not be decoded: {chosen.get('payload_status')}")
    thread = chosen.get("thread_id")
    if thread:
        related = [row for row in details.events if row.get("thread_id") == thread]
        related.extend(row for row in details.messages if row.get("thread_id") == thread)
        st.markdown(f"**Recorded evidence sharing thread_id {thread!r}**")
        st.caption("A thread may contain multiple messages; this is not proof that any two rows are copies of the same message.")
        st.dataframe(_pretty_event_rows(related), use_container_width=True, hide_index=True) if related else st.info("No other records with this exact thread_id are present.")


def _month_rows(rows: Sequence[Mapping[str, Any]], month_index: int) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if str(row.get("month_index", "")) == str(month_index)]


def _render_round_inspector(st: Any, round_row: Mapping[str, Any],
                           messages: Sequence[Mapping[str, Any]] = ()) -> None:
    title = (f"month_index={round_row.get('month_index')} · round={round_row.get('round')} · "
             f"{round_row.get('award_status', 'status unavailable')}")
    with st.expander(title):
        st.caption(f"Thread/correlation ID: {round_row.get('thread_id', 'Unavailable')} · resource: {round_row.get('resource', 'Unavailable')} · pool: {round_row.get('resource_pool', 'Unavailable')}")
        st.caption("The coordinator normalizes raw bids by the maximum and ranks highest normalized need first; ties use alphabetical division name. The round policy allows at most one unit per village. A selected award is not confirmed until a matching village confirmation is recorded.")
        thread = round_row.get("thread_id")
        protocol_messages = [row for row in messages if thread and row.get("thread_id") == thread]
        st.markdown("**Observed protocol messages for this thread**")
        if protocol_messages:
            st.dataframe([{
                "timestamp": row.get("timestamp"), "sender": row.get("sender"),
                "receiver": row.get("receiver"), "performative": row.get("performative"),
                "direction": row.get("direction"), "type": row.get("message_type"),
                "month_index": row.get("month_index"), "round": row.get("round"),
                "thread_id": row.get("thread_id"), "payload": row.get("payload"),
            } for row in protocol_messages], use_container_width=True, hide_index=True)
        else:
            st.info("No message-log rows with this exact round thread_id are available; the round JSON remains a persisted coordinator result.")
        states = round_row.get("state_history", [])
        st.write({"Phase history": states if isinstance(states, list) else "Unavailable",
                  "Selected winner": round_row.get("selected_winner"),
                  "Confirmed winner": round_row.get("winner"),
                  "Allocation successful": round_row.get("allocation_successful", "Unavailable")})
        for key, label in (("proposals", "Eligible proposals"), ("refusals", "Refusals"),
                           ("ineligible_proposals", "Ineligible proposals"),
                           ("selected_awardees", "Selected awardees and confirmation state"),
                           ("missing_recipients", "Missing recipients"),
                           ("timed_out_recipients", "Proposal timeouts"),
                           ("confirmation_timeouts", "Confirmation timeouts"),
                           ("malformed_replies", "Malformed replies"),
                           ("unmatched_replies", "Unmatched replies"),
                           ("protocol_errors", "Protocol errors")):
            st.markdown(f"**{label}**")
            value = round_row.get(key)
            if value:
                if isinstance(value, (dict, list)):
                    st.json(value)
                else:
                    st.write(value)
            else:
                st.caption("No records in this artifact.")
        if round_row.get("award_status") in {"confirmed", "partially_confirmed"}:
            st.success("Confirmed award evidence is present for the listed village(s).")
        elif round_row.get("selected_awardees"):
            st.warning("Awardees were selected, but the result does not confirm every selected award.")


def _render_monthly_details(st: Any, details: SimulationRunDetails) -> None:
    st.subheader("Month-by-month results")
    manifest = details.manifest or {}
    month_rows = manifest.get("month_status", [])
    if not isinstance(month_rows, list):
        month_rows = []
    indexes = sorted({int(row["month_index"]) for row in month_rows
                      if isinstance(row, Mapping) and isinstance(row.get("month_index"), int)} |
                     {int(row["month_index"]) for row in details.risk_records.rows
                      if isinstance(row.get("month_index"), int)} |
                     {int(row["month_index"]) for row in details.well_scores.rows
                      if str(row.get("month_index", "")).isdigit()} |
                     {int(row["month_index"]) for row in details.hotspots.rows
                      if isinstance(row.get("month_index"), int)} |
                     {int(row["month_index"]) for row in details.alerts.rows
                      if isinstance(row.get("month_index"), int)} |
                     {int(row["month_index"]) for row in details.rounds.rows
                      if isinstance(row.get("month_index"), int)} |
                     {int(row["month_index"]) for row in details.events
                      if isinstance(row.get("month_index"), int)} |
                     {int(row["month_index"]) for row in details.messages
                      if isinstance(row.get("month_index"), int)})
    if not indexes:
        st.info("No month-indexed results are available yet.")
        return
    for index in indexes:
        with st.expander(f"month_index {index}"):
            month_status = next((row for row in month_rows if row.get("month_index") == index), {})
            if not month_status:
                started_event = next((event for event in details.events
                                      if event.get("event_type") == "month_started"
                                      and event.get("month_index") == index), {})
                end_event = next((event for event in reversed(details.events)
                                  if event.get("event_type") in {"month_completed", "month_failed"}
                                  and event.get("month_index") == index), {})
                month_status = {
                    "calendar_month": started_event.get("calendar_month"),
                    "status": end_event.get("status", "running" if started_event else "unknown"),
                    **{event.get("stage"): event.get("status") for event in details.events
                       if event.get("event_type") == "month_stage" and event.get("month_index") == index
                       and isinstance(event.get("stage"), str)},
                }
            st.write({"month_index": index, "calendar_month": month_status.get("calendar_month", "Unavailable"),
                      "month status": month_status.get("status", "Unavailable"),
                      "clock tick": month_status.get("clock_tick", "Unavailable"),
                      "risk delivery": month_status.get("risk_delivery", "Unavailable"),
                      "hotspot screening": month_status.get("hotspot_screening", "Unavailable"),
                      "alert decisions": month_status.get("alert_decisions", "Unavailable"),
                      "Contract Net": month_status.get("contract_net", "Unavailable")})
            reading_rows = [row for row in details.messages
                            if row.get("message_type") == "well_readings"
                            and str(row.get("month_index")) == str(index)]
            st.caption(f"Observed well_readings message-log rows: {len(reading_rows)} (sent/received log copies are retained separately). Well-score artifact rows: {len(_month_rows(details.well_scores.rows, index))}.")
            for artifact_name, artifact in (("Risk records", details.risk_records),
                                             ("Village-level WQI/risk score details", details.risk_scores),
                                             ("Well-level scores and indicator ratings", details.well_scores),
                                             ("Hotspot screening evidence", details.hotspots),
                                             ("Alert decisions and simulated inbox delivery", details.alerts)):
                st.markdown(f"**{artifact_name} · {artifact.status}**")
                relevant = _month_rows(artifact.rows, index)
                if relevant:
                    _render_page_table(st, artifact_name, relevant, key=f"run_month_{index}_{artifact_name}")
                else:
                    st.info(f"{artifact.message} No {artifact_name.casefold()} rows are recorded for this month_index.")
            round_rows = [row for row in details.rounds.rows if row.get("month_index") == index]
            if round_rows:
                st.markdown("**Contract Net round details**")
                for row in round_rows:
                    _render_round_inspector(st, row, details.messages)
            else:
                st.info(f"Round results: {details.rounds.status}; no round artifact for month_index {index}.")


def _status_callout(st: Any, status: str, run_id: Any,
                   decisions: Mapping[str, Any] | None = None) -> None:
    message = f"Run {run_id} · {_display_run_status(status)}"
    if status == "failed":
        st.error(message)
    elif status == "completed_with_unavailable_stages":
        decisions = decisions or {}
        explanations = decisions.get("unavailable_explanations", ())
        metrics = decisions.get("unavailable_metrics", ())
        expected_warmup_only = bool(explanations) and all(
            item.get("expected") is True for item in explanations
        ) and not decisions.get("unavailable_stages") and not decisions.get("failures")
        unavailable_metrics_only = (
            decisions.get("evaluation_status") in {"unavailable", "empty", "incomplete"}
            and bool(metrics)
            and all(item.get("status") == "unavailable" for item in metrics)
        )
        if expected_warmup_only and unavailable_metrics_only:
            st.warning(
                message + " · remaining gaps are expected hotspot-history warm-up and "
                "evaluation metrics unavailable for missing evidence; see the reasons below"
            )
        else:
            st.warning(message + " · some stages have unavailable evidence")
    elif status == "completed":
        st.success(message + " · required operational stages completed")
        decisions = decisions or {}
        if (decisions.get("unavailable_explanations")
                or decisions.get("unavailable_stages")
                or decisions.get("unavailable_metrics")):
            st.caption(
                "Expected warm-up and unavailable evidence are listed separately below; "
                "they do not change operational completion."
            )
    elif status in {"queued", "running"}:
        st.info(message + " · refreshing persisted run evidence")
    else:
        st.warning(message + " · status is not confirmed")


def _render_agent_cards(st: Any, cards: Sequence[Mapping[str, Any]]) -> None:
    st.subheader("Agent activity")
    if not cards:
        st.info("No agent identities have been observed yet. Status and activity are unknown.")
        return
    for offset in range(0, len(cards), 3):
        columns = st.columns(min(3, len(cards) - offset))
        for column, card in zip(columns, cards[offset:offset + 3]):
            with column.container(border=True):
                st.markdown(f"**{card['name']}**")
                st.caption(str(card.get("identity", "Identity unavailable")))
                state = str(card.get("status", "unknown"))
                if state == "running":
                    st.info("Running")
                elif state == "waiting":
                    st.warning("Waiting")
                elif state == "completed":
                    st.success("Completed")
                elif state == "failed":
                    st.error("Failed")
                elif state == "starting":
                    st.warning("Starting")
                else:
                    st.warning("Status unknown/stale")
                st.markdown(f"**Activity:** {card['activity']}")
                st.caption(f"Latest input: {card['latest_input']}")
                st.caption(f"Latest result: {card['latest_result']}")
                st.caption(f"Output recipient: {card['output_to']}")


def _render_collaboration_flow(st: Any, flow: Mapping[str, Any]) -> None:
    st.subheader("How the agents collaborate")
    st.caption("Solid arrows use observed sent rows from the seven-column SPADE log. Dashed arrows show runner/input orchestration and are not agent messages.")
    st.caption("The runner fans risk evidence to village, collector, hotspot, and alert agents; it waits for the risk, hotspot, and alert stages before launching that month's Contract Net round. The diagram distinguishes those runner-controlled steps from logged SPADE messages.")
    rows = []
    for edge in flow.get("message_edges", ()):
        types = ", ".join(edge.get("message_types", ())) or "typed message"
        rows.append((edge.get("source", "Unknown"), edge.get("target", "Unknown"),
                     f"{types} · {edge.get('sent_log_rows', 'Unavailable')} sent log row(s)", "message"))
    for edge in flow.get("runner_orchestration", ()):
        rows.append((edge.get("source", "SimulationRunner"), edge.get("target", "Unknown"),
                     edge.get("evidence", "runner coordination"), "orchestration"))
    if rows:
        pieces = ["<div style='display:flex;flex-direction:column;gap:8px'>"]
        for source, target, evidence, kind in rows:
            dash = "dashed" if kind == "orchestration" else "solid"
            color = "#64748b" if kind == "orchestration" else "#15803d"
            pieces.append(
                "<div style='display:grid;grid-template-columns:minmax(130px,1fr) 34px minmax(130px,1fr) 2fr;"
                "align-items:center;gap:10px;padding:10px;border:1px " + dash + " " + color + ";border-radius:10px'>"
                "<span style='font-weight:600'>" + html.escape(str(source)) + "</span>"
                "<span style='color:" + color + ";font-size:20px;text-align:center'>→</span>"
                "<span style='font-weight:600'>" + html.escape(str(target)) + "</span>"
                "<span style='color:#64748b;font-size:0.85em'>" + html.escape(str(evidence)) + "</span></div>"
            )
        pieces.append("</div>")
        st.markdown("".join(pieces), unsafe_allow_html=True)
    else:
        st.info("No handoff rows have been recorded yet. No agent-to-agent flow is inferred.")
    st.caption(f"Input source: {flow.get('input_label', 'Input source unavailable')}.")
    if flow.get("message_status") != "available":
        st.caption(f"Message evidence: {flow.get('message_status')}; missing messages are not inferred.")


def _render_decision_group(st: Any, title: str, rows: Sequence[Mapping[str, Any]], status: str,
                           *, kind: str) -> None:
    st.markdown(f"#### {title}")
    if not rows:
        st.info(f"{title} unavailable · {status}")
        return
    for index, row in enumerate(rows):
        month = row.get("month_index", "Unavailable")
        division = row.get("division", "Unavailable")
        if kind == "risk":
            summary = f"{row.get('tier', 'Unavailable')} · WQI {row.get('wqi', 'Unavailable')}"
            source = "RiskScoringAgent result"
        elif kind == "allocation":
            summary = "Confirmed award" if row.get("confirmed_winner") else f"Selected · {row.get('status', 'confirmation unavailable')}"
            source = f"ResourceCoordinatorAgent result · {row.get('evidence', 'round artifact unavailable')}"
        elif kind == "hotspot":
            summary = str(row.get("status", "Unavailable"))
            source = "HotspotAgent screening evidence"
        else:
            delivery = row.get("delivery", "Unavailable")
            summary = f"{row.get('decision', 'Unavailable')} · delivery: {delivery}"
            if delivery == "confirmed":
                summary += " (simulated inbox acknowledged)"
            source = "AlertProcessingAgent result; inbox acknowledgement is local simulation only"
        label = f"month_index {month} · {division} · {summary}"
        with st.expander(label, expanded=False):
            if kind == "hotspot":
                st.write(f"Supporting month_index history: {row.get('history', 'Unavailable')}")
            if kind == "risk":
                st.write(f"Drivers: {row.get('drivers', 'Unavailable')}")
            st.caption(f"Evidence: {source}")
            if row.get("reason") not in (None, "", "Unavailable"):
                st.write(f"Reason: {row['reason']}")
            st.json(dict(row))


def _render_final_decisions(st: Any, decisions: Mapping[str, Any], status: str) -> None:
    st.subheader("Simulation results")
    st.caption("Final items link to their recorded agent result or artifact. Only confirmed Contract Net awards are labelled confirmed.")
    left, right = st.columns(2)
    with left:
        _render_decision_group(st, "Risk by village", decisions["risks"], decisions["risk_status"], kind="risk")
        _render_decision_group(st, "Hotspot screening", decisions["hotspots"], decisions["hotspot_status"], kind="hotspot")
    with right:
        _render_decision_group(st, "Resource allocation", decisions["allocations"], decisions["allocation_status"], kind="allocation")
        _render_decision_group(st, "Alerts and simulated inbox", decisions["alerts"], decisions["alert_status"], kind="alert")
    if status == "failed" or decisions["failures"]:
        st.error("The run records failure evidence.")
        for failure in decisions["failures"]:
            st.caption(str(failure))
    if decisions.get("unavailable_explanations"):
        st.warning("Unavailable stages and reasons")
        for item in decisions["unavailable_explanations"]:
            month = item["month_index"]
            divisions = ", ".join(item["divisions"])
            expected = " · expected warm-up" if item["expected"] else ""
            st.caption(f"{item['stage']} · month_index {month} · {divisions}{expected}: {item['reason']}")
    elif decisions["unavailable_stages"]:
        st.warning("Unavailable or incomplete stages")
        for stage, reason in decisions["unavailable_stages"]:
            st.caption(f"{stage}: {reason}")
    if decisions["evaluation_status"] in {"unavailable", "empty", "incomplete"}:
        st.info(f"Evaluation metrics: {decisions['evaluation_status']}. Missing evidence is not reported as zero.")
    for metric in decisions["unavailable_metrics"]:
        st.caption(f"Evaluation · {metric['metric']}: {metric['status']} · {metric.get('classification', 'evidence')}")
        st.write(f"Why: {metric['details']}")
        st.caption(f"Missing: {metric.get('missing_dependency', 'metric evidence')}")
        st.caption(f"Next: {metric.get('required_action', 'Review the documented metric inputs.')}")


def _render_run_detail_page(st: Any, details: SimulationRunDetails, lab_root: Path,
                            control_path: Path) -> None:
    manifest = details.manifest or {}
    control = details.control or {}
    run_id = manifest.get("run_id", control.get("run_id", "Unknown"))
    resolved = get_run_status(str(manifest.get("run_id", control.get("run_id", ""))), lab_root)
    status = (resolved.get("status") if resolved is not None
              else manifest.get("status", control.get("status", "unknown")))
    view = build_execution_view(details, run_status=str(status))
    st.title("Simulation Lab · Agent execution")
    st.caption("Synthetic illustrative run · reduced-parameter WQI · provisional screening")
    _status_callout(st, str(status), run_id, view["decisions"])
    progress_cols = st.columns([3, 1])
    if view["progress"] is not None:
        progress_cols[0].progress(view["progress"], text=(
            f"Months complete: {view['completed_months']} / {view['total_months']} · "
            f"latest month_index: {view['latest_month_index'] if view['latest_month_index'] is not None else 'Unavailable'}"))
    else:
        progress_cols[0].info("Month progress unavailable until the run records its configuration or month events.")
    progress_cols[1].metric("Agents observed", len(view["agents"]))
    _render_agent_cards(st, view["agents"])
    _render_collaboration_flow(st, view["flow"])
    _render_final_decisions(st, view["decisions"], str(status))
    st.caption("Water-quality indicators do not diagnose CKDu or establish that water caused CKDu. Heartbeats indicate liveness evidence, not correct behavior. Alert acknowledgements refer to the simulated inbox, not an external health officer.")

    with st.expander("Technical details", expanded=False):
        provenance = manifest.get("provenance", {})
        config = manifest.get("configuration", {})
        if not isinstance(provenance, Mapping):
            provenance = {}
        if not isinstance(config, Mapping):
            config = {}
        st.caption(f"Selected isolated run: {details.run_dir}")
        st.json({"run_id": run_id, "status": status, "started_at": manifest.get("started_at", control.get("created_at", "Unavailable")),
                 "completed_at": manifest.get("completed_at", "Unavailable"),
                 "duration": _format_run_duration(manifest), "provenance": provenance,
                 "configuration": config, "control": control})
        stage_status = manifest.get("stage_status", {})
        if isinstance(stage_status, Mapping) and stage_status:
            st.dataframe([{"Stage": key, "Status": value} for key, value in stage_status.items()],
                         use_container_width=True, hide_index=True)
        if details.errors:
            with st.expander("Incomplete or partially written artifacts"):
                for error in details.errors:
                    st.warning(error)
        _render_run_timeline(st, details)
        _render_agent_execution(st, details)
        _render_message_inspector(st, details)
        _render_monthly_details(st, details)
        st.subheader("Evaluation detail")
        st.caption(f"Evaluation artifact: {details.evaluation.status} · {details.evaluation.message}")
        evaluation_dataset = Dataset(
            tuple({str(k): str(v) for k, v in row.items()} for row in details.evaluation.rows),
            details.evaluation.status, details.evaluation.message,
            "Selected run's persisted evaluation_summary.csv", tuple(EVALUATION_COLUMNS),
        )
        _render_evaluation_summary(
            st, evaluation_dataset, allocation_results_verified=False,
            context="Allocation comparisons remain unavailable unless provenance, baselines, population/risk inputs, and an approved risk-weight definition are supplied.",
        )


def _live_coordination_model(snapshot: AgentMonitorSnapshot) -> dict[str, Any]:
    """Prepare a display model from the same run-local monitor snapshot used elsewhere."""
    coordinator_events = [event for event in snapshot.timeline_events
                         if str(event.get("component", "")).startswith("ResourceCoordinatorAgent")
                         and event.get("event_type") in {"agent_phase", "agent_waiting"}]
    current_month = next((row.get("month_index") for row in reversed(snapshot.months)
                          if row.get("status") == "running"), None)
    if current_month is None and snapshot.months:
        current_month = snapshot.months[-1].get("month_index")
    latest_round = snapshot.rounds[-1] if snapshot.rounds else None
    latest_phase = coordinator_events[-1] if coordinator_events else None
    return {
        "run_state": snapshot.freshness,
        "run_status": snapshot.run_status,
        "run_id": snapshot.run_id,
        "run_dir": str(snapshot.run_dir),
        "agents": snapshot.agents,
        "timeline": snapshot.timeline_events,
        # Keep precisely the seven logger columns. Do not collapse sent/received copies.
        "messages": tuple({key: row.get(key, "") for key in (
            "timestamp", "sender", "receiver", "performative", "thread_id", "payload", "direction",
        )} for row in snapshot.messages),
        "message_status": snapshot.message_status,
        "flows": snapshot.flows,
        "months": snapshot.months,
        "current_month_index": current_month,
        "latest_coordinator_phase": latest_phase,
        "latest_round": latest_round,
        "rounds": snapshot.rounds,
        "rounds_status": snapshot.rounds_status,
        "hotspots": snapshot.hotspots,
        "hotspots_status": snapshot.hotspots_status,
        "alerts": snapshot.alerts,
        "alerts_status": snapshot.alerts_status,
        "errors": snapshot.errors,
    }


def _coordination_event_source(event: Mapping[str, Any]) -> str:
    event_type = event.get("event_type")
    if event.get("component") == "SimulationRunner" or event_type in {
        "hotspot_result", "alert_outcome", "round_completed",
    }:
        return "Runner observation / persisted stage result"
    if event_type in {
        "agent_starting", "agent_started", "agent_start_failed", "agent_work_completed",
        "agent_work_failed", "agent_stopped", "agent_stop_failed",
    }:
        return "Runner lifecycle/work observation"
    return "Agent behavior event journal"


def _live_coordination_run_options(configured_run: str | None, lab_root: Path) -> tuple[tuple[str, Path], ...]:
    """Return an explicitly configured run and available lab-created run directories."""
    options: list[tuple[str, Path]] = []
    if configured_run:
        configured_path = Path(configured_run).expanduser()
        options.append((f"Configured run · {configured_path}", configured_path))
    for row in list_lab_runs(lab_root):
        path = Path(row["run_dir"])
        if all(existing.resolve() != path.resolve() for _label, existing in options):
            options.append((f"Simulation Lab · {row['run_id']} · {row['status']}", path))
    return tuple(options)


def _render_live_coordination_snapshot(st: Any, snapshot: AgentMonitorSnapshot) -> None:
    model = _live_coordination_model(snapshot)
    st.subheader("Live Agent Coordination")
    st.caption(f"Run ID: {model['run_id']} · configured run directory: {model['run_dir']}")
    st.caption("Source is one explicitly configured SPADE run. Team outputs, illustrative fixtures, and generated local-demo data are separate.")
    if model["run_state"] == "live":
        st.success("Run active: the runner heartbeat is fresh. This is not a health check of domain behavior.")
    elif model["run_state"] == "stale":
        st.warning("Run manifest says running, but runner heartbeat evidence is stale. Current activity is not confirmed.")
    elif model["run_state"] == "completed snapshot":
        st.info("Run completed. This is a persisted snapshot, not a live process view.")
    elif model["run_state"] == "failed snapshot":
        st.error("Run failed. This is its persisted failure snapshot.")
    else:
        st.warning("Run state is unknown. No live state is inferred.")
    st.caption("Per-agent running/waiting states require that agent's fresh heartbeat. Heartbeats indicate liveness evidence only.")

    current_phase = model["latest_coordinator_phase"]
    current_round = model["latest_round"]
    left, right, third = st.columns(3)
    left.metric("Current month_index", model["current_month_index"] if model["current_month_index"] is not None else "Unavailable")
    right.metric("Contract Net phase", current_phase.get("phase", "Unavailable") if current_phase else "Unavailable")
    third.metric("Latest round outcome", current_round.get("award_status", "Unavailable") if current_round else "Unavailable")

    st.markdown("#### Agent lifecycle and heartbeat evidence")
    if model["agents"]:
        st.dataframe(list(model["agents"]), use_container_width=True)
    else:
        st.info("No agent identities have been observed or configured in this run manifest.")

    st.markdown("#### Event timeline")
    st.caption("Runner stage summaries are distinguished from agent behavior events. SPADE message exchanges are shown separately from both.")
    if model["timeline"]:
        timeline = [{
            "Timestamp": event.get("timestamp", "Unavailable"),
            "Sequence": event.get("event_seq", "Unavailable"),
            "Source": _coordination_event_source(event),
            "Component": event.get("component", "Unknown"),
            "Identity": event.get("identity", "Unavailable"),
            "Event": event.get("event_type", "Unknown"),
            "month_index": event.get("month_index", "Unavailable"),
            "Round": event.get("round", "Unavailable"),
            "Thread/correlation": event.get("thread_id", "Unavailable"),
            "Phase": event.get("phase", "Unavailable"),
            "Outcome": event.get("outcome", event.get("status", "Unavailable")),
            "Details": event.get("reason", event.get("decision_status", "")),
        } for event in model["timeline"][-100:]]
        st.caption(f"Showing the most recent {len(timeline)} of {len(model['timeline'])} journal events.")
        st.dataframe(timeline, use_container_width=True)
    else:
        st.info("No event-journal rows are available yet.")

    st.markdown("#### SPADE message flow")
    st.caption(f"Seven-column logger status: {model['message_status']}. Rows are preserved as logged, including direction and ambiguous copies.")
    st.caption("Logged message rows are evidence of SPADE send/receive operations. Runner stage events and local helper results are not message exchanges.")
    if model["flows"]:
        st.dataframe(list(model["flows"]), use_container_width=True)
    else:
        st.info("No sender/receiver message flow is available.")
    if model["messages"]:
        st.dataframe(list(model["messages"]), use_container_width=True)
    else:
        st.info("No message rows are available. Missing rows are not inferred or reconstructed.")

    st.markdown("#### Month progression and Contract Net outcomes")
    if model["months"]:
        st.dataframe(list(model["months"]), use_container_width=True)
    else:
        st.info("Month progression is unavailable.")
    if model["rounds"]:
        st.dataframe([{
            "month_index": row.get("month_index"), "Round": row.get("round", "Unavailable"),
            "Resource": row.get("resource", "Unavailable"),
            "Award status": row.get("award_status", "Unavailable"),
            "Winner": row.get("winner") if row.get("award_status") == "confirmed" else "Not confirmed",
        } for row in model["rounds"]], use_container_width=True)
    else:
        st.info(f"Contract Net results {model['rounds_status']}; no persisted round result is available.")

    st.markdown("#### Hotspot screening and alert outcomes")
    st.caption("Hotspot results are project-defined provisional screening, not epidemiological validation. Unknown or unavailable does not mean inactive or safe.")
    if model["hotspots"]:
        st.dataframe(list(model["hotspots"]), use_container_width=True)
    else:
        st.info(f"Hotspot results {model['hotspots_status']}.")
    st.caption("Alert confirmation refers only to the run-local simulated inbox, not an external health officer.")
    if model["alerts"]:
        st.dataframe(list(model["alerts"]), use_container_width=True)
    else:
        st.info(f"Alert outcomes {model['alerts_status']}.")
    evaluation = _read_csv(
        snapshot.run_dir / "logs" / "evaluation_summary.csv",
        EVALUATION_COLUMNS,
        "Persisted evaluator summary for this selected SPADE run",
    )
    _render_evaluation_summary(
        st, evaluation, allocation_results_verified=False,
        context=("Allocation comparisons remain unavailable unless provenance, required inputs, "
                 "baselines, and the risk-weight definition are verified."),
    )
    if model["errors"]:
        with st.expander("Incomplete or malformed run data"):
            for error in model["errors"]:
                st.warning(error)


def _render_live_coordination(st: Any, run_dir: Path | None) -> None:
    if run_dir is None:
        st.subheader("Live Agent Coordination")
        st.info("No simulation run is configured. Set CKDU_SIMULATION_RUN_DIR to the run directory containing run_manifest.json, agent_events.jsonl, and logs/messages.csv.")
        return
    _render_live_coordination_snapshot(st, load_agent_monitor(run_dir))


def _render_lab_runs(st: Any, lab_root: Path) -> tuple[dict[str, Any], ...]:
    runs = _lab_run_history_rows(lab_root)
    st.markdown("#### Simulation Lab run history")
    if not runs:
        st.info("No Simulation Lab runs have been launched from this configured lab directory.")
        return ()
    shown = [{key: value for key, value in row.items() if not key.startswith("_")} for row in runs]
    session = getattr(st, "session_state", {})
    epoch = session.get("simulation_lab_history_epoch", 0) if hasattr(session, "get") else 0
    selection = st.dataframe(
        shown, use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="single-row",
        key=f"simulation_lab_run_history_{epoch}",
    )
    selected_id = _selected_run_id(selection, shown)
    if selected_id and hasattr(st, "session_state"):
        st.session_state["simulation_lab_selected_run_id"] = selected_id
    failed_runs = [row for row in runs if row.get("Status") == "failed"]
    if failed_runs:
        with st.expander("Failed-run process details"):
            for row in failed_runs:
                matching = next((item for item in list_lab_runs(lab_root)
                                 if item.get("run_id") == row.get("Run ID")), {})
                st.markdown(f"**{row.get('Run ID')}**: {matching.get('error') or 'Failure detail unavailable'}")
                st.code(read_process_log_tail(matching.get("process_log", "")), language="text")
    st.caption("Select a row to open that isolated run's details. Previously completed runs remain available for selection.")
    return runs


def _render_simulation_lab(st: Any, lab_root: Path) -> None:
    st.subheader("Simulation Lab")
    session = getattr(st, "session_state", {})
    selected_id = session.get("simulation_lab_selected_run_id") if hasattr(session, "get") else None
    history_rows = _lab_run_history_rows(lab_root)
    selected_row = next((row for row in history_rows if row.get("Run ID") == selected_id), None)
    if selected_id and selected_row is None and hasattr(st, "session_state"):
        st.session_state.pop("simulation_lab_selected_run_id", None)
    if selected_row is not None:
        if st.button("← Back to Simulation Lab", key="simulation_lab_back_to_form"):
            if hasattr(st, "session_state"):
                st.session_state.pop("simulation_lab_selected_run_id", None)
                st.session_state["simulation_lab_history_epoch"] = session.get("simulation_lab_history_epoch", 0) + 1
            st.rerun()
        with st.expander("Run history and saved runs"):
            _render_lab_runs(st, lab_root)
        current = next((row for row in list_lab_runs(lab_root) if row.get("run_id") == selected_id), None)
        if current is None:
            st.warning("The selected run record is unavailable.")
            return
        status = current.get("status", "unknown")

        def render_selected_run() -> None:
            details = load_simulation_run_details(
                selected_row["_run_dir"], lab_root=lab_root,
                control_path=selected_row["_control_path"],
            )
            _render_run_detail_page(st, details, lab_root, selected_row["_control_path"])

        if status in {"queued", "running"} and hasattr(st, "fragment"):
            @st.fragment(run_every=2)
            def refresh_selected_run():
                render_selected_run()
            refresh_selected_run()
        else:
            render_selected_run()
        return

    st.caption("Runs the existing SPADE multi-agent simulation in a background process. Inputs and results are isolated under the configured Simulation Lab directory.")
    st.warning("The built-in and accepted uploaded inputs are synthetic seasonal readings. Repeated seasonal values are expanded by the existing runner and are not new monthly measurements.")
    _render_lab_runs(st, lab_root)

    source = st.radio("Simulation input", ("Built-in synthetic sample", "Upload synthetic CSV"),
                      horizontal=True, key="simulation_lab_input_source")
    selected_input: Path | None = None
    uploaded_bytes: bytes | None = None
    input_error: str | None = None
    preview_rows: tuple[dict[str, str], ...] = ()
    row_count: int | None = None
    if source == "Built-in synthetic sample":
        try:
            preview = validate_input_path(SAMPLE_INPUT)
            selected_input = SAMPLE_INPUT
            preview_rows, row_count = preview.preview, preview.row_count
            st.success(f"Validated {row_count} rows from {preview.source_label}.")
        except (OSError, ValueError) as exc:
            input_error = str(exc)
    else:
        upload = st.file_uploader("Upload seasonal well-readings CSV", type=("csv",), key="simulation_lab_csv")
        if upload is not None:
            uploaded_bytes = upload.getvalue()
            try:
                preview = validate_input_bytes(uploaded_bytes)
                preview_rows, row_count = preview.preview, preview.row_count
                st.success(f"Validated {row_count} rows. Source labels are synthetic and will be preserved.")
            except (OSError, UnicodeError, csv.Error, ValueError) as exc:
                input_error = str(exc)
    if input_error:
        st.error(f"Input unavailable: {input_error}")
    # if preview_rows:
    #     st.caption("Preview: exact existing seasonal input schema; additional or missing columns are rejected.")
        # st.dataframe(list(preview_rows), use_container_width=True)

    st.markdown("#### Provisional hotspot screening")
    with st.expander("Configure an optional tier-persistence screen"):
        st.caption("This project-defined rule is provisional, not validated epidemiology. No tiers are preselected.")
        enable_hotspot = st.checkbox(
            "Configure a provisional tier-persistence screen",
            value=False,
            key="simulation_lab_hotspot_enabled",
        )
        trigger_tiers = st.multiselect(
            "Trigger tiers (Unknown is excluded)",
            options=HOTSPOT_TRIGGER_TIERS,
            default=(),
            disabled=not enable_hotspot,
            key="simulation_lab_hotspot_trigger_tiers",
        )
        consecutive_months = st.number_input(
            "Required consecutive month_index records",
            min_value=2,
            max_value=24,
            value=2,
            step=1,
            disabled=not enable_hotspot,
            key="simulation_lab_hotspot_consecutive_months",
        )
        if enable_hotspot:
            st.warning("Select at least one tier. The selected tier set and persistence window are provisional user configuration, not a validated medical or epidemiological rule.")

    st.markdown("#### Run configuration")
    with st.form("simulation_lab_run_form"):
        left, middle, right = st.columns(3)
        start_index = left.number_input("Start month_index", min_value=0, max_value=10000,
                                        value=0, step=1)
        month_count = middle.number_input("Number of simulated months", min_value=1, max_value=24,
                                          value=2, step=1)
        first_month = right.selectbox("Calendar month for first month_index",
                                      tuple(range(1, 13)), index=9,
                                      format_func=lambda value: f"{value:02d}")
        resource = st.selectbox("Resource type", tuple(sorted(RESOURCES)))
        resource_pool = st.number_input("Resource pool (integer units)", min_value=0,
                                        max_value=10000, value=1, step=1)
        round_number = st.number_input("Contract Net round number", min_value=0,
                                        max_value=10000, value=1, step=1)
        timeout_cols = st.columns(3)
        proposal_timeout = timeout_cols[0].number_input("Proposal timeout (s)", min_value=0.1,
                                                       value=2.0, step=0.5)
        confirmation_timeout = timeout_cols[1].number_input("Confirmation timeout (s)", min_value=0.1,
                                                            value=2.0, step=0.5)
        stage_timeout = timeout_cols[2].number_input("Stage timeout (s)", min_value=0.1,
                                                     value=30.0, step=1.0)
        with st.expander("Additional timeouts"):
            extra_cols = st.columns(4)
            hotspot_timeout = extra_cols[0].number_input("Hotspot stage timeout (s)", min_value=0.1,
                                                         value=5.0, step=0.5)
            alert_receive_timeout = extra_cols[1].number_input("Alert receive timeout (s)", min_value=0.1,
                                                              value=0.2, step=0.1)
            alert_timeout = extra_cols[2].number_input("Alert stage timeout (s)", min_value=0.1,
                                                      value=5.0, step=0.5)
            ack_timeout = extra_cols[3].number_input("Inbox acknowledgement timeout (s)", min_value=0.1,
                                                     value=2.0, step=0.5)
        active_runs = [row for row in list_lab_runs(lab_root)
                       if row.get("status") in {"queued", "running"}]
        submitted = st.form_submit_button("Run Simulation", disabled=bool(active_runs))

    if active_runs:
        st.warning(f"A simulation is already {active_runs[0]['status']} ({active_runs[0]['run_id']}). Wait for it to finish before launching another.")
    if submitted:
        if input_error or (selected_input is None and uploaded_bytes is None):
            st.error("Select a valid synthetic input before starting the runner.")
        else:
            try:
                hotspot_tiers, hotspot_months = resolve_hotspot_configuration(
                    enable_hotspot, trigger_tiers, int(consecutive_months),
                )
                settings = SimulationSettings(
                    start_month_index=int(start_index), month_count=int(month_count),
                    first_calendar_month=int(first_month), resource=resource,
                    resource_pool=int(resource_pool), round_number=int(round_number),
                    proposal_timeout_s=float(proposal_timeout),
                    confirmation_timeout_s=float(confirmation_timeout), stage_timeout_s=float(stage_timeout),
                    hotspot_stage_timeout_s=float(hotspot_timeout),
                    alert_receive_timeout_s=float(alert_receive_timeout),
                    alert_stage_timeout_s=float(alert_timeout),
                    alert_acknowledgement_timeout_s=float(ack_timeout),
                    hotspot_trigger_tiers=hotspot_tiers,
                    hotspot_consecutive_months=hotspot_months,
                )
                launched = launch_simulation(
                    lab_root, settings, input_path=selected_input if uploaded_bytes is None else None,
                    uploaded_csv=uploaded_bytes,
                )
                if launched["status"] == "failed":
                    st.error(f"Simulation {launched['run_id']} failed to start: {launched.get('error')}")
                else:
                    if hasattr(st, "session_state"):
                        st.session_state["simulation_lab_selected_run_id"] = launched["run_id"]
                    st.success(f"Simulation {launched['run_id']} {launched['status']}. Output: {launched['run_dir']}")
                    st.rerun()
            except (OSError, ValueError, RuntimeError, FileExistsError) as exc:
                st.error(f"Simulation was not started: {exc}")

    if st.button("Refresh run history"):
        st.rerun()


def main() -> None:
    try:
        import streamlit as st
    except ImportError as exc:
        raise SystemExit("Streamlit is required to launch this dashboard; see project dependency instructions.") from exc

    st.set_page_config(page_title="CKDu Well-Water Risk Indicators", layout="wide")
    st.title("CKDu Well-Water Risk-Indicator Support")
    st.warning(DISCLAIMER)
    data_root = Path(os.environ.get("CKDU_DATA_ROOT", ROOT / "data"))
    demo_output = os.environ.get("CKDU_DEMO_OUTPUT_DIR")
    simulation_run = os.environ.get("CKDU_SIMULATION_RUN_DIR")
    lab_root = Path(os.environ.get("CKDU_SIMULATION_LAB_DIR", default_lab_root())).expanduser()
    views = ("Team outputs", "Illustrative demo fixtures") + (("Generated local demo",) if demo_output else ()) + ("Agent Monitor", "Live Coordination", "Simulation Lab")
    default_view = views.index("Generated local demo") if demo_output else 0
    view = st.radio("Data view", views, index=default_view, horizontal=True)
    if view == "Agent Monitor":
        if not simulation_run:
            st.subheader("Live Agent Monitor")
            st.info("No simulation run selected. Set CKDU_SIMULATION_RUN_DIR to one isolated src.sim.runner output directory containing run_manifest.json, agent_events.jsonl, and logs/messages.csv.")
            return
        monitor_dir = Path(simulation_run).expanduser()
        st.caption("This source is separate from team outputs, illustrative fixtures, and CKDU_DEMO_OUTPUT_DIR generated local-demo artifacts.")
        if hasattr(st, "fragment"):
            @st.fragment(run_every=2)
            def refresh_monitor():
                _render_agent_monitor(st, monitor_dir)
            refresh_monitor()
        else:
            if st.button("Refresh Agent Monitor"):
                st.rerun()
            _render_agent_monitor(st, monitor_dir)
        with st.expander("Scientific limitations"):
            st.caption("Water-quality/risk indicators do not diagnose CKDu or establish that water caused CKDu.")
            st.caption("The WQI uses the project's reduced parameter set; hotspot screening is provisional.")
        return
    if view == "Live Coordination":
        run_options = _live_coordination_run_options(simulation_run, lab_root)
        if run_options:
            selected_label = st.selectbox("Simulation run", tuple(label for label, _path in run_options),
                                          key="live_coordination_run_selector")
            monitor_dir = next(path for label, path in run_options if label == selected_label)
            st.caption("Configured Compose runs use /app/simulation_run. Simulation Lab runs are read from their isolated run directory.")
        else:
            monitor_dir = None
        if hasattr(st, "fragment"):
            @st.fragment(run_every=2)
            def refresh_coordination():
                _render_live_coordination(st, monitor_dir)
            refresh_coordination()
        else:
            if st.button("Refresh Live Coordination"):
                st.rerun()
            _render_live_coordination(st, monitor_dir)
        with st.expander("Scientific and operational limitations"):
            st.caption("Water-quality/risk indicators do not diagnose CKDu or establish that water caused CKDu.")
            st.caption("The WQI uses a reduced parameter set; hotspot screening is provisional. Missing and Unknown evidence remains unavailable.")
            st.caption("Alert acknowledgements refer to the simulated inbox only, not an external health officer.")
        return
    if view == "Simulation Lab":
        _render_simulation_lab(st, lab_root)
        with st.expander("Scientific and operational limitations"):
            st.caption("The runner accepts the project's synthetic seasonal readings. It repeats seasonal values according to its existing expansion behavior; these are not new monthly observations.")
            st.caption("The WQI uses a reduced parameter set and does not diagnose CKDu or establish causation. Hotspot rules are provisional and user-configured.")
            st.caption("Allocation results are local SPADE simulation outputs, not verified Member C results. Alert acknowledgement is to a simulated inbox only.")
        return
    if view == "Generated local demo":
        _render_local_demo(st, Path(demo_output))
        with st.expander("Scientific limitations"):
            st.caption("Water-quality/risk indicators do not diagnose CKDu or establish that water caused CKDu.")
            st.caption("Approximate division town points are not well locations.")
        return
    logs_root = Path(os.environ.get("CKDU_LOGS_DIR", ROOT / "logs"))
    data = load_dashboard_data(
        synthetic_wells_path=data_root / "processed/synthetic_wells.csv",
        divisions_path=data_root / "raw/ckdu_03_sampling_design_ds_divisions.csv",
        reference_stats_path=data_root / "raw/ckdu_01_water_quality_area_stats.csv",
        risk_scores_path=logs_root / "risk_scores.csv", well_scores_path=logs_root / "well_scores.csv",
        allocations_path=logs_root / "allocations.csv", evaluation_path=logs_root / "evaluation_summary.csv",
    )
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
        _render_evaluation_summary(
            st, data.team_metrics, allocation_results_verified=False,
            context="Evaluator outputs are reported as recorded; allocation metrics stay unavailable until their inputs and provenance are verified.",
        )

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
        if data.team_wells.rows:
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
        if data.synthetic_wells.rows:
            st.caption(f"{data.synthetic_wells.source_label}. {data.synthetic_wells.message}.")
            seasons = sorted({r["season"] for r in data.synthetic_wells.rows})
            season = st.selectbox("Season", seasons)
            st.dataframe([r for r in data.synthetic_wells.rows if r["season"] == season], use_container_width=True)
        else:
            st.caption(data.synthetic_wells.source_label)
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
