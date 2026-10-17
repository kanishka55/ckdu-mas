from src.agents import xmpp_patch

xmpp_patch.apply()  # STARTTLS race with `spade run`; see src/agents/xmpp_patch.py
