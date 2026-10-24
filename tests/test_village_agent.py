"""Focused VillageAgent CFP tests; framework imports are stubbed when unavailable.

These tests exercise the decision method only. They do not verify SPADE scheduling or XMPP.
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


def _install_minimal_import_stubs() -> None:
    """Allow importing the agent module in environments without SPADE/pandas."""
    if importlib.util.find_spec("pandas") is None:
        sys.modules.setdefault("pandas", types.ModuleType("pandas"))
    if importlib.util.find_spec("spade") is not None:
        return

    spade = types.ModuleType("spade")
    spade.__path__ = []
    agent_module = types.ModuleType("spade.agent")
    behaviour_module = types.ModuleType("spade.behaviour")
    template_module = types.ModuleType("spade.template")
    message_module = types.ModuleType("spade.message")

    class Agent:
        pass

    class CyclicBehaviour:
        pass

    class OneShotBehaviour:
        pass

    class Template:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Message:
        pass

    agent_module.Agent = Agent
    behaviour_module.CyclicBehaviour = CyclicBehaviour
    behaviour_module.OneShotBehaviour = OneShotBehaviour
    template_module.Template = Template
    message_module.Message = Message
    sys.modules.update({
        "spade": spade,
        "spade.agent": agent_module,
        "spade.behaviour": behaviour_module,
        "spade.template": template_module,
        "spade.message": message_module,
    })


_install_minimal_import_stubs()

from src.agents import village  # noqa: E402
from src.core import ontology  # noqa: E402


class VillageCfpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(village.VillageAgent)
        self.agent.ds_division = "Fixture Division"
        self.agent.population = 100
        self.agent.risk = 120.0
        self.agent.risk_tier = "Poor"
        self.agent.risk_month_index = 4
        self.behaviour = village.VillageAgent.RespondToCfp()
        self.behaviour.agent = self.agent
        self.msg = types.SimpleNamespace(sender="coordinator@example", thread="cfp-4")
        self.cfp = {
            "type": "cfp", "month_index": 4, "round": 1,
            "resource": "RO_unit", "units_available": 1, "deadline_s": 1.0,
        }
        self.send = AsyncMock()
        self.receive = AsyncMock(return_value=None)

    async def _handle(self, *, month_index: int = 4) -> None:
        self.cfp["month_index"] = month_index
        with patch.object(village.common, "send", self.send), \
                patch.object(village.common, "receive", self.receive):
            await self.behaviour.handle_cfp(self.msg, self.cfp)

    def _last_body(self) -> tuple[str, dict]:
        args = self.send.call_args.args
        return args[2], args[3]

    async def test_current_month_risk_keeps_existing_bid_path(self) -> None:
        await self._handle()
        performative, body = self._last_body()
        self.assertEqual(performative, ontology.PROPOSE)
        ontology.validate(performative, body)
        self.assertEqual(body["bid"], 12000.0)
        self.receive.assert_awaited_once()

    async def test_previous_month_risk_is_refused(self) -> None:
        self.agent.risk_month_index = 3
        await self._handle()
        performative, body = self._last_body()
        self.assertEqual(performative, ontology.REFUSE)
        ontology.validate(performative, body)
        self.assertIn("CFP month", body["reason"])
        self.receive.assert_not_awaited()

    async def test_missing_risk_is_refused(self) -> None:
        self.agent.risk = None
        self.agent.risk_tier = None
        self.agent.risk_month_index = None
        await self._handle()
        performative, body = self._last_body()
        self.assertEqual(performative, ontology.REFUSE)
        ontology.validate(performative, body)
        self.receive.assert_not_awaited()

    async def test_unknown_and_malformed_risk_cannot_produce_a_bid(self) -> None:
        cases = ((120.0, "Unknown", "Unknown"),
                 (float("nan"), "Poor", "invalid"),
                 (120.0, "unrecognized-tier", "invalid"))
        for risk, tier, expected in cases:
            with self.subTest(risk=risk, tier=tier):
                self.send.reset_mock()
                self.receive.reset_mock()
                self.receive.return_value = None
                self.agent.risk = risk
                self.agent.risk_tier = tier
                await self._handle()
                performative, body = self._last_body()
                self.assertEqual(performative, ontology.REFUSE)
                ontology.validate(performative, body)
                if expected == "Unknown":
                    self.assertIn("Unknown", body["reason"])
                else:
                    self.assertIn("invalid", body["reason"])
                self.receive.assert_not_awaited()

    def test_ranking_uses_wqi_times_population_and_ignores_depot_distance(self) -> None:
        from src.core import bidding, contract_net

        villages = {}
        for name, risk, population, distance in (
            ("Remote high-need", 150.0, 200, 190.0),
            ("Near lower-need", 110.0, 200, 5.0),
            ("Tie B", 100.0, 100, 3.0),
            ("Tie A", 100.0, 100, 220.0),
        ):
            agent = object.__new__(village.VillageAgent)
            agent.ds_division = name
            agent.population = population
            agent.distance_to_depot_km = distance
            agent.set_risk(risk, "Poor", month_index=4)
            villages[name] = agent

        raw_bids = {name: agent.raw_bid() for name, agent in villages.items()}
        normalized = bidding.normalise_bids(raw_bids)
        ranked = bidding.rank_bids(normalized)

        self.assertEqual(raw_bids["Remote high-need"], 30000.0)
        self.assertEqual(ranked[0][0], "Remote high-need")
        self.assertEqual(ranked[1][0], "Near lower-need")
        self.assertEqual([name for name, _ in ranked[2:]], ["Tie A", "Tie B"])
        self.assertEqual(normalized["Remote high-need"], 1.0)
        self.assertEqual(normalized["Near lower-need"], 22000.0 / 30000.0)
        proposals = {
            name: {"bid": agent.raw_bid()}
            for name, agent in villages.items()
        }
        self.assertEqual(
            [name for name, _ in contract_net.select_awardees(proposals, 4)],
            ["Remote high-need", "Near lower-need", "Tie A", "Tie B"],
        )

    async def test_demo_risk_configuration_produces_a_proposal_and_award_candidate(self) -> None:
        from src.sim import demo_contract_net

        demo_contract_net.configure_stub_risk(self.agent, "Padaviya")
        self.assertEqual(self.agent.risk_month_index, demo_contract_net.DEMO_MONTH_INDEX)
        self.cfp["month_index"] = demo_contract_net.DEMO_MONTH_INDEX
        with patch.object(village.common, "send", self.send), \
                patch.object(village.common, "receive", self.receive):
            await self.behaviour.handle_cfp(self.msg, self.cfp)

        performative, body = self._last_body()
        self.assertEqual(performative, ontology.PROPOSE)
        ontology.validate(performative, body)
        ranked, winners = demo_contract_net.select_winners({
            body["ds_division"]: ("village@example", body["bid"]),
        })
        self.assertTrue(ranked, "demo risk must produce a valid proposal")
        self.assertTrue(winners, "a valid proposal must produce an award candidate")
        demo_contract_net.require_successful_round({
            "ranked": ranked, "winners": winners, "confirmed": winners,
        })

    def test_demo_rejects_an_empty_proposal_or_award_result(self) -> None:
        from src.sim import demo_contract_net

        with self.assertRaisesRegex(RuntimeError, "no valid proposals"):
            demo_contract_net.require_successful_round(
                {"ranked": [], "winners": [], "confirmed": []}
            )
        with self.assertRaisesRegex(RuntimeError, "no award"):
            demo_contract_net.require_successful_round(
                {"ranked": [("Fixture Division", 1.0)], "winners": [], "confirmed": []}
            )


if __name__ == "__main__":
    unittest.main()
