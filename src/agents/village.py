"""VillageAgent: one per DS division. Holds only its own data, bids on CFPs.

No agent sees the full picture: a village knows its own wells, population, depot distance and
current risk, nothing about the other villages. Bid logic lives in src/core/bidding.py.
It bids only when its tier is Poor or worse (``bidding.has_need``); otherwise it refuses.
``distance_to_depot_km`` is kept as village data but is not part of the bid (delivery cost is the
coordinator's concern).

Two behaviours, split by message template so neither steals the other's messages:
- RespondToCfp: cfp / accept-proposal / reject-proposal (the Contract Net conversation)
- TrackRisk: inform risk_record from RiskScoringAgent, updating the village's current risk
"""
from __future__ import annotations

import pandas as pd
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.template import Template

from src.agents import common
from src.core import bidding, ontology, wells

CFP_WAIT_S = 30.0        # how long to wait for the next CFP before checking again
RISK_WAIT_S = 30.0       # how long to wait for the next risk record before checking again
DECISION_MARGIN_S = 5.0  # extra wait for accept/reject beyond the CFP deadline


def _performative(name: str) -> Template:
    return Template(metadata={"performative": name})


class VillageAgent(Agent):
    def __init__(
        self,
        jid: str,
        password: str,
        ds_division: str,
        population: int,
        distance_to_depot_km: float,
        local_wells: pd.DataFrame | None = None,
        **kwargs,
    ):
        super().__init__(jid, password, **kwargs)
        self.ds_division = ds_division
        self.population = population
        self.distance_to_depot_km = distance_to_depot_km
        self.local_wells = self._own_wells_only(local_wells)
        self.risk: float | None = None
        self.risk_tier: str | None = None
        self.risk_month_index: int | None = None
        self.allocations: list[dict] = []

    def _own_wells_only(self, df: pd.DataFrame | None) -> pd.DataFrame | None:
        if df is None:
            return None
        df = wells.validate_readings(df, allow_mixed=True)
        others = set(df["ds_division"]) - {self.ds_division}
        if others:
            raise ValueError(f"{self.ds_division} was given wells from {sorted(others)}")
        return df

    def set_risk(self, risk: float | None, tier: str, month_index: int | None = None) -> None:
        """Current risk from a RiskScoringAgent record (or a stub until Member B's agent exists).

        Provisional until Member B's risk record is frozen: ``risk`` is the village WQI, so a
        higher WQI means more need. ``None`` means the WQI could not be computed this month.
        """
        self.risk, self.risk_tier, self.risk_month_index = risk, tier, month_index

    def raw_bid(self) -> float:
        return bidding.bid(self.risk, self.population)

    class TrackRisk(CyclicBehaviour):
        async def run(self):
            got = await common.receive(self, timeout=RISK_WAIT_S)
            if got is None:
                return
            _, _, body = got
            agent: VillageAgent = self.agent
            if body["type"] != "risk_record" or body["ds_division"] != agent.ds_division:
                return
            agent.set_risk(body["wqi"], body["tier"], body["month_index"])

    class RespondToCfp(CyclicBehaviour):
        async def run(self):
            got = await common.receive(self, timeout=CFP_WAIT_S)
            if got is None:
                return
            msg, performative, body = got
            if performative != ontology.CFP:
                return  # stray accept/reject from an earlier round; already logged
            await self.handle_cfp(msg, body)

        async def handle_cfp(self, msg, cfp):
            agent: VillageAgent = self.agent
            coordinator = str(msg.sender)
            base = {"month_index": cfp["month_index"], "round": cfp["round"],
                    "ds_division": agent.ds_division}

            async def refuse(reason: str):
                await common.send(self, coordinator, ontology.REFUSE,
                                  {"type": "refuse", **base, "reason": reason}, msg.thread)

            if agent.risk is None:
                await refuse("no risk indicator available this month")
                return
            if not bidding.has_need(agent.risk_tier):
                await refuse(f"no need this month (tier {agent.risk_tier})")
                return
            try:
                raw = agent.raw_bid()
            except ValueError as exc:
                await refuse(f"cannot compute bid: {exc}")
                return
            if raw <= 0:
                await refuse("no need this month")
                return

            # The coordinator normalises across villages; a village only knows its own raw score.
            await common.send(self, coordinator, ontology.PROPOSE, {
                "type": "propose", **base, "bid": raw,
                "risk_tier": agent.risk_tier or "unknown", "population": int(agent.population),
            }, msg.thread)

            got = await common.receive(self, timeout=cfp["deadline_s"] + DECISION_MARGIN_S)
            if got is None:
                print(f"[{agent.ds_division}] no decision before timeout; giving up this round")
                return
            reply, performative, decision = got
            if performative == ontology.CFP:
                await self.handle_cfp(reply, decision)  # a new round started; answer it
            elif performative == ontology.ACCEPT_PROPOSAL:
                agent.allocations.append(decision)
                await common.send(self, coordinator, ontology.INFORM, {
                    "type": "inform_done", **base,
                    "resource": decision["resource"], "units": decision["units"],
                }, reply.thread)

    async def setup(self):
        cfp_conversation = (_performative(ontology.CFP)
                            | _performative(ontology.ACCEPT_PROPOSAL)
                            | _performative(ontology.REJECT_PROPOSAL))
        self.add_behaviour(self.RespondToCfp(), cfp_conversation)
        self.add_behaviour(self.TrackRisk(), _performative(ontology.INFORM))
