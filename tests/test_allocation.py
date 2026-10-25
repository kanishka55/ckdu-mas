import pytest

from src.core.allocation import allocate_equal_split, allocate_first_come

VILLAGES = ["Talawa", "Galnewa", "Kandy", "Mihintale", "Padaviya"]  # sorted: G, K, M, P, T


def test_equal_split_rotates_in_name_order_and_resumes_next_month():
    first, nxt = allocate_equal_split(VILLAGES, 2, 0)
    assert first == ["Galnewa", "Kandy"]
    second, nxt = allocate_equal_split(VILLAGES, 2, nxt)
    assert second == ["Mihintale", "Padaviya"]
    third, _ = allocate_equal_split(VILLAGES, 2, nxt)
    assert third == ["Talawa", "Galnewa"]


def test_equal_split_skips_holders_and_unavailable_without_using_a_unit():
    winners, nxt = allocate_equal_split(VILLAGES, 2, 0, held={"Galnewa"}, unavailable={"Kandy"})
    assert winners == ["Mihintale", "Padaviya"]
    assert nxt == 4  # resumes at Talawa


def test_equal_split_pool_larger_than_villages_serves_each_once():
    winners, _ = allocate_equal_split(VILLAGES, 10, 0, held={"Kandy"})
    assert sorted(winners) == ["Galnewa", "Mihintale", "Padaviya", "Talawa"]


def test_equal_split_empty_pool_or_villages():
    assert allocate_equal_split(VILLAGES, 0, 3) == ([], 3)
    assert allocate_equal_split([], 2, 0) == ([], 0)


def test_first_come_serves_needy_villages_in_arrival_order():
    arrival = ["Talawa", "Kandy", "Padaviya", "Mihintale"]
    needy = {"Talawa", "Padaviya", "Mihintale"}
    assert allocate_first_come(arrival, 2, needy) == ["Talawa", "Padaviya"]


def test_first_come_skips_holders_and_unavailable():
    arrival = ["Talawa", "Padaviya", "Mihintale"]
    needy = set(arrival)
    assert allocate_first_come(arrival, 2, needy, held={"Talawa"}, unavailable={"Padaviya"}) == ["Mihintale"]


def test_first_come_rejects_duplicate_arrivals_and_bad_pool():
    with pytest.raises(ValueError):
        allocate_first_come(["Talawa", "Talawa"], 1, {"Talawa"})
    with pytest.raises(ValueError):
        allocate_first_come(["Talawa"], -1, {"Talawa"})
    with pytest.raises(ValueError):
        allocate_equal_split(VILLAGES, True, 0)


def test_risk_weight_counts_only_villages_with_need():
    from src.core.allocation import risk_weight

    assert risk_weight(120.0, "Poor") == 120.0
    assert risk_weight(250.0, "Very Poor") == 250.0
    assert risk_weight(80.0, "Good") == 0.0
    assert risk_weight(30.0, "Excellent") == 0.0
    assert risk_weight(None, "Poor") == 0.0
    assert risk_weight(120.0, "Unknown") == 0.0
