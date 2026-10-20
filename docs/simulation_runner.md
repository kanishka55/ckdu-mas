# Multi-month SPADE simulation runner

`python -m src.sim.runner` advances the existing SPADE agents one month at a time. It starts
`WellMonitorAgent`, `RiskScoringAgent`, one `VillageAgent` for each division in the configured
sampling-design file, a run-local risk-record collector, and `ClockAgent`. For each tick, the runner
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
- `logs/messages.csv`: the shared seven-column typed directional message log, isolated to this run.
- `logs/risk_scores.csv` and `logs/well_scores.csv`: outputs from the existing risk output writer.
- `logs/evaluation_summary.csv`: existing evaluator output. Missing references, baselines, and event
  correlation remain unavailable.
- `rounds/month_index_XXXX_round_N.json`: one result from the existing bounded SPADE coordinator per
  completed month.
- `risk_records.json`, `hotspot_screening.json`, `alert_decisions.json`: typed risk records and
  outputs from the existing screening and alert decision functions.

This runner does not implement a `HotspotAgent`, live SPADE alert transport, multi-round Contract
Net convergence, or an Agent Monitor. The hotspot module is only a configurable project-defined
screening rule, not a validated medical or epidemiological model. The alert code is a decision
function; its issued-alert log rows do not establish that a live health-officer endpoint received
them. `Unknown`, unavailable screening, and alert tiers without an approved policy remain
unavailable. WQI uses the project's reduced parameter set and is not diagnostic or causal evidence
about CKDu.

Example: two simulated months, with explicit calendar labels and a fresh output path:

```sh
python -m src.sim.runner \
  --start-month-index 0 --end-month-index 1 --calendar-months 10,11 \
  --output-dir /tmp/ckdu-simulation-run \
  --resource RO_unit --resource-pool 1 --embedded-server
```

For a configured screening rule, provide both `--hotspot-trigger-tier` (repeatable) and
`--hotspot-consecutive-months`. No screening rule is enabled by default. Do not use the generated
round outcomes as verified Member C or team allocation results until their provenance is reviewed.
