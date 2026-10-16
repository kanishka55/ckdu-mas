"""Evaluation unit/integration tests with deterministic illustrative fixtures."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from src.sim.evaluate import (
    ALLOCATION_COLUMNS,
    EvaluationInputError,
    calculate_alert_latency,
    calculate_allocation_efficiency,
    calculate_negotiation_efficiency,
    calculate_risk_alignment,
    evaluate,
    read_allocation_rows,
    read_message_events,
)
from src.core.ontology import validate_message


FIXTURES = Path(__file__).parent / "fixtures"
ILLUSTRATIVE_REFERENCE_TIERS = {"Fixture A": "Poor", "Fixture B": "Poor", "Fixture C": "Excellent"}
ILLUSTRATIVE_POPULATION = {"Fixture A": 10, "Fixture B": 20, "Fixture C": 5}
ILLUSTRATIVE_RISK_WEIGHTS = {("Fixture A", 1): 3.0, ("Fixture B", 1): 1.0, ("Fixture C", 1): 2.0}


def _fixture_records() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "risk_records.json").read_text(encoding="utf-8"))["records"]


def _fixture_events():
    return read_message_events(FIXTURES / "messages.csv")


class TestEvaluationMetrics(unittest.TestCase):
    def test_risk_alignment_exact_agreement_and_partial_coverage(self) -> None:
        result = calculate_risk_alignment(_fixture_records(), ILLUSTRATIVE_REFERENCE_TIERS)
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.value or 0, 2 / 3)
        self.assertEqual(result.details["aligned"], 2)
        self.assertIn("not causal validation", result.details["interpretation"])

        partial = calculate_risk_alignment(_fixture_records(), {"Fixture A": "Poor"})
        self.assertEqual(partial.status, "partial")
        self.assertEqual(partial.details["unmatched_records"], 2)

    def test_risk_alignment_without_mapping_or_data_is_unavailable_not_zero(self) -> None:
        self.assertIsNone(calculate_risk_alignment(_fixture_records(), None).value)
        empty = calculate_risk_alignment([], ILLUSTRATIVE_REFERENCE_TIERS)
        self.assertEqual(empty.status, "unavailable")
        self.assertIsNone(empty.value)

    def test_invalid_risk_record_and_invalid_reference_tier_fail_loudly(self) -> None:
        with self.assertRaises(EvaluationInputError):
            calculate_risk_alignment([{"ds_division": "Fixture A", "tier": "Poor"}], ILLUSTRATIVE_REFERENCE_TIERS)
        bad_reference = dict(ILLUSTRATIVE_REFERENCE_TIERS, **{"Fixture A": "Unknown"})
        with self.assertRaises(EvaluationInputError):
            calculate_risk_alignment(_fixture_records(), bad_reference)

    def test_allocation_comparisons_use_shared_risk_weighted_population_metric(self) -> None:
        rows = read_allocation_rows(FIXTURES / "allocation_outcomes.csv")
        results = calculate_allocation_efficiency(rows, ILLUSTRATIVE_POPULATION, ILLUSTRATIVE_RISK_WEIGHTS)
        by_metric = {result.metric: result for result in results}
        self.assertEqual(by_metric["risk_weighted_population_served"].status, "ok")
        scores = {result.scope.split("method=")[-1]: result.value for result in results if result.metric == "risk_weighted_population_served"}
        self.assertEqual(scores, {"contract_net": 30.0, "equal_split": 20.0, "first_come": 10.0})
        deltas = {result.metric: result.value for result in results if result.metric.startswith("contract_net_minus_")}
        self.assertEqual(deltas, {"contract_net_minus_equal_split": 10.0, "contract_net_minus_first_come": 20.0})

    def test_missing_baseline_is_unavailable_and_not_counted_as_zero(self) -> None:
        rows = [row for row in read_allocation_rows(FIXTURES / "allocation_outcomes.csv") if row.method != "first_come"]
        results = calculate_allocation_efficiency(rows, ILLUSTRATIVE_POPULATION, ILLUSTRATIVE_RISK_WEIGHTS)
        first_come_score = next(r for r in results if r.metric == "risk_weighted_population_served" and r.scope.endswith("method=first_come"))
        comparison = next(r for r in results if r.metric == "contract_net_minus_first_come")
        self.assertIsNone(first_come_score.value)
        self.assertEqual(first_come_score.status, "unavailable")
        self.assertIsNone(comparison.value)
        self.assertIn("first_come", comparison.details["reason"])

    def test_missing_population_or_risk_weight_does_not_drop_allocation_silently(self) -> None:
        rows = read_allocation_rows(FIXTURES / "allocation_outcomes.csv")
        with self.assertRaisesRegex(EvaluationInputError, "missing population"):
            calculate_allocation_efficiency(rows, {}, ILLUSTRATIVE_RISK_WEIGHTS)
        with self.assertRaisesRegex(EvaluationInputError, "missing numeric risk weight"):
            calculate_allocation_efficiency(rows, ILLUSTRATIVE_POPULATION, {})
        unavailable = calculate_allocation_efficiency(rows, None, ILLUSTRATIVE_RISK_WEIGHTS)
        self.assertIsNone(unavailable[0].value)

    def test_alert_latency_pairs_by_village_month_and_ignores_suppression(self) -> None:
        events = _fixture_events()
        result = calculate_alert_latency(
            events,
            lambda event: event.payload.get("event_type") == "risk_input_received",
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, 75.0)
        self.assertEqual(result.observations, 1)
        self.assertFalse(result.details["suppression_events_counted"])

    def test_alert_latency_reports_unmatched_and_missing_selector(self) -> None:
        events = [event for event in _fixture_events() if event.payload.get("ds_division") != "Fixture A" or event.payload.get("event_type") != "risk_input_received"]
        partial = calculate_alert_latency(events, lambda event: event.payload.get("event_type") == "risk_input_received")
        self.assertEqual(partial.status, "unavailable")
        self.assertIsNone(partial.value)
        self.assertEqual(partial.details["unmatched_alerts"], 1)
        self.assertEqual(calculate_alert_latency(_fixture_events(), None).status, "unavailable")
        self.assertEqual(calculate_alert_latency([], lambda _: True).status, "unavailable")

    def test_alert_latency_refuses_ambiguous_duplicate_endpoint_rows(self) -> None:
        events = _fixture_events()
        issued = next(event for event in events if event.payload.get("event_type") == "alert_issued")
        duplicated = events + [issued]
        result = calculate_alert_latency(
            duplicated,
            lambda event: event.payload.get("event_type") == "risk_input_received",
        )
        self.assertEqual(result.status, "unavailable")
        self.assertIn("ambiguous sent/received direction", result.details["reason"])

    def test_alert_event_fixture_obeys_both_provisional_message_schemas(self) -> None:
        payloads = json.loads((FIXTURES / "alert_events.json").read_text(encoding="utf-8"))["events"]
        for payload in payloads:
            schema_name = "alert" if payload["event_type"] == "alert_issued" else "alert_suppression"
            with self.subTest(schema=schema_name):
                validate_message(schema_name, payload)

    def test_negotiation_rounds_deduplicate_cfp_rounds_but_count_log_rows(self) -> None:
        results = calculate_negotiation_efficiency(_fixture_events())
        rounds = next(result for result in results if result.metric == "negotiation_rounds")
        messages = next(result for result in results if result.metric == "negotiation_logged_event_rows")
        self.assertEqual(rounds.value, 2)
        self.assertEqual(messages.value, 5)
        self.assertIn("sent and received rows cannot be distinguished", messages.details["counting_note"])

    def test_empty_negotiation_history_is_unavailable(self) -> None:
        results = calculate_negotiation_efficiency([])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.value is None and result.status == "unavailable" for result in results))


class TestEvaluationInputsAndOutput(unittest.TestCase):
    def test_missing_timestamp_and_malformed_payload_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "messages.csv"
            path.write_text(
                "timestamp,sender,receiver,performative,thread_id,payload\n"
                ",a,b,inform,t,{}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvaluationInputError, "timestamp is missing"):
                read_message_events(path)
            path.write_text(
                "timestamp,sender,receiver,performative,thread_id,payload\n"
                "2026-01-01T00:00:00+00:00,a,b,inform,t,{bad json}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvaluationInputError, "valid JSON"):
                read_message_events(path)

    def test_missing_files_and_malformed_allocation_rows_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.csv"
            with self.assertRaises(FileNotFoundError):
                read_message_events(missing)
            malformed = Path(temp_dir) / "allocations.csv"
            malformed.write_text(",".join(ALLOCATION_COLUMNS) + "\nmonth,bowser,A,1,0.4,contract_net\n", encoding="utf-8")
            with self.assertRaisesRegex(EvaluationInputError, "month, round and winning_bid"):
                read_allocation_rows(malformed)

    def test_unmatched_alerts_missing_baselines_and_empty_dataset_write_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "evaluation_summary.csv"
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                results = evaluate(
                    messages_path=FIXTURES / "messages.csv",
                    allocations_path=FIXTURES / "allocation_outcomes.csv",
                    output_path=output_path,
                    reference_tier_by_village=ILLUSTRATIVE_REFERENCE_TIERS,
                    population_by_village=ILLUSTRATIVE_POPULATION,
                    risk_weight_by_village_month=ILLUSTRATIVE_RISK_WEIGHTS,
                    risk_input_selector=lambda event: event.payload.get("event_type") == "risk_input_received",
                    risk_records=_fixture_records(),
                )
            self.assertTrue(output_path.is_file())
            with output_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows)
            self.assertTrue(any(row["metric"] == "alert_latency" and row["value"] == "75.0" for row in rows))
            self.assertIn("METRIC", printed.getvalue())
            self.assertIn("risk_classification_alignment", {result.metric for result in results})

            empty_output = Path(temp_dir) / "empty" / "summary.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                empty = evaluate(
                    messages_path=Path(temp_dir) / "absent_messages.csv",
                    allocations_path=Path(temp_dir) / "absent_allocations.csv",
                    output_path=empty_output,
                )
            self.assertTrue(empty_output.is_file())
            self.assertTrue(all(result.value is None for result in empty))


if __name__ == "__main__":
    unittest.main()
