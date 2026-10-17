# WQI method and risk tiers (Member B)

The risk tier is a **risk indicator derived from published water-quality thresholds**. It is not a
CKDu diagnosis and not a claim that well water causes CKDu. Nikagolla et al. 2020 state their data
are insufficient to prove a chemistry–CKDu link; the report should quote that.

Code: `src/core/wqi.py`, `src/core/risk_tiers.py`, `src/agents/risk_scoring.py`.
Tests: `tests/test_wqi.py`, `tests/test_risk_tiers.py`.

## Source

Cooray et al. 2019, *Assessment of Groundwater Quality in CKDu Affected Areas of Sri Lanka*,
IJERPH 16:1698 (CC BY), Sec 2.3 and **Supplementary Tables S1 and S2**.
The supplement PDF (`ijerph-16-01698-s001.pdf`) was retrieved on 2026-10-10 through the Europe PMC
supplementary-files API for PMC6572134 (`https://www.ebi.ac.uk/europepmc/webservices/rest/PMC6572134/supplementaryFiles`).
The MDPI link returns 403 to scripts.

All weights, standards and classes are in `data/raw/ckdu_02_wqi_method_and_thresholds.csv`
(rows `Weight wi:`, `Standard Si:`, `WQI class:`). The code reads them from there. Nothing is
hard-coded in Python, and a `TO_FILL` value raises `ToFillError`.

## Formula (Eq. 1–3)

```
Wi  = wi / Σwi
qi  = (Ci / Si) · 100
WQI = Σ Wi · qi
```

| Parameter | Si (SLS 614:2013) | wi | Wi (of 48) |
|---|---|---|---|
| pH | 6.5–8.5 (8.5 used, see below) | 4 | 0.0833 |
| TDS | 500 mg/L | 5 | 0.1042 |
| Total hardness | 250 mg/L | 2 | 0.0417 |
| Total alkalinity | 200 mg/L | 3 | 0.0625 |
| Ca | 100 mg/L | 2 | 0.0417 |
| Mg | 30 mg/L | 2 | 0.0417 |
| Na | 200 mg/L | 3 | 0.0625 |
| F | 1 mg/L | 5 | 0.1042 |
| Cl | 250 mg/L | 4 | 0.0833 |
| SO4 | 250 mg/L | 4 | 0.0833 |
| NO3 | 50 mg/L | 5 | 0.1042 |
| Fe | 0.3 mg/L | 4 | 0.0833 |
| DOC | 4* mg/L (not an SLS limit) | 5 | 0.1042 |

Note: in the PDF the printed Wi column is shifted down one row relative to the parameter names. The
values above are wi / 48, which reproduce every printed Wi value.

Classes (Table S2): Excellent `<50`, Good `50–100`, Poor `100–200`, Very Poor `200–300`,
Unsuitable `>300`. Boundaries are lower-inclusive (50 → Good, 100 → Poor).

## Interpretations we had to make (for the team to check)

1. **pH Si = 8.5.** Table S1 gives a range, and Eq. 2 needs one number. The paper describes no
   special pH rule (no ideal value of 7), so we apply Eq. 2 literally with the upper limit.
2. **The "WQI = 90" reference point.** Sec 3.4 says a sample that exactly meets the Sri Lankan
   standards scores 90. Under Eq. 1–3, a sample on all 13 standards scores **100**. The 90 is
   reproduced (**89.6**) when the 12 SLS parameters sit on their limits and DOC is 0. DOC has no
   SLS limit (it is printed as `4*`), so it is excluded from "meets the standards". The tests check
   both facts. The brief's "≈ 90 for a sample on every SLS limit" therefore holds only under this
   reading.
3. **Fe units.** The data is in µg/L and Si is 0.3 mg/L, so the code divides by 1000.

4. **Known weakness of Eq. 2 for pH (team decision).** Because qi = pH / 8.5 · 100, *acidic*
   water scores better. pH 5.0, which is outside the standard, gives qi = 59; neutral pH 7.0 gives
   qi = 82. We kept the paper's formula for fidelity. A common alternative in the WQI literature,
   qi = |pH − 7| / (8.5 − 7) · 100, fixes the direction but is not what Cooray et al. state, and it
   would change every score. Decide before the report.

## Parameter coverage: currently a reduced-parameter WQI

The synthetic wells carry 8 of the 13 WQI parameters: pH, hardness, alkalinity, F, Cl, SO4, Fe and
DOC. EC is not a WQI parameter. **TDS, Ca, Mg, Na and NO3 are missing**, so the available
parameters cover 31/48 = **65 %** of the published weight.

How the code handles it (option (b) of the brief): Wi is **renormalised over the parameters
present**, a missing parameter is never set to zero, and every score records `params_used` and
`weight_covered`. Until those five parameters are added, call the output a *reduced-parameter WQI*,
not "the Cooray WQI". Its absolute values are not directly comparable with the paper's.

Upgrading to the full WQI (option (a)) needs no code change. Once Member A adds columns
`TDS_mg_L`, `Ca_mg_L`, `Mg_mg_L`, `Na_mg_L` and `NO3_mg_L`, they are picked up automatically and
`weight_covered` becomes 1.0. Note that deriving TDS from EC needs a conversion factor with a cited
source; we have not chosen one. **Team decision still open.**

