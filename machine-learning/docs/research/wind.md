# HRRR gusts and hourly-maximum wind in the large-fire model

Run 2026-11-15 on the v2 table and protocol. Scripts, logs and tables are on the server in
`~/spaceapps/ml-work/research/wind/`; the staged data are in `~/spaceapps/lanes/W01` … `W27` and visible through the
lake view `~/spaceapps/ml-work/lake`.

## Verdict

- **HRRR wind carries what the model lacks.** With the v2 out-of-year prediction held fixed, one standard deviation
  more of the day's gust raises the odds of a large fire by 1.35 [1.22, 1.49]; of the hourly-maximum 10 m wind by
  1.41 [1.26, 1.57]. gridMET's daily mean wind, the only wind v2 has, shows nothing (1.03 [0.93, 1.14]).
- **Adding it improves the ranking, significantly for ROC-AUC and for the top 5–10 %, not for PR-AUC.** Every
  pre-registered variant: ROC-AUC +0.017 to +0.021 (all intervals exclude zero), large fires in the top 5 % of
  ignitions +4.4 to +5.9 points, in the top 10 % +4.2 to +5.9 points (all exclude zero); PR-AUC +0.002 to +0.009
  (every interval includes zero), top 1 % unchanged.
- **By the rule fixed in advance (PR-AUC interval above zero) no variant is adopted as a PR-AUC gain.** The gain
  is positive in all six variants and significant in the metrics that describe the whole ranking.
- **Recommended for v3: W2**, v2 + `gust_max` + `wind10_max`. It has the highest lower end of the PR-AUC interval
  among the six, uses operations `ml.series` already has, and both fields are staged for the map day on `ca` and
  `smm`. PR-AUC 0.0675 → 0.0752 (+0.0077 [−0.0024, +0.0170]), ROC-AUC 0.835 → 0.856 (+0.020 [0.013, 0.030]), top
  5 % 37.5 → 43.0 % (+5.5 [1.5, 8.4]), top 10 % 52.4 → 57.7 % (+5.2 [1.9, 8.2]).

## Data

HRRR (NOAA, 3 km, hourly) through fuel-lake's `hrrr-gusts` connector, AOI `ca`, 2016-01-01 → 2026-11-14.

| | |
|---|---|
| Variables | `gust_max`: surface gust analyses; `wind10_max`: hourly maximum 10 m wind (1-hour forecasts) |
| Sampling | the connector's default: two valid hours per gridMET day, 12 UTC and 00 UTC (05 and 17 PDT) |
| Volume | 18.6 GB of GRIB messages downloaded (each message is the whole CONUS grid, about 1.2 MB); 4.7 GB kept (day crops 4 GB, monthly Zarr stores 0.64 GB) |
| Time | 9 minutes for the first pass in 27 lake roots (5 months each), 2 minutes for a second pass |
| Days staged | 3,960 of 3,971. Absent from the archive: 2016-03-11, 03-18 to 03-20, 06-15, 08-09, 11-24 to 11-26, 2017-05-12, 06-27 |
| Samples per day | gust: 2 on 3,844 days, 1 on 112, 0 on 4; hourly-maximum wind: 2 on 3,845, 1 on 107, 0 on 8 |
| Fill at ignitions | 2016: 98.1 % / 96.2 % (gust / wind); 2017: 98.2 % / 99.0 %; 2018: 99.7 % / 100 %; 2019–2024: 100 % |

- `--force-scale` is needed (`hrrr-gusts` is declared `scales: [local]`). The connector fetches one message after
  another, about 1.6 s each from this server, so one process would need about 6 hours for 2016–2024; 27 processes
  on 27 lake roots (one writer each) reached about 45 MB/s.
