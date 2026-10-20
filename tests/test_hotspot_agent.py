"""Unit and opt-in embedded-SPADE tests for the real risk-record subscriber.

Fixtures below are illustrative typed records, not environmental observations.
The embedded case verifies local SPADE delivery, not an external XMPP deployment.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from src.agents.hotspot import HotspotAgent
from src.core.hotspot_screening import ScreeningRule


def risk_record(month_index, tier="Poor", wqi=125.0, ds_division="Demo Division"):
    """Build an illustrative risk_record body for isolated tests."""
    return {"type": "risk_record", "month_index": month_index,
            "ds_division": ds_division, "wqi": wqi, "tier": tier, "drivers": ["F"]}


class HotspotAgentUnitTests(unittest.TestCase):
    def setUp(self):
        self.rule = ScreeningRule("illustrative-test-rule", frozenset({"Poor", "Very Poor"}), 2)
        self.temp = tempfile.TemporaryDirectory()
        self.agent = HotspotAgent(
            "hotspot@localhost", "unused", divisions=["Demo Division"], rule=self.rule,
            output_path=Path(self.temp.name) / "hotspots.json",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_consecutive_valid_months_produce_screening_result(self):
        first = self.agent.process_risk_record(risk_record(0))
        second = self.agent.process_risk_record(risk_record(1, tier="Very Poor", wqi=200.0))
        self.assertEqual(first.status, "unavailable")
        self.assertEqual(second.status, "active")
        self.assertEqual(second.evidence_month_indexes, (0, 1))

    def test_gap_is_unavailable(self):
        self.agent.process_risk_record(risk_record(0))
        result = self.agent.process_risk_record(risk_record(2))
        self.assertEqual(result.status, "unavailable")
        self.assertIn("missing_month_indexes:1", result.reason)

    def test_duplicate_month_is_unavailable(self):
        self.agent.process_risk_record(risk_record(0))
        self.agent.process_risk_record(risk_record(1))
        result = self.agent.process_risk_record(risk_record(1, tier="Good", wqi=80.0))
        self.assertEqual(result.status, "unavailable")
        self.assertIn("duplicate_record", result.reason)

    def test_out_of_order_message_is_rejected(self):
        self.agent.process_risk_record(risk_record(1))
        result = self.agent.process_risk_record(risk_record(0))
        self.assertEqual(result.status, "unavailable")
        self.assertIn("out_of_order", result.reason)
        self.assertEqual(len(self.agent.risk_history["Demo Division"]), 1)

    def test_unknown_wqi_is_unavailable_and_not_inactive(self):
        self.agent.process_risk_record(risk_record(0))
        result = self.agent.process_risk_record(risk_record(1, tier="Unknown", wqi=None))
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.alert_hotspot)

    def test_malformed_record_and_absent_month_are_explicitly_unavailable(self):
        malformed = risk_record(0)
        malformed["unexpected"] = "not in ontology"
        invalid = self.agent.process_risk_record(malformed)
        missing = self.agent.finalize_month(0)[0]
        self.assertEqual(invalid.status, "unavailable")
        self.assertIn("invalid_risk_record", invalid.reason)
        self.assertEqual(missing.status, "unavailable")
        self.assertIn("invalid_risk_record", missing.reason)
        absent = self.agent.finalize_month(1)[0]
        self.assertEqual(absent.status, "unavailable")
        self.assertEqual(absent.reason, "risk_record_missing_for_month")

    def test_output_is_provenanced_and_never_overwritten(self):
        self.agent.process_risk_record(risk_record(0))
        self.agent.finalize_month(0)
        self.agent.write_output()
        payload = json.loads(self.agent.output_path.read_text(encoding="utf-8"))
        self.assertEqual(payload[0]["provenance"], "project_defined_screening_not_validated_model")
        with self.assertRaises(FileExistsError):
            self.agent.write_output()


@unittest.skipUnless(os.environ.get("CKDU_RUN_SPADE_HOTSPOT") == "1",
                     "set CKDU_RUN_SPADE_HOTSPOT=1 for embedded-SPADE delivery")
class EmbeddedHotspotAgentTests(unittest.TestCase):
    def test_risk_scoring_fanout_reaches_hotspot_and_produces_result(self):
        import spade
        from spade.agent import Agent
        from spade.behaviour import OneShotBehaviour

        from src.agents import common
        from src.agents.risk_scoring import RiskScoringAgent
        from src.core import ontology, risk_tiers, wells, wqi
        from src.sim import load_data

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "hotspot.json"
            method_frame = load_data.load_wqi_method()
            method = wqi.method_from_frame(method_frame)
            classes = risk_tiers.classes_from_frame(method_frame)
            monthly = load_data.load_monthly_readings()
            source, rows = wells.readings_for_month(monthly, "Padaviya", 1)
            readings_body = {"type": "well_readings", "month_index": 0,
                             "ds_division": "Padaviya", "source": source, "readings": rows}
            ontology.validate(ontology.INFORM, readings_body)
            # This configured rule exists only to make the transport integration observable.
            rule = ScreeningRule("embedded-test-rule",
                                 frozenset({"Excellent", "Good", "Poor", "Very Poor", "Unsuitable"}), 1)
            hotspot = HotspotAgent("hotspot@localhost", "demo", divisions=["Padaviya"],
                                   rule=rule, output_path=output, receive_timeout_s=0.1)
            scorer = RiskScoringAgent("scorer@localhost", "demo", subscribers=["hotspot@localhost"],
                                      method=method, classes=classes, village_jids={})

            class ReadingsPublisher(Agent):
                class Publish(OneShotBehaviour):
                    async def run(self):
                        await common.send(self, "scorer@localhost", ontology.INFORM,
                                          readings_body, "readings-0")

                async def setup(self):
                    self.add_behaviour(self.Publish())

            publisher = ReadingsPublisher("publisher@localhost", "demo")
            started = []

            async def scenario():
                try:
                    for agent in (hotspot, scorer, publisher):
                        await agent.start(auto_register=True)
                        started.append(agent)
                    deadline = asyncio.get_running_loop().time() + 15.0
                    key = ("Padaviya", 0)
                    while key not in hotspot.results_by_key:
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("HotspotAgent did not receive scorer fan-out")
                        await asyncio.sleep(0.02)
                    result = hotspot.results_by_key[key]
                    self.assertIn(result.status, {"active", "inactive"})
                    self.assertEqual(result.evidence_month_indexes, (0,))
                    hotspot.finalize_month(0)
                    hotspot.write_output()
                finally:
                    for agent in reversed(started):
                        await agent.stop()

            spade.run(scenario(), embedded_xmpp_server=True)
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["ds_division"], "Padaviya")
            self.assertIn(saved[0]["status"], {"active", "inactive"})


if __name__ == "__main__":
    unittest.main()
