# Multi-month SPADE simulation runner

`python -m src.sim.runner` advances the existing SPADE agents one month at a time. It starts
`WellMonitorAgent`, `RiskScoringAgent`, one `VillageAgent` for each division in the configured
sampling-design file, a run-local risk-record collector, `HotspotAgent`, `AlertProcessingAgent`,
`AlertInboxAgent`, and `ClockAgent`. For each tick, the runner
waits for the monitor, scorer, collector, and every village to show current-month state before it
starts the `ResourceCoordinatorAgent` (one per round; see below). If current-month risk delivery is
incomplete, that month's Contract Net round is skipped; the runner does not allow villages to bid
from stale risk.

The runner accepts only `source=synthetic` input and records that provenance in the manifest. The
resulting simulation is illustrative, not a verified team or real-world result. The runner's
calendar mapping is supplied explicitly using one calendar label per simulated `month_index`. The
`month` field in `month_tick` is a 1–12 calendar month and `month_index` is the simulation counter.
Neither is derived from the other by the runner. The source seasonal values are expanded using the
existing `wells.expand_to_months` behavior; repeated seasonal rows are not new monthly measurements.

## Full 12-month run

```
python -m src.sim.runner --end-month-index 11 --calendar-months 10,11,12,1,2,3,4,5,6,7,8,9   --output-dir <new empty dir> --resource-pool 3   --hotspot-trigger-tier Poor --hotspot-consecutive-months 2   --simulate-delivery-failure Talawa:0 --embedded-server
```
Drop `--embedded-server` to use an external `spade run --host localhost --memory`. Options added on
2026-10-25:
- `--max-rounds N` (default 3): extra CFP rounds in a month only re-offer units freed by a `failure`
  or an unconfirmed award. `1` gives the old single-round behaviour.
- `--simulate-delivery-failure DIVISION:MONTH_INDEX` (repeatable): a **simulated scenario** in which that
  village answers its award with `failure`, to exercise re-offer. It is recorded in
  `run_manifest.json` under `configuration.simulated_delivery_failures`.

Contract Net behaviour (re-offer, RO carry-over, statuses) is described in
`docs/contract_net_coordinator.md`. The `contract_net` stage value is now
`bounded_rounds_per_completed_month`.

## Baselines and the allocation comparison

The runner writes `logs/allocations.csv` with three methods for every month the Contract Net ran:
`contract_net` (confirmed awards from all rounds), and `equal_split` and `first_come` computed by
`src/core/allocation.py`. Both baselines use the same pool and the same durable-unit rule (each method
keeps its own RO holdings), and both skip a village that cannot take delivery that month.
- `equal_split` ignores risk: a round-robin over villages in name order, continuing month to month.
- `first_come` serves villages that have a need (tier Poor or worse) in the order their round-1
  replies reached the coordinator, taken from the message log. In one process the coordinator
  sends CFPs in name order and the replies arrive in that order, so in practice this is close to
  alphabetical.

Risk weight for "risk-weighted population served" = **need-gated village WQI**: the WQI if the tier
is Poor or worse, else 0 (`allocation.risk_weight`). Plain WQI was tried first and rejected: it let
equal split "win" by serving large Good-tier villages (e.g. Kandy) that had no need. This is a
project definition, not a published weight.

The runner attests (`evaluated_month_resources`) that every method was computed for each Contract Net
month, so a method that placed no unit in a month scores 0 instead of `unavailable`. The evaluator
also reports **run totals** (scope `all;resource=...`). For durable RO units the totals are the
meaningful comparison: after the first months the Contract Net correctly places nothing new.

## Run status and evaluation availability

The top-level manifest status describes operational execution. `completed` means
the configured required stages produced their operational outcomes without a
recorded execution failure. An expected initial `expected_warmup` result for a
configured multi-month hotspot rule, and alert unavailability caused by that
warm-up, do not make the run operationally incomplete. They remain explicit in
the per-month stage statuses, hotspot and alert artifacts, and dashboard result
explanations. Unexpected missing risk records, incomplete screening or alert
processing, failed alert delivery, an unsuccessful required Contract Net round,
or recorded execution/cleanup failures keep the run at
`completed_with_unavailable_stages` or `failed`, as appropriate.

Evaluation availability is reported separately in `evaluation_status` and
`logs/evaluation_summary.csv`. Missing independent reference tiers or approved
allocation baselines/risk-weight provenance can leave metrics unavailable while
the simulation is operationally `completed`. This does not mean scientific
validation or complete evaluation evidence; the dashboard continues to list
each unavailable metric and its missing evidence.

## UI Simulation Lab

