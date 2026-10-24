"""Simulation Run Details loader/filter tests use isolated, synthetic fixture artifacts."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from src.core import msg_log, ontology
from src.sim.event_journal import append_event, register_run
from src.sim.run_details import (
    agent_execution_details,
    build_execution_view,
    combined_timeline,
    filter_evidence,
    filter_messages,
    load_simulation_run_details,
)
from dashboard.app import _pretty_event_rows, _render_final_decisions, _render_run_detail_page


class SimulationRunDetailsTests(unittest.TestCase):
    def _run(self, root: Path) -> Path:
        lab = root / "lab"
        run_dir = lab / "runs" / "run-fixture"
        (run_dir / "logs").mkdir(parents=True)
        (run_dir / "rounds").mkdir()
        (lab / "control").mkdir()
        manifest = {
            "run_id": "run-fixture", "status": "completed_with_unavailable_stages",
            "started_at": "2026-01-01T00:00:00+00:00", "completed_at": "2026-01-01T00:00:05+00:00",
            "provenance": {"classification": "synthetic_simulation"},
            "configuration": {"start_month_index": 2, "end_month_index": 2},
            "month_status": [{"month_index": 2, "calendar_month": 12,
                              "status": "complete", "contract_net": "confirmed"}],
            "evaluation_status": {"allocation_efficiency": {"status": "unavailable", "value": None}},
        }
        (run_dir / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        event_path = run_dir / "agent_events.jsonl"
        register_run(event_path, "run-fixture")
        append_event(event_path, "agent_started", "RiskScoringAgent", identity="risk@localhost")
        append_event(event_path, "agent_heartbeat", "RiskScoringAgent", identity="risk@localhost",
                     month_index=2)
        append_event(event_path, "agent_phase", "RiskScoringAgent", identity="risk@localhost",
                     phase="scoring", month_index=2)
        append_event(event_path, "risk_stage", "SimulationRunner", month_index=2,
                     status="complete")
        body = {"type": "month_tick", "month_index": 2, "month": 12, "season": "Wet"}
        msg_log.log_message("clock@localhost", "well@localhost", ontology.INFORM, "tick-2",
                            ontology.encode(ontology.INFORM, body), "sent",
                            path=run_dir / "logs/messages.csv")
        round_result = {"month_index": 2, "round": 1, "thread_id": "round-2",
                        "resource": "RO_unit", "resource_pool": 1,
                        "proposals": [{"ds_division": "Sample", "bid": 12.0}],
                        "selected_awardees": [{"ds_division": "Sample", "normalized_bid": 1.0,
                                               "status": "confirmed"}],
                        "winner": "Sample", "award_status": "confirmed"}
        (run_dir / "rounds" / "month_index_0002_round_1.json").write_text(
            json.dumps(round_result), encoding="utf-8")
        (run_dir / "risk_records.json").write_text(json.dumps([{
            "type": "risk_record", "month_index": 2, "ds_division": "Sample",
            "wqi": 110.0, "tier": "Poor", "drivers": ["F_mg_L"], "source": "synthetic",
        }]), encoding="utf-8")
        self._write_csv(run_dir / "logs/evaluation_summary.csv",
                        ["metric", "scope", "value", "unit", "status", "observations", "details"],
                        [["allocation_efficiency", "all", "", "ratio", "unavailable", "0",
                          '{"reason":"baseline not verified"}']])
        control_path = lab / "control" / "run-fixture.json"
        control_path.write_text(json.dumps({"run_id": "run-fixture", "status": "completed",
                                            "run_dir": str(run_dir), "input_source": "synthetic fixture",
                                            "input_record_count": 4}), encoding="utf-8")
        return run_dir

    @staticmethod
    def _write_csv(path: Path, columns: list[str], rows: list[list[str]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)

    def test_selected_run_artifacts_load_in_isolation_with_month_and_round_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            details = load_simulation_run_details(
                run_dir, lab_root=run_dir.parent.parent,
                control_path=run_dir.parent.parent / "control/run-fixture.json",
            )
            other = Path(temp) / "outside" / "run"
            other.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "outside the configured runs"):
                load_simulation_run_details(other, lab_root=run_dir.parent.parent)
        self.assertEqual(details.manifest_status, "available")
        self.assertEqual(details.control["input_record_count"], 4)
        self.assertEqual(details.risk_records.rows[0]["tier"], "Poor")
        self.assertEqual(details.rounds.rows[0]["winner"], "Sample")
        self.assertEqual(details.evaluation.rows[0]["status"], "unavailable")
        self.assertTrue(all(message["evidence_source"] == "Seven-column SPADE message log"
                            for message in details.messages))

    def test_execution_view_reports_live_status_outputs_and_only_observed_message_edges(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            manifest_path = run_dir / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "running"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            event_path = run_dir / "agent_events.jsonl"
            append_event(event_path, "agent_started", "RiskScoringAgent", identity="risk@localhost")
            append_event(event_path, "agent_heartbeat", "RiskScoringAgent", identity="risk@localhost")
            append_event(event_path, "agent_starting", "VillageAgent:Sample", identity="village@localhost")
            (run_dir / "hotspot_screening.json").write_text(json.dumps([{
                "month_index": 2, "ds_division": "Sample", "status": "active",
                "evidence_month_indexes": [1, 2], "reason": "Illustrative configured rule",
            }]), encoding="utf-8")
            (run_dir / "alert_decisions.json").write_text(json.dumps([{
                "month_index": 2, "ds_division": "Sample", "status": "emitted",
                "delivery_status": "confirmed", "reason": "Simulated inbox acknowledged",
            }]), encoding="utf-8")
            well_body = {"type": "well_readings", "month_index": 2, "ds_division": "Sample",
                         "source": "synthetic", "readings": []}
            risk_body = {"type": "risk_record", "month_index": 2, "ds_division": "Sample",
                         "wqi": 110.0, "tier": "Poor", "drivers": ["F_mg_L"]}
            msg_log.log_message("well@localhost", "risk@localhost", ontology.INFORM, "input-2",
                                ontology.encode(ontology.INFORM, well_body), "received",
                                path=run_dir / "logs/messages.csv")
            msg_log.log_message("risk@localhost", "village@localhost", ontology.INFORM, "risk-2",
                                ontology.encode(ontology.INFORM, risk_body), "sent",
                                path=run_dir / "logs/messages.csv")
            details = load_simulation_run_details(run_dir)
            view = build_execution_view(details, run_status="running")

        card, = [row for row in view["agents"] if row["identity"] == "risk@localhost"]
        self.assertEqual(card["status"], "running")
        self.assertIn("well_readings", card["latest_input"])
        self.assertIn("risk_record", card["latest_result"])
        self.assertEqual(card["output_to"], "village@localhost")
        self.assertEqual(view["run_status"], "running")
        self.assertEqual(view["progress"], 1.0)  # One month record is explicitly complete in the fixture.
        self.assertTrue(any(edge["source"] == "RiskScoringAgent"
                            and edge["target"] == "VillageAgent"
                            and edge["message_types"] == ("risk_record",)
                            for edge in view["flow"]["message_edges"]))
        self.assertTrue(view["flow"]["runner_orchestration"])
        self.assertEqual(view["decisions"]["risks"][0]["tier"], "Poor")
        self.assertEqual(view["decisions"]["allocations"][0]["confirmed_winner"], True)
        self.assertEqual(view["decisions"]["hotspots"][0]["history"], [1, 2])
        self.assertEqual(view["decisions"]["hotspots"][0]["status"], "active")
        self.assertEqual(view["decisions"]["alerts"][0]["delivery"], "confirmed")
        allocation_metric, = [item for item in view["decisions"]["unavailable_metrics"]
                              if item["metric"] == "allocation_efficiency"]
        self.assertIn("baseline is not verified", allocation_metric["details"])
        self.assertNotIn("{\"reason\"", allocation_metric["details"])
        village_card, = [row for row in view["agents"] if row["identity"] == "village@localhost"]
        self.assertEqual(village_card["latest_input"], "Waiting for input.")
        self.assertEqual(village_card["output_to"], "No output recipient/destination recorded.")

    def test_agent_event_grouping_separates_explicit_events_from_runner_summaries(self):
        with tempfile.TemporaryDirectory() as temp:
            details = load_simulation_run_details(self._run(Path(temp)))
        agent, = agent_execution_details(details)
        self.assertEqual(agent["component"], "RiskScoringAgent")
        self.assertEqual(agent["latest_phase"], "scoring")
        timeline = combined_timeline(details)
        self.assertEqual({row["evidence_source"] for row in timeline},
                         {"Agent event journal", "Seven-column SPADE message log"})
        runner_evidence = [row for row in filter_evidence(timeline, agent="SimulationRunner")]
        self.assertTrue(runner_evidence)
        self.assertEqual(runner_evidence[0]["event_type"], "risk_stage")

    def test_message_filters_keep_rows_and_payload_direction_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            path = run_dir / "logs/messages.csv"
            body = {"type": "month_tick", "month_index": 2, "month": 12, "season": "Wet"}
            payload = ontology.encode(ontology.INFORM, body)
            msg_log.log_message("clock@localhost", "monitor@localhost", ontology.INFORM, "tick-2",
                                payload, "sent", path=path)
            details = load_simulation_run_details(run_dir)
        self.assertEqual(len(details.messages), 2)
        selected = filter_messages(details.messages, receiver="monitor@localhost", direction="sent")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["decoded_payload"], body)
        self.assertEqual(selected[0]["thread_id"], "tick-2")
        self.assertEqual(selected[0]["payload_status"], "valid")

    def test_reloading_selected_run_observes_new_hotspot_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            first = load_simulation_run_details(run_dir)
            self.assertFalse(any(event.get("event_type") == "hotspot_result" for event in first.events))

            append_event(run_dir / "agent_events.jsonl", "hotspot_result", "HotspotAgent",
                         identity="hotspot@localhost", month_index=2, ds_division="Sample",
                         screening_status="unavailable", reason="Illustrative missing history")
            refreshed = load_simulation_run_details(run_dir)
            event, = [row for row in combined_timeline(refreshed)
                      if row.get("event_type") == "hotspot_result"]

        self.assertEqual(event["month_index"], 2)
        self.assertEqual(event["ds_division"], "Sample")
        self.assertEqual(event["screening_status"], "unavailable")
        self.assertEqual(event["evidence_source"], "Agent event journal")
        display_row, = _pretty_event_rows([event])
        self.assertEqual(display_row["ds_division"], "Sample")
        self.assertEqual(display_row["screening_status"], "unavailable")

    def test_unavailable_stage_reasons_trace_unconfigured_rule_to_alert_withholding(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            manifest_path = run_dir / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["stage_status"] = {"hotspot_screening": "unavailable_rule_not_configured"}
            manifest["configuration"]["hotspot_rule"] = None
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (run_dir / "hotspot_screening.json").write_text(json.dumps([{
                "month_index": 2, "ds_division": "Sample", "status": "unavailable",
                "evidence_month_indexes": [], "reason": "screening_rule_not_configured",
            }]), encoding="utf-8")
            (run_dir / "alert_decisions.json").write_text(json.dumps([{
                "month_index": 2, "ds_division": "Sample", "status": "unavailable",
                "hotspot_status": "unavailable", "delivery_status": "not_requested",
                "reason": "hotspot_status_unavailable",
            }]), encoding="utf-8")
            details = load_simulation_run_details(run_dir)
            decisions = build_execution_view(details)["decisions"]

        explanations = decisions["unavailable_explanations"]
        hotspot, = [row for row in explanations if row["stage"] == "Hotspot screening"]
        alert, = [row for row in explanations if row["stage"] == "Alert processing"]
        self.assertIn("No provisional tier-persistence rule was configured", hotspot["reason"])
        self.assertEqual(hotspot["month_index"], "2")
        self.assertEqual(hotspot["divisions"], ("Sample",))
        self.assertFalse(hotspot["expected"])
        self.assertIn("no provisional rule was configured", alert["reason"])
        self.assertFalse(alert["expected"])

    def test_configured_persistence_warmup_is_reported_as_expected_not_as_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            manifest_path = run_dir / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["configuration"]["start_month_index"] = 0
            manifest["configuration"]["end_month_index"] = 1
            manifest["configuration"]["hotspot_rule"] = {
                "rule_id": "illustrative", "trigger_tiers": ["Poor"], "consecutive_months": 2,
            }
            manifest["stage_status"] = {"hotspot_screening": "screening_outputs_recorded"}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (run_dir / "hotspot_screening.json").write_text(json.dumps([
                {"month_index": 0, "ds_division": "Sample", "status": "unavailable",
                 "evidence_month_indexes": [], "reason": "insufficient_consecutive_month_history"},
                {"month_index": 1, "ds_division": "Sample", "status": "active",
                 "evidence_month_indexes": [0, 1], "reason": None},
            ]), encoding="utf-8")
            (run_dir / "alert_decisions.json").write_text(json.dumps([{
                "month_index": 0, "ds_division": "Sample", "status": "unavailable",
                "hotspot_status": "unavailable", "delivery_status": "not_requested",
                "reason": "hotspot_status_unavailable",
            }]), encoding="utf-8")
            decisions = build_execution_view(load_simulation_run_details(run_dir))["decisions"]

        hotspot, = [row for row in decisions["unavailable_explanations"]
                    if row["stage"] == "Hotspot screening"]
        alert, = [row for row in decisions["unavailable_explanations"]
                  if row["stage"] == "Alert processing"]
        self.assertTrue(hotspot["expected"])
        self.assertTrue(alert["expected"])
        self.assertIn("warm-up", alert["reason"])

    def test_unavailable_stage_summary_renders_short_cause_and_affected_month(self):
        class Box:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Recorder:
            def __init__(self):
                self.text = []

            def __getattr__(self, name):
                if name in {"columns"}:
                    return lambda count: [Box() for _ in range(count)]
                return lambda value, **_kwargs: self.text.append((name, str(value)))

        recorder = Recorder()
        _render_final_decisions(recorder, {
            "risks": (), "risk_status": "available", "hotspots": (), "hotspot_status": "available",
            "allocations": (), "allocation_status": "available", "alerts": (), "alert_status": "available",
            "failures": (), "unavailable_stages": (), "unavailable_explanations": ({
                "stage": "Hotspot screening", "month_index": "0", "divisions": ("Sample",),
                "expected": True, "reason": "Insufficient prior history.",
            },), "evaluation_status": "available", "unavailable_metrics": (),
        }, "completed_with_unavailable_stages")
        rendered = " ".join(value for _name, value in recorder.text)
        self.assertIn("month_index 0", rendered)
        self.assertIn("expected warm-up", rendered)
        self.assertIn("Insufficient prior history", rendered)

    def test_missing_and_partially_written_artifacts_remain_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            (run_dir / "risk_records.json").write_text('[{"type":', encoding="utf-8")
            (run_dir / "agent_events.jsonl").write_text('{"event_type":\n', encoding="utf-8")
            (run_dir / "rounds" / "broken.json").write_text('{', encoding="utf-8")
            details = load_simulation_run_details(run_dir)
            missing = load_simulation_run_details(Path(temp) / "absent")
        self.assertEqual(details.risk_records.status, "incomplete")
        self.assertEqual(details.event_status, "incomplete")
        self.assertEqual(details.rounds.status, "incomplete")
        self.assertEqual(details.evaluation.rows[0]["status"], "unavailable")
        self.assertEqual(missing.manifest_status, "unavailable")
        self.assertEqual(missing.message_status, "unavailable")

    def test_details_page_renders_agent_month_round_and_evaluation_evidence(self):
        class Box:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def metric(self, *_args, **_kwargs):
                pass

            def container(self, **_kwargs):
                return self

            def selectbox(self, _label, options, **_kwargs):
                return options[0]

            def __getattr__(self, _name):
                return lambda *_args, **_kwargs: None

        class Recorder:
            def __init__(self):
                self.tables = []
                self.json_values = []
                self.text = []
                self.session_state = {}

            def __getattr__(self, name):
                if name == "button":
                    return lambda *_args, **_kwargs: False
                if name == "text_input":
                    return lambda *_args, **_kwargs: ""
                if name == "columns":
                    return lambda count: [Box() for _ in range(count if isinstance(count, int) else len(count))]
                if name in {"selectbox", "number_input"}:
                    return lambda _label, options, **_kwargs: options[0] if isinstance(options, (list, tuple, range)) else 1
                if name in {"expander"}:
                    return lambda *_args, **_kwargs: Box()
                return lambda *args, **_kwargs: self.text.extend(str(arg) for arg in args)

            def dataframe(self, rows, **_kwargs):
                self.tables.append(rows)

            def json(self, value, **_kwargs):
                self.json_values.append(value)

        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            lab_root = run_dir.parent.parent
            control_path = lab_root / "control/run-fixture.json"
            details = load_simulation_run_details(run_dir, lab_root=lab_root,
                                                  control_path=control_path)
            recorder = Recorder()
            _render_run_detail_page(recorder, details, lab_root, control_path)
        self.assertTrue(any(any(row.get("tier") == "Poor" for row in table if isinstance(row, dict))
                            for table in recorder.tables))
        self.assertTrue(any(isinstance(value, list) and value
                            and value[0].get("ds_division") == "Sample"
                            and "normalized_bid" in value[0]
                            for value in recorder.json_values))
        self.assertTrue(any("unavailable" in str(row).casefold()
                            for table in recorder.tables for row in table if isinstance(row, dict)))


if __name__ == "__main__":
    unittest.main()
