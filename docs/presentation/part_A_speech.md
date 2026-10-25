# Part A speech: Data, ClockAgent and WellMonitorAgent

**Speaker:** [Member A name]  ·  **Length:** 2–3 minutes at a calm pace
**How to use this file:** read the speech aloud. Text in *(italics and brackets)* tells you what to do
or show; don't read it out. The questions and references below are for preparation, not for reading out.

---

## 2–3 minute version (for slide 10)

*(Show slide 10. About 2½ minutes at a calm pace.)*

Thank you. My name is [Member A name], and I built Part A of our project: the data, and the first
two agents in the system.

CKDu, chronic kidney disease of unknown cause, is a serious problem for farming families in the dry zone of Sri Lanka. One study, by Ranasinghe and
colleagues in 2019, found more than thirty thousand patients in the North Central Province alone.
Research shows a link with drinking well water, but the real cause is still not known. We chose
this topic because it is a real local problem, a natural multi-agent problem, and there is
published research data we could use.

So, how did I get the data? There are no live well sensors in Sri Lanka. So I collected numbers from
published articles and saved them as CSV files, simple tables a program can read.
The water-quality statistics and the Water Quality Index method come from Cooray and colleagues,
2019. The population of each of our 11 areas comes from the 2012 national census, and the locations
come from OpenStreetMap.

But the papers only give averages, not the reading of each well. So I generated 440 synthetic well
readings: 20 wells for each area, for the wet and dry season. They stay inside the published ranges,
and every row is labelled synthetic, so it is never confused with real data.

Now, my two agents. The Clock agent starts every new month in the simulation. It sends a message
saying which month it is, and whether it is the wet or dry season.

When the Well Monitor agent receives that message, it takes the 20 well readings of each village for
that month, and sends them to the Risk Scoring agent. So, one tick comes in, and eleven messages go
out, one for each village.

And it worked. In our twelve-month run, the Clock sent 12 ticks, and the Well Monitor sent 132
reading messages, with no failures.

Thank you. Now I will hand over to [Member B name], who will explain how these readings become a
risk score.

---

## Questions you might be asked (with simple answers)

**Q: Why didn't you use real well readings?**
The papers publish only summary statistics, not each well's reading. So we generated wells that
match the published averages and ranges, and we label them clearly as synthetic.

**Q: Is the data live?**
No. There is no live sensor network. The Well Monitor replays the readings month by month, as if
they were monthly tests. That is realistic, because wells are tested by hand, not every second.

**Q: How do you know the synthetic data is realistic?**
Every value stays inside the published minimum and maximum, and the averages match the published
averages for each type of area and season. Member B also compared the score range with the paper:
ours is about 42 to 183, and the paper's is about 21 to 178.

**Q: Why the number 0.6?**
It is an assumption, so that parameters in the same well move together a little, as they do in real
water. It is not published, so we report it as an assumption. It could be tested with other values
like 0.3 or 0.9.

**Q: Why October to January for the wet season?**
That is the main rainy season in the dry zone, the Maha rains, from the second inter-monsoon and the
north-east monsoon. It is a modelling assumption, and we documented it.

**Q: But Cooray et al. took their dry-season samples in December and their wet-season samples in May.
Isn't your mapping the other way round?**
Yes, that is a fair point, and we noted it in our assumptions document. Our mapping follows the usual
rainy calendar of the dry zone, but it does not match the paper's sampling months. It only changes
which months get which season's values; it does not change any published number. Aligning the
mapping with the paper's sampling months is on our list of improvements.

**Q: What happens if a village has no readings?**
The Well Monitor reports it and skips that village. It never makes up a value. And because every wait
has a time limit, the system cannot get stuck.

**Q: Did you use any patient data?**
No. We used only published totals, like the 30,566 patients across the whole province, never data
about any person.

---

## References to cite

1. Cooray T., et al. (2019). *Assessment of Groundwater Quality in CKDu Affected Areas of Sri Lanka:
   Implications for Drinking Water Treatment.* International Journal of Environmental Research and
   Public Health, 16(10), 1698. doi:10.3390/ijerph16101698
2. Ranasinghe A.V., et al. (2019). *The incidence, prevalence and trends of Chronic Kidney Disease and
   Chronic Kidney Disease of uncertain aetiology (CKDu) in the North Central Province of Sri Lanka: an
   analysis of 30,566 patients.* BMC Nephrology, 20, 338. doi:10.1186/s12882-019-1501-0
3. Department of Census and Statistics, Sri Lanka (2012). *Census of Population and Housing 2012.*
4. OpenStreetMap contributors. *OpenStreetMap* (division locations, accessed through Nominatim search).

*(Before you submit, open each article and check the exact full title. The titles above are from our
data notes.)*

### Project files behind each claim (if someone asks to see them)

| Claim | File |
|---|---|
| Published water statistics | `data/raw/ckdu_01_water_quality_area_stats.csv` |
| WQI method, weights, standards | `data/raw/ckdu_02_wqi_method_and_thresholds.csv` |
| 11 areas, population, location, distance | `data/raw/ckdu_03_sampling_design_ds_divisions.csv` |
| District CKDu statistics | `data/raw/ckdu_04_ckdu_aggregate_stats.csv` |
| Synthetic wells (440 rows) | `data/processed/synthetic_wells.csv`, made by `src/generate_synthetic_wells.py` |
| Assumptions (locations, distance, 0.6, seasons) | `docs/data_assumptions.md` |
| Data loading and checks | `src/sim/load_data.py`, `src/core/wells.py` |
| ClockAgent | `src/agents/clock.py` |
| WellMonitorAgent | `src/agents/well_monitor.py` |