The dashboard's **Simulation Lab** page uses the same `src.sim.runner` SPADE runner. Select the
built-in `data/processed/synthetic_wells.csv` sample or upload a CSV with exactly the existing
seasonal input columns from `src.core.wells.READING_COLUMNS` with `month` omitted:
`well_id,ds_division,district,ckdu_prevalence_class,season,source,pH,EC_uS_cm,alkalinity_mg_L,hardness_mg_L,F_mg_L,DOC_mg_L,Cl_mg_L,SO4_mg_L,Fe_ug_L`.
The page previews the file and validates it with `src.sim.load_data.load_monthly_readings` before
launch. The existing runner requires every source label to be `synthetic`; real or mixed-source
uploads are rejected. A selected upload is copied unchanged into the isolated lab input directory
and its exact path is passed to the runner.

Configure `month_index` range, the explicit first calendar month label, resource, integer pool,
and timeouts in the page. The subsequent calendar labels are passed explicitly as the normal
12-month calendar sequence; the UI does not equate a simulated index with a calendar month. The
optional hotspot controls are user-supplied provisional screening settings; leaving them disabled
does not substitute a default rule. Starting a run launches the existing runner in a background
process. Only one queued/running lab simulation is allowed at a time, each run gets a new directory,
and the runner still enforces its fresh-output protections. The page displays run status and bounded
process-log details for failures.

Compose sets `CKDU_SIMULATION_LAB_DIR=/app/simulation_lab` and mounts the configurable host
`CKDU_SIMULATION_LAB_HOST_DIR` there for persistent uploads, control records, process logs, and run
directories. The **Live Coordination** page lists those lab runs as well as the separately configured
`CKDU_SIMULATION_RUN_DIR` run. Select the lab run to refresh its persisted event journal, agent
heartbeats/status, seven-column messages, round results, hotspot and alert artifacts. The UI shows
runner observations separately from recorded agent behavior and logged SPADE messages. A heartbeat
is liveness evidence only, and the local alert acknowledgement is not delivery to a real health
officer. Allocation/evaluation metrics lacking provenance or required inputs remain unavailable.

The Simulation Lab run-history table is row-selectable. Selecting the run launched by the page opens
its live execution view immediately. The view shows run progress, one compact card per observed or
configured agent identity, latest recorded activity/input/result, and a collaboration graph. Solid
graph arrows come from sent rows in the seven-column SPADE message log; dashed arrows represent
runner/input orchestration and are not messages. The current runner fans risk evidence to village,
collector, hotspot, and alert agents, waits for risk/hotspot/alert stages, then starts Contract Net.
The order reflects the runner implementation, rather than an assumed linear agent chain.

Active selected runs refresh every two seconds; completed and failed runs are snapshots. The result
summary shows risk by division, selected versus confirmed awards, hotspot evidence month history,
and alert delivery/acknowledgement state. Alert acknowledgements mean receipt by the simulated inbox
only. Missing outputs and unavailable evaluation metrics remain unavailable. Expand **Technical
details** for the filtered timeline, full seven-column message rows and payloads, month artifacts,
round details, and evaluator summary. Sent/received rows remain distinct because the logger has no
unique message ID; the UI does not merge rows based on payload similarity.

Optional technical per-agent sections summarize only recorded identities and events. A missing
startup, heartbeat, phase, work outcome, or message remains unknown/unavailable. Runner stage
summaries are labeled separately from agent events and SPADE messages. Month and Contract Net
inspectors use round artifacts and typed messages when present; selected awardees remain distinct
from confirmed awardees. The displayed ranking policy follows the current implementation: bids are
normalized by the maximum, ranked highest first, and ties are alphabetical by division name.
Evaluation metrics are read from the run's
`logs/evaluation_summary.csv`; allocation comparisons stay unavailable unless required provenance,
baselines, population/risk inputs, and an approved risk-weight definition are available. Alert
latency uses the explicit emitted-decision bridge from each unique `alert_thread_id` to its original
`source_thread_id`, division, and `month_index`, and selects the received risk record at
`AlertProcessingAgent`. The measured interval is between the logger's UTC append timestamps for
that receipt and the sent alert; it is not a sample-collection-time metric. Runs without a unique
bridge remain unavailable. The evaluator summary and manifest include the availability category,
missing dependency, reason, and required action for unavailable metrics.

## Isolated outputs

The caller must provide a new or empty output directory. The runner refuses production `logs/`,
raw/processed data, source, dashboard, fixtures, the existing `demo_output/` bundle, and any
non-empty target. Outputs include:

- `run_manifest.json`: provenance, configuration, agent lifecycle events, month completion, round
  paths, message row counts, stage statuses, failures, and evidence-gated evaluator statuses.
