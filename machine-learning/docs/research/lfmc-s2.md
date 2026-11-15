# LFMC (System A) with Sentinel-2 and daily-series features

Run 2026-11-15 on `point_features_v2_S2_5000.parquet` (identical to v1b plus the Sentinel-2 columns).
Exploratory; scripts and predictions are in `~/spaceapps/ml-work/research/lfmc_s2/` on the server.

## Verdict

- **Recommended System A:** the repo's feature set plus two features, `pr_sum30` and `pr_sum90` (precipitation
  summed over the 30 and 90 days ending on the date). Use `full + pr` where LANDFIRE layers are staged on the AOI,
  `core + pr` elsewhere. Same LightGBM quantile models and settings as `ml.train`.
- **Gain over the repo baseline (unseen block and year):** `full` 27.09 → 26.49 RMSE, gain 0.61 [0.32, 0.85];
  `core` 27.02 → 26.72, gain 0.30 [0.07, 0.50]. Both intervals exclude zero; the gain is about 1–2 %.
- **Sentinel-2 is not adopted.** Same-day NDVI/NDMI adds 0.4 RMSE at best and nothing once the model is trained with
  gaps. A per-site multi-year mean of NDVI/NDMI adds 0.68 [0.21, 1.23] when the mean may use later years, but
  0.13 [−0.06, 0.33] when it uses earlier years only, which is the situation of a 2026 map.
- **Sentinel-2 does not replace species.** Field-known species composition of the site improves RMSE by
  3.66 [2.58, 4.72]; the Sentinel-2 mean recovers about a fifth of that at most.
- **Keep the level target.** A model trained on anomalies from the site norm is worse at anomalies than the level
  model with its own past mean removed (22.91 against 21.94 RMSE, difference 0.97 [0.53, 1.53]).

## Protocol

Rows: `ml.data.lfmc_rows` (one row per site and date, mean over species): 15,591 rows, 488 site ids, 322
distinct locations, 102 blocks, 2016–2024. Headline: every (block fold, year) cell is predicted by a model that saw
neither the fold nor the year (`ml.train.lfmc_new_place_and_year`). Models: `ml.train.REG`, quantile 0.5, one
thread per fit. Intervals: bootstrap over blocks, 500 resamples, paired for differences. Positive gain = lower RMSE.

Site-level numbers use the location (lon/lat rounded to 4 decimals), not `site`: the `site` key changes between
Globe-LFMC (to 2023) and FEMS (2024) for the same place.

## 1. Sentinel-2 coverage

| | Rows | Locations | Blocks |
|---|---|---|---|
| All LFMC rows (site-date) | 15,591 | 322 | 102 |
| With Sentinel-2 in table v2 | 499 (3.2 %) | 207 | 91 |
| After extra sampling for this study | 4,626 (29.7 %) | 315 | 100 |

- Table v2: of 45,860 raw LFMC rows, 1,525 are `ok`, 251 `no_scene`, 134 `cloudy`, 1 `error`, 43,949 `not_sampled`.
  After `lfmc_rows`: 499 `ok`, 78 `no_scene`, 48 `cloudy`, 1 `error`. Median `s2_dt_days` −2 (range −8 to 0).
- Table rows by year: 38 / 58 / 52 / 72 / 107 / 86 / 0 / 86 for 2017–2024; 439 shrub, 60 tree; about 2 rows per
  site. Too few for a test: on these 499 rows the effect of NDVI/NDMI is 0.05 [−0.55, 0.71].
- Extra sampling: 4,629 site-dates (up to 12 per site id, from 2017-04) through fuel-lake's `S2PointSampler` with
  the table's settings (8-day causal window, 30 m footprint). 4,179 `ok`, 367 `cloudy`, 79 `no_scene`, 4 `error`.
  2024 is over-represented (1,700 rows), because FEMS site ids exist only in 2024. Median 14 scenes per location.
- NDVI in the table reaches 3.73; values outside [−1, 1] were set to missing.

## 2. Sentinel-2 variants

