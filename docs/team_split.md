# Team split — 4 members

Multi-Agent Systems coursework, MSc Data Science and AI, University of Sri Jayewardenepura.
Derived from `docs/project_plan.md` §7 and the actual contents of `data/raw/` + `data/processed/`.

**Each member has their own task brief in [`docs/tasks/`](tasks/README.md) — open yours and keep it
beside you while you code. This file is the overview of how the four parts fit together.**

Ownership means **you are accountable for that code, its tests and its section of the report**.
It does not mean nobody else may touch it — the course grades individual contribution, so everyone
reviews everyone's pull requests and everyone must be able to explain the whole system in the demo.

Reminder for every member: this is a **risk-indicator tool built on published water-quality research**.
It never diagnoses CKDu and never claims water chemistry causes CKDu. Keep that wording in code
comments, UI strings, alert text and report sections.

---

## 0. Shared foundation — week 1, all four together (do this before splitting)

One pairing session, one branch, merged before anyone starts their own stream. This is the single
biggest collision risk in a 4-person SPADE project: if the message schema is not frozen early, four
agents get written against four different payload shapes.

| Item | File | Who |
|---|---|---|
| Message ontology: JSON schema + performatives for every message type | `src/core/ontology.py` | D drafts, all 4 review and sign off |
| Message logger (timestamp, sender, receiver, performative, thread id, payload) writing `logs/messages.csv` | `src/core/msg_log.py` | D |
| `requirements.txt` with **pinned** versions (install first, then pin — do not guess) | `requirements.txt` | A |
| `.venv` + `spade run` verified on each machine | — | each member on their own |
| Toy Contract Net demo: 1 coordinator + 3 villages, cfp → propose → accept/reject, writes `logs/messages.csv` | `src/sim/demo_contract_net.py` | C, with D on the logger |

**Freeze date: end of week 3.** After that, a change to `ontology.py` needs all four to agree,
because it breaks everyone's agents at once.

---

## 1. Member A — Data layer, ClockAgent, WellMonitorAgent

Ownership changed on 2026-10-25 by team agreement: agents are split in message-flow order, so A
owns the first two agents (ClockAgent, WellMonitorAgent) and VillageAgent moved to Member C.

Brief: [`docs/tasks/member_a_data_and_villages.md`](tasks/member_a_data_and_villages.md) · Branch: `feat/a-data`

**Owns**

- `data/` — all of it. `data/raw/` stays read-only; everything derived goes to `data/processed/`.
- `src/sim/load_data.py` — load the raw CSVs and `data/processed/synthetic_wells.csv` into tidy frames.
- `src/core/wells.py` — well/village record schema, validation, real-vs-synthetic guard.
- `src/core/bidding.py` — the bid function (pure, tested). First draft from START_HERE:
  `need = risk × population / distance_to_depot`. Current version (2026-10-17):
  `bid(risk, population) = risk × population`, bid only when tier is Poor or worse; distance is the
  coordinator's delivery cost. See `docs/data_assumptions.md`.
- `src/agents/clock.py` — ClockAgent: broadcasts the simulated "new month" tick (month, season).
- `src/agents/well_monitor.py` — WellMonitorAgent: on each tick, sends each village's readings.
- `src/core/bidding.py` stays with A: the bid formula C's VillageAgent calls.

**Must deliver in week 1–2**

1. Fill the blank columns in `data/raw/ckdu_03_sampling_design_ds_divisions.csv`:
   `population` (Census 2011, Dept of Census and Statistics), `n_wells_modelled`,
   `distance_to_depot_km`, `lat`, `lon`. Population is real data and must be cited; the rest are
   **modelling assumptions** — write them down in `docs/` as assumptions, not as findings.
   That file has 11 DS divisions, which is inside the 10–15 VillageAgent target.
2. Enforce the real-vs-synthetic rule in code: every synthetic row carries `source="synthetic"`
   (the 440 rows in `synthetic_wells.csv` already do). Never mix silently.
3. Disclose `RHO = 0.6` (the assumed inter-parameter correlation in `src/generate_synthetic_wells.py`)
   as an unpublished assumption — it belongs in the methodology section, not buried in the script.

**Interface you publish to the others:** the `month_tick` and `well_readings` messages, the
well-reading frame columns, and the `bid(...)` / `has_need(...)` signatures.
B consumes your readings; C's VillageAgent and runner consume your ticks and bid function.

