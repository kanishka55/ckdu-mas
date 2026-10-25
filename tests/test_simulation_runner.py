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
    _has_complete_negotiation_history,
    _has_operational_gaps,
    _write_confirmed_allocation_rows,
    run_simulation,
    validate_output_dir,
)
from src.sim.agent_monitor import load_agent_monitor
from src.sim.agent_monitor import read_event_journal


class SimulationRunnerConfigurationTests(unittest.TestCase):
    def test_allocation_export_contains_only_confirmed_awards_in_evaluator_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "rounds").mkdir()
            (root / "rounds/month_0.json").write_text(json.dumps({
                "month_index": 0, "round": 2, "resource": "RO_unit",
                "selected_awardees": [
                    {"ds_division": "Alpha", "bid": 0.8, "status": "confirmed"},
                    {"ds_division": "Beta", "bid": 0.4, "status": "failed"},
                ],
            }), encoding="utf-8")
            path = _write_confirmed_allocation_rows(
                root, ["rounds/month_0.json"], {0: 10},
            )
            with path.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, [
                    "month", "resource", "ds_division", "round", "winning_bid", "method",
                ])
                rows = list(reader)
            self.assertEqual(rows, [{
                "month": "10", "resource": "RO_unit", "ds_division": "Alpha",
                "round": "2", "winning_bid": "0.8", "method": "contract_net",
            }])

    def test_negotiation_history_is_attested_only_for_complete_round_artifacts(self):
        complete = {
            "month_index": 0, "round": 1, "thread_id": "round-0",
            "state_history": ["IDLE", "CFP_SENT", "COLLECTING", "AWARDING", "CONFIRMING", "DONE"],
            "proposals": [{"sender": "a@localhost"}],
            "refusals": [{"sender": "b@localhost"}],
            "ineligible_proposals": [], "missing_recipients": [],
            "timed_out_recipients": [], "malformed_replies": [], "unmatched_replies": [],
            "protocol_errors": [], "confirmation_timeouts": [],
            "selected_awardees": [{"status": "confirmed"}],
        }
        months = [{"month_index": 0, "risk_delivery": "complete"}]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result_path = root / "rounds/round-0.json"
            result_path.parent.mkdir()
            result_path.write_text(json.dumps(complete), encoding="utf-8")
            self.assertTrue(_has_complete_negotiation_history(
                root, ["rounds/round-0.json"], months, recipient_count=2, round_number=1,
            ))

            for field, value in (
                ("timed_out_recipients", ["b@localhost"]),
                ("malformed_replies", [{"sender": "b@localhost"}]),
                ("unmatched_replies", [{"sender": "b@localhost"}]),
                ("protocol_errors", ["protocol issue"]),
                ("confirmation_timeouts", ["a@localhost"]),
                ("selected_awardees", [{"status": "unconfirmed_timeout"}]),
            ):
                with self.subTest(field=field):
                    incomplete = {**complete, field: value}
                    result_path.write_text(json.dumps(incomplete), encoding="utf-8")
                    self.assertFalse(_has_complete_negotiation_history(
                        root, ["rounds/round-0.json"], months,
                        recipient_count=2, round_number=1,
                    ))

            result_path.write_text(json.dumps(complete), encoding="utf-8")
            self.assertFalse(_has_complete_negotiation_history(
                root, [], months, recipient_count=2, round_number=1,
            ))
            self.assertFalse(_has_complete_negotiation_history(
                root, ["rounds/round-0.json"], months, recipient_count=3, round_number=1,
            ))

    def test_operational_completion_ignores_expected_warmup_and_optional_metrics(self):
        stage_status = {
            "risk_scoring": "complete",
            "hotspot_screening": "screening_outputs_recorded",
            "hotspot_agent": "processed_risk_records",
            "alert_decisions": "processed_typed_risk_and_hotspot_messages",
            "contract_net": "bounded_rounds_per_completed_month",
        }
        months = [
            {"status": "complete", "risk_delivery": "complete",
             "hotspot_screening": "expected_warmup", "alert_decisions": "expected_warmup",
             "alert_delivery": {"unavailable": 11, "failed": 0}, "contract_net": "confirmed"},
            {"status": "complete", "risk_delivery": "complete",
             "hotspot_screening": "complete", "alert_decisions": "complete",
             "alert_delivery": {"unavailable": 0, "failed": 0}, "contract_net": "confirmed"},
        ]
        self.assertFalse(_has_operational_gaps(stage_status, months, []))

    def test_operational_gaps_cover_unexpected_failures_and_missing_outputs(self):
        complete_stages = {
            "risk_scoring": "complete",
            "hotspot_screening": "screening_outputs_recorded",
            "hotspot_agent": "processed_risk_records",
            "alert_decisions": "processed_typed_risk_and_hotspot_messages",
            "contract_net": "bounded_rounds_per_completed_month",
        }
        complete_month = {"status": "complete", "risk_delivery": "complete",
                          "hotspot_screening": "complete", "alert_decisions": "complete",
                          "alert_delivery": {"failed": 0}, "contract_net": "confirmed"}
        self.assertFalse(_has_operational_gaps(complete_stages, [complete_month], []))
        cases = (
            ("unexpected hotspot failure", complete_stages,
             [{**complete_month, "hotspot_screening": "partially_unavailable"}], []),
            ("missing risk records", complete_stages,
             [{**complete_month, "status": "incomplete", "risk_delivery": "unavailable"}], []),
            ("failed Contract Net", complete_stages,
             [{**complete_month, "contract_net": "failed"}], []),
            ("alert delivery failure", complete_stages,
             [{**complete_month, "alert_delivery": {"failed": 1}}], []),
            ("recorded execution failure", complete_stages, [complete_month],
             [{"stage": "agent", "error": "failed"}]),
        )
        for name, stages, months, failures in cases:
            with self.subTest(name=name):
                self.assertTrue(_has_operational_gaps(stages, months, failures))

    def test_fully_successful_run_with_complete_evaluation_is_operationally_complete(self):
        stage_status = {
            "risk_scoring": "complete",
            "hotspot_screening": "screening_outputs_recorded",
            "hotspot_agent": "processed_risk_records",
            "alert_decisions": "processed_typed_risk_and_hotspot_messages",
            "contract_net": "bounded_rounds_per_completed_month",
        }
        month_status = [{
            "status": "complete", "risk_delivery": "complete",
            "hotspot_screening": "complete", "alert_decisions": "complete",
            "alert_delivery": {"failed": 0}, "contract_net": "confirmed",
        }]
        evaluation_status = {
            "risk_classification_alignment": {"status": "ok", "value": 1.0},
            "allocation_efficiency": {"status": "ok", "value": 1.0},
        }

        self.assertTrue(all(metric["status"] == "ok" for metric in evaluation_status.values()))
        self.assertFalse(_has_operational_gaps(stage_status, month_status, []))

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

            self.assertEqual(result["status"], "completed")
            self.assertEqual([month["month_index"] for month in result["month_status"]], [0, 1])
            self.assertTrue(all(month["risk_delivery"] == "complete" for month in result["month_status"]))
            self.assertTrue(all(month["contract_net"] != "pending" for month in result["month_status"]))
            self.assertEqual(result["month_status"][0]["hotspot_screening"], "expected_warmup")
            self.assertEqual(result["month_status"][0]["alert_decisions"], "expected_warmup")
            # Baselines and the need-gated WQI risk weight are supplied by the runner, so the
            # allocation comparison is available (run totals are the last rows per metric).
            self.assertNotIn("allocation_efficiency", result["evaluation_status"])
            for metric in ("risk_weighted_population_served", "contract_net_minus_equal_split",
                           "contract_net_minus_first_come"):
                self.assertEqual(result["evaluation_status"][metric]["status"], "ok")
            self.assertEqual(result["allocation_evidence"]["baselines_supplied"],
                             ["equal_split", "first_come"])
            self.assertEqual(result["evaluation_status"]["risk_classification_alignment"]["status"],
                             "unavailable")
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
            self.assertEqual(manifest["evaluation_status"]["risk_weighted_population_served"]["status"], "ok")
            allocation_rows_path = output / "logs/allocations.csv"
            self.assertTrue(allocation_rows_path.is_file())
            with allocation_rows_path.open(newline="", encoding="utf-8") as stream:
                allocation_reader = csv.DictReader(stream)
                allocation_rows = list(allocation_reader)
            self.assertEqual(allocation_reader.fieldnames,
                             ["month", "resource", "ds_division", "round", "winning_bid", "method"])
            self.assertEqual({row["method"] for row in allocation_rows},
                             {"contract_net", "equal_split", "first_come"})
            self.assertEqual(manifest["allocation_evidence"]["baselines_supplied"],
                             ["equal_split", "first_come"])
            self.assertTrue(manifest["allocation_evidence"]["team_output_attestation"])
            self.assertIn("not real-world", manifest["allocation_evidence"]["attestation_scope"])
            self.assertIn("need-gated", manifest["allocation_evidence"]["risk_weight_definition"])
            self.assertTrue(manifest["allocation_evidence"]["population_source"])
            self.assertEqual(manifest["evaluation_status"]["contract_net_minus_equal_split"]["status"], "ok")
            self.assertEqual(manifest["evaluation_status"]["alert_latency"]["status"], "ok")
            with (output / "logs/evaluation_summary.csv").open(newline="", encoding="utf-8") as stream:
                evaluation_rows = list(csv.DictReader(stream))
            latency_rows = [row for row in evaluation_rows if row["metric"] == "alert_latency"]
            self.assertEqual(len(latency_rows), 1)
            self.assertEqual(latency_rows[0]["status"], "ok")
            self.assertEqual(latency_rows[0]["observations"], str(len(result["divisions"])))
            self.assertGreater(float(latency_rows[0]["value"]), 0.0)
            negotiation_rounds = [row for row in evaluation_rows
                                  if row["metric"] == "negotiation_rounds"]
            negotiation_messages = [row for row in evaluation_rows
                                    if row["metric"] == "negotiation_logged_event_rows"]
            self.assertEqual([(row["scope"], row["status"], row["value"])
                              for row in negotiation_rounds],
                             [("month_index=0", "ok", "1"), ("month_index=1", "ok", "1")])
            # 11 cfp + 11 replies + accept/reject per proposer + 1 inform_done. Month 0: 4 proposers
            # (1 accept, 3 reject) = 27. Month 1: month 0's winner already holds the RO unit and
            # refuses, so 3 proposers (1 accept, 2 reject) = 26.
            self.assertEqual([(row["scope"], row["status"], row["value"])
                              for row in negotiation_messages],
                             [("month_index=0", "ok", "27"), ("month_index=1", "ok", "26")])
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
