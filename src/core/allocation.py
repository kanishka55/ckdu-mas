"""Baseline allocators to compare against the Contract Net. Pure module: no SPADE imports.

Both baselines answer "which villages get a unit this month" for the same pool the Contract Net
negotiates, so their results are directly comparable on risk-weighted population served
(see src/sim/evaluate.py). Each method keeps its own record of who already holds a durable unit
(``bidding.DURABLE_RESOURCES``), exactly as villages do in the Contract Net, so no method gets a
second RO unit for the same village.

- ``allocate_equal_split``: ignores risk. The pool cannot be split evenly over 11 villages in one
  month, so it is divided evenly over time: a round-robin over villages in name order, continuing
  where the previous month stopped.
- ``allocate_first_come``: ignores how large the need is. Among villages that have a need this
  month, serve them in the order their replies reached the coordinator.

Villages that cannot take delivery this month (``unavailable``) are skipped by both, which matches
the Contract Net re-offering their unit to the next village.
"""
from __future__ import annotations

from collections.abc import Collection, Sequence

from src.core import bidding

RISK_WEIGHT_DEFINITION = (
    "need-gated village WQI: the village median WQI that month if its tier is Poor, Very Poor or "
    "Unsuitable (bidding.has_need, published WQI 100 class boundary), else 0. Project definition, "
    "not a published weight."
)


def risk_weight(wqi: float | None, tier: str | None) -> float:
    """Weight for "risk-weighted population served": WQI when the village has a need, else 0.

    Plain WQI would credit serving a village whose water is Good (WQI ~80) almost as much as a Poor
    one, so a method that ignores need (equal split) would score well for serving large villages
    that did not need the unit. Gating on the same need rule the villages bid with avoids that.
    """
    if wqi is None or not bidding.has_need(tier):
        return 0.0
    return float(wqi)


def _check_pool(pool: int) -> None:
    if isinstance(pool, bool) or not isinstance(pool, int) or pool < 0:
        raise ValueError("pool must be a non-negative integer")


def allocate_equal_split(
    villages: Sequence[str],
    pool: int,
    start: int,
    held: Collection[str] = (),
    unavailable: Collection[str] = (),
) -> tuple[list[str], int]:
    """Round-robin ``pool`` units over ``villages`` (name order), starting at position ``start``.

    Returns (winners, next_start). Villages in ``held`` or ``unavailable`` are skipped and do not
    use up a unit. ``next_start`` is the position after the last village served, so the rotation
    resumes there next month.
    """
    _check_pool(pool)
    order = sorted(villages)
    if not order:
        return [], 0
    winners: list[str] = []
    position = start % len(order)
    for step in range(len(order)):
        if len(winners) == pool:
            break
        index = (start + step) % len(order)
        village = order[index]
        if village in held or village in unavailable:
            continue
        winners.append(village)
        position = (index + 1) % len(order)
    return winners, position


def allocate_first_come(
    arrival_order: Sequence[str],
    pool: int,
    needy: Collection[str],
    held: Collection[str] = (),
    unavailable: Collection[str] = (),
) -> list[str]:
    """The first ``pool`` villages in ``arrival_order`` that have a need and can take a unit."""
    _check_pool(pool)
    if len(set(arrival_order)) != len(arrival_order):
        raise ValueError("arrival_order must list each village at most once")
    eligible = [v for v in arrival_order if v in needy and v not in held and v not in unavailable]
    return eligible[:pool]
