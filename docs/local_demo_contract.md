# Local synthetic demo contract (proposal)

This document defines an isolated local-demo boundary. It does not approve hotspot science, alert
policy, allocation policy, or scientific evaluation. Rows produced by the local demo must be labelled
illustrative/synthetic and must not be represented as verified team results.

## Output location and provenance

The runner `python -m src.sim.run_local_demo` defaults to `demo_output/` and accepts a configurable
`--output-dir`. The target must be new or empty. It refuses roots within repository `logs/`,
`data/raw/`, or `data/processed/`. Artifacts use `demo_output/logs/` for compatible risk, message,
well-score and evaluation filenames. `demo_output/provenance.json` records synthetic source,
configuration, rule identifiers, limitations and stage statuses. The runner does not overwrite
existing output files.

The dashboard supports `CKDU_DEMO_OUTPUT_DIR` as a separate generated-demo view. It reads artifacts
under that root and does not route them through `CKDU_LOGS_DIR`. The view uses the published
division sampling-design file only for its approximate division town points; it does not infer well
locations. Existing team-output and illustrative-fixture views remain separate.

## Existing output contracts to reuse

| Proposed demo file | Contract confirmed in current code | Provenance and notes |
|---|---|---|
| `logs/risk_scores.csv` | Rows from `src/sim/risk_outputs.py`; the dashboard requires `month_index, ds_division, wqi, tier, drivers, source`. B's detail also supplies `month`, `driver_contributions`, `n_wells`, `n_scored`, `share_poor_or_worse`, `worst_tier`, `weight_covered`, `params_used`, `n_rejected`, `rejected`, and `error`. | Preserve `source=synthetic`; `month_index` is the simulation counter. Do not convert it to calendar `month` implicitly. `Unknown` stays `Unknown`. |
| `logs/well_scores.csv` | Flattened `well_risk` rows: `month_index, month, ds_division, district, ckdu_prevalence_class, source, well_id, wqi, tier`, followed by available `q_<parameter>` columns. | `month` is calendar month; `month_index` is simulated month. The 440 source readings are synthetic; seasonal values are repeated across their assigned months, not newly observed. |
| `logs/messages.csv` | Exact logger columns and order: `timestamp,sender,receiver,performative,thread_id,payload,direction`; `direction` is `sent` or `received`. Payloads use `src/core/ontology.py` typed schemas. | Use the existing logger with an output path under the demo directory. Record only actual emitted messages; internal suppressed/unavailable alert decisions are not delivered alerts. |
| `logs/allocations.csv` | Evaluator columns: `month,resource,ds_division,round,winning_bid,method`; methods are `contract_net`, `equal_split`, `first_come`. `month` is calendar month 1–12. | This schema is implemented as a reader in `src/sim/evaluate.py`, but no full coordinator/allocation output writer is present. Demo rows must be explicitly illustrative and must not be attested as verified team outcomes. |
| `logs/evaluation_summary.csv` | Evaluator columns: `metric,scope,value,unit,status,observations,details`. | Generate through `src/sim/evaluate.py` with all unsupported mappings and attestations omitted. Unavailable metrics remain blank-valued with `status=unavailable`; do not replace them with zero. |

The implemented pure adapter is `src/core/hotspot_screening.py`. Its
`screen_risk_history(risk_records, ds_division, month_index, rule)` interface consumes only strict
typed `risk_record` bodies. `ScreeningRule` requires an explicit `rule_id`, non-`Unknown`
`trigger_tiers`, and `consecutive_months`; the caller supplies all policy choices. The
`ScreeningResult` schema is `status` (`active` | `inactive` | `unavailable`), `ds_division`,
`month_index`, `rule_id`, `evidence_month_indexes`, and optional `reason`. Its `alert_hotspot`
adapter returns `bool | None` for the current alert API. Unknown tier/WQI, missing month history,
duplicates, and malformed input return `unavailable`. This internal result is not an ontology
message type. A future CSV writer, if needed, should use the same fields plus explicit provenance;
it must not add a hotspot message family to the shared ontology without team review.

