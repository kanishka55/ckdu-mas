"""Toy Contract Net demo: 1 coordinator, 3 VillageAgents, one CFP for one RO unit.

cfp -> propose/refuse -> accept-proposal/reject-proposal -> inform. Every message is validated
against src/core/ontology.py and logged to logs/messages.csv.

Risk values here are STUB placeholders on the WQI scale (Member B's RiskScoringAgent replaces them).
They are not computed scores; they only make two villages bid differently and one refuse
(tier Good = no need).

Run (XMPP server in another terminal: `spade run`):
    python -m src.sim.demo_contract_net
or with SPADE's in-process server:
    python -m src.sim.demo_contract_net --embedded-server
"""
from __future__ import annotations

import argparse
import asyncio
import time
import uuid

import spade
from spade.agent import Agent
from spade.behaviour import OneShotBehaviour

from src.agents import common
from src.agents.village import VillageAgent
from src.core import bidding, ontology
from src.sim.load_data import load_ds_divisions

DOMAIN = "localhost"
PASSWORD = "demo"
DEMO_VILLAGES = ["Padaviya", "Medawachchiya", "Mihintale"]
STUB_RISK = {  # STUB placeholder (WQI-scale risk, tier) per village, not real scores
    "Padaviya": (140.0, "Poor"),
    "Medawachchiya": (120.0, "Poor"),
    "Mihintale": (80.0, "Good"),  # tier Good -> no need -> village refuses
}
CFP_DEADLINE_S = 5.0
INFORM_WAIT_S = 5.0
UNITS_AVAILABLE = 1  # scarce: one RO unit for three villages


class ToyCoordinator(Agent):
    def __init__(self, jid, password, villages: list[str], **kwargs):
        super().__init__(jid, password, **kwargs)
        self.villages = villages
        self.result: dict = {}

    class RunRound(OneShotBehaviour):
        async def run(self):
            agent: ToyCoordinator = self.agent
            thread = f"cn-{uuid.uuid4().hex[:8]}"
            cfp = {"type": "cfp", "month_index": 0, "round": 1, "resource": "RO_unit",
                   "units_available": UNITS_AVAILABLE, "deadline_s": CFP_DEADLINE_S}
            for v in agent.villages:
                await common.send(self, v, ontology.CFP, cfp, thread)

            proposals: dict[str, tuple[str, float]] = {}  # ds_division -> (jid, raw bid)
            replied: set[str] = set()
            deadline = time.monotonic() + CFP_DEADLINE_S
            while len(replied) < len(agent.villages):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                got = await common.receive(self, timeout=remaining)
                if got is None:
                    continue
                msg, perf, body = got
                replied.add(str(msg.sender))
                if perf == ontology.PROPOSE:
                    proposals[body["ds_division"]] = (str(msg.sender), body["bid"])

            ranked = bidding.rank_bids(
                bidding.normalise_bids({k: b for k, (_, b) in proposals.items()})
            )
            winners = {name for name, _ in ranked[:UNITS_AVAILABLE]}
            for name, (jid, _) in proposals.items():
                if name in winners:
                    await common.send(self, jid, ontology.ACCEPT_PROPOSAL, {
                        "type": "accept_proposal", "month_index": 0, "round": 1,
                        "resource": "RO_unit", "units": 1}, thread)
                else:
                    await common.send(self, jid, ontology.REJECT_PROPOSAL, {
                        "type": "reject_proposal", "month_index": 0, "round": 1,
                        "reason": "higher-need bid selected for the available unit"}, thread)

            confirmed = []
            deadline = time.monotonic() + INFORM_WAIT_S
            while len(confirmed) < len(winners) and time.monotonic() < deadline:
                got = await common.receive(self, timeout=deadline - time.monotonic())
                if got and got[2]["type"] == "inform_done":
                    confirmed.append(got[2]["ds_division"])
            agent.result = {"ranked": ranked, "winners": sorted(winners),
                            "confirmed": confirmed, "no_reply": sorted(set(agent.villages) - replied)}

    async def setup(self):
        self.add_behaviour(self.RunRound())


async def main() -> None:
    ds = load_ds_divisions().set_index("ds_division")
    villages = []
    for name in DEMO_VILLAGES:
        row = ds.loc[name]
        v = VillageAgent(f"village_{name.lower()}@{DOMAIN}", PASSWORD, ds_division=name,
                         population=int(row["population"]),
                         distance_to_depot_km=float(row["distance_to_depot_km"]))
        v.set_risk(*STUB_RISK[name])
        villages.append(v)
    for v in villages:
        await v.start(auto_register=True)

    coord = ToyCoordinator(f"coordinator@{DOMAIN}", PASSWORD, [str(v.jid) for v in villages])
    await coord.start(auto_register=True)
    behaviour = next(iter(coord.behaviours))
    await asyncio.wait_for(behaviour.join(), timeout=CFP_DEADLINE_S + INFORM_WAIT_S + 15)

    print("ranked (normalised):", coord.result["ranked"])
    print("winners:", coord.result["winners"], "confirmed:", coord.result["confirmed"],
          "no reply:", coord.result["no_reply"])
    await coord.stop()
    for v in villages:
        await v.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedded-server", action="store_true",
                    help="start SPADE's in-process XMPP server instead of using `spade run`")
    spade.run(main(), embedded_xmpp_server=ap.parse_args().embedded_server)
