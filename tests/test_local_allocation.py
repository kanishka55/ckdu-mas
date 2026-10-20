"""Dependency-free tests for the illustrative local allocation engine."""

import csv
import tempfile
import unittest
from pathlib import Path

from src.core.local_allocation import (
    AllocationInputError,
    DECISION_COLUMNS,
    METHODS,
    PROJECT_ROOT,
    allocate_local,
    write_allocation_report,
)


def record(division, tier="Poor", wqi=125.0, month_index=0):
    """Create illustrative ontology-shaped test data, never team observations."""
    return {
        "type": "risk_record",
        "month_index": month_index,
        "ds_division": division,
        "wqi": wqi,
        "tier": tier,
        "drivers": ["illustrative_test_driver"],
    }


class LocalAllocationTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            record("Alpha", wqi=100.0),
            record("Bravo", wqi=100.0),
            record("Charlie", wqi=200.0),
            record("No Need", tier="Good", wqi=80.0),
        ]
        self.population = {"Alpha": 100, "Bravo": 100, "Charlie": 50, "No Need": 80}

    def allocate(self, **overrides):
        options = {
            "risk_records": self.records,
            "population_by_division": self.population,
            "month_index": 0,
            "resource": "RO_unit",
            "resource_pool_size": 2,
        }
        options.update(overrides)
        return allocate_local(**options)

    @staticmethod
    def allocated(result, method):
        return {
            row.ds_division for row in result.decisions
            if row.method == method and row.status == "allocated"
        }

    def test_repeated_runs_are_deterministic(self):
        self.assertEqual(self.allocate(), self.allocate())

    def test_each_method_conserves_resource_pool_and_awards_at_most_one_unit(self):
        result = self.allocate()
        for method in METHODS:
            awards = [row for row in result.decisions if row.method == method and row.status == "allocated"]
            self.assertLessEqual(sum(row.units for row in awards), 2)
            self.assertTrue(all(row.units == 1 for row in awards))

    def test_contract_net_ties_use_alphabetical_division_name(self):
        result = self.allocate(resource_pool_size=1)
        self.assertEqual(self.allocated(result, "local_contract_net_style"), {"Alpha"})

    def test_equal_split_remainder_uses_alphabetical_order(self):
        result = self.allocate(resource_pool_size=1)
        self.assertEqual(self.allocated(result, "equal_split"), {"Alpha"})

    def test_first_come_uses_configured_order_and_default_is_explicit(self):
        configured = self.allocate(resource_pool_size=1, arrival_order=("Charlie", "Bravo", "Alpha", "No Need"))
        self.assertEqual(self.allocated(configured, "first_come"), {"Charlie"})
        default = self.allocate(resource_pool_size=1)
        self.assertEqual(default.arrival_order_source, "provisional_alphabetical_synthetic_arrival_order")
        self.assertEqual(self.allocated(default, "first_come"), {"Alpha"})

    def test_zero_pool_is_valid_and_allocates_nothing(self):
        result = self.allocate(resource_pool_size=0)
        self.assertEqual(result.status, "available")
        self.assertFalse(any(row.status == "allocated" for row in result.decisions))
        self.assertTrue(all(row.reason == "resource_pool_empty" for row in result.decisions
                            if row.status == "not_allocated"))

    def test_negative_boolean_and_noninteger_pools_are_rejected(self):
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(AllocationInputError):
                self.allocate(resource_pool_size=value)

    def test_unknown_risk_is_unavailable_and_kept_for_each_method(self):
        records = [record("Known"), record("Unknown Division", tier="Unknown", wqi=None)]
        result = self.allocate(
            risk_records=records,
            population_by_division={"Known": 10, "Unknown Division": 20},
            resource_pool_size=1,
        )
        unknown_rows = [row for row in result.decisions if row.ds_division == "Unknown Division"]
        self.assertEqual(len(unknown_rows), len(METHODS))
        self.assertTrue(all(row.status == "unavailable" for row in unknown_rows))
        self.assertTrue(all(row.reason == "risk_tier_unknown" for row in unknown_rows))
        self.assertEqual(result.status, "partial")

    def test_missing_risk_and_population_are_explicitly_unavailable(self):
        missing_risk = self.allocate(
            risk_records=[], population_by_division={"No Risk Record": 10}, resource_pool_size=1,
        )
        missing_population = self.allocate(
            risk_records=[record("No Population")], population_by_division={}, resource_pool_size=1,
        )
        self.assertTrue(all(row.reason == "risk_record_missing" for row in missing_risk.decisions))
        self.assertTrue(all(row.reason == "population_missing" for row in missing_population.decisions))

    def test_missing_wqi_invalid_tier_and_bad_population_are_unavailable(self):
        records = [record("No WQI", wqi=None), record("Bad Tier", tier="Other"), record("Bad Pop")]
        result = self.allocate(
            risk_records=records,
            population_by_division={"No WQI": 1, "Bad Tier": 1, "Bad Pop": "many"},
        )
        reasons = {row.ds_division: row.reason for row in result.decisions if row.method == METHODS[0]}
        self.assertEqual(reasons["No WQI"], "wqi_missing")
        self.assertEqual(reasons["Bad Tier"], "unsupported_risk_tier:Other")
        self.assertEqual(reasons["Bad Pop"], "population_invalid")

    def test_structurally_incomplete_or_duplicate_records_fail_loudly(self):
        incomplete = record("Incomplete")
        del incomplete["drivers"]
        with self.assertRaisesRegex(AllocationInputError, "violates the ontology"):
            self.allocate(risk_records=[incomplete], population_by_division={"Incomplete": 1})
        with self.assertRaisesRegex(AllocationInputError, "duplicate risk record"):
            self.allocate(risk_records=[record("Dup"), record("Dup")], population_by_division={"Dup": 1})

    def test_baselines_use_the_same_eligible_candidate_set(self):
        result = self.allocate(resource_pool_size=3)
        for method in METHODS:
            rows = [row for row in result.decisions if row.method == method]
            self.assertEqual({row.ds_division for row in rows}, set(self.population))
            self.assertEqual(
                {row.ds_division for row in rows if row.status == "ineligible"},
                {"No Need"},
            )
        for method in METHODS:
            self.assertEqual(len(self.allocated(result, method)), 3)

    def test_invalid_first_come_order_marks_that_baseline_unavailable(self):
        result = self.allocate(arrival_order=("Alpha", "Alpha"))
        rows = [row for row in result.decisions if row.method == "first_come"]
        self.assertTrue(all(row.status == "unavailable" for row in rows if row.tier == "Poor"))
        self.assertTrue(all(row.reason == "arrival_order_must_list_each_division_exactly_once"
                            for row in rows if row.tier == "Poor"))

    def test_output_schema_provenance_and_protected_path(self):
        result = self.allocate()
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = write_allocation_report(result, temp_dir)
            with output_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            self.assertEqual(tuple(reader.fieldnames), DECISION_COLUMNS)
            self.assertTrue(rows)
            self.assertEqual({row["provenance"] for row in rows}, {"illustrative_local_demo"})
            self.assertEqual(len(rows), len(result.decisions))
        with self.assertRaisesRegex(ValueError, "protected path"):
            write_allocation_report(result, PROJECT_ROOT / "logs")


if __name__ == "__main__":
    unittest.main()
