# Known issues

## Agents cannot log in to an external `spade run` server (Windows, spade 4.1.4)

**Symptom:** `python -m src.sim.demo_contract_net` against `spade run --host localhost` hangs for
about 85 s and then prints `Exception in the event loop: Error during the connection with the server`.

**What we observed (2026-10-10, Windows 10, Python 3.12, spade 4.1.4, slixmpp in requirements.txt):**
- `--embedded-server` (SPADE's in-process server) works every time. Use it for now.
- Against the external server, a login hangs when the `slixmpp.xmlstream.xmlstream` logger is
  below DEBUG. With DEBUG logging on (even to a NullHandler), a single bare agent connects at once,
  but the full demo still failed. Root cause not found.
- `spade run` with the default `--host 0.0.0.0` names the XMPP domain `0.0.0.0`, which does not
  match the agents' `@localhost` JIDs. Always pass `--host localhost`.
- The first spade install was missing `greenlet` (SQLAlchemy asyncio, used by pyjabber).
  It is now pinned in `requirements.txt`.

**To try:** run under WSL (START_HERE recommends it for SPADE), or test against another XMPP server
such as Prosody. The one-process-per-agent setup needs an external server, so this must be fixed
before the week-9 integration.
