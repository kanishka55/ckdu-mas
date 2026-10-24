"""Unit and opt-in embedded-SPADE tests for live alert processing.

All payloads are illustrative synthetic protocol fixtures, not observations. The embedded
recipient is a local transport sink, not a real village-health-officer service.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from src.agents.alert_spade import AlertInboxAgent, AlertProcessingAgent
from src.agents.hotspot import HotspotAgent
from src.core.hotspot_screening import ScreeningRule


def risk(tier="Poor", month=0, division="Demo"):
    return {"type": "risk_record", "month_index": month, "ds_division": division,
            "wqi": 125.0, "tier": tier, "drivers": ["illustrative_fixture"]}


def hotspot(status="active", month=0, division="Demo"):
    return {"type": "hotspot_result", "month_index": month, "ds_division": division,
            "status": status, "rule_id": "illustrative-rule",
            "evidence_month_indexes": [month], "reason": None}


class AlertProcessingAgentUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.agent = AlertProcessingAgent(
            "alert@localhost", "unused", divisions=["Demo"], recipient_jid="inbox@localhost",
            risk_sender_jid="risk@localhost", hotspot_sender_jid="hotspot@localhost",
            output_path=Path(self.temp.name) / "alerts.json",
        )
        async def capture(body, thread):
            self.sent.append((body, thread))
        self.sent = []
        self.agent._send_alert = capture

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_current_month_correlated_evidence_issues_typed_alert(self):
        await self.agent.accept_evidence(risk(), "readings-0", "risk@localhost")
        await self.agent.accept_evidence(hotspot(), "readings-0", "hotspot@localhost")
        self.assertEqual(self.agent.records[0]["status"], "emitted")
        self.assertEqual(self.agent.records[0]["delivery_status"], "sent")
        self.assertEqual(self.agent.records[0]["delivery_events"],
                         [{"state": "requested"}, {"state": "sent"}])
        self.assertEqual(self.sent[0][0]["type"], "alert")

    async def test_unavailable_hotspot_and_unknown_tier_never_issue_alert(self):
        await self.agent.accept_evidence(risk(), "readings-0")
        await self.agent.accept_evidence(hotspot("unavailable"), "readings-0")
        self.assertEqual(self.agent.records[0]["status"], "unavailable")
        self.assertEqual(self.agent.records[0]["hotspot"], None)
        self.agent = AlertProcessingAgent(
            "alert@localhost", "unused", divisions=["Demo"], recipient_jid="inbox@localhost",
            risk_sender_jid="risk@localhost", hotspot_sender_jid="hotspot@localhost",
            output_path=Path(self.temp.name) / "unknown.json")
        await self.agent.accept_evidence(risk("Unknown", month=1), "readings-1")
        await self.agent.accept_evidence(hotspot("inactive", month=1), "readings-1")
        self.assertEqual(self.agent.records[0]["status"], "unavailable")
        self.assertEqual(self.agent.records[0]["reason"], "unknown_tier_policy_unapproved")
        self.assertEqual(self.sent, [])

    async def test_duplicate_identical_state_is_suppressed_by_existing_policy(self):
        for month in (0, 1):
            await self.agent.accept_evidence(risk(month=month), f"readings-{month}")
            await self.agent.accept_evidence(hotspot(month=month), f"readings-{month}")
        self.assertEqual([row["status"] for row in self.agent.records], ["emitted", "suppressed"])
        self.assertEqual(len(self.sent), 1)

    async def test_different_threads_do_not_pair_and_finalize_unavailable(self):
        await self.agent.accept_evidence(risk(), "risk-thread")
        await self.agent.accept_evidence(hotspot(), "hotspot-thread")
        self.agent.finalize_month(0)
        self.assertEqual(len(self.agent.records), 1)
        self.assertEqual(self.agent.records[0]["status"], "unavailable")
        self.assertIn("unmatched_or_missing", self.agent.records[0]["reason"])

    async def test_missing_thread_and_mismatched_month_do_not_pair(self):
        await self.agent.accept_evidence(risk(), None)
        await self.agent.accept_evidence(hotspot(), "readings-0")
        self.assertEqual(self.agent.records[0]["reason"], "missing_correlation_thread")
        self.agent.finalize_month(0)
        self.assertEqual(self.agent.records[-1]["status"], "unavailable")

        other = AlertProcessingAgent(
            "alert@localhost", "unused", divisions=["Demo"], recipient_jid="inbox@localhost",
            risk_sender_jid="risk@localhost", hotspot_sender_jid="hotspot@localhost",
            output_path=Path(self.temp.name) / "mismatched-month.json",
        )
        await other.accept_evidence(risk(month=0), "readings-0")
        await other.accept_evidence(hotspot(month=1, division="Demo"), "readings-0")
        other.finalize_month(0)
        self.assertEqual(other.records[0]["status"], "unavailable")
        self.assertEqual(other.records[0]["reason"], "risk_hotspot_evidence_unmatched_or_missing")
        self.assertEqual(self.sent, [])

    async def test_ack_requires_expected_sender_thread_and_body(self):
        await self.agent.accept_evidence(risk(), "readings-0")
        await self.agent.accept_evidence(hotspot(), "readings-0")
        thread = self.agent.records[0]["alert_thread_id"]
        body = {"type": "alert_ack", "month_index": 0, "ds_division": "Demo"}
        self.assertFalse(self.agent.accept_ack(body, "intruder@localhost", thread))
        self.assertFalse(self.agent.accept_ack(body, "inbox@localhost", "wrong-thread"))
        self.assertTrue(self.agent.accept_ack(body, "inbox@localhost", thread))
        self.assertEqual(self.agent.records[0]["delivery_status"], "confirmed")
        self.assertEqual([item["state"] for item in self.agent.records[0]["delivery_events"]],
                         ["requested", "sent", "confirmed"])

    async def test_failed_send_is_recorded_and_does_not_suppress_a_later_retry(self):
        async def fail_send(body, thread):
            raise OSError("illustrative transport failure")
        self.agent._send_alert = fail_send
        await self.agent.accept_evidence(risk(month=0), "readings-0")
        await self.agent.accept_evidence(hotspot(month=0), "readings-0")
        self.assertEqual(self.agent.records[0]["delivery_status"], "failed")
        self.assertEqual(self.agent.records[0]["delivery_events"][-1]["state"], "failed")
        async def capture(body, thread):
            self.sent.append((body, thread))
        self.agent._send_alert = capture
        await self.agent.accept_evidence(risk(month=1), "readings-1")
        await self.agent.accept_evidence(hotspot(month=1), "readings-1")
        self.assertEqual(self.agent.records[1]["status"], "emitted")

    async def test_confirmation_timeout_is_a_failed_delivery_state(self):
        await self.agent.accept_evidence(risk(), "readings-0")
        await self.agent.accept_evidence(hotspot(), "readings-0")
        thread = self.agent.records[0]["alert_thread_id"]
        self.agent.pending_ack[thread] = (0, "Demo", 0, 0.0)
        self.agent.expire_confirmations(now=1.0)
        self.assertEqual(self.agent.records[0]["delivery_status"], "failed")
        self.assertEqual(self.agent.records[0]["delivery_reason"], "confirmation_timeout")

    async def test_output_serializes_transport_and_policy_states(self):
        await self.agent.accept_evidence(risk(), "readings-0")
        await self.agent.accept_evidence(hotspot(), "readings-0")
        self.agent.write_output()
        saved = json.loads(self.agent.output_path.read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["provenance"], "synthetic_spade_simulation_local_inbox")
        self.assertEqual(saved[0]["delivery_status"], "sent")


@unittest.skipUnless(os.environ.get("CKDU_RUN_SPADE_ALERT") == "1",
                     "set CKDU_RUN_SPADE_ALERT=1 for embedded-SPADE alert transport")
class EmbeddedAlertFlowTests(unittest.TestCase):
    def test_typed_alert_reaches_local_inbox_and_is_acknowledged(self):
        import spade
        from spade.agent import Agent
        from spade.behaviour import OneShotBehaviour
        from src.agents import common
        from src.core import ontology

        with tempfile.TemporaryDirectory() as temp:
            processor = AlertProcessingAgent(
                "alert@localhost", "demo", divisions=["Demo"], recipient_jid="inbox@localhost",
                risk_sender_jid="risk@localhost", hotspot_sender_jid="hotspot@localhost",
                output_path=Path(temp) / "alerts.json", receive_timeout_s=0.05,
                acknowledgement_timeout_s=3.0)
            inbox = AlertInboxAgent("inbox@localhost", "demo", allowed_sender="alert@localhost",
                                    receive_timeout_s=0.05)
            hotspot_agent = HotspotAgent(
                "hotspot@localhost", "demo", divisions=["Demo"],
                rule=ScreeningRule("embedded-alert-test-rule", frozenset({"Poor"}), 1),
                output_path=Path(temp) / "hotspots.json", result_recipient_jid="alert@localhost",
                receive_timeout_s=0.05)
            risk_body = risk()
            class EvidencePublisher(Agent):
                class Publish(OneShotBehaviour):
                    async def run(self):
                        await common.send(self, "alert@localhost", ontology.INFORM,
                                          risk_body, "readings-0")
                        await common.send(self, "hotspot@localhost", ontology.INFORM,
                                          risk_body, "readings-0")
                async def setup(self):
                    self.add_behaviour(self.Publish())

            publisher = EvidencePublisher("risk@localhost", "demo")
            log_path = Path(temp) / "messages.csv"
            for agent in (processor, inbox, hotspot_agent, publisher):
                agent.message_log_path = log_path
            started = []

            async def scenario():
                try:
                    for agent in (inbox, processor, hotspot_agent, publisher):
                        await agent.start(auto_register=True)
                        started.append(agent)
                    deadline = asyncio.get_running_loop().time() + 5
                    while ("Demo", 0) not in hotspot_agent.results_by_key:
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("HotspotAgent did not receive synthetic risk evidence")
                        await asyncio.sleep(0.02)
                    await hotspot_agent.publish_month_results(0)
                    deadline = asyncio.get_running_loop().time() + 10
                    while processor.records[0]["delivery_status"] != "confirmed":
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("alert did not reach and receive acknowledgement from local inbox")
                        await asyncio.sleep(0.02)
                    hotspot_agent.finalize_month(0)
                finally:
                    for agent in reversed(started):
                        await agent.stop()

            spade.run(scenario(), embedded_xmpp_server=True)
            self.assertEqual(len(inbox.received_alerts), 1)
            self.assertEqual(inbox.received_alerts[0]["body"]["type"], "alert")
            self.assertEqual(processor.records[0]["delivery_status"], "confirmed")
            self.assertEqual(processor.records[0]["hotspot_status"], "active")
            self.assertEqual(processor.records[0]["delivery_events"],
                             [{"state": "requested"}, {"state": "sent"},
                              {"state": "confirmed", "recipient": "inbox@localhost"}])
            # Persist after spade.run returns so container teardown cannot interrupt
            # synchronous output writes at the end of the embedded event loop.
            hotspot_agent.write_output()
            processor.write_output()
            self.assertTrue((Path(temp) / "alerts.json").is_file())
            self.assertTrue((Path(temp) / "hotspots.json").is_file())
