# System B (P(fire >= 400 ha | ignition)): search for a configuration that beats the baseline

Research run of 2026-11-15, not part of the package. Scripts and outputs: `~/spaceapps/ml-work/research/fireml/` on the
project server (`prep.py`, `prep2.py`, `cv.py`, `ev.py`, `contrast.py`, `post.py`, `calib.py`, `fire_proto.py`).
Table: `point_features_v1b_noS2_canopy.parquet`, 74,568 FPA-FOD ignitions 2016-2024, 477 large fires (0.64 %).
`ml` code as of commit `0dded3e`.

## Summary

- **Recommended configuration** (chosen by the rule below): LightGBM with small trees and monotone constraints, a
  5-seed ensemble, on weather of the day + LANDFIRE/terrain statics + wind direction and relative wind + antecedent
  weather + SMAP root-zone dryness anomaly (42 features). No single-day lags, no raw soil moisture, no season, no
  `lfmc_hat`. PR-AUC in an unseen year **0.0545** against **0.0424** for the baseline ensemble:
  **+0.0120, 95 % interval [+0.0026, +0.0266]**. A second ensemble with other seeds gives 0.0546 and
  +0.0124 [+0.0023, +0.0274].
- **The gain is modest and only partly confirmed.** The configuration was chosen on test years 2016-2021
  (+0.0096 [+0.0029, +0.0187]). On test years 2022-2024, which played no part in the choice, the gain is
  +0.0233 [-0.0098, +0.0708]: same sign, interval includes zero. In the strict split (train <= 2021, test 2022-2024)
  it is +0.0355 [-0.0100, +0.0888]; on unseen 50 km blocks +0.001 to +0.004, intervals include zero.
- **Against the single model that `ml.train` fits (seed 0) the gain is not significant**: +0.0078 [-0.0046, +0.0238].
  Seed 0 is the best of five seeds for the baseline (0.0466; seeds 0-4: mean 0.0404, sd 0.0037,
  range 0.0376-0.0466).
- **Seed noise is as large as the effects under test.** One refit with another seed moves the baseline PR-AUC by
  up to 0.009. Every comparison below therefore uses 5-seed ensembles on both sides (ensemble-to-ensemble noise:
  0.0424 against 0.0422).
- **`lfmc_hat` adds nothing once seeds are averaged**: -0.0007 [-0.0036, +0.0020]. The +0.008 seen with single
  seeds is seed noise.
- **Static layers are the one large, certain step**: `full` against `core`, +0.0222 [+0.0142, +0.0318].
- **The dryness anomaly, antecedent weather and wind features each add +0.000 to +0.006 and none is resolved on
  its own.** What is resolved is the package: fewer, aggregated features and smaller trees.

## Protocol

As in `docs/METHODS.md` and `ml.train.fire_report`:

- **Headline:** PR-AUC over all ignitions, each year predicted by models that saw neither its ignitions nor (for
  `lfmc_hat`) its LFMC samples. Leave-one-year-out, 2016-2024.
- **Intervals:** bootstrap over calendar weeks, 500 resamples, paired for differences. The tables and the paired
  contrasts come from two bootstrap runs, so the same interval can differ in the third decimal.
- **Secondary:** unseen 50 km blocks (5 folds, 3 fold assignments, `lfmc_hat` refitted per fold); strict temporal
  split (train <= 2021, test 2022-2024, 114 large fires, LFMC models never see 2022+ samples).
- **Baseline:** `with_lfmc` for `core` and `full` with the `CLF` settings of `train.py`, recomputed with the same
  code (features, settings, seed 0).

Two deviations, both made before any candidate ensemble was scored:

1. **Seed ensembles.** After the baseline's seed noise turned out to be 0.004-0.009, the reference became the
   5-seed ensemble of the `full` baseline (mean logit over seeds 0-4), and only 5-seed ensembles were eligible.
2. **Selection rule.** Among the 52 eligible configurations, take the one with the highest lower 95 % bound of
   the paired PR-AUC gain on test years 2016-2021; report its gain on test years 2022-2024 as the confirmation.
   Ablations of the chosen configuration that were run afterwards were not eligible.

## Baselines and seed noise (unseen year)

| Model | PR-AUC [95 %] | ROC-AUC | top 10 % of ignitions hold |
|---|---|---|---|
| base rate | 0.0064 | 0.50 | 10 % |
| gridMET `bi` | 0.0142 | | |
| `core` with `lfmc_hat`, seed 0 | 0.0203 [0.0146, 0.0278] | 0.748 | 35 % |
| `core` with `lfmc_hat`, 5-seed ensemble | 0.0202 | 0.751 | 36 % |
| `full` with `lfmc_hat`, seed 0 (what `ml.train` reports) | 0.0466 [0.0328, 0.0640] | 0.820 | 51 % |
| `full` with `lfmc_hat`, seeds 1 / 2 / 3 / 4 | 0.0386 / 0.0382 / 0.0411 / 0.0376 | 0.814-0.820 | 48-51 % |
| `full` with `lfmc_hat`, 5-seed ensemble (**reference**) | 0.0424 [0.0309, 0.0569] | 0.823 | 51 % |
| `full` without `lfmc_hat`, 5-seed ensemble | 0.0431 | 0.825 | 51 % |

## What moves the score (paired differences, 5-seed ensembles, unseen year)

`a - b | all years | test years 2016-2021 | test years 2022-2024`, each with its 95 % interval.

```
B_full_bag5 - B_full_nolfmc_bag5 | all: -0.0007 [-0.0036, +0.0020] | 2016-2021: +0.0003 [-0.0013, +0.0029] | 2022-2024: -0.0044 [-0.0184, +0.0058]
B_full_bag5 - B_core_bag5 | all: +0.0222 [+0.0142, +0.0318] | 2016-2021: +0.0195 [+0.0139, +0.0277] | 2022-2024: +0.0334 [+0.0070, +0.0669]
R_ds_small_mono_bag5 - B_full_bag5 | all: +0.0093 [-0.0008, +0.0234] | 2016-2021: +0.0022 [-0.0053, +0.0118] | 2022-2024: +0.0404 [-0.0000, +0.0832]
R_ds_small_mono_bag5 - G_ds_base_bag5 | all: +0.0093 [+0.0034, +0.0161] | 2016-2021: +0.0070 [+0.0023, +0.0136] | 2022-2024: +0.0160 [-0.0028, +0.0310]
G_ds_ante_smallmono_bag5 - R_ds_small_mono_bag5 | all: +0.0018 [-0.0038, +0.0066] | 2016-2021: +0.0016 [-0.0041, +0.0052] | 2022-2024: +0.0024 [-0.0195, +0.0167]
G_ds_ante_zl_smallmono_bag5 - G_ds_ante_smallmono_bag5 | all: +0.0020 [-0.0019, +0.0072] | 2016-2021: +0.0012 [-0.0028, +0.0071] | 2022-2024: +0.0035 [-0.0055, +0.0161]
G_ds_zl_smallmono_bag5 - R_ds_small_mono_bag5 | all: +0.0038 [-0.0015, +0.0097] | 2016-2021: +0.0033 [-0.0013, +0.0073] | 2022-2024: +0.0079 [-0.0124, +0.0275]
G_ds_wind_ante_zl_smallmono_bag5 - G_ds_ante_zl_smallmono_bag5 | all: +0.0060 [+0.0001, +0.0124] | 2016-2021: +0.0052 [+0.0003, +0.0114] | 2022-2024: +0.0002 [-0.0181, +0.0115]
G_ds_wind_ante_zl_regmono_bag5 - G_ds_ante_zl_regmono_bag5 | all: +0.0007 [-0.0044, +0.0046] | 2016-2021: +0.0039 [+0.0001, +0.0091] | 2022-2024: -0.0149 [-0.0397, +0.0002]
G_ds_ante_zl_regmono_bag5 - G_ds_ante_regmono_bag5 | all: +0.0005 [-0.0030, +0.0043] | 2016-2021: +0.0011 [-0.0019, +0.0051] | 2022-2024: -0.0052 [-0.0179, +0.0108]
G_ds_lh_ante_zl_regmono_bag5 - G_ds_ante_zl_regmono_bag5 | all: -0.0004 [-0.0019, +0.0005] | 2016-2021: -0.0004 [-0.0012, +0.0004] | 2022-2024: +0.0036 [-0.0083, +0.0085]
POST_M_S1_bag5__rz_z12 - M_S1_bag5 | all: +0.0021 [-0.0004, +0.0059] | 2016-2021: +0.0018 [-0.0006, +0.0052] | 2022-2024: +0.0057 [-0.0007, +0.0186]
G_ds_wind_ante_zl_regmono_bag5 - B_full | all: +0.0078 [-0.0046, +0.0238] | 2016-2021: +0.0053 [-0.0055, +0.0163] | 2022-2024: +0.0157 [-0.0270, +0.0692]
G_ds_wind_ante_zl_smallmono_bag5 - B_full | all: +0.0149 [-0.0016, +0.0353] | 2016-2021: +0.0059 [-0.0061, +0.0191] | 2022-2024: +0.0389 [-0.0150, +0.0906]
G_ds_wind_ante_zl_regmono_bag5 - B_core_bag5 | all: +0.0343 [+0.0195, +0.0562] | 2016-2021: +0.0292 [+0.0195, +0.0405] | 2022-2024: +0.0567 [+0.0070, +0.1291]
G_ds_wind_ante_zl_regmono_bag5b - B_full_bag5b | all: +0.0124 [+0.0023, +0.0274] | 2016-2021: +0.0093 [+0.0017, +0.0179] | 2022-2024: +0.0255 [-0.0108, +0.0735]
G_ds_wind_ante_zl_smallmono_bag5b - B_full_bag5b | all: +0.0192 [+0.0059, +0.0378] | 2016-2021: +0.0105 [+0.0028, +0.0211] | 2022-2024: +0.0472 [-0.0021, +0.0898]
G_ds_wind_ante_zl_regmono_bag5 - G_ds_wind_ante_zl_regnomono_bag5 | all: -0.0065 [-0.0158, +0.0024] | 2016-2021: +0.0013 [-0.0021, +0.0065] | 2022-2024: -0.0312 [-0.0504, -0.0003]
G_ds_wind_ante_zl_regmono_bag5 - G_ds_wind_ante_regmono_bag5 | all: +0.0004 [-0.0022, +0.0034] | 2016-2021: +0.0014 [-0.0011, +0.0069] | 2022-2024: -0.0011 [-0.0087, +0.0053]
G_ds_wind_ante_zl_regmono_bag5 - G_ds_wind_zl_regmono_bag5 | all: +0.0036 [+0.0003, +0.0077] | 2016-2021: +0.0032 [+0.0004, +0.0068] | 2022-2024: +0.0057 [-0.0020, +0.0172]
G_ds_wind_ante_zl_regmono_logitblend_bag5 - B_full_bag5 | all: +0.0180 [+0.0018, +0.0441] | 2016-2021: +0.0096 [+0.0009, +0.0204] | 2022-2024: +0.0481 [-0.0102, +0.1153]
G_ds_wind_ante_zl_smallmono_bag5 - G_ds_wind_ante_zl_regmono_bag5 | all: +0.0071 [-0.0015, +0.0149] | 2016-2021: +0.0006 [-0.0068, +0.0094] | 2022-2024: +0.0232 [+0.0009, +0.0341]
G_ds_wind_ante_zl_regmono_bag5 - G_ds_regmono_bag5 | all: +0.0047 [-0.0009, +0.0118] | 2016-2021: +0.0089 [+0.0028, +0.0178] | 2022-2024: -0.0078 [-0.0227, +0.0074]
G_ds_regmono_bag5 - B_full_bag5 | all: +0.0074 [-0.0021, +0.0203] | 2016-2021: +0.0008 [-0.0057, +0.0059] | 2022-2024: +0.0311 [-0.0072, +0.0757]
M_S1_reg_bag5 - G_ds_base_bag5 | all: +0.0035 [-0.0016, +0.0076] | 2016-2021: +0.0036 [-0.0009, +0.0070] | 2022-2024: +0.0011 [-0.0159, +0.0219]
G_ds_regmono_bag5 - M_S1_reg_bag5 | all: +0.0038 [+0.0003, +0.0075] | 2016-2021: +0.0019 [-0.0002, +0.0042] | 2022-2024: +0.0056 [-0.0084, +0.0151]
```

Reading:

- `lfmc_hat`: no effect. Statics: +0.022, certain.
- Day weather + statics with small monotone trees against the full baseline: +0.009, interval touches zero.
- Small monotone trees against the default trees on the same 27 features: +0.0093 [+0.0034, +0.0161].
- Antecedent sums and means: +0.002 [-0.0038, +0.0066] with one tree setting, +0.0036 [+0.0003, +0.0077] with the other.
- Dryness anomaly with lags: +0.000 to +0.004 in four comparisons, every interval includes zero.
- Wind direction and relative wind: +0.006 [+0.0001, +0.0124] with one tree setting, +0.001 with the other.
- Dryness anomaly as a multiplier after the model (`logit(p) + beta * z`, beta fitted on training years only,
  -0.63 to -0.92 per fold): +0.0021 [-0.0004, +0.0059].
