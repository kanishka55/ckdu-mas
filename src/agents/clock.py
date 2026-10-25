"""SPADE simulation clock publishing the shared, typed ``month_tick`` message.

The runner controls advancement: one tick is published only when the runner asks
for it, which lets downstream month processing finish before the next month.
Calendar months are supplied explicitly; this agent never derives them from an
index.
"""
from __future__ import annotations

import asyncio
from typing import Sequence

from spade.agent import Agent
from spade.behaviour import OneShotBehaviour

from src.agents import common
from src.core import ontology, wells


class ClockAgent(Agent):
    """Publish one ontology-valid month tick at a time to configured recipients."""

    def __init__(self, jid: str, password: str, recipients: Sequence[str], **kwargs):
        super().__init__(jid, password, **kwargs)
        if not recipients or any(not isinstance(item, str) or not item.strip() for item in recipients):
            raise ValueError("ClockAgent requires non-empty recipient JIDs")
        self.recipients = tuple(recipients)
        self.published_month_indexes: list[int] = []

    @staticmethod
    def tick_body(month_index: int, calendar_month: int) -> dict:
        """Build and validate a tick; ``month`` is the explicitly supplied calendar label."""
        if isinstance(month_index, bool) or not isinstance(month_index, int) or month_index < 0:
            raise ValueError("month_index must be a non-negative integer")
        season = wells.season_for_month(calendar_month)
        body = {"type": "month_tick", "month_index": month_index,
                "month": calendar_month, "season": season}
        ontology.validate(ontology.INFORM, body)
        return body

    class PublishTick(OneShotBehaviour):
        def __init__(self, body: dict, thread_id: str):
            super().__init__()
            self.body = body
            self.thread_id = thread_id

        async def run(self):
            agent: ClockAgent = self.agent
            for recipient in agent.recipients:
                await common.send(self, recipient, ontology.INFORM, self.body, self.thread_id)
            agent.published_month_indexes.append(self.body["month_index"])

    async def publish_month(self, month_index: int, calendar_month: int, *, timeout_s: float = 10.0) -> None:
        """Publish a single tick and wait for all sends to complete, with a finite timeout."""
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.published_month_indexes and month_index != self.published_month_indexes[-1] + 1:
            raise ValueError("month_index ticks must advance sequentially")
        body = self.tick_body(month_index, calendar_month)
        behaviour = self.PublishTick(body, f"month-tick-{month_index}")
        common.add_observed_behaviour(self, behaviour)
        await asyncio.wait_for(behaviour.join(), timeout=timeout_s)
