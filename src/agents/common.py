"""Shared SPADE glue: every send and receive goes through here so it is validated and logged."""
from __future__ import annotations

import asyncio
from typing import Any

from spade.behaviour import CyclicBehaviour
from spade.message import Message

from src.core import msg_log, ontology
from src.sim.event_journal import append_event


def _log_path(behaviour: CyclicBehaviour):
    """Use an agent-specific isolated log when configured; preserve the shared default otherwise."""
    return getattr(behaviour.agent, "message_log_path", msg_log.DEFAULT_PATH)


def record_agent_event(agent: Any, event_type: str, **details: Any) -> None:
    """Append telemetry only when the runner configured an isolated run journal."""
    path = getattr(agent, "event_journal_path", None)
    if path is None:
        return
    append_event(
        path, event_type, getattr(agent, "monitor_component", type(agent).__name__),
        identity=str(agent.jid), run_id=getattr(agent, "run_id", None), **details,
    )


def add_observed_behaviour(agent: Any, behaviour: Any, template: Any = None) -> None:
    """Register a SPADE behaviour that records an uncaught failure, then preserves SPADE handling."""
    original_run = behaviour.run

    async def observed_run():
        try:
            return await original_run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures = getattr(agent, "monitor_behavior_failures", None)
            if failures is not None:
                failures.append({"behavior": type(behaviour).__name__,
                                 "error": f"{type(exc).__name__}: {exc}"})
            record_agent_event(
                agent, "agent_behavior_failed", behavior=type(behaviour).__name__,
                reason=f"{type(exc).__name__}: {exc}",
            )
            raise

    behaviour.run = observed_run
    if template is None:
        agent.add_behaviour(behaviour)
    else:
        agent.add_behaviour(behaviour, template)


def add_agent_heartbeat(agent: Any, *, interval_s: float = 1.0) -> None:
    """Add a periodic behavior-loop heartbeat to this SPADE agent, once per instance."""
    if getattr(agent, "_monitor_heartbeat_installed", False):
        return
    if interval_s <= 0:
        raise ValueError("heartbeat interval must be positive")

    class Heartbeat(CyclicBehaviour):
        async def run(self):
            details: dict[str, Any] = {"interval_s": interval_s}
            month_index = getattr(agent, "monitor_month_index", None)
            if month_index is None:
                config = getattr(agent, "config", None)
                month_index = getattr(config, "month_index", None)
            if month_index is not None:
                details["month_index"] = month_index
            config = getattr(agent, "config", None)
            round_number = getattr(config, "round_number", None)
            thread_id = getattr(agent, "thread_id", None)
            if round_number is not None:
                details["round"] = round_number
            if thread_id is not None:
                details["thread_id"] = thread_id
            record_agent_event(agent, "agent_heartbeat", **details)
            await asyncio.sleep(interval_s)

    agent._monitor_heartbeat_installed = True
    add_observed_behaviour(agent, Heartbeat())


async def send(
    behaviour: CyclicBehaviour,
    to: str,
    performative: str,
    body: dict[str, Any],
    thread: str | None = None,
) -> None:
    """Validate ``body`` against the ontology, send it, and log it as sent."""
    text = ontology.encode(performative, body)
    msg = Message(to=to, body=text, thread=thread, metadata={"performative": performative})
    await behaviour.send(msg)
    msg_log.log_message(
        str(behaviour.agent.jid), to, performative, thread, text, "sent",
        path=_log_path(behaviour),
    )


async def receive(
    behaviour: CyclicBehaviour, timeout: float
) -> tuple[Message, str, dict[str, Any]] | None:
    """Wait at most ``timeout`` seconds. Returns (msg, performative, body) or None on timeout.

    A message that fails ontology validation is logged and dropped (returns None).
    """
    msg = await behaviour.receive(timeout=timeout)
    if msg is None:
        return None
    performative = msg.get_metadata("performative") or ""
    msg_log.log_message(
        str(msg.sender), str(behaviour.agent.jid), performative, msg.thread, msg.body or "", "received",
        path=_log_path(behaviour),
    )
    try:
        body = ontology.decode(performative, msg.body)
    except ontology.SchemaError as exc:
        print(f"[{behaviour.agent.jid}] dropped invalid message from {msg.sender}: {exc}")
        return None
    return msg, performative, body
