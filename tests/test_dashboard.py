"""Pure dashboard data preparation tests; no Streamlit server is required."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.core import msg_log, ontology
from src.sim.agent_monitor import load_agent_monitor
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
    _live_coordination_model,
    _render_live_coordination,
    _render_live_coordination_snapshot,
    _coordination_event_source,
    _live_coordination_run_options,
    _selected_run_id,
    _lab_run_history_rows,
    _status_callout,
)


class TestDashboardData(unittest.TestCase):
    def _write_csv(self, path: Path, columns: list[str], rows: list[list[str]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(rows)

    def test_completed_with_expected_warmup_and_unavailable_evidence_has_clear_callout(self) -> None:
        class Recorder:
            def __init__(self):
                self.warnings = []

            def warning(self, text):
                self.warnings.append(text)

        recorder = Recorder()
        decisions = {
            "unavailable_explanations": ({"expected": True}, {"expected": True}),
            "unavailable_stages": (),
            "failures": (),
            "evaluation_status": "unavailable",
            "unavailable_metrics": ({"status": "unavailable"},),
        }
        _status_callout(recorder, "completed_with_unavailable_stages", "run-review", decisions)
        self.assertEqual(len(recorder.warnings), 1)
        self.assertIn("completed with unavailable stages", recorder.warnings[0])
        self.assertIn("expected hotspot-history warm-up", recorder.warnings[0])
        self.assertIn("evaluation metrics unavailable for missing evidence", recorder.warnings[0])

    def test_live_coordination_model_preserves_logged_fields_and_unavailable_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "run"
            (root / "logs").mkdir(parents=True)
            now = datetime.now(timezone.utc)
            (root / "run_manifest.json").write_text(json.dumps({
                "run_id": "live-view-fixture", "status": "running", "configuration": {},
            }), encoding="utf-8")
            events = [
                {"event_type": "runner_heartbeat", "timestamp": now.isoformat(), "component": "SimulationRunner"},
                {"event_type": "month_started", "timestamp": now.isoformat(), "component": "SimulationRunner",
                 "month_index": 2},
                {"event_type": "agent_started", "timestamp": now.isoformat(), "event_seq": 1,
                 "component": "ClockAgent", "identity": "clock@localhost"},
                {"event_type": "agent_heartbeat", "timestamp": now.isoformat(), "event_seq": 2,
                 "component": "ClockAgent", "identity": "clock@localhost"},
            ]
            (root / "agent_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events),
                                                      encoding="utf-8")
            message_path = root / "logs/messages.csv"
            body = {"type": "month_tick", "month_index": 2, "month": 12, "season": "Wet"}
            msg_log.log_message("clock@localhost", "well@localhost", ontology.INFORM, "tick-2",
                                ontology.encode(ontology.INFORM, body), "sent", path=message_path)
            snapshot = load_agent_monitor(root, now=now)
        model = _live_coordination_model(snapshot)
        self.assertEqual(model["run_state"], "live")
        self.assertEqual(model["current_month_index"], 2)
        self.assertEqual(set(model["messages"][0]), set(msg_log.COLUMNS))
        self.assertEqual(model["messages"][0]["thread_id"], "tick-2")
        self.assertEqual(model["messages"][0]["direction"], "sent")
        self.assertEqual(model["hotspots"], ())
        self.assertIn("unavailable", model["hotspots_status"])
        self.assertEqual(model["alerts"], ())
        self.assertIn("unavailable", model["alerts_status"])

    def test_live_coordination_renders_active_banner_timeline_and_message_fields(self) -> None:
        class Box:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def metric(self, *_args, **_kwargs):
                pass

        class Recorder:
            def __init__(self):
                self.calls = []

            def __getattr__(self, name):
                if name in {"subheader", "caption", "success", "warning", "error", "info", "markdown"}:
                    return lambda value: self.calls.append((name, value))
                raise AttributeError(name)

            def dataframe(self, rows, **_kwargs):
                self.calls.append(("dataframe", rows))

            def columns(self, count):
                return [Box() for _ in range(count)]

            def expander(self, _label):
                return Box()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "run"
            (root / "logs").mkdir(parents=True)
            now = datetime.now(timezone.utc)
            (root / "run_manifest.json").write_text(json.dumps({
                "run_id": "render-live", "status": "running", "configuration": {},
            }), encoding="utf-8")
            events = [
                {"event_type": "runner_heartbeat", "timestamp": now.isoformat(), "component": "SimulationRunner"},
                {"event_type": "agent_started", "timestamp": now.isoformat(), "event_seq": 1,
                 "component": "ClockAgent", "identity": "clock@localhost"},
                {"event_type": "agent_heartbeat", "timestamp": now.isoformat(), "event_seq": 2,
                 "component": "ClockAgent", "identity": "clock@localhost"},
                {"event_type": "month_started", "timestamp": now.isoformat(), "component": "SimulationRunner",
                 "month_index": 0},
            ]
            (root / "agent_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events),
                                                      encoding="utf-8")
            body = {"type": "month_tick", "month_index": 0, "month": 10, "season": "Wet"}
            msg_log.log_message("clock@localhost", "well@localhost", ontology.INFORM, "tick-0",
                                ontology.encode(ontology.INFORM, body), "sent", path=root / "logs/messages.csv")
            snapshot = load_agent_monitor(root, now=now)
        recorder = Recorder()
        _render_live_coordination_snapshot(recorder, snapshot)
        self.assertTrue(any(kind == "success" and "Run active" in value for kind, value in recorder.calls))
        tables = [rows for kind, rows in recorder.calls if kind == "dataframe"]
        self.assertTrue(any(rows and "Event" in rows[0] for rows in tables))
        self.assertTrue(any(rows and set(rows[0]) == set(msg_log.COLUMNS) for rows in tables))

    def test_live_coordination_renders_persisted_evaluation_and_gates_allocations(self) -> None:
        class Box:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def metric(self, *_args, **_kwargs):
                pass

        class Recorder:
            def __init__(self):
                self.tables = []

            def __getattr__(self, _name):
                return lambda *_args, **_kwargs: None

            def dataframe(self, rows, **_kwargs):
                self.tables.append(rows)

            def columns(self, count):
                return [Box() for _ in range(count)]

            def expander(self, _label):
                return Box()

        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            logs = run_dir / "logs"
            logs.mkdir()
            self._write_csv(logs / "evaluation_summary.csv",
                            ["metric", "scope", "value", "unit", "status", "observations", "details"],
                            [["allocation_efficiency", "all", "0.75", "ratio", "ok", "3", "{}"],
                             ["risk_classification_alignment", "all", "", "ratio", "unavailable", "0",
                              '{"reason":"reference mapping missing"}']])
            (run_dir / "run_manifest.json").write_text(
                json.dumps({"run_id": "evaluation-display-fixture", "status": "completed"}),
                encoding="utf-8",
            )
            snapshot = load_agent_monitor(run_dir)
            st = Recorder()
            _render_live_coordination_snapshot(st, snapshot)
            evaluation_rows = next(table for table in st.tables
                                   if table and table[0].get("Metric") == "allocation_efficiency")
            self.assertEqual(evaluation_rows[0]["Value"], "Unavailable")
            self.assertEqual(evaluation_rows[0]["Status"], "unavailable")
            self.assertIn("not verified", evaluation_rows[0]["Details"])
            self.assertEqual(evaluation_rows[1]["Value"], "Unavailable")
            self.assertEqual(evaluation_rows[1]["Status"], "unavailable")

    def test_live_coordination_model_marks_completed_run_as_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "run_manifest.json").write_text(json.dumps({
                "run_id": "completed-fixture", "status": "completed",
            }), encoding="utf-8")
            model = _live_coordination_model(load_agent_monitor(root))
        self.assertEqual(model["run_state"], "completed snapshot")
        self.assertEqual(model["run_status"], "completed")

    def test_live_coordination_model_keeps_stale_failed_and_malformed_states_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            stale_dir = base / "stale"
            failed_dir = base / "failed"
            malformed_dir = base / "malformed"
            for root in (stale_dir, failed_dir, malformed_dir):
                (root / "logs").mkdir(parents=True)
            (stale_dir / "run_manifest.json").write_text(
                json.dumps({"run_id": "stale", "status": "running"}), encoding="utf-8")
            (failed_dir / "run_manifest.json").write_text(
                json.dumps({"run_id": "failed", "status": "failed"}), encoding="utf-8")
            (malformed_dir / "run_manifest.json").write_text(
                json.dumps({"run_id": "malformed", "status": "running"}), encoding="utf-8")
            (malformed_dir / "agent_events.jsonl").write_text('{"event_type":\n', encoding="utf-8")
            (malformed_dir / "logs/messages.csv").write_text("timestamp,sender\n", encoding="utf-8")
            stale = _live_coordination_model(load_agent_monitor(stale_dir))
            failed = _live_coordination_model(load_agent_monitor(failed_dir))
            malformed = _live_coordination_model(load_agent_monitor(malformed_dir))
        self.assertEqual(stale["run_state"], "stale")
        self.assertEqual(failed["run_state"], "failed snapshot")
        self.assertIn("Malformed event journal", " ".join(malformed["errors"]))
        self.assertIn("Unexpected message-log columns", " ".join(malformed["errors"]))

    def test_live_coordination_distinguishes_runner_observations_from_agent_behavior(self) -> None:
        self.assertEqual(_coordination_event_source({
            "event_type": "agent_started", "component": "ClockAgent",
        }), "Runner lifecycle/work observation")
        self.assertEqual(_coordination_event_source({
            "event_type": "agent_waiting", "component": "ResourceCoordinatorAgent",
        }), "Agent behavior event journal")
        self.assertEqual(_coordination_event_source({
            "event_type": "alert_outcome", "component": "AlertProcessingAgent",
        }), "Runner observation / persisted stage result")

    def test_live_coordination_without_configured_run_has_clear_state(self) -> None:
        class Recorder:
            def __init__(self):
                self.calls = []

            def subheader(self, value):
                self.calls.append(("subheader", value))

            def info(self, value):
                self.calls.append(("info", value))

        st = Recorder()
        _render_live_coordination(st, None)
        self.assertEqual(st.calls[0], ("subheader", "Live Agent Coordination"))
        self.assertIn("No simulation run is configured", st.calls[1][1])

    def test_live_coordination_run_selector_includes_lab_run_and_configured_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "lab"
            run_dir = root / "runs" / "lab-run"
            control = root / "control" / "lab-run.json"
            control.parent.mkdir(parents=True)
            control.write_text(json.dumps({
                "run_id": "lab-run", "status": "completed", "run_dir": str(run_dir),
                "created_at": "2026-01-01T00:00:00+00:00",
            }), encoding="utf-8")
            configured = Path(temp) / "configured-run"
            options = _live_coordination_run_options(str(configured), root)
        self.assertEqual(len(options), 2)
        self.assertIn("Configured run", options[0][0])
        self.assertTrue(any("Simulation Lab · lab-run · completed" == label and path == run_dir
                            for label, path in options))

    def test_simulation_lab_history_selection_and_status_summary(self) -> None:
        class Selection:
            class Result:
                rows = [0]
            selection = Result()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "lab"
            run_dir = root / "runs" / "history-run"
            run_dir.mkdir(parents=True)
            (root / "control").mkdir()
            (run_dir / "run_manifest.json").write_text(json.dumps({
                "run_id": "history-run", "status": "completed_with_unavailable_stages",
                "started_at": "2026-01-01T00:00:00+00:00",
                "configuration": {"start_month_index": 3, "end_month_index": 4,
                                  "resource": "RO_unit", "resource_pool": 2},
                "provenance": {"classification": "synthetic_simulation"},
            }), encoding="utf-8")
            (root / "control/history-run.json").write_text(json.dumps({
                "run_id": "history-run", "status": "completed_with_unavailable_stages",
                "run_dir": str(run_dir), "created_at": "2026-01-01T00:00:00+00:00",
                "input_source": "built-in sample", "input_record_count": 5,
            }), encoding="utf-8")
            rows = _lab_run_history_rows(root)
        self.assertEqual(_selected_run_id(Selection(), rows), "history-run")
        self.assertEqual(rows[0]["Status"], "completed with unavailable stages")
        self.assertEqual(rows[0]["month_index range"], "3–4")
        self.assertEqual(rows[0]["Input records"], 5)

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
