"""RiskScoringAgent: WQI per well and one risk tier per village (DS division), per month.

Receives ``well_readings`` (inform) from WellMonitorAgent (Function 1), one message per DS
division per month. For each message it sends:
- ``risk_record`` (village level) to every subscriber (Coordinator, Alert, Hotspot) and to that
  division's own VillageAgent only, if ``village_jids`` is given (a village never sees the others);
- ``well_risk`` (per-well detail) to every hotspot subscriber (Function 3, HotspotAgent).
Scoring logic lives in src/core/wqi.py and src/core/risk_tiers.py.

The tier is a risk indicator derived from published water-quality thresholds. It is not a CKDu
diagnosis and not a claim that the water causes CKDu.
"""
from __future__ import annotations

import pandas as pd
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour

from src.agents import common
from src.core import ontology, risk_tiers, wells, wqi

RECEIVE_TIMEOUT_S = 30.0  # wait for the next readings message before looping again


class RiskScoringAgent(Agent):
    def __init__(
        self,
        jid: str,
        password: str,
        subscribers: list[str],
        method: wqi.WqiMethod,
        classes: list[risk_tiers.WqiClass],
        hotspot_subscribers: list[str] | None = None,
        village_jids: dict[str, str] | None = None,
        **kwargs,
    ):
        super().__init__(jid, password, **kwargs)
        self.subscribers = list(subscribers)
        self.hotspot_subscribers = list(hotspot_subscribers or [])
        self.village_jids = dict(village_jids or {})
        self.method = method
        self.classes = classes
        # Full village detail per message (beyond the ontology fields) for the dashboard/evaluation
        self.history: list[dict] = []

    def score(self, body: dict) -> tuple[dict, dict]:
        """Score one well_readings body. Returns (risk_record body, full village detail).

        ``detail["well_risk"]`` is the well_risk message body, or None when nothing was scored.

        Never raises on bad readings: SPADE kills a behaviour on the first uncaught exception,
        which would silence this agent for the rest of the simulation. Bad input instead yields
        a risk_record with wqi=None and tier "Unknown", and the reason is kept in ``error``.
        """
        error = None
        rejected: list[str] = []
        readings = pd.DataFrame()
        month = None
        well_risk = None
        try:
            readings = pd.DataFrame(body["readings"])
            if readings.empty or readings.columns.empty:
                raise ValueError("no readings in message")
            # Row-level: a well with an unusable value is dropped and reported; the others are
            # still scored (one lab typo must not turn the whole village "Unknown", which would
            # make its VillageAgent refuse the CFP). Also turns an all-blank column into float.
            check_cols = list(dict.fromkeys(wells.PARAMS + [c for c, _ in wqi.COLUMNS.values()]))
            valid, rejected = wqi.reject_invalid_rows(readings, check_cols)
            if valid.empty:
                raise ValueError(f"all {len(readings)} wells have unusable values")
            # Message-level: Member A's contract (columns, months, duplicates, real-vs-synthetic
            # guard). Scored on ``valid`` itself: validate_readings drops extra columns such as
            # a future TDS_mg_L.
            wells.validate_readings(valid)
            if valid["month"].nunique() != 1:
                raise ValueError(f"rows span months {sorted(int(m) for m in valid['month'].unique())}; "
                                 "one message must hold one month")
            labels = set(valid["source"])
            if labels != {body["source"]}:
                raise ValueError(f"message source {body['source']!r} but rows labelled {sorted(labels)}")
            if set(valid["ds_division"]) != {body["ds_division"]}:
                raise ValueError(f"rows from other divisions than {body['ds_division']!r}")
            scored = wqi.score_frame(valid, self.method)
            village = risk_tiers.village_risk(scored, self.classes)
            month = int(valid["month"].iloc[0])
            well_risk = risk_tiers.well_risk(body["month_index"], valid, scored, self.classes)
            if village["n_scored"] == 0:
                error = "no WQI parameter present in any well"
        except (ValueError, KeyError, TypeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            village = risk_tiers.village_risk(
                pd.DataFrame(columns=["wqi", "params_used", "weight_covered"]), self.classes)
        if error or rejected:
            print(f"[{self.jid}] {body.get('ds_division')} month {body.get('month_index')}: "
                  f"error={error} rejected={rejected}")
        record = risk_tiers.risk_record(body["month_index"], body["ds_division"], village)
        detail = {"month_index": body["month_index"], "month": month,
                  "ds_division": body["ds_division"], "source": body["source"], **village,
                  "n_wells": len(readings),  # wells received, including rejected ones
                  "n_rejected": len(rejected), "rejected": "; ".join(rejected), "error": error,
                  "well_risk": well_risk}
        return record, detail

    class ScoreReadings(CyclicBehaviour):
        async def run(self):
            got = await common.receive(self, timeout=RECEIVE_TIMEOUT_S)
            if got is None:
                return
            msg, performative, body = got
            if performative != ontology.INFORM or body["type"] != "well_readings":
                return  # not for this agent; already logged by common.receive
            agent: RiskScoringAgent = self.agent
            record, detail = agent.score(body)
            agent.history.append(detail)
            village = agent.village_jids.get(body["ds_division"])
            for to in agent.subscribers + ([village] if village else []):
                await common.send(self, to, ontology.INFORM, record, msg.thread)
            if detail["well_risk"] is not None:
                for to in agent.hotspot_subscribers:
                    await common.send(self, to, ontology.INFORM, detail["well_risk"], msg.thread)

    async def setup(self):
        common.add_observed_behaviour(self, self.ScoreReadings())