- Two changes to the connector were needed for 2016–2017, made in a worktree (`~/spaceapps/ml-work/da-wt-wind`),
  not in the repo's working directory; the diff is `hrrr_gusts.patch`: (1) a day with no sampled file in the
  archive is skipped instead of failing its month (6 months affected); (2) the HRRRv2 upgrade of 2016-08-23 moved
  the grid origin by 53 m (1.8 % of a cell), which the connector treated as a different grid and failed August
  2016; origins less than a tenth of a cell apart are now one grid. `ml.series.Field` places all months on the
  first store's grid, so the 53 m shift of the seven earlier months is ignored.
- View: `merge_view.py` links the 131 `ca` items of `W01` … `W27` and the 23 `smm` items of `lanes/D` into
  `lake/.merged/` and re-points `catalog/hrrr-gusts` and `staged/hrrr-gusts` atomically.
  `LakeClient.search("hrrr-gusts", aoi="ca")` returns 131 items, `aoi="smm"` 23.

### Are two hours a day enough?

Hourly gust analyses for 24 days (the ignition days of the 12 largest fires since 2018 and 12 random ignition days),
574 of 576 messages, cropped to California (`calib.py`, `calib.csv`).

| | |
|---|---|
| Share of the hourly daily maximum kept by the two samples, mean over cells | 0.77 (0.70–0.83 by day) |
| The same in the windiest tenth of cells | 0.78 |
| Rank correlation over cells within a day, sampled against hourly maximum | 0.75 (0.51–0.89 by day) |
| Correlation over days of the statewide mean | 0.96 |
| Hour of the statewide peak | 00–03 UTC on 18 of 24 days; 15–16 UTC on 2; 22–23 UTC on 3; 07 UTC on 1 |

The two samples are a steady 77 % of the daily maximum and rank days well. They rank places within a day only
moderately. More hours were not downloaded (each extra hour adds 9 GB); whether they would add skill is untested.

## Features (`feat.py`, table `points_v2_wind.parquet`)

Computed for all 120,428 rows of `points_v2.parquet` through `ml.series.Field` (bilinear on the native HRRR grid, the
code path the map uses).

| Feature | Definition |
|---|---|
| `gust_max`, `wind10_max` | the day's value |
| `gust_max3`, `wind10_max3` | maximum of the day and the two days before |
| `gust_ratio`, `wind10_ratio` | the day's value divided by the cell's mean over 2016–2024 |
| `gust_x_vpd` | `gust_max` × the day's gridMET VPD |

At the ignitions: `gust_max` 6.2 ± 2.9 m/s, `wind10_max` 5.1 ± 2.0 m/s, gridMET `vs` 3.3 ± 1.4 m/s. Rank correlation
of `gust_max` with `vs` 0.46, with `wind10_max` 0.77, with VPD −0.01.

## What gusts carry beyond v2 (`an.py`, `odds.csv`)

Logistic regression on 74,568 ignitions (477 large), standard errors clustered by 50 km block; "with v2" adds the logit
of the v2 prediction made without the ignition's year.

| Feature | Odds ratio per sd, alone | With v2 prediction | Large fires by quintile, low to high, % |
|---|---|---|---|
| `gust_max` | 1.43 [1.31, 1.56] | 1.35 [1.22, 1.49] | 0.25 / 0.55 / 0.55 / 0.75 / 1.07 |
| `wind10_max` | 1.61 [1.45, 1.78] | 1.41 [1.26, 1.57] | 0.17 / 0.47 / 0.51 / 0.68 / 1.38 |
| `gust_max3` | 1.34 [1.21, 1.49] | 1.29 [1.15, 1.45] | 0.36 / 0.50 / 0.64 / 0.72 / 0.98 |
| `wind10_max3` | 1.53 [1.36, 1.71] | 1.35 [1.20, 1.52] | 0.22 / 0.44 / 0.55 / 0.78 / 1.21 |
| `gust_ratio` | 1.33 [1.23, 1.44] | 1.39 [1.29, 1.49] | 0.38 / 0.71 / 0.55 / 0.57 / 0.96 |
| `wind10_ratio` | 1.46 [1.36, 1.57] | 1.51 [1.40, 1.64] | 0.33 / 0.46 / 0.64 / 0.62 / 1.15 |
| gridMET `vs` | 1.06 [0.93, 1.21] | 1.03 [0.93, 1.14] | 0.61 / 0.74 / 0.58 / 0.66 / 0.61 |
| gridMET `vs_max3` | 0.94 [0.81, 1.09] | 0.98 [0.87, 1.10] | 0.81 / 0.64 / 0.68 / 0.52 / 0.54 |