Features: `s2now` = NDVI, NDMI of the scene on or up to 8 days before the date; `s2anom` = `s2now` minus the mean
of the location's earlier scenes (at least 2); `s2mean` = mean NDVI, NDMI of the location over scenes from other
calendar years (at least 3 scenes, filled for 94.6 % of rows); `s2past` = the same from earlier years only
(filled for 57 %).

| Comparison (base → variant) | Rows scored | Gain in RMSE | 95 % interval |
|---|---|---|---|
| full+pr → + s2now, trained on Sentinel-2 rows only | 4,626 | 0.42 | [0.06, 0.75] |
| full+pr → + s2now + s2anom, Sentinel-2 rows only | 4,626 | 0.58 | [0.20, 0.88] |
| full+pr → + s2now, trained on all rows with gaps, scored on Sentinel-2 rows | 4,626 | 0.10 | [−0.14, 0.36] |
| full+pr → + s2mean, all rows | 15,591 | 0.68 | [0.21, 1.23] |
| full+pr → + s2now + s2mean, all rows | 15,591 | 0.74 | [0.33, 1.22] |
| full+pr → + s2past (earlier years only), all rows | 15,591 | 0.13 | [−0.06, 0.33] |
| full+pr → + s2past, rows where it exists | 8,953 | 0.16 | [−0.17, 0.52] |
| full+pr → + s2mean, the same 8,953 rows | 8,953 | 0.74 | [0.26, 1.27] |
| full+pr → + s2mean built from 3 random other-year scenes | 15,591 | 0.45 | [0.10, 0.83] |
| full+pr → + species composition of the site (oracle) | 15,591 | 3.66 | [2.58, 4.72] |
| core+pr → + s2mean, all rows | 15,591 | 0.84 | [0.18, 1.70] |

| Model, all rows | RMSE | R² | R² of site means | Within-site r |
|---|---|---|---|---|
| core (repo) | 27.02 [25.18, 29.07] | 0.389 | 0.332 | 0.637 |
| full (repo) | 27.09 [25.40, 28.89] | 0.386 | 0.343 | 0.651 |
| core + pr | 26.72 [24.83, 28.81] | 0.403 | 0.336 | 0.653 |
| full + pr | 26.49 [24.83, 28.34] | 0.413 | 0.359 | 0.671 |
| full + pr + s2mean | 25.80 [24.06, 27.74] | 0.443 | 0.391 | 0.686 |
| full + pr + s2past | 26.35 [24.68, 28.21] | 0.419 | 0.370 | 0.672 |
| full + pr + species oracle | 22.82 [21.23, 24.37] | 0.564 | 0.598 | 0.753 |

- **Why `s2mean` is not adopted.** A 2026 map can only use scenes from earlier years. In that form the gain's
  interval includes zero. Few scenes per location explain part of the drop but not all of it: a mean of only 3
  random other-year scenes still gives 0.45. The other-year means are dominated by 2024 scenes sampled in the fire
  season, so they may be more comparable between sites than a mix of seasons. This was not resolved.
- **Site level, whole state (289 locations with at least 3 scenes and 5 samples):** mean NDMI against mean LFMC
  r = 0.41, mean NDVI r = 0.31; against the share of chamise samples r = −0.38 and −0.25; against the share of
  shrub samples r = −0.41 and −0.31. The r = −0.91 seen at 9 Santa Monica sites does not hold statewide. A linear
  model of site-mean LFMC on mean NDVI and NDMI has R² 0.13 on unseen blocks.
- core → full: −0.07 [−0.83, 0.82], no difference in the headline. On FEMS 2024 (train to 2023, mostly the same
  sites) `full` is much better: RMSE 25.86 against 29.83.

## 3. Level target or anomaly target

Anomaly = LFMC minus the mean of the location's earlier samples (at least 5): 14,050 rows, SD 29.4.

| Anomaly prediction | RMSE | R² | r |
|---|---|---|---|
| Zero (the place norm) | 29.68 | −0.02 | |
| Anomaly target, day of year only | 25.13 | 0.27 | 0.52 |
| Anomaly target, full + pr | 22.91 | 0.39 | 0.63 |
| Anomaly target, full + pr + relative features | 22.41 | 0.42 | 0.65 |
| Level model full + pr, own past mean removed | 21.94 | 0.44 | 0.67 |
| Level model full + pr + relative features, own past mean removed | 21.44 | 0.47 | 0.69 |
| Level model full (repo), own past mean removed | 22.48 | 0.42 | 0.65 |

