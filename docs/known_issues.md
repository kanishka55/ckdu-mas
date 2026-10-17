# Known issues

## FIXED: agents could not log in to an external `spade run` server (Windows, spade 4.1.4)

**Status:** worked around in `src/agents/xmpp_patch.py` (2026-10-17). Applied automatically when
anything imports `src.agents`. Both modes now work:
```
spade run --host localhost --memory           # terminal 1
python -m src.sim.demo_contract_net            # terminal 2, external server
python -m src.sim.demo_contract_net --embedded-server   # or one process, no terminal 1
```
Do not run both at once: the embedded server cannot start while `spade run` holds port 5222.

**Symptom (before the fix):** against `spade run --host localhost`, agents hung during login and
SPADE eventually printed `Exception in the event loop: Error during the connection with the server`.
The embedded server always worked. Turning on DEBUG logging for `slixmpp.xmlstream.xmlstream` made it
work sometimes.

**Root cause:** a STARTTLS race between the client (slixmpp_multiplatform 1.12.0) and the server
(pyjabber 0.4.5). It is a timing bug, not a Windows-only or Linux-only limitation.
1. The server replies `<proceed/>`, then `await loop.start_tls(...)`, and only **after** that calls
   `parser.reset_stack()` (`pyjabber/stream/negotiators/StreamNegotiator.py`, `_handle_tls`).
2. The client sends its new `<stream:stream>` header the instant the TLS handshake completes.
3. If the header arrives before step 1's reset, the server parses it and then discards it. Both sides
   then wait for each other forever.

This explains every observation: the embedded server shares one event loop with the agents, so the
server always resets first. DEBUG logging slows the client just enough to lose the race. Logins
failed intermittently (one agent in, the next stuck).

**How it was confirmed (2026-10-17):** a byte-level trace of one login showed the client's post-TLS
stream header was never answered. With a 50 ms client-side delay before that header: 6 of 6 logins
succeeded, against 0 of 4 without. Ruled out on the way: the event loop (winloop and the standard
selector loop both failed), and IPv6 `localhost` → `::1` (forcing 127.0.0.1 still failed).

**The workaround:** `xmpp_patch.apply()` wraps slixmpp's `XMLStream.connection_made`. On the TLS
upgrade call (`send_event=False`) it waits `TLS_STREAM_DELAY_S` (0.05 s) before sending the header.
Fresh connections are untouched. Remove the patch if a later pyjabber resets its parser before
starting TLS. If logins hang again after a dependency upgrade, check here first.

**Other notes that still apply:**
- `spade run` with the default `--host 0.0.0.0` names the XMPP domain `0.0.0.0`, which does not match
  the agents' `@localhost` JIDs. Always pass `--host localhost`.
- On Windows, `localhost` resolves to `::1` first but the server listens on IPv4 only. slixmpp falls
  back to 127.0.0.1, so this only costs a failed first attempt; it is not the cause of the hang.
- The first spade install was missing `greenlet` (SQLAlchemy asyncio, used by pyjabber).
  It is pinned in `requirements.txt`.
