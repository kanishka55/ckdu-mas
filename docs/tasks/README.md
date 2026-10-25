# Per-member task briefs

Open **your own file** and keep it beside you while you code. Each brief lists the files you own,
your tasks week by week as checkboxes, the interfaces you publish to the others, the verification
commands, and prompts that work in Claude Code.

| Member | Brief | Branch | Owns |
|---|---|---|---|
| **A** | [member_a_data_and_villages.md](member_a_data_and_villages.md) | `feat/a-data` | `data/`, loaders, well schema, bidding, ClockAgent, WellMonitorAgent |
| **B** | [member_b_risk_and_hotspot.md](member_b_risk_and_hotspot.md) | `feat/b-risk` | WQI, risk tiers, RiskScoringAgent, HotspotAgent |
| **C** | [member_c_contract_net.md](member_c_contract_net.md) | `feat/c-contractnet` | Contract Net, VillageAgent, ResourceCoordinatorAgent, allocation baselines |
| **D** | [member_d_alerts_eval_dashboard.md](member_d_alerts_eval_dashboard.md) | `feat/d-integration` | ontology, message log, AlertAgent, evaluation, dashboard, report |

Agent ownership follows the message flow (team agreement, 2026-10-25): A = Clock, WellMonitor;
B = RiskScoring, Hotspot; C = Village, ResourceCoordinator; D = Alert, evaluation, dashboard.

How the four fit together, the shared week-1 foundation and the full timeline:
**[../team_split.md](../team_split.md)**. Permanent rules: `CLAUDE.md`. Background: `../project_plan.md`.

---

## Before anyone starts their own stream

Week 1 is **shared**, not split. `src/core/ontology.py` and `src/core/msg_log.py` get written by all
four together and merged first — see §0 of [team_split.md](../team_split.md). Four people writing
SPADE agents against an unfrozen message schema is the fastest way to lose a week on this project.
**Schema freeze: end of week 3.**

## Two open blockers (B leads, whole team decides)

1. **`TO_FILL` weights** in `data/raw/ckdu_02_wqi_method_and_thresholds.csv` — get Tables S1 and S2
   from the MDPI supplement. Never invent a weight or a threshold.
2. **Parameter coverage gap** — the published WQI needs 13 parameters; the data carries 9
   (TDS, Ca, Mg, Na, NO3 are missing). Complete them, or commit to a documented reduced-parameter WQI.

Both are detailed in §3 of [B's brief](member_b_risk_and_hotspot.md). They change A's generator,
C's bids and D's alerts, so decide together.

## Applies to all four

- Business logic in `src/core` (pure, no SPADE imports), unit-tested without XMPP. `src/agents` stays thin.
- Every message logged. Every `receive()` and every CFP deadline has a timeout. Fixed random seeds.
- Any synthetic row carries `source="synthetic"`. Never mix real and synthetic silently.
- No individual patient data. Published aggregates only.
- This is a **risk-indicator tool built on published water-quality research**. It never diagnoses
  CKDu and never claims water chemistry causes CKDu. Keep that wording in code, UI and docs.
- Before saying work is done: `pytest` passes, the toy Contract Net demo runs end to end and writes
  `logs/messages.csv`, and you **show the command and its output**.
