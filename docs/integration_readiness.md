# Member D integration readiness and report notes

This checklist records the current contracts and open decisions. It does not
freeze the shared ontology or establish that Members A, B, or C have completed
their implementations.

## Environment and reproducibility

The documented target is Python 3.11 or 3.12. The current interpreter is
Python 3.14.6; neither target interpreter is available in this environment.
The package inventory does not include SPADE, pandas, NumPy, SciPy,
scikit-learn, Streamlit, Folium, or pytest. The current tests use the standard
library `unittest`; dashboard rendering and the synthetic-well generator need
their declared third-party dependencies.

No `requirements.txt` or other dependency manifest exists. Proposed manifest
design: maintain a short direct-dependency input for the documented stack
(SPADE, pandas, NumPy, SciPy, scikit-learn, Streamlit, Folium, pytest), then
generate hash-pinned lock files separately under Python 3.11 and Python 3.12
after resolving and testing them in those supported interpreters. Record the
Python minor version with each lock. Do not copy the current Python 3.14
environment or guess pins: no supported interpreter or project dependencies
are installed here, so compatible version pins cannot yet be verified.

Dashboard launch command: `streamlit run dashboard/app.py`. It was not run:
Streamlit and Folium are absent, and this runtime is outside the documented
Python range. A successful unit-test run is not evidence that the dashboard
renders.

## Cross-component contract checks

- The alert agent validates B-shaped risk records with `validate_message`,
  validates outgoing alert/suppression payloads, and uses the shared logger.
- `alert_issued` uses the existing `inform` performative through the explicit
  provisional `MESSAGE_PERFORMATIVES["alert"]` mapping.
- Suppression is logged as `event_type=alert_suppressed`, self-addressed, with
  an empty performative. The evaluator validates that shape separately and
  the alert-latency calculation selects only `alert_issued` events.
- The CSV columns remain `timestamp,sender,receiver,performative,thread_id,payload`.
- The dashboard reads existing logs and evaluator output through injectable
  paths. Absent or malformed team inputs display unavailable/incomplete states;
  no team result is inferred from demo fixtures.
- Focused integration tests compose logger → alert → evaluator using a
  temporary log and separately verify evaluator/dashboard missing-input states.

### Integration checklist for Members A, B and C

| Owner | Confirm before integration |
|---|---|
| **A** | Confirm the exact `well_reading` body against the ontology and whether `inform` is its agreed performative. Publish the simulated-month-to-season mapping, stable `ds_division`/`month` identifiers, coordinate provenance and source labels. State whether a logged timestamp means measurement time, arrival time, or append time. Agree how receivers identify sent versus received rows without adding/reordering the six CSV columns. |
| **B** | Confirm risk-record field names/types and tier vocabulary (`Excellent`, `Good`, `Poor` currently exists provisionally in ontology); source values and driver units/meaning. Define the HotspotAgent output and how it maps to the alert agent's current `hotspot: bool` adapter. Confirm whether hotspot status is village/month scoped. Agree the reference-tier mapping from `ckdu_03` prevalence classes and `ckdu_04` district aggregates; the evaluator deliberately does not invent it. |
| **C** | Confirm `logs/allocations.csv` header and method labels exactly: `month,resource,ds_division,round,winning_bid,method`, with `contract_net`, `equal_split`, and `first_come`. Emit comparable rows for all methods for each month/resource, or define how unavailable baselines are reported. Agree that the shared score is `sum(risk_weight × population)` over distinct served divisions per month/resource/method; supply the agreed population and numeric risk-weight inputs/provenance. Confirm suppression audit rows are excluded from delivered-agent events by downstream consumers. |
| **All** | Review the strict provisional ontology payloads and performative mappings. In particular, confirm `alert → inform`; ontology coverage for readings, month ticks and Contract Net replies; risk `source` vocabulary; alert and suppression event payload fields; and downstream handling of the internal suppression row. |

### Known incompatibilities and smallest proposed fixes

