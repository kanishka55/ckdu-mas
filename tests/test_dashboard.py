"""Pure dashboard data tests; do not require a Streamlit server."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dashboard.app import (
    available_months,
    load_dashboard_data,
    _valid_coordinates,
)


class TestDashboardData(unittest.TestCase):
    def test_demo_month_and_metrics_are_explicitly_fixture_backed(self) -> None:
        data = load_dashboard_data()
        self.assertEqual(available_months(data, demo=True), (1,))
        self.assertIn("Illustrative fixtures", data.demo_status)
        self.assertEqual(len(data.demo_risks), 3)
        comparisons = {metric.metric for metric in data.demo_metrics}
        self.assertIn("contract_net_minus_equal_split", comparisons)
        self.assertIn("contract_net_minus_first_come", comparisons)

    def test_team_months_remain_unavailable_without_team_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = load_dashboard_data(
                messages_path=Path(temp) / "missing-messages.csv",
                allocations_path=Path(temp) / "missing-allocations.csv",
            )
        self.assertEqual(available_months(data, demo=False), ())
        self.assertIn("Unavailable", data.team_risk_status)
        self.assertIn("Unavailable", data.team_allocation_status)

    def test_missing_inputs_have_visible_states_not_fake_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = load_dashboard_data(
                synthetic_wells_path=Path(temp) / "missing-wells.csv",
                divisions_path=Path(temp) / "missing-divisions.csv",
                reference_stats_path=Path(temp) / "missing-reference.csv",
            )
        self.assertEqual(data.synthetic_wells.status, "unavailable")
        self.assertEqual(data.synthetic_wells.rows, ())
        self.assertEqual(data.divisions.status, "unavailable")
        self.assertEqual(data.reference_stats.status, "unavailable")

    def test_map_coordinates_are_validated_without_inference(self) -> None:
        rows = (
            {"ds_division": "missing", "lat": "", "lon": ""},
            {"ds_division": "bad", "lat": "91", "lon": "0"},
            {"ds_division": "valid", "lat": "7.5", "lon": "80.2"},
        )
        points = _valid_coordinates(rows)
        self.assertEqual([p["ds_division"] for p in points], ["valid"])

    def test_repository_division_file_has_no_valid_coordinates(self) -> None:
        data = load_dashboard_data()
        self.assertEqual(_valid_coordinates(data.divisions.rows), ())


if __name__ == "__main__":
    unittest.main()
