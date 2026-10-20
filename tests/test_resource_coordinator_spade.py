"""Opt-in embedded-SPADE integration test; does not verify external XMPP deployment."""
from __future__ import annotations

import asyncio
import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import spade

from src.agents.resource_coordinator import ResourceCoordinatorAgent
from src.agents.village import VillageAgent
from src.core.contract_net import CoordinatorConfig, VillageRecipient
from src.core import msg_log, ontology


@unittest.skipUnless(os.environ.get("CKDU_RUN_SPADE_INTEGRATION") == "1",
                     "set CKDU_RUN_SPADE_INTEGRATION=1 to run embedded-XMPP integration")
class EmbeddedCoordinatorTests(unittest.TestCase):
    def test_coordinator_and_villages_complete_one_confirmed_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "messages.csv"
            output_path = root / "round.json"
            recipients = [VillageRecipient("village-alpha@localhost", "Alpha"),
                          VillageRecipient("village-beta@localhost", "Beta"),
                          VillageRecipient("village-gamma@localhost", "Gamma")]
            config = CoordinatorConfig(
                month_index=2, round_number=1, resource="RO_unit", resource_pool=1,
                recipients=recipients, proposal_timeout_s=4, confirmation_timeout_s=4,
                output_path=output_path, thread_id="integration-round-2",
                provenance="illustrative SPADE integration fixture only",
            )
            villages = [
                VillageAgent(recipients[0].jid, "demo", "Alpha", population=100,
                             distance_to_depot_km=1),
                VillageAgent(recipients[1].jid, "demo", "Beta", population=100,
                             distance_to_depot_km=1),
                VillageAgent(recipients[2].jid, "demo", "Gamma", population=100,
                             distance_to_depot_km=1),
            ]
            villages[0].set_risk(120, "Poor", month_index=2)
            villages[1].set_risk(100, "Poor", month_index=2)
            villages[2].set_risk(80, "Good", month_index=2)
            coordinator = ResourceCoordinatorAgent("coordinator@localhost", "demo", config)
            started = []
            original_log = msg_log.log_message

            def isolated_log(sender, receiver, performative, thread_id, payload, direction,
                             path=msg_log.DEFAULT_PATH):
                original_log(sender, receiver, performative, thread_id, payload, direction,
                             path=log_path)

            async def scenario():
                try:
                    for agent in [*villages, coordinator]:
                        await agent.start(auto_register=True)
                        started.append(agent)
                    behaviour = next(iter(coordinator.behaviours))
                    await asyncio.wait_for(behaviour.join(), timeout=20)
                finally:
                    await asyncio.gather(*(agent.stop() for agent in reversed(started)),
                                         return_exceptions=True)

            with patch("src.core.msg_log.log_message", side_effect=isolated_log):
                spade.run(scenario(), embedded_xmpp_server=True)

            result = coordinator.round_result
            self.assertIsNotNone(result)
            self.assertIn(result["winner"], {"Alpha", "Beta"})
            self.assertTrue(result["allocation_successful"])
            self.assertEqual(result["award_status"], "confirmed")
            self.assertTrue(output_path.is_file())
            self.assertTrue(log_path.is_file())
            with log_path.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
                self.assertEqual(reader.fieldnames, msg_log.COLUMNS)
            self.assertTrue({ontology.CFP, ontology.PROPOSE, ontology.REFUSE,
                             ontology.ACCEPT_PROPOSAL, ontology.REJECT_PROPOSAL,
                             ontology.INFORM}.issubset({row["performative"] for row in rows}))
            self.assertEqual({row["direction"] for row in rows}, {"sent", "received"})


if __name__ == "__main__":
    unittest.main()
