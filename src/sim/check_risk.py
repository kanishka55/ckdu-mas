"""Check the risk scoring function by hand: one well you type in, or one village from the data.

Shows every step: concentration -> qi -> weight -> contribution -> WQI -> tier.
Risk indicator from published water-quality thresholds - not a CKDu diagnosis.

One well (any parameter you leave out is treated as missing):
    python -m src.sim.check_risk --F 2.5 --hardness 300 --pH 7.2 --DOC 5
One village for one calendar month (synthetic wells):
    python -m src.sim.check_risk --village Padaviya --month 1
"""
from __future__ import annotations

import argparse

import pandas as pd

from src.agents.risk_scoring import RiskScoringAgent
from src.core import risk_tiers, wqi
from src.sim import load_data


def _setup():
    frame = load_data.load_wqi_method()
    return wqi.method_from_frame(frame), risk_tiers.classes_from_frame(frame)


def check_well(values: dict[str, float]) -> None:
    method, classes = _setup()
    reading = {wqi.COLUMNS[p][0]: v for p, v in values.items()}
    result = wqi.score_well(reading, method)
    rel = wqi.relative_weights(method, result["params_used"])
    col_unit = {"Fe": "ug/L"}
    rows = [{"parameter": p, "value": values[p], "unit": col_unit.get(p, "" if p == "pH" else "mg/L"),
             "standard_Si": method.standards[p], "qi": round(result["q"][p], 1),
             "Wi": round(rel[p], 4), "contribution": round(result["contributions"][p], 1),
             "above_standard": "YES" if result["q"][p] > 100 else ""}
            for p in result["params_used"]]
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"\nWQI  = {result['wqi']:.1f}")
    print(f"tier = {risk_tiers.tier_for_wqi(result['wqi'], classes)}")
    print(f"parameters used: {len(result['params_used'])} of 13 "
          f"({result['weight_covered']:.0%} of the published weight); "
          f"missing: {', '.join(result['params_missing']) or 'none'}")


def check_village(village: str, month: int) -> None:
    method, classes = _setup()
    readings = load_data.load_monthly_readings()
    rows = readings[(readings["ds_division"] == village) & (readings["month"] == month)]
    if rows.empty:
        names = ", ".join(sorted(readings["ds_division"].unique()))
        raise SystemExit(f"no readings for {village!r} month {month}. Villages: {names}")

    scored = wqi.score_frame(rows, method)
    per_well = rows[["well_id", "F_mg_L", "hardness_mg_L", "DOC_mg_L"]].assign(
        WQI=scored["wqi"].round(1),
        tier=scored["wqi"].map(lambda v: risk_tiers.tier_for_wqi(v, classes)))
    print(f"{village}, month {month} ({rows['season'].iloc[0]} season), {len(rows)} wells\n")
    print(per_well.to_string(index=False))

    # Same path the SPADE agent uses, without starting XMPP
    agent = RiskScoringAgent("check@localhost", "x", subscribers=[], method=method, classes=classes)
    record, detail = agent.score({"type": "well_readings", "month_index": 0, "ds_division": village,
                                  "source": "synthetic", "readings": rows.to_dict("records")})
    print("\nrisk_record sent to Coordinator / Alert / Hotspot:")
    print(f"  {record}")
    print(f"extra detail: share of wells Poor or worse = {detail['share_poor_or_worse']:.0%}, "
          f"worst well = {detail['worst_tier']}, rejected wells = {detail['n_rejected']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--village", help="DS division name, e.g. Padaviya")
    ap.add_argument("--month", type=int, default=1, help="calendar month 1-12 (default 1)")
    for p in wqi.WQI_PARAMS:
        unit = "ug/L" if p == "Fe" else ("" if p == "pH" else "mg/L")
        ap.add_argument(f"--{p}", type=float, help=f"{p} {unit}".strip())
    args = ap.parse_args()

    values = {p: getattr(args, p) for p in wqi.WQI_PARAMS if getattr(args, p) is not None}
    if args.village:
        check_village(args.village, args.month)
    elif values:
        check_well(values)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
