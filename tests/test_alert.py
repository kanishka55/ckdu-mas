"""Alert behavior tests use deterministic synthetic fixtures, not observations."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from src.agents.alert import AlertAgent
from src.core.ontology import MESSAGE_PERFORMATIVES, validate_message


# Explicitly synthetic test fixture: values are contract-shaped only and do
# not represent measured water quality or a calculated WQI.
SYNTHETIC_SAMPLE_RISK_RECORD: dict[str, Any] = {
    "ds_division": "Synthetic Test Division",
    "month": 1,
    "wqi": 75.0,
    "tier": "Good",
    "drivers": {"synthetic_fixture": 0.0},
    "n_wells": 1,
    "source": "synthetic",
}


def record(month: int, tier: str) -> dict[str, Any]:
    data = dict(SYNTHETIC_SAMPLE_RISK_RECORD)
    data.update(month=month, tier=tier)
    return data


class TestAlertAgent(unittest.TestCase):
    def setUp(self) -> None:
        self.logged: list[tuple[Any, ...]] = []
        self.agent = AlertAgent(logger=lambda *args: self.logged.append(args))

    def test_good_and_excellent_are_informational(self) -> None:
        for tier in ("Good", "Excellent"):
            with self.subTest(tier=tier):
                result = self.agent.process_risk_record(record(1, tier))
                self.assertEqual(result.status, "emitted")
                self.assertIn("within published guideline ranges", result.text or "")
                self.assertIn("Risk indicator derived from published water-quality thresholds.", result.text or "")

    def test_poor_tier_advises_alternate_source_without_diagnosis_or_causal_claim(self) -> None:
        result = self.agent.process_risk_record(record(1, "Poor"))
        self.assertIn("use an alternate source", result.text or "")
        self.assertIn("for drinking and cooking", result.text or "")
        prohibited = ("unsafe", "contaminated", "causes CKDu", "you may have CKDu", "diagnosis")
        self.assertFalse(any(phrase.casefold() in (result.text or "").casefold() for phrase in prohibited))

    def test_poor_hotspot_adds_routine_screening_guidance(self) -> None:
        result = self.agent.process_risk_record(record(1, "Poor"), hotspot=True)
        self.assertIn("routine kidney-health screening", (result.text or "").casefold())
        self.assertIn("alternate source", result.text or "")

    def test_duplicate_same_village_month_and_tier_is_suppressed(self) -> None:
        first = self.agent.process_risk_record(record(1, "Poor"))
        duplicate = self.agent.process_risk_record(record(1, "Poor"))
        self.assertEqual(first.status, "emitted")
        self.assertEqual(duplicate.status, "suppressed")
        self.assertEqual(duplicate.reason, "duplicate_same_state_same_month")
        self.assertEqual(
            self.agent.suppression_state["state_by_village_month"][("Synthetic Test Division", 1)],
            ("Poor", False),
        )

    def test_unchanged_tier_is_suppressed_in_later_month(self) -> None:
        first = self.agent.process_risk_record(record(1, "Good"))
        next_month = self.agent.process_risk_record(record(2, "Good"))
        self.assertEqual(first.status, "emitted")
        self.assertEqual(next_month.status, "suppressed")
        self.assertEqual(next_month.reason, "unchanged_state")

    def test_tier_change_emits_again(self) -> None:
        first = self.agent.process_risk_record(record(1, "Good"))
        changed = self.agent.process_risk_record(record(2, "Poor"))
        self.assertEqual(first.status, "emitted")
        self.assertEqual(changed.status, "emitted")
        self.assertIn("alternate source", changed.text or "")

    def test_emission_and_suppression_are_distinguishable_in_log_rows(self) -> None:
        self.agent.process_risk_record(record(1, "Good"))
        self.agent.process_risk_record(record(2, "Good"))
        self.assertEqual(len(self.logged), 2)
        issued, suppressed = self.logged
        self.assertEqual(MESSAGE_PERFORMATIVES["alert"], "inform")
        self.assertEqual(issued[1], "Synthetic Test Division")
        self.assertEqual(issued[2], "inform")
        self.assertEqual(issued[4]["event_type"], "alert_issued")
        self.assertEqual(suppressed[1], "AlertAgent")
        self.assertEqual(suppressed[2], "")
        self.assertEqual(suppressed[4]["event_type"], "alert_suppressed")
        self.assertNotIn("text", suppressed[4])
        validate_message("alert", issued[4])
        validate_message("alert_suppression", suppressed[4])

    def test_newly_active_hotspot_emits_at_unchanged_poor_tier(self) -> None:
        first = self.agent.process_risk_record(record(1, "Poor"), hotspot=False)
        active = self.agent.process_risk_record(record(2, "Poor"), hotspot=True)
        self.assertEqual(first.status, "emitted")
        self.assertEqual(active.status, "emitted")
        self.assertIn("routine kidney-health screening", (active.text or "").casefold())

    def test_cleared_hotspot_updates_poor_alert_without_withdrawing_base_guidance(self) -> None:
        first = self.agent.process_risk_record(record(1, "Poor"), hotspot=True)
        cleared = self.agent.process_risk_record(record(2, "Poor"), hotspot=False)
        self.assertEqual(first.status, "emitted")
        self.assertEqual(cleared.status, "emitted")
        self.assertIn("alternate source", cleared.text or "")
        self.assertNotIn("routine kidney-health screening", (cleared.text or "").casefold())
        self.assertIn("routine kidney-health screening", (first.text or "").casefold())
        self.assertEqual(cleared.tier, "Poor")

    def test_alert_payload_schema_rejects_missing_extra_and_invalid_fields(self) -> None:
        valid = self.agent.process_risk_record(record(1, "Poor"), hotspot=True).alert_payload()
        validate_message("alert", valid)

        missing = dict(valid)
        del missing["text"]
        with self.assertRaisesRegex(ValueError, "missing fields"):
            validate_message("alert", missing)

        unexpected = dict(valid, invented=True)
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            validate_message("alert", unexpected)

        invalid_tier = dict(valid, tier="Unpublished")
        with self.assertRaisesRegex(ValueError, "tier.*one of"):
            validate_message("alert", invalid_tier)

        invalid_bool = dict(valid, hotspot="yes")
        with self.assertRaisesRegex(ValueError, "hotspot.*bool"):
            validate_message("alert", invalid_bool)

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