- "Own past mean removed" = the model's prediction minus the mean of its own predictions at the location's earlier
  sample dates. On a map this is the prediction minus the cell's own predicted norm; it needs no field data.
- The level model is better at anomalies than the anomaly-target model: 0.97 [0.53, 1.53] RMSE.
- Changes over time are predicted better than the level: anomaly R² 0.44 against level R² 0.41, within-site r 0.67.
  Reporting the map as a departure from the cell's own norm is supported; training on anomalies is not.
- Relative features (`rz_anom12`, `vpd_mean30` and `pr_sum90` divided by the location's 2015–2026 means, and those
  means) improve anomaly skill by 0.50 [0.10, 0.91] RMSE but not the level (0.21 [−0.26, 0.61]). Not in the
  recommendation because the headline metric does not move.
- For reference, with field history at the site: site past mean + predicted anomaly gives level RMSE 22.91 on
  these rows against 25.95 for the level model.

## 4. Features from daily series

Computed causally from the daily gridMET and SMAP L4 series at each location.

| Added to `full` | Gain in RMSE | 95 % interval |
|---|---|---|
| `pr_sum30`, `pr_sum90` | 0.61 | [0.32, 0.85] |
| `vpd_mean30` | 0.05 | [−0.07, 0.18] |
| `rz_anom12` (root zone against the previous 365 days, z-score) | 0.07 | [−0.04, 0.17] |
| all four | 0.63 | [0.38, 0.90] |
| all four, added to `core` | 0.51 | [0.23, 0.75] |

Only the precipitation sums help beyond the single-day lags already in the table.

## 5. Intervals (5 %, 50 %, 95 % quantile models)

| Set | Raw coverage, unseen block | Raw width | Widening constant | Width after widening | Coverage on 2024, constant from 2016–2023 | Coverage, unseen block and year |
|---|---|---|---|---|---|---|
| full (repo) | 0.722 | 51.5 | 10.63 | 72.8 | 0.862 | 0.895 |
| full + pr | 0.728 | 50.2 | 10.10 | 70.4 | 0.859 | 0.893 |
| core + pr | 0.800 | 58.2 | 6.78 | 71.7 | 0.876 | 0.891 |

The widened interval covers about 89 % in the map's situation and 86–88 % on 2024. It is about 70 LFMC points wide.

## Recommended configuration

- **Features:** `ml.data.FEATURES["full"]` + `pr_sum30` + `pr_sum90` (or `FEATURES["core"]` + the two where LANDFIRE
  is not staged on the AOI).
- **Definitions:** `pr_sum30` = sum of gridMET `pr` over the 30 days ending on the date (D−29 … D), at least 24
  valid days; `pr_sum90` = the same over 90 days, at least 72 valid days. They are sums of the same daily values
  `WeatherSeries` returns (`points` for table rows, `grid_slice` for map cells); summing commutes with the bilinear
  sampling.
- **Models:** unchanged (`ml.train.REG`, quantiles 0.05 / 0.5 / 0.95, conformal widening).
- **Map product:** LFMC with the interval, plus LFMC minus the cell's own predicted norm as the "departure from
  normal" layer.

## Limits

- The extra Sentinel-2 samples are not in the lake or the table; they are in `s2_extra.parquet` in the study folder.
- `s2mean` here averages scenes at field-sampling dates, not a full composite; a raster composite for a map would
  weight seasons differently.
- The location normals for the relative features use 2015–2026, including the test year's weather (no labels).
- No tuning; one seed.
- 2023 has 33 rows: Globe-LFMC ends early in 2023 and FEMS is used only from 2024.

## Files

`h.py` (harness), `s2sample.py` (extra sampling), `st1.py` (daily features), `st2.py` (Sentinel-2), `st3.py`
(targets, intervals), `st4.py` (final candidates), `st5.py` (scene-count check), `st*.json`, `st*_pred.pkl`,
`site_level.csv`, `s2_extra.parquet`.
