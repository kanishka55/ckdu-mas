"""Run the isolated synthetic local demo without SPADE or network services.

Scoring delegates to the existing Member B pure WQI/risk-tier functions. If
their pandas dependency is unavailable, downstream artifacts record unavailable
stages instead of substituting a second scoring implementation.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.alert import AlertAgent
from src.core.hotspot_screening import ScreeningRule, screen_risk_history
from src.core.local_allocation import DECISION_COLUMNS, allocate_local
from src.core.msg_log import COLUMNS as MESSAGE_COLUMNS, log_message
from src.sim.evaluate import evaluate

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "demo_output"
RISK_COLUMNS = ("month_index", "month", "ds_division", "source", "wqi", "tier", "drivers",
                "driver_contributions", "n_wells", "n_scored", "share_poor_or_worse",
                "worst_tier", "weight_covered", "params_used", "n_rejected", "rejected", "error")
WELL_COLUMNS = ("month_index", "month", "ds_division", "district", "ckdu_prevalence_class",
                "source", "well_id", "wqi", "tier")
HOTSPOT_COLUMNS = ("provenance", "month_index", "ds_division", "rule_id", "status",
                   "evidence_month_indexes", "reason")
ALERT_COLUMNS = ("provenance", "month_index", "ds_division", "tier", "hotspot_status",
                 "status", "reason", "message", "payload")
DIVISION_POINT_COLUMNS = ("ds_division", "district", "ckdu_prevalence_class", "samples_per_season",
                          "population", "n_wells_modelled", "distance_to_depot_km", "lat", "lon",
                          "source", "notes")
MANIFEST_NAME = "provenance.json"


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _population(path: Path) -> dict[str, int]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"ds_division", "population"}.issubset(reader.fieldnames):
            raise ValueError(f"division input lacks ds_division/population columns: {path}")
        result: dict[str, int] = {}
        for number, row in enumerate(reader, start=2):
            name = (row.get("ds_division") or "").strip()
            raw = (row.get("population") or "").strip()
            if not name or not raw:
                continue
            value = int(float(raw))
            if value < 0 or name in result:
                raise ValueError(f"invalid or duplicate population row {number} in {path}")
            result[name] = value
        return result


def _division_points(path: Path) -> list[dict[str, str]]:
    """Copy published division reference rows into the isolated demo bundle."""
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(DIVISION_POINT_COLUMNS):
            return []
        return [{column: row.get(column, "") or "" for column in DIVISION_POINT_COLUMNS}
                for row in reader]


def _inspect_synthetic_input(path: Path) -> tuple[int, str]:
    """Check source provenance with stdlib before invoking the pandas pipeline."""
    if not path.is_file():
        return 0, f"unavailable: synthetic input file not found ({path})"
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "source" not in reader.fieldnames:
            return 0, "unavailable: synthetic input has no source column"
        count = 0
        for count, row in enumerate(reader, start=1):
            if row.get("source") != "synthetic":
                return count, "unavailable: input contains a non-synthetic or missing source label"
    if count == 0:
        return 0, "unavailable: synthetic input contains no records"
    return count, "available"


def _score_existing_pipeline(input_path: Path, method_path: Path, divisions_path: Path,
                             months: int, start_month: int) -> tuple[list[dict[str, Any]], str]:
    """Run Member B's existing data loading, WQI and village aggregation code."""
    try:
        from src.core import risk_tiers, wells, wqi
        from src.sim import load_data
        monthly = load_data.load_monthly_readings(input_path)
        method_frame = load_data.load_wqi_method(method_path)
        classes = risk_tiers.classes_from_frame(method_frame)
        method = wqi.method_from_frame(method_frame)
        divisions = load_data.load_ds_divisions(divisions_path, require_complete=False)
        history: list[dict[str, Any]] = []
        missing_pairs = 0
        for month_index in range(months):
            calendar_month = wells.month_for_index(month_index, start_month)
            for division in sorted(str(v) for v in divisions["ds_division"].tolist()):
                readings = monthly[(monthly.ds_division == division) & (monthly.month == calendar_month)]
                if readings.empty:
                    missing_pairs += 1
                    continue
                valid, rejected = wqi.reject_invalid_rows(readings, list(dict.fromkeys(
                    wells.PARAMS + [column for column, _factor in wqi.COLUMNS.values()])))
                if valid.empty:
                    village = risk_tiers.village_risk(
                        __import__("pandas").DataFrame(columns=["wqi", "params_used", "weight_covered"]), classes)
                    well_body = None
                else:
                    scored = wqi.score_frame(valid, method)
                    village = risk_tiers.village_risk(scored, classes)
                    well_body = risk_tiers.well_risk(month_index, valid, scored, classes)
                detail = {"month_index": month_index, "month": calendar_month,
                          "ds_division": division, "source": "synthetic", **village,
                          "n_wells": len(readings), "n_rejected": len(rejected),
                          "rejected": "; ".join(rejected), "error": None if not rejected else "some rows rejected",
                          "well_risk": well_body}
                history.append(detail)
        if not history:
            return [], "unavailable: no division/month risk records were produced"
        # Reuse the established writer, but direct both files into the demo root.
        status = "available" if missing_pairs == 0 else f"partial: {missing_pairs} division/month inputs had no readings"
        return history, status
    except (ImportError, ModuleNotFoundError) as exc:
        return [], f"unavailable: scoring dependency/import unavailable ({exc})"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [], f"unavailable: scoring input or method error ({type(exc).__name__}: {exc})"


