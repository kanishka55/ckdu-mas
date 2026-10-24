"""Configuration and opt-in embedded-SPADE tests for the multi-month runner."""
from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path

from src.agents.clock import ClockAgent
from src.agents import common
from src.core import msg_log, ontology
from src.core.hotspot_screening import ScreeningRule
from src.sim.runner import (
    SimulationConfig,
    _has_unavailable_stages,
    run_simulation,
    validate_output_dir,
)
from src.sim.agent_monitor import load_agent_monitor
from src.sim.agent_monitor import read_event_journal


class SimulationRunnerConfigurationTests(unittest.TestCase):
    def test_success_status_is_based_on_recorded_stage_and_metric_evidence(self):
        complete = {
            "stage_status": {"risk_scoring": "complete", "hotspot_screening": "screening_outputs_recorded"},
            "months": [{"status": "complete", "hotspot_screening": "complete",
                        "alert_decisions": "complete", "alert_delivery": {"unavailable": 0}}],
            "evaluation_status": {"example_metric": {"status": "ok", "value": 1}},
            "failures": [],
        }
        incomplete = {
            **complete,
            "months": [{"status": "complete", "hotspot_screening": "partially_unavailable",
                        "alert_delivery": {"unavailable": 1}}],
        }
        self.assertFalse(_has_unavailable_stages(
            complete["stage_status"], complete["months"],
            complete["evaluation_status"], complete["failures"],
        ))
        self.assertTrue(_has_unavailable_stages(
            incomplete["stage_status"], incomplete["months"],
            incomplete["evaluation_status"], {"failures": incomplete["failures"]},
        ))
        metric_unavailable = {"metric": {"status": "unavailable", "value": None}}
        self.assertTrue(_has_unavailable_stages(complete["stage_status"], complete["months"],
                                                metric_unavailable, []))
        self.assertTrue(_has_unavailable_stages(complete["stage_status"], complete["months"],
                                                complete["evaluation_status"],
                                                {"failures": [{"stage": "output", "error": "write failed"}]}))

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


class BehaviorFailureJournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_uncaught_behavior_failure_is_recorded_then_reraised(self):
        with tempfile.TemporaryDirectory() as temp:
            class FakeAgent:
                jid = "agent@localhost"
                event_journal_path = Path(temp) / "events.jsonl"
                run_id = "failure-fixture"
                monitor_component = "IllustrativeAgent"
                monitor_behavior_failures = []

                def add_behaviour(self, behavior, template=None):
                    self.behavior = behavior

            class FailingBehavior:
                async def run(self):
                    raise ValueError("illustrative behavior failure")

            agent = FakeAgent()
            common.add_observed_behaviour(agent, FailingBehavior())
            with self.assertRaisesRegex(ValueError, "illustrative behavior failure"):
                await agent.behavior.run()
            events, status, errors = read_event_journal(agent.event_journal_path)
            self.assertEqual(status, "available")
            self.assertEqual(errors, ())
            self.assertEqual(events[0]["event_type"], "agent_behavior_failed")
            self.assertEqual(events[0]["run_id"], "failure-fixture")
            self.assertEqual(len(agent.monitor_behavior_failures), 1)


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
            event_rows = [json.loads(line) for line in
                          (output / "agent_events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(row.get("run_id") == "test-run" for row in event_rows))
            self.assertTrue(all(isinstance(row.get("event_seq"), int) for row in event_rows))
            started_identities = {row.get("identity") for row in event_rows
                                  if row.get("event_type") == "agent_started"}
            heartbeat_identities = {row.get("identity") for row in event_rows
                                    if row.get("event_type") == "agent_heartbeat"}
            self.assertTrue(started_identities)
            self.assertTrue(started_identities.issubset(heartbeat_identities))
            self.assertTrue(any(row.get("event_type") == "agent_work_completed"
                                for row in event_rows))
            self.assertFalse(any(row.get("event_type") == "agent_behavior_failed"
                                 for row in event_rows))
            self.assertTrue(all(left["event_seq"] < right["event_seq"]
                                for left, right in zip(event_rows, event_rows[1:])))
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
            confirmed_rounds = 0
            for index in (0, 1):
                round_result = json.loads(
                    (output / "rounds" / f"month_index_{index:04d}_round_1.json")
                    .read_text(encoding="utf-8")
                )
                self.assertEqual(round_result["month_index"], index)
                if (round_result["award_status"] == "confirmed"
                        and round_result["allocation_successful"]
                        and round_result["confirmed_awardees"]):
                    confirmed_rounds += 1
            self.assertGreater(confirmed_rounds, 0,
                               "successful integration scenario must confirm a resource award")

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

            emitted = [item for item in alert_decisions if item["status"] == "emitted"]
            self.assertTrue(emitted, "successful scenario should emit at least one alert")
            self.assertTrue(all(item["delivery_status"] == "confirmed"
                                and item.get("alert_thread_id") for item in emitted))

            def typed_rows(message_type, direction):
                matches = []
                for row in rows:
                    if row["performative"] != ontology.INFORM or row["direction"] != direction:
                        continue
                    body = ontology.decode(row["performative"], row["payload"])
                    if body["type"] == message_type:
                        matches.append((row, body))
                return matches

            emitted_keys = [
                (item["alert_thread_id"], item["ds_division"], item["month_index"])
                for item in emitted
            ]
            self.assertEqual(len(emitted_keys), len(set(emitted_keys)),
                             "each emitted alert must have a distinct correlation key")
            sent_alerts = typed_rows("alert", "sent")
            self.assertCountEqual(
                [(row["thread_id"], body["ds_division"], body["month_index"])
                 for row, body in sent_alerts],
                emitted_keys,
            )
            sent_acknowledgements = typed_rows("alert_ack", "sent")
            received_acknowledgements = typed_rows("alert_ack", "received")
            self.assertCountEqual(
                [(row["thread_id"], body["ds_division"], body["month_index"])
                 for row, body in sent_acknowledgements],
                emitted_keys,
            )
            self.assertCountEqual(
                [(row["thread_id"], body["ds_division"], body["month_index"])
                 for row, body in received_acknowledgements],
                emitted_keys,
            )
            alert_endpoints = {
                (row["thread_id"], row["sender"], row["receiver"])
                for row, _body in sent_alerts
            }
            self.assertTrue(alert_endpoints)
            self.assertTrue(all(
                any(row["thread_id"] == thread and row["sender"] == alert_receiver
                    and row["receiver"] == alert_sender
                    for row, _body in sent_acknowledgements)
                for thread, alert_sender, alert_receiver in alert_endpoints
            ), "simulated inbox acknowledgements must return to the alert processor")
            self.assertTrue(all(
                any(row["thread_id"] == thread and row["sender"] == alert_receiver
                    and row["receiver"] == alert_sender
                    for row, _body in received_acknowledgements)
                for thread, alert_sender, alert_receiver in alert_endpoints
            ), "the alert processor must receive the acknowledgement from the simulated inbox")

            manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["provenance"]["not_new_monthly_measurements"])
            self.assertEqual(manifest["configuration"]["hotspot_rule"], {
                "rule_id": "illustrative-persistent-risk-tier-v1",
                "trigger_tiers": ["Poor", "Unsuitable", "Very Poor"],
                "consecutive_months": 2,
            })
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