- v2 misses along the gust: in the calmest fifth of ignitions it expects 72.5 large fires and 37 occurred; in the
  windiest fifth it expects 102.6 and 159 occurred.
- Wind and dryness multiply, as found before: large fires in 0.13 % of calm-and-moist ignitions (12 fires), 1.55 %
  of windy-and-dry ones (119 fires); terciles of gust and VPD.

## Variants in the v2 protocol

Fixed before any result (`PREREG.md`). Every year predicted by models that did not see it, mean of 5 seeds, fuel age
applied, LFMC features unchanged, new features with monotone constraint +1. Changes against v2 `full` are paired on the
same ignitions with a bootstrap over calendar weeks, 500 resamples (`ml.compare` for PR-AUC, `boot.py` for the rest).
Reference v2: PR-AUC 0.0675, ROC-AUC 0.835, large fires in the top 1 / 5 / 10 %: 17.2 / 37.5 / 52.4 %.

| Variant | Large-fire features | PR-AUC | Change [95 %] | ROC-AUC | Change [95 %] | Top 5 % | Change [95 %] | Top 10 % | Change [95 %] |
|---|---|---|---|---|---|---|---|---|---|
| W1 | + `gust_max` | 0.0714 | +0.0040 [−0.0029, +0.0111] | 0.852 | +0.017 [0.010, 0.025] | 42.3 % | +4.8 [1.3, 7.5] | 56.6 % | +4.2 [1.3, 7.6] |
| **W2** | + `gust_max`, `wind10_max` | 0.0752 | +0.0077 [−0.0024, +0.0170] | 0.856 | +0.020 [0.013, 0.030] | 43.0 % | +5.5 [1.5, 8.4] | 57.7 % | +5.2 [1.9, 8.2] |
| W3 | + both and their 3-day maxima | 0.0761 | +0.0087 [−0.0026, +0.0186] | 0.856 | +0.021 [0.012, 0.031] | 41.9 % | +4.4 [1.1, 7.3] | 58.3 % | +5.9 [2.4, 9.1] |
| W4 | both instead of `vs`, `vs_max3` | 0.0760 | +0.0086 [−0.0027, +0.0192] | 0.854 | +0.018 [0.010, 0.029] | 42.3 % | +4.8 [0.9, 8.2] | 58.3 % | +5.9 [1.5, 8.5] |
| W5 | + `gust_max`, `gust_ratio` | 0.0696 | +0.0021 [−0.0069, +0.0105] | 0.853 | +0.018 [0.010, 0.026] | 43.4 % | +5.9 [2.0, 8.5] | 57.7 % | +5.2 [0.9, 7.9] |
| W6 | + `gust_max`, `gust_x_vpd` | 0.0705 | +0.0030 [−0.0041, +0.0104] | 0.856 | +0.021 [0.014, 0.030] | 42.3 % | +4.8 [0.9, 7.6] | 58.1 % | +5.7 [1.8, 8.6] |
| W7 (added after the odds ratios were seen) | + `gust_max`, `wind10_max`, `gust_ratio`, `wind10_ratio` | 0.0792 | +0.0117 [−0.0002, +0.0238] | 0.856 | +0.021 [0.012, 0.031] | 44.4 % | +6.9 [3.7, 10.8] | 58.9 % | +6.5 [1.9, 9.8] |

Other figures (`variants_prereg.csv`, `variants_W7.csv`, `boot_prereg.csv`, `boot_W7.csv`):

