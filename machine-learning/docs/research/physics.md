# Physical spread component: Rothermel surface fire on the 40 Scott & Burgan fuel models

Research note, 2026-11-15. Files: `rothermel.py`, `test_rothermel.py` (module and tests, numpy only), `prep2.py`, `hyb.py`, `summ.py`
(validation; `val.py` is the same in one process), `sens.py` (sensitivity, run time), `nbhd.py` (neighbourhood fuels). Nothing here is tuned on test data.

## Findings in brief

1. **The module is correct against an independent implementation**: with the same parameters it reproduces
   pyretechnics within 0.02 % for all 40 fuel models.
2. **Physical quantities rank large fires without any training.** Mean flame length around the ignition reaches
   PR-AUC 0.033 (ROC-AUC 0.79) on 2017-2024 ignitions, against 0.015 (0.59) for the gridMET burning index and a
   base rate of 0.0065.
3. **They add nothing to the `full` LightGBM model** (+0.0006 PR-AUC, 95 % interval -0.0067 to +0.0067, year never
   seen), which already has fuels, terrain and weather. They lift the `core` model by +0.023 (interval +0.012 to
   +0.038), the same as the static layers do. Nine physical features alone give 0.032, against 0.018 for the 54
   `core` features.
4. **LFMC through the physics changes nothing measurable**: with constant live moisture (woody 90 %, herbaceous
   60 %) instead of LFMC and the seasonal herbaceous moisture, the hybrid changes by +0.0012 PR-AUC (interval -0.0039
   to +0.0052), and the untrained rankings are the same within 0.001. Physically LFMC matters only in shrub and
   timber-understory models (+-15 to 20 % in spread rate for +-20 points of LFMC).
5. **Herbaceous moisture is the physically powerful live-fuel input** (grass models spread 100 times or more faster cured
   than green), and grass and grass-shrub models are 58 % of the burnable ignition pixels. Three ways of setting it
   (constant, season, soil-moisture anomaly) ranked fires the same; there are no herbaceous field samples in the lake
   to test it directly.
6. **Fuel at the ignition pixel is misleading**: 64 % of ignitions with a fuel model sit on a non-burnable pixel
   (roads). Features for ignitions should come from the neighbourhood.

## What was built

