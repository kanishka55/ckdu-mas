"""Cross-component tests for the Member D logger, alerts and evaluator."""

from __future__ import annotations

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path

from dashboard.app import load_dashboard_data
from src.agents.alert import AlertAgent
from src.core.msg_log import CSV_COLUMNS, log_message
from src.sim.evaluate import (
    calculate_alert_latency,
    evaluate,
    read_message_events,
)


class TestMemberDIntegration(unittest.TestCase):
    def test_alert_log_events_validate_and_suppression_is_not_latency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            message_path = Path(temp) / "isolated-messages.csv"

            # This event marker is an explicit test seam for evaluate.py's
            # injected selector, not yet an agreed shared ontology message.
            log_message(
                "WellMonitorAgent", "RiskScoringAgent", "inform", "risk:Fixture A:1",
                {"event_type": "risk_input_received", "ds_division": "Fixture A",
                 "month": 1, "source": "synthetic"},
                log_path=message_path,
            )

            def isolated_logger(sender, receiver, performative, thread_id, payload):
                log_message(sender, receiver, performative, thread_id, payload, log_path=message_path)

            alert_agent = AlertAgent(logger=isolated_logger)
            risk_record = {
                "ds_division": "Fixture A", "month": 1, "wqi": 120.0,
                "tier": "Poor", "drivers": {"fixture_parameter": 1.0},
                "n_wells": 1, "source": "synthetic",
            }
            issued = alert_agent.process_risk_record(risk_record)
            suppressed = alert_agent.process_risk_record(risk_record)

            events = read_message_events(message_path)
            event_types = [event.payload.get("event_type") for event in events]
            self.assertEqual(event_types.count("alert_issued"), 1)
            self.assertEqual(event_types.count("alert_suppressed"), 1)
            self.assertEqual(issued.status, "emitted")
            self.assertEqual(suppressed.status, "suppressed")
            suppression = next(event for event in events if event.payload["event_type"] == "alert_suppressed")
            self.assertEqual(suppression.sender, suppression.receiver)
            self.assertEqual(suppression.performative, "")

            latency = calculate_alert_latency(
                events,
                lambda event: event.payload.get("event_type") == "risk_input_received",
            )
            self.assertEqual(latency.observations, 1)
            self.assertEqual(latency.status, "ok")
            self.assertFalse(latency.details["suppression_events_counted"])
            with message_path.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(next(csv.reader(handle)), list(CSV_COLUMNS))

    def test_missing_team_outputs_stay_unavailable_across_evaluator_and_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            absent_messages = temp_path / "absent-messages.csv"
            absent_allocations = temp_path / "absent-allocations.csv"
            summary_path = temp_path / "evaluation-summary.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                results = evaluate(
                    messages_path=absent_messages,
                    allocations_path=absent_allocations,
                    output_path=summary_path,
                )
            result_by_metric = {result.metric: result for result in results}
            self.assertEqual(result_by_metric["risk_classification_alignment"].status, "unavailable")
            self.assertEqual(result_by_metric["allocation_efficiency"].status, "unavailable")
            self.assertIsNone(result_by_metric["risk_classification_alignment"].value)
            self.assertIsNone(result_by_metric["allocation_efficiency"].value)
            self.assertTrue(summary_path.is_file())

            dashboard_data = load_dashboard_data(
                messages_path=absent_messages,
                allocations_path=absent_allocations,
                evaluation_path=temp_path / "absent-summary.csv",
            )
            self.assertEqual(dashboard_data.team_risks, ())
            self.assertEqual(dashboard_data.team_allocations, ())
            self.assertIn("Unavailable", dashboard_data.team_risk_status)
            self.assertIn("Unavailable", dashboard_data.team_allocation_status)


if __name__ == "__main__":
    unittest.main()
