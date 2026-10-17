"""Function 1 -> Function 2 -> (Function 3): real WellMonitorAgent feeding the real RiskScoringAgent.

    clock (STUB) --month_tick--> WellMonitorAgent (F1, Member A)
                 --well_readings--> RiskScoringAgent (F2, Member B)
                 --risk_record--> each division's own VillageAgent (Member A)
                 --risk_record + well_risk--> Hotspot stand-in (F3 not built yet; it only collects)
    then a probe CFP checks that villages now bid on real risk instead of refusing.

STUBS: the clock (Member C's ClockAgent replaces it), the hotspot collector (Function 3 replaces
it) and the probe coordinator (Member C's ResourceCoordinatorAgent replaces it).

Writes logs/risk_scores.csv and logs/well_scores.csv (src/sim/risk_outputs.py): Function 2's output
for Function 3 and the dashboard. Every message is logged to logs/messages.csv.

Run:
    python -m src.sim.demo_f1_f2 --embedded-server            # 3 months from October
    python -m src.sim.demo_f1_f2 --embedded-server --months 12
"""
from __future__ import annotations

import argparse
import asyncio
import time
import uuid

import spade
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from src.agents import common
from src.agents.risk_scoring import RiskScoringAgent
from src.agents.village import VillageAgent
from src.agents.well_monitor import WellMonitorAgent
from src.core import ontology, risk_tiers, wells, wqi
from src.sim import load_data
from src.sim.risk_outputs import RISK_SCORES_PATH, WELL_SCORES_PATH, write_outputs

DOMAIN = "localhost"
PASSWORD = "demo"
START_MONTH = 10        # first simulated month: October, start of the Wet season
TICK_GAP_S = 0.5        # pause between clock ticks
COLLECT_BASE_S = 30.0   # hotspot stand-in waits this long, plus PER_MESSAGE_S per expected message
PER_MESSAGE_S = 0.6
CFP_DEADLINE_S = 5.0


def jid(name: str) -> str:
    return f"{name}@{DOMAIN}"


def village_jid(ds: str) -> str:
    return jid(f"village_{ds.lower()}")


