import math

import pandas as pd
import pytest

from src.core import ontology, risk_tiers, wqi
from src.sim import load_data


@pytest.fixture(scope="module")
def method_frame():
    return load_data.load_wqi_method()


@pytest.fixture(scope="module")
def classes(method_frame):
    return risk_tiers.classes_from_frame(method_frame)


@pytest.fixture(scope="module")
def method(method_frame):
    return wqi.method_from_frame(method_frame)


def test_classes_are_table_s2(classes):
    assert [(c.label, c.lower, c.upper) for c in classes] == [
        ("Excellent", 0, 50), ("Good", 50, 100), ("Poor", 100, 200),
        ("Very Poor", 200, 300), ("Unsuitable", 300, math.inf),
    ]


@pytest.mark.parametrize("value,label", [
    (0, "Excellent"), (49.9, "Excellent"), (50, "Good"), (99.99, "Good"), (100, "Poor"),
    (199.9, "Poor"), (200, "Very Poor"), (300, "Unsuitable"), (1e6, "Unsuitable"),
])
def test_tier_boundaries_are_lower_inclusive(classes, value, label):
    assert risk_tiers.tier_for_wqi(value, classes) == label


def test_missing_wqi_is_unknown_and_negative_raises(classes):
    assert risk_tiers.tier_for_wqi(None, classes) == risk_tiers.UNKNOWN_TIER
    assert risk_tiers.tier_for_wqi(float("nan"), classes) == risk_tiers.UNKNOWN_TIER
    with pytest.raises(ValueError):
        risk_tiers.tier_for_wqi(-1, classes)


def test_class_gap_or_to_fill_raises(method_frame):
    gap = method_frame.copy()
    gap.loc[gap["item"] == "WQI class: Good", "value"] = "50-90"
    with pytest.raises(ValueError, match="contiguous"):
        risk_tiers.classes_from_frame(gap)
    todo = method_frame.copy()
    todo.loc[todo["item"] == "WQI class: Poor", "value"] = "TO_FILL"
    with pytest.raises(wqi.ToFillError):
        risk_tiers.classes_from_frame(todo)


def _wells(method, f_values):
    """Wells on every standard except fluoride, which drives the score up or down."""
    rows = []
    for f in f_values:
        row = {wqi.COLUMNS[p][0]: method.standards[p] / wqi.COLUMNS[p][1] for p in wqi.WQI_PARAMS}
        row["F_mg_L"] = f
        rows.append(row)
    return wqi.score_frame(pd.DataFrame(rows), method)


def test_village_uses_median_so_one_bad_well_is_flagged_not_dominant(method, classes):
    # 19 wells around WQI ~ 90 and one far above the standard on fluoride
    scored = _wells(method, [0.0] * 19 + [30.0])
    v = risk_tiers.village_risk(scored, classes)
    assert v["tier"] == "Good"
    assert v["worst_tier"] in {"Very Poor", "Unsuitable"}
    assert v["share_poor_or_worse"] == pytest.approx(1 / 20)
    assert v["n_wells"] == v["n_scored"] == 20


def test_drivers_are_only_parameters_above_their_standard(method, classes):
    # every parameter sits on its standard (qi = 100) except fluoride at 5x
    v = risk_tiers.village_risk(_wells(method, [5.0, 5.0, 5.0]), classes)
    assert v["drivers"] == ["F"]


def test_drivers_ranked_by_contribution_and_capped(method, classes):
    row = {wqi.COLUMNS[p][0]: method.standards[p] / wqi.COLUMNS[p][1] for p in wqi.WQI_PARAMS}
    # F (wi 5) at 2x, Cl (wi 4) at 3x, SO4 (wi 4) at 1.5x, Mg (wi 2) at 1.2x
    row.update(F_mg_L=2.0, Cl_mg_L=750.0, SO4_mg_L=375.0, Mg_mg_L=36.0)
    v = risk_tiers.village_risk(wqi.score_frame(pd.DataFrame([row]), method), classes)
    assert v["drivers"] == ["Cl", "F", "SO4"]  # contributions 25.0, 20.8, 12.5; Mg dropped


def test_clean_village_has_no_drivers(method, classes):
    v = risk_tiers.village_risk(_wells(method, [0.5, 0.5]), classes)
    assert v["drivers"] == []


def test_sent_wqi_maps_back_to_the_sent_tier(classes):
    scored = pd.DataFrame({"wqi": [99.97], "params_used": ["F"], "weight_covered": [1.0],
                           **{f"q_{p}": [float("nan")] for p in wqi.WQI_PARAMS},
                           **{f"c_{p}": [float("nan")] for p in wqi.WQI_PARAMS}})
    body = risk_tiers.risk_record(0, "X", risk_tiers.village_risk(scored, classes))
    assert risk_tiers.tier_for_wqi(body["wqi"], classes) == body["tier"]


def test_village_with_no_scorable_wells(classes):
    scored = pd.DataFrame({"wqi": [float("nan")] * 3, "params_used": [""] * 3,
                           "weight_covered": [0.0] * 3})
    v = risk_tiers.village_risk(scored, classes)
    assert v["wqi"] is None and v["tier"] == risk_tiers.UNKNOWN_TIER


def test_risk_record_matches_the_ontology(method, classes):
    v = risk_tiers.village_risk(_wells(method, [0.5, 1.5, 3.0]), classes)
    body = risk_tiers.risk_record(4, "Padaviya", v)
    ontology.validate(ontology.INFORM, body)
    assert body["month_index"] == 4 and body["tier"] == v["tier"]
    empty = risk_tiers.risk_record(0, "Kandy", {"wqi": None, "tier": "Unknown", "drivers": []})
    ontology.validate(ontology.INFORM, empty)