## Reproducible run flow and integration seams

The proposed runner should use this sequence, with path arguments injected into each writer:

1. Read `data/processed/synthetic_wells.csv` and `data/raw/ckdu_03_sampling_design_ds_divisions.csv`
   through `src/sim/load_data.py`; require `source=synthetic` for this demo. Read the WQI method and
   class data from `data/raw/ckdu_02_wqi_method_and_thresholds.csv`.
2. Expand the existing two seasonal readings into the project's documented month sequence with
   `src/core/wells.py`; the seasonal readings are repeated, not interpolated.
3. The standalone `src/sim/run_local_demo.py` runner reuses `src/sim/load_data.py`,
   `src/core/wqi.py`, `src/core/risk_tiers.py`, and `src/sim/risk_outputs.write_outputs(...)` using
   demo paths; it does not instantiate SPADE agents. The separate multi-agent `src/sim/runner.py`
   starts the production-style SPADE agents, including `HotspotAgent`, using its own fresh output
   root. If pandas or the method/input is unavailable in the standalone demo, risk and well score
   files have headers only and the manifest records the scoring stage as unavailable.
4. The live SPADE `AlertProcessingAgent` receives scorer risk copies and typed hotspot results. It
   pairs only matching division, `month_index`, and source thread, then delegates policy and
   suppression to `AlertAgent.process_risk_record()`. Unavailable hotspot evidence and `Unknown`
   remain non-deliverable. Current wording supports `Excellent`, `Good`, and `Poor`; `Very Poor` and
   `Unsuitable` remain unavailable pending policy approval. Issued alerts go to a run-local
   simulated inbox, not a deployed health-officer endpoint.
5. The runner invokes `src/core/local_allocation.py` for illustrative detailed decisions. It does not
   write the evaluator's team `allocations.csv` format or claim to execute Member C's full protocol.
6. The runner invokes `src/sim/evaluate.evaluate()` with demo message and summary paths, but leaves
   the evaluator allocation input absent and omits mappings, attestations, history claims and latency
   selectors. Unsupported metrics therefore remain unavailable.
7. `dashboard/app.py` reads the generated artifacts only in the explicit generated local-demo view.

## Decisions required before implementing demo rules

### Hotspot screening

`src/agents/hotspot.py` subscribes to the scorer's typed `risk_record` fan-out and reuses
`src/core/hotspot_screening.py`. It stores history by division and simulated month, rejects
duplicates and out-of-order records, and returns unavailable for gaps, Unknown values, and
insufficient history. The strict `inform` `hotspot_result` body is implemented in
`src/core/ontology.py`: `month_index:int`, `ds_division:str`, `status` (`active` | `inactive` |
`unavailable`), `rule_id:str|null`, `evidence_month_indexes:list[int]`, and `reason:str|null`.
This transport extension supports the live local simulation; Member B review is still needed for
the screening semantics. The rule is not a validated medical or epidemiological model.

The live alert adapter sends typed `alert` messages to a run-local inbox and accepts typed
`alert_ack` messages (`month_index`, `ds_division`) on the same thread. The inbox acknowledgement
confirms receipt by that local process only, not a real health officer or external service. Message
logs continue to use the shared seven-column schema and record sent/received message copies.

### Contract Net and baselines

`src/sim/demo_contract_net.py` is a small SPADE toy with hard-coded risk values. The actual pure
allocation module, full coordinator, runner, config, and allocation CSV writer listed in Member C's
specification are absent. A local adapter must not be called the full FIPA Contract Net unless it
executes and logs the required CFP/propose/refuse/accept/reject/inform/failure exchange. Before
coding, Member C or the project owner must settle resource units and pool sizes, multi-round/failure
behavior, equal-split handling of indivisible resources, first-come arrival order, and whether the
existing WQI value is the agreed scalar in the documented `risk × population` comparison. The
evaluator requires all three method rows, population and risk-weight mappings, a verified-results
attestation, and an explicit risk-weight definition before it calculates comparisons.