---

## 2. Member B — Risk scoring (WQI), risk tiers, HotspotAgent

Brief: [`docs/tasks/member_b_risk_and_hotspot.md`](tasks/member_b_risk_and_hotspot.md) · Branch: `feat/b-risk`

**Owns**

- `src/core/wqi.py` — the Water Quality Index: `WQI = Σ(Wi · qi)`, `qi = (Ci / Si) · 100`,
  `Wi = wi / Σwi`, exactly as stated in `data/raw/ckdu_02_wqi_method_and_thresholds.csv`.
- `src/core/risk_tiers.py` — WQI → tier using the published class ranges only:
  Excellent `<50`, Good `50–100`, Poor `100–200`. Cooray et al. never exceeded 200, so if you need a
  class above 200, take it from Table S2 — do not invent one.
- `src/agents/risk_scoring.py` — RiskScoringAgent: WQI per well, risk tier per village.
- `src/agents/hotspot.py` — HotspotAgent: compare village risk against the published aggregates in
  `ckdu_04_ckdu_aggregate_stats.csv` and the benchmarks in `ckdu_05_validation_benchmarks.csv`
  (clustering / correlation). This is a **sanity check on the model, not a causal claim**.

**Two blockers to resolve in week 1 — the WQI is not real until both are closed**

1. **`TO_FILL` weights.** In `ckdu_02`, the row
   `Weight wi: pH; hardness; alkalinity; Ca; Mg; Na; Cl; SO4; Fe` is `TO_FILL`, and the SLS standard
   values `Si` live in the same supplement. Download <https://www.mdpi.com/1660-4601/16/10/1698/s1>
   (Tables S1 and S2, CC BY) and fill the row. Only TDS, F, NO3 and DOC are confirmed at weight 5.
   **Project rule: never invent a threshold or a weight — stop and ask.**
2. **Parameter coverage gap.** The published WQI uses 13 parameters (pH, TDS, hardness, alkalinity,
   Ca, Mg, Na, F, Cl, SO4, NO3, Fe, DOC) but `ckdu_01` — and therefore the synthetic wells — carries
   only 9 (pH, EC, alkalinity, hardness, F, DOC, Cl, SO4, Fe). **TDS, Ca, Mg, Na and NO3 are missing.**
   Either (a) derive TDS from EC and transcribe the rest from the supplement / Nikagolla et al. 2020,
   or (b) compute a **reduced-parameter WQI** over the 9 available and reweight — in which case say so
   explicitly and stop calling it "the Cooray WQI". Decide with the team: it changes every downstream
   number, including C's bids and D's alerts.

**Validation target:** Cooray et al. report 3.8% wet / 2.6% dry excellent and 28.2% wet / 30.3% dry
poor. If your tiers on the synthetic wells land nowhere near that distribution, either the model or
the synthetic generator is wrong.

**Interface you publish:** the risk record (village, month, wqi, tier, drivers). C uses `tier` in the
CFP; D displays it and alerts on it.

---

## 3. Member C — Contract Net negotiation, ResourceCoordinatorAgent, baselines

Brief: [`docs/tasks/member_c_contract_net.md`](tasks/member_c_contract_net.md) · Branch: `feat/c-contractnet`

This is the core MAS demonstration and the part the course grades hardest.

**Owns**

- `src/agents/resource_coordinator.py` — ResourceCoordinatorAgent running the full FIPA Contract Net
  Protocol: `cfp` → `propose` / `refuse` → `accept-proposal` / `reject-proposal` → `inform` / `failure`.
- `src/core/allocation.py` — pure allocation logic plus the two **baselines to compare against**:
  equal split, and first-come-first-served.
- `src/agents/village.py` — VillageAgent (11 instances, moved from A on 2026-10-25): holds its own
  village data, decides propose/refuse on each CFP using A's `bid()`/`has_need()`, confirms or fails awards.
- `src/sim/runner.py` — the simulation runner: wires A's ClockAgent ticks to monthly negotiation rounds.
- Scarcity configuration: the pool of RO units and bowsers **must be genuinely scarce** — fewer
  resources than high-risk villages, or the negotiation is theatre and there is nothing to evaluate.

**Hard rules you own**

