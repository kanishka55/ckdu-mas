# Data sources and modelling assumptions (Member A)

This file separates **real data** (cited) from **modelling assumptions** (our choices, not findings).
The methodology section of the report should draw on it directly.

## Real data filled into `data/raw/ckdu_03_sampling_design_ds_divisions.csv`

### `population`: real

Source: **Census of Population and Housing 2012**, Department of Census and Statistics, Sri Lanka
(census date 20 March 2012), DS-division totals, as tabulated by City Population
(citypopulation.de, Sri Lanka admin pages for Anuradhapura, Moneragala and Kandy districts).
Values were checked against the raw page HTML, not a summary. The task brief says "Census 2011",
but the census was held in **2012**. There was no 2011 census.

| ds_division (file) | Census DS name | Population 2012 |
|---|---|---:|
| Padaviya | Padaviya | 22,998 |
| Kebithigollawa | Kebithigollewa | 22,325 |
| Medawachchiya | Medawachchiya | 46,906 |
| Kahatagasdigiliya | Kahatagasdigiliya | 40,339 |
| Mihintale | Mihinthale | 35,293 |
| Talawa | Thalawa | 57,793 |
| Nochchiyagama | Nochchiyagama | 49,886 |
| Galnewa | Galnewa | 34,756 |
| Palagale | Palagala | 34,002 |
| Monaragala | Moneragala | 49,520 |
| Kandy | Kandy Four Gravets & Gangawata Korale | 158,561 |

**Interpretation to confirm:** Cooray et al. list "Monaragala" and "Kandy" as comparison
*districts*. We used the DS division of the same name (the district capital's DS), not the whole
district (451,058 and 1,375,382 people), so that each VillageAgent represents a DS-sized unit.
Check the paper's Methods to see where the control samples were actually taken.

To cite: Department of Census and Statistics (2012), *Census of Population and Housing 2012*.
Before the final report, prefer the DCS table itself (statistics.gov.lk) over the secondary
tabulation.

## Modelling assumptions

### `lat`, `lon`: approximate location, assumption

OpenStreetMap (Nominatim search) **point** for each DS division's main town, not a polygon
centroid. For Palagala we used the OSM Divisional Secretariat building, because the town search
matched only roads. Four decimal places, which is about 11 m of precision, though not 11 m of
accuracy for the DS area. Good enough for distances and map markers. Not a boundary.

### `distance_to_depot_km`: assumption

- A single resource depot at **Anuradhapura city** (OSM place node, 8.3350 N, 80.4106 E), the
  district capital and the obvious regional hub for the North Central Province sites.
- Distance = **straight-line (haversine) km** from the DS point to the depot
  (`src/core/geo.py`). It is not a road distance, and we applied no road-detour factor, because
  we have no published factor to use.
- Distance is **not** part of a village's bid (see "Bid function" below), and it does **not** affect
  the current ResourceCoordinator ranking. It is retained as descriptive village metadata only;
  the current implementation has no bowser delivery-cost formula or cost-based allocation rule.
  The comparison DS divisions (Monaragala 192.8 km, Kandy 118.5 km) sit far from the depot, but
  the simulation does not currently use those distances to rank or price resource delivery.
- `tests/test_wells.py` recomputes every distance from `lat`/`lon` and fails if they drift apart.

### `n_wells_modelled = 20`: assumption

This matches `--wells-per-ds 20` in `src/generate_synthetic_wells.py`, which produced the 440 rows in
`data/processed/synthetic_wells.csv` (20 wells × 11 DS × 2 seasons). It does **not** count the
real wells in each DS. Cooray et al. sampled 5–11 locations per DS (`samples_per_season`).

### `RHO = 0.6`: unpublished assumption in the synthetic generator

`src/generate_synthetic_wells.py` links the 9 parameters within a well through one latent factor
with correlation `RHO = 0.6`, so that hardness, EC and F tend to move together. **This value is not
published.** We chose it so that the synthetic wells are neither independent per parameter nor
perfectly collinear. `CONCENTRATION = 4.0` (the Beta spread) is likewise our own choice. Both
belong in the methodology section and should get a sensitivity check (e.g. RHO 0.3 / 0.6 / 0.9).

### Season → month mapping: assumption (`src/core/wells.py`)

The published data has two seasons (Wet, Dry); the ClockAgent ticks monthly.

| Months | Season | Rationale |
|---|---|---|
| Oct, Nov, Dec, Jan | Wet | Maha rains in the dry zone (second inter-monsoon + north-east monsoon) |
| Feb – Sep | Dry | Includes the minor Mar–Apr inter-monsoon rains, treated as dry for simplicity |

Each month reuses that well's reading for its season (`wells.expand_to_months`). Values are
**repeated, not interpolated**, because twelve monthly values would be invented precision.
Before the report, check Cooray et al.'s sampling months and align this table with them.

## Bid function (`src/core/bidding.py`)

Every design decision and change is recorded in the module docstring. In summary:
`bid = risk × population`, where risk is the village WQI. A village bids only when its tier is
Poor, Very Poor or Unsuitable (`has_need`); at Excellent or Good it refuses with "no need this
month". The boundary is the published Cooray et al. class limit (WQI 100, from ckdu_02), not a new
threshold. Zero population or zero risk gives 0. A missing risk or population raises, and the
VillageAgent then refuses the CFP. The coordinator compares bids after `normalise_bids` (scaled to
[0, 1]). Ties are broken alphabetically by `ds_division`.

Changed on 2026-10-17: the first draft divided by `distance_to_depot_km`. In the A+B test run that
made Padaviya (tier Poor, 68 km) bid less than Kandy (tier Good), i.e. the most remote at-risk
villages were served last. Distance was removed from the need score. The current coordinator ranks
only normalized `risk × population` bids; a separate delivery-cost policy has not been implemented.
