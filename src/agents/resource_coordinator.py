"""SPADE coordinator for one production Contract Net round.

The instance is configured for one ``month_index``/round pair. A scheduler may create a fresh
round configuration for each negotiation; the thread ID keeps concurrent or successive rounds
separate. This is the distributed agent implementation, distinct from the illustrative local
allocator in ``src/core/local_allocation.py``.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from spade.agent import Agent
from spade.behaviour import OneShotBehaviour
from spade.template import Template

from src.agents import common
from src.core import contract_net, msg_log, ontology


class ResourceCoordinatorAgent(Agent):
    """Run a configured, bounded Contract Net round and persist its audit result."""

    IDLE = "IDLE"
    CFP_SENT = "CFP_SENT"
    COLLECTING = "COLLECTING"
    AWARDING = "AWARDING"
    CONFIRMING = "CONFIRMING"
    DONE = "DONE"

    def __init__(self, jid: str, password: str, config: contract_net.CoordinatorConfig, **kwargs):
        super().__init__(jid, password, **kwargs)
        self.config = config
        self.thread_id = config.correlation_id
        self.round_result: dict[str, Any] | None = None

    class RunRound(OneShotBehaviour):
        async def _receive_raw(self, timeout: float):
            """Receive and log one raw packet so malformed replies remain auditable."""
            msg = await self.receive(timeout=timeout)
            if msg is None:
                return None, None
            performative = msg.get_metadata("performative") or ""
            msg_log.log_message(
                str(msg.sender), str(self.agent.jid), performative, msg.thread,
                msg.body or "", "received",
                path=getattr(self.agent, "message_log_path", msg_log.DEFAULT_PATH),
            )
            try:
                body = ontology.decode(performative, msg.body or "")
                return msg, body
            except ontology.SchemaError as exc:
                return msg, {"_invalid": str(exc)}

        def _base_result(self) -> dict[str, Any]:
            config = self.agent.config
            return {
                "schema_version": 1,
                "month_index": config.month_index,
                "round": config.round_number,
                "resource": config.resource,
                "resource_pool": config.resource_pool,
                "thread_id": self.agent.thread_id,
                "state_history": [self.agent.IDLE],
                "proposals": [],
                "refusals": [],
                "ineligible_proposals": [],
                "malformed_replies": [],
                "unmatched_replies": [],
                "missing_recipients": [],
                "timed_out_recipients": [],
                "confirmation_timeouts": [],
                "selected_awardees": [],
                "selected_winner": None,
                "winner": None,
                "award_status": "not_started",
                "allocation_successful": False,
                "confirmed_awardees": [],
                "protocol_errors": [],
                "provenance": config.provenance,
            }

        def _transition(self, result: dict[str, Any], state: str) -> None:
            result["state_history"].append(state)

        async def run(self):
            agent: ResourceCoordinatorAgent = self.agent
            config = agent.config
            result = self._base_result()
            recipient_by_jid = {r.jid: r for r in config.recipients}
            division_to_jid = {r.ds_division: r.jid for r in config.recipients}
            proposal_by_division: dict[str, dict[str, Any]] = {}
            responded: set[str] = set()
            try:
                self._transition(result, agent.CFP_SENT)
                cfp = config.cfp_body()
                for recipient in config.recipients:
                    await common.send(self, recipient.jid, ontology.CFP, cfp, agent.thread_id)

                self._transition(result, agent.COLLECTING)
                proposal_deadline = time.monotonic() + float(config.proposal_timeout_s)
                while len(responded) < len(config.recipients):
                    remaining = proposal_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    msg, body = await self._receive_raw(remaining)
                    if msg is None:
                        break
                    sender = str(msg.sender)
                    if msg.thread != agent.thread_id or sender not in recipient_by_jid:
                        result["unmatched_replies"].append({
                            "sender": sender, "thread_id": msg.thread,
                            "performative": msg.get_metadata("performative") or "",
                            "reason": "sender or thread does not match this CFP",
                        })
                        continue
                    if sender in responded:
                        result["malformed_replies"].append({
                            "sender": sender, "reason": "duplicate proposal-phase reply",
                        })
                        continue
                    performative = msg.get_metadata("performative") or ""
                    if "_invalid" in body:
                        result["malformed_replies"].append({"sender": sender, "reason": body["_invalid"]})
                        responded.add(sender)
                        continue
                    if body.get("month_index") != config.month_index \
                            or body.get("round") != config.round_number:
                        result["unmatched_replies"].append({
                            "sender": sender, "thread_id": msg.thread, "performative": performative,
                            "reason": "month_index or round does not match this CFP",
                        })
                        continue
                    division = recipient_by_jid[sender].ds_division
                    if performative == ontology.REFUSE:
                        if body.get("ds_division") != division:
                            result["malformed_replies"].append({
                                "sender": sender, "reason": "refusal division does not match recipient",
                            })
                        else:
                            result["refusals"].append({"sender": sender, **body})
                        responded.add(sender)
                    elif performative == ontology.PROPOSE:
                        valid, reason = contract_net.valid_proposal(body, config, division)
                        if not valid:
                            result["ineligible_proposals"].append({
                                "sender": sender, "body": dict(body), "reason": reason,
                            })
                            responded.add(sender)
                            continue
                        proposal_by_division[division] = dict(body)
                        result["proposals"].append({"sender": sender, **body})
                        responded.add(sender)
                    else:
                        result["malformed_replies"].append({
                            "sender": sender, "reason": f"unexpected performative {performative!r} in proposal phase",
                        })
                        responded.add(sender)

                missing = sorted(set(recipient_by_jid) - responded)
                result["missing_recipients"] = missing
                result["timed_out_recipients"] = list(missing)

                self._transition(result, agent.AWARDING)
                ranked = contract_net.select_awardees(proposal_by_division, config.resource_pool)
                selected = {division for division, _ in ranked}
                result["selected_awardees"] = [
                    {"ds_division": division, "sender": division_to_jid[division],
                     "bid": proposal_by_division[division]["bid"], "normalized_bid": normalized,
                     "units": 1, "status": "selected"}
                    for division, normalized in ranked
                ]
                result["selected_winner"] = ranked[0][0] if ranked else None

                for division, jid in division_to_jid.items():
                    if division in proposal_by_division and division not in selected:
                        try:
                            await common.send(self, jid, ontology.REJECT_PROPOSAL, {
                                "type": "reject_proposal", "month_index": config.month_index,
                                "round": config.round_number,
                                "reason": "another eligible bid ranked higher for the available pool",
                            }, agent.thread_id)
                        except Exception as exc:  # record transport errors, continue other awards
                            result["protocol_errors"].append({"recipient": jid, "stage": "reject", "error": str(exc)})

                for item in result["selected_awardees"]:
                    try:
                        await common.send(self, item["sender"], ontology.ACCEPT_PROPOSAL, {
                            "type": "accept_proposal", "month_index": config.month_index,
                            "round": config.round_number, "resource": config.resource, "units": 1,
                        }, agent.thread_id)
                        item["status"] = "awaiting_confirmation"
                    except Exception as exc:
                        item["status"] = "award_send_failed"
                        item["error"] = str(exc)
                        result["protocol_errors"].append({
                            "recipient": item["sender"], "stage": "accept", "error": str(exc),
                        })

                pending = {item["sender"]: item for item in result["selected_awardees"]
                           if item["status"] == "awaiting_confirmation"}
                if pending:
                    self._transition(result, agent.CONFIRMING)
                    confirmation_deadline = time.monotonic() + float(config.confirmation_timeout_s)
                    while pending:
                        remaining = confirmation_deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        msg, body = await self._receive_raw(remaining)
                        if msg is None:
                            break
                        sender = str(msg.sender)
                        performative = msg.get_metadata("performative") or ""
                        if msg.thread != agent.thread_id or sender not in pending:
                            result["unmatched_replies"].append({
                                "sender": sender, "thread_id": msg.thread, "performative": performative,
                                "reason": "sender or thread does not match an outstanding award",
                            })
                            continue
                        if "_invalid" in body:
                            result["malformed_replies"].append({"sender": sender, "reason": body["_invalid"]})
                            continue
                        item = pending[sender]
                        if (performative == ontology.INFORM and body.get("type") == "inform_done"
                                and body.get("month_index") == config.month_index
                                and body.get("round") == config.round_number
                                and body.get("ds_division") == item["ds_division"]
                                and body.get("resource") == config.resource and body.get("units") == 1):
                            item["status"] = "confirmed"
                            item["confirmation"] = dict(body)
                            del pending[sender]
                        elif (performative == ontology.FAILURE and body.get("month_index") == config.month_index
                              and body.get("round") == config.round_number
                              and body.get("ds_division") == item["ds_division"]):
                            item["status"] = "failed"
                            item["failure"] = dict(body)
                            del pending[sender]
                        else:
                            result["malformed_replies"].append({
                                "sender": sender, "reason": "reply does not confirm or fail the outstanding award",
                            })
                    for item in pending.values():
                        item["status"] = "unconfirmed_timeout"
                        result["confirmation_timeouts"].append(item["sender"])

                confirmed = [item["ds_division"] for item in result["selected_awardees"]
                             if item["status"] == "confirmed"]
                result["confirmed_awardees"] = sorted(confirmed)
                result["winner"] = next(
                    (item["ds_division"] for item in result["selected_awardees"]
                     if item["status"] == "confirmed"), None,
                )
                statuses = [item["status"] for item in result["selected_awardees"]]
                if not statuses:
                    result["award_status"] = "no_award"
                elif all(status == "confirmed" for status in statuses):
                    result["award_status"] = "confirmed"
                elif any(status == "confirmed" for status in statuses):
                    result["award_status"] = "partially_confirmed"
                elif any(status == "unconfirmed_timeout" for status in statuses):
                    result["award_status"] = "unconfirmed"
                elif any(status == "failed" for status in statuses):
                    result["award_status"] = "failed"
                else:
                    result["award_status"] = "send_failed"
                result["allocation_successful"] = bool(statuses) and all(
                    status == "confirmed" for status in statuses
                )
            except Exception as exc:
                result["award_status"] = "protocol_error"
                result["protocol_errors"].append({"stage": "round", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                self._transition(result, agent.DONE)
                try:
                    contract_net.write_round_result(config.output_path, result)
                except Exception as exc:
                    result["protocol_errors"].append({
                        "stage": "persist", "error": f"{type(exc).__name__}: {exc}",
                    })
                agent.round_result = result

    async def setup(self):
        expected = Template(metadata={"performative": ontology.PROPOSE}) \
            | Template(metadata={"performative": ontology.REFUSE}) \
            | Template(metadata={"performative": ontology.INFORM}) \
            | Template(metadata={"performative": ontology.FAILURE})
        self.add_behaviour(self.RunRound(), expected)
