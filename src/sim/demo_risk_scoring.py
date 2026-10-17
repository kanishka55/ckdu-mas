"""RiskScoringAgent demo: stub WellMonitor -> RiskScoringAgent -> stub subscriber.

The feeder sends one ``well_readings`` message per DS division for each simulated month
(synthetic wells, source="synthetic"). RiskScoringAgent replies with one ``risk_record`` per
message. Every message is validated and logged to logs/messages.csv; the full village detail
(share of wells Poor or worse, worst tier, parameters used) goes to logs/risk_scores.csv.

The feeder and subscriber are STUBS standing in for Member A's WellMonitorAgent and for the
Coordinator / Alert / Hotspot agents.

Run (XMPP server in another terminal: `spade run --host localhost --memory`):
    python -m src.sim.demo_risk_scoring
or with SPADE's in-process server:
    python -m src.sim.demo_risk_scoring --embedded-server
All 12 months (feeds the risk map, dashboard/risk_map.py):
    python -m src.sim.demo_risk_scoring --embedded-server --months all
"""
from __future__ import annotations

import argparse
import asyncio
import time
import uuid

import pandas as pd
import spade
from spade.agent import Agent
from spade.behaviour import OneShotBehaviour

from src.agents import common
from src.agents.risk_scoring import RiskScoringAgent
from src.core import ontology, risk_tiers, wqi
from src.sim import load_data
from src.sim.risk_outputs import RISK_SCORES_PATH, WELL_SCORES_PATH, write_outputs

DOMAIN = "localhost"
PASSWORD = "demo"
DEFAULT_MONTHS = [1, 7]  # calendar months: January (Wet) and July (Dry); --months all for 12
COLLECT_TIMEOUT_S = 30.0
PER_RECORD_S = 1.0  # extra wait per expected record (scoring takes ~0.2 s per message)


class StubWellMonitor(Agent):
    """Sends the monthly readings of each DS division to the risk scorer."""

    def __init__(self, jid, password, scorer: str, readings: pd.DataFrame, months: list[int],
                 **kwargs):
        super().__init__(jid, password, **kwargs)
        self.scorer = scorer
        self.readings = readings
        self.months = months

    class SendReadings(OneShotBehaviour):
        async def run(self):
            agent: StubWellMonitor = self.agent
            for month_index, month in enumerate(agent.months):
                month_rows = agent.readings[agent.readings["month"] == month]
                for ds, rows in month_rows.groupby("ds_division", sort=True):
                    body = {"type": "well_readings", "month_index": month_index,
                            "ds_division": ds, "source": "synthetic",
                            "readings": rows.to_dict(orient="records")}
                    await common.send(self, agent.scorer, ontology.INFORM, body,
                                      f"wr-{month_index}-{uuid.uuid4().hex[:6]}")

    async def setup(self):
        self.add_behaviour(self.SendReadings())


class StubSubscriber(Agent):
    """Collects risk_record messages until it has ``expected`` or the timeout passes."""

    def __init__(self, jid, password, expected: int, **kwargs):
        super().__init__(jid, password, **kwargs)
        self.expected = expected
        self.records: list[dict] = []
        self.timeout_s = COLLECT_TIMEOUT_S + PER_RECORD_S * expected

    class Collect(OneShotBehaviour):
        async def run(self):
            agent: StubSubscriber = self.agent
            deadline = time.monotonic() + agent.timeout_s
            while len(agent.records) < agent.expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                got = await common.receive(self, timeout=remaining)
                if got and got[2]["type"] == "risk_record":
                    agent.records.append(got[2])

    async def setup(self):
        self.add_behaviour(self.Collect())


async def main(months: list[int]) -> None:
    method_frame = load_data.load_wqi_method()
    method = wqi.method_from_frame(method_frame)
    classes = risk_tiers.classes_from_frame(method_frame)
    readings = load_data.load_monthly_readings()
    n_divisions = readings["ds_division"].nunique()

    sink = StubSubscriber(f"risk_subscriber@{DOMAIN}", PASSWORD,
                          expected=n_divisions * len(months))
    scorer = RiskScoringAgent(f"risk_scoring@{DOMAIN}", PASSWORD, subscribers=[str(sink.jid)],
                              method=method, classes=classes)
    feeder = StubWellMonitor(f"well_monitor@{DOMAIN}", PASSWORD, scorer=str(scorer.jid),
                             readings=readings, months=months)
    await sink.start(auto_register=True)
    await scorer.start(auto_register=True)
    await feeder.start(auto_register=True)

    collect = next(iter(sink.behaviours))
    await asyncio.wait_for(collect.join(), timeout=sink.timeout_s + 10)

    for agent in (feeder, scorer, sink):
        await agent.stop()

    detail, _ = write_outputs(scorer.history)

    got = pd.DataFrame(sink.records).sort_values(["month_index", "ds_division"])
    print(f"risk_records received: {len(got)} of {sink.expected}")
    print(got[["month_index", "ds_division", "wqi", "tier", "drivers"]].to_string(index=False))
    print(f"\nwrote {RISK_SCORES_PATH} ({len(detail)} rows) and {WELL_SCORES_PATH}; "
          "messages in logs/messages.csv")
    print("Risk indicator from published water-quality thresholds - not a CKDu diagnosis.")
    if len(got) < sink.expected:
        raise RuntimeError(f"only {len(got)} of {sink.expected} risk_records arrived before the timeout")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedded-server", action="store_true",
                    help="start SPADE's in-process XMPP server instead of using `spade run`")
    ap.add_argument("--months", default=",".join(map(str, DEFAULT_MONTHS)),
                    help="calendar months to simulate, e.g. 1,7 (default) or all")
    args = ap.parse_args()
    months = list(range(1, 13)) if args.months == "all" else [int(m) for m in args.months.split(",")]
    spade.run(main(months), embedded_xmpp_server=args.embedded_server)