class StubClock(Agent):
    def __init__(self, *args, n_months: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.n_months = n_months

    class Tick(OneShotBehaviour):
        async def run(self):
            for i in range(self.agent.n_months):
                month = wells.month_for_index(i, START_MONTH)
                await common.send(self, jid("well_monitor"), ontology.INFORM, {
                    "type": "month_tick", "month_index": i, "month": month,
                    "season": wells.season_for_month(month)}, f"tick-{i}")
                await asyncio.sleep(TICK_GAP_S)

    async def setup(self):
        self.add_behaviour(self.Tick())


class HotspotStandIn(Agent):
    """Placeholder for Function 3: collects what Function 2 sends it, does no analysis."""

    def __init__(self, *args, expected: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected = expected
        self.records: list[dict] = []
        self.well_risk: list[dict] = []

    class Collect(OneShotBehaviour):
        async def run(self):
            agent: HotspotStandIn = self.agent
            deadline = time.monotonic() + COLLECT_BASE_S + PER_MESSAGE_S * 2 * agent.expected
            while len(agent.records) < agent.expected or len(agent.well_risk) < agent.expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                got = await common.receive(self, timeout=remaining)
                if got is None:
                    continue
                body = got[2]
                if body["type"] == "risk_record":
                    agent.records.append(body)
                elif body["type"] == "well_risk":
                    agent.well_risk.append(body)

    async def setup(self):
        self.add_behaviour(self.Collect())


class ProbeCoordinator(Agent):
    """Sends one CFP to every village and records propose (with bid) or refuse (with reason)."""

    def __init__(self, *args, villages: list[str], month_index: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.villages = villages
        self.month_index = month_index
        self.replies: dict[str, str] = {}

    class Probe(OneShotBehaviour):
        async def run(self):
            agent: ProbeCoordinator = self.agent
            thread = f"cn-{uuid.uuid4().hex[:8]}"
            cfp = {"type": "cfp", "month_index": agent.month_index, "round": 1,
                   "resource": "RO_unit", "units_available": 1, "deadline_s": CFP_DEADLINE_S}
            for ds in agent.villages:
                await common.send(self, village_jid(ds), ontology.CFP, cfp, thread)
            deadline = time.monotonic() + CFP_DEADLINE_S
            while len(agent.replies) < len(agent.villages) and time.monotonic() < deadline:
                got = await common.receive(self, timeout=deadline - time.monotonic())
                if got is None:
                    continue
                _, perf, body = got
                agent.replies[body["ds_division"]] = (
                    f"propose bid={body['bid']:.0f} ({body['risk_tier']})" if perf == ontology.PROPOSE
                    else f"refuse: {body.get('reason', perf)}")

    async def setup(self):
        pass


async def main(n_months: int) -> None:
    divisions = load_data.load_ds_divisions().set_index("ds_division")
    monthly = load_data.load_monthly_readings()
    names = sorted(divisions.index)
    method_frame = load_data.load_wqi_method()

    hotspot = HotspotStandIn(jid("hotspot"), PASSWORD, expected=n_months * len(names))
    scorer = RiskScoringAgent(
        jid("risk_scoring"), PASSWORD,
        subscribers=[str(hotspot.jid)],
        hotspot_subscribers=[str(hotspot.jid)],
        village_jids={ds: village_jid(ds) for ds in names},
        method=wqi.method_from_frame(method_frame),
        classes=risk_tiers.classes_from_frame(method_frame))
    monitor = WellMonitorAgent(jid("well_monitor"), PASSWORD, monthly, names,
                               recipients=[str(scorer.jid)])
    villages = [
        VillageAgent(village_jid(ds), PASSWORD, ds_division=ds,
                     population=int(divisions.loc[ds, "population"]),
                     distance_to_depot_km=float(divisions.loc[ds, "distance_to_depot_km"]),
                     local_wells=monthly[monthly["ds_division"] == ds])
        for ds in names]
    for a in [hotspot, scorer, monitor, *villages]:
        await a.start(auto_register=True)

    clock = StubClock(jid("clock"), PASSWORD, n_months=n_months)
    await clock.start(auto_register=True)
    collect = next(iter(hotspot.behaviours))
    await asyncio.wait_for(collect.join(), timeout=COLLECT_BASE_S + PER_MESSAGE_S * 2 * hotspot.expected + 10)
    await asyncio.sleep(1.0)  # let the last risk_records reach the villages

    probe = ProbeCoordinator(jid("coordinator"), PASSWORD, villages=names, month_index=n_months - 1)
    await probe.start(auto_register=True)
    behaviour = ProbeCoordinator.Probe()
    probe.add_behaviour(behaviour)
    await asyncio.wait_for(behaviour.join(), timeout=CFP_DEADLINE_S + 10)

    for a in [probe, clock, *villages, monitor, scorer, hotspot]:
        await a.stop()

    detail, well_table = write_outputs(scorer.history)
    print(f"F1 WellMonitor ticks handled: {monitor.ticks_handled}")
    print(f"F2 messages scored: {len(scorer.history)} "
          f"(errors: {int(detail['error'].notna().sum())}, rejected wells: {int(detail['n_rejected'].sum())})")
    print(f"F3 stand-in received: {len(hotspot.records)} risk_record, {len(hotspot.well_risk)} well_risk "
          f"(expected {hotspot.expected} each)")
    last = detail[detail["month_index"] == n_months - 1].sort_values("wqi", ascending=False)
    print(f"\nlast month (month_index {n_months - 1}, calendar month {int(last['month'].iloc[0])}):")
    print(last[["ds_division", "wqi", "tier", "drivers", "share_poor_or_worse"]].to_string(index=False))
    print("\nvillage state after F2 (risk = WQI, tier, month_index):")
    for v in villages:
        print(f"  {v.ds_division:18s} {v.risk!s:6} {v.risk_tier!s:9} {v.risk_month_index}")
    print("\nprobe CFP replies:")
    for ds in names:
        print(f"  {ds:18s} {probe.replies.get(ds, 'no reply')}")
    print(f"\nwrote {RISK_SCORES_PATH} ({len(detail)} rows), {WELL_SCORES_PATH} ({len(well_table)} rows)")
    print("Risk indicator from published water-quality thresholds - not a CKDu diagnosis.")

    missing = hotspot.expected - min(len(hotspot.records), len(hotspot.well_risk))
    if missing:
        raise RuntimeError(f"{missing} expected messages did not reach the hotspot stand-in")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedded-server", action="store_true",
                    help="start SPADE's in-process XMPP server instead of using `spade run`")
    ap.add_argument("--months", type=int, default=3, help="number of simulated months (default 3)")
    args = ap.parse_args()
    spade.run(main(args.months), embedded_xmpp_server=args.embedded_server)
