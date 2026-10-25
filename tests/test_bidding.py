import math

import pytest

from src.core import risk_tiers
from src.core.bidding import bid, has_need, holds_durable_unit, normalise_bids, rank_bids
from src.sim.load_data import load_wqi_method


def test_formula_is_risk_times_population():
    assert bid(2.0, 1000) == pytest.approx(2000.0)


def test_more_risk_or_population_bids_more():
    assert bid(2.0, 1000) > bid(1.0, 1000)
    assert bid(1.0, 2000) > bid(1.0, 1000)


def test_population_zero_bids_zero():
    assert bid(1.0, 0) == 0.0


@pytest.mark.parametrize("population", [None, math.nan])
def test_population_missing_raises(population):
    with pytest.raises(ValueError, match="population"):
        bid(1.0, population)


@pytest.mark.parametrize("risk", [None, math.nan])
def test_risk_missing_raises(risk):
    with pytest.raises(ValueError, match="risk"):
        bid(risk, 100)


def test_zero_risk_bids_zero():
    assert bid(0.0, 1000) == 0.0


@pytest.mark.parametrize("args", [(-1.0, 10), (1.0, -10)])
def test_negative_inputs_raise(args):
    with pytest.raises(ValueError):
        bid(*args)


def test_continuous_across_a_tier_boundary():
    # bid is a continuous function of risk: no jump either side of a boundary value
    eps = 1e-9
    assert bid(100.0 + eps, 1000) == pytest.approx(bid(100.0 - eps, 1000))


@pytest.mark.parametrize("tier,expected", [
    ("Excellent", False), ("Good", False),
    ("Poor", True), ("Very Poor", True), ("Unsuitable", True),
    (None, False), ("Unknown", False), ("STUB-unscored", False),
])
def test_has_need_only_for_poor_or_worse(tier, expected):
    assert has_need(tier) is expected


def test_has_need_follows_published_class_boundary():
    # the need boundary is the ckdu_02 Good/Poor limit, not a threshold defined in bidding.py
    classes = risk_tiers.classes_from_frame(load_wqi_method())
    assert not has_need(risk_tiers.tier_for_wqi(99.9, classes))
    assert has_need(risk_tiers.tier_for_wqi(100.0, classes))


def test_normalise_scales_to_unit_range():
    out = normalise_bids({"A": 50.0, "B": 200.0, "C": 0.0})
    assert out == {"A": 0.25, "B": 1.0, "C": 0.0}


def test_normalise_all_zero_and_empty():
    assert normalise_bids({"A": 0.0, "B": 0.0}) == {"A": 0.0, "B": 0.0}
    assert normalise_bids({}) == {}


def test_normalise_rejects_bad_values():
    with pytest.raises(ValueError):
        normalise_bids({"A": -1.0})
    with pytest.raises(ValueError):
        normalise_bids({"A": math.nan})


def test_identical_bids_tie_break_by_name():
    ranked = rank_bids({"Talawa": 0.5, "Galnewa": 0.5, "Padaviya": 0.5})
    assert [n for n, _ in ranked] == ["Galnewa", "Padaviya", "Talawa"]


def test_rank_highest_first():
    assert rank_bids({"A": 0.1, "B": 0.9})[0] == ("B", 0.9)


def test_ro_unit_is_durable_and_blocks_a_second_bid():
    assert holds_durable_unit("RO_unit", {"RO_unit": 3})
    assert not holds_durable_unit("RO_unit", {})


def test_bowser_is_recurring_and_never_held():
    assert not holds_durable_unit("bowser", {"RO_unit": 3})
    assert not holds_durable_unit("bowser", {"bowser": 3})
