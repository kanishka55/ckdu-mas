"""Alert tests use deterministic synthetic records, not observations."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from src.agents.alert import AlertAgent
from src.core.msg_log import COLUMNS, DIRECTIONS
from src.core.ontology import INFORM, SchemaError, decode, encode, validate


# Explicit synthetic contract fixture; it is not measured water-quality data.
SYNTHETIC_SAMPLE_RISK_RECORD: dict[str, Any] = {
    "type": "risk_record",
    "month_index": 0,
    "ds_division": "Synthetic Test Division",
    "wqi": 75.0,
    "tier": "Good",
    "drivers": ["synthetic_fixture"],
}


def record(month_index: int, tier: str) -> dict[str, Any]:
    data = dict(SYNTHETIC_SAMPLE_RISK_RECORD)
    data.update(month_index=month_index, tier=tier)
    return data


class TestAlertAgent(unittest.TestCase):
    def setUp(self) -> None:
        self.logged: list[tuple[Any, ...]] = []
        self.agent = AlertAgent(logger=lambda *args: self.logged.append(args))

    def test_current_ontology_and_logger_apis_are_used(self) -> None:
        self.assertEqual(COLUMNS, [
            "timestamp", "sender", "receiver", "performative", "thread_id", "payload", "direction"
        ])
        self.assertEqual(DIRECTIONS, {"sent", "received"})
        body = {
            "type": "alert", "month_index": 0,
            "ds_division": "Synthetic Test Division", "tier": "Good",
            "hotspot": False, "message": "Illustrative message.",
        }
        self.assertEqual(decode(INFORM, encode(INFORM, body)), body)

    def test_good_and_excellent_are_informational(self) -> None:
        for tier in ("Good", "Excellent"):
            with self.subTest(tier=tier):
                result = self.agent.process_risk_record(record(0, tier), hotspot=False)
                self.assertEqual(result.status, "emitted")
                self.assertIn("within published guideline ranges", result.message or "")
                self.assertIn("Risk indicator derived from published water-quality thresholds.", result.message or "")

    def test_poor_guidance_is_non_diagnostic(self) -> None:
        result = self.agent.process_risk_record(record(0, "Poor"), hotspot=False)
        self.assertIn("use an alternate source", result.message or "")
        self.assertIn("for drinking and cooking", result.message or "")
        prohibited = ("unsafe", "contaminated", "causes CKDu", "you may have CKDu", "diagnosis")
        self.assertFalse(any(p.casefold() in (result.message or "").casefold() for p in prohibited))

    def test_poor_hotspot_adds_routine_screening_guidance(self) -> None:
        result = self.agent.process_risk_record(record(0, "Poor"), hotspot=True)
        self.assertIn("routine kidney-health screening", (result.message or "").casefold())
        self.assertIn("alternate source", result.message or "")

    def test_payload_has_exact_typed_schema_and_month_index(self) -> None:
        decision = self.agent.process_risk_record(record(7, "Poor"), hotspot=True)
        payload = decision.alert_payload()
        self.assertEqual(set(payload), {"type", "month_index", "ds_division", "tier", "hotspot", "message"})
        self.assertEqual(payload["month_index"], 7)
        validate(INFORM, payload)
        self.assertEqual(decode(INFORM, encode(INFORM, payload)), payload)

    def test_schema_rejects_missing_unexpected_and_invalid_fields(self) -> None:
        valid = self.agent.process_risk_record(record(0, "Poor"), hotspot=True).alert_payload()
        missing = dict(valid)
        del missing["message"]
        with self.assertRaisesRegex(SchemaError, "missing fields"):
            validate(INFORM, missing)
        unexpected = dict(valid, invented=True)
        with self.assertRaisesRegex(SchemaError, "unexpected fields"):
            validate(INFORM, unexpected)
        invalid_bool = dict(valid, hotspot="yes")
        with self.assertRaisesRegex(SchemaError, "hotspot.*bool"):
            validate(INFORM, invalid_bool)

    def test_issued_alert_uses_sent_direction_and_seven_column_contract(self) -> None:
        self.agent.process_risk_record(record(0, "Good"), hotspot=False)
        self.assertEqual(len(self.logged), 1)
        sender, receiver, performative, thread_id, payload_text, direction = self.logged[0]
        self.assertEqual(sender, "AlertAgent")
        self.assertEqual(receiver, "Synthetic Test Division")
        self.assertEqual(performative, INFORM)
        self.assertEqual(thread_id, "alert:Synthetic Test Division:0")
        self.assertEqual(direction, "sent")
        self.assertEqual(decode(INFORM, payload_text)["type"], "alert")

    def test_identical_month_record_is_suppressed_without_logging(self) -> None:
        first = self.agent.process_risk_record(record(0, "Poor"), hotspot=False)
        duplicate = self.agent.process_risk_record(record(0, "Poor"), hotspot=False)
        self.assertEqual(first.status, "emitted")
        self.assertEqual(duplicate.status, "suppressed")
        self.assertEqual(duplicate.reason, "duplicate_same_state_same_month")
        self.assertEqual(len(self.logged), 1)

    def test_unchanged_tier_and_hotspot_suppressed_in_later_month(self) -> None:
        self.agent.process_risk_record(record(0, "Good"), hotspot=False)
        next_month = self.agent.process_risk_record(record(1, "Good"), hotspot=False)
        self.assertEqual(next_month.status, "suppressed")
        self.assertEqual(next_month.reason, "unchanged_state")
        self.assertEqual(len(self.logged), 1)

    def test_tier_change_emits_again(self) -> None:
        self.agent.process_risk_record(record(0, "Good"), hotspot=False)
        changed = self.agent.process_risk_record(record(1, "Poor"), hotspot=False)
        self.assertEqual(changed.status, "emitted")
        self.assertIn("alternate source", changed.message or "")

    def test_hotspot_state_change_emits_again(self) -> None:
        self.agent.process_risk_record(record(0, "Poor"), hotspot=False)
        active = self.agent.process_risk_record(record(1, "Poor"), hotspot=True)
        self.assertEqual(active.status, "emitted")
        self.assertIn("routine kidney-health screening", (active.message or "").casefold())
        cleared = self.agent.process_risk_record(record(2, "Poor"), hotspot=False)
        self.assertEqual(cleared.status, "emitted")
        self.assertIn("alternate source", cleared.message or "")
        self.assertNotIn("routine kidney-health screening", (cleared.message or "").casefold())
        self.assertEqual(len(self.logged), 3)

    def test_missing_or_invalid_hotspot_is_unavailable_and_not_logged(self) -> None:
        missing = self.agent.process_risk_record(record(0, "Poor"))
        invalid = self.agent.process_risk_record(record(1, "Poor"), hotspot="unknown")  # type: ignore[arg-type]
        self.assertEqual(missing.status, "unavailable")
        self.assertEqual(missing.reason, "hotspot_status_unavailable")
        self.assertEqual(invalid.status, "unavailable")
        self.assertEqual(invalid.reason, "hotspot_status_must_be_bool")
        self.assertEqual(self.agent.suppression_state["state_by_village_month"], {})
        self.assertEqual(self.logged, [])

    def test_unknown_and_unapproved_tiers_are_unavailable_not_safe(self) -> None:
        for tier, reason in (("Unknown", "unknown_tier_policy_unapproved"),
                             ("Very Poor", "tier_policy_unapproved"),
                             ("Unsuitable", "tier_policy_unapproved")):
            with self.subTest(tier=tier):
                decision = self.agent.process_risk_record(record(0, tier), hotspot=False)
                self.assertEqual(decision.status, "unavailable")
                self.assertEqual(decision.reason, reason)
        self.assertEqual(self.logged, [])
        self.assertEqual(self.agent.suppression_state["state_by_village_month"], {})

    def test_suppression_is_not_logged_as_a_message(self) -> None:
        self.agent.process_risk_record(record(0, "Good"), hotspot=False)
        suppressed = self.agent.process_risk_record(record(1, "Good"), hotspot=False)
        self.assertEqual(suppressed.status, "suppressed")
        self.assertEqual(len(self.logged), 1)
        self.assertEqual(self.logged[0][-1], "sent")

    def test_receive_timeout_is_applied(self) -> None:
        async def never_returns() -> object:
            await asyncio.Event().wait()
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(self.agent.receive_once(never_returns, timeout_s=0.01))

    def test_nonpositive_receive_timeout_is_rejected(self) -> None:
        async def receive() -> str:
            return "unused"
        with self.assertRaisesRegex(ValueError, "positive"):
            asyncio.run(self.agent.receive_once(receive, timeout_s=0))


if __name__ == "__main__":
    unittest.main()