`rothermel.spread(code, m_1h, m_10h, m_100h, m_herb, m_woody, wind, wind_from, slope, aspect)` returns, per cell or row,
the head-fire **rate of spread (m/min), fireline intensity (kW/m), flame length (m), direction of maximum spread
(degrees)**, the no-wind no-slope rate and the length-to-width ratio of the fire ellipse. It implements Rothermel (1972)
with the Albini (1976) revisions as documented by Andrews (2018), the dynamic herbaceous load transfer of the Scott &
Burgan (2005) models, vector addition of wind and slope on the slope plane, the 0.9 x reaction-intensity wind limit and
Byram's flame length. Fuel parameters are Table 7 of Scott & Burgan (2005), copied from fuel-lake's
`config/tables/fbfm40_parameters.csv` (which fuel-lake's own test checks against the PDF). Non-burnable codes give 0,
unknown codes NaN. **LFMC enters as the live woody fuel moisture.**

`rothermel.grid_layers(...)` takes arrays on a map grid (fuel code, gridMET day, LFMC, slope, aspect, optional canopy)
and returns `rate_of_spread`, `flame_length`, `fireline_intensity`, `spread_direction` as float32.

Surface fire only. No crown fire, no spotting, no fire growth simulation.

## Inputs and conversions

| Input of the model | Taken from | Conversion |
|---|---|---|
| 1-h, 10-h dead fuel moisture | gridMET `rmin`, `tmmx` | equilibrium moisture content (Simard 1968) at the driest hour; 1-h = 1.03 x EMC, 10-h = 1.28 x EMC (1978 NFDRS, Bradshaw et al. 1984) |
| 100-h dead fuel moisture | gridMET `fm100` | as is |
| live woody moisture | LFMC (`lfmc_hat`) | as is; control variant: constant 90 % |
| live herbaceous moisture | greenness 0-1 | 30 % (cured) to 120 % (green), the range of the dynamic load transfer. Variants: constant 60 %; season (cosine, greenest mid-March); surface soil moisture anomaly against the previous 12 months (SMAP z-score, `0.5 + 0.5 z`) |
| midflame wind | gridMET `vs` (10 m, daily mean) | 20-ft wind = 10 m / 1.15 (Turner & Lawson 1978); wind adjustment factor of Andrews (2012): unsheltered from fuel bed depth, sheltered under canopy from LANDFIRE cover and height |
| wind direction, slope, aspect | gridMET `th`, LANDFIRE topography | degrees; flat cells without aspect use slope only |
| fuel model | LANDFIRE FBFM40, release valid before the date | 2016 ignitions have no causal release and get NaN |

## Checks (`test_rothermel.py`, 7 tests, all pass)

- **Equations against pyretechnics** (Pyregence, EPL-2.0, version 2026.10.20), 40 models x 100 random cases of
  moisture, wind, slope and aspect, with pyretechnics' own parameter table: largest relative difference 0.02 % in rate
  of spread, 0.02 % in intensity, 0.01 % in flame length, 0.02 degrees in direction.
- **With the published parameters**: pyretechnics stores loads in lb/ft2 rounded to four decimals, this module uses
  the t/ac values of Table 7. Rate of spread differs by 0.02 % (median), 0.5 % (99th percentile), 3.4 % (maximum, in
  cases close to the moisture of extinction).
- Table: 40 burnable models; packing ratio of GR1 equals the printed 0.00143.
- Behaviour: faster with wind and with drier dead fuel; zero above the moisture of extinction; spread direction
  downwind on flat ground and upslope without wind; drier LFMC is faster in shrub models; cured grass is faster in
  the dynamic models.
- Wind adjustment factor (unsheltered and under canopy) equals pyretechnics within 0.1 %.
- No reference values from BehavePlus were available offline; pyretechnics is the independent reference.

pyretechnics is needed only for the comparison tests. The module itself imports numpy only.

## Validation on the 2016-2024 ignitions

74,568 FPA-FOD ignitions, 477 large fires (>= 400 ha). LFMC for an ignition comes from an LFMC model
(weather, soil moisture, season; no species) that did not see the ignition's year.

**The ignition pixel is the wrong place to evaluate fuel.** 42,514 of the
66,674 ignitions with a fuel model fall on a non-burnable 30 m pixel (mostly "urban/developed":
roads), and 113 of the 431 large fires started there. A spread
rate computed at the pixel is zero for them. Each ignition therefore also got the fuel model at 16 points around it
(8 at 120 m, 8 at 300 m, same LANDFIRE release, sampled with fuel-lake's sampler); `*_nb` is the mean over the 17
samples (non-burnable = 0), `*_max` the maximum, `burn_frac` the burnable share.

### Physical quantities alone (no training)

Ranking of large fires by one quantity. "Fuel known" = 2017-2024 ignitions (66,674 rows, base
rate 0.0065); "burnable pixel" = ignition pixel has a burnable model
(24,160 rows, 318 large fires, base rate 0.0132).

| Quantity (herb by season, live woody = LFMC) | PR-AUC, fuel known | ROC-AUC | PR-AUC, burnable pixel | ROC-AUC |
|---|---|---|---|---|
| rate of spread, pixel | 0.0232 | 0.73 | 0.0291 | 0.64 |
| flame length, pixel | 0.0258 | 0.74 | 0.0327 | 0.70 |
| fireline intensity, pixel | 0.0258 | 0.74 | 0.0327 | 0.70 |
| rate of spread, neighbourhood mean | 0.0225 | 0.72 | 0.0337 | 0.66 |
| flame length, neighbourhood mean | 0.0334 | 0.79 | 0.0448 | 0.74 |
| intensity, neighbourhood mean | 0.0270 | 0.77 | 0.0382 | 0.72 |
| rate of spread, neighbourhood max | 0.0151 | 0.64 | 0.0308 | 0.63 |
| flame length, neighbourhood max | 0.0197 | 0.72 | 0.0296 | 0.67 |
| burnable share (no physics) | 0.0168 | 0.77 | 0.0191 | 0.65 |
| gridMET burning index `bi` (baseline) | 0.0147 | 0.59 | 0.0300 | 0.60 |
| wind speed `vs` | 0.0078 | 0.51 | 0.0173 | 0.52 |
| `vpd` | 0.0101 | 0.64 | 0.0240 | 0.67 |
| `erc` | 0.0100 | 0.65 | 0.0217 | 0.65 |
| slope | 0.0229 | 0.74 | 0.0290 | 0.66 |

Live-moisture variants, neighbourhood means (PR-AUC / ROC-AUC, fuel known):

| Herbaceous / live woody | rate of spread | flame length |
|---|---|---|
| constant 60 % / constant 90 % | 0.0221 / 0.71 | 0.0324 / 0.80 |
| constant 60 % / LFMC | 0.0215 / 0.71 | 0.0326 / 0.80 |
| season / constant 90 % | 0.0234 / 0.72 | 0.0338 / 0.80 |
| season / LFMC | 0.0225 / 0.72 | 0.0334 / 0.79 |
| soil moisture anomaly / constant 90 % | 0.0216 / 0.71 | 0.0342 / 0.80 |
| soil moisture anomaly / LFMC | 0.0213 / 0.71 | 0.0337 / 0.80 |

### As features of the LightGBM model (hybrid), year never seen

All 74,568 ignitions, each year predicted by a model that did not see it (`ml.train.fire_cv`, repo settings).
`core` = weather, soil moisture, season, `lfmc_hat`; `full` adds LANDFIRE fuels, canopy and terrain.

| Features | PR-AUC | ROC-AUC |
|---|---|---|
| core | 0.0183 | 0.74 |
| core+physics | 0.0415 | 0.79 |
| full | 0.0409 | 0.82 |
| full+burn_frac | 0.0418 | 0.81 |
| full+physics | 0.0415 | 0.81 |
| full+physics(point only) | 0.0366 | 0.82 |
| full+physics(const live 90, herb 60) | 0.0427 | 0.82 |
| full+physics(herb from soil anomaly) | 0.0408 | 0.82 |
| physics only | 0.0324 | 0.74 |

Paired differences in PR-AUC, 95 % interval from 500 bootstrap resamples of calendar weeks:

| Difference | Gain | 95 % interval |
|---|---|---|
| full+physics - full | +0.0006 | [-0.0067, +0.0067] |
| full+burn_frac - full | +0.0009 | [-0.0077, +0.0069] |
| full+physics - full+burn_frac | -0.0003 | [-0.0061, +0.0059] |
| core+physics - core | +0.0232 | [+0.0121, +0.0376] |
| full+physics(point only) - full | -0.0043 | [-0.0127, +0.0005] |
| full+physics - full+physics(const live 90, herb 60) | -0.0012 | [-0.0052, +0.0039] |
| full+physics(herb from soil anomaly) - full+physics | -0.0007 | [-0.0049, +0.0036] |
| full - core | +0.0227 | [+0.0129, +0.0375] |
| physics only - core | +0.0141 | [+0.0056, +0.0262] |

Seed noise (change in PR-AUC from refitting with another seed): `full` 0.0002, `full+physics` 0.0040.
Share of large fires in the 10 % of ignitions ranked highest: `full` 50%, `full+physics` 48%.

### Operator thresholds from the brief

Flame length above 10 ft (3.05 m) and rate of spread above 30 chains/h (10.06 m/min), 2017-2024 ignitions, herb by
season, live woody = LFMC.

| Criterion | Ignitions above | Large fires above | Rate above | Rate below | Share of all large fires above |
|---|---|---|---|---|---|
| flame_pt>3.05m | 522 | 24 | 4.60% | 0.62% | 6% |
| ros_pt>10.06m/min | 911 | 37 | 4.06% | 0.60% | 9% |
| flame_max>3.05m | 4,284 | 81 | 1.89% | 0.56% | 19% |
| ros_max>10.06m/min | 6,034 | 88 | 1.46% | 0.57% | 20% |
| flame_nb>3.05m | 93 | 9 | 9.68% | 0.63% | 2% |
| ros_nb>10.06m/min | 612 | 23 | 3.76% | 0.62% | 5% |

(`pt` = ignition pixel, `nb` = neighbourhood mean, `max` = neighbourhood maximum.) Median / 90th / 99th percentile:
rate of spread, neighbourhood mean 0.8 / 4.0 / 9.7 m/min; flame length,
neighbourhood maximum 1.0 / 2.3 / 4.6 m.

Large-fire rate by decile of the neighbourhood-mean rate of spread (lowest to highest): 0.07%, 0.16%, 0.09%, 0.27%, 0.60%, 0.66%, 0.76%, 0.94%, 1.18%, 1.71%.

## Sensitivity of the spread rate to live fuel moisture

Reference: 1-h 5 %, 10-h 6 %, 100-h 8 %, herbaceous 60 %, LFMC 90 %, midflame wind 2.5 m/s, flat.

| Model | ROS m/min | Flame m | LFMC 90 -> 70 | LFMC 90 -> 110 | ROS(LFMC 60) / ROS(LFMC 140) | Herb 60 -> 30 | Herb 60 -> 90 | ROS(herb 30) / ROS(herb 120) | Dead fuel -2 points | Wind x 2 |
|---|---|---|---|---|---|---|---|---|---|---|
| GR1 | 5.2 | 0.6 | +0% | +0% | 1.00 | +17% | -56% | 99.5 | +35% | +0% |
| GR2 | 14.3 | 1.6 | +0% | +0% | 1.00 | +46% | -54% | 463.5 | +14% | +168% |
| GR4 | 29.0 | 2.9 | +0% | +0% | 1.00 | +44% | -52% | 121.0 | +15% | +169% |
| GS1 | 6.4 | 1.1 | +18% | -18% | 2.77 | +16% | -85% | 38.5 | +10% | +168% |
| GS2 | 9.2 | 1.7 | +17% | -13% | 1.80 | +11% | -24% | 6.2 | +9% | +168% |
| SH1 | 0.6 | 0.2 | +19% | -14% | 1.84 | +38% | -32% | 3.2 | +37% | +0% |
| SH2 | 2.2 | 1.3 | +57% | -66% | 6.44 | +0% | +0% | 1.0 | +37% | +151% |
| SH5 | 22.6 | 4.5 | +22% | -15% | 1.99 | +0% | +0% | 1.0 | +11% | +124% |
| SH7 | 14.7 | 4.3 | +23% | -16% | 2.02 | +0% | +0% | 1.0 | +10% | +121% |
| TU1 | 1.2 | 0.6 | +20% | -19% | 2.81 | +4% | -15% | 3.7 | +8% | +147% |
| TU5 | 3.7 | 2.5 | +15% | -11% | 1.64 | +0% | +0% | 1.0 | +13% | +114% |
| TL3 | 0.7 | 0.4 | +0% | +0% | 1.00 | +0% | +0% | 1.0 | +23% | +64% |
| TL6 | 2.6 | 0.9 | +0% | +0% | 1.00 | +0% | +0% | 1.0 | +24% | +168% |

## Map function and run time

`grid_layers` on random inputs, one process, numpy, chunks of 1 M cells:

| Grid | Cells | Time |
|---|---|---|
| Santa Monica Mountains, 30 m | 1.2 M | 3.8 s |
| California, 200 m | 25 M | 87 s |

Memory is about 1 GB per chunk. The spread direction and length-to-width ratio are what an elliptical spread
envelope needs (the backend's `spread.py` draws such envelopes from wind alone).

## Simplifications and limits

- **Wind**: gridMET daily mean at 10 m on a 4 km grid. No gusts, no diurnal peak, no terrain channelling. The mean
  understates the wind of the burning period, so absolute spread rates are low (median 0.8 m/min around ignitions)
  and few cells pass the operator thresholds. Wind speed alone does not rank large fires here (ROC-AUC 0.51).
- **Surface fire only**: canopy fuels, crown fire and spotting are ignored; the canopy is used only to shelter the wind.
- **Dead fuel moisture**: 1-h and 10-h from the daily minimum humidity and maximum temperature, i.e. the driest hour,
  without the solar-heating correction of the fuel surface.
- **Herbaceous moisture** is an assumption in all three variants. The soil-anomaly variant uses SMAP surface soil
  moisture against the previous 12 months; it is where NISAR SME2 would enter physically, and it is untested.
- **LFMC** is the model without species (weather, soil moisture, season), one value per ignition for every shrub.
- **Fuel model**: LANDFIRE release valid before the date (LF 2016 for 2017 to September 2022), so fuels are up to six
  years old and 2016 ignitions have none. The neighbourhood is 16 points within 300 m.
- **Label**: final fire size depends on suppression and access as well as on spread.
- **Hybrid features carry fuel and terrain**: the gain over `core` is not evidence for the physics itself, because
  `core` has no fuel or slope input. Against `full`, which has them, there is no gain.
- Thresholds, deciles and raw rankings use 2017-2024; LightGBM results use all years (NaN physics in 2016).
- With 477 large fires, differences in PR-AUC below about 0.005 are not resolvable.

## Recommendation

- **Map**: add `rate_of_spread`, `flame_length`, `fireline_intensity` and `spread_direction` as output layers
  (`rothermel.grid_layers`, 4 s for the 30 m AOI). They need the FBFM40 code, slope and aspect on the grid, so they
  can be made wherever the `full` models can (today the Santa Monica Mountains). They give the result physical
  units and a direction, and LFMC a physical role in shrub fuels. Present them as conditional on the daily mean wind.
- **Risk model**: keep `full` as it is. Do not add the physical features to it: no gain, and one more dependency.
  Do not claim that LFMC improves the fire model through the physics.
- **If one transparent index is wanted** next to the learned probability, use the neighbourhood-mean flame length:
  untrained, 2.3 times the PR-AUC of the burning index.
- **Ignition features**: any future use of fuels at ignitions should average a neighbourhood, not read the pixel.
- **NISAR**: the physically coherent entry is herbaceous curing in grass and grass-shrub fuels (surface soil moisture
  drives shallow-rooted grasses). The sensitivity is large; the evidence is missing. A test needs herbaceous
  moisture or greenness (Sentinel-2 NDVI in grass pixels) against SME2.

## References

- Albini, F. A. (1976). Estimating wildfire behavior and effects. USDA Forest Service GTR INT-30.
- Andrews, P. L. (2012). Modeling wind adjustment factor and midflame wind speed for Rothermel's surface fire
  spread model. RMRS-GTR-266.
- Andrews, P. L. (2018). The Rothermel surface fire spread model and associated developments. RMRS-GTR-371.
- Bradshaw, L. S., Deeming, J. E., Burgan, R. E., Cohen, J. D. (1984). The 1978 National Fire-Danger Rating System:
  technical documentation. GTR INT-169.
- Burgan, R. E. (1979). Estimating live fuel moisture for the 1978 National Fire Danger Rating System. INT-226.
- Byram, G. M. (1959). Combustion of forest fuels. In: Forest fire: control and use.
- Rothermel, R. C. (1972). A mathematical model for predicting fire spread in wildland fuels. INT-115.
- Scott, J. H., Burgan, R. E. (2005). Standard fire behavior fuel models. RMRS-GTR-153.
- Simard, A. J. (1968). The moisture content of forest fuels. Forest Fire Research Institute, Ottawa.
- Turner, J. A., Lawson, B. D. (1978). Weather in the Canadian Forest Fire Danger Rating System. BC-X-177.
- pyretechnics 2026.10.20 (Pyregence Consortium), EPL-2.0: used only as the test reference.

## Files (server paths, `~/spaceapps/ml-work/research/physics/`)

| File | Content |
|---|---|
| `rothermel.py` | the module (numpy only) |
| `test_rothermel.py` | 7 tests; `uv run --no-project --with pyretechnics --with pytest --with numpy pytest test_rothermel.py` |
| `physics_features.parquet` | per ignition: `lfmc_hat` (year held out) and the physical features for three moisture variants |
| `nbhd_fuel.parquet`, `nbhd.py` | FBFM40 at 16 points around each ignition |
| `prep2.py`, `hyb.py`, `summ.py`, `lf1.py` | validation (features, one LightGBM variant per process, metrics and intervals, LFMC per year) |
| `val.json`, `preds/` | all numbers above; out-of-year predictions per variant |
| `sens.py`, `sens.json` | sensitivity table and run time |
