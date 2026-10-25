"""VillageAgent bid function. Pure module: no SPADE imports.

First draft (START_HERE): need = risk x population / distance_to_depot_km.
Current version: need = risk x population, bid only when the village tier is Poor or worse.

Design decisions (record every change to this function here, with the reason):
- Population 0 means nobody to serve, so the bid is 0. A missing population or risk raises:
  the caller (VillageAgent) must refuse the CFP rather than bid on a guessed value.
- Raw bids differ by orders of magnitude between villages, so the coordinator compares
  ``normalise_bids`` output in [0, 1] instead.
- Ties are broken by ds_division name (alphabetical) in ``rank_bids`` so allocation is
  deterministic and reproducible.
- 2026-10-17: distance to the depot removed from the bid. Dividing by distance made the most remote
  at-risk villages bid lowest (A+B test run: Padaviya, tier Poor, 68 km, bid 33,924 below Kandy,
  tier Good). Distance is retained as village metadata, but the current coordinator does not use it
  for ranking and no bowser delivery-cost formula is implemented.
- 2026-10-17: ``has_need`` gates bidding on the risk tier. Once risk became the village WQI (never 0),
  every village always bid and "no need" refusals never happened. A village now bids only when its
  tier is Poor, Very Poor or Unsuitable. The boundary (WQI 100) is the published Cooray et al. class
  limit read from ckdu_02 by src/core/risk_tiers.py; no new threshold is introduced here.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from src.core.risk_tiers import POOR_OR_WORSE


def _is_missing(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def has_need(tier: str | None) -> bool:
    """True when the village tier is Poor or worse. Unknown, stub or missing tiers have no need."""
    return tier in POOR_OR_WORSE


def bid(risk: float, population: int) -> float:
    """Raw need score for one village: risk x population."""
    if _is_missing(risk):
        raise ValueError("risk is missing")
    if _is_missing(population):
        raise ValueError("population is missing")
    if risk < 0:
        raise ValueError(f"risk must be >= 0, got {risk}")
    if population < 0:
        raise ValueError(f"population must be >= 0, got {population}")

    if population == 0 or risk == 0:
        return 0.0
    return float(risk) * float(population)


def normalise_bids(raw: Mapping[str, float]) -> dict[str, float]:
    """Scale raw bids into [0, 1] by dividing by the largest bid. All-zero input stays zero."""
    for name, value in raw.items():
        if _is_missing(value) or value < 0:
            raise ValueError(f"bid for {name!r} must be a number >= 0, got {value!r}")
    top = max(raw.values(), default=0.0)
    if top == 0:
        return {name: 0.0 for name in raw}
    return {name: value / top for name, value in raw.items()}


def rank_bids(bids: Mapping[str, float]) -> list[tuple[str, float]]:
    """Highest bid first; equal bids ordered by ds_division name (the documented tie-break)."""
    return sorted(bids.items(), key=lambda kv: (-kv[1], kv[0]))
