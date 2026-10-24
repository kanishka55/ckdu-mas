"""Evaluator tests use deterministic illustrative fixtures, never team results."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.ontology import INFORM, SchemaError, encode
from src.sim.evaluate import (
    ALLOCATION_COLUMNS, AllocationRow, EvaluationInputError, MessageEvent,
    calculate_alert_latency, calculate_allocation_efficiency,
    calculate_negotiation_efficiency, calculate_risk_alignment, evaluate,
    extract_risk_records, read_allocation_rows, read_message_events,
)

FIXTURES = Path(__file__).parent / "fixtures"
ILLUSTRATIVE_REFERENCE_TIERS = {"Fixture A": "Poor", "Fixture B": "Good", "Fixture C": "Excellent"}


def fixture_records() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "risk_records.json").read_text(encoding="utf-8"))["records"]


def fixture_events() -> list[MessageEvent]:
    return read_message_events(FIXTURES / "messages.csv")


class TestEvaluationMetrics(unittest.TestCase):
    def test_current_seven_column_typed_fixture_parses_and_extracts_once(self) -> None:
        events = fixture_events()
        self.assertTrue(all(event.direction in {"sent", "received"} for event in events))
        records = extract_risk_records(events)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["month_index"], 0)
        self.assertEqual(records[0]["drivers"], ["synthetic_fixture"])

    def test_risk_alignment_unavailable_without_explicit_reference_mapping(self) -> None:
        result = calculate_risk_alignment(fixture_records(), None)
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.value)
        self.assertIn("not supplied", result.details["reason"])

    def test_explicit_alignment_mapping_is_labelled_as_alignment_only(self) -> None:
        result = calculate_risk_alignment(fixture_records(), ILLUSTRATIVE_REFERENCE_TIERS)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, 1.0)
        self.assertIn("not scientific validation", result.details["interpretation"])
        self.assertIn("provenance not verified", result.details["reference_mapping_source"])

    def test_partial_risk_alignment_reports_unmatched_illustrative_records(self) -> None:
        # Illustrative records only: this mapping is a test seam, not a team or
        # scientific reference-tier mapping.
        result = calculate_risk_alignment(fixture_records(), {"Fixture A": "Poor"})
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.value, 1.0)
        self.assertEqual(result.observations, 1)
        self.assertEqual(result.details["unmatched_records"], 2)
        self.assertIn("reference-tier labels", result.details["missing_dependency"])
        self.assertTrue(result.details["required_action"])

    def test_invalid_risk_record_fails_loudly(self) -> None:
        with self.assertRaises(EvaluationInputError):
            calculate_risk_alignment([{"type": "risk_record", "ds_division": "A"}], {"A": "Poor"})

    def test_allocation_metric_unavailable_without_actual_outputs_baselines_and_definition(self) -> None:
        rows = [AllocationRow(1, "bowser", "Fixture A", 1, 0.9, "contract_net")]
        result = calculate_allocation_efficiency(rows, {"Fixture A": 10}, {("Fixture A", 1): 1.0})
        self.assertEqual(result[0].status, "unavailable")
        self.assertIsNone(result[0].value)
        self.assertIn("not attested", result[0].details["reason"])

    def test_allocation_metric_unavailable_for_missing_baselines_even_when_attested(self) -> None:
        rows = [AllocationRow(1, "bowser", "Fixture A", 1, 0.9, "contract_net")]
        results = calculate_allocation_efficiency(
            rows, {"Fixture A": 10}, {("Fixture A", 1): 1.0},
            allocation_results_verified=True, risk_weight_definition="team-approved definition",
        )
        comparison = next(item for item in results if item.metric == "contract_net_minus_equal_split")
        self.assertEqual(comparison.status, "unavailable")
        self.assertIsNone(comparison.value)

    def test_illustrative_complete_allocation_comparisons_calculate_expected_values(self) -> None:
        # Synthetic arithmetic fixtures, not actual Member C outcomes or an
        # approved project risk-weight definition.
        rows = [
            AllocationRow(1, "bowser", "Fixture A", 1, 0.9, "contract_net"),
            AllocationRow(1, "bowser", "Fixture B", 1, 0.8, "equal_split"),
            AllocationRow(1, "bowser", "Fixture C", 1, 0.7, "first_come"),
        ]
        population = {"Fixture A": 10, "Fixture B": 5, "Fixture C": 4}
        weights = {("Fixture A", 1): 2.0, ("Fixture B", 1): 1.0, ("Fixture C", 1): 0.5}
        results = calculate_allocation_efficiency(
            rows, population, weights,
            allocation_results_verified=True,
            risk_weight_definition="illustrative synthetic fixture weight",
        )
        served = {item.scope.rsplit("method=", 1)[1]: item.value for item in results
                  if item.metric == "risk_weighted_population_served"}
        comparisons = {item.metric: item.value for item in results
                       if item.metric.startswith("contract_net_minus_")}
        self.assertEqual(served, {"contract_net": 20.0, "equal_split": 5.0, "first_come": 2.0})
        self.assertEqual(comparisons, {
            "contract_net_minus_equal_split": 15.0,
            "contract_net_minus_first_come": 18.0,
        })
        self.assertTrue(all(item.status == "ok" for item in results))

    def test_alert_latency_requires_exact_thread_village_and_month_correlation(self) -> None:
        events = fixture_events()
        result = calculate_alert_latency(
            events,
            lambda event: event.payload.get("type") == "risk_record",
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, 75.0)
        self.assertEqual(result.observations, 1)
        self.assertIn("exact thread_id", result.details["pairing"])
        self.assertFalse(result.details["suppression_events_counted"])

    def test_alert_latency_unavailable_for_unmatched_or_ambiguous_events(self) -> None:
        events = fixture_events()
        alert = next(event for event in events if event.payload.get("type") == "alert")
        unmatched = [event for event in events if event.payload.get("type") != "risk_record"]
        result = calculate_alert_latency(unmatched, lambda _: False)
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.value)
        duplicated = events + [alert]
        ambiguous = calculate_alert_latency(duplicated, lambda event: event.payload.get("type") == "risk_record")
        self.assertEqual(ambiguous.status, "unavailable")
        self.assertIn("ambiguous", ambiguous.details["reason"])

    def test_alert_latency_uses_explicit_decision_bridge_for_distinct_alert_thread(self) -> None:
        # Illustrative event rows model the persisted AlertProcessingAgent
        # bridge: alert thread -> original source risk thread/division/month.
        events = fixture_events()
        source_thread = next(event.thread_id for event in events
                             if event.payload.get("type") == "risk_record")
        bridged = [
            MessageEvent(event.timestamp, event.sender, event.receiver, event.performative,
                         "alert:Fixture A:0:1" if event.payload.get("type") == "alert" else event.thread_id,
                         event.payload, event.direction)
            for event in events
        ]
        result = calculate_alert_latency(
            bridged,
            lambda event: (event.payload.get("type") == "risk_record"
                           and event.receiver == "AlertAgent"),
            alert_correlation_by_thread={"alert:Fixture A:0:1": [{
                "source_thread_id": source_thread, "ds_division": "Fixture A", "month_index": 0,
            }]},
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, 75.0)
        self.assertIn("source_thread_id", result.details["pairing"])

    def test_alert_latency_bridge_missing_event_is_unavailable(self) -> None:
        events = [event for event in fixture_events() if event.payload.get("type") != "risk_record"]
        alert = next(event for event in events if event.payload.get("type") == "alert")
        result = calculate_alert_latency(
            events, lambda event: event.receiver == "AlertProcessingAgent",
            alert_correlation_by_thread={alert.thread_id: [{
                "source_thread_id": "missing-risk-thread", "ds_division": "Fixture A", "month_index": 0,
            }]},
        )
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.value)
        self.assertEqual(result.details["unmatched_alerts"], 1)

    def test_alert_latency_bridge_duplicate_risk_receipts_are_ambiguous(self) -> None:
        events = fixture_events()
        risk = next(event for event in events if event.payload.get("type") == "risk_record"
                    and event.direction == "received")
        alert = next(event for event in events if event.payload.get("type") == "alert")
        duplicate = events + [risk]
        result = calculate_alert_latency(
            duplicate,
            lambda event: event.payload.get("type") == "risk_record",
            alert_correlation_by_thread={alert.thread_id: [{
                "source_thread_id": risk.thread_id, "ds_division": "Fixture A", "month_index": 0,
            }]},
        )
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.value)
        self.assertEqual(result.details["ambiguous_pairs"], 1)

    def test_alert_latency_bridge_conflicting_links_are_ambiguous(self) -> None:
        alert = next(event for event in fixture_events() if event.payload.get("type") == "alert")
        result = calculate_alert_latency(
            fixture_events(), lambda event: event.payload.get("type") == "risk_record",
            alert_correlation_by_thread={alert.thread_id: [
                {"source_thread_id": "risk-a", "ds_division": "Fixture A", "month_index": 0},
                {"source_thread_id": "risk-b", "ds_division": "Fixture A", "month_index": 0},
            ]},
        )
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.details["ambiguous_pairs"], 1)

    def test_partial_alert_latency_explains_unmatched_issued_alerts(self) -> None:
        events = fixture_events()
        risk = next(event for event in events if event.payload.get("type") == "risk_record")
        alert = next(event for event in events if event.payload.get("type") == "alert")
        first_thread, second_thread = "alert:Fixture A:0:1", "alert:Fixture A:0:2"
        changed = [MessageEvent(event.timestamp, event.sender, event.receiver, event.performative,
                                first_thread if event.payload.get("type") == "alert" else event.thread_id,
                                event.payload, event.direction)
                   for event in events]
        changed.append(MessageEvent(alert.timestamp, alert.sender, alert.receiver, alert.performative,
                                    second_thread, alert.payload, "sent"))
        result = calculate_alert_latency(
            changed, lambda event: event.payload.get("type") == "risk_record",
            alert_correlation_by_thread={
                first_thread: [{"source_thread_id": risk.thread_id, "ds_division": "Fixture A", "month_index": 0}],
                second_thread: [{"source_thread_id": "missing-thread", "ds_division": "Fixture A", "month_index": 0}],
            },
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.value, 75.0)
        self.assertEqual(result.details["unmatched_alerts"], 1)
        self.assertIn("unique risk receipt", result.details["missing_dependency"])

    def test_latency_requires_received_risk_record_selector(self) -> None:
        events = fixture_events()
        with self.assertRaisesRegex(EvaluationInputError, "typed risk_record"):
            calculate_alert_latency(events, lambda event: event.payload.get("type") == "alert")
        self.assertEqual(calculate_alert_latency(events, None).status, "unavailable")
        self.assertEqual(calculate_alert_latency([], lambda _: True).status, "unavailable")

    def test_negotiation_is_unavailable_until_history_completeness_is_confirmed(self) -> None:
        results = calculate_negotiation_efficiency(fixture_events())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item.status == "unavailable" and item.value is None for item in results))
        self.assertIn("not confirmed", results[0].details["reason"])

    def test_complete_illustrative_negotiation_history_counts_direction_copies_once(self) -> None:
        results = calculate_negotiation_efficiency(fixture_events(), history_complete=True)
        rounds = next(item for item in results if item.metric == "negotiation_rounds")
        messages = next(item for item in results if item.metric == "negotiation_logged_event_rows")
        self.assertEqual(rounds.value, 2)
        self.assertEqual(messages.value, 5)
        self.assertIn("count once", messages.details["counting_note"])

    def test_broadcast_cfp_to_multiple_receivers_counts_distinct_messages_once_each(self) -> None:
        # The shared logger records the same oriented endpoints on sent and
        # received rows. Identical CFP broadcasts to different recipients are
        # different messages despite sharing thread and payload.
        body = {"type": "cfp", "month_index": 3, "round": 1, "resource": "bowser",
                "units_available": 1, "deadline_s": 2.0}
        events = []
        for recipient in ("Village A", "Village B"):
            events.extend((
                MessageEvent(datetime(2026, 1, 1, tzinfo=timezone.utc), "Coordinator", recipient,
                             "cfp", "broadcast-thread", body, "sent"),
                MessageEvent(datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc), "Coordinator", recipient,
                             "cfp", "broadcast-thread", body, "received"),
            ))
        results = calculate_negotiation_efficiency(events, history_complete=True)
        rounds = next(item for item in results if item.metric == "negotiation_rounds")
        messages = next(item for item in results if item.metric == "negotiation_logged_event_rows")
        self.assertEqual(rounds.value, 1)
        self.assertEqual(messages.value, 2)

    def test_repeated_same_direction_identical_message_is_unavailable_as_ambiguous(self) -> None:
        body = {"type": "cfp", "month_index": 3, "round": 1, "resource": "bowser",
                "units_available": 1, "deadline_s": 2.0}
        duplicate = MessageEvent(datetime(2026, 1, 1, tzinfo=timezone.utc), "Coordinator", "Village A",
                                 "cfp", "duplicate-thread", body, "sent")
        results = calculate_negotiation_efficiency([duplicate, duplicate], history_complete=True)
        self.assertTrue(all(item.status == "unavailable" and item.value is None for item in results))
        self.assertIn("ambiguous", results[0].details["reason"])

    def test_empty_negotiation_history_is_unavailable(self) -> None:
        results = calculate_negotiation_efficiency([])
        self.assertTrue(all(item.value is None and item.status == "unavailable" for item in results))


class TestEvaluatorInputsAndOutput(unittest.TestCase):
    def test_missing_seven_column_header_timestamp_direction_and_payload_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "messages.csv"
            path.write_text("timestamp,sender,receiver,performative,thread_id,payload\n", encoding="utf-8")
            with self.assertRaisesRegex(EvaluationInputError, "expected columns"):
                read_message_events(path)
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["timestamp", "sender", "receiver", "performative", "thread_id", "payload", "direction"])
                writer.writerow(["", "a", "b", INFORM, "t", "{}", "received"])
            with self.assertRaisesRegex(EvaluationInputError, "timestamp is missing"):
                read_message_events(path)
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["timestamp", "sender", "receiver", "performative", "thread_id", "payload", "direction"])
                writer.writerow(["2026-01-01T00:00:00+00:00", "a", "b", INFORM, "t", "{}", "sideways"])
            with self.assertRaisesRegex(EvaluationInputError, "direction"):
                read_message_events(path)
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["timestamp", "sender", "receiver", "performative", "thread_id", "payload", "direction"])
                writer.writerow(["2026-01-01T00:00:00+00:00", "a", "b", INFORM, "t", "{bad}", "received"])
            with self.assertRaisesRegex(EvaluationInputError, "invalid typed message"):
                read_message_events(path)

    def test_malformed_column_count_and_unmatched_events_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "messages.csv"
            path.write_text(
                "timestamp,sender,receiver,performative,thread_id,payload,direction\n"
                "2026-01-01T00:00:00+00:00,a,b,inform,t,{},received,extra\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(EvaluationInputError, "malformed column count"):
                read_message_events(path)
            with self.assertRaises(FileNotFoundError):
                read_message_events(Path(temp) / "missing.csv")

    def test_invalid_ontology_payload_is_not_silently_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.csv"
            payload = json.dumps({"type": "alert", "month_index": 0})
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["timestamp", "sender", "receiver", "performative", "thread_id", "payload", "direction"])
                writer.writerow(["2026-01-01T00:00:00+00:00", "a", "b", INFORM, "t", payload, "sent"])
            with self.assertRaises(EvaluationInputError):
                read_message_events(path)

    def test_allocation_missing_file_and_malformed_rows_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileNotFoundError):
                read_allocation_rows(Path(temp) / "missing.csv")
            malformed = Path(temp) / "allocations.csv"
            malformed.write_text(",".join(ALLOCATION_COLUMNS) + "\nmonth,bowser,A,1,0.4,contract_net\n", encoding="utf-8")
            with self.assertRaisesRegex(EvaluationInputError, "month, round and winning_bid"):
                read_allocation_rows(malformed)

    def test_evaluate_reports_malformed_allocation_input_without_exposing_exception_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            malformed = Path(temp) / "allocations.csv"
            malformed.write_text(
                ",".join(ALLOCATION_COLUMNS) + "\nmonth,bowser,A,1,not-a-number,contract_net\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                results = evaluate(
                    messages_path=Path(temp) / "missing_messages.csv",
                    allocations_path=malformed,
                    output_path=Path(temp) / "summary.csv",
                )
            metric = next(item for item in results if item.metric == "allocation_efficiency")
            self.assertEqual(metric.status, "unavailable")
            self.assertEqual(metric.details["classification"], "implementation")
            self.assertIn("failed validation", metric.details["reason"])
            self.assertNotIn("month, round and winning_bid", metric.details["reason"])

    def test_evaluate_writes_documented_summary_and_unavailable_empty_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "nested" / "evaluation_summary.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                results = evaluate(
                    messages_path=Path(temp) / "absent_messages.csv",
                    allocations_path=Path(temp) / "absent_allocations.csv",
                    output_path=output,
                )
            self.assertTrue(output.is_file())
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(list(rows[0]), ["metric", "scope", "value", "unit", "status", "observations", "details"])
            self.assertTrue(all(result.status == "unavailable" and result.value is None for result in results))

    def test_evaluate_uses_fixture_only_for_test_and_reports_mapping_unavailable_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "summary.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                results = evaluate(
                    messages_path=FIXTURES / "messages.csv",
                    allocations_path=Path(temp) / "no_allocations.csv",
                    output_path=output,
                    risk_input_selector=lambda event: event.payload.get("type") == "risk_record",
                )
            by_name = {item.metric: item for item in results}
            self.assertEqual(by_name["risk_classification_alignment"].status, "unavailable")
            self.assertEqual(by_name["alert_latency"].value, 75.0)  # synthetic fixture correlation only
            self.assertEqual(by_name["allocation_efficiency"].status, "unavailable")
            self.assertTrue(all(item.status == "unavailable" for item in results if item.metric.startswith("negotiation_")))

    def test_evaluate_exposes_actionable_unavailable_metric_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "summary.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                results = evaluate(
                    messages_path=Path(temp) / "absent_messages.csv",
                    allocations_path=Path(temp) / "absent_allocations.csv",
                    output_path=output,
                    risk_records=fixture_records(),
                )
            risk = next(item for item in results if item.metric == "risk_classification_alignment")
            self.assertEqual(risk.details["classification"], "evidence")
            self.assertIn("reference-tier mapping", risk.details["missing_dependency"])
            self.assertTrue(risk.details["required_action"])

    def test_evaluate_accepts_alert_agent_decision_bridge_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            messages = Path(temp) / "messages.csv"
            with (FIXTURES / "messages.csv").open(newline="", encoding="utf-8") as src:
                rows = list(csv.DictReader(src))
            risk_thread = next(row["thread_id"] for row in rows
                               if json.loads(row["payload"]).get("type") == "risk_record")
            alert_row = next(row for row in rows if json.loads(row["payload"]).get("type") == "alert")
            alert_thread = "alert:Fixture A:0:1"
            alert_row["thread_id"] = alert_thread
            with messages.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with contextlib.redirect_stdout(io.StringIO()):
                results = evaluate(
                    messages_path=messages,
                    allocations_path=Path(temp) / "absent_allocations.csv",
                    output_path=Path(temp) / "summary.csv",
                    risk_input_selector=lambda event: (
                        event.payload.get("type") == "risk_record" and event.receiver == "AlertAgent"
                    ),
                    alert_correlation_records=[{
                        "status": "emitted", "alert_thread_id": alert_thread,
                        "source_thread_id": risk_thread, "ds_division": "Fixture A", "month_index": 0,
                    }],
                )
            latency = next(item for item in results if item.metric == "alert_latency")
            self.assertEqual(latency.status, "ok")
            self.assertEqual(latency.value, 75.0)


if __name__ == "__main__":
    unittest.main()
