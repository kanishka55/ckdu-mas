"""Function 2 output files: what the RiskScoringAgent produced, for Function 3 and the dashboard.

logs/risk_scores.csv  one row per village per month (the risk_record fields + extra detail:
                      share of wells Poor or worse, worst tier, rejected wells, errors)
logs/well_scores.csv  one row per well per month (the well_risk message, flattened: identifiers,
                      ckdu_prevalence_class, wqi, tier and q_<parameter> quality ratings)

Function 3 (HotspotAgent) receives the same data live as risk_record + well_risk messages; these
files let it be built and tested on its own, from Function 2's output.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.core import risk_tiers

RISK_SCORES_PATH = Path("logs") / "risk_scores.csv"
WELL_SCORES_PATH = Path("logs") / "well_scores.csv"


def write_outputs(history: list[dict], risk_path: Path = RISK_SCORES_PATH,
                  well_path: Path = WELL_SCORES_PATH) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Write the agent's ``history`` (one detail dict per scored message) to the two CSV files."""
    villages = pd.DataFrame(history).drop(columns="well_risk", errors="ignore")
    wells = pd.DataFrame([row for d in history if d.get("well_risk")
                          for row in risk_tiers.well_rows(d["well_risk"])])
    risk_path.parent.mkdir(parents=True, exist_ok=True)
    villages.to_csv(risk_path, index=False)
    wells.to_csv(well_path, index=False)
    return villages, wells