- Every `receive()` and every CFP deadline has a timeout. Never block forever.
- Fixed random seeds everywhere.
- Every message goes through D's logger — the evaluation metrics are computed from `logs/messages.csv`.

**Realism anchor:** Horbulyk et al. 2021 (`ckdu_05`) found that by 2018 only ~35% of households drank
untreated groundwater while ~50% used RO-treated water. Use that to justify RO units as the contested
resource rather than picking a pool size out of the air.

**Interface you publish:** the CFP payload and the allocation record (month, resource, village, round,
winning bid) written to `logs/`. D's evaluation and dashboard read it.

---

## 4. Member D — AlertAgent, infrastructure, evaluation, dashboard, report

Brief: [`docs/tasks/member_d_alerts_eval_dashboard.md`](tasks/member_d_alerts_eval_dashboard.md) · Branch: `feat/d-integration`

**Owns**

- `src/core/ontology.py` + `src/core/msg_log.py` — the shared contract and the message log (from §0).
- `src/agents/alert.py` — AlertAgent: tiered, **non-alarmist** alerts. "Use an alternate water source,
  consider screening." Never a diagnosis, never a causal claim. Draft the exact strings and have all
  four review the wording — this is the main place the project can lose marks on framing.
- `src/sim/evaluate.py` — the four metrics from the plan: risk-classification alignment, allocation
  efficiency vs both baselines, alert latency, negotiation rounds to converge.
- `dashboard/app.py` — Streamlit + Folium, villages coloured by risk tier. It **reads the log files**;
  it does not run inside the agent process. Put the risk-indicator disclaimer on screen.
- Integration: making all seven agent types run together, plus the demo rehearsal.
- Report assembly and the limitations section (small real datasets — tens to ~90 samples; synthetic
  data disclosure; the prevalence inconsistency noted in `data/raw/README_data_sources.md`).

**Caveats to carry into the report** (all from the data pack README)

- `ckdu_04` incidence values have no unit in the text — check Figure 1 of the paper before quoting one.
- Ranasinghe et al. give prevalence 1.52–3.35 (Anuradhapura) in the Results but 2.44–4.35 in the
  Abstract. Cite the Results figures and note the inconsistency.
- Several papers — Nikagolla et al. 2020 explicitly — conclude the chemistry–CKDu link is **not proven**.

---

## 5. Who depends on whom

```
A (wells, villages, bids) --> B (WQI, tiers, hotspots) --> C (Contract Net) --> D (alerts, eval, dashboard)
            |                          |                          |                        ^
            +--------------------------+--------------------------+------------------------+
                        all four depend on D's ontology.py + msg_log.py (section 0)
```

Nobody is blocked waiting: A, B and C each start against a stub. B stubs A's frames from
`synthetic_wells.csv` directly; C stubs B's tiers with a fixed dict; D stubs everything from a
hand-written `logs/messages.csv`. Stubs are replaced with real calls at the week-9 integration milestone.

## 6. Timeline mapped to owners

| Week | A | B | C | D |
|---|---|---|---|---|
| 1–2 | fill `ckdu_03` blanks, pin requirements | close `TO_FILL` + parameter gap | toy Contract Net demo | ontology, logger |
| 3–4 | loaders, well schema | WQI draft + tests | CFP state machine | alert text drafts |
| 5–6 | ClockAgent, WellMonitorAgent | RiskScoringAgent | VillageAgent, scarcity model, baselines | AlertAgent |
| 7–8 | bid function tuning | HotspotAgent | full negotiation, 10–15 villages | evaluation script |
| 9 | integration | integration | integration | integration lead |
| 10 | — | validate vs `ckdu_05` | efficiency vs baselines | metrics write-up |
| 11 | data appendix | model section | negotiation section | dashboard + report assembly |
| 12 | demo rehearsal — all four; everyone explains a part they do not own |

## 7. Workflow

- One branch per person: `feat/a-data`, `feat/b-risk`, `feat/c-contractnet`, `feat/d-integration`.
- Pull before starting. Small commits. `CLAUDE.md` is committed and shared, so the rules stay
  identical across all four Claude Code sessions.
- Business logic lives in `src/core` and is unit-tested **without XMPP**. `src/agents` stays thin.
- Before calling a task done: `pytest` passes, the toy Contract Net demo runs end to end and writes
  `logs/messages.csv`, and you show the command and its output — not a claim that it works.
