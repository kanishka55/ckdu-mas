"""Agent Monitor reads isolated run events, never inferred/demo status."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core import msg_log, ontology
from src.sim.agent_monitor import (
    derive_agent_states,
    load_agent_monitor,
    read_event_journal,
    read_message_events,
)


class AgentMonitorParsingTests(unittest.TestCase):
    def test_empty_and_malformed_event_journals_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_text("", encoding="utf-8")
            self.assertEqual(read_event_journal(path)[1], "empty")
            path.write_text('{"event_type":"heartbeat"}\nnot-json\n', encoding="utf-8")
            events, status, errors = read_event_journal(path)
            self.assertEqual(len(events), 1)
            self.assertEqual(status, "incomplete")
            self.assertIn("line 2", errors[0])

    def test_message_reader_requires_seven_columns_and_reports_bad_payloads(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "messages.csv"
            path.write_text("timestamp,sender,receiver\n", encoding="utf-8")
            self.assertEqual(read_message_events(path)[1], "incomplete")
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(msg_log.COLUMNS)
                writer.writerow(["2026-01-01T00:00:00+00:00", "a@x", "b@x", "inform",
                                 "t", "{}", "sent"])
            messages, status, errors = read_message_events(path)
            self.assertEqual(status, "incomplete")
            self.assertEqual(messages[0]["payload_status"], "malformed")
            self.assertTrue(errors)

    def test_empty_log_is_not_reported_as_successful_activity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "messages.csv"
            path.write_text(",".join(msg_log.COLUMNS) + "\n", encoding="utf-8")
            rows, status, _ = read_message_events(path)
            self.assertEqual(rows, ())
            self.assertEqual(status, "empty")

    def test_lifecycle_state_requires_observed_events_and_fresh_run(self):
        timestamp = "2026-01-01T00:00:00+00:00"
        observed_at = datetime.fromisoformat(timestamp)
        events = ({"event_type": "agent_started", "timestamp": timestamp,
                   "component": "RiskScoringAgent", "identity": "risk@localhost"},)
        rows = derive_agent_states(events, (), run_status="running", run_fresh=True,
                                   now=observed_at)
        self.assertEqual(rows[0]["Status"], "running (start observed)")
        with_round = derive_agent_states(
            (*events, {"event_type": "agent_stopped", "timestamp": timestamp,
                       "component": "RiskScoringAgent", "identity": "risk@localhost"},
             {"event_type": "round_completed", "timestamp": timestamp,
              "component": "ResourceCoordinatorAgent", "identity": "risk@localhost"}),
            (), run_status="completed", run_fresh=False, now=observed_at,
        )
        self.assertEqual(with_round[0]["Status"], "completed")
        stale = derive_agent_states(events, (), run_status="running", run_fresh=False)
        self.assertEqual(stale[0]["Status"], "stale")
        stopped = derive_agent_states((*events, {"event_type": "agent_stopped",
                                                "timestamp": timestamp,
                                                "component": "RiskScoringAgent",
                                                "identity": "risk@localhost"}), (),
                                      run_status="completed", run_fresh=False)
        self.assertEqual(stopped[0]["Status"], "completed")
        failed = derive_agent_states(
            ({"event_type": "agent_start_failed", "timestamp": timestamp,
              "component": "RiskScoringAgent", "identity": "risk@localhost"},),
            (), run_status="failed", run_fresh=False, now=observed_at,
        )
        self.assertEqual(failed[0]["Status"], "failed")
        unknown = derive_agent_states((), (), run_status="running", run_fresh=True)
        self.assertEqual(unknown, ())

    def test_configured_agent_without_observed_lifecycle_is_unknown(self):
        rows = derive_agent_states((), (), run_status="running", run_fresh=True,
                                   configured_agents=({"component": "ClockAgent",
                                                       "identity": "clock@localhost"},))
        self.assertEqual(rows[0]["Status"], "unknown")

    def test_direction_counts_do_not_double_count_sent_received_copies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "logs").mkdir()
            path = root / "logs/messages.csv"
            (root / "run_manifest.json").write_text(
                json.dumps({"run_id": "direction-counts", "status": "completed"}),
                encoding="utf-8",
            )
            body = {"type": "month_tick", "month_index": 0, "month": 10, "season": "Wet"}
            payload = ontology.encode(ontology.INFORM, body)
            msg_log.log_message("clock@localhost", "monitor@localhost", ontology.INFORM,
                                "tick-0", payload, "sent", path=path)
            msg_log.log_message("clock@localhost", "monitor@localhost", ontology.INFORM,
                                "tick-0", payload, "received", path=path)
            snapshot = load_agent_monitor(root, now=datetime.now(timezone.utc))
            counts = {row["Identity"]: row for row in snapshot.message_counts}
            self.assertEqual(counts["clock@localhost"]["Sent"], 1)
            self.assertEqual(counts["clock@localhost"]["Received"], 0)
            self.assertEqual(counts["monitor@localhost"]["Sent"], 0)
            self.assertEqual(counts["monitor@localhost"]["Received"], 1)

    def test_active_stale_completed_failed_and_missing_run_states(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "run"
            (root / "logs").mkdir(parents=True)
            now = datetime.now(timezone.utc)
            manifest = {"run_id": "test-run", "status": "running", "agent_lifecycle": []}
            (root / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            event_path = root / "agent_events.jsonl"
            event_path.write_text(json.dumps({"event_type": "heartbeat", "timestamp": now.isoformat(),
                                              "component": "SimulationRunner"}) + "\n"
                                  + json.dumps({"event_type": "agent_started", "timestamp": now.isoformat(),
                                                "component": "ClockAgent", "identity": "clock@localhost"}) + "\n",
                                  encoding="utf-8")
            (root / "logs/messages.csv").write_text(",".join(msg_log.COLUMNS) + "\n", encoding="utf-8")
            live = load_agent_monitor(root, now=now)
            self.assertEqual(live.freshness, "live")
            self.assertEqual(live.agents[0]["Status"], "running (start observed)")
            stale = load_agent_monitor(root, now=now + timedelta(seconds=30))
            self.assertEqual(stale.freshness, "stale")

            manifest["status"] = "failed"
            (root / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            failed = load_agent_monitor(root, now=now)
            self.assertEqual(failed.freshness, "failed snapshot")

    def test_demo_provenance_file_is_not_accepted_as_simulation_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "provenance.json").write_text(json.dumps({"provenance": "illustrative_local_demo"}),
                                                  encoding="utf-8")
            snapshot = load_agent_monitor(root)
            self.assertEqual(snapshot.source_status, "unavailable")
            self.assertEqual(snapshot.run_status, "unknown")
            self.assertIn("Run manifest unavailable", snapshot.errors[0])

    def test_months_rounds_hotspots_and_alerts_are_loaded_from_selected_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "rounds").mkdir()
            (root / "logs").mkdir()
            (root / "run_manifest.json").write_text(json.dumps({
                "run_id": "snapshot", "status": "completed_with_unavailable_stages",
                "agent_lifecycle": [], "month_status": [{"month_index": 3,
                "calendar_month": 8, "status": "complete", "contract_net": "confirmed"}],
            }), encoding="utf-8")
            (root / "agent_events.jsonl").write_text(json.dumps({
                "event_type": "run_finished", "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "completed_with_unavailable_stages"}) + "\n", encoding="utf-8")
            (root / "logs/messages.csv").write_text(",".join(msg_log.COLUMNS) + "\n", encoding="utf-8")
            (root / "rounds/month_index_0003_round_1.json").write_text(json.dumps({
                "month_index": 3, "round": 1, "award_status": "confirmed", "winner": "Demo"}),
                encoding="utf-8")
            (root / "hotspot_screening.json").write_text(json.dumps([{
                "month_index": 3, "ds_division": "Demo", "status": "unavailable"}]), encoding="utf-8")
            (root / "alert_decisions.json").write_text(json.dumps([{
                "month_index": 3, "ds_division": "Demo", "status": "unavailable",
                "delivery_status": "not_requested"}]), encoding="utf-8")
            snapshot = load_agent_monitor(root)
            self.assertEqual(snapshot.freshness, "completed snapshot")
            self.assertEqual(snapshot.months[0]["month_index"], 3)
            self.assertEqual(snapshot.rounds[0]["winner"], "Demo")
            self.assertEqual(snapshot.hotspots[0]["status"], "unavailable")
            self.assertEqual(snapshot.alerts[0]["delivery_status"], "not_requested")

    def test_malformed_round_is_incomplete_not_silently_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "rounds").mkdir()
            (root / "run_manifest.json").write_text(json.dumps({"run_id": "partial", "status": "failed"}),
                                                    encoding="utf-8")
            (root / "rounds/broken.json").write_text("{", encoding="utf-8")
            snapshot = load_agent_monitor(root)
            self.assertEqual(snapshot.rounds_status, "incomplete")
            self.assertTrue(any("Malformed round result" in error for error in snapshot.errors))


if __name__ == "__main__":
    unittest.main()
