"""Member A agent demo: month ticks -> WellMonitorAgent -> (stand-in risk scorer) -> VillageAgents.

- A STUB clock sends N month ticks (Member C's ClockAgent replaces it).
- WellMonitorAgent sends one well_readings message per village per tick.
- A STUB risk scorer (Member B's RiskScoringAgent replaces it) answers each village with a
  risk_record carrying wqi=None and tier "STUB-unscored". It invents no WQI value.
- A final CFP checks that a village with no risk indicator refuses instead of bidding.

Run:
    python -m src.sim.demo_well_monitor --embedded-server
"""
from __future__ import annotations

import argparse
import asyncio
import uuid

import spade
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from src.agents import common
from src.agents.village import VillageAgent
from src.agents.well_monitor import WellMonitorAgent
from src.core import ontology, wells
from src.sim.load_data import load_ds_divisions, load_monthly_readings

DOMAIN = "localhost"
PASSWORD = "demo"
DEMO_VILLAGES = ["Padaviya", "Medawachchiya", "Mihintale"]
N_TICKS = 3
START_MONTH = 10  # first simulated month is October (start of the Wet season)
RECV_TIMEOUT_S = 5.0


def jid(name: str) -> str:
    return f"{name}@{DOMAIN}"


def village_jid(ds: str) -> str:
    return jid(f"village_{ds.lower()}")


class StubClock(Agent):
    class Tick(OneShotBehaviour):
        async def run(self):
            for i in range(N_TICKS):
                month = wells.month_for_index(i, START_MONTH)
                await common.send(self, jid("well_monitor"), ontology.INFORM, {
                    "type": "month_tick", "month_index": i, "month": month,
                    "season": wells.season_for_month(month)}, f"tick-{i}")
                await asyncio.sleep(0.5)

    async def setup(self):
        self.add_behaviour(self.Tick())


class StubRiskScorer(Agent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.received: list[tuple[int, str, int]] = []  # (month_index, ds_division, n_wells)

    class Collect(CyclicBehaviour):
        async def run(self):
            got = await common.receive(self, timeout=RECV_TIMEOUT_S)
            if got is None:
                return
            msg, _, body = got
            if body["type"] != "well_readings":
                return
            self.agent.received.append((body["month_index"], body["ds_division"], len(body["readings"])))
            await common.send(self, village_jid(body["ds_division"]), ontology.INFORM, {
                "type": "risk_record", "month_index": body["month_index"],
                "ds_division": body["ds_division"], "wqi": None,
                "tier": "STUB-unscored", "drivers": []}, msg.thread)

    async def setup(self):
        self.add_behaviour(self.Collect())


class ProbeCoordinator(Agent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.replies: dict[str, str] = {}

    class Probe(OneShotBehaviour):
        async def run(self):
            thread = f"cn-{uuid.uuid4().hex[:8]}"
            cfp = {"type": "cfp", "month_index": N_TICKS - 1, "round": 1, "resource": "RO_unit",
                   "units_available": 1, "deadline_s": 3.0}
            for ds in DEMO_VILLAGES:
                await common.send(self, village_jid(ds), ontology.CFP, cfp, thread)
            for _ in DEMO_VILLAGES:
                got = await common.receive(self, timeout=cfp["deadline_s"])
                if got is None:
                    break
                self.agent.replies[got[2]["ds_division"]] = got[2].get("reason", got[1])

    async def setup(self):
        pass


async def main() -> None:
    ds = load_ds_divisions().set_index("ds_division")
    monthly = load_monthly_readings()

    scorer = StubRiskScorer(jid("risk_stub"), PASSWORD)
    monitor = WellMonitorAgent(jid("well_monitor"), PASSWORD, monthly, DEMO_VILLAGES,
                               recipients=[str(scorer.jid)])
    villages = [
        VillageAgent(village_jid(name), PASSWORD, ds_division=name,
                     population=int(ds.loc[name, "population"]),
                     distance_to_depot_km=float(ds.loc[name, "distance_to_depot_km"]),
                     local_wells=monthly[monthly["ds_division"] == name])
        for name in DEMO_VILLAGES
    ]
    for a in [scorer, monitor, *villages]:
        await a.start(auto_register=True)

    clock = StubClock(jid("clock_stub"), PASSWORD)
    await clock.start(auto_register=True)
    await asyncio.wait_for(next(iter(clock.behaviours)).join(), timeout=30)
    await asyncio.sleep(1.0)  # let the last readings and risk records arrive

    probe = ProbeCoordinator(jid("coordinator"), PASSWORD)
    await probe.start(auto_register=True)
    behaviour = ProbeCoordinator.Probe()
    probe.add_behaviour(behaviour)
    await asyncio.wait_for(behaviour.join(), timeout=30)

    print("ticks handled by WellMonitor:", monitor.ticks_handled)
    print("readings received (month_index, village, n_wells):", sorted(scorer.received))
    print("village risk state:", {v.ds_division: (v.risk, v.risk_tier, v.risk_month_index) for v in villages})
    print("CFP replies:", probe.replies)

    for a in [probe, clock, *villages, monitor, scorer]:
        await a.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedded-server", action="store_true",
                    help="start SPADE's in-process XMPP server instead of using `spade run`")
    spade.run(main(), embedded_xmpp_server=ap.parse_args().embedded_server)