- Large fires in the top 1 %: 17.2 % in v2, 17.2–18.7 % in the variants; no interval excludes zero.
- Test years 2022–2024 only: ROC-AUC 0.858 in v2, 0.875–0.883 in the variants (+0.018 to +0.026 in all seven);
  PR-AUC 0.112 in v2, 0.103–0.122 in the variants (W1 −0.005, W2 −0.001, W3 +0.008, W4 +0.010, W5 −0.010,
  W6 −0.004, W7 +0.001). 114 large fires: no intervals computed.
- Unseen 50 km blocks, PR-AUC before fuel age: v2 0.0616; W1 0.0631, W2 0.0680, W3 0.0677, W4 0.0669, W5 0.0652,
  W6 0.0633, W7 0.0729.
- Brier: v2 0.006158; variants 0.006115–0.006150.
- Seed spread of PR-AUC: 0.0006–0.0018, as in v2 (0.0009).
- In W2 the other ablations keep their verdicts: terrain and fuel +0.047 [0.028, 0.066], dryness −0.0015
  [−0.006, 0.003], `lfmc_hat` +0.0005 [−0.0015, 0.0032], fuel age β = 4.02.

## Reading

- The gain sits in the bulk of the ranking: ignitions that v2 placed in the upper half move up or down with the
  day's gust, so more large fires fall in the top 5–10 % and ROC-AUC rises. The very top (top 1 %, which PR-AUC
  weighs most) stays as it was; there terrain, fuels and fuel age decide. With 477 large fires a PR-AUC change
  below about 0.01 cannot be resolved.
- Replacing gridMET wind (W4) is as good as adding to it: the daily mean wind adds nothing once HRRR is in.
- 3-day maxima, the explicit product with VPD and the ratio to the cell's mean (W3, W5, W6) do not differ from W2
  within the intervals. W7 is the best in every column but was chosen after seeing the odds ratios, so its
  advantage over W2 is not a tested result.
- The two-hour sampling ranks places within a day only moderately (0.75). Denser sampling may add skill; untested.

## For implementation (v3)

```python
# ml.data
SERIES.update({"gust_max": ("hrrr-gusts", "gust_max", "max", 1), "wind10_max": ("hrrr-gusts", "wind10_max", "max", 1)})
WIND = ["gust_max", "wind10_max"]            # add to SERIES_ALL and to FEATURES[...]["fire"], not to the LFMC lists
MONOTONE.update({"gust_max": 1, "wind10_max": 1})
```

- "max" with a window of 1 is the day's value; `ml.series` and `ml.grid.arrays` need no change.
- Checked for the map day 2026-10-20: `ml.series.fields(...)[name].on_grid(day, grid)` returns both fields on `smm`
  (837 × 1398, 84 % of cells finite: the rest is sea) and on `ca` (4827 × 4956, 43.5 % finite: California land), in
  19 and 22 s for four fields.
- The table `points_v2_wind.parquet` already has the columns for every row; a retraining from
  `python -m ml.series` with the two entries added should reproduce W2
  (`~/spaceapps/ml-work/models/x/wind_W2`).
- Reproducing the data needs the connector patch (`hrrr_gusts.patch`) and parallel lake roots, or about 6 hours.
- W7's ratios need a new operation in `ml.series.derive` (value divided by the cell's mean over the record) and,
  for a map, the whole record loaded for the denominator (about 40 s per field).

## Limits

- Model analyses and 1-hour forecasts, not observations; HRRR has known wind biases in complex terrain.
- Two samples a day: a lower bound of the daily maximum (77 % on average).
- 11 days of 2016–2017 are absent and 2016 has 2–4 % of ignitions without a value.
- Not tested: the `core` set (no terrain and fuels), wind direction, more sampled hours, the effect on LFMC.
- The 2026 map uses the retrospective analysis of the map day, as for gridMET; no forecast.