**Open risk once real data arrives:** if wells in one village are measured on different parameter
sets, the median mixes WQIs that are not comparable. In the extreme case, a well with only pH
scores about 82, "Good", on 8 % of the weight. A more realistic case found in QA: if fluoride
is not measured one month, Padaviya drops from 100.6 "Poor" to 83.0 "Good". Renormalising
without the strongest risk parameter makes a village look **safer** and lowers its bid. Today every synthetic well has the same 8
parameters, so this cannot happen yet. Before real data is loaded, the team should agree on a
minimum `weight_covered` below which a well is reported as "Unknown" rather than scored. We have
not picked that number, because it is a data-quality threshold and needs team sign-off.

## Village rule (well tiers → one village tier)

- **Village WQI = median of the well WQIs** that month; the village tier is the class of that median.
  One bad well in twenty does not relabel the village.
- `share_poor_or_worse` (fraction of wells in Poor or worse) and `worst_tier` are kept alongside it,
  so the "one bad well" case stays visible to the AlertAgent and the dashboard.
- `drivers` = parameters whose village-mean qi is above 100 (above their standard), ranked by mean
  contribution Wi·qi, at most 3. An empty list means no parameter exceeds its standard on average.
  (Ranking every parameter would list F, DOC and pH for every village, because pH scores qi ≈ 87
  even when it is within range, so that ranking explains nothing.)
- The village WQI is rounded to 1 decimal **before** the tier is assigned, so the `wqi` in the
  message always maps back to the `tier` in the same message.

## Input handling in the agent

- A blank parameter value is treated as missing: the score is renormalised and `weight_covered`
  drops. This holds even when the parameter is blank in every well.
- A well with an unusable value (non-numeric, boolean, negative or infinite) is **dropped and
  reported**: `n_rejected` and `rejected` appear in `logs/risk_scores.csv`. The village's other
  wells are still scored. Voiding the whole village would make its VillageAgent refuse the CFP
  ("no need"), so the village would lose the resource over a single typo.
- One message must hold exactly one calendar month. The agent cannot check `month_index` against
  the rows' calendar month, because the ontology carries no mapping between them.
- Every `well_readings` message is checked against Member A's contract
  (`wells.validate_readings`, which includes the real-vs-synthetic guard), and its rows must match
  the message's `source` and `ds_division`.
- Bad input never crashes the agent; SPADE would kill the behaviour and silence it for the rest of
  the run. The agent sends `wqi: null, tier: "Unknown"` and keeps the reason in
  `logs/risk_scores.csv` (`error` column).

The ontology's `risk_record` carries `wqi`, `tier` and `drivers`. The extra fields are written to
`logs/risk_scores.csv` by the demo, for D's dashboard and evaluation.

## Validation against Cooray et al. (synthetic wells, reduced WQI)

`python -m src.sim.validate_wqi` (run 2026-10-10):

| Season | Tier | Ours | Cooray et al. |
|---|---|---|---|
| Wet | Excellent | 0.0 % | 3.8 % |
| Dry | Excellent | 3.2 % | 2.6 % |
| Wet | Poor | 42.3 % | 28.2 % |
| Dry | Poor | 35.5 % | 30.3 % |

WQI range 41.9–182.5 against the paper's 21.2–178.3. No reading reaches Very Poor.
The dry season is close. The wet season has too many Poor readings and no Excellent ones. Possible
causes are (i) the reduced parameter set: if the five missing parameters sit below their
limits, leaving them out pushes the renormalised WQI up; and (ii) the
synthetic generator's assumed correlation (`RHO = 0.6`). Re-check once the full parameter set exists.

Median WQI by Cooray prevalence class: High 97.1, Moderate 108.0, Mild 91.4, Non-prevalence 69.9.
Only the Non-prevalence controls separate clearly. That is an alignment observation on synthetic
data, not evidence about CKDu.

## Output for Function 3 (HotspotAgent)

Function 2 gives Function 3 two things per village per month, live as messages and as files.

| Message (live) | File (standalone) | Content |
|---|---|---|
| `risk_record` | `logs/risk_scores.csv` | village WQI (median), tier, drivers; the file adds share of wells Poor or worse, worst tier, rejected wells, errors |
| `well_risk` (new, `src/core/ontology.py`) | `logs/well_scores.csv` | per well: `well_id`, `wqi`, `tier`, `q` (quality rating per parameter), plus `ds_division`, `district`, `ckdu_prevalence_class`, `month`, `source` |

To receive the messages, pass the HotspotAgent's JID in `hotspot_subscribers` (for `well_risk`)
and `subscribers` (for `risk_record`) when creating `RiskScoringAgent`. `ckdu_prevalence_class`
is Cooray et al.'s class per division: it is what the alignment check (Spearman or clustering)
compares against. Wells rejected for unusable values are left out of `well_risk`.

`well_risk` is an additive ontology change (no existing message changed). Ask the team to
sign it off before the week-3 schema freeze.

Run the real chain: `python -m src.sim.demo_f1_f2 --embedded-server` (Function 1's
WellMonitorAgent → Function 2 → VillageAgents and a Function 3 stand-in that only collects).