- Monotone constraints on the recommended features: -0.0065 [-0.0158, +0.0024] against the same model without
  them; on day weather + statics +0.0038 [+0.0003, +0.0075]. Not resolved for accuracy. They are kept because they
  fix the sign of the response outside the training range (a map cell whose dryness is shifted by NISAR).
- The two finalists differ by +0.0071 [-0.0015, +0.0149]: a tie.

## Recommended configuration

`G_ds_wind_ante_zl_regmono_bag5`. Stand-alone implementation: `fire_proto.py` (reproduces the harness: PR-AUC 0.0549,
identical feature values).

**Features (42), in table/`ml.data` names:**

| Group | Columns | Source |
|---|---|---|
| Weather of the day | `vs rmin erc bi fm100 fm1000 vpd pr tmmx pet` | table (gridMET) |
| Statics | `total_load dead_1h_load dead_10h_load dead_100h_load live_herb_load live_woody_load fuel_bed_depth moisture_of_extinction burnable canopy_cover canopy_height canopy_base_height canopy_bulk_density elevation slope northness eastness` | table (LANDFIRE); `northness`, `eastness` from `ml.data.add_derived` |
| Wind direction | `th_sin th_cos` = sin, cos of gridMET `th` (degrees, wind from) | table column `th`; map: gridMET `th` |
| Relative wind and dryness | `vs_ratio vpd_ratio` = value of the day / the cell's mean over 2015-10-01..2024-12-31 | daily gridMET store, 4 km |
| Antecedent weather | `pr_sum7 pr_sum30 pr_sum90` (mm), `days_since_rain`, `vpd_mean7 vpd_mean30`, `vs_max3` | daily gridMET store |
| Dryness anomaly | `rz_z12 rz_z12_lag30d rz_z12_lag60d rz_z12_lag90d` | daily SMAP L4 store, 9 km |

**Derived features** (one definition for table rows and map cells: a field on the dataset's native grid for a day;
rows take the nearest native cell, a map warps the field to its grid):

- `rz_z12(d)` = (`sm_rootzone`(d) - mean of days d-365..d-1) / standard deviation of those days; needs >= 330 of
  the 365 days; `_lagNd` is the same quantity on day d-N. Missing for January-September 2016 (8 % of rows).
- `pr_sumN`, `vpd_meanN` = sum / mean over the N days ending on d (>= 80 % of the days present).
- `vs_max3` = maximum of `vs` on d, d-1, d-2.
- `days_since_rain` = days since the 3-day precipitation sum was >= 8.5 mm, capped at 365.
- `vs_ratio`, `vpd_ratio` = value on d divided by the cell's mean over 2015-10-01..2024-12-31 (a static layer).

**Model:** `lightgbm.LGBMClassifier(n_estimators=300, learning_rate=0.03, num_leaves=7, min_child_samples=200,
subsample=0.8, subsample_freq=1, colsample_bytree=0.5, reg_lambda=10.0, monotone_constraints=...,
monotone_constraints_method="intermediate")`, seeds 0-4, no class weights. Score = mean over seeds of the logit.

**Monotone constraints:** +1 for `vs vpd tmmx erc bi pet slope vs_max3 vpd_mean7 vpd_mean30 days_since_rain vs_ratio
vpd_ratio`; -1 for `rmin fm100 fm1000 pr_sum7 pr_sum30 pr_sum90 rz_z12 rz_z12_lag30d rz_z12_lag60d rz_z12_lag90d`; 0 for
`pr`, the fuel and canopy layers, `elevation`, `northness`, `eastness`, `th_sin`, `th_cos`. The vector in feature
order is in `proto_out/meta.json`.

