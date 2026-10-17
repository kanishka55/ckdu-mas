"""Workaround for a STARTTLS race between slixmpp (SPADE's XMPP client) and pyjabber (`spade run`).

The bug (spade 4.1.4, pyjabber 0.4.5, slixmpp_multiplatform 1.12.0; see docs/known_issues.md):
after STARTTLS, pyjabber awaits ``loop.start_tls(...)`` and only then calls
``parser.reset_stack()``. slixmpp sends the new ``<stream:stream>`` header the moment the TLS
handshake completes. If that header reaches the server before the reset, the server parses it and
then throws it away, so both sides wait forever and the agent never logs in.

The fix: when slixmpp's transport is replaced by the TLS one (``connection_made`` with
``send_event=False``), wait ``TLS_STREAM_DELAY_S`` before sending the header, so the server has
reset its parser first. The embedded server never hits the race (one event loop, fixed ordering);
the delay is harmless there.

Applied once, on import of ``src.agents``. Remove it when pyjabber fixes the ordering.
"""
from __future__ import annotations

import asyncio

from slixmpp.xmlstream.xmlstream import XMLStream

TLS_STREAM_DELAY_S = 0.05  # 0 of 4 logins succeeded without it, 6 of 6 with it (2026-10-17)

_applied = False


def apply() -> None:
    global _applied
    if _applied:
        return
    original = XMLStream.connection_made

    def connection_made(self, transport, *args, **kwargs):
        if kwargs.get("send_event") is False:  # TLS upgrade, not a fresh connection
            asyncio.get_event_loop().call_later(
                TLS_STREAM_DELAY_S, lambda: original(self, transport, *args, **kwargs))
            return
        return original(self, transport, *args, **kwargs)

    XMLStream.connection_made = connection_made
    _applied = True
