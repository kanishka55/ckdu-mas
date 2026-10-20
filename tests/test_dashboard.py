"""Pure dashboard data preparation tests; no Streamlit server is required."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from dashboard.app import (
    available_allocation_months,
    available_months,
    load_dashboard_data,
    _metrics_for_month,
    prepare_evaluation_display,
    Dataset,
    _valid_coordinates,
    _show_demo_rows,
    load_local_demo_data,
    _render_evaluation_summary,
)


class TestDashboardData(unittest.TestCase):
    def _write_csv(self, path: Path, columns: list[str], rows: list[list[str]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(rows)

    def test_demo_risk_index_and_allocation_month_stay_separate(self) -> None:
        data = load_dashboard_data()
        self.assertEqual(available_months(data, demo=True), (0,))
        self.assertEqual(available_allocation_months(data, demo=True), (1,))
        self.assertIn("Illustrative fixtures", data.demo_status)
        self.assertEqual(len(data.demo_risks), 3)
        self.assertTrue(data.demo_metrics)
        self.assertTrue(all(metric.value is None and metric.status == "unavailable"
                            for metric in data.demo_metrics))

    def test_missing_member_b_outputs_and_allocations_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = load_dashboard_data(
                risk_scores_path=root / "missing-risk.csv",
                well_scores_path=root / "missing-wells.csv",
                allocations_path=root / "missing-allocations.csv",
            )
        self.assertEqual(data.team_risks, ())
        self.assertIsInstance(data.team_wells, Dataset)
        self.assertEqual(data.team_wells.rows, ())
        self.assertEqual(data.team_wells.status, "unavailable")
        self.assertEqual(available_months(data, demo=False), ())
        self.assertIn("unavailable", data.team_risk_status.casefold())
        self.assertIn("unavailable", data.team_well_status.casefold())
        self.assertIn("Unavailable", data.team_allocation_status)

    def test_valid_member_b_outputs_use_month_index_and_preserve_unknown(self) -> None:
        risk_columns = ["month_index", "month", "ds_division", "source", "wqi", "tier", "drivers",
                        "n_wells", "n_scored", "share_poor_or_worse", "worst_tier", "weight_covered",
                        "params_used", "driver_contributions", "n_rejected", "rejected", "error"]
        well_columns = ["month_index", "month", "ds_division", "district", "ckdu_prevalence_class",
                        "source", "well_id", "wqi", "tier", "q_F"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            risk_path, well_path = root / "risk_scores.csv", root / "well_scores.csv"
            self._write_csv(risk_path, risk_columns, [[
                "0", "1", "Synthetic Division", "synthetic", "", "Unknown", "[]",
                "1", "0", "", "Unknown", "0.0", "[]", "{}", "1", "fixture error", "fixture",
            ]])
            self._write_csv(well_path, well_columns, [[
                "0", "1", "Synthetic Division", "District", "Mild", "synthetic",
                "synthetic-well-1", "", "Unknown", "",
            ]])
            data = load_dashboard_data(risk_scores_path=risk_path, well_scores_path=well_path)
        self.assertEqual(available_months(data, demo=False), (0,))
        self.assertEqual(data.team_risks[0]["tier"], "Unknown")
        self.assertEqual(data.team_risks[0]["wqi"], "")
        self.assertIsInstance(data.team_wells, Dataset)
        self.assertEqual(data.team_wells.rows[0]["well_id"], "synthetic-well-1")
        self.assertIn("Member B", data.team_risk_source_label)

    def test_empty_member_b_csvs_are_empty_not_zero_valued(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            risk_path, well_path = root / "risk_scores.csv", root / "well_scores.csv"
            self._write_csv(risk_path, ["month_index", "ds_division", "wqi", "tier", "drivers", "source"], [])
            self._write_csv(well_path, ["month_index", "month", "ds_division", "district",
                                        "ckdu_prevalence_class", "source", "well_id", "wqi", "tier"], [])
            data = load_dashboard_data(risk_scores_path=risk_path, well_scores_path=well_path)
        self.assertEqual(data.team_risks, ())
        self.assertEqual(data.team_wells.rows, ())
        self.assertIn("No records", data.team_risk_status)
        self.assertIn("No records", data.team_well_status)

    def test_malformed_and_incomplete_member_b_files_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            malformed, incomplete = root / "malformed.csv", root / "incomplete.csv"
            malformed.write_text("\ufeff\"unterminated", encoding="utf-8")
            self._write_csv(incomplete, ["month_index", "ds_division", "wqi", "tier", "source"], [
                ["bad-index", "Fixture", "", "Unknown", "synthetic"]
            ])
            data = load_dashboard_data(risk_scores_path=incomplete, well_scores_path=malformed)
        self.assertIn("Incomplete", data.team_risk_status)
        self.assertIn("Incomplete", data.team_well_status)
        self.assertEqual(data.team_risks, ())
        self.assertEqual(data.team_wells.rows, ())
        self.assertEqual(data.team_wells.status, "incomplete")

    def test_present_allocation_rows_are_not_claimed_as_verified_team_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "allocations.csv"
            self._write_csv(path, ["month", "resource", "ds_division", "round", "winning_bid", "method"], [
                ["1", "bowser", "Fixture", "1", "0.8", "contract_net"]
            ])
            data = load_dashboard_data(allocations_path=path)
        self.assertEqual(len(data.team_allocations), 1)
        self.assertIn("unverified", data.team_allocation_status.casefold())

    def test_unavailable_evaluator_metric_never_renders_as_zero(self) -> None:
        dataset = Dataset(({
            "metric": "alert_latency", "scope": "month=1;resource=bowser",
            "value": "0", "unit": "seconds", "status": "unavailable",
            "observations": "0", "details": "{}",
        },), "available", "1 rows", "fixture summary")
        metric, = _metrics_for_month(dataset, 1)
        self.assertIsNone(metric.value)
        self.assertEqual(metric.status, "unavailable")

    def test_evaluation_display_preserves_available_values_and_hides_allocation_score(self) -> None:
        dataset = Dataset((
            {"metric": "risk_classification_alignment", "scope": "all", "value": "0.75",
             "unit": "proportion", "status": "partial", "observations": "4", "details": "{}"},
            {"metric": "contract_net_minus_equal_split", "scope": "month=1;resource=bowser",
             "value": "12", "unit": "risk-weighted persons", "status": "ok",
             "observations": "3", "details": "{}"},
        ), "available", "2 rows", "Member D evaluation summary")
        status, rows = prepare_evaluation_display(dataset)
        self.assertIn("Available", status)
        self.assertEqual(rows[0]["Value"], 0.75)
        self.assertEqual(rows[0]["Status"], "partial")
        self.assertEqual(rows[1]["Value"], "Unavailable")
        self.assertEqual(rows[1]["Status"], "unavailable")
        self.assertIn("provenance", rows[1]["Details"])

    def test_evaluation_display_shows_unavailable_without_zero(self) -> None:
        dataset = Dataset(({
            "metric": "alert_latency", "scope": "all", "value": "0", "unit": "seconds",
            "status": "unavailable", "observations": "0", "details": '{"reason":"no pair"}',
        },), "available", "1 row", "Member D evaluation summary")
        _, rows = prepare_evaluation_display(dataset)
        self.assertEqual(rows[0]["Value"], "Unavailable")
        self.assertEqual(rows[0]["Status"], "unavailable")
        self.assertEqual(rows[0]["Details"], "no pair")

    def test_evaluation_display_handles_absent_empty_and_malformed_summaries(self) -> None:
        absent = Dataset((), "unavailable", "file not found", "Member D evaluation summary")
        empty = Dataset((), "empty", "No records", "Member D evaluation summary", (
            "metric", "scope", "value", "unit", "status", "observations", "details",
        ))
        malformed = Dataset(({
            "metric": "risk_classification_alignment", "scope": "all", "value": "NaN",
            "unit": "proportion", "status": "ok", "observations": "1", "details": "{}",
        },), "available", "1 row", "Member D evaluation summary")
        self.assertIn("Unavailable", prepare_evaluation_display(absent)[0])
        self.assertIn("Empty", prepare_evaluation_display(empty)[0])
        self.assertIn("Incomplete", prepare_evaluation_display(malformed)[0])
        self.assertEqual(prepare_evaluation_display(malformed)[1], ())

    def test_missing_evaluation_summary_is_rendered_once(self) -> None:
        class Recorder:
            def __init__(self):
                self.calls = []

            def subheader(self, value):
                self.calls.append(("subheader", value))

            def caption(self, value):
                self.calls.append(("caption", value))

            def info(self, value):
                self.calls.append(("info", value))

            def dataframe(self, rows, **kwargs):
                self.calls.append(("dataframe", rows))

        absent = Dataset((), "unavailable", "File not found: evaluation_summary.csv",
                         "Member D evaluation summary")
        recorder = Recorder()
        _render_evaluation_summary(recorder, absent, context="Evaluation source context.")

        message = "Unavailable: File not found: evaluation_summary.csv"
        rendered = [value for kind, value in recorder.calls if kind in {"caption", "info"}]
        self.assertEqual(rendered.count(message), 1)
        self.assertEqual([kind for kind, _ in recorder.calls].count("info"), 1)
        self.assertEqual([kind for kind, _ in recorder.calls].count("dataframe"), 0)

    def test_allocation_metric_month_scope_and_verification_gate(self) -> None:
        dataset = Dataset(({
            "metric": "contract_net_minus_first_come", "scope": "month=2;resource=RO_unit",
            "value": "5", "unit": "risk-weighted persons", "status": "ok",
            "observations": "2", "details": "{}",
        },), "available", "1 row", "Member D evaluation summary")
        self.assertEqual(_metrics_for_month(dataset, 1), ())
        gated, = _metrics_for_month(dataset, 2)
        self.assertIsNone(gated.value)
        self.assertEqual(gated.status, "unavailable")
        verified, = _metrics_for_month(dataset, 2, allocation_results_verified=True)
        self.assertEqual(verified.value, 5.0)

    def test_allocation_efficiency_summary_is_gated_without_verified_evidence(self) -> None:
        dataset = Dataset(({
            "metric": "allocation_efficiency", "scope": "all", "value": "42",
            "unit": "risk-weighted persons", "status": "ok", "observations": "3", "details": "{}",
        },), "available", "1 row", "Member D evaluation summary")
        _, rows = prepare_evaluation_display(dataset)
        self.assertEqual(rows[0]["Value"], "Unavailable")
        self.assertEqual(rows[0]["Status"], "unavailable")
        self.assertIn("provenance", rows[0]["Details"])

    def test_missing_reference_inputs_have_visible_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = load_dashboard_data(
                synthetic_wells_path=root / "missing-wells.csv",
                divisions_path=root / "missing-divisions.csv",
                reference_stats_path=root / "missing-reference.csv",
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
        self.assertEqual([p["ds_division"] for p in _valid_coordinates(rows)], ["valid"])

    def test_demo_render_helper_renders_table_or_empty_state_without_expression_output(self) -> None:
        class Recorder:
            def __init__(self):
                self.calls = []

            def dataframe(self, rows, **kwargs):
                self.calls.append(("dataframe", rows))
                return object()

            def info(self, message):
                self.calls.append(("info", message))
                return object()

            def write(self, value):
                self.calls.append(("write", value))

        st = Recorder()
        self.assertIsNone(_show_demo_rows(st, ({"status": "active"},), "Empty table"))
        self.assertIsNone(_show_demo_rows(st, (), "Empty table"))
        self.assertEqual([kind for kind, _value in st.calls], ["dataframe", "info"])
        self.assertEqual(st.calls[1][1], "Empty table")

    def test_repository_division_coordinates_are_approximate_town_points(self) -> None:
        data = load_dashboard_data()
        points = _valid_coordinates(data.divisions.rows)
        self.assertTrue(points)
        self.assertTrue(all("approximate" in point["notes"] for point in points))
        self.assertIn("approximate division town points", data.divisions.source_label)

    def test_generated_demo_loader_is_separate_and_labels_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "logs").mkdir()
            (root / "provenance.json").write_text(
                '{"provenance":"illustrative_local_demo","stages":{"risk_scoring":"unavailable"}}',
                encoding="utf-8",
            )
            risk_columns = ["month_index", "month", "ds_division", "source", "wqi", "tier", "drivers"]
            self._write_csv(root / "logs/risk_scores.csv", risk_columns, [[
                "0", "10", "Fixture Division", "synthetic", "", "Unknown", "[]",
            ]])
            data = load_local_demo_data(root)
        self.assertEqual(data.manifest["provenance"], "illustrative_local_demo")
        self.assertEqual(data.risks.rows[0]["tier"], "Unknown")
        self.assertIn("Synthetic", data.risks.source_label)
        self.assertEqual(data.allocations.status, "unavailable")
        self.assertEqual(data.metrics.status, "unavailable")
        self.assertEqual(data.divisions.status, "unavailable")

    def test_generated_demo_loader_reports_malformed_empty_and_missing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "logs").mkdir()
            (root / "logs/risk_scores.csv").write_text("month_index,ds_division\nbad,Fixture\n", encoding="utf-8")
            (root / "hotspot_screening.csv").write_text("wrong,header\nx,y\n", encoding="utf-8")
            data = load_local_demo_data(root)
        self.assertEqual(data.risks.status, "incomplete")
        self.assertIn("Unexpected columns", data.hotspots.message)
        self.assertEqual(data.alerts.status, "unavailable")

    def test_generated_demo_allocation_summary_is_not_verified(self) -> None:
        dataset = Dataset(({
            "metric": "allocation_efficiency", "scope": "all", "value": "100",
            "unit": "risk-weighted persons", "status": "ok", "observations": "1", "details": "{}",
        },), "available", "1 row", "local illustrative evaluator fixture")
        _, rows = prepare_evaluation_display(dataset, allocation_results_verified=False)
        self.assertEqual(rows[0]["Value"], "Unavailable")
        self.assertEqual(rows[0]["Status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
