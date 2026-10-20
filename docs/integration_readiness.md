# Member D integration readiness and report notes

This checklist records the current contracts and open decisions. It does not
freeze the shared ontology or establish that Members A, B, or C have completed
their end-to-end implementations.

## Environment and reproducibility

The repository has a pinned `requirements.txt` and a Docker-specific
`requirements-docker.txt`. The Docker image uses Python 3.12; the documented
local target remains Python 3.11 or 3.12. The Docker setup installs packages
inside the image and exposes Streamlit on port 8501. Local dashboard command:
`streamlit run dashboard/app.py`. Demo command:
`python -m src.sim.run_local_demo --output-dir demo_output` (the target must be
new or empty). The runner writes only under its configured demo output root
and refuses protected production data/log paths.

Reported focused verification includes 87 tests in Docker across the demo
runner, hotspot, allocation, alert, evaluator, dashboard, and integration
suites; the most recent dashboard and integration runs reported 20 and 3 tests
passing respectively. The dashboard was previously launched and visually
inspected, but was not relaunched after the latest presentation cleanup. These
results are focused checks, not a claim that the complete project test suite or
all team components pass end to end.

## Cross-component contract checks

- `src/core/ontology.py` is the typed message-body source of truth. Callers use
  `validate(performative, body)` to validate a body, `encode(performative,
  body)` before sending, and `decode(performative, text)` when receiving. Each
  body has a `type` field and must match the exact fields and types declared for
  that type and performative.
- The current logger in `src/core/msg_log.py` writes exactly seven columns, in
  order: `timestamp,sender,receiver,performative,thread_id,payload,direction`.
  `direction` is `sent` or `received` and distinguishes endpoint copies of a
  message. The timestamp is the logger's UTC append time; it is not inherently
  the measurement time or the time a reading arrived at an agent.
- The alert agent validates typed `risk_record` input and typed `alert` output.
  Issued alerts use the existing `inform` performative as a provisional mapping
  that still needs team confirmation. They are logged with `direction="sent"`.
  Suppressed and unavailable decisions are internal and are not logged as
  delivered messages.
- The typed alert body is `type="alert"`, `month_index`, `ds_division`,
  `tier`, `hotspot`, and `message`. `month_index` is a simulated index, not a
  calendar month. The current recipient policy supports `Excellent`, `Good`,
  and `Poor`; `Very Poor`, `Unsuitable`, and `Unknown` remain unavailable
  pending policy agreement. Missing hotspot status is unavailable, never
  inferred as inactive.
- The evaluator reads the seven-column logger schema and validates typed
  payloads. Alert latency requires a received `risk_record` and a sent `alert`
  paired by exact `thread_id`, `ds_division`, and `month_index`. It does not
  count suppressed decisions or infer pairs from file proximity. The current
  risk and alert thread identifiers do not match, so latency remains
  unavailable until the team agrees on and supplies a correlation convention.
- Allocation comparisons remain unavailable unless the evaluator receives
  the required allocation methods/baselines, population and risk-weight inputs,
  an agreed risk-weight definition, and verified provenance. Local illustrative
  allocation outputs are not team allocation results.
- The dashboard keeps three sources separate: verified team output files,
  labelled illustrative test fixtures, and the generated local-demo bundle
  selected through `CKDU_DEMO_OUTPUT_DIR`. Missing or malformed files produce
  unavailable/incomplete states. Generated demo outputs are labelled synthetic
  and illustrative; they do not fill in missing team outputs.
- The CKDu disclaimer remains visible: water-quality/risk indicators do not
  diagnose CKDu or establish that water caused CKDu. Approximate division town
  points are not represented as well locations. Reduced-parameter WQI outputs
  are not directly comparable to the full 13-parameter index.

### Integration checklist for Members A, B and C

| Owner | Confirm before integration |
|---|---|
| **A** | Confirm stable `ds_division`, source labels, and the reading schema published by `src/core/wells.py`/`src/sim/load_data.py`. Confirm the existing simulated-month-to-season mapping and keep `month_index` distinct from calendar `month`. Confirm provenance for population, distance, and approximate division-town coordinates in the sampling-design input. The logger direction identifies sent/received message rows, but its timestamp is append time; agree how actual reading-arrival time will be represented if latency is needed. |
| **B** | Confirm the current typed `risk_record` contract: `month_index:int`, `ds_division:str`, `wqi:number|null`, `tier:str`, `drivers:list`; confirm tier vocabulary, source handling, and driver interpretation. Publish the HotspotAgent output shape and village/month scope, and agree how it maps to the alert agent's required boolean-or-unavailable input. Agree a sourced reference-tier mapping for risk alignment; none is inferred by the evaluator. Confirm alert wording/policy for `Very Poor`, `Unsuitable`, and `Unknown` before making those tiers deliverable. |
| **C** | Confirm the evaluator's allocation CSV contract exactly: `month,resource,ds_division,round,winning_bid,method`, with `contract_net`, `equal_split`, and `first_come` method labels. Supply comparable outputs for all methods or an agreed unavailable representation, along with population and numeric risk-weight inputs/provenance. Agree the definition of risk-weighted population served and how allocation results are attested as verified. Provide complete, consistently threaded negotiation logs if negotiation metrics are to be reported. |
| **All** | Confirm `alert → inform` as the provisional performative mapping, and review the current typed schemas and payloads before treating them as a frozen team contract. Agree a risk-receipt/alert correlation convention that can be represented by the current `thread_id`, division, and `month_index` fields. Confirm that only sent issued alerts count as delivered alerts; alert suppression is currently internal and produces no message-log audit row. |

