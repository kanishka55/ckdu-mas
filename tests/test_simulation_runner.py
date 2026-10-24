"""Configuration and opt-in embedded-SPADE tests for the multi-month runner."""
from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path

from src.agents.clock import ClockAgent
from src.core import msg_log, ontology
from src.core.hotspot_screening import ScreeningRule
from src.sim.runner import SimulationConfig, run_simulation, validate_output_dir
from src.sim.agent_monitor import load_agent_monitor


class SimulationRunnerConfigurationTests(unittest.TestCase):
    def test_clock_tick_uses_explicit_calendar_month_and_shared_schema(self):
        body = ClockAgent.tick_body(17, 10)
        self.assertEqual(body, {"type": "month_tick", "month_index": 17,
                                "month": 10, "season": "Wet"})
        ontology.validate(ontology.INFORM, body)

    def test_tick_rejects_invalid_indices_and_calendar_labels(self):
        for month_index, month in ((True, 1), (-1, 1), (0, 13), (0, True)):
            with self.subTest(month_index=month_index, month=month):
                with self.assertRaises(ValueError):
                    ClockAgent.tick_body(month_index, month)

    def test_config_requires_an_explicit_calendar_label_for_each_simulated_month(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "explicitly map"):
                SimulationConfig(2, 3, {2: 11}, Path(temp) / "run")

    def test_output_guard_rejects_production_and_existing_result_paths(self):
        with self.assertRaisesRegex(ValueError, "protected path"):
            validate_output_dir(Path(__file__).resolve().parents[1] / "logs")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "nonempty"
            root.mkdir()
            (root / "keep.txt").write_text("preserve", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                validate_output_dir(root)
            self.assertEqual((root / "keep.txt").read_text(encoding="utf-8"), "preserve")


@unittest.skipUnless(os.environ.get("CKDU_RUN_SPADE_SIMULATION") == "1",
                     "set CKDU_RUN_SPADE_SIMULATION=1 to run embedded multi-month SPADE integration")
class EmbeddedMultiMonthSimulationTests(unittest.TestCase):
    def test_two_months_complete_risk_delivery_and_bounded_rounds_in_isolated_output(self):
        import spade

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "simulation"
            config = SimulationConfig(
                start_month_index=0,
                end_month_index=1,
                calendar_month_by_index={0: 10, 1: 11},
                output_dir=output,
                resource="RO_unit",
                resource_pool=1,
                embedded_server=True,
                proposal_timeout_s=2.0,
                confirmation_timeout_s=2.0,
                stage_timeout_s=30.0,
                run_id="test-run",
                # Explicitly configured project-defined screening parameters for this
                # illustrative integration run; they are not scientific thresholds.
                hotspot_rule=ScreeningRule(
                    "illustrative-persistent-risk-tier-v1",
                    frozenset({"Poor", "Very Poor", "Unsuitable"}),
                    2,
                ),
            )
            result_holder = {}

            async def scenario():
                result_holder["manifest"] = await run_simulation(config)

            spade.run(scenario(), embedded_xmpp_server=True)
            result = result_holder["manifest"]

            self.assertEqual(result["status"], "completed_with_unavailable_stages")
            self.assertEqual([month["month_index"] for month in result["month_status"]], [0, 1])
            self.assertTrue(all(month["risk_delivery"] == "complete" for month in result["month_status"]))
            self.assertTrue(all(month["contract_net"] != "pending" for month in result["month_status"]))
            self.assertEqual(result["stage_status"]["hotspot_agent"], "processed_risk_records")
            self.assertEqual(result["stage_status"]["alert_transport"],
                             "local_simulated_inbox_not_real_health_officer")
            self.assertEqual(result["stage_status"]["agent_monitor"],
                             "event_journal_and_message_log_available")
            self.assertTrue((output / "logs/messages.csv").is_file())
            self.assertTrue((output / "logs/risk_scores.csv").is_file())
            self.assertTrue((output / "logs/evaluation_summary.csv").is_file())
            self.assertTrue((output / "agent_events.jsonl").is_file())
            self.assertEqual(len(list((output / "rounds").glob("*.json"))), 2)
            persisted_risks = json.loads((output / "risk_records.json").read_text(encoding="utf-8"))
            self.assertEqual(len(persisted_risks), len(result["divisions"]) * 2)
            hotspot_results = json.loads(
                (output / "hotspot_screening.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(hotspot_results), len(result["divisions"]) * 2)
            self.assertTrue(all(row["component"] == "SPADE HotspotAgent"
                                and row["risk_record_received"] for row in hotspot_results))
            available_screens = [row for row in hotspot_results
                                 if row["status"] in {"active", "inactive"}]
            self.assertTrue(available_screens, "configured rule should screen complete two-month histories")
            self.assertTrue(all(row["rule_id"] == "illustrative-persistent-risk-tier-v1"
                                for row in available_screens))
            self.assertTrue(all(row["evidence_month_indexes"] == [0, 1]
                                for row in available_screens))
            self.assertTrue(all(row["provenance"] == "project_defined_screening_not_validated_model"
                                for row in hotspot_results))
            month_zero_screens = [row for row in hotspot_results if row["month_index"] == 0]
            month_one_screens = [row for row in hotspot_results if row["month_index"] == 1]
            self.assertTrue(all(row["status"] == "unavailable"
                                and row["reason"] == "insufficient_consecutive_month_history"
                                for row in month_zero_screens))
            self.assertEqual(len(month_one_screens), len(result["divisions"]))
            self.assertTrue(all(row["status"] in {"active", "inactive"}
                                for row in month_one_screens))
            self.assertGreater(result["message_counts"]["logged_rows_including_sent_received_copies"], 0)
            alert_decisions = json.loads(
                (output / "alert_decisions.json").read_text(encoding="utf-8")
            )
            self.assertTrue(alert_decisions)
            self.assertTrue(any(item["status"] in {"emitted", "suppressed"}
                                for item in alert_decisions))
            self.assertTrue(all(
                item["status"] == "unavailable" or item["hotspot"] in {True, False}
                for item in alert_decisions
            ))
            self.assertTrue(all(item["provenance"] == "synthetic_spade_simulation_local_inbox"
                                for item in alert_decisions))
            self.assertTrue(all(item["delivery_status"] in {
                "not_requested", "sent", "confirmed", "failed"
            } for item in alert_decisions))
            month_zero_alerts = [item for item in alert_decisions if item["month_index"] == 0]
            month_one_alerts = [item for item in alert_decisions if item["month_index"] == 1]
            self.assertTrue(all(item["status"] == "unavailable"
                                and item["reason"] == "hotspot_status_unavailable"
                                and item["delivery_status"] == "not_requested"
                                for item in month_zero_alerts))
            self.assertTrue(any(item["status"] == "emitted"
                                and item["delivery_status"] == "confirmed"
                                for item in month_one_alerts))
            for index in (0, 1):
                round_result = json.loads(
                    (output / "rounds" / f"month_index_{index:04d}_round_1.json")
                    .read_text(encoding="utf-8")
                )
                self.assertEqual(round_result["month_index"], index)
                self.assertIn(round_result["award_status"], {
                    "confirmed", "partially_confirmed", "unconfirmed", "failed",
                    "send_failed", "no_award", "protocol_error",
                })

            with (output / "logs/messages.csv").open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, msg_log.COLUMNS)
                rows = list(reader)
            self.assertEqual({row["direction"] for row in rows}, {"sent", "received"})
            decoded = [ontology.decode(row["performative"], row["payload"])
                       for row in rows if row["performative"] == ontology.INFORM]
            risk_bodies = [body for body in decoded if body["type"] == "risk_record"]
            self.assertTrue(risk_bodies)
            self.assertTrue(all(body["month_index"] in {0, 1} for body in risk_bodies))
            self.assertTrue(any(row["performative"] == ontology.INFORM
                                and row["direction"] == "sent"
                                and json.loads(row["payload"]).get("type") == "alert"
                                for row in rows))
            self.assertTrue(any(row["performative"] == ontology.INFORM
                                and row["direction"] == "sent"
                                and json.loads(row["payload"]).get("type") == "hotspot_result"
                                for row in rows))

            manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["provenance"]["not_new_monthly_measurements"])
            self.assertEqual(manifest["evaluation_status"]["allocation_efficiency"]["status"], "unavailable")
            self.assertNotIn("password", manifest["configuration"])
            self.assertTrue(all(event["event"] in {"started", "stopped"}
                                for event in manifest["agent_lifecycle"]))
            monitor = load_agent_monitor(output)
            self.assertEqual(monitor.freshness, "completed snapshot")
            self.assertTrue(monitor.agents)
            self.assertTrue(all(agent["Status"] == "completed" for agent in monitor.agents),
                            {"agents": monitor.agents,
                             "coordinator_events": [event for event in monitor.lifecycle_events
                                                    if "Coordinator" in str(event.get("component"))]})
            self.assertEqual(len(monitor.months), 2)
            self.assertTrue(monitor.message_counts)
            self.assertTrue(monitor.hotspots)
            self.assertTrue(monitor.alerts)
            self.assertEqual(len(monitor.rounds), 2)
            self.assertTrue(any(row.get("status") in {"active", "inactive"}
                                for row in monitor.hotspots))
            self.assertTrue(any(row.get("status") in {"emitted", "suppressed"}
                                for row in monitor.alerts))


if __name__ == "__main__":
    unittest.main()
