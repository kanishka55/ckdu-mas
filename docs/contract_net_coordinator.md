# Production Contract Net coordinator (single-round increment)

`src/agents/resource_coordinator.py` implements one bounded SPADE Contract Net round. It is a
distributed coordinator that exchanges ontology-validated messages with `VillageAgent`; it is not
the toy demo and it is not the illustrative, in-process allocator in `src/core/local_allocation.py`.
An external clock or runner can construct a configured coordinator per round. Monthly scheduling,
state carry-over, failure re-offers, and a complete multi-month simulation remain separate work.

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
`unconfirmed_timeout`. This increment records failure and timeout but does not re-offer a failed
unit. The Member C task specification requires failure re-offer and multi-round convergence; those
remain explicit follow-up work rather than being silently claimed here.

In the JSON result, `selected_winner` is the highest-ranked proposed awardee. `winner` is set only
after an `inform_done` confirms an award. `allocation_successful` is true only when every selected
award in that round is confirmed; per-award states and `confirmed_awardees` preserve partial outcomes.

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
