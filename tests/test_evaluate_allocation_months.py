"""Allocation comparison when the caller attests which months every method was evaluated for."""
from src.sim.evaluate import AllocationRow, calculate_allocation_efficiency

POP = {"A": 10, "B": 20, "C": 30}
WEIGHT = {("A", 1): 2.0, ("B", 1): 0.0, ("C", 1): 1.0, ("A", 2): 2.0, ("B", 2): 3.0, ("C", 2): 1.0}


def _rows():
    return [
        AllocationRow(1, "RO_unit", "A", 1, 20.0, "contract_net"),
        AllocationRow(1, "RO_unit", "B", 1, 0.0, "equal_split"),
        AllocationRow(1, "RO_unit", "A", 1, 20.0, "first_come"),
        # month 2: the Contract Net and first-come place nothing new (durable unit already held)
        AllocationRow(2, "RO_unit", "C", 1, 30.0, "equal_split"),
    ]


def _by_scope(results):
    return {(r.metric, r.scope): r for r in results}


def test_without_attestation_a_method_with_no_rows_stays_unavailable():
    results = _by_scope(calculate_allocation_efficiency(
        _rows(), POP, WEIGHT, allocation_results_verified=True, risk_weight_definition="fixture"))
    assert results[("risk_weighted_population_served", "month=2;resource=RO_unit;method=contract_net")].status == "unavailable"
    assert not any(scope.startswith("all;") for _, scope in results)


def test_attested_months_count_no_rows_as_zero_and_report_run_totals():
    results = _by_scope(calculate_allocation_efficiency(
        _rows(), POP, WEIGHT, allocation_results_verified=True, risk_weight_definition="fixture",
        evaluated_month_resources=[(1, "RO_unit"), (2, "RO_unit"), (3, "RO_unit")]))
    month2_cn = results[("risk_weighted_population_served", "month=2;resource=RO_unit;method=contract_net")]
    assert (month2_cn.status, month2_cn.value) == ("ok", 0.0)
    # month 3: nobody placed anything; still reported, all zero
    assert results[("risk_weighted_population_served", "month=3;resource=RO_unit;method=equal_split")].value == 0.0
    totals = {m: results[("risk_weighted_population_served", f"all;resource=RO_unit;method={m}")].value
              for m in ("contract_net", "equal_split", "first_come")}
    assert totals == {"contract_net": 20.0, "equal_split": 30.0, "first_come": 20.0}
    assert results[("contract_net_minus_equal_split", "all;resource=RO_unit")].value == -10.0
    assert results[("contract_net_minus_first_come", "all;resource=RO_unit")].value == 0.0
