# Member C — Contract Net negotiation, ResourceCoordinatorAgent, baselines

Branch: `feat/c-contractnet`  ·  Overview: [team_split.md](../team_split.md)  ·  Rules: `CLAUDE.md`

You own the core MAS demonstration. A Multi-Agent Systems course grades the negotiation protocol
harder than anything else in this project: a correct FIPA Contract Net over a **genuinely scarce**
resource pool, with logged messages and a measurable advantage over naive baselines.

---

## 0. Status as of 2026-10-17: what A and B already deliver

Parts A and B are built and tested together (`pytest`: 101 passed). You can build on them now.

| Already working | Owner | What it gives you |
|---|---|---|
| `WellMonitorAgent` (`src/agents/well_monitor.py`) | A | On each month tick, sends `well_readings` per village to RiskScoring |
| `RiskScoringAgent` (`src/agents/risk_scoring.py`) | B | Sends one `risk_record` per village per month to its subscribers, **including you** |
| `VillageAgent` × 11 (`src/agents/village.py`) | A | Holds its own wells only, answers your `cfp` with `propose` or `refuse`, answers `accept-proposal` with `inform` |
| `bidding.normalise_bids` / `rank_bids` (`src/core/bidding.py`) | A | Scale raw bids to [0, 1]; rank with the alphabetical tie-break |
| `common.send` / `common.receive` (`src/agents/common.py`) | A/D | Send/receive with ontology validation, a timeout, and logging to `logs/messages.csv` |
| Toy demo `src/sim/demo_contract_net.py` | C | 1 coordinator + 3 villages, one round: **done, runs end to end** |

**How villages decide (changed 2026-10-17):**
- A village **refuses** with `"no need this month (tier Good)"` when its tier is Excellent or Good,
  and bids only when it is Poor, Very Poor or Unsuitable (`bidding.has_need`). The boundary is the
  published WQI class limit (100) from ckdu_02. With the current synthetic data about 4 of 11 villages
  bid each month, so `refuse` shows up in the log as the brief requires.
- It also refuses with `"no risk indicator available this month"` when it has no WQI yet.
- The bid is `risk × population` (risk = village WQI). **Distance is no longer in the bid.**
  Delivery cost is yours to apply (see section 3).

**Run what exists (Windows: always use `--embedded-server`, see `docs/known_issues.md`):**
```
python -m src.sim.demo_contract_net --embedded-server   # your toy demo
python -m src.sim.demo_f1_f2 --embedded-server          # A + B chain, all 11 villages answer a probe CFP
pytest
```
Agents currently cannot log in to an external `spade run` server on Windows. Until that is fixed,
everything runs in one process with the embedded server.

**Still yours to build:** `allocation.py` with the three allocators, `ResourceCoordinatorAgent`
(state machine, multi-round, `failure` + re-offer), `ClockAgent` (the demos use a stub clock),
`runner.py`, `config.py`, and `logs/allocations.csv`.

---

## 1. Files you own

| File | What it does |
|---|---|
| `src/core/allocation.py` | Pure allocation logic + the two baselines. No SPADE imports. |
| `src/agents/resource_coordinator.py` | ResourceCoordinatorAgent — runs the Contract Net. |
| `src/agents/clock.py` | ClockAgent — broadcasts the simulated "new month" tick. |
| `src/sim/runner.py` | Simulation runner — wires ticks to monthly negotiation rounds. |
| `src/sim/demo_contract_net.py` | Week-1 toy demo: 1 coordinator + 3 villages. |
| `src/sim/config.py` | Resource pool sizes, seeds, timeouts. |
| `tests/test_allocation.py` | Your tests. |

## 2. The protocol — all six performatives, no shortcuts

```
Coordinator                               VillageAgents (11)
     |                                              |
     |------------------ cfp ---------------------->|   broadcast: resource, round, deadline
     |<----------- propose / refuse ----------------|   bid, or refuse if no need this month
     |                                              |
     |--- accept-proposal (winners) --------------->|
     |--- reject-proposal (losers) ---------------->|
     |<----------- inform / failure ----------------|   village confirms it can take the resource
```

- `refuse` is not optional. A village with no need that month must refuse, and that must show up in
  the log — it is evidence the agents are autonomous rather than always bidding.
  **Implemented on the village side:** tier Excellent/Good, or no WQI, means refuse. A village that
  does not answer before the CFP deadline must still be treated as a refuse by the coordinator, and logged.
- `failure` is not optional either. Model at least one case where an accepted village cannot take
  delivery, and re-offer to the next-best bidder. That is what makes it a negotiation and not a
  ranked list.
- Multi-round: if the pool is not exhausted and villages remain unserved, issue another CFP. Count
  the rounds — **negotiation rounds to converge is one of the four graded metrics.**

## 3. Scarcity — get this right or there is nothing to evaluate

The pool **must** be genuinely scarce: fewer resources than villages that need them. With 11
villages, something like 3 RO units and 4 bowser trips per month. If every village can be served,
the Contract Net is theatre and your evaluation section has nothing to show.

**Realism anchor** from `data/raw/ckdu_05_validation_benchmarks.csv` — Horbulyk et al. 2021,
survey of ~1500 households: by 2018 only **35%** of households drank untreated groundwater while
about **50%** used RO-treated water. Use that to justify RO units as the contested resource and to
argue your pool size, rather than picking a number out of the air. Put the justification in
`src/sim/config.py` as a comment **and** in your report section.

The following resource differences are design targets from the original task specification, not a
claim about the current coordinator. The current coordinator ranks `risk × population` bids for its
configured resource and has no distance-based delivery-cost calculation.

Model the two resources differently — that is where the interesting negotiation lives:

| Resource | Character |
|---|---|
| **RO unit** | Durable, high value, serves a village for many months once placed. Allocate few, rarely. |
| **Water bowser** | Recurring monthly trips, cost scales with `distance_to_depot_km`. Allocate several, every month. |

**Current implementation status:** since 2026-10-17 the village bid no longer divides by distance
(that made remote at-risk villages bid lowest). The current `VillageAgent` retains
`distance_to_depot_km` as metadata, but neither village bids nor `ResourceCoordinatorAgent`
ranking use it; no bowser trip-cost calculation is implemented. This older task's request to apply
distance as a bowser cost remains unimplemented and does not define an agreed cost formula. Do not
assume distance affects current allocation results or introduce a formula without team agreement.

## 4. Tasks

### Week 1–2 — toy demo (part of the shared foundation, pair with D on the logger)

- [x] `src/sim/demo_contract_net.py`: 1 coordinator + 3 villages, hard-coded bids, one round.
- [x] Runs end to end over SPADE's built-in XMPP server and **writes `logs/messages.csv`**.
- [x] Verify and record the exact commands in `CLAUDE.md`:
      ```
      python -m src.sim.demo_contract_net --embedded-server
      ```
- [ ] On Windows, agents cannot log in to an external `spade run` server yet (`docs/known_issues.md`).
      The one-process-per-agent design needs it, so fix it (or move to WSL / Prosody) before week 9,
      and tell the team which you used.

### Week 3–4 — the CFP state machine

- [ ] Coordinator as an explicit state machine: `IDLE → CFP_SENT → COLLECTING → AWARDING → CONFIRMING → DONE`.
- [ ] **Every `receive()` and every CFP deadline has a timeout. Never block forever.** A village that
      goes silent must not stall the month — treat the missing reply as a refuse, and log that you did.
- [ ] Thread id per negotiation so D can reconstruct one full exchange from `messages.csv`.
- [ ] A documented tie-break for identical bids. Fixed seed, so it is reproducible.

### Week 5–6 — scarcity model and baselines

In `allocation.py`, three pure functions over the same inputs so they are directly comparable:

- [ ] `allocate_contract_net(bids, pool)` — your negotiated result.
- [ ] `allocate_equal_split(villages, pool)` — baseline 1: divide the pool evenly, ignoring risk.
- [ ] `allocate_first_come(bids, pool)` — baseline 2: serve in arrival order until the pool is empty.
- [ ] One shared metric so the comparison means something: **risk-weighted population served**
      (Σ risk × population over served villages). Agree the definition with D, who computes the
      evaluation. If the Contract Net does not beat both baselines on it, say so and investigate —
      a negative result honestly reported beats a fudged positive one.

### Week 7–8 — full negotiation

- [ ] Scale to all 11 VillageAgents and 12 simulated months.
- [ ] Carry state across months: a village with an RO unit placed last month needs less this month.
- [ ] Exercise `failure` and re-offer at least once in the demo run.
- [ ] Record per month: rounds used, messages sent, villages served, unserved high-risk villages.

## 5. Interfaces

These match `src/core/ontology.py` as of 2026-10-17. The ontology is the source of truth; if this
section and the ontology disagree, the ontology wins.

```python
# you consume
bid(risk, population) -> float              # from A; villages compute it, you receive it in `propose`
has_need(tier) -> bool                      # from A; True only for Poor / Very Poor / Unsuitable
normalise_bids(raw) / rank_bids(bids)       # from A; scale to [0, 1], alphabetical tie-break
risk_record = {"type": "risk_record", "month_index": int, "ds_division": str,
               "wqi": float | None, "tier": str, "drivers": list}           # from B
propose = {"type": "propose", "month_index", "round", "ds_division",
           "bid": float, "risk_tier": str, "population": int}             # from villages
refuse  = {"type": "refuse", "month_index", "round", "ds_division", "reason": str}
log_message(...)                            # from D; common.send / common.receive call it for you

# you publish — cfp payload (JSON, schema in src/core/ontology.py)
{"type": "cfp", "month_index": int, "round": int,
 "resource": "RO_unit" | "bowser", "units_available": int, "deadline_s": float}
# the thread id travels as the SPADE message thread, not inside the body

# you publish — logs/allocations.csv
month, resource, ds_division, round, winning_bid, method   # method: contract_net | equal_split | first_come
```

## 6. Rules that bite you specifically

- Every message between agents is logged: timestamp, sender, receiver, performative, thread id,
  payload. **The evaluation metrics are computed from that log** — an unlogged message is a hole in
  D's numbers.
- Fixed random seeds everywhere.
- Message bodies are JSON against D's schema in `src/core/ontology.py`. Do not invent a field
  locally — after the week-3 freeze, a schema change needs all four to agree.
- `src/core/allocation.py` must be testable **without XMPP**. The three allocators are plain
  functions over plain data.

## 7. Before you say a task is done

```
pytest -q
python -m src.sim.demo_contract_net --embedded-server
```
(Once the external-server issue is fixed: `spade run --host localhost --memory` in terminal 1, then
the demo without `--embedded-server` in terminal 2.)
Then show `logs/messages.csv` — the actual rows, with all six performatives present. Show the
command and its output, not a claim that it works.

## 8. Prompts that work in Claude Code

- `Read CLAUDE.md and docs/tasks/member_c_contract_net.md. Implement src/core/allocation.py with all three allocators and pytest tests. Pure functions only, no SPADE.`
- `Implement the coordinator state machine in src/agents/resource_coordinator.py with a timeout on every receive and every CFP deadline. Use the schema in src/core/ontology.py.`
- `Run the toy demo and show me logs/messages.csv. Confirm cfp, propose, refuse, accept-proposal, reject-proposal and inform all appear.`