#### Increment 2 local allocation interface (illustrative)

The pure module `src/core/local_allocation.py` is a demo adapter, not Member C's implementation or
the full FIPA protocol. Its input is:

- a sequence of ontology-valid `risk_record` bodies for one `month_index`;
- `population_by_division: Mapping[str, int]` (missing values stay unavailable);
- one resource from the ontology's `RO_unit`/`bowser` set;
- a configurable non-negative integer `resource_pool_size`;
- an optional `arrival_order` permutation for the first-come baseline.

The provisional resource model is a pool of identical indivisible units, with at most one unit per
division per resource/month. This is a local simplification; it does not model RO durability,
bowser distance costs, delivery failures, or multiple negotiation rounds. Eligible divisions have a
known non-null WQI, a known population greater than zero, and tier `Poor`, `Very Poor`, or
`Unsuitable`, matching `src/core/bidding.has_need`. `Excellent`/`Good` and zero-population divisions
are recorded as `ineligible`. `Unknown`, missing/invalid WQI, missing/invalid population, and absent
risk records are retained with `unavailable` status; they are not treated as safe or silently
removed from the report. The implementation trusts B's tier label and does not recompute its WQI.

The local Contract Net-style award ranks eligible divisions by the existing A bid convention
`WQI × population`, with alphabetical `ds_division` order as the deterministic tie-break. This
reuses that documented bid choice for a demo ranking, not as an agreed evaluation risk-weight
definition. Equal split assigns quotient shares and distributes indivisible remainder units in
alphabetical division order, subject to the one-unit-per-division cap. First-come serves eligible
divisions in `arrival_order`; the provisional default is alphabetical order, explicitly a synthetic
arrival-order assumption. Both baseline choices are configurable and must be reported in provenance.

The detailed output is `demo_output/allocation_decisions.csv` with columns:
`provenance,month_index,resource,resource_pool_size,ds_division,tier,population,method,status,units,bid,rank,reason`.
`provenance` is `illustrative_local_demo`; methods are `local_contract_net_style`, `equal_split`,
and `first_come`; statuses are `allocated`, `not_allocated`, `ineligible`, or `unavailable`.
All input divisions, including unavailable ones, get a row for every method. This detailed report
deliberately does not use the evaluator's `allocations.csv` schema: it includes decisions and
provenance and is not a verified team allocation result. The writer rejects paths inside the
repository's production `logs/` directory. A later runner may create evaluator-compatible data only
with a separate explicit adapter and must keep the illustrative provenance visible.

### Alert policy and event pairing

The alert API can accept only a known boolean hotspot status; it does not determine one. `Very Poor`
and `Unsuitable` need approved recipient wording/policy; `Unknown` must remain unavailable. The
current alert agent issues alerts on its own alert thread, while the evaluator's latency pairing
requires matching `thread_id`, `ds_division`, and `month_index` between a received risk record and a
sent alert. Do not fabricate a matching thread or treat collection time as receipt time. Latency
remains unavailable unless the team agrees a correlation convention and the event history records
it consistently.

## Environment and commands

The project documentation specifies Python 3.11 or 3.12. A local virtual environment can be created
without a global install when one of those interpreters is available. Check first, then use the
matching command:

```sh
python3.12 --version
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Use `python3.11` in place of `python3.12` if that is the available chosen interpreter. The checked
workspace currently exposes only Python 3.14.6, so neither command can run here yet. No environment
or package changes were made as part of this contract stage. Compatibility of the pinned manifest
with Python 3.11/3.12 has not been verified by creating an environment.

Run the demo into a new or empty isolated directory:

```sh
python -m src.sim.run_local_demo --output-dir demo_output --months 12 --start-month 10 --resource RO_unit --pool-size 3
```

When Streamlit and dashboard dependencies are available, launch the explicit demo view with:

```sh
CKDU_DEMO_OUTPUT_DIR=demo_output streamlit run dashboard/app.py
```

The dashboard has not been runtime-verified in environments where Streamlit is unavailable.

## Docker workflow for macOS

The repository Docker image uses `python:3.12-slim` and installs `requirements-docker.txt` inside
the image. It carries the existing pinned requirements, with the existing `winloop==0.7.2` pin
marked Windows-only because its Linux source build fails. The original `requirements.txt` remains
unchanged. Compose targets `linux/amd64`, which has a Python 3.12 wheel for the pinned
`slixmpp_multiplatform` package. Apple Silicon Macs run this architecture through Docker Desktop
emulation, which may be slower than native ARM64. Build from the repository root:

```sh
docker compose build
```

Before generating output, ensure `demo_output/` is new or empty. The runner refuses a non-empty
output root to avoid replacing prior artifacts. The Compose runner mounts only this directory and
writes the requested 12 simulated months:

```sh
docker compose run --rm demo-runner
```

To keep an earlier bundle and generate another, select a fresh host directory:

```sh
CKDU_DEMO_HOST_DIR=./demo_output/run-2 docker compose run --rm demo-runner
```

After the runner exits successfully, start only the dashboard service:

```sh
docker compose up -d dashboard
docker compose logs -f dashboard
```

Open `http://localhost:8501`. With `CKDU_DEMO_OUTPUT_DIR` set by Compose, the generated local demo
is selected by default; the team-output and fixture views remain selectable. The dashboard reads
the existing output bundle and does not run or replace the runner.

Run the focused tests in the same image (the service's output bind mount is unused by these
temporary-directory tests):

```sh
docker compose run --rm --entrypoint python demo-runner -m unittest \
  tests.test_local_demo_runner tests.test_hotspot_screening tests.test_local_allocation \
  tests.test_alert tests.test_evaluate tests.test_dashboard tests.test_member_d_integration -v
```

Stop and remove containers/network while preserving `demo_output/` and the built image:

```sh
docker compose down --remove-orphans
```

Set `CKDU_DEMO_HOST_DIR` consistently for the runner and dashboard if you choose a non-default
output directory. Do not run the one-shot service again against a non-empty output directory; use a
new path or move the previous bundle aside first.

## Implemented runner artifacts and unavailable stages

The runner writes `logs/risk_scores.csv`, `logs/well_scores.csv`, `logs/messages.csv`, and
`logs/evaluation_summary.csv` beneath the configured output root, alongside
`hotspot_screening.csv`, `allocation_decisions.csv`, `alerts.csv`, `division_points.csv`, and
`provenance.json`. Risk score
and per-well schemas reuse the Member B writer when available; if scoring cannot run, empty files
with dashboard-compatible headers communicate absence without fabricated rows. Hotspot, allocation,
and alert CSVs carry `provenance=illustrative_local_demo`. `messages.csv` retains the shared seven
columns and contains only messages actually issued by `AlertAgent`; suppression/unavailable
decisions are represented only in `alerts.csv`. The dashboard's generated-demo view reads its
approximate division-town coordinates from the copied `division_points.csv` artifact rather than
loading team-output paths.

The configurable default hotspot rule (`Poor`, `Very Poor`, or `Unsuitable` for two consecutive
`month_index` records; CLI flags `--hotspot-trigger-tiers` and
`--hotspot-consecutive-months`) is an explicit project-defined demo screening rule, not a validated
epidemiological model. Alert delivery still follows the current AlertAgent policy: unsupported tiers
including `Very Poor`, `Unsuitable`, and `Unknown` remain unavailable. The allocation engine is a
local illustrative Contract Net-style ranking adapter, not the original Member C implementation.
Its allocations are not fed into evaluator team-allocation inputs, so allocation comparisons remain
unavailable. Alert latency remains unavailable without a documented risk-receipt/alert correlation.
The manifest records the runner timestamp; data processing and deterministic allocation decisions
are stable for identical inputs and options, but the manifest timestamp necessarily changes per run.