**Calibration:** as in `ml.train`: sigmoid on the unseen-year scores, `p = 1 / (1 + exp(-(a * score + b)))` with
a = 0.942, b = -0.154 on the harness scores (the prototype's own fit is in `proto_out/meta.json`); `rank_quantiles`
from the calibrated unseen-year predictions.

**For `ml.data` / `ml.train`:** the working tree already defines `rz_z12`, `sf_z12`, the `rz_z12` lags, `pr_sum30`,
`pr_sum90`, `vpd_mean30`, `vs_max3` and an empty `MONOTONE`. Missing for the recommended set: `pr_sum7`, `vpd_mean7`,
`days_since_rain`, `vs_ratio`, `vpd_ratio`, `th_sin`, `th_cos`. With only the series already defined
(day weather + statics + those series, same tree settings):

| Config | Features | Settings | PR-AUC unseen year | ROC-AUC | PR-AUC train <= 2021, test 2022-2024 |
|---|---|---|---|---|---|
| `P_repo_series_regmono_bag5` | DAY+STATIC+ZL+sf_z12+pr_sum30+pr_sum90+vpd_mean30+vs_max3 | reg mono x5 | 0.0516 | 0.833 | 0.0849 |
| `P_repo_series_smallmono_bag5` | DAY+STATIC+ZL+sf_z12+pr_sum30+pr_sum90+vpd_mean30+vs_max3 | small mono x5 | 0.0561 | 0.833 | 0.1012 |
| `P_repo_series_nosf_regmono_bag5` | DAY+STATIC+ZL+pr_sum30+pr_sum90+vpd_mean30+vs_max3 | reg mono x5 | 0.0518 | 0.835 | 0.0883 |

```
P_repo_series_regmono_bag5 - B_full_bag5 | all: +0.0092 [+0.0004, +0.0224] | 2016-2021: +0.0048 [-0.0014, +0.0116] | 2022-2024: +0.0323 [-0.0066, +0.0854]
P_repo_series_smallmono_bag5 - B_full_bag5 | all: +0.0137 [+0.0017, +0.0306] | 2016-2021: +0.0066 [-0.0018, +0.0168] | 2022-2024: +0.0471 [+0.0003, +0.0928]
P_repo_series_nosf_regmono_bag5 - B_full_bag5 | all: +0.0094 [+0.0007, +0.0226] | 2016-2021: +0.0053 [-0.0009, +0.0124] | 2022-2024: +0.0320 [-0.0060, +0.0858]
```

Whichever set is used: do not add the single-day lags, raw SMAP, season or `lfmc_hat` to the fire model
(`full` features + antecedent + anomaly scores 0.047-0.048, against 0.054-0.062 without them), and set `n_jobs`
explicitly (see Limitations).

## Secondary validation

PR-AUC. Blocks: three fold assignments and their mean.

| Config | Unseen year (headline) | Unseen 50 km blocks, 3 fold assignments (mean) | Train <= 2021, test 2022-2024 [95 %] | gain vs baseline ensemble in that split |
|---|---|---|---|---|
| `B_core` | 0.0203 | 0.0311 / 0.0281 / 0.0342 (0.0311) | 0.0166 [+0.0098, +0.0257] | -0.0281 [-0.0522, -0.0111] |
| `B_full` | 0.0466 | 0.0549 / 0.0453 / 0.0466 (0.0489) | 0.0471 [+0.0227, +0.0810] | +0.0023 [-0.0042, +0.0137] |
| `B_full_bag5` | 0.0424 | 0.0605 / 0.0516 / 0.0541 (0.0554) | 0.0447 [+0.0221, +0.0752] | +0.0000 [+0.0000, +0.0000] |
| `B_full_nolfmc_bag5` | 0.0431 | 0.0611 / 0.0520 / 0.0589 (0.0573) | 0.0500 [+0.0240, +0.0817] | +0.0053 [-0.0004, +0.0181] |
| `G_ds_regmono_bag5` | 0.0498 | 0.0567 / 0.0471 / 0.0457 (0.0498) | 0.0837 [+0.0272, +0.1769] | +0.0389 [-0.0023, +0.1088] |
| `G_ds_ante_zl_regmono_bag5` | 0.0537 | 0.0589 / 0.0523 / 0.0537 (0.0550) | 0.0881 [+0.0244, +0.1681] | +0.0434 [-0.0042, +0.1001] |
| `G_ds_wind_ante_zl_regmono_bag5` | 0.0545 | 0.0613 / 0.0557 / 0.0584 (0.0585) | 0.0802 [+0.0215, +0.1587] | +0.0355 [-0.0100, +0.0888] |
| `G_ds_wind_ante_zl_smallmono_bag5` | 0.0616 | 0.0631 / 0.0592 / 0.0595 (0.0606) | 0.1036 [+0.0261, +0.2022] | +0.0588 [-0.0006, +0.1300] |
| `G_ds_wind_ante_zl_reg_bag5` | 0.0609 | 0.0701 / 0.0596 / 0.0618 (0.0638) | 0.1030 [+0.0220, +0.1800] | +0.0583 [-0.0060, +0.1134] |
| `G_ds_all_regmono_bag5` | 0.0525 | 0.0577 / 0.0556 / 0.0551 (0.0561) | 0.0643 [+0.0210, +0.1232] | +0.0196 [-0.0096, +0.0535] |


On unseen blocks the baseline is as good as the candidates. `docs/METHODS.md` explains why block folds favour
models that can recognise a fire episode from other blocks, which the lagged and seasonal inputs allow. In the
strict temporal split every configuration without those inputs scores 0.07-0.13 against 0.045 for the baseline,
with wide intervals (114 large fires).

## Calibration and capture (unseen-year predictions, sigmoid calibrated)

| Config | Brier | Brier of the base rate | top 1 % | top 5 % | top 10 % | top 20 % |
|---|---|---|---|---|---|---|
| `B_full` | 0.006240 | 0.006356 | 0.101 | 0.371 | 0.512 | 0.706 |
| `B_full_bag5` | 0.006244 | 0.006356 | 0.109 | 0.365 | 0.509 | 0.700 |
| `G_ds_wind_ante_zl_regmono_bag5` | 0.006191 | 0.006356 | 0.153 | 0.411 | 0.560 | 0.723 |
| `G_ds_wind_ante_zl_smallmono_bag5` | 0.006184 | 0.006356 | 0.149 | 0.400 | 0.560 | 0.721 |

Share of all large fires among the highest-ranked ignitions. Reliability by decile of predicted probability:

| Decile | `B_full_bag5` predicted / observed | `G_ds_wind_ante_zl_regmono_bag5` predicted / observed |
|---|---|---|
| 1 | 0.0006 / 0.0011 | 0.0007 / 0.0005 |
| 2 | 0.0010 / 0.0011 | 0.0010 / 0.0015 |
| 3 | 0.0014 / 0.0012 | 0.0013 / 0.0008 |
| 4 | 0.0019 / 0.0011 | 0.0016 / 0.0008 |
| 5 | 0.0025 / 0.0022 | 0.0021 / 0.0020 |
| 6 | 0.0033 / 0.0026 | 0.0026 / 0.0019 |
| 7 | 0.0045 / 0.0029 | 0.0036 / 0.0039 |
| 8 | 0.0064 / 0.0071 | 0.0053 / 0.0063 |
| 9 | 0.0103 / 0.0122 | 0.0096 / 0.0105 |
| 10 | 0.0316 / 0.0326 | 0.0362 / 0.0358 |

## Null and negative results

- **`lfmc_hat`:** no gain in the baseline (-0.0007 [-0.0036, +0.0020]) nor in the simple family
  (-0.0004 [-0.0019, +0.0005]).
- **Single-day lags of weather, season, raw SMAP:** on day weather + statics with the baseline trees (single seed):
  0.0410; + weather lags 0.0414; + season 0.0423; + same-day SMAP 0.0427; + SMAP with lags 0.0465. None resolved.
- **Dryness anomaly:** the sign is positive in every comparison and the interval includes zero in every comparison
  (see above). Surface anomaly `sf_z12` and the anomaly in m3/m3 instead of z-score: no gain.
- **Auxiliary labels** (day weather + statics, single seed, against 0.0457 for the same settings on the 400 ha
  label): trained on >= 40 ha 0.0415; on >= 4 ha 0.0305; regression on log size 0.0365; chained
  P(>=4)·P(>=40|>=4)·P(>=400|>=40) 0.0477; mean of the three thresholds 0.0439. No gain.
- **Weights on large fires** (x6, x21): 0.0477, 0.0456. No gain.
- **Logistic regression:** PR-AUC 0.0475-0.0521 on the simple sets, ROC-AUC 0.805-0.816 (below the trees). Averaged
  with the trees on the recommended features: 0.0604, +0.0180 [+0.0018, +0.0441], 0.1003 in the temporal split. A
  reasonable alternative; not the one the rule picks.
- **Dropping `bi` and `erc`:** 0.0371 against 0.0410. Keep them.

## Limitations

- **477 large fires.** Differences below about 0.005 are not resolvable; the intervals are wide and skewed.
- **Selection.** 52 ensembles were compared and one chosen. Its all-years interval is optimistic; the
  confirmation years are the honest check and there the interval includes zero. 6 of the 52 have an interval above
  zero on the selection years, 27 on all years, 49 have a positive gain.
- **The rule changed once**, from single models to ensembles, after seeing the baseline's seed noise and before
  scoring candidate ensembles.
- **Presets, not tuning.** Eight tree settings were tried; no nested search.
- **`vs_ratio`, `vpd_ratio`** divide by a mean that includes the test years (a climatological constant, no labels).
- **Series features use the nearest native cell**, the table's own columns are bilinear.
- **Labels.** Final size reflects suppression; the score is conditional on an ignition.
- **Threads.** `lightgbm`'s scikit-learn wrapper with `n_jobs=None` uses every physical core whatever
  `OMP_NUM_THREADS` says. `ml.train.fit_lfmc` / `fit_fire` do that; several such fits in parallel stalled the
  server (load above 1,000). One thread per fit and many processes was 10-50 times faster here.

## All seed ensembles (unseen year; gain against the baseline ensemble)

Feature groups: DAY = weather of the day; DAYX = DAY without `bi`, `erc`; WLAG = single-day lags of weather;
SOIL = SMAP with lags; SOIL0 = SMAP of the day; SEAS = day of year; STATIC = LANDFIRE + terrain; TERR, FUEL = its
parts; ANTE = `pr_sum7 pr_sum30 pr_sum90 days_since_rain vpd_mean7 vpd_mean30 vs_max3`; Z = `rz_z12`; ZL = Z + its
three lags; WIND = `th_sin th_cos vs_ratio vpd_ratio`; LHC / LHF = `lfmc_hat` from the core / full LFMC model.
Settings: base = `CLF` of `train.py` (15 leaves, 50 per leaf); small = 7 leaves, 200 per leaf; reg = small +
`colsample_bytree` 0.5 + `reg_lambda` 10; tiny = 4 leaves, 400 per leaf, 400 trees; small5 = 5 leaves, 300 per leaf;
smallc = small + `colsample_bytree` 0.5; smalllong, long = 800-900 trees at rate 0.01; xt = extra-trees; mono = monotone
constraints. `bag5b` = seeds 5-9.

| Config | Features | Model | Settings | PR-AUC | ROC-AUC | Gain, all years [95 %] | Gain, test years 2016-2021 | Gain, test years 2022-2024 | Top 10 % |
|---|---|---|---|---|---|---|---|---|---|
| `G_ds_wind_ante_zl_smallmono_bag5` | DAY+STATIC+WIND+ANTE+ZL | trees | small mono x5 seeds | 0.0616 | 0.841 | +0.0192 [+0.0043, +0.0391] | +0.0103 [+0.0018, +0.0200] | +0.0465 [+0.0002, +0.0939] | 0.56 |
| `G_ds_wind_ante_zl_smallmono_bag5b` | DAY+STATIC+WIND+ANTE+ZL | trees | small mono x5 seeds | 0.0614 | 0.840 | +0.0190 [+0.0043, +0.0378] | +0.0100 [+0.0016, +0.0199] | +0.0483 [+0.0021, +0.0928] | 0.55 |
| `G_ds_wind_ante_zl_reg_bag5` | DAY+STATIC+WIND+ANTE+ZL | trees | reg x5 seeds | 0.0609 | 0.836 | +0.0185 [+0.0029, +0.0410] | +0.0083 [+0.0010, +0.0149] | +0.0545 [-0.0025, +0.1068] | 0.55 |
| `G_ds_wind_ante_zl_regnomono_bag5` | DAY+STATIC+WIND+ANTE+ZL | trees | reg x5 seeds | 0.0609 | 0.836 | +0.0185 [+0.0029, +0.0410] | +0.0083 [+0.0010, +0.0149] | +0.0545 [-0.0025, +0.1068] | 0.55 |
| `G_ds_wind_ante_zl_regmono_logitblend_bag5` | DAY+STATIC+WIND+ANTE+ZL | trees+logistic | reg mono x5 seeds | 0.0604 | 0.835 | +0.0180 [+0.0020, +0.0445] | +0.0096 [+0.0004, +0.0207] | +0.0481 [-0.0091, +0.1195] | 0.56 |
| `G_ds_wind_ante_zl_base_bag5` | DAY+STATIC+WIND+ANTE+ZL | trees | base x5 seeds | 0.0592 | 0.832 | +0.0168 [+0.0029, +0.0389] | +0.0072 [-0.0001, +0.0148] | +0.0602 [+0.0019, +0.1210] | 0.55 |
| `R_ds_all_small_mono_bag5` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees | small mono x5 seeds | 0.0588 | 0.841 | +0.0164 [+0.0050, +0.0304] | +0.0114 [+0.0028, +0.0221] | +0.0333 [-0.0028, +0.0674] | 0.56 |
| `G_ds_lh_ante_zl_base_bag5` | DAY+STATIC+LHF+ANTE+ZL | trees | base x5 seeds | 0.0585 | 0.828 | +0.0161 [+0.0020, +0.0387] | +0.0050 [-0.0035, +0.0130] | +0.0622 [+0.0046, +0.1130] | 0.55 |
| `G_ds_ante_zl_regmono_blend_bag5` | DAY+STATIC+ANTE+ZL | trees+logistic | reg mono x5 seeds | 0.0585 | 0.832 | +0.0160 [+0.0014, +0.0409] | +0.0057 [-0.0022, +0.0150] | +0.0632 [-0.0020, +0.1308] | 0.55 |
| `G_ds_ante_z_regmono_blend_bag5` | DAY+STATIC+ANTE+Z | trees+logistic | reg mono x5 seeds | 0.0578 | 0.832 | +0.0154 [+0.0005, +0.0396] | +0.0048 [-0.0028, +0.0129] | +0.0625 [-0.0028, +0.1297] | 0.55 |
| `G_ds_ante_zl_reg_bag5` | DAY+STATIC+ANTE+ZL | trees | reg x5 seeds | 0.0572 | 0.831 | +0.0148 [-0.0005, +0.0423] | +0.0033 [-0.0036, +0.0085] | +0.0671 [+0.0011, +0.1263] | 0.53 |
| `G_ds_ante_zl_base_bag5` | DAY+STATIC+ANTE+ZL | trees | base x5 seeds | 0.0572 | 0.829 | +0.0148 [-0.0001, +0.0380] | +0.0018 [-0.0064, +0.0079] | +0.0649 [+0.0096, +0.1091] | 0.54 |
| `G_ds_lh_ante_zl_smallmono_bag5` | DAY+STATIC+LHF+ANTE+ZL | trees | small mono x5 seeds | 0.0561 | 0.834 | +0.0136 [+0.0014, +0.0339] | +0.0056 [-0.0031, +0.0148] | +0.0512 [+0.0017, +0.1036] | 0.55 |
| `G_ds_ante_zl_smallmono_bag5` | DAY+STATIC+ANTE+ZL | trees | small mono x5 seeds | 0.0556 | 0.834 | +0.0131 [+0.0009, +0.0319] | +0.0050 [-0.0035, +0.0143] | +0.0463 [+0.0022, +0.0922] | 0.56 |
| `G_ds_zl_smallmono_bag5` | DAY+STATIC+ZL | trees | small mono x5 seeds | 0.0555 | 0.828 | +0.0131 [+0.0004, +0.0317] | +0.0055 [-0.0041, +0.0161] | +0.0484 [+0.0008, +0.1000] | 0.53 |
| `M_S2_reg_bag5` | DAY+STATIC+ANTE+Z | trees | reg x5 seeds | 0.0555 | 0.834 | +0.0131 [-0.0009, +0.0408] | +0.0037 [-0.0021, +0.0092] | +0.0582 [-0.0047, +0.1272] | 0.56 |
| `G_ds_ratio_ante_zl_regmono_bag5` | DAY+STATIC+vs_ratio+vpd_ratio+ANTE+ZL | trees | reg mono x5 seeds | 0.0548 | 0.840 | +0.0124 [+0.0017, +0.0286] | +0.0089 [+0.0005, +0.0178] | +0.0293 [-0.0074, +0.0845] | 0.57 |
| `G_ds_wind_ante_zl_lh_regmono_bag5` | DAY+STATIC+WIND+ANTE+ZL+LHF | trees | reg mono x5 seeds | 0.0547 | 0.840 | +0.0123 [+0.0022, +0.0275] | +0.0093 [+0.0027, +0.0183] | +0.0260 [-0.0098, +0.0776] | 0.56 |
| `R_ds_ante_z_small_mono_bag5` | DAY+STATIC+ANTE+Z | trees | small mono x5 seeds | 0.0547 | 0.835 | +0.0123 [+0.0010, +0.0307] | +0.0042 [-0.0045, +0.0127] | +0.0512 [+0.0035, +0.0977] | 0.55 |
| `G_ds_wind_ante_zl_regmono_bag5b` | DAY+STATIC+WIND+ANTE+ZL | trees | reg mono x5 seeds | 0.0546 | 0.840 | +0.0122 [+0.0019, +0.0281] | +0.0088 [+0.0017, +0.0172] | +0.0266 [-0.0093, +0.0806] | 0.56 |
| `G_ds_wind_ante_zl_regmono_bag5` | DAY+STATIC+WIND+ANTE+ZL | trees | reg mono x5 seeds | 0.0545 | 0.840 | +0.0120 [+0.0026, +0.0266] | +0.0096 [+0.0029, +0.0187] | +0.0233 [-0.0098, +0.0708] | 0.56 |
| `G_ds_thonly_ante_zl_regmono_bag5` | DAY+STATIC+th_sin+th_cos+ANTE+ZL | trees | reg mono x5 seeds | 0.0543 | 0.835 | +0.0119 [+0.0012, +0.0284] | +0.0064 [-0.0004, +0.0130] | +0.0335 [-0.0042, +0.0870] | 0.53 |
| `R_ds_z_small_mono_bag5` | DAY+STATIC+Z | trees | small mono x5 seeds | 0.0542 | 0.829 | +0.0118 [+0.0003, +0.0282] | +0.0045 [-0.0058, +0.0154] | +0.0456 [+0.0043, +0.0869] | 0.54 |
| `G_ds_ante_z_regmono_bag5` | DAY+STATIC+ANTE+Z | trees | reg mono x5 seeds | 0.0541 | 0.835 | +0.0117 [+0.0012, +0.0315] | +0.0050 [-0.0025, +0.0113] | +0.0478 [-0.0020, +0.1128] | 0.54 |
| `G_ds_wind_ante_regmono_bag5` | DAY+STATIC+WIND+ANTE | trees | reg mono x5 seeds | 0.0541 | 0.839 | +0.0117 [+0.0017, +0.0263] | +0.0082 [+0.0006, +0.0153] | +0.0244 [-0.0110, +0.0746] | 0.55 |
| `G_ds_ante_zl_regmono_bag5` | DAY+STATIC+ANTE+ZL | trees | reg mono x5 seeds | 0.0537 | 0.835 | +0.0113 [+0.0015, +0.0298] | +0.0057 [-0.0011, +0.0124] | +0.0382 [-0.0023, +0.1049] | 0.56 |
| `G_ds_ante_smallmono_bag5` | DAY+STATIC+ANTE | trees | small mono x5 seeds | 0.0535 | 0.834 | +0.0111 [+0.0007, +0.0266] | +0.0039 [-0.0040, +0.0107] | +0.0428 [-0.0002, +0.0873] | 0.55 |
| `G_ds_lh_ante_zl_regmono_bag5` | DAY+STATIC+LHF+ANTE+ZL | trees | reg mono x5 seeds | 0.0533 | 0.835 | +0.0109 [+0.0013, +0.0281] | +0.0053 [-0.0015, +0.0121] | +0.0418 [-0.0018, +0.0998] | 0.56 |
| `G_ds_ante_regmono_bag5` | DAY+STATIC+ANTE | trees | reg mono x5 seeds | 0.0532 | 0.834 | +0.0108 [+0.0006, +0.0292] | +0.0046 [-0.0026, +0.0100] | +0.0434 [-0.0039, +0.1137] | 0.55 |
| `R_ds_wind_small_mono_bag5` | DAY+STATIC+WIND | trees | small mono x5 seeds | 0.0532 | 0.835 | +0.0108 [+0.0004, +0.0249] | +0.0065 [-0.0028, +0.0165] | +0.0281 [-0.0058, +0.0723] | 0.53 |
| `G_ds_ante_z_regmono_multi_bag5` | DAY+STATIC+ANTE+Z | trees, mean of >=4/40/400 ha | reg mono x3 seeds | 0.0531 | 0.836 | +0.0107 [+0.0006, +0.0262] | +0.0067 [-0.0022, +0.0162] | +0.0315 [-0.0054, +0.0726] | 0.54 |
| `G_ds_all_base_bag5` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees | base x5 seeds | 0.0530 | 0.834 | +0.0106 [+0.0032, +0.0211] | +0.0087 [+0.0024, +0.0158] | +0.0240 [-0.0025, +0.0570] | 0.54 |
| `G_ds_ante_zl_regmono_multi_bag5` | DAY+STATIC+ANTE+ZL | trees, mean of >=4/40/400 ha | reg mono x3 seeds | 0.0529 | 0.836 | +0.0105 [+0.0006, +0.0255] | +0.0069 [-0.0014, +0.0164] | +0.0310 [-0.0066, +0.0741] | 0.55 |
| `G_ds_soil_ante_smallmono_bag5` | DAY+STATIC+SOIL+ANTE | trees | small mono x5 seeds | 0.0526 | 0.834 | +0.0101 [+0.0007, +0.0232] | +0.0038 [-0.0042, +0.0100] | +0.0401 [+0.0016, +0.0775] | 0.55 |
| `G_ds_all_regmono_bag5` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees | reg mono x5 seeds | 0.0525 | 0.839 | +0.0101 [+0.0017, +0.0216] | +0.0090 [+0.0020, +0.0177] | +0.0163 [-0.0095, +0.0493] | 0.56 |
| `R_ds_lh_small_mono_bag5` | DAY+STATIC+LHF | trees | small mono x5 seeds | 0.0524 | 0.828 | +0.0100 [-0.0004, +0.0258] | +0.0030 [-0.0050, +0.0116] | +0.0457 [-0.0003, +0.0905] | 0.53 |
| `G_ds_ante_base_bag5` | DAY+STATIC+ANTE | trees | base x5 seeds | 0.0522 | 0.827 | +0.0098 [-0.0021, +0.0311] | -0.0002 [-0.0068, +0.0047] | +0.0513 [-0.0007, +0.1045] | 0.53 |
| `G_ds_soil_ante_regmono_bag5` | DAY+STATIC+SOIL+ANTE | trees | reg mono x5 seeds | 0.0520 | 0.834 | +0.0096 [+0.0009, +0.0234] | +0.0044 [-0.0029, +0.0093] | +0.0354 [-0.0024, +0.0828] | 0.54 |
| `R_ds_soil_small_mono_bag5` | DAY+STATIC+SOIL | trees | small mono x5 seeds | 0.0517 | 0.830 | +0.0093 [+0.0004, +0.0227] | +0.0032 [-0.0038, +0.0108] | +0.0387 [+0.0008, +0.0759] | 0.54 |
| `R_ds_small_mono_bag5` | DAY+STATIC | trees | small mono x5 seeds | 0.0517 | 0.828 | +0.0093 [-0.0010, +0.0231] | +0.0022 [-0.0059, +0.0112] | +0.0404 [+0.0008, +0.0797] | 0.52 |
| `G_ds_wind_zl_regmono_bag5` | DAY+STATIC+WIND+ZL | trees | reg mono x5 seeds | 0.0508 | 0.834 | +0.0084 [+0.0002, +0.0211] | +0.0065 [-0.0005, +0.0144] | +0.0176 [-0.0136, +0.0566] | 0.55 |
| `G_ds_zl_regmono_bag5` | DAY+STATIC+ZL | trees | reg mono x5 seeds | 0.0502 | 0.830 | +0.0078 [-0.0008, +0.0217] | +0.0038 [-0.0031, +0.0105] | +0.0295 [-0.0031, +0.0913] | 0.55 |
| `G_ds_regmono_bag5` | DAY+STATIC | trees | reg mono x5 seeds | 0.0498 | 0.829 | +0.0074 [-0.0019, +0.0205] | +0.0008 [-0.0063, +0.0058] | +0.0311 [-0.0063, +0.0801] | 0.52 |
| `M_S2_bag5` | DAY+STATIC+ANTE+Z | trees | base x5 seeds | 0.0493 | 0.825 | +0.0069 [-0.0035, +0.0238] | -0.0007 [-0.0082, +0.0052] | +0.0377 [-0.0036, +0.0804] | 0.54 |
| `G_ds_soil_regmono_bag5` | DAY+STATIC+SOIL | trees | reg mono x5 seeds | 0.0489 | 0.830 | +0.0065 [-0.0004, +0.0175] | +0.0029 [-0.0029, +0.0075] | +0.0263 [-0.0057, +0.0640] | 0.53 |
| `G_ds_z_regmono_bag5` | DAY+STATIC+Z | trees | reg mono x5 seeds | 0.0487 | 0.830 | +0.0063 [-0.0016, +0.0176] | +0.0019 [-0.0051, +0.0078] | +0.0269 [-0.0022, +0.0725] | 0.53 |
| `G_ds_wind_regmono_bag5` | DAY+STATIC+WIND | trees | reg mono x5 seeds | 0.0482 | 0.833 | +0.0058 [-0.0019, +0.0155] | +0.0034 [-0.0047, +0.0095] | +0.0129 [-0.0131, +0.0449] | 0.54 |
| `G_ds_zl_base_bag5` | DAY+STATIC+ZL | trees | base x5 seeds | 0.0482 | 0.817 | +0.0058 [-0.0039, +0.0188] | +0.0009 [-0.0085, +0.0106] | +0.0347 [-0.0004, +0.0784] | 0.51 |
| `G_ds_soil_base_bag5` | DAY+STATIC+SOIL | trees | base x5 seeds | 0.0481 | 0.824 | +0.0057 [-0.0019, +0.0195] | -0.0008 [-0.0060, +0.0065] | +0.0387 [+0.0019, +0.0713] | 0.51 |
| `G_full_lh_ante_zl_base_bag5` | DAY+WLAG+SOIL+SEAS+STATIC+LHF+ANTE+ZL | trees | base x5 seeds | 0.0478 | 0.827 | +0.0054 [+0.0003, +0.0121] | +0.0027 [-0.0017, +0.0073] | +0.0131 [-0.0015, +0.0315] | 0.53 |
| `G_full_lh_ante_zl_smallmono_bag5` | DAY+WLAG+SOIL+SEAS+STATIC+LHF+ANTE+ZL | trees | small mono x5 seeds | 0.0474 | 0.829 | +0.0050 [-0.0012, +0.0123] | +0.0028 [-0.0040, +0.0097] | +0.0121 [-0.0004, +0.0357] | 0.53 |
| `G_full_lh_ante_zl_regmono_bag5` | DAY+WLAG+SOIL+SEAS+STATIC+LHF+ANTE+ZL | trees | reg mono x5 seeds | 0.0469 | 0.832 | +0.0045 [-0.0011, +0.0102] | +0.0047 [-0.0017, +0.0102] | +0.0030 [-0.0094, +0.0189] | 0.53 |
| `M_S4_reg_bag5` | DAY+STATIC+Z | trees | reg x5 seeds | 0.0466 | 0.827 | +0.0041 [-0.0038, +0.0137] | +0.0004 [-0.0070, +0.0057] | +0.0229 [-0.0058, +0.0622] | 0.54 |
| `M_S1_reg_bag5` | DAY+STATIC | trees | reg x5 seeds | 0.0460 | 0.825 | +0.0036 [-0.0043, +0.0155] | -0.0012 [-0.0083, +0.0033] | +0.0255 [-0.0094, +0.0794] | 0.53 |
| `R_full_lh_small_mono_bag5` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | small mono x5 seeds | 0.0458 | 0.824 | +0.0034 [-0.0019, +0.0107] | +0.0015 [-0.0042, +0.0088] | +0.0114 [-0.0053, +0.0332] | 0.52 |
| `M_S3_reg_bag5` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | reg x5 seeds | 0.0447 | 0.828 | +0.0023 [-0.0030, +0.0070] | +0.0027 [-0.0026, +0.0072] | +0.0001 [-0.0123, +0.0146] | 0.53 |
| `G_full_lh_regmono_bag5` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | reg mono x5 seeds | 0.0444 | 0.827 | +0.0020 [-0.0036, +0.0063] | +0.0022 [-0.0033, +0.0065] | +0.0012 [-0.0133, +0.0152] | 0.53 |
| `B_full_nolfmc_bag5` | DAY+WLAG+SOIL+SEAS+STATIC | trees | base x5 seeds | 0.0431 | 0.825 | +0.0007 [-0.0023, +0.0036] | -0.0003 [-0.0036, +0.0011] | +0.0044 [-0.0042, +0.0188] | 0.51 |
| `G_ds_base_bag5` | DAY+STATIC | trees | base x5 seeds | 0.0424 | 0.816 | +0.0000 [-0.0077, +0.0109] | -0.0048 [-0.0125, +0.0014] | +0.0244 [-0.0066, +0.0687] | 0.50 |
| `M_S1_bag5` | DAY+STATIC | trees | base x5 seeds | 0.0424 | 0.816 | +0.0000 [-0.0077, +0.0109] | -0.0048 [-0.0125, +0.0014] | +0.0244 [-0.0066, +0.0687] | 0.50 |
| `B_full_bag5` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | base x5 seeds | 0.0424 | 0.823 | +0.0000 [+0.0000, +0.0000] | +0.0000 [+0.0000, +0.0000] | +0.0000 [+0.0000, +0.0000] | 0.51 |
| `B_full_bag5b` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | base x5 seeds | 0.0422 | 0.823 | -0.0002 [-0.0032, +0.0024] | -0.0005 [-0.0045, +0.0029] | +0.0011 [-0.0058, +0.0089] | 0.51 |
| `M_S4_bag5` | DAY+STATIC+Z | trees | base x5 seeds | 0.0416 | 0.819 | -0.0008 [-0.0076, +0.0068] | -0.0044 [-0.0133, +0.0024] | +0.0186 [-0.0046, +0.0463] | 0.52 |
| `G_ds_z_base_bag5` | DAY+STATIC+Z | trees | base x5 seeds | 0.0415 | 0.820 | -0.0009 [-0.0079, +0.0070] | -0.0044 [-0.0132, +0.0022] | +0.0178 [-0.0060, +0.0469] | 0.52 |
| `B_core_bag5` | DAY+WLAG+SOIL+SEAS+LHC | trees | base x5 seeds | 0.0202 | 0.751 | -0.0222 [-0.0323, -0.0139] | -0.0195 [-0.0276, -0.0129] | -0.0334 [-0.0659, -0.0065] | 0.36 |

## All single-seed runs (unseen year; gain against the baseline ensemble)

269 configurations in total with the ensembles. Single-seed scores carry the seed noise shown above.
`POST_*` = dryness anomaly as a multiplier after the named model.

| Config | Features | Model | Settings | PR-AUC | ROC-AUC | Gain, all years [95 %] | Gain, test years 2016-2021 | Gain, test years 2022-2024 | Top 10 % |
|---|---|---|---|---|---|---|---|---|---|
| `R_ds_all_small_mono_blend` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees+logistic | small mono | 0.0640 | 0.839 | +0.0216 [+0.0050, +0.0438] | +0.0138 [+0.0046, +0.0286] | +0.0485 [-0.0043, +0.1028] | 0.55 |
| `R_ds_wind_small_mono_blend` | DAY+STATIC+WIND | trees+logistic | small mono | 0.0609 | 0.831 | +0.0185 [+0.0022, +0.0396] | +0.0088 [-0.0012, +0.0212] | +0.0483 [-0.0056, +0.1027] | 0.54 |
| `R_ds_all_small_mono_chain` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees, chained >=4/40/400 ha | small mono | 0.0605 | 0.836 | +0.0181 [+0.0028, +0.0412] | +0.0133 [+0.0031, +0.0259] | +0.0390 [-0.0109, +0.0942] | 0.55 |
| `R_ds_all_small_mono_multi` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees, mean of >=4/40/400 ha | small mono | 0.0594 | 0.841 | +0.0170 [+0.0047, +0.0327] | +0.0145 [+0.0040, +0.0281] | +0.0269 [-0.0076, +0.0607] | 0.55 |
| `R_ds_wind_ante_zl_small_mono` | DAY+STATIC+WIND+ANTE+ZL | trees | small mono | 0.0589 | 0.838 | +0.0165 [+0.0024, +0.0351] | +0.0097 [+0.0000, +0.0204] | +0.0407 [-0.0043, +0.0858] | 0.56 |
| `R_ds_all_small` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees | small | 0.0588 | 0.836 | +0.0164 [+0.0028, +0.0389] | +0.0080 [+0.0017, +0.0135] | +0.0484 [-0.0026, +0.1137] | 0.55 |
| `M_S2_blend` | DAY+STATIC+ANTE+Z | trees+logistic | reg | 0.0587 | 0.831 | +0.0163 [+0.0004, +0.0430] | +0.0044 [-0.0028, +0.0117] | +0.0669 [-0.0028, +0.1301] | 0.56 |
| `R_ds_soil_small_mono_blend` | DAY+STATIC+SOIL | trees+logistic | small mono | 0.0584 | 0.831 | +0.0160 [+0.0019, +0.0350] | +0.0048 [-0.0025, +0.0135] | +0.0625 [+0.0048, +0.1095] | 0.54 |
| `R_ds_all_smallc_mono` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees | smallc mono | 0.0580 | 0.839 | +0.0156 [+0.0047, +0.0297] | +0.0122 [+0.0042, +0.0248] | +0.0297 [-0.0035, +0.0591] | 0.56 |
| `R_ds_all_smalllong_mono` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees | smalllong mono | 0.0580 | 0.840 | +0.0156 [+0.0033, +0.0301] | +0.0104 [+0.0014, +0.0206] | +0.0347 [-0.0016, +0.0685] | 0.54 |
| `R_ds_all_small5_mono` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees | small5 mono | 0.0578 | 0.841 | +0.0154 [+0.0040, +0.0320] | +0.0140 [+0.0043, +0.0267] | +0.0292 [-0.0073, +0.0723] | 0.55 |
| `R_ds_all_tiny_mono` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees | tiny mono | 0.0578 | 0.841 | +0.0154 [+0.0029, +0.0317] | +0.0124 [+0.0016, +0.0247] | +0.0258 [-0.0091, +0.0693] | 0.55 |
| `L_S2_spw20` | DAY+STATIC+ANTE+Z | trees, positives x21 | reg | 0.0576 | 0.835 | +0.0152 [+0.0002, +0.0451] | +0.0039 [-0.0021, +0.0105] | +0.0663 [-0.0053, +0.1355] | 0.55 |
| `R_ds_ante_z_small_mono_blend` | DAY+STATIC+ANTE+Z | trees+logistic | small mono | 0.0572 | 0.834 | +0.0148 [+0.0009, +0.0356] | +0.0046 [-0.0027, +0.0119] | +0.0541 [-0.0013, +0.1132] | 0.56 |
| `L_S2_spw5` | DAY+STATIC+ANTE+Z | trees, positives x6 | reg | 0.0568 | 0.835 | +0.0144 [+0.0003, +0.0422] | +0.0050 [-0.0019, +0.0122] | +0.0595 [-0.0054, +0.1302] | 0.55 |
| `R_ds_all_small_mono_seed1` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees | small mono | 0.0566 | 0.840 | +0.0142 [+0.0022, +0.0286] | +0.0083 [+0.0009, +0.0153] | +0.0322 [-0.0044, +0.0716] | 0.55 |
| `R_ds_ante_z_small_mono_chain` | DAY+STATIC+ANTE+Z | trees, chained >=4/40/400 ha | small mono | 0.0564 | 0.833 | +0.0140 [+0.0007, +0.0400] | +0.0056 [-0.0026, +0.0137] | +0.0516 [-0.0044, +0.1281] | 0.55 |
| `R_ds_all_small_mono` | DAY+STATIC+SOIL+WIND+ANTE+ZL+LHF | trees | small mono | 0.0564 | 0.841 | +0.0140 [+0.0037, +0.0262] | +0.0114 [+0.0023, +0.0220] | +0.0248 [-0.0047, +0.0546] | 0.55 |
| `M_S2_reg` | DAY+STATIC+ANTE+Z | trees | reg | 0.0562 | 0.833 | +0.0138 [+0.0001, +0.0399] | +0.0043 [-0.0025, +0.0104] | +0.0577 [-0.0047, +0.1176] | 0.56 |
| `R_ds_ante_z_tiny_mono` | DAY+STATIC+ANTE+Z | trees | tiny mono | 0.0560 | 0.836 | +0.0136 [+0.0018, +0.0331] | +0.0055 [-0.0030, +0.0136] | +0.0506 [+0.0027, +0.1043] | 0.56 |
| `R_ds_wind_small_mono_chain` | DAY+STATIC+WIND | trees, chained >=4/40/400 ha | small mono | 0.0559 | 0.836 | +0.0135 [+0.0002, +0.0362] | +0.0084 [+0.0005, +0.0170] | +0.0337 [-0.0122, +0.0981] | 0.55 |
| `R_ds_ante_z_small5_mono` | DAY+STATIC+ANTE+Z | trees | small5 mono | 0.0557 | 0.836 | +0.0133 [+0.0021, +0.0310] | +0.0058 [-0.0018, +0.0139] | +0.0433 [+0.0010, +0.0905] | 0.55 |
| `R_ds_ante_z_smallc_mono` | DAY+STATIC+ANTE+Z | trees | smallc mono | 0.0557 | 0.834 | +0.0133 [+0.0020, +0.0312] | +0.0064 [-0.0024, +0.0157] | +0.0430 [+0.0044, +0.0914] | 0.54 |
| `R_ds_ante_z_smalllong_mono` | DAY+STATIC+ANTE+Z | trees | smalllong mono | 0.0557 | 0.835 | +0.0133 [+0.0013, +0.0313] | +0.0044 [-0.0044, +0.0123] | +0.0551 [+0.0052, +0.1042] | 0.54 |
| `R_ds_wind_tiny_mono` | DAY+STATIC+WIND | trees | tiny mono | 0.0555 | 0.836 | +0.0131 [+0.0010, +0.0311] | +0.0093 [-0.0003, +0.0211] | +0.0263 [-0.0114, +0.0775] | 0.54 |
| `R_ds_zl_small_mono` | DAY+STATIC+ZL | trees | small mono | 0.0554 | 0.827 | +0.0130 [+0.0010, +0.0292] | +0.0063 [-0.0033, +0.0166] | +0.0415 [+0.0009, +0.0902] | 0.53 |
| `R_ds_ante_zl_small_mono` | DAY+STATIC+ANTE+ZL | trees | small mono | 0.0552 | 0.833 | +0.0128 [+0.0012, +0.0280] | +0.0055 [-0.0030, +0.0143] | +0.0378 [+0.0025, +0.0752] | 0.55 |
| `R_ds_wind_small5_mono` | DAY+STATIC+WIND | trees | small5 mono | 0.0551 | 0.835 | +0.0127 [+0.0010, +0.0290] | +0.0101 [+0.0000, +0.0217] | +0.0251 [-0.0112, +0.0729] | 0.55 |
| `R_ds_wind_small_mono` | DAY+STATIC+WIND | trees | small mono | 0.0550 | 0.835 | +0.0126 [+0.0004, +0.0290] | +0.0051 [-0.0031, +0.0155] | +0.0376 [-0.0047, +0.0833] | 0.53 |
| `R_ds_ante_z_small_mono_seed1` | DAY+STATIC+ANTE+Z | trees | small mono | 0.0548 | 0.834 | +0.0124 [+0.0008, +0.0303] | +0.0042 [-0.0050, +0.0128] | +0.0487 [+0.0045, +0.0993] | 0.55 |
| `L_S2_chain` | DAY+STATIC+ANTE+Z | trees, chained >=4/40/400 ha | reg | 0.0547 | 0.834 | +0.0123 [-0.0020, +0.0430] | +0.0026 [-0.0042, +0.0089] | +0.0626 [-0.0091, +0.1482] | 0.55 |
| `R_ds_lh_small_mono_seed1` | DAY+STATIC+LHF | trees | small mono | 0.0545 | 0.829 | +0.0121 [+0.0000, +0.0293] | +0.0049 [-0.0041, +0.0144] | +0.0470 [+0.0020, +0.0883] | 0.52 |
| `R_ds_wind_small_mono_multi` | DAY+STATIC+WIND | trees, mean of >=4/40/400 ha | small mono | 0.0544 | 0.834 | +0.0120 [+0.0013, +0.0271] | +0.0097 [-0.0008, +0.0210] | +0.0245 [-0.0080, +0.0666] | 0.54 |
| `M_S2_tiny` | DAY+STATIC+ANTE+Z | trees | tiny | 0.0544 | 0.836 | +0.0120 [+0.0001, +0.0354] | +0.0034 [-0.0040, +0.0095] | +0.0497 [-0.0019, +0.1147] | 0.57 |
| `R_ds_soil_tiny_mono` | DAY+STATIC+SOIL | trees | tiny mono | 0.0543 | 0.832 | +0.0119 [+0.0016, +0.0256] | +0.0044 [-0.0024, +0.0117] | +0.0367 [-0.0019, +0.0704] | 0.54 |
| `M_S2_long` | DAY+STATIC+ANTE+Z | trees | long | 0.0543 | 0.833 | +0.0118 [-0.0005, +0.0346] | +0.0024 [-0.0055, +0.0075] | +0.0558 [-0.0016, +0.1168] | 0.54 |
| `R_ds_lh_tiny_mono` | DAY+STATIC+LHF | trees | tiny mono | 0.0542 | 0.829 | +0.0118 [-0.0004, +0.0308] | +0.0022 [-0.0048, +0.0085] | +0.0503 [-0.0004, +0.1011] | 0.53 |
| `L_S2_multi` | DAY+STATIC+ANTE+Z | trees, mean of >=4/40/400 ha | reg | 0.0542 | 0.837 | +0.0117 [-0.0010, +0.0320] | +0.0053 [-0.0021, +0.0123] | +0.0411 [-0.0112, +0.0906] | 0.53 |
| `M_S2_reg_mono` | DAY+STATIC+ANTE+Z | trees | reg mono | 0.0541 | 0.836 | +0.0117 [+0.0017, +0.0284] | +0.0061 [-0.0012, +0.0132] | +0.0411 [-0.0030, +0.0991] | 0.55 |
| `R_ds_wind_smalllong_mono` | DAY+STATIC+WIND | trees | smalllong mono | 0.0540 | 0.835 | +0.0116 [+0.0009, +0.0242] | +0.0069 [-0.0027, +0.0182] | +0.0299 [-0.0048, +0.0616] | 0.54 |
| `R_ds_ante_z_small_mono_multi` | DAY+STATIC+ANTE+Z | trees, mean of >=4/40/400 ha | small mono | 0.0540 | 0.836 | +0.0116 [+0.0014, +0.0273] | +0.0066 [-0.0029, +0.0166] | +0.0368 [-0.0023, +0.0840] | 0.53 |
| `F_day_static_ante_zl` | DAY+STATIC+ANTE+ZL | trees | base | 0.0539 | 0.826 | +0.0115 [-0.0009, +0.0329] | +0.0024 [-0.0055, +0.0093] | +0.0519 [+0.0004, +0.1080] | 0.53 |
| `M_S2_small` | DAY+STATIC+ANTE+Z | trees | small | 0.0537 | 0.831 | +0.0112 [-0.0011, +0.0333] | +0.0019 [-0.0059, +0.0070] | +0.0535 [-0.0014, +0.1109] | 0.54 |
| `R_ds_ante_z_small` | DAY+STATIC+ANTE+Z | trees | small | 0.0537 | 0.831 | +0.0112 [-0.0011, +0.0333] | +0.0019 [-0.0059, +0.0070] | +0.0535 [-0.0014, +0.1109] | 0.54 |
| `R_ds_lh_small_mono_chain` | DAY+STATIC+LHF | trees, chained >=4/40/400 ha | small mono | 0.0537 | 0.831 | +0.0112 [-0.0010, +0.0353] | +0.0039 [-0.0042, +0.0100] | +0.0439 [-0.0110, +0.1203] | 0.53 |
| `R_ds_rain_small_mono` | DAY+STATIC+RAIN | trees | small mono | 0.0536 | 0.833 | +0.0112 [-0.0001, +0.0259] | +0.0041 [-0.0043, +0.0110] | +0.0370 [-0.0023, +0.0778] | 0.54 |
| `R_ds_z_smalllong_mono` | DAY+STATIC+Z | trees | smalllong mono | 0.0535 | 0.829 | +0.0111 [+0.0003, +0.0272] | +0.0032 [-0.0058, +0.0127] | +0.0471 [+0.0052, +0.0887] | 0.53 |
| `R_ds_z_tiny_mono` | DAY+STATIC+Z | trees | tiny mono | 0.0535 | 0.828 | +0.0111 [-0.0003, +0.0301] | +0.0029 [-0.0052, +0.0117] | +0.0445 [+0.0015, +0.0960] | 0.54 |
| `R_ds_z_small_mono_seed1` | DAY+STATIC+Z | trees | small mono | 0.0535 | 0.828 | +0.0110 [-0.0008, +0.0276] | +0.0047 [-0.0059, +0.0157] | +0.0398 [+0.0029, +0.0860] | 0.53 |
| `R_ds_small_mono_blend` | DAY+STATIC | trees+logistic | small mono | 0.0534 | 0.825 | +0.0110 [-0.0009, +0.0270] | +0.0007 [-0.0069, +0.0068] | +0.0534 [+0.0036, +0.1034] | 0.52 |
| `R_ds_z_small_mono_chain` | DAY+STATIC+Z | trees, chained >=4/40/400 ha | small mono | 0.0531 | 0.827 | +0.0107 [-0.0001, +0.0306] | +0.0041 [-0.0040, +0.0118] | +0.0393 [-0.0052, +0.1130] | 0.54 |
| `R_ds_ante_small_mono` | DAY+STATIC+ANTE | trees | small mono | 0.0530 | 0.833 | +0.0106 [-0.0006, +0.0279] | +0.0022 [-0.0056, +0.0084] | +0.0485 [-0.0011, +0.0988] | 0.53 |
| `R_ds_tiny_mono` | DAY+STATIC | trees | tiny mono | 0.0529 | 0.830 | +0.0105 [-0.0002, +0.0272] | +0.0028 [-0.0044, +0.0095] | +0.0401 [-0.0045, +0.0911] | 0.53 |
| `R_ds_lh_small_mono_multi` | DAY+STATIC+LHF | trees, mean of >=4/40/400 ha | small mono | 0.0526 | 0.829 | +0.0102 [-0.0005, +0.0246] | +0.0054 [-0.0046, +0.0154] | +0.0362 [-0.0058, +0.0811] | 0.51 |
| `R_ds_z_small5_mono` | DAY+STATIC+Z | trees | small5 mono | 0.0525 | 0.830 | +0.0101 [-0.0006, +0.0266] | +0.0030 [-0.0052, +0.0122] | +0.0422 [+0.0002, +0.0883] | 0.53 |
| `R_ds_soil_smallc_mono` | DAY+STATIC+SOIL | trees | smallc mono | 0.0525 | 0.831 | +0.0101 [+0.0021, +0.0203] | +0.0054 [-0.0019, +0.0137] | +0.0320 [+0.0036, +0.0578] | 0.53 |
| `R_ds_small_mono_chain` | DAY+STATIC | trees, chained >=4/40/400 ha | small mono | 0.0524 | 0.831 | +0.0100 [-0.0005, +0.0302] | +0.0031 [-0.0052, +0.0094] | +0.0436 [-0.0070, +0.1104] | 0.54 |
| `R_ds_smalllong_mono` | DAY+STATIC | trees | smalllong mono | 0.0524 | 0.828 | +0.0100 [-0.0014, +0.0251] | +0.0013 [-0.0067, +0.0100] | +0.0415 [+0.0004, +0.0842] | 0.51 |
| `R_ds_z_small_mono_multi` | DAY+STATIC+Z | trees, mean of >=4/40/400 ha | small mono | 0.0523 | 0.829 | +0.0099 [-0.0007, +0.0228] | +0.0065 [-0.0044, +0.0180] | +0.0264 [-0.0026, +0.0662] | 0.52 |
| `R_ds_soil_smalllong_mono` | DAY+STATIC+SOIL | trees | smalllong mono | 0.0523 | 0.831 | +0.0099 [+0.0002, +0.0228] | +0.0026 [-0.0046, +0.0102] | +0.0372 [+0.0016, +0.0703] | 0.53 |
| `R_ds_lh_z_small_mono` | DAY+STATIC+LHF+Z | trees | small mono | 0.0523 | 0.829 | +0.0099 [+0.0001, +0.0245] | +0.0037 [-0.0048, +0.0127] | +0.0422 [+0.0024, +0.0800] | 0.53 |
| `POST_M_S1_small_mono__rz_z12` |  |  |  | 0.0522 | 0.822 | +0.0098 [-0.0016, +0.0245] | +0.0018 [-0.0067, +0.0114] | +0.0456 [+0.0042, +0.0878] | 0.54 |
| `R_ds_soil_z_small_mono` | DAY+STATIC+SOIL+Z | trees | small mono | 0.0522 | 0.828 | +0.0098 [-0.0004, +0.0210] | +0.0041 [-0.0046, +0.0144] | +0.0328 [+0.0023, +0.0598] | 0.53 |
| `R_ds_z_small_mono_blend` | DAY+STATIC+Z | trees+logistic | small mono | 0.0522 | 0.826 | +0.0098 [-0.0004, +0.0255] | +0.0021 [-0.0063, +0.0103] | +0.0481 [+0.0053, +0.0946] | 0.54 |
| `M_S2_logit` | DAY+STATIC+ANTE+Z | logistic | C=0.1 | 0.0521 | 0.816 | +0.0097 [-0.0048, +0.0306] | -0.0007 [-0.0092, +0.0084] | +0.0562 [-0.0113, +0.1211] | 0.51 |
| `R_ds_small_mono_multi` | DAY+STATIC | trees, mean of >=4/40/400 ha | small mono | 0.0521 | 0.828 | +0.0097 [-0.0009, +0.0240] | +0.0048 [-0.0052, +0.0144] | +0.0341 [-0.0031, +0.0787] | 0.52 |
| `M_S2_small_mono` | DAY+STATIC+ANTE+Z | trees | small mono | 0.0520 | 0.836 | +0.0096 [+0.0000, +0.0244] | +0.0036 [-0.0045, +0.0108] | +0.0410 [+0.0010, +0.0895] | 0.56 |
| `R_ds_ante_z_small_mono` | DAY+STATIC+ANTE+Z | trees | small mono | 0.0520 | 0.836 | +0.0096 [+0.0000, +0.0244] | +0.0036 [-0.0045, +0.0108] | +0.0410 [+0.0010, +0.0895] | 0.56 |
| `R_ds_wind_small_mono_seed1` | DAY+STATIC+WIND | trees | small mono | 0.0519 | 0.834 | +0.0095 [-0.0005, +0.0227] | +0.0039 [-0.0034, +0.0121] | +0.0299 [-0.0068, +0.0735] | 0.53 |
| `R_ds_small5_mono` | DAY+STATIC | trees | small5 mono | 0.0518 | 0.830 | +0.0094 [-0.0001, +0.0221] | +0.0026 [-0.0049, +0.0100] | +0.0368 [-0.0029, +0.0783] | 0.53 |
| `R_ds_soil_small_mono_multi` | DAY+STATIC+SOIL | trees, mean of >=4/40/400 ha | small mono | 0.0518 | 0.833 | +0.0094 [+0.0007, +0.0220] | +0.0055 [-0.0028, +0.0144] | +0.0329 [-0.0033, +0.0689] | 0.53 |
| `R_ds_lh_small_mono_blend` | DAY+STATIC+LHF | trees+logistic | small mono | 0.0517 | 0.825 | +0.0093 [-0.0014, +0.0247] | +0.0006 [-0.0068, +0.0058] | +0.0539 [+0.0012, +0.1008] | 0.54 |
| `M_S2_logitC1` | DAY+STATIC+ANTE+Z | logistic | C=1.0 | 0.0517 | 0.814 | +0.0093 [-0.0056, +0.0319] | -0.0014 [-0.0100, +0.0067] | +0.0525 [-0.0121, +0.1205] | 0.51 |
| `R_ds_lh_small5_mono` | DAY+STATIC+LHF | trees | small5 mono | 0.0517 | 0.828 | +0.0093 [-0.0015, +0.0243] | +0.0019 [-0.0058, +0.0085] | +0.0356 [-0.0052, +0.0784] | 0.53 |
| `R_ds_soil0_z_small_mono` | DAY+STATIC+SOIL0+Z | trees | small mono | 0.0516 | 0.830 | +0.0092 [-0.0009, +0.0233] | +0.0023 [-0.0066, +0.0117] | +0.0392 [+0.0028, +0.0801] | 0.53 |
| `R_ds_lh_smalllong_mono` | DAY+STATIC+LHF | trees | smalllong mono | 0.0515 | 0.829 | +0.0091 [-0.0007, +0.0235] | +0.0027 [-0.0054, +0.0114] | +0.0422 [+0.0001, +0.0832] | 0.54 |
| `M_S1_small_mono` | DAY+STATIC | trees | small mono | 0.0515 | 0.828 | +0.0090 [-0.0019, +0.0230] | +0.0007 [-0.0073, +0.0086] | +0.0447 [+0.0034, +0.0848] | 0.52 |
| `R_ds_small_mono` | DAY+STATIC | trees | small mono | 0.0515 | 0.828 | +0.0090 [-0.0019, +0.0230] | +0.0007 [-0.0073, +0.0086] | +0.0447 [+0.0034, +0.0848] | 0.52 |
| `R_ds_soil_small5_mono` | DAY+STATIC+SOIL | trees | small5 mono | 0.0514 | 0.830 | +0.0090 [+0.0001, +0.0201] | +0.0039 [-0.0034, +0.0116] | +0.0313 [-0.0032, +0.0606] | 0.53 |
| `R_ds_soil_small_mono_chain` | DAY+STATIC+SOIL | trees, chained >=4/40/400 ha | small mono | 0.0514 | 0.832 | +0.0090 [-0.0002, +0.0252] | +0.0039 [-0.0037, +0.0093] | +0.0346 [-0.0093, +0.0867] | 0.54 |
| `R_ds_lh_smallc_mono` | DAY+STATIC+LHF | trees | smallc mono | 0.0513 | 0.829 | +0.0089 [-0.0016, +0.0240] | +0.0029 [-0.0047, +0.0099] | +0.0419 [-0.0036, +0.0951] | 0.54 |
| `M_S2_mono` | DAY+STATIC+ANTE+Z | trees | base mono | 0.0513 | 0.828 | +0.0089 [-0.0018, +0.0263] | +0.0026 [-0.0074, +0.0120] | +0.0439 [-0.0006, +0.1037] | 0.53 |
| `R_ds_wind_small` | DAY+STATIC+WIND | trees | small | 0.0511 | 0.829 | +0.0087 [-0.0021, +0.0245] | +0.0026 [-0.0040, +0.0105] | +0.0285 [-0.0120, +0.0760] | 0.55 |
| `R_ds_seas_small_mono` | DAY+STATIC+SEAS | trees | small mono | 0.0511 | 0.824 | +0.0086 [-0.0015, +0.0230] | +0.0017 [-0.0061, +0.0096] | +0.0388 [+0.0000, +0.0761] | 0.52 |
| `R_ds_z_smallc_mono` | DAY+STATIC+Z | trees | smallc mono | 0.0510 | 0.829 | +0.0086 [-0.0014, +0.0242] | +0.0025 [-0.0064, +0.0109] | +0.0425 [+0.0039, +0.0938] | 0.54 |
| `L_S2_y40` | DAY+STATIC+ANTE+Z | trees, label >= 40 ha | reg | 0.0510 | 0.834 | +0.0086 [-0.0032, +0.0268] | +0.0035 [-0.0044, +0.0128] | +0.0324 [-0.0114, +0.0798] | 0.54 |
| `M_S4_small_mono` | DAY+STATIC+Z | trees | small mono | 0.0509 | 0.830 | +0.0085 [-0.0014, +0.0208] | +0.0034 [-0.0068, +0.0138] | +0.0361 [+0.0007, +0.0700] | 0.53 |
| `R_ds_soil_small` | DAY+STATIC+SOIL | trees | small | 0.0508 | 0.824 | +0.0084 [+0.0009, +0.0191] | +0.0021 [-0.0026, +0.0080] | +0.0357 [+0.0026, +0.0643] | 0.52 |
| `M_S3_blend` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees+logistic | reg | 0.0508 | 0.827 | +0.0084 [+0.0003, +0.0167] | +0.0055 [-0.0020, +0.0137] | +0.0185 [-0.0033, +0.0401] | 0.52 |
| `R_ds_soil0_small_mono` | DAY+STATIC+SOIL0 | trees | small mono | 0.0508 | 0.827 | +0.0084 [-0.0015, +0.0207] | +0.0015 [-0.0069, +0.0104] | +0.0333 [-0.0016, +0.0659] | 0.52 |
| `R_ds_smallc_mono` | DAY+STATIC | trees | smallc mono | 0.0507 | 0.826 | +0.0083 [-0.0004, +0.0214] | +0.0025 [-0.0047, +0.0102] | +0.0345 [+0.0005, +0.0763] | 0.52 |
| `R_full_lh_small_mono_blend` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees+logistic | small mono | 0.0507 | 0.827 | +0.0083 [+0.0011, +0.0178] | +0.0058 [-0.0012, +0.0160] | +0.0232 [-0.0009, +0.0453] | 0.54 |
| `R_ds_z_small_mono` | DAY+STATIC+Z | trees | small mono | 0.0507 | 0.829 | +0.0083 [-0.0017, +0.0203] | +0.0032 [-0.0071, +0.0139] | +0.0353 [+0.0006, +0.0689] | 0.54 |
| `R_ds_soil_small_mono` | DAY+STATIC+SOIL | trees | small mono | 0.0506 | 0.832 | +0.0082 [-0.0003, +0.0198] | +0.0025 [-0.0044, +0.0102] | +0.0387 [+0.0021, +0.0702] | 0.53 |
| `R_ds_soil_ante_small_mono` | DAY+STATIC+SOIL+ANTE | trees | small mono | 0.0505 | 0.833 | +0.0081 [-0.0003, +0.0189] | +0.0031 [-0.0053, +0.0093] | +0.0311 [+0.0019, +0.0611] | 0.54 |
| `R_ds_lh_small_mono` | DAY+STATIC+LHF | trees | small mono | 0.0504 | 0.828 | +0.0080 [-0.0019, +0.0206] | +0.0005 [-0.0074, +0.0066] | +0.0386 [+0.0005, +0.0750] | 0.52 |
| `R_ds_small_mono_seed1` | DAY+STATIC | trees | small mono | 0.0503 | 0.826 | +0.0079 [-0.0017, +0.0209] | +0.0013 [-0.0063, +0.0103] | +0.0419 [+0.0023, +0.0799] | 0.52 |
| `R_ds_wind_smallc_mono` | DAY+STATIC+WIND | trees | smallc mono | 0.0500 | 0.833 | +0.0075 [-0.0013, +0.0186] | +0.0059 [-0.0032, +0.0151] | +0.0167 [-0.0089, +0.0493] | 0.54 |
| `R_dayx_s_small_mono` | DAYX+STATIC | trees | small mono | 0.0498 | 0.826 | +0.0074 [-0.0011, +0.0197] | +0.0021 [-0.0058, +0.0107] | +0.0287 [-0.0017, +0.0669] | 0.52 |
| `M_S4_blend` | DAY+STATIC+Z | trees+logistic | reg | 0.0498 | 0.824 | +0.0074 [-0.0024, +0.0214] | +0.0012 [-0.0067, +0.0073] | +0.0374 [-0.0059, +0.0944] | 0.53 |
| `M_S2_xt` | DAY+STATIC+ANTE+Z | trees | xt | 0.0498 | 0.833 | +0.0074 [-0.0014, +0.0199] | +0.0024 [-0.0049, +0.0077] | +0.0330 [-0.0071, +0.0695] | 0.52 |
| `M_S1_blend` | DAY+STATIC | trees+logistic | reg | 0.0497 | 0.822 | +0.0073 [-0.0030, +0.0257] | -0.0008 [-0.0086, +0.0051] | +0.0500 [-0.0052, +0.1134] | 0.53 |
| `M_S1_long` | DAY+STATIC | trees | long | 0.0496 | 0.823 | +0.0072 [-0.0032, +0.0252] | -0.0013 [-0.0084, +0.0041] | +0.0459 [-0.0047, +0.1054] | 0.52 |
| `M_S4_long` | DAY+STATIC+Z | trees | long | 0.0494 | 0.826 | +0.0070 [-0.0020, +0.0190] | +0.0010 [-0.0068, +0.0079] | +0.0329 [-0.0004, +0.0728] | 0.53 |
| `F_day_static_ante_z` | DAY+STATIC+ANTE+Z | trees | base | 0.0493 | 0.816 | +0.0069 [-0.0047, +0.0268] | -0.0022 [-0.0107, +0.0050] | +0.0472 [-0.0027, +0.0970] | 0.51 |
| `L_S3_chain` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees, chained >=4/40/400 ha | reg | 0.0491 | 0.834 | +0.0067 [-0.0001, +0.0152] | +0.0041 [-0.0022, +0.0099] | +0.0206 [-0.0035, +0.0544] | 0.53 |
| `M_S1_reg_mono` | DAY+STATIC | trees | reg mono | 0.0489 | 0.828 | +0.0065 [-0.0029, +0.0193] | +0.0004 [-0.0067, +0.0051] | +0.0270 [-0.0089, +0.0764] | 0.53 |
| `M_S4_xt` | DAY+STATIC+Z | trees | xt | 0.0489 | 0.818 | +0.0065 [-0.0034, +0.0222] | +0.0004 [-0.0086, +0.0070] | +0.0304 [-0.0070, +0.0897] | 0.52 |
| `R_ds_soil_small_mono_seed1` | DAY+STATIC+SOIL | trees | small mono | 0.0489 | 0.829 | +0.0065 [-0.0018, +0.0180] | +0.0012 [-0.0062, +0.0082] | +0.0321 [-0.0014, +0.0631] | 0.53 |
| `M_S4_tiny` | DAY+STATIC+Z | trees | tiny | 0.0485 | 0.827 | +0.0061 [-0.0037, +0.0200] | +0.0002 [-0.0083, +0.0068] | +0.0308 [-0.0030, +0.0798] | 0.54 |
| `R_full_lh_small_mono_multi` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees, mean of >=4/40/400 ha | small mono | 0.0485 | 0.830 | +0.0061 [-0.0011, +0.0137] | +0.0049 [-0.0031, +0.0146] | +0.0116 [-0.0031, +0.0318] | 0.52 |
| `POST_M_S1_reg_mono__rz_z12` |  |  |  | 0.0485 | 0.823 | +0.0061 [-0.0025, +0.0171] | +0.0009 [-0.0064, +0.0066] | +0.0287 [-0.0020, +0.0787] | 0.54 |
| `R_ds_vs3_small_mono` | DAY+STATIC+VS3 | trees | small mono | 0.0483 | 0.826 | +0.0059 [-0.0025, +0.0173] | -0.0002 [-0.0082, +0.0078] | +0.0327 [-0.0016, +0.0639] | 0.51 |
| `M_S4_reg_mono` | DAY+STATIC+Z | trees | reg mono | 0.0482 | 0.830 | +0.0058 [-0.0021, +0.0175] | +0.0015 [-0.0056, +0.0077] | +0.0268 [-0.0050, +0.0742] | 0.55 |
| `M_S1_tiny` | DAY+STATIC | trees | tiny | 0.0482 | 0.826 | +0.0058 [-0.0037, +0.0202] | -0.0007 [-0.0077, +0.0043] | +0.0272 [-0.0099, +0.0733] | 0.53 |
| `M_S1_small` | DAY+STATIC | trees | small | 0.0481 | 0.822 | +0.0057 [-0.0040, +0.0177] | -0.0016 [-0.0086, +0.0049] | +0.0336 [-0.0044, +0.0683] | 0.52 |
| `R_ds_small` | DAY+STATIC | trees | small | 0.0481 | 0.822 | +0.0057 [-0.0040, +0.0177] | -0.0016 [-0.0086, +0.0049] | +0.0336 [-0.0044, +0.0683] | 0.52 |
| `M_S3_long` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | long | 0.0478 | 0.825 | +0.0054 [-0.0001, +0.0132] | +0.0026 [-0.0021, +0.0059] | +0.0163 [-0.0047, +0.0490] | 0.52 |
| `L_S1_spw5` | DAY+STATIC | trees, positives x6 | reg | 0.0477 | 0.826 | +0.0053 [-0.0054, +0.0210] | -0.0021 [-0.0091, +0.0031] | +0.0313 [-0.0106, +0.0776] | 0.52 |
| `M_S3_logit` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | logistic | C=0.1 | 0.0477 | 0.817 | +0.0053 [-0.0044, +0.0151] | +0.0021 [-0.0075, +0.0129] | +0.0173 [-0.0083, +0.0407] | 0.52 |
| `L_S1_chain` | DAY+STATIC | trees, chained >=4/40/400 ha | reg | 0.0477 | 0.831 | +0.0053 [-0.0038, +0.0209] | +0.0001 [-0.0072, +0.0060] | +0.0300 [-0.0133, +0.0924] | 0.53 |
| `M_S4_logitC1` | DAY+STATIC+Z | logistic | C=1.0 | 0.0475 | 0.809 | +0.0051 [-0.0060, +0.0206] | -0.0019 [-0.0115, +0.0073] | +0.0416 [-0.0082, +0.0918] | 0.50 |
| `M_S1_logit` | DAY+STATIC | logistic | C=0.1 | 0.0475 | 0.805 | +0.0051 [-0.0071, +0.0225] | -0.0032 [-0.0119, +0.0062] | +0.0470 [-0.0068, +0.0986] | 0.49 |
| `R_full_lh_smalllong_mono` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | smalllong mono | 0.0474 | 0.824 | +0.0050 [-0.0006, +0.0128] | +0.0022 [-0.0036, +0.0098] | +0.0183 [-0.0020, +0.0439] | 0.54 |
| `R_full_lh_small_mono_chain` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees, chained >=4/40/400 ha | small mono | 0.0474 | 0.828 | +0.0050 [-0.0017, +0.0142] | +0.0017 [-0.0039, +0.0077] | +0.0214 [-0.0074, +0.0640] | 0.54 |
| `M_S1_logitC1` | DAY+STATIC | logistic | C=1.0 | 0.0471 | 0.804 | +0.0046 [-0.0071, +0.0211] | -0.0033 [-0.0126, +0.0060] | +0.0419 [-0.0096, +0.0974] | 0.49 |
| `F_day_static_ante` | DAY+STATIC+ANTE | trees | base | 0.0467 | 0.821 | +0.0043 [-0.0049, +0.0208] | -0.0020 [-0.0087, +0.0031] | +0.0327 [-0.0063, +0.0961] | 0.51 |
| `M_S4_logit` | DAY+STATIC+Z | logistic | C=0.1 | 0.0467 | 0.809 | +0.0043 [-0.0058, +0.0176] | -0.0023 [-0.0116, +0.0066] | +0.0423 [-0.0067, +0.0917] | 0.51 |
| `B_full` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | base | 0.0466 | 0.820 | +0.0042 [-0.0019, +0.0138] | +0.0044 [-0.0036, +0.0153] | +0.0076 [-0.0084, +0.0280] | 0.51 |
| `M_S3_tiny` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | tiny | 0.0466 | 0.825 | +0.0042 [-0.0006, +0.0100] | +0.0025 [-0.0021, +0.0066] | +0.0100 [-0.0085, +0.0275] | 0.53 |
| `R_d_terr_small_mono` | DAY+TERR | trees | small mono | 0.0466 | 0.819 | +0.0042 [-0.0044, +0.0164] | -0.0012 [-0.0102, +0.0077] | +0.0327 [+0.0013, +0.0645] | 0.53 |
| `F_day_static_soil` | DAY+STATIC+SOIL | trees | base | 0.0465 | 0.820 | +0.0041 [-0.0029, +0.0157] | -0.0008 [-0.0068, +0.0080] | +0.0331 [+0.0033, +0.0630] | 0.52 |
| `R_full_lh_z_small_mono` | DAY+WLAG+SOIL+SEAS+STATIC+LHF+Z | trees | small mono | 0.0463 | 0.825 | +0.0039 [-0.0035, +0.0117] | +0.0030 [-0.0055, +0.0118] | +0.0059 [-0.0088, +0.0262] | 0.53 |
| `R_ds_z_small` | DAY+STATIC+Z | trees | small | 0.0463 | 0.823 | +0.0039 [-0.0030, +0.0127] | +0.0000 [-0.0078, +0.0083] | +0.0185 [-0.0030, +0.0392] | 0.53 |
| `M_S4_small` | DAY+STATIC+Z | trees | small | 0.0462 | 0.823 | +0.0038 [-0.0033, +0.0130] | -0.0002 [-0.0083, +0.0079] | +0.0197 [-0.0018, +0.0417] | 0.53 |
| `R_ds_lh_small` | DAY+STATIC+LHF | trees | small | 0.0459 | 0.821 | +0.0035 [-0.0041, +0.0132] | -0.0010 [-0.0080, +0.0054] | +0.0271 [-0.0062, +0.0550] | 0.51 |
| `L_S3_spw5` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees, positives x6 | reg | 0.0458 | 0.826 | +0.0034 [-0.0022, +0.0084] | +0.0035 [-0.0021, +0.0082] | +0.0033 [-0.0126, +0.0169] | 0.51 |
| `M_S4_mono` | DAY+STATIC+Z | trees | base mono | 0.0458 | 0.820 | +0.0034 [-0.0055, +0.0147] | -0.0002 [-0.0104, +0.0107] | +0.0222 [-0.0034, +0.0583] | 0.51 |
| `F_day_static_zl` | DAY+STATIC+ZL | trees | base | 0.0457 | 0.812 | +0.0033 [-0.0057, +0.0167] | -0.0003 [-0.0107, +0.0126] | +0.0272 [-0.0070, +0.0622] | 0.53 |
| `M_S1_xt` | DAY+STATIC | trees | xt | 0.0457 | 0.818 | +0.0033 [-0.0052, +0.0161] | -0.0024 [-0.0094, +0.0026] | +0.0248 [-0.0117, +0.0778] | 0.48 |
| `M_S1_reg` | DAY+STATIC | trees | reg | 0.0457 | 0.825 | +0.0033 [-0.0052, +0.0166] | -0.0019 [-0.0088, +0.0030] | +0.0296 [-0.0098, +0.0867] | 0.52 |
| `R_full_lh_small5_mono` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | small5 mono | 0.0456 | 0.823 | +0.0032 [-0.0019, +0.0090] | +0.0013 [-0.0047, +0.0085] | +0.0097 [-0.0080, +0.0303] | 0.52 |
| `R_full_lh_tiny_mono` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | tiny mono | 0.0456 | 0.825 | +0.0032 [-0.0020, +0.0087] | +0.0016 [-0.0035, +0.0066] | +0.0079 [-0.0129, +0.0324] | 0.53 |
| `L_S1_spw20` | DAY+STATIC | trees, positives x21 | reg | 0.0456 | 0.824 | +0.0032 [-0.0067, +0.0210] | -0.0027 [-0.0103, +0.0020] | +0.0270 [-0.0145, +0.0924] | 0.51 |
| `R_full_lh_smallc_mono` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | smallc mono | 0.0455 | 0.823 | +0.0031 [-0.0022, +0.0095] | +0.0008 [-0.0046, +0.0066] | +0.0127 [-0.0048, +0.0470] | 0.52 |
| `M_S3_xt` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | xt | 0.0455 | 0.826 | +0.0030 [-0.0027, +0.0088] | +0.0010 [-0.0044, +0.0052] | +0.0122 [-0.0066, +0.0342] | 0.51 |
| `L_S3_spw20` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees, positives x21 | reg | 0.0454 | 0.828 | +0.0030 [-0.0027, +0.0080] | +0.0034 [-0.0013, +0.0077] | +0.0005 [-0.0167, +0.0148] | 0.53 |
| `L_S3_multi` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees, mean of >=4/40/400 ha | reg | 0.0452 | 0.830 | +0.0028 [-0.0030, +0.0099] | +0.0041 [-0.0030, +0.0119] | -0.0016 [-0.0143, +0.0097] | 0.51 |
| `M_S4_reg` | DAY+STATIC+Z | trees | reg | 0.0451 | 0.826 | +0.0027 [-0.0049, +0.0114] | -0.0001 [-0.0076, +0.0055] | +0.0167 [-0.0099, +0.0548] | 0.54 |
| `M_S3_small_mono` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | small mono | 0.0451 | 0.823 | +0.0027 [-0.0027, +0.0089] | +0.0016 [-0.0051, +0.0101] | +0.0057 [-0.0064, +0.0207] | 0.53 |
| `R_full_lh_small_mono` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | small mono | 0.0451 | 0.823 | +0.0027 [-0.0027, +0.0089] | +0.0016 [-0.0051, +0.0101] | +0.0057 [-0.0064, +0.0207] | 0.53 |
| `R_ds_wlag_small_mono` | DAY+WLAG+STATIC | trees | small mono | 0.0450 | 0.820 | +0.0026 [-0.0032, +0.0091] | +0.0021 [-0.0049, +0.0100] | +0.0052 [-0.0122, +0.0251] | 0.52 |
| `M_S1_mono` | DAY+STATIC | trees | base mono | 0.0450 | 0.815 | +0.0026 [-0.0064, +0.0158] | -0.0033 [-0.0119, +0.0045] | +0.0301 [-0.0060, +0.0742] | 0.50 |
| `R_full_lh_small_mono_seed1` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | small mono | 0.0450 | 0.822 | +0.0026 [-0.0028, +0.0093] | +0.0005 [-0.0056, +0.0074] | +0.0104 [-0.0063, +0.0330] | 0.53 |
| `M_S3_logitC1` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | logistic | C=1.0 | 0.0447 | 0.814 | +0.0023 [-0.0062, +0.0112] | -0.0001 [-0.0082, +0.0107] | +0.0092 [-0.0126, +0.0304] | 0.51 |
| `POST_M_S1_bag5__rz_z12` |  |  |  | 0.0445 | 0.815 | +0.0021 [-0.0058, +0.0137] | -0.0030 [-0.0109, +0.0039] | +0.0301 [-0.0038, +0.0782] | 0.52 |
| `F_day_static_seas_ante_z` | DAY+STATIC+SEAS+ANTE+Z | trees | base | 0.0444 | 0.819 | +0.0020 [-0.0071, +0.0154] | -0.0029 [-0.0112, +0.0029] | +0.0260 [-0.0113, +0.0717] | 0.51 |
| `M_S3_small` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | small | 0.0443 | 0.824 | +0.0019 [-0.0022, +0.0052] | +0.0005 [-0.0039, +0.0030] | +0.0071 [-0.0072, +0.0198] | 0.52 |
| `R_full_lh_small` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | small | 0.0443 | 0.824 | +0.0019 [-0.0022, +0.0052] | +0.0005 [-0.0039, +0.0030] | +0.0071 [-0.0072, +0.0198] | 0.52 |
| `M_S3_reg_mono` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | reg mono | 0.0443 | 0.827 | +0.0019 [-0.0033, +0.0061] | +0.0014 [-0.0042, +0.0058] | +0.0031 [-0.0112, +0.0176] | 0.53 |
| `L_S1_multi` | DAY+STATIC | trees, mean of >=4/40/400 ha | reg | 0.0439 | 0.828 | +0.0015 [-0.0057, +0.0114] | -0.0007 [-0.0090, +0.0053] | +0.0102 [-0.0154, +0.0469] | 0.52 |
| `M_S3_reg` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | reg | 0.0438 | 0.826 | +0.0014 [-0.0030, +0.0068] | +0.0027 [-0.0031, +0.0074] | +0.0009 [-0.0119, +0.0220] | 0.53 |
| `F_day_static_rain_z` | DAY+STATIC+RAIN+Z | trees | base | 0.0436 | 0.821 | +0.0012 [-0.0057, +0.0085] | -0.0017 [-0.0100, +0.0051] | +0.0122 [-0.0086, +0.0353] | 0.51 |
| `POST_F_day_static__rz_z12` |  |  |  | 0.0433 | 0.809 | +0.0009 [-0.0069, +0.0113] | -0.0038 [-0.0122, +0.0029] | +0.0311 [-0.0051, +0.0751] | 0.51 |
| `F_full_ante_zl` | DAY+WLAG+SOIL+SEAS+STATIC+LHF+ANTE+ZL | trees | base | 0.0433 | 0.824 | +0.0009 [-0.0043, +0.0057] | -0.0005 [-0.0078, +0.0046] | +0.0048 [-0.0054, +0.0185] | 0.54 |
| `L_S3_y40` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees, label >= 40 ha | reg | 0.0431 | 0.823 | +0.0007 [-0.0061, +0.0079] | +0.0033 [-0.0050, +0.0107] | -0.0079 [-0.0227, +0.0053] | 0.49 |
| `M_S3_mono` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | base mono | 0.0428 | 0.822 | +0.0004 [-0.0052, +0.0066] | +0.0028 [-0.0030, +0.0102] | -0.0077 [-0.0214, +0.0038] | 0.53 |
| `F_day_static_soil0` | DAY+STATIC+SOIL0 | trees | base | 0.0427 | 0.819 | +0.0003 [-0.0073, +0.0108] | -0.0043 [-0.0118, +0.0014] | +0.0181 [-0.0106, +0.0515] | 0.50 |
| `F_day_static_seas` | DAY+STATIC+SEAS | trees | base | 0.0423 | 0.817 | -0.0001 [-0.0072, +0.0093] | -0.0038 [-0.0118, +0.0042] | +0.0143 [-0.0108, +0.0438] | 0.52 |
| `F_day_static_vs3` | DAY+STATIC+VS3 | trees | base | 0.0423 | 0.813 | -0.0001 [-0.0072, +0.0083] | -0.0044 [-0.0121, +0.0026] | +0.0235 [-0.0058, +0.0549] | 0.49 |
| `F_day_static_an` | DAY+STATIC+AN | trees | base | 0.0423 | 0.819 | -0.0001 [-0.0070, +0.0073] | -0.0028 [-0.0111, +0.0051] | +0.0213 [-0.0017, +0.0500] | 0.52 |
| `F_day_static_z` | DAY+STATIC+Z | trees | base | 0.0417 | 0.814 | -0.0007 [-0.0078, +0.0093] | -0.0046 [-0.0130, +0.0048] | +0.0177 [-0.0057, +0.0481] | 0.51 |
| `L_S1_y40` | DAY+STATIC | trees, label >= 40 ha | reg | 0.0415 | 0.824 | -0.0009 [-0.0078, +0.0064] | -0.0018 [-0.0113, +0.0049] | +0.0008 [-0.0164, +0.0233] | 0.52 |
| `F_day_wlag_static` | DAY+WLAG+STATIC | trees | base | 0.0414 | 0.818 | -0.0010 [-0.0058, +0.0033] | -0.0013 [-0.0078, +0.0033] | -0.0007 [-0.0166, +0.0145] | 0.51 |
| `F_day_static_lh` | DAY+STATIC+LHF | trees | base | 0.0412 | 0.812 | -0.0012 [-0.0079, +0.0063] | -0.0038 [-0.0114, +0.0016] | +0.0134 [-0.0100, +0.0388] | 0.48 |
| `B_full_seed3` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | base | 0.0411 | 0.820 | -0.0013 [-0.0067, +0.0026] | -0.0013 [-0.0061, +0.0030] | -0.0002 [-0.0164, +0.0132] | 0.48 |
| `F_day_static` | DAY+STATIC | trees | base | 0.0410 | 0.809 | -0.0014 [-0.0083, +0.0078] | -0.0057 [-0.0140, +0.0002] | +0.0257 [-0.0054, +0.0673] | 0.49 |
| `F_full_nobi` | DAYX+WLAG+SOIL+SEAS+STATIC+LHF | trees | base | 0.0407 | 0.823 | -0.0017 [-0.0061, +0.0016] | -0.0011 [-0.0062, +0.0023] | -0.0050 [-0.0181, +0.0035] | 0.51 |
| `F_day_wlag_static_seas` | DAY+WLAG+STATIC+SEAS | trees | base | 0.0406 | 0.813 | -0.0018 [-0.0063, +0.0025] | -0.0032 [-0.0104, +0.0020] | +0.0010 [-0.0074, +0.0088] | 0.49 |
| `F_full_ante` | DAY+WLAG+SOIL+SEAS+STATIC+LHF+ANTE | trees | base | 0.0404 | 0.823 | -0.0020 [-0.0070, +0.0043] | -0.0048 [-0.0116, -0.0005] | +0.0110 [-0.0029, +0.0376] | 0.51 |
| `F_day_static_zsf` | DAY+STATIC+ZSF | trees | base | 0.0403 | 0.810 | -0.0021 [-0.0088, +0.0066] | -0.0044 [-0.0129, +0.0054] | +0.0197 [-0.0050, +0.0470] | 0.49 |
| `L_S2_logsize` | DAY+STATIC+ANTE+Z | trees, log size regression | reg | 0.0402 | 0.798 | -0.0022 [-0.0099, +0.0104] | -0.0038 [-0.0113, +0.0027] | +0.0111 [-0.0199, +0.0683] | 0.46 |
| `F_full_zl` | DAY+WLAG+SOIL+SEAS+STATIC+LHF+ZL | trees | base | 0.0397 | 0.819 | -0.0027 [-0.0067, +0.0004] | -0.0016 [-0.0070, +0.0019] | -0.0057 [-0.0167, +0.0120] | 0.50 |
| `F_full_z` | DAY+WLAG+SOIL+SEAS+STATIC+LHF+Z | trees | base | 0.0388 | 0.817 | -0.0036 [-0.0085, +0.0005] | -0.0024 [-0.0084, +0.0024] | -0.0076 [-0.0185, +0.0005] | 0.50 |
| `F_day_terr` | DAY+TERR | trees | base | 0.0388 | 0.805 | -0.0036 [-0.0111, +0.0059] | -0.0073 [-0.0166, +0.0011] | +0.0127 [-0.0113, +0.0350] | 0.48 |
| `B_full_seed1` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | base | 0.0386 | 0.814 | -0.0038 [-0.0077, -0.0001] | -0.0041 [-0.0083, -0.0001] | -0.0036 [-0.0153, +0.0086] | 0.49 |
| `B_full_nolfmc` | DAY+WLAG+SOIL+SEAS+STATIC | trees | base | 0.0385 | 0.821 | -0.0039 [-0.0085, -0.0003] | -0.0032 [-0.0087, +0.0001] | -0.0070 [-0.0194, +0.0073] | 0.51 |
| `B_full_seed2` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | base | 0.0382 | 0.818 | -0.0042 [-0.0083, -0.0012] | -0.0049 [-0.0105, -0.0013] | -0.0027 [-0.0101, +0.0015] | 0.51 |
| `B_full_seed4` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees | base | 0.0376 | 0.818 | -0.0048 [-0.0099, -0.0008] | -0.0028 [-0.0074, +0.0012] | -0.0113 [-0.0271, +0.0054] | 0.50 |
| `F_dayx_static` | DAYX+STATIC | trees | base | 0.0371 | 0.810 | -0.0053 [-0.0117, +0.0012] | -0.0086 [-0.0174, -0.0025] | +0.0057 [-0.0127, +0.0269] | 0.49 |
| `L_S1_logsize` | DAY+STATIC | trees, log size regression | reg | 0.0365 | 0.788 | -0.0059 [-0.0138, +0.0061] | -0.0087 [-0.0173, -0.0020] | +0.0051 [-0.0236, +0.0577] | 0.46 |
| `L_S2_y4` | DAY+STATIC+ANTE+Z | trees, label >= 4 ha | reg | 0.0355 | 0.807 | -0.0069 [-0.0141, +0.0006] | -0.0047 [-0.0139, +0.0045] | -0.0118 [-0.0307, +0.0048] | 0.46 |
| `L_S3_logsize` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees, log size regression | reg | 0.0351 | 0.785 | -0.0073 [-0.0142, +0.0024] | -0.0082 [-0.0164, -0.0002] | +0.0135 [-0.0191, +0.0717] | 0.44 |
| `L_S1_y4` | DAY+STATIC | trees, label >= 4 ha | reg | 0.0305 | 0.799 | -0.0119 [-0.0193, -0.0049] | -0.0082 [-0.0179, +0.0001] | -0.0244 [-0.0468, -0.0064] | 0.45 |
| `F_day_fuel` | DAY+FUEL | trees | base | 0.0291 | 0.752 | -0.0133 [-0.0217, -0.0049] | -0.0137 [-0.0240, -0.0052] | -0.0089 [-0.0340, +0.0158] | 0.41 |
| `L_S3_y4` | DAY+WLAG+SOIL+SEAS+STATIC+LHF | trees, label >= 4 ha | reg | 0.0288 | 0.795 | -0.0136 [-0.0226, -0.0064] | -0.0089 [-0.0177, -0.0017] | -0.0278 [-0.0564, -0.0073] | 0.45 |
| `F_day` | DAY | trees | base | 0.0224 | 0.690 | -0.0200 [-0.0304, -0.0093] | -0.0181 [-0.0310, -0.0046] | -0.0232 [-0.0452, -0.0096] | 0.25 |
| `R_core_lh_small_mono` | DAY+WLAG+SOIL+SEAS+LHC | trees | small mono | 0.0209 | 0.744 | -0.0215 [-0.0330, -0.0124] | -0.0187 [-0.0275, -0.0118] | -0.0330 [-0.0723, +0.0007] | 0.35 |
| `B_core` | DAY+WLAG+SOIL+SEAS+LHC | trees | base | 0.0203 | 0.748 | -0.0221 [-0.0328, -0.0138] | -0.0192 [-0.0282, -0.0126] | -0.0338 [-0.0668, -0.0093] | 0.35 |
| `B_core_nolfmc` | DAY+WLAG+SOIL+SEAS | trees | base | 0.0202 | 0.748 | -0.0222 [-0.0314, -0.0144] | -0.0199 [-0.0279, -0.0137] | -0.0302 [-0.0584, +0.0024] | 0.36 |
| `B_core_seed1` | DAY+WLAG+SOIL+SEAS+LHC | trees | base | 0.0201 | 0.747 | -0.0223 [-0.0328, -0.0129] | -0.0194 [-0.0274, -0.0114] | -0.0320 [-0.0673, +0.0042] | 0.36 |
| `F_dayx` | DAYX | trees | base | 0.0191 | 0.687 | -0.0233 [-0.0323, -0.0149] | -0.0242 [-0.0355, -0.0150] | -0.0164 [-0.0377, -0.0022] | 0.25 |
| `F_day_seas` | DAY+SEAS | trees | base | 0.0184 | 0.706 | -0.0240 [-0.0330, -0.0142] | -0.0228 [-0.0336, -0.0128] | -0.0297 [-0.0511, -0.0128] | 0.29 |