- `agent_events.jsonl`: append-only runner lifecycle, month/round/hotspot/alert events plus a
  two-second runner heartbeat and periodic per-agent SPADE behavior-loop heartbeats. Rows include
  UTC `timestamp`, monotonic-in-run `event_seq`, `event_type`, `component`, `run_id`, and an
  `identity` for agent-specific events; applicable events also include `month_index`, `round`,
  `thread_id`, `phase`, `outcome`, or `reason`. `agent_starting` is emitted before `start()`;
  `agent_started` only after it returns successfully. `agent_work_completed` records that the
  runner finished that agent's assigned run work; its `outcome` can still indicate unavailable
  stages. `agent_stopped` records process shutdown only and never proves successful work by itself.
  Coordinator `agent_waiting` events identify proposal or confirmation collection phases. Uncaught
  registered-behavior exceptions are recorded as `agent_behavior_failed` and re-raised. The journal
  is separate from, and does not change, the seven-column message CSV. A per-agent heartbeat proves
  that a heartbeat behavior ran in that SPADE agent's event loop recently; it does not prove that
  domain calculations are correct, external XMPP is healthy, or a particular workflow stage
  succeeded. Event ordering uses `event_seq`; readers continue to accept legacy rows without it.
- `logs/messages.csv`: the shared seven-column typed directional message log, isolated to this run.
- `logs/allocations.csv`: confirmed Contract Net awards only, using the evaluator's existing
  `month,resource,ds_division,round,winning_bid,method` schema. Calendar `month` is taken from
  the explicitly configured map. The evaluator receives population values from the configured
  division input when present. The manifest marks the awards as synthetic run evidence, with no
  baselines and no team-output attestation; this export does not enable allocation comparisons.
- `logs/risk_scores.csv` and `logs/well_scores.csv`: outputs from the existing risk output writer.
- `logs/evaluation_summary.csv`: existing evaluator output. Risk alignment remains unavailable
  without an independent sourced reference-tier mapping. Allocation comparisons remain unavailable
  without comparable baselines, population inputs, provenance, and an approved risk-weight formula.
  Alert latency is available only when the persisted risk-to-alert bridge and unique message events
  support a pair. Missing dependencies are not reported as zero.
- `rounds/month_index_XXXX_round_N.json`: one result from the existing bounded SPADE coordinator per
  completed month.
- `risk_records.json`, `hotspot_screening.json`, `alert_decisions.json`: typed risk records,
  screening results, policy decisions, and alert transport states.

`RiskScoringAgent` sends independent `risk_record` copies to the collector, `HotspotAgent`, and
`AlertProcessingAgent`, in addition to each division's `VillageAgent`. The hotspot agent reuses the
pure screening helper, persists `hotspot_screening.json`, and sends an ontology-validated
`hotspot_result` on the matching risk thread. The alert processor requires matching division,
`month_index`, and thread before applying the existing decision/suppression helper. An issued typed
`alert` goes to the run-local `AlertInboxAgent`, which responds with `alert_ack` on the same thread.
The acknowledgement confirms receipt at that local process only, not at a real health-officer
endpoint. External XMPP deployment and real-world alert delivery are not verified. The new
`hotspot_result` and `alert_ack` schemas use the existing `inform` performative;
they do not alter the logger's seven columns. Duplicate, gapped, out-of-order, malformed, missing,
or Unknown evidence remains unavailable. The configurable rule is project-defined screening, not a
validated medical or epidemiological model.

This runner implements local SPADE alert transport to its simulated inbox and persists events for
the dashboard Agent Monitor, but not a deployed health-officer endpoint or multi-round Contract Net
convergence. Alert tiers without an approved policy remain unavailable. WQI uses the project's
reduced parameter set and is not diagnostic or causal evidence about CKDu. A rule requiring multiple
consecutive months cannot produce an available screening result until that full history is present;
for the two-month example, the first simulated month is unavailable during warm-up.

`dashboard/app.py` exposes an **Agent Monitor** snapshot and a separate **Live Coordination** page
when selected. Set `CKDU_SIMULATION_RUN_DIR` to one isolated output directory from this runner. Both
views use the same monitor loader and read that run's event journal, manifest, message log, round
files, hotspot results, and alert outcomes; neither infers a run from `logs/`, fixtures, or
`CKDU_DEMO_OUTPUT_DIR`. Live Coordination refreshes every two seconds when Streamlit fragment
support is available. It shows the recent event timeline, per-agent heartbeat/phase evidence, the
unmodified seven-column message rows, month/round progress, and recorded hotspot and alert outcomes.
Timeline rows identify runner stage observations separately from agent behavior events; only the
directional message CSV represents logged SPADE send/receive operations. No missing correlation is
reconstructed.

