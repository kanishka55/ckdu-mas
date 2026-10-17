"""Focused integration checks for the current logger, alert and evaluator."""

from __future__ import annotations

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path

from src.agents.alert import AlertAgent
from src.core.msg_log import COLUMNS, log_message
from src.sim.evaluate import calculate_alert_latency, read_message_events


class TestMemberDIntegration(unittest.TestCase):
    def test_alert_uses_typed_sent_row_and_suppression_is_not_logged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            message_path = Path(temp) / "isolated-messages.csv"

            def isolated_logger(sender, receiver, performative, thread_id, payload, direction):
                log_message(sender, receiver, performative, thread_id, payload, direction, path=message_path)

            agent = AlertAgent(logger=isolated_logger)
            risk_record = {
                "type": "risk_record", "month_index": 0,
                "ds_division": "Synthetic Fixture Division", "wqi": 120.0,
                "tier": "Poor", "drivers": ["synthetic_fixture"],
            }
            issued = agent.process_risk_record(risk_record, hotspot=True)
            suppressed = agent.process_risk_record(risk_record, hotspot=True)
            self.assertEqual(issued.status, "emitted")
            self.assertEqual(suppressed.status, "suppressed")

            events = read_message_events(message_path)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].direction, "sent")
            self.assertEqual(events[0].payload["type"], "alert")
            self.assertEqual(events[0].payload["month_index"], 0)
            with message_path.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(next(csv.reader(handle)), COLUMNS)

            # AlertAgent's generated alert thread differs from its input thread;
            # the evaluator must not infer a match from village/month proximity.
            latency = calculate_alert_latency(events, lambda event: event.payload.get("type") == "risk_record")
            self.assertEqual(latency.status, "unavailable")
            self.assertIsNone(latency.value)
            self.assertFalse(latency.details["suppression_events_counted"])

    def test_missing_team_outputs_remain_unavailable_in_evaluator(self) -> None:
        from src.sim.evaluate import evaluate

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with contextlib.redirect_stdout(io.StringIO()):
                results = evaluate(
                    messages_path=root / "absent-messages.csv",
                    allocations_path=root / "absent-allocations.csv",
                    output_path=root / "evaluation-summary.csv",
                )
            result_by_metric = {result.metric: result for result in results}
            for metric in (
                "risk_classification_alignment", "allocation_efficiency", "alert_latency",
                "negotiation_rounds", "negotiation_logged_event_rows",
            ):
                self.assertEqual(result_by_metric[metric].status, "unavailable")
                self.assertIsNone(result_by_metric[metric].value)

    def test_dashboard_missing_team_outputs_remain_unavailable(self) -> None:
        from dashboard.app import load_dashboard_data

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = load_dashboard_data(
                risk_scores_path=root / "absent-risk-scores.csv",
                well_scores_path=root / "absent-well-scores.csv",
                allocations_path=root / "absent-allocations.csv",
            )
        self.assertEqual(data.team_risks, ())
        self.assertEqual(data.team_wells, ())
        self.assertIn("Unavailable", data.team_risk_status)
        self.assertIn("Unavailable", data.team_well_status)
        self.assertIn("Unavailable", data.team_allocation_status)


if __name__ == "__main__":
    unittest.main()
