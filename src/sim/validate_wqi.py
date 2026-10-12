"""Compare the WQI tier distribution of the synthetic wells with Cooray et al. 2019.

Benchmarks (data/raw/ckdu_05_validation_benchmarks.csv): 3.8% wet / 2.6% dry Excellent and
28.2% wet / 30.3% dry Poor. This is a sanity check on the model and the synthetic generator,
not a validation of any chemistry-CKDu link.

    python -m src.sim.validate_wqi
"""
from __future__ import annotations

import pandas as pd

from src.core import risk_tiers, wqi
from src.sim import load_data

# Cooray et al. 2019, as transcribed in ckdu_05 (percent of samples)
COORAY_PCT = {("Wet", "Excellent"): 3.8, ("Dry", "Excellent"): 2.6,
              ("Wet", "Poor"): 28.2, ("Dry", "Poor"): 30.3}


def tier_distribution() -> tuple[pd.DataFrame, pd.DataFrame]:
    method_frame = load_data.load_wqi_method()
    method = wqi.method_from_frame(method_frame)
    classes = risk_tiers.classes_from_frame(method_frame)
    wells = load_data.load_synthetic_wells()
    scored = wqi.score_frame(wells, method)
    wells = wells.assign(wqi=scored["wqi"], weight_covered=scored["weight_covered"],
                         params_used=scored["params_used"],
                         tier=scored["wqi"].map(lambda v: risk_tiers.tier_for_wqi(v, classes)))
    pct = (wells.groupby("season")["tier"].value_counts(normalize=True).mul(100)
           .unstack(fill_value=0.0).reindex(columns=risk_tiers.tier_order(classes), fill_value=0.0))
    return wells, pct


def main() -> None:
    wells, pct = tier_distribution()
    print(f"synthetic wells: {len(wells)} readings, WQI {wells['wqi'].min():.1f}-"
          f"{wells['wqi'].max():.1f} (Cooray et al.: 21.2-178.3)")
    print(f"parameters used: {wells['params_used'].iloc[0]} "
          f"({wells['weight_covered'].iloc[0]:.0%} of the published weight) -> reduced-parameter WQI\n")
    rows = []
    for (season, tier), paper in COORAY_PCT.items():
        rows.append({"season": season, "tier": tier, "ours_%": round(pct.loc[season, tier], 1),
                     "cooray_%": paper})
    print(pd.DataFrame(rows).to_string(index=False))
    print("\nfull distribution (% of readings):")
    print(pct.round(1).to_string())
    print("\nmedian WQI by Cooray prevalence class:")
    print(wells.groupby("ckdu_prevalence_class")["wqi"].median().round(1).to_string())


if __name__ == "__main__":
    main()
