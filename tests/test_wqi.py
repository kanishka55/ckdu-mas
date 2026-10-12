import math

import pandas as pd
import pytest

from src.core import wqi
from src.sim import load_data


@pytest.fixture(scope="module")
def method():
    return wqi.method_from_frame(load_data.load_wqi_method())


def _at_standard(method, **overrides):
    """A reading with every one of the 13 parameters exactly on its standard Si."""
    row = {wqi.COLUMNS[p][0]: method.standards[p] / wqi.COLUMNS[p][1] for p in wqi.WQI_PARAMS}
    row.update(overrides)
    return row


def _frame(rows):
    return pd.DataFrame(rows, columns=["item", "type", "value", "unit", "source", "notes"])


def test_weights_match_supplementary_table_s1(method):
    assert sum(method.weights.values()) == 48
    expected = {"pH": 4, "TDS": 5, "hardness": 2, "alkalinity": 3, "Ca": 2, "Mg": 2, "Na": 3,
                "F": 5, "Cl": 4, "SO4": 4, "NO3": 5, "Fe": 4, "DOC": 5}
    assert method.weights == expected
    rel = wqi.relative_weights(method)
    assert math.isclose(sum(rel.values()), 1.0)
    # Table S1 prints Wi = 0.1042 for the weight-5 parameters and 0.0417 for weight 2
    assert round(rel["F"], 4) == 0.1042
    assert round(rel["hardness"], 4) == 0.0417


def test_sample_on_every_standard_scores_100(method):
    # Eq. 1-3 applied literally: every qi is 100 and the Wi sum to 1.
    assert wqi.score_well(_at_standard(method), method)["wqi"] == pytest.approx(100.0)


def test_paper_reference_point_90(method):
    # Cooray et al. Sec 3.4: "a sample that exactly meets the Sri Lankan standards" scores 90.
    # DOC has no SLS limit (4* in Table S1). The 12 SLS parameters on their limit with DOC = 0
    # gives 100 * (1 - 5/48) = 89.6, the closest reading of that sentence (docs/wqi_method.md).
    result = wqi.score_well(_at_standard(method, DOC_mg_L=0.0), method)
    assert result["wqi"] == pytest.approx(89.58, abs=0.01)
    assert round(result["wqi"], -1) == 90


def test_contributions_sum_to_wqi_and_q_follows_eq2(method):
    reading = _at_standard(method, F_mg_L=2.0, Fe_ug_L=150.0)
    result = wqi.score_well(reading, method)
    assert result["q"]["F"] == pytest.approx(200.0)
    assert result["q"]["Fe"] == pytest.approx(50.0)  # 150 ug/L = 0.15 mg/L against 0.3 mg/L
    assert sum(result["contributions"].values()) == pytest.approx(result["wqi"])


def test_missing_parameter_renormalises_and_is_recorded(method):
    reading = _at_standard(method)
    del reading["TDS_mg_L"]
    reading["NO3_mg_L"] = float("nan")
    result = wqi.score_well(reading, method)
    assert result["params_missing"] == ["TDS", "NO3"]
    assert result["weight_covered"] == pytest.approx(38 / 48)
    # renormalised, not zero-filled: a sample on every remaining standard still scores 100
    assert result["wqi"] == pytest.approx(100.0)


@pytest.mark.parametrize("bad", ["n/a", -0.5])
def test_non_numeric_or_negative_concentration_raises(method, bad):
    # a lab typo must not silently become "missing" and shrink the parameter set
    with pytest.raises(ValueError, match="F_mg_L"):
        wqi.score_frame(pd.DataFrame([_at_standard(method, F_mg_L=bad)]), method)


def test_reject_invalid_rows_keeps_blanks_and_drops_unusable_values():
    df = pd.DataFrame({"well_id": ["A", "B", "C", "D", "E", "F"],
                       "F_mg_L": [1.0, None, -1.0, "x", float("inf"), True]})
    valid, reasons = wqi.reject_invalid_rows(df)
    assert list(valid["well_id"]) == ["A", "B"]          # a blank is missing, not invalid
    assert valid["F_mg_L"].dtype == float
    assert [r.split(":")[0] for r in reasons] == ["C", "D", "E", "F"]


def test_duplicate_weight_row_raises():
    df = load_data.load_wqi_method()
    dup = df[df["item"] == "Weight wi: F"].assign(value="1")
    with pytest.raises(ValueError, match="more than one"):
        wqi.method_from_frame(pd.concat([df, dup], ignore_index=True))


def test_no_parameters_raises(method):
    with pytest.raises(ValueError):
        wqi.score_well({"well_id": "X"}, method)


def test_score_frame_on_synthetic_wells_uses_the_8_available_parameters(method):
    wells = load_data.load_synthetic_wells()
    scored = wqi.score_frame(wells, method)
    assert scored["wqi"].notna().all()
    assert set(scored["params_used"]) == {"pH;hardness;alkalinity;F;Cl;SO4;Fe;DOC"}
    assert scored["weight_covered"].iloc[0] == pytest.approx(31 / 48)
    contrib = scored[[f"c_{p}" for p in wqi.WQI_PARAMS]].sum(axis=1)
    assert (contrib - scored["wqi"]).abs().max() < 1e-9


def test_to_fill_weight_raises():
    df = load_data.load_wqi_method()
    df.loc[df["item"] == "Weight wi: Fe", "value"] = "TO_FILL"
    with pytest.raises(wqi.ToFillError, match="TO_FILL"):
        wqi.method_from_frame(df)


def test_missing_standard_row_raises():
    df = load_data.load_wqi_method()
    df = df[df["item"] != "Standard Si: Na"]
    with pytest.raises(wqi.ToFillError, match="Na"):
        wqi.method_from_frame(df)


def test_unknown_parameter_row_raises():
    df = load_data.load_wqi_method()
    extra = _frame([["Weight wi: Pb", "weight", "3", "", "", ""]])
    with pytest.raises(ValueError, match="Pb"):
        wqi.method_from_frame(pd.concat([df, extra], ignore_index=True))