While the manifest says running, run freshness requires a recent runner heartbeat. Per-agent
`running` and `waiting` require a recent heartbeat from that identity; `waiting` additionally
requires an unresolved recorded workflow phase. `completed` requires explicit
`agent_work_completed`; clean shutdown alone maps to unknown if work completion was not recorded.
Behavior/start/stop failures map to failed. Once `run_finished` is recorded, the page labels the run
as a persisted snapshot, never live. Heartbeats are liveness evidence only, not a behavior health
check or proof that external XMPP is reachable. Missing or stale evidence is shown as unknown/stale.
Older journals remain readable, but without per-agent heartbeat and work-completion events their
agent states remain unknown rather than being inferred from message freshness.

Example: two simulated months, with explicit calendar labels and a fresh output path:

```sh
python -m src.sim.runner \
  --start-month-index 0 --end-month-index 1 --calendar-months 10,11 \
  --output-dir /tmp/ckdu-simulation-run \
  --resource RO_unit --resource-pool 1 --embedded-server
```

For a configured screening rule, provide both `--hotspot-trigger-tier` (repeatable) and
`--hotspot-consecutive-months`. `--hotspot-id` selects the agent identity, and
`--hotspot-stage-timeout` configures the finite wait for its current-month risk fan-out.
`--alert-processing-id`, `--alert-inbox-id`, `--alert-receive-timeout`, `--alert-stage-timeout`,
and `--alert-acknowledgement-timeout` configure the local alert lifecycle. No screening rule is
enabled by default. Do not use the generated round outcomes as verified Member C or team allocation
results until their provenance is reviewed.

## Docker Compose path mapping

`CKDU_SIMULATION_RUN_HOST_DIR` is a **host-shell variable** used by Compose to select the bind-mount
source. It must be set before invoking `docker compose`; passing it with `docker compose run -e`
changes only a container environment variable and does not change the host mount. Compose mounts that
host directory at `/app/simulation_run` in both services. Inside the dashboard container,
`CKDU_SIMULATION_RUN_DIR=/app/simulation_run` selects the same mounted run. The runner's
`--output-dir` is also a **container path**, so use `/app/simulation_run` there.

Use a fresh host directory and reuse the same variable for the runner and dashboard. This macOS
example creates a new temporary directory, runs two simulated months, and then starts the monitor
on the completed persisted snapshot:

```sh
RUN_HOST_DIR="$(mktemp -d /private/tmp/ckdu-simulation.XXXXXX)"
export CKDU_SIMULATION_RUN_HOST_DIR="$RUN_HOST_DIR"

docker compose run --rm --no-deps demo-runner python -m src.sim.runner \
  --start-month-index 0 --end-month-index 1 --calendar-months 10,11 \
  --output-dir /app/simulation_run --resource RO_unit --resource-pool 1 \
  --proposal-timeout 2 --confirmation-timeout 2 --stage-timeout 30 \
  --embedded-server --hotspot-trigger-tier Poor \
  --hotspot-trigger-tier "Very Poor" --hotspot-trigger-tier Unsuitable \
  --hotspot-consecutive-months 2

docker compose up -d dashboard
```

The tier-persistence rule above is explicitly supplied for this run; it is provisional screening,
not a validated epidemiological model. Keep the generated host directory. The runner refuses
non-empty output roots, and the dashboard should use that same host directory to inspect the
persisted snapshot. `RUN_HOST_DIR` is just a shell variable; retain it in the shell where you start
the dashboard, or export `CKDU_SIMULATION_RUN_HOST_DIR` again to the same host path.

To observe the run while it executes, start the dashboard first in terminal 1:

```sh
export CKDU_SIMULATION_RUN_HOST_DIR="$(mktemp -d /private/tmp/ckdu-live-run.XXXXXX)"
printf 'Simulation run host directory: %s\n' "$CKDU_SIMULATION_RUN_HOST_DIR"
docker compose up -d dashboard
```

In terminal 2, export `CKDU_SIMULATION_RUN_HOST_DIR` to the exact path printed in terminal 1, then
run the same two-month `docker compose run ... python -m src.sim.runner` command above. The runner
writes to `/app/simulation_run` through its read/write bind mount; the dashboard reads that same
host directory at `/app/simulation_run` through a read-only mount. Open `http://localhost:8501`,
select **Live Coordination**, and watch the two-second refresh. After `run_finished` is recorded, the
page reports a completed snapshot. `docker compose down` stops the dashboard and preserves the host
run directory. Do not reuse a non-empty run directory or use different host paths in the two
terminals.
