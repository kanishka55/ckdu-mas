"""Shared SPADE glue: every send and receive goes through here so it is validated and logged."""
from __future__ import annotations

from typing import Any

from spade.behaviour import CyclicBehaviour
from spade.message import Message

from src.core import msg_log, ontology


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
    msg_log.log_message(str(behaviour.agent.jid), to, performative, thread, text, "sent")


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
        str(msg.sender), str(behaviour.agent.jid), performative, msg.thread, msg.body or "", "received"
    )
    try:
        body = ontology.decode(performative, msg.body)
    except ontology.SchemaError as exc:
        print(f"[{behaviour.agent.jid}] dropped invalid message from {msg.sender}: {exc}")
        return None
    return msg, performative, body
