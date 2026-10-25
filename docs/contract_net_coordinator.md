# Production Contract Net coordinator

`src/agents/resource_coordinator.py` implements one bounded SPADE Contract Net round. It is a
distributed coordinator that exchanges ontology-validated messages with `VillageAgent`; it is not
the toy demo and it is not the illustrative, in-process allocator in `src/core/local_allocation.py`.
The runner (`src/sim/runner.py`) constructs one configured coordinator per round. A month can have
several rounds: later rounds re-offer units freed by a `failure` (see "Failure and re-offer" below).

## Inputs and correlation

`CoordinatorConfig` requires a simulated `month_index`, integer `round_number`, ontology resource,
integer resource pool, explicit village JID/division pairs, positive proposal and confirmation
timeouts, and a JSON result path. It accepts an optional SPADE thread ID; otherwise the agent makes
one UUID-based thread ID when initialized. The ontology `month_index` is never converted into a
calendar month. Every response must match the thread, month index, round, and configured sender to
division mapping. The ontology bodies remain unchanged; the SPADE message thread carries the
correlation identity.

## Decision rule and provisional resource model

Only ontology-valid proposals for the configured month and round with a matching recipient,
positive finite bid and population, and a `Poor`, `Very Poor`, or `Unsuitable` tier are eligible.
`Unknown`, missing, safe-tier, stale, malformed, and non-positive proposals are not ranked. Valid
but ineligible proposals, malformed replies, and wrong-thread/round replies are recorded in
separate result lists. Refusals are recorded separately and never ranked. Eligible bids are normalized and ranked with
`src/core/bidding.py`; equal bids use its alphabetical `ds_division` tie-break.

The pool is an integer count of identical, indivisible units. The provisional rule is at most one
unit per village per round; at most `resource_pool` highest-ranked eligible villages receive an
accept message. This simplifies the existing `accept_proposal` contract, which gives an integer
unit count but does not specify multi-unit need or fairness. It is a coordinator implementation
choice and still needs Member C/team review. A zero pool is valid and results in no award.

An accept message is a selected offer, not a completed allocation. An award becomes successful
only after a correlated ontology-valid `inform_done` confirms the configured resource and one unit.
An ontology-valid `failure` is recorded as failed; a missing confirmation becomes
`unconfirmed_timeout`. One coordinator instance runs one round; re-offering is done by the runner
starting another round (next section).

In the JSON result, `selected_winner` is the highest-ranked proposed awardee. `winner` is set only
after an `inform_done` confirms an award. `allocation_successful` is true only when every selected
award in that round is confirmed; per-award states and `confirmed_awardees` preserve partial outcomes.

## Failure and re-offer (multi-round), carry-over between months (added 2026-10-25)

- **Re-offer.** After a round, the runner computes the units still free (`pool − confirmed`). If any
  award in that round ended `failed` or `unconfirmed_timeout`, it starts the next round: a new
  coordinator (`coordinator_m<month>_r<round>`), a new thread id, `round = previous + 1`,
  `units_available` = the freed units. At most `--max-rounds` rounds per month (default 3). Round
  files are `rounds/month_index_XXXX_round_N.json`.
- **Who bids in a re-offer round.** Villages decide for themselves (`src/agents/village.py`): one that
  already received the resource this month refuses ("already received a … this month"), one that
  failed delivery this month refuses ("cannot take delivery this month"), and RO holders refuse (next
  point). So the freed unit goes to the next-best bidder.
- **Carry-over.** An RO unit is durable (`bidding.DURABLE_RESOURCES`). A village that confirmed an RO
  unit keeps it and refuses later RO CFPs ("already holds an RO_unit (placed month_index N)").
  Bowsers are recurring, so there is no carry-over for them. Once every village with a need holds an RO
  unit, later months end `no_award`, which is a valid outcome.
- **Failure is a simulated scenario.** `--simulate-delivery-failure DIVISION:MONTH_INDEX` makes that
  village answer `accept-proposal` with `failure` ("simulated scenario: site cannot take delivery
  this month"). Nothing in the data produces failures on its own. The flag exists to exercise the protocol.
- **Month status** (`month_status[].contract_net`): one round gives the coordinator's own status. After a
  re-offer, `confirmed_after_reoffer` means the last round confirmed, and `reoffer_no_eligible_bidder`
  means nobody else needed the freed unit. `rounds` and `round_award_statuses` give the detail.

## Persistence and provenance

Each round writes one JSON result to the caller-provided `output_path`, using exclusive creation so
an existing file is never replaced. The document records month index, round, resource and pool,
thread ID, received proposals/refusals, ineligible proposals, malformed and unmatched replies, missing/timed-out
recipients, selected awardees and their confirmation statuses, errors, state history, and a caller
supplied provenance string. This is not `logs/allocations.csv`: that evaluator input requires a
calendar `month`, and the current coordinator contract provides only `month_index`. No calendar
mapping or allocation provenance convention is invented here.

SPADE messages continue through `src/agents/common.py`, which applies the shared typed ontology and
appends the existing seven-column sent/received logger. The structured result is separate from that
message log. Configure the process working directory/log path appropriately when running this
agent; the shared common send API currently uses the logger's default `logs/messages.csv` path.