def _risk_body(detail: dict[str, Any]) -> dict[str, Any]:
    return {"type": "risk_record", "month_index": int(detail["month_index"]),
            "ds_division": str(detail["ds_division"]),
            "wqi": None if detail["wqi"] is None else float(detail["wqi"]),
            "tier": str(detail["tier"]), "drivers": list(detail["drivers"])}


def _validate_output_root(output_root: Path) -> Path:
    root = output_root.expanduser().resolve()
    protected = (ROOT / "logs", ROOT / "data/raw", ROOT / "data/processed")
    for target in protected:
        try:
            root.relative_to(target.resolve())
        except ValueError:
            continue
        raise ValueError(f"refusing to write local demo under protected path: {target}")
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"output directory is not empty; choose a fresh isolated path: {root}")
    return root


def run_demo(*, output_dir: str | Path = DEFAULT_OUTPUT, input_path: str | Path = ROOT / "data/processed/synthetic_wells.csv",
             method_path: str | Path = ROOT / "data/raw/ckdu_02_wqi_method_and_thresholds.csv",
             divisions_path: str | Path = ROOT / "data/raw/ckdu_03_sampling_design_ds_divisions.csv",
             months: int = 12, start_month: int = 10, resource: str = "RO_unit",
             resource_pool_size: int = 3,
             hotspot_trigger_tiers: tuple[str, ...] = ("Poor", "Very Poor", "Unsuitable"),
             hotspot_consecutive_months: int = 2) -> dict[str, Any]:
    """Generate artifacts in a new/empty output root and return the manifest."""
    if isinstance(months, bool) or not isinstance(months, int) or months < 1:
        raise ValueError("months must be a positive integer")
    if start_month not in range(1, 13):
        raise ValueError("start_month must be 1..12")
    rule = ScreeningRule("demo_configured_tier_persistence_v1",
                         frozenset(hotspot_trigger_tiers), hotspot_consecutive_months)
    root = _validate_output_root(Path(output_dir))
    root.mkdir(parents=True, exist_ok=True)
    risk_path, wells_path = root / "logs/risk_scores.csv", root / "logs/well_scores.csv"
    messages_path = root / "logs/messages.csv"
    input_rows, input_status = _inspect_synthetic_input(Path(input_path))
    if input_status != "available":
        history, risk_status = [], input_status
    else:
        try:
            history, risk_status = _score_existing_pipeline(Path(input_path), Path(method_path),
                                                            Path(divisions_path), months, start_month)
        except Exception as exc:  # input errors must become visible stage status, not fake output
            history, risk_status = [], f"unavailable: scoring stage failed ({type(exc).__name__}: {exc})"

    if history:
        from src.sim.risk_outputs import write_outputs
        write_outputs(history, risk_path, wells_path)
    else:
        _write_csv(risk_path, RISK_COLUMNS, [])
        _write_csv(wells_path, WELL_COLUMNS, [])

    populations: dict[str, int] = {}
    try:
        populations = _population(Path(divisions_path))
        population_status = "available"
    except (OSError, ValueError, TypeError) as exc:
        population_status = f"unavailable: {type(exc).__name__}: {exc}"
    points = _division_points(Path(divisions_path))
    _write_csv(root / "division_points.csv", DIVISION_POINT_COLUMNS, points)

    risk_records = [_risk_body(row) for row in history]
    hotspot_rows: list[dict[str, Any]] = []
    division_names = sorted(set(populations) | {r.get("ds_division", "") for r in risk_records})
    for index in range(months):
        for division in division_names:
            result = screen_risk_history(risk_records, ds_division=division,
                                         month_index=index, rule=rule)
            hotspot_rows.append({"provenance": "illustrative_local_demo", **result.__dict__,
                                 "evidence_month_indexes": json.dumps(result.evidence_month_indexes),
                                 "reason": result.reason or ""})
    _write_csv(root / "hotspot_screening.csv", HOTSPOT_COLUMNS, hotspot_rows)

    allocation_rows: list[dict[str, Any]] = []
    allocation_statuses: list[str] = []
    for index in range(months):
        month_records = [r for r in risk_records if r["month_index"] == index]
        try:
            allocation = allocate_local(month_records, populations, month_index=index,
                                        resource=resource, resource_pool_size=resource_pool_size)
            allocation_rows.extend(d.as_row() for d in allocation.decisions)
            allocation_statuses.append(allocation.status)
        except (ValueError, TypeError) as exc:
            allocation_statuses.append("unavailable")
    if not risk_records or not populations or not allocation_statuses or all(s == "unavailable" for s in allocation_statuses):
        allocations_status = "unavailable: risk or population evidence is missing/incomplete"
    elif any(s != "available" for s in allocation_statuses):
        allocations_status = "partial: one or more simulated months have unavailable allocation inputs"
    else:
        allocations_status = "available"
    _write_csv(root / "allocation_decisions.csv", DECISION_COLUMNS, allocation_rows)

    alert_agent = AlertAgent(logger=lambda sender, receiver, performative, thread_id, payload, direction:
                             log_message(sender, receiver, performative, thread_id, payload, direction,
                                          path=messages_path))
    alert_rows: list[dict[str, Any]] = []
    record_by_key = {(record["ds_division"], record["month_index"]): record for record in risk_records}
    for index in range(months):
        for division in division_names:
            record = record_by_key.get((division, index))
            if record is None:
                alert_rows.append({"provenance": "illustrative_local_demo", "month_index": index,
                                   "ds_division": division, "tier": "Unavailable",
                                   "hotspot_status": "unavailable", "status": "unavailable",
                                   "reason": "risk_record_unavailable", "message": "", "payload": "{}"})
                continue
            result = screen_risk_history(risk_records, ds_division=division,
                                         month_index=index, rule=rule)
            decision = alert_agent.process_risk_record(record, hotspot=result.alert_hotspot)
            alert_rows.append({"provenance": "illustrative_local_demo", "month_index": decision.month_index,
                               "ds_division": decision.ds_division, "tier": decision.tier,
                               "hotspot_status": "unavailable" if decision.hotspot is None else str(decision.hotspot).lower(),
                               "status": decision.status, "reason": decision.reason or "",
                               "message": decision.message or "", "payload": json.dumps(
                                   decision.alert_payload() if decision.status == "emitted" else {},
                                   sort_keys=True, separators=(",", ":"))})
    _write_csv(root / "alerts.csv", ALERT_COLUMNS, alert_rows)

    # Ensure an empty but valid seven-column log exists; only emitted alerts appear in it.
    if not messages_path.exists():
        _write_csv(messages_path, tuple(MESSAGE_COLUMNS), [])
    summary_path = root / "logs/evaluation_summary.csv"
    metric_results = evaluate(messages_path=messages_path,
                              allocations_path=root / "logs/allocations.csv",
                              output_path=summary_path)
    manifest = {
        "provenance": "illustrative_local_demo", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "synthetic_input": {"path": str(Path(input_path)), "source": "synthetic",
                            "rows": input_rows, "preflight_status": input_status,
                            "monthly_expansion": "seasonal values repeated by existing MONTH_TO_SEASON rule; not new measurements"},
        "scoring": {"implementation": "existing src.core.wqi and src.core.risk_tiers", "status": risk_status,
                    "limitation": "reduced-parameter WQI; not directly comparable to 13-parameter published WQI"},
        "configuration": {"months": months, "start_calendar_month": start_month,
                          "resource": resource, "resource_pool_size": resource_pool_size,
                          "rho_assumption_from_generator": 0.6},
        "policies": {"hotspot": {"rule_id": rule.rule_id, "trigger_tiers": sorted(rule.trigger_tiers),
                                   "consecutive_months": rule.consecutive_months,
                                   "status": "project-defined illustrative screening, not validated epidemiology"},
                     "allocation": {"implementation": "local illustrative Contract Net-style ranking; not Member C code",
                                    "bid": "existing demo engine WQI × population", "tie_break": "alphabetical DS division",
                                    "first_come_order": "provisional alphabetical synthetic order"},
                     "alert": "current approved wording only; Very Poor, Unsuitable and Unknown remain unavailable"},
        "stages": {"risk_scoring": risk_status, "population": population_status,
                   "hotspot": "generated with explicit configured rule; unavailable records retained",
                   "allocation": allocations_status,
                   "alerts": "generated only when risk record and hotspot bool are available",
                   "evaluation": "written with unsupported mappings, correlation and allocation attestations omitted"},
        "artifacts": {"risk_scores": "logs/risk_scores.csv", "well_scores": "logs/well_scores.csv",
                      "messages": "logs/messages.csv", "hotspots": "hotspot_screening.csv",
                      "allocations": "allocation_decisions.csv", "alerts": "alerts.csv",
                      "division_points": "division_points.csv (published approximate division-town coordinates)",
                      "evaluation_summary": "logs/evaluation_summary.csv"},
        "allocation_metric_results": "unavailable: illustrative local allocations are not evaluator team allocation inputs",
        "scientific_disclaimer": "Water-quality/risk indicators do not diagnose CKDu or establish causation.",
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Local demo artifacts written under {root}")
    print(f"Risk scoring: {risk_status}; evaluation metrics: {len(metric_results)} rows (evidence-gated).")
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--input", default=str(ROOT / "data/processed/synthetic_wells.csv"))
    parser.add_argument("--method", default=str(ROOT / "data/raw/ckdu_02_wqi_method_and_thresholds.csv"))
    parser.add_argument("--divisions", default=str(ROOT / "data/raw/ckdu_03_sampling_design_ds_divisions.csv"))
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--start-month", type=int, default=10)
    parser.add_argument("--resource", choices=("RO_unit", "bowser"), default="RO_unit")
    parser.add_argument("--pool-size", type=int, default=3)
    parser.add_argument("--hotspot-trigger-tiers", default="Poor,Very Poor,Unsuitable",
                        help="comma-separated demo screening tiers; Unknown is rejected")
    parser.add_argument("--hotspot-consecutive-months", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        run_demo(output_dir=args.output_dir, input_path=args.input, method_path=args.method,
                 divisions_path=args.divisions, months=args.months, start_month=args.start_month,
                 resource=args.resource, resource_pool_size=args.pool_size,
                 hotspot_trigger_tiers=tuple(tier.strip() for tier in args.hotspot_trigger_tiers.split(",") if tier.strip()),
                 hotspot_consecutive_months=args.hotspot_consecutive_months)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
