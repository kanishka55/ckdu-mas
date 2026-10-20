"""Focused coordinator tests using fake transport packets, not a live XMPP server."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.agents import resource_coordinator
from src.core import contract_net, ontology


class FakeMessage:
    def __init__(self, sender, thread, performative, body):
        self.sender = sender
        self.thread = thread
        self.body = body if isinstance(body, str) else json.dumps(body)
        self.metadata = {"performative": performative}

    def get_metadata(self, key):
        return self.metadata.get(key)


class FakeAgent:
    IDLE = "IDLE"
    CFP_SENT = "CFP_SENT"
    COLLECTING = "COLLECTING"
    AWARDING = "AWARDING"
    CONFIRMING = "CONFIRMING"
    DONE = "DONE"

    def __init__(self, config):
        self.config = config
        self.thread_id = config.correlation_id
        self.jid = "coordinator@localhost"
        self.round_result = None


class FakeBehaviour:
    _receive_raw = resource_coordinator.ResourceCoordinatorAgent.RunRound._receive_raw
    _base_result = resource_coordinator.ResourceCoordinatorAgent.RunRound._base_result
    _transition = resource_coordinator.ResourceCoordinatorAgent.RunRound._transition
    run = resource_coordinator.ResourceCoordinatorAgent.RunRound.run

    def __init__(self, agent, incoming):
        self.agent = agent
        self.incoming = list(incoming)
        self.timeouts = []

    async def receive(self, timeout):
        self.timeouts.append(timeout)
        return self.incoming.pop(0) if self.incoming else None


class ContractNetConfigTests(unittest.TestCase):
    def test_cfp_uses_month_index_and_round_from_configuration(self):
        config = make_config(Path("round.json"), month_index=9, round_number=3)
        body = config.cfp_body()
        ontology.validate(ontology.CFP, body)
        self.assertEqual(body["month_index"], 9)
        self.assertEqual(body["round"], 3)
        self.assertNotIn("month", body)

    def test_invalid_pool_and_timeouts_are_rejected(self):
        with self.assertRaises(ValueError):
            make_config(Path("round.json"), resource_pool=-1)
        with self.assertRaises(ValueError):
            make_config(Path("round.json"), proposal_timeout_s=0)

    def test_valid_proposal_requires_current_month_and_need_tier(self):
        config = make_config(Path("round.json"))
        valid = proposal("Village A", 100.0)
        self.assertEqual(contract_net.valid_proposal(valid, config, "Village A"), (True, None))
        stale = {**valid, "month_index": 1}
        self.assertFalse(contract_net.valid_proposal(stale, config, "Village A")[0])
        unknown = {**valid, "risk_tier": "Unknown"}
        self.assertFalse(contract_net.valid_proposal(unknown, config, "Village A")[0])
        malformed = {key: value for key, value in valid.items() if key != "population"}
        self.assertFalse(contract_net.valid_proposal(malformed, config, "Village A")[0])

    def test_selection_is_deterministic_with_shared_alphabetical_tie_break(self):
        proposals = {
            "Beta": proposal("Beta", 60),
            "Alpha": proposal("Alpha", 60),
            "Gamma": proposal("Gamma", 20),
        }
        self.assertEqual(contract_net.select_awardees(proposals, 2), [("Alpha", 1.0), ("Beta", 1.0)])
        self.assertEqual(contract_net.select_awardees(proposals, 0), [])

    def test_json_output_is_serialized_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round.json"
            result = {"month_index": 0, "allocation_successful": False, "proposals": []}
            contract_net.write_round_result(path, result)
            self.assertEqual(json.loads(path.read_text()), result)
            with self.assertRaises(FileExistsError):
                contract_net.write_round_result(path, {"replaced": True})


class CoordinatorRoundTests(unittest.TestCase):
    def test_deterministic_tie_awards_and_confirmation_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp) / "round.json", pool=1)
            messages = [
                FakeMessage("beta@localhost", "thread-1", ontology.PROPOSE, proposal("Beta", 100)),
                FakeMessage("alpha@localhost", "thread-1", ontology.PROPOSE, proposal("Alpha", 100)),
            ]
            confirmations = [FakeMessage("alpha@localhost", "thread-1", ontology.INFORM, {
                    "type": "inform_done", "month_index": 0, "round": 1,
                    "ds_division": "Alpha", "resource": "RO_unit", "units": 1,
                })]
            result, sent, timeouts = run_round(config, messages, confirmations)
            self.assertEqual(result["winner"], "Alpha")
            self.assertEqual(result["award_status"], "confirmed")
            self.assertTrue(result["allocation_successful"])
            self.assertEqual(result["confirmed_awardees"], ["Alpha"])
            self.assertEqual(result["state_history"], ["IDLE", "CFP_SENT", "COLLECTING", "AWARDING", "CONFIRMING", "DONE"])
            self.assertTrue(all(value > 0 for value in timeouts))
            self.assertEqual(sent["accept-proposal"], ["alpha@localhost"])
            self.assertEqual(sent["reject-proposal"], ["beta@localhost"])

    def test_refusals_are_not_ranked_and_stale_malformed_and_silent_replies_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp) / "round.json", recipients=three_recipients())
            messages = [
                FakeMessage("alpha@localhost", "thread-1", ontology.REFUSE, {
                    "type": "refuse", "month_index": 0, "round": 1,
                    "ds_division": "Alpha", "reason": "no need",
                }),
                FakeMessage("beta@localhost", "thread-1", ontology.PROPOSE,
                            {**proposal("Beta", 200), "month_index": 4}),
                FakeMessage("gamma@localhost", "thread-1", ontology.PROPOSE, "not-json"),
            ]
            result, _, _ = run_round(config, messages)
            self.assertEqual([row["ds_division"] for row in result["refusals"]], ["Alpha"])
            self.assertEqual(result["proposals"], [])
            self.assertIsNone(result["winner"])
            self.assertEqual(result["missing_recipients"], ["beta@localhost"])
            self.assertEqual(result["timed_out_recipients"], ["beta@localhost"])
            self.assertEqual(len(result["unmatched_replies"]), 1)
            self.assertEqual(len(result["malformed_replies"]), 1)
            self.assertEqual(result["ineligible_proposals"], [])
            self.assertEqual(result["award_status"], "no_award")
            self.assertFalse(result["allocation_successful"])

    def test_no_reply_is_recorded_as_missing_and_timed_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp) / "round.json")
            result, _, _ = run_round(config, [])
            self.assertEqual(result["missing_recipients"], ["alpha@localhost", "beta@localhost"])
            self.assertEqual(result["timed_out_recipients"], result["missing_recipients"])
            self.assertEqual(result["award_status"], "no_award")

    def test_failure_and_unconfirmed_awards_are_not_successful_allocations(self):
        for confirm, expected in ((False, "unconfirmed"), (True, "failed")):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                config = make_config(Path(tmp) / "round.json", pool=1)
                messages = [FakeMessage("alpha@localhost", "thread-1", ontology.PROPOSE,
                                        proposal("Alpha", 90))]
                if confirm:
                    confirmations = [FakeMessage("alpha@localhost", "thread-1", ontology.FAILURE, {
                        "type": "failure", "month_index": 0, "round": 1,
                        "ds_division": "Alpha", "reason": "unable to deliver",
                    })]
                else:
                    confirmations = []
                result, _, _ = run_round(config, messages, confirmations)
                self.assertEqual(result["award_status"], expected)
                self.assertFalse(result["allocation_successful"])
                self.assertIsNone(result["winner"])
                self.assertEqual(result["selected_awardees"][0]["status"],
                                 "failed" if confirm else "unconfirmed_timeout")

    def test_unknown_and_stale_proposals_cannot_win_and_rounds_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = make_config(Path(tmp) / "one.json", thread="round-one")
            second = make_config(Path(tmp) / "two.json", month_index=1, round_number=2,
                                 thread="round-two")
            messages = [FakeMessage("alpha@localhost", "round-one", ontology.PROPOSE,
                                    {**proposal("Alpha", 999), "risk_tier": "Unknown"}),
                        FakeMessage("beta@localhost", "round-one", ontology.PROPOSE,
                                    {**proposal("Beta", 120), "month_index": 1})]
            result1, _, _ = run_round(first, messages)
            result2, _, _ = run_round(second, [
                FakeMessage("alpha@localhost", "round-two", ontology.PROPOSE,
                            {**proposal("Alpha", 50), "month_index": 1, "round": 2}),
            ])
            self.assertIsNone(result1["winner"])
            self.assertEqual(len(result1["ineligible_proposals"]), 1)
            self.assertEqual(result1["thread_id"], "round-one")
            self.assertEqual(result2["thread_id"], "round-two")
            self.assertEqual(result2["month_index"], 1)
            self.assertEqual(result2["round"], 2)
            self.assertTrue(result2["allocation_successful"] is False)  # no confirmation in this round


def recipients():
    return [contract_net.VillageRecipient("alpha@localhost", "Alpha"),
            contract_net.VillageRecipient("beta@localhost", "Beta")]


def three_recipients():
    return [*recipients(), contract_net.VillageRecipient("gamma@localhost", "Gamma")]


def make_config(path, *, month_index=0, round_number=1, resource_pool=1, pool=None,
                recipients=None, thread="thread-1", proposal_timeout_s=1,
                confirmation_timeout_s=1):
    return contract_net.CoordinatorConfig(
        month_index=month_index, round_number=round_number, resource="RO_unit",
        resource_pool=resource_pool if pool is None else pool,
        recipients=recipients if recipients is not None else globals()["recipients"](),
        proposal_timeout_s=proposal_timeout_s, confirmation_timeout_s=confirmation_timeout_s,
        output_path=path, thread_id=thread,
    )


def proposal(division, bid):
    return {"type": "propose", "month_index": 0, "round": 1,
            "ds_division": division, "bid": bid, "risk_tier": "Poor", "population": 100}


def run_round(config, incoming, confirmations=()):
    fake_agent = FakeAgent(config)
    behavior = FakeBehaviour(fake_agent, incoming)
    sent = {"accept-proposal": [], "reject-proposal": [], "cfp": []}

    async def fake_send(_behavior, to, performative, body, thread):
        ontology.validate(performative, body)
        if performative in sent:
            sent[performative].append(to)
        assert thread == fake_agent.thread_id
        if performative == ontology.ACCEPT_PROPOSAL:
            behavior.incoming.extend(confirmations)

    async def execute():
        with patch("src.agents.resource_coordinator.common.send", new=fake_send), \
             patch("src.agents.resource_coordinator.msg_log.log_message"):
            await behavior.run()

    import asyncio
    asyncio.run(execute())
    result = fake_agent.round_result
    assert json.loads(config.output_path.read_text()) == result
    return result, sent, behavior.timeouts


if __name__ == "__main__":
    unittest.main()
