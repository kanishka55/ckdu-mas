"""Illustrative unit tests for the configurable project-defined hotspot screen."""

import unittest

from src.core.hotspot_screening import ScreeningRule, screen_risk_history


def risk_record(month_index, tier="Poor", wqi=125.0, ds_division="Demo Division"):
    """Create an illustrative typed record; values are not project observations."""
    return {
        "type": "risk_record",
        "month_index": month_index,
        "ds_division": ds_division,
        "wqi": wqi,
        "tier": tier,
        "drivers": ["F"],
    }


class HotspotScreeningTests(unittest.TestCase):
    def setUp(self):
        # Explicit test-only rule parameters; they are not scientific thresholds.
        self.rule = ScreeningRule("illustrative-persistent-tier-v1", frozenset({"Poor", "Very Poor", "Unsuitable"}), 2)

    def test_two_consecutive_trigger_tiers_are_active(self):
        result = screen_risk_history(
            [risk_record(0), risk_record(1, tier="Very Poor", wqi=250.0)],
            ds_division="Demo Division", month_index=1, rule=self.rule,
        )
        self.assertEqual(result.status, "active")
        self.assertIs(result.alert_hotspot, True)
        self.assertEqual(result.evidence_month_indexes, (0, 1))

    def test_complete_window_without_all_trigger_tiers_is_inactive(self):
        result = screen_risk_history(
            [risk_record(0), risk_record(1, tier="Good", wqi=80.0)],
            ds_division="Demo Division", month_index=1, rule=self.rule,
        )
        self.assertEqual(result.status, "inactive")
        self.assertIs(result.alert_hotspot, False)

    def test_missing_rule_is_unavailable_not_inactive(self):
        result = screen_risk_history(
            [risk_record(0)], ds_division="Demo Division", month_index=0, rule=None,
        )
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.alert_hotspot)
        self.assertEqual(result.reason, "screening_rule_not_configured")

    def test_insufficient_or_gapped_history_is_unavailable(self):
        too_short = screen_risk_history(
            [risk_record(0)], ds_division="Demo Division", month_index=0, rule=self.rule,
        )
        gap = screen_risk_history(
            [risk_record(0), risk_record(2)], ds_division="Demo Division", month_index=2, rule=self.rule,
        )
        self.assertEqual(too_short.status, "unavailable")
        self.assertEqual(gap.status, "unavailable")
        self.assertIsNone(too_short.alert_hotspot)
        self.assertIsNone(gap.alert_hotspot)

    def test_unknown_tier_is_unavailable_and_never_safe(self):
        result = screen_risk_history(
            [risk_record(0), risk_record(1, tier="Unknown", wqi=None)],
            ds_division="Demo Division", month_index=1, rule=self.rule,
        )
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.alert_hotspot)
        self.assertIn("unknown_risk_tier", result.reason)

    def test_duplicate_division_month_is_unavailable(self):
        result = screen_risk_history(
            [risk_record(0), risk_record(0, tier="Good", wqi=80.0), risk_record(1)],
            ds_division="Demo Division", month_index=1, rule=self.rule,
        )
        self.assertEqual(result.status, "unavailable")
        self.assertIn("duplicate_record", result.reason)

    def test_invalid_typed_record_is_unavailable(self):
        bad = risk_record(0)
        bad["unexpected"] = "not in ontology"
        result = screen_risk_history(
            [bad, risk_record(1)], ds_division="Demo Division", month_index=1, rule=self.rule,
        )
        self.assertEqual(result.status, "unavailable")
        self.assertIn("invalid_risk_record", result.reason)

    def test_rule_cannot_treat_unknown_as_a_trigger(self):
        with self.assertRaises(ValueError):
            ScreeningRule("bad-rule", frozenset({"Unknown"}), 1)

    def test_invalid_rule_configuration_is_rejected(self):
        invalid = (
            ("", frozenset({"Poor"}), 1),
            ("empty-tiers", frozenset(), 1),
            ("zero-window", frozenset({"Poor"}), 0),
            ("unknown-tier", frozenset({"Unknown"}), 1),
        )
        for rule_id, tiers, months in invalid:
            with self.subTest(rule_id=rule_id, tiers=tiers, months=months):
                with self.assertRaises(ValueError):
                    ScreeningRule(rule_id, tiers, months)

    def test_other_divisions_do_not_supply_target_evidence(self):
        result = screen_risk_history(
            [risk_record(0, ds_division="Other"), risk_record(1)],
            ds_division="Demo Division", month_index=1, rule=self.rule,
        )
        self.assertEqual(result.status, "unavailable")
        self.assertIn("missing_month_indexes:0", result.reason)


if __name__ == "__main__":
    unittest.main()