1. **Sent/received direction and measurement-arrival time are not recoverable.**
   `log_message` and `log_received_message` write indistinguishable rows with
   an append-time timestamp; the fixed CSV has no direction or measurement
   timestamp column. Minimal compatible-column proposal: agree on a reserved
   JSON payload envelope for logger metadata (direction and event/measurement
   time) while preserving the six CSV columns, and update evaluator/dashboard
   readers to validate the inner ontology body. This changes the payload
   representation and needs all-member approval before adoption. Until then,
   alert latency remains unavailable unless an event selector and unambiguous
   timestamps are provided.
2. **Hotspot adapter is provisional.** Member D currently accepts a boolean;
   B's published brief does not define a HotspotAgent output schema. B should
   publish the smallest output shape and scope; if it is a field on the risk
   record, update the shared risk schema only after team agreement.
3. **Reference alignment is unresolved.** The raw reference inputs include
   DS prevalence classes and district aggregates, not an agreed tier per DS
   division. B and D should document a sourced mapping before reporting an
   alignment value; otherwise the metric remains unavailable.
4. **Allocation inputs are unresolved.** C documents allocation rows and the
   shared score name but not yet an agreed numeric risk-weight mapping or
   population provenance. The evaluator requires those inputs and emits
   unavailable results if absent.
5. **Spatial/monthly team outputs are not present.** The current DS design CSV
   has blank population, distance and coordinate fields; current synthetic
   wells have wet/dry seasons, not months. Dashboard map and monthly views need
   A's documented outputs. No location or month is inferred from the fixtures.
6. **Suppression audit compatibility.** Current rows remain distinguishable
   by `event_type=alert_suppressed`, empty performative and self-addressed
   endpoints. All consumers must classify by event type and must not count
   these internal records as delivered alerts. Confirm this convention during
   the shared contract review.

## Report limitations section (draft)

The available real datasets are small, ranging from tens of samples to about
90 in the cited water-quality studies. Cooray et al. report 88 sampled
locations across 11 areas—35 High, 26 Moderate, 17 Mild, and 10 control—and
two sample sets per location; the article abstract also says 88 samples. Check
the study Methods and state the unit of observation accurately before quoting
a sample count. District-level aggregates and 11 DS divisions do not support
claims of broad predictive generalizability.

The repository contains 440 generated well readings, all marked
`source=synthetic`, covering 20 modelled wells in each of 11 divisions over
Wet and Dry seasons. Their generator uses `RHO = 0.6` as an assumed
inter-parameter correlation; it is unpublished and must be disclosed as a
modelling assumption, not a measured or literature-established value. The
current readings are seasonal rather than monthly. No individual patient data
is used, and the project has no live sensor feed.

The `ckdu_04` incidence values have no unit stated in the source text available
to the project. Check Figure 1 of Ranasinghe et al. before assigning or quoting
a unit. Ranasinghe et al. report Results point-prevalence ranges of 1.52–3.35
for Anuradhapura and 0.67–1.25 for Polonnaruwa, while the Abstract gives
2.44–4.35; cite the Results values and note this discrepancy. Do not silently
reconcile the two passages.

The published evidence describes associations and risk indicators; it does
not establish that water chemistry causes CKDu. Nikagolla et al. explicitly
state that their data are insufficient to prove a chemistry–CKDu link. System
tiers, hotspot outputs, and alerts must therefore remain risk-indicator support
and must not be described as diagnosis or causation.

The full Cooray WQI uses 13 parameters, whereas the current raw and synthetic
well files contain nine; TDS, Ca, Mg, Na, and NO3 are absent. The source CSV
also marks weights for pH, hardness, alkalinity, Ca, Mg, Na, Cl, SO4, and Fe
as `TO_FILL`, and notes that the SLS standards are in the unavailable
supplement. B's task brief permits a reduced-parameter WQI over the nine
available parameters only if the team agrees and documents it, calls it a
reduced-parameter WQI, lists all five omitted parameters, and states its
absolute values are not comparable with Cooray's full WQI. No B WQI
implementation or team decision currently exists in the inspected source;
whether this reduced route was selected is unresolved and must not be claimed
in the final report yet.

The current allocation comparison and risk-alignment fixtures are illustrative
test inputs, not CKDu observations or scientific validation. A measured
allocation advantage, risk-alignment result, or alert latency cannot be
reported until A/B/C provide integrated outputs and the team agrees on the
mapping and input definitions described above.
