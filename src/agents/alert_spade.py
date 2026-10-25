"""SPADE transport adapter for typed risk and hotspot evidence.

The adapter delegates policy and suppression to :class:`AlertAgent`. Its local inbox is a
simulation recipient used to verify transport and acknowledgement; it is not a deployed
village-health-officer endpoint. ``alert_ack`` confirms receipt at that inbox only.
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Mapping

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from src.agents import common
from src.agents.alert import AlertAgent
from src.core import ontology


class AlertProcessingAgent(Agent):
    """Correlate same-division/month/thread evidence and issue typed local alerts."""

    def __init__(self, jid: str, password: str, *, divisions: list[str] | tuple[str, ...],
                 recipient_jid: str, risk_sender_jid: str, hotspot_sender_jid: str,
                 output_path: str | Path, receive_timeout_s: float = 0.25,
                 acknowledgement_timeout_s: float = 2.0, **kwargs):
        super().__init__(jid, password, **kwargs)
        if not divisions or len(set(divisions)) != len(divisions):
            raise ValueError("divisions must be a non-empty unique sequence")
        for name, value in (("receive_timeout_s", receive_timeout_s),
                            ("acknowledgement_timeout_s", acknowledgement_timeout_s)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        self.divisions = tuple(divisions)
        self.recipient_jid = recipient_jid
        self.risk_sender_jid = risk_sender_jid
        self.hotspot_sender_jid = hotspot_sender_jid
        self.output_path = Path(output_path)
        self.receive_timeout_s = float(receive_timeout_s)
        self.acknowledgement_timeout_s = float(acknowledgement_timeout_s)
        # The helper's optional logger is deliberately disabled. Only common.send records a
        # sent row, after SPADE accepted the send call.
        self.policy = AlertAgent(agent_id=str(self.jid), logger=lambda *args: None)
        self.risk_pending: dict[tuple[str, int, str], deque[dict[str, Any]]] = defaultdict(deque)
        self.hotspot_pending: dict[tuple[str, int, str], deque[dict[str, Any]]] = defaultdict(deque)
        self.pending_ack: dict[str, tuple[int, str, int, float]] = {}
        self.records: list[dict[str, Any]] = []
        self.decision_keys: set[tuple[str, int]] = set()
        self.finalized_month_indexes: set[int] = set()
        self._sequence = 0

    def _pair_key(self, body: Mapping[str, Any], thread: str | None):
        if not thread:
            return None
        return (body["ds_division"], body["month_index"], thread)

    async def accept_evidence(self, body: dict[str, Any], thread: str | None,
                              sender: str | None = None) -> None:
        """Queue a valid risk/hotspot body and process only an exact correlation match."""
        kind = body.get("type")
        allowed = {"risk_record", "hotspot_result"}
        if kind not in allowed:
            return
        expected_sender = (self.risk_sender_jid if kind == "risk_record"
                           else self.hotspot_sender_jid)
        if sender is not None and sender != expected_sender:
            self.records.append({"status": "unavailable", "delivery_status": "not_requested",
                                 "month_index": body.get("month_index"),
                                 "ds_division": body.get("ds_division"), "tier": body.get("tier"),
                                 "hotspot": None, "reason": "unexpected_evidence_sender"})
            return
        ontology.validate(ontology.INFORM, body)
        division, month = body["ds_division"], body["month_index"]
        if division not in self.divisions or month in self.finalized_month_indexes:
            self.records.append({"status": "unavailable", "delivery_status": "not_requested",
                                 "month_index": month, "ds_division": division,
                                 "tier": body.get("tier"), "hotspot": None,
                                 "reason": "unexpected_division_or_late_evidence"})
            return
        key = self._pair_key(body, thread)
        if key is None:
            self.records.append({"status": "unavailable", "delivery_status": "not_requested",
                                 "month_index": month, "ds_division": division,
                                 "tier": body.get("tier"), "hotspot": None,
                                 "reason": "missing_correlation_thread"})
            return
        own, other = ((self.risk_pending, self.hotspot_pending) if kind == "risk_record"
                      else (self.hotspot_pending, self.risk_pending))
        if other[key]:
            counterpart = other[key].popleft()
            risk, hotspot = ((body, counterpart) if kind == "risk_record"
                             else (counterpart, body))
            await self._decide(risk, hotspot, thread)
        else:
            own[key].append(dict(body))

    async def _decide(self, risk: dict[str, Any], hotspot_body: dict[str, Any], source_thread: str) -> None:
        division, month = risk["ds_division"], risk["month_index"]
        if hotspot_body["ds_division"] != division or hotspot_body["month_index"] != month:
            return
        status = hotspot_body["status"]
        hotspot = True if status == "active" else False if status == "inactive" else None
        suppression_before = self.policy.suppression_state
        decision = self.policy.process_risk_record(risk, hotspot=hotspot)
        row: dict[str, Any] = {
            "provenance": "synthetic_spade_simulation_local_inbox",
            "status": decision.status, "month_index": month, "ds_division": division,
            "tier": decision.tier, "hotspot": decision.hotspot, "message": decision.message,
            "reason": decision.reason, "source_thread_id": source_thread,
            "hotspot_status": status, "delivery_status": "not_requested",
            "delivery_events": [],
        }
        self.decision_keys.add((division, month))
        if decision.status == "emitted":
            row["delivery_status"] = "requested"
            row["delivery_events"].append({"state": "requested"})
            self._sequence += 1
            thread = f"alert:{division}:{month}:{self._sequence}"
            row["alert_thread_id"] = thread
            row_index = len(self.records)
            self.records.append(row)
            self.pending_ack[thread] = (month, division, row_index,
                                        time.monotonic() + self.acknowledgement_timeout_s)
            try:
                await self._send_alert(decision.alert_payload(), thread)
            except Exception as exc:
                self.pending_ack.pop(thread, None)
                # A failed transport send must not leave policy suppression state implying
                # that this alert was successfully issued. The next identical decision may retry.
                self.policy.last_state_by_village = suppression_before["last_state_by_village"]
                self.policy.state_by_village_month = suppression_before["state_by_village_month"]
                row["delivery_status"] = "failed"
                row["delivery_reason"] = f"send_failed:{type(exc).__name__}:{exc}"
                row["delivery_events"].append({"state": "failed", "reason": row["delivery_reason"]})
            else:
                if row["delivery_status"] != "confirmed":
                    row["delivery_status"] = "sent"
                    row["delivery_events"].append({"state": "sent"})
                else:
                    row["delivery_events"].insert(1, {"state": "sent"})
        else:
            self.records.append(row)

    async def _send_alert(self, payload: dict[str, Any], thread: str) -> None:
        class Send(OneShotBehaviour):
            async def run(inner):
                await common.send(inner, self.recipient_jid, ontology.INFORM, payload, thread)
        behaviour = Send()
        common.add_observed_behaviour(self, behaviour)
        await behaviour.join()

    def accept_ack(self, body: dict[str, Any], sender: str, thread: str | None) -> bool:
        pending = self.pending_ack.get(thread or "")
        if pending is None or sender != self.recipient_jid:
            return False
        month, division, row_index, _deadline = pending
        if body["month_index"] != month or body["ds_division"] != division:
            return False
        self.records[row_index]["delivery_status"] = "confirmed"
        self.records[row_index]["delivery_events"].append({"state": "confirmed",
                                                            "recipient": self.recipient_jid})
        self.pending_ack.pop(thread, None)
        return True

    def expire_confirmations(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for thread, (month, division, row_index, deadline) in list(self.pending_ack.items()):
            if now >= deadline:
                self.records[row_index]["delivery_status"] = "failed"
                self.records[row_index]["delivery_reason"] = "confirmation_timeout"
                self.records[row_index]["delivery_events"].append({"state": "failed",
                                                                    "reason": "confirmation_timeout"})
                self.pending_ack.pop(thread, None)

    def finalize_month(self, month_index: int) -> None:
        """Record unavailable decisions for divisions lacking a matched pair."""
        self.finalized_month_indexes.add(month_index)
        decided = {key[0] for key in self.decision_keys if key[1] == month_index}
        for division in self.divisions:
            if division not in decided:
                self.records.append({"provenance": "synthetic_spade_simulation_local_inbox",
                                     "status": "unavailable", "month_index": month_index,
                                     "ds_division": division, "tier": None, "hotspot": None,
                                     "message": None, "reason": "risk_hotspot_evidence_unmatched_or_missing",
                                     "delivery_status": "not_requested"})
                self.decision_keys.add((division, month_index))

    def write_output(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(self.records, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")

    class Receive(CyclicBehaviour):
        async def run(self):
            agent: AlertProcessingAgent = self.agent
            got = await common.receive(self, timeout=agent.receive_timeout_s)
            agent.expire_confirmations()
            if got is None:
                return
            msg, performative, body = got
            if performative != ontology.INFORM:
                return
            if body["type"] in {"risk_record", "hotspot_result"}:
                await agent.accept_evidence(body, msg.thread, str(msg.sender))
            elif body["type"] == "alert_ack":
                agent.accept_ack(body, str(msg.sender), msg.thread)

    async def setup(self):
        common.add_observed_behaviour(self, self.Receive())


class AlertInboxAgent(Agent):
    """Run-local typed alert sink; it confirms transport receipt, not real-world delivery."""

    def __init__(self, jid: str, password: str, *, allowed_sender: str,
                 receive_timeout_s: float = 0.25, **kwargs):
        super().__init__(jid, password, **kwargs)
        if receive_timeout_s <= 0:
            raise ValueError("receive_timeout_s must be positive")
        self.allowed_sender = allowed_sender
        self.receive_timeout_s = float(receive_timeout_s)
        self.received_alerts: list[dict[str, Any]] = []

    class Receive(CyclicBehaviour):
        async def run(self):
            agent: AlertInboxAgent = self.agent
            got = await common.receive(self, timeout=agent.receive_timeout_s)
            if got is None:
                return
            msg, performative, body = got
            if (performative != ontology.INFORM or body["type"] != "alert"
                    or str(msg.sender) != agent.allowed_sender):
                return
            agent.received_alerts.append({"thread_id": msg.thread, "sender": str(msg.sender),
                                          "recipient": str(agent.jid), "body": body})
            ack = {"type": "alert_ack", "month_index": body["month_index"],
                   "ds_division": body["ds_division"]}
            await common.send(self, agent.allowed_sender, ontology.INFORM, ack, msg.thread)

    async def setup(self):
        common.add_observed_behaviour(self, self.Receive())
