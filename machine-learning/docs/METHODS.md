# Methods

What the code does, what each number means, and what it does not show. Numbers come from
`models/<set>/metrics.json` and `nisar_check.json`; none are quoted here because the real run has not happened.

## Questions

1. **Driver.** Does antecedent soil moisture explain live fuel moisture (LFMC) beyond weather? Earlier work on
   chamise in Southern California used a two-month lag between SMAP soil moisture and LFMC (Jia et al. 2019).
2. **Use.** Do soil moisture and predicted LFMC improve the prediction of which ignitions become large fires,
   compared with weather alone and with the gridMET burning index?
3. **NISAR.** Does adding the NISAR SME2 change at 200 m keep or improve LFMC predictions against 2026 field
   samples, compared with SMAP alone?

A gain is claimed only if its 95 % interval excludes zero and, for the fire model, exceeds the seed noise.

## Data

One table from fuel-lake, 2016–2024, features sampled at the point and only from data on or before the row's date
(`docs/DATA.md`), extended by `python -m ml.series` with features computed from the lake's daily stores and fire
perimeters by the same code the map uses. LFMC rows: Globe-LFMC 2.0 to 2023, FEMS from 2024, without rows flagged
as outliers or out of range, one row per site and date (mean over species). Ignition rows: FPA-FOD; label = final
size ≥ 400 ha.

| Feature group | Columns | LFMC model | Large-fire model |
|---|---|---|---|
| gridMET weather of the day | wind, min RH, ERC, BI, 100-h and 1000-h fuel moisture, VPD, rain, max temperature, PET | yes | yes |
| HRRR 3 km wind of the day | `gust_max`, `wind10_max`: the larger of the 12 and 00 UTC analyses | no | yes |
| the same 15–90 days earlier (single days) | six of them at five lags | yes | no |
| SMAP L4 surface and root-zone soil moisture, and 15–90 days earlier | | yes | no |
| day of year | sine, cosine | yes | no |
| rain summed over 30 and 90 days | `pr_sum30`, `pr_sum90` | yes | yes |
| mean VPD of 30 days, wind maximum of 3 days | `vpd_mean30`, `vs_max3` | no | yes |
| dryness: soil moisture of the day as a z-score against the cell's previous 365 days | `rz_z12` (root zone; also 30, 60, 90 days earlier), `sf_z12` (surface) | no | yes |
| LANDFIRE fuels, canopy, terrain (`full` set only), as means over the 200 m cell | 17 columns | yes | yes |
| years since the last recorded fire | `years_since_fire` | no | after the model (fuel age) |

## Models

- **LFMC:** three LightGBM quantile models (5 %, 50 %, 95 %). The interval is widened by one constant so that it
  covers 90 % of out-of-fold samples (conformalized quantile regression, Romano et al. 2019).
- **Large fire:** the mean, in logit, of five LightGBM classifiers that differ only in their seed. Small trees
  (7 leaves, at least 200 ignitions per leaf), no class weights, monotone constraints: risk may only rise with
  wind and gusts, VPD, temperature, ERC, BI, PET, slope, and only fall with humidity, dead fuel moisture, rain sums and the
  dryness z-scores. The constraints fix the response outside the training range, which matters where the NISAR
  term moves a dryness anomaly on the map. Probabilities are calibrated with a sigmoid (Platt 1999) fitted on
  predictions for years the models did not see; `risk_index` ranks against those same predictions.
- **Vegetation type** (from v3.2): the share of the 200 m cell under each of 16 classes of LANDFIRE existing
  vegetation, large-fire model only, no monotone constraints. Accepted by a rule fixed before its test on
  2025–2026 (`docs/research/vegetation-type-out-of-time.md`).
- **Surroundings of the cell** (from v3.3, `ml.context`): means over discs of 1 and 3 km on the 200 m California
  grid of built-up, agriculture, burnable, forest and shrub shares, fuel load and canopy cover; standard deviation
  of elevation within 1 km; elevation minus its mean within 1 and 3 km. Large-fire model only, no monotone
  constraints, same release rule as the other LANDFIRE inputs. Accepted by a rule fixed before the run
  (`docs/research/landscape-context.md`).
