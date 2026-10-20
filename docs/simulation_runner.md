# Multi-month SPADE simulation runner

`python -m src.sim.runner` advances the existing SPADE agents one month at a time. It starts
`WellMonitorAgent`, `RiskScoringAgent`, one `VillageAgent` for each division in the configured
sampling-design file, a run-local risk-record collector, `HotspotAgent`, `AlertProcessingAgent`,
`AlertInboxAgent`, and `ClockAgent`. For each tick, the runner
waits for the monitor, scorer, collector, and every village to show current-month state before it
starts the existing single-round `ResourceCoordinatorAgent`. If current-month risk delivery is
incomplete, that month's Contract Net round is skipped; the runner does not allow villages to bid
from stale risk.

The runner's calendar mapping is supplied explicitly using one calendar label per simulated
`month_index`. The `month` field in `month_tick` is a 1–12 calendar month and `month_index` is the
simulation counter. Neither is derived from the other by the runner. The source seasonal values are
expanded using the existing `wells.expand_to_months` behavior; repeated seasonal rows are not new
monthly measurements.

## Isolated outputs

The caller must provide a new or empty output directory. The runner refuses production `logs/`,
raw/processed data, source, dashboard, fixtures, the existing `demo_output/` bundle, and any
non-empty target. Outputs include:

- `run_manifest.json`: provenance, configuration, agent lifecycle events, month completion, round
  paths, message row counts, stage statuses, failures, and evidence-gated evaluator statuses.
- `agent_events.jsonl`: append-only runner lifecycle, month/round/hotspot/alert events plus a
  two-second runner heartbeat. It is separate from, and does not change, the seven-column message
  CSV. Heartbeats establish runner-process freshness, not per-behaviour health.
- `logs/messages.csv`: the shared seven-column typed directional message log, isolated to this run.
- `logs/risk_scores.csv` and `logs/well_scores.csv`: outputs from the existing risk output writer.
- `logs/evaluation_summary.csv`: existing evaluator output. Missing references, baselines, and event
  correlation remain unavailable.
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
endpoint. The new `hotspot_result` and `alert_ack` schemas use the existing `inform` performative;
they do not alter the logger's seven columns. Duplicate, gapped, out-of-order, malformed, missing,
or Unknown evidence remains unavailable. The configurable rule is project-defined screening, not a
validated medical or epidemiological model.

This runner implements local SPADE alert transport to its simulated inbox and persists events for
the dashboard Agent Monitor, but not a deployed health-officer endpoint or multi-round Contract Net
convergence. Alert tiers
without an approved policy remain unavailable. WQI uses the project's reduced parameter set and is
not diagnostic or causal evidence about CKDu.

`dashboard/app.py` exposes an **Agent Monitor** source when selected. Set
`CKDU_SIMULATION_RUN_DIR` to one isolated output directory from this runner. It reads that run's
event journal, final manifest, message log, round files, hotspot results, and alert outcomes; it does
not infer a run from `logs/`, fixture files, or `CKDU_DEMO_OUTPUT_DIR`. While the manifest is running,
freshness is based on recent journal/message evidence and the dashboard fragment refreshes every two
seconds. Once `run_finished` is recorded, the same view is a persisted snapshot. A started lifecycle
event plus fresh runner activity indicates a started agent process; it does not prove each behaviour
is healthy or that an external XMPP service is reachable. Missing or stale evidence is shown as
unknown/stale.

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