### Known incompatibilities and smallest proposed fixes

1. **Alert-latency correlation and timestamps remain unresolved.** The logger
   distinguishes sent and received rows using `direction`, and its seven
   columns must remain unchanged. Its timestamp is append time, not a
   measurement timestamp. The evaluator currently pairs only exact
   `thread_id`, division, and `month_index`; the current alert thread ID does
   not match the risk-input thread ID. Agree on a shared correlation
   convention and ensure risk receipt and alert issue timestamps have the
   intended meaning before reporting latency. Do not infer correlation from
   row proximity or sample-collection time.
2. **Hotspot adapter is provisional.** The local project-defined screening
   module is not Member B's HotspotAgent and is not validated epidemiological
   evidence. Member B should publish the output schema and status semantics.
   Missing or `Unknown` input remains unavailable; it must not become
   `False`.
3. **Reference alignment is unresolved.** Existing division prevalence
   classes and district aggregates do not by themselves define an agreed
   reference risk tier for each division. Supply and document a sourced
   mapping before reporting alignment; otherwise the evaluator returns
   unavailable.
4. **Allocation inputs and provenance are unresolved.** The local allocation
   engine is a deterministic illustrative adapter, not the original Member C
   implementation or full FIPA exchange. Team allocation comparisons require
   Member C outputs, baselines, population/risk inputs, an approved
   risk-weight definition, and provenance. Until those are available, the
   evaluator and dashboard keep allocation comparisons unavailable.
5. **Team-generated output availability is distinct from source data.** The
   repository has synthetic seasonal readings and published/assumption-labelled
   division data, including approximate town coordinates. Those source files
   do not themselves establish that monthly team risk, hotspot, allocation, or
   message outputs have been generated. The dashboard reports missing team
   output files as unavailable and does not substitute demo artifacts.
6. **Suppression audit compatibility.** Current alert suppression decisions
   stay internal and are not written as sent or received messages or as rows
   in the shared logger. The generated demo's separate `alerts.csv` records
   decision statuses with illustrative provenance. Consumers must count only
   issued alerts as delivered; a shared suppression audit-row format would
   require a separate team agreement and must not silently alter the logger
   columns.

## Report limitations section (draft)

The available real water-quality studies and local reference summaries are
small, with limited sample counts and geographic coverage. Cooray et al.
describe 88 sampled locations across 11 areas—35 High, 26 Moderate, 17 Mild,
and 10 control locations—with two seasonal sample sets per location; the
article abstract also refers to 88 samples. Verify and state the unit of
observation accurately before quoting a sample count. District aggregates and
11 DS divisions do not support claims of broad predictive generalizability.

The repository contains 440 generated well readings, all marked
`source=synthetic`, covering 20 modelled wells in each of 11 divisions over
Wet and Dry seasons. The generator uses `RHO = 0.6` as an assumed
inter-parameter correlation; it is not a published or measured value and must
be disclosed as a modelling assumption. Seasonal values expanded over
simulated months are repeated inputs, not new monthly measurements. No
individual patient data is used, and the project has no live sensor feed.

The `ckdu_04` incidence values do not specify a unit in the source text
available to the project. Check Figure 1 of Ranasinghe et al. before assigning
or quoting a unit. Ranasinghe et al. report Results point-prevalence ranges of
1.52–3.35 for Anuradhapura and 0.67–1.25 for Polonnaruwa, while the Abstract
gives 2.44–4.35; cite the Results values and note this discrepancy. Do not
silently reconcile the two passages.

The published evidence describes associations and risk indicators; it does
not establish that water chemistry causes CKDu. Nikagolla et al. explicitly
state that their data are insufficient to prove a chemistry–CKDu link. System
tiers, hotspot outputs, and alerts must remain risk-indicator support and must
not be described as diagnosis or causation.

The current WQI code reads weights, standards, and classes from the project
method data and computes a reduced-parameter WQI. The synthetic readings
contain eight of the 13 WQI parameters; TDS, Ca, Mg, Na, and NO3 are absent
(EC is an input column but is not a WQI parameter). Available weights are
renormalised over measured parameters; missing parameters are not treated as
zero. The resulting absolute scores are not directly comparable with the full
13-parameter Cooray WQI. The remaining parameter-coverage decision and any
minimum coverage rule for real inputs require team confirmation. The method
also uses the documented pH standard interpretation; its effect should be
disclosed when discussing results.

The current allocation-comparison and risk-alignment tests use illustrative
inputs, not CKDu observations or scientific validation. A measured allocation
advantage, risk-alignment result, or alert latency cannot be reported until
the required A/B/C outputs, agreed mappings, correlation convention, and
input definitions are available. Generated local-demo outputs remain
synthetic/illustrative and must be labelled accordingly.
