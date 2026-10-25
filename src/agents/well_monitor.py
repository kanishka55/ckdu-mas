"""WellMonitorAgent: on each ClockAgent month tick, emits that month's readings for each village.

One ``well_readings`` message per village per tick, sent to every recipient (RiskScoringAgent).
Selection logic lives in src/core/wells.py.
"""
from __future__ import annotations

import pandas as pd
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.template import Template

from src.agents import common
from src.core import ontology, wells

TICK_WAIT_S = 30.0  # how long to wait for the next month tick before checking again


class WellMonitorAgent(Agent):
    def __init__(
        self,
        jid: str,
        password: str,
        monthly: pd.DataFrame,
        villages: list[str],
        recipients: list[str],
        **kwargs,
    ):
        super().__init__(jid, password, **kwargs)
        self.monthly = wells.validate_readings(monthly, allow_mixed=True)
        unknown = set(villages) - set(self.monthly["ds_division"])
        if unknown:
            raise ValueError(f"no readings for villages {sorted(unknown)}")
        self.villages = list(villages)
        self.recipients = list(recipients)
        self.ticks_handled: list[int] = []

    class OnMonthTick(CyclicBehaviour):
        async def run(self):
            got = await common.receive(self, timeout=TICK_WAIT_S)
            if got is None:
                return
            msg, _, tick = got
            if tick["type"] != "month_tick":
                return
            agent: WellMonitorAgent = self.agent
            if wells.season_for_month(tick["month"]) != tick["season"]:
                print(f"[well_monitor] tick {tick['month_index']} season does not match month; skipped")
                return

            thread = f"readings-{tick['month_index']}"
            for village in agent.villages:
                try:
                    source, rows = wells.readings_for_month(agent.monthly, village, tick["month"])
                except ValueError as exc:
                    print(f"[well_monitor] {village}: {exc}")
                    continue
                body = {"type": "well_readings", "month_index": tick["month_index"],
                        "ds_division": village, "source": source, "readings": rows}
                for to in agent.recipients:
                    await common.send(self, to, ontology.INFORM, body, thread)
            agent.ticks_handled.append(tick["month_index"])

    async def setup(self):
        common.add_observed_behaviour(
            self, self.OnMonthTick(), Template(metadata={"performative": ontology.INFORM}),
        )