- **Fuel age:** after calibration the odds are multiplied by exp(−β·exp(−years)) where a fire perimeter of the
  last 3.5 years covers the cell (β fitted on ignitions, for each test year from the other years; 12 on
  2016–2024), and stay lowered for 7 years where the fuel model of the oldest LANDFIRE release was a forest model
  (exp(−3.4·exp(−years/3))). It is a modifier and not a tree input because only 2.75 % of ignitions lie in scars.
  History: one curve for all vegetation fading over 3 years (v1, v2), over 2 years (v2.1, v3); both understated
  risk in old scars on open land in 2025–2026 (`docs/research/out-of-time.md`, `fuel-age-by-vegetation.md`).
- **How the settings were chosen.** A study of 269 variants (`docs/research/fire-model.md`) compared 5-seed
  ensembles in unseen years by a rule fixed in advance: the highest lower end of the gain interval on test years
  2016–2021, confirmed on 2022–2024. The configuration here is the variant of that study that uses the features
  of this repo. Its gain over the first version's model is +0.014 [0.002, 0.031] on all years; on 2022–2024 alone
  it is positive and its interval includes zero. Choosing among many variants on the same years overstates the
  gain; the confirmation years are the fair figure.
- What was tested and left out of the large-fire model because it did not hold up in unseen years: single-day
  weather lags, raw soil moisture, season, `lfmc_hat` (−0.0007 [−0.0036, +0.0020]), auxiliary labels (≥ 40 ha,
  ≥ 4 ha, log size), class weights, Rothermel outputs as features (+0.0006 [−0.0067, +0.0067]).

## Validation

| Check | What it measures |
|---|---|
| **LFMC: unseen block and year** | error where both the place (50 km block) and the year are new: the map's situation |
| LFMC: unseen block; unseen year | each alone (Roberts et al. 2017 for blocks) |
| LFMC: train ≤ 2023, test FEMS 2024 | new year and a different label source; mostly the same sites |
| LFMC: interval | the widening constant is taken from 2016–2023 and its coverage measured on 2024 |
| LFMC baselines | fold mean; linear on VPD and soil moisture |
| LFMC lag curve | weather plus soil moisture at one lag (0–90 days): gain over weather only, and best lag against lag 0 |
| **Fire: unseen year** (headline) | each year predicted by models that saw neither its ignitions nor its LFMC samples |
| Fire: unseen block, random split | secondary; see below |
| Fire baseline | gridMET burning index on the ignition day |
| Fire ablations | the model without each group in turn (dryness, antecedent sums, gusts, terrain and fuel, vegetation type, surroundings), weather of the day only, and the model plus `lfmc_hat`; 5-seed ensembles on both sides |
| Fire: a year without fuel layers | LANDFIRE fuel, canopy and vegetation layers start with the release valid from 2017-01-01, so the 2016 rows have terrain only. In the 2016 fold they are scored by models fitted without those layers (from v3.2); before, trees that had never seen a missing value there decided arbitrarily. For the same features this changes PR-AUC by −0.0045 [−0.0120, 0.0026] (`docs/versions/v3.2.md`) |
| Fuel age | β for each year fitted on the other years; observed against expected large fires inside scars before and after |
| 95 % intervals | bootstrap, 500 resamples, paired for differences: over blocks for LFMC, over calendar weeks for fire |
| Fire seed noise | spread of PR-AUC over the five single-seed models, and the change when the ensemble is refitted with five other seeds |

**Why the fire headline is by year.** Large fires cluster in a few statewide episodes. The weather of one day
is nearly the same in every block, so with block folds a model can recognise the episode from other blocks. On a
test table whose labels depended only on the episode and on no feature, block folds reported a PR-AUC of seven
times the base rate; leaving out the year reported none. The 2026 map is a year the models never saw.

**Years after training.** The ignitions of 2025–2026 (WFIGS, another source than the training labels) are scored
by the saved models (`docs/research/out-of-time.md`). Changes that cannot be judged in unseen years alone are
decided there by a rule written before the run. Two changes have been decided this way (vegetation type in v3.2,
surroundings in v3.3), so these years are now part of model selection: a further change needs newer data.

Fire metrics are PR-AUC (the base rate is 0.64 %), ROC-AUC, Brier score against the base rate, and reliability
by decile. With 477 large fires, small differences between variants are not resolvable; the intervals say so.

**What the ablations can and cannot show.** `lfmc_hat` is computed from weather and soil-moisture inputs like
those the fire model has. The step "+ `lfmc_hat`" therefore tests whether a representation learned from 26,000
field samples helps, not whether LFMC adds information. The step "dryness" is the test of soil moisture.
The lag curve shows how much soil moisture at each lag adds over weather; lags are correlated with each other,
so the best lag is reported with its interval against lag 0 and is not proof of a lead time.

## NISAR on the map

Training rows have SMAP only; NISAR starts in late 2025. NISAR enters the map in two places, both chosen from
measurements on the real products (`docs/research/sme2-resolution.md`, `docs/research/gcov.md`).

### SME2 soil moisture: the fine-scale dryness term

For a map on day D, per track and per 200 m cell:

1. anomaly = SME2 scene − mean of that track's earlier scenes at the cell (at least 6; values on the retrieval
   bounds 0.02 and 0.60 dropped; a scene whose anomaly spreads more than 3 times the track's median is left out);
2. fine part = 600 m mean of the anomaly − its 9 km mean (one SMAP cell);
3. mean over the passes of the last 24 days (the newest within 12 days of D), then over tracks; limited to
   ±0.03 m³/m³. Ascending and descending tracks are each compared with their own history only; their soil-moisture
   values are never averaged with each other.

That term is added to SMAP's surface soil moisture of the day. Divided by the spread in time of SME2's own 9 km
mean (0.017 m³/m³) and multiplied by its measured reliability (0.5 in cells flagged "recommended", 0.25 elsewhere),
limited to ±1, it is added to the two 12-month dryness z-scores (below). SMAP gives the level and the 9 km
anomaly; NISAR adds only what SMAP cannot resolve.

Why this form, from the study on 226 California scenes (November 2025 – May 2026):

- **Repeatable.** Two independent tracks 5–7 days apart agree on the fine part with r = 0.39 at 600 m (0.50 in
  recommended cells, 0.26 elsewhere; 0.22 in Santa Monica chaparral). The 9 km mean of the same anomaly agrees
  with 0.48, so the 200 m term is almost as repeatable as what SMAP-scale averaging gives.
- **Anomaly, not difference.** The difference between two passes agrees with 0.13–0.19 only; a reference of 6
  earlier scenes gives 0.28–0.30, 8 scenes 0.31–0.33. A mean of past scenes is as good as a whole-period mean.
- **SME2's own 9 km mean is subtracted, not SMAP's change.** SME2's 9 km mean follows SMAP in time (median r
  0.68) with about one sixth of SMAP's amplitude, so subtracting SMAP's change would mostly put the negative SMAP
  change on the map. (The first version of this repo did that.)
- **Short-lived.** On one track the term's autocorrelation is 0.32 after 12 days, 0.13 after 24: it is refreshed
  at every pass and set to nothing when no pass is recent.
- **Not shown to be soil moisture.** There is no in-situ soil moisture in the lake. In the Santa Monica
  Mountains the term adds R² 0.02–0.06 to terrain and season when predicting the later change of Sentinel-2 NDMI,
  and nothing beyond the current NDMI.

### SME2: the anomaly itself, as experimental layers

`sme2_anomaly` is the newest pass as a z-score against the earlier scenes of its track at the cell (at least 6;
spread floored at 0.01 m³/m³; limited to ±5). `p_large_fire_sme2` and `risk_index_sme2` scale the odds by 0.84 per
unit of it (limited to ±3 units). The evidence and its limits are in `docs/research/nisar-fire-season.md`: the
anomaly passed the pre-registered tests of association in the 2026 season, the coefficient was fitted on that same
season and for fires of 4 ha or more. `risk_index` does not use these layers.

### GCOV backscatter: fuel state after fire

`ml.gcov.fuel_state` (where GCOV is staged; today the Santa Monica Mountains): yearly mean L-band HV per track,
with the dependence on how a slope faces the radar removed (quadratic fit in shrub cells; the product has no
layover mask), tracks averaged, minus the mean of cells unburned since 2015 in the same pre-fire fuel model, slope,
facing and elevation class, averaged over 90 m. `fuel_state` = that HV power ratio, limited to 0.3–1.2: 1 is mature
fuel, lower is regrowth.

- Burned cells against matched unburned ones: −3.1 dB 1.4 years after fire, −2.0 dB after 7.5 years (Woolsey 2018).
- Detecting the 7.5-year-old scar: AUC 0.89 with L-HV, 0.65 with NDVI, 0.58 with the LANDFIRE 2025 fuel load,
  0.81 with Sentinel-1 C-VH. Optical greenness and the fuel map have recovered; L-band still sees the difference.
- Not a fuel-moisture measurement: its scene-to-scene change follows rain (r 0.8) and SME2 (r 0.9).

## Assumptions and limits

- **The NISAR soil term is small by construction.** Typical size 0.1–0.3 z-score units; it cannot move a cell's
  dryness anomaly by more than 1. `nisar_effect` reports what it changes. Its link to fire outcomes at 200 m is
  untested until the 2026 season is staged (`docs/EXPERIMENTS.md`, N2).
- **Scale transfer.** The models learned from 9 km soil moisture; adding a 600 m term assumes the relation holds
  at fine scale, and that a fine-scale surface anomaly comes with a root-zone anomaly of the same size, as it does
  at 9 km in SMAP (slope 0.99, r 0.92 between the two z-scores).
- **SME2 quality.** PROVISIONAL product; 45 % of valid cells in California are not flagged "recommended" (63 % in
  the Santa Monica Mountains) and are half as repeatable. They are used with half the weight;
  `--sme2-recommended-only` excludes them.
- **Only same-day inputs get NISAR**; lagged ones stay SMAP.
- **GCOV fuel state** is a relative index from one year of PROVISIONAL data in one region, compared across space
  (there is no L-band image from before the fires). "Unburned" means no perimeter since 2015.
- **Conditional risk.** `p_large_fire` is P(large | ignition), learned where ignitions happened. It says nothing
  about how likely an ignition is. `risk_index` is a rank among historical ignition-day conditions: from v3.2.1
  among the 13,333 ignitions of 2017–2024 in cells at most 10 % built up. Against all ignitions, of which more than
  half start in built-up cells, 45 % of California's burnable land was in the top fifth on an ordinary October day
  and the map showed little; against ignitions on open land the five classes hold 20 / 30 / 26 / 17 / 8 % of it.
  The probabilities are the same.
- **Non-burnable land.** The `core` models have no fuel input, so cropland, desert and urban cells are not masked.
  The `full` models mask cells that LANDFIRE marks non-burnable.
- **LFMC without vegetation type.** `core` predicts LFMC from weather and soil moisture only; differences between
  species at one place are not represented. Use the interval.
- **Retrospective.** Weather is the gridMET day, which includes hours after an ignition. No forecast is used.
- **Lags are single days**, not accumulated rain or drying.
- **Calibration and intervals** are fitted on 2016–2024 and are not guaranteed in 2026.
- **Labels.** Globe-LFMC and FEMS overlap in sites; FPA-FOD final sizes reflect suppression as well as conditions.
- **Static layers** (`full`): means over 200 m cells in the table and on the map (finer grids are averaged over
  windows of that size), from the LANDFIRE release valid on the date. Within-cell detail does not reach the models;
  the burnable mask, fuel age, the GCOV fuel state and the spread physics stay at the grid's own resolution.

## Reproducibility

fuel-lake is the copy in `../data-aggregation` (`pyproject.toml`); package versions are in `uv.lock`.
`meta.json` records the table hash, the features used and their fill rates; `result.json` records model hashes
and the NISAR scenes.

## References

- Abatzoglou, J. T. (2013). Development of gridded surface meteorological data for ecological applications and
  modelling. *Int. J. Climatol.* 33, 121–131.
- Jia, S., Kim, S. H., Nghiem, S. V., Kafatos, M. (2019). Estimating live fuel moisture using SMAP L-band
  radiometer soil moisture for Southern California, USA. *Remote Sens.* 11, 1575, doi:10.3390/rs11131575.
- Ke, G. et al. (2017). LightGBM: a highly efficient gradient boosting decision tree. *NeurIPS 30*.
- Platt, J. (1999). Probabilistic outputs for support vector machines and comparisons to regularized likelihood
  methods. *Advances in Large Margin Classifiers*.
- Roberts, D. R. et al. (2017). Cross-validation strategies for data with temporal, spatial, hierarchical, or
  phylogenetic structure. *Ecography* 40, 913–929.
- Romano, Y., Patterson, E., Candès, E. (2019). Conformalized quantile regression. *NeurIPS 32*.
- Shi et al. (2026). *Remote Sens.* 18, 3777, doi:10.3390/rs18213777 (SME2 PROVISIONAL at NEON sites).
- Yebra, M. et al. (2024). Globe-LFMC 2.0, an enhanced and updated dataset for live fuel moisture content
  research. *Sci. Data* 11, 332, doi:10.1038/s41597-024-03159-6.
- Data citations (SMAP L4, NISAR SME2, gridMET, LANDFIRE, FPA-FOD, FEMS): fuel-lake README, "Required attribution".
