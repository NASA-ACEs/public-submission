# LANDFIRE vegetation-type shares in the large-fire model: the out-of-time test

Run 2026-11-15. Question: do the 16 EVT class shares of the 200 m cell go into the large-fire model as v3.2?
The earlier study (`vegetation-type.md`) found a ROC-AUC gain in the unseen-year protocol and no PR-AUC gain, with
no independent data to settle it. Here the same variant is built on v3.1 and tested on the ignitions of 2025–2026,
which neither model saw. Scripts, models and predictions: `~/spaceapps/ml-work/research/evt_oot/` on the server.

## Rule, written before the numbers

Adopt only if, on 2025–2026, the paired change in ROC-AUC for fires ≥ 400 ha has a 95 % interval above zero **and**
PR-AUC does not fall (point change ≥ 0), **and** PR-AUC is not significantly worse in the unseen-year protocol.
Otherwise do not adopt.

## Verdict: adopted by the rule

| Condition | Result | Met |
|---|---|---|
| 2025–2026, ≥ 400 ha: paired change in ROC-AUC | +0.029 [+0.014, +0.044] | yes |
| 2025–2026, ≥ 400 ha: PR-AUC does not fall | 0.0594 → 0.0640 (+0.0046; interval [−0.044, +0.023]) | yes |
| Unseen year 2016–2024: PR-AUC not significantly worse | 0.0748 → 0.0763 (+0.0015 [−0.0044, +0.0081]) | yes |

What the rule does not require and the data do not show: a significant PR-AUC gain for large fires. The gain is in
ROC-AUC and in the ranking of smaller fires; the top 1 % of ignitions is unchanged in the protocol.

## What was compared

- **v3.1**: `~/spaceapps/ml-work/models/v3.1/full` (38 large-fire inputs).
- **v3.1 + EVT**: the same code (`model-v3.1`), table rows, LFMC inputs, tree settings, seeds and fuel-age form;
  the large-fire inputs get 16 more columns, `evt_share_<class>` (54 inputs), no monotone constraint on them.
  Trained by `variant.py`; the script asserts that the feature lists are those of v3.1 plus the 16 columns.
- **EVT columns**: share of 30 m pixels of each class in the 200 m `ca` cell, from the LANDFIRE release valid on the
  row's date (LF 2016 from 2017-01-01, LF 2022 from 2022-10-01, LF 2025 from 2025-10-01), exactly the columns of
  `research/veg/points_evt.parquet`. Rows of 2016 have none (8 % of ignitions).
- **2025–2026**: 13,296 WFIGS ignitions with a reported size (80 ≥ 400 ha, 239 ≥ 40 ha, 875 ≥ 4 ha). EVT sampled
  from the staged rasters with the same rule: LF 2022 for 7,532 ignitions (to 2025-09-30), LF 2025 for 5,764.
  The sampling routine reproduces the training table's columns on 5,000 table ignitions (72,512 values, largest
  difference 0.0, same missing pattern). Both models predict the same rows; intervals are a paired bootstrap over
  calendar weeks, 1,000 resamples.

## 1. Unseen-year protocol, 2016–2024 (74,568 ignitions, 477 large)

Out-of-year predictions of `ml.train`, fuel age applied; paired.

| Test years | Events | PR-AUC, v3.1 → + EVT (change [95 %]) | ROC-AUC (change [95 %]) | Large fires in top 1 / 5 / 10 % |
|---|---|---|---|---|
| 2016–2024 | 477 | 0.0748 → 0.0763 (+0.0015 [−0.0044, +0.0081]) | 0.8557 → 0.8662 (+0.0105 [+0.0046, +0.0177]) | 17.8 / 43.0 / 57.9 % → 18.2 / 43.8 / 61.4 % |
| 2017–2024 (rows that have EVT) | 431 | 0.0801 → 0.0809 (+0.0008 [−0.0051, +0.0085]) | 0.8623 → 0.8698 (+0.0075 [+0.0032, +0.0131]) | 18.6 / 45.0 / 60.3 % → 19.5 / 45.5 / 62.2 % |
| 2016–2021 | 363 | 0.0676 → 0.0694 (+0.0018 [−0.0052, +0.0096]) | 0.8535 → 0.8633 (+0.0098 [+0.0032, +0.0171]) | 16.0 / 39.9 / 56.5 % → 16.0 / 41.9 / 59.2 % |
| 2022–2024 | 114 | 0.1121 → 0.1080 (−0.0042 [−0.0226, +0.0094]) | 0.8777 → 0.8816 (+0.0039 [−0.0046, +0.0140]) | 24.6 / 52.6 / 64.9 % → 25.4 / 50.0 / 68.4 % |

- Change of the share in the top 10 %, all years: +3.6 points [+0.3, +6.9]; top 1 %: [−2.8, +2.2]; top 5 %:
  [−1.6, +4.1].
- `ml.compare` verdict: fire "no difference" (PR-AUC), LFMC unchanged (same LFMC models).
- From `metrics.json`: PR-AUC before fuel age 0.0721 → 0.0734; single-seed PR-AUC 0.0703 ± 0.0006 → 0.0712 ± 0.0015
  (the spread over seeds more than doubles); refit with five other seeds changes PR-AUC by 0.0019 in both; unseen
  50 km blocks 0.0680 ± 0.0032 → 0.0698 ± 0.0040; Brier 0.006128 → 0.006128; fuel-age β 12.0 in both.

## 2. Years after training, 2025–2026 (13,296 ignitions)

| Sample | Outcome | Events | PR-AUC, v3.1 → + EVT (change [95 %]) | ROC-AUC (change [95 %]) |
|---|---|---|---|---|
| 2025–2026 | ≥ 400 ha | 80 | 0.0594 → 0.0640 (+0.0046 [−0.0435, +0.0226]) | 0.8430 → 0.8718 (+0.0288 [+0.0139, +0.0444]) |
| | ≥ 40 ha | 239 | 0.0689 → 0.0786 (+0.0097 [−0.0091, +0.0177]) | 0.7581 → 0.7852 (+0.0270 [+0.0181, +0.0369]) |
| | ≥ 4 ha | 875 | 0.1237 → 0.1360 (+0.0123 [+0.0062, +0.0174]) | 0.6736 → 0.6949 (+0.0213 [+0.0170, +0.0262]) |
| 2025 (8,335) | ≥ 400 ha | 38 | 0.0892 → 0.0968 (+0.0076 [−0.0985, +0.0396]) | 0.8741 → 0.8927 (+0.0186 [+0.0014, +0.0413]) |
| | ≥ 40 ha | 126 | 0.0815 → 0.0923 (+0.0108 [−0.0208, +0.0246]) | 0.7778 → 0.7946 (+0.0169 [+0.0053, +0.0306]) |
| | ≥ 4 ha | 507 | 0.1222 → 0.1344 (+0.0121 [+0.0029, +0.0210]) | 0.6743 → 0.6954 (+0.0210 [+0.0151, +0.0275]) |
| 2026 (4,961) | ≥ 400 ha | 42 | 0.0504 → 0.0584 (+0.0079 [+0.0026, +0.0202]) | 0.8058 → 0.8465 (+0.0406 [+0.0162, +0.0627]) |
| | ≥ 40 ha | 113 | 0.0635 → 0.0741 (+0.0106 [+0.0062, +0.0161]) | 0.7303 → 0.7693 (+0.0390 [+0.0251, +0.0553]) |
| | ≥ 4 ha | 368 | 0.1284 → 0.1420 (+0.0136 [+0.0074, +0.0198]) | 0.6691 → 0.6909 (+0.0217 [+0.0149, +0.0287]) |

- Large fires (≥ 400 ha) among the top-ranked ignitions, 2025–2026: top 1 % 11.3 → 16.3 %, top 5 % 37.5 → 37.5 %,
  top 10 % 50.0 → 55.0 %, top 20 % 70.0 → 73.8 %.
- Calibration (large fires expected / observed): 2025–2026 92.5 → 88.7 / 80; 2025 56.6 → 54.0 / 38; 2026
  35.9 → 34.7 / 42.
- The ROC-AUC gain has an interval above zero for every outcome and in each year separately. The PR-AUC gain for
  large fires is positive in both years; its interval excludes zero in 2026 only, and over both years it is wide
  on the lower side (a few high-ranked large fires of 2025 decide it).

## What the shares carry

By gain in the five boosters the 16 columns hold 22 % of the model; `evt_share_developed` alone holds 12 % and is
the single most important input, ahead of slope (11 %). Next: grass 1.8 %, mesic conifer 1.4 %, each other class
under 1 %. So most of what is added is how built-up the 200 m cell is, i.e. where an ignition sits against roads
and settlement, not which vegetation grows there. A variant with that one column was not run here.

## What v3.2 needs in the repo

- **Columns** (`ml.data`, `full` large-fire inputs only; no monotone constraint; LFMC lists unchanged, the earlier
  study found no LFMC gain): `evt_share_chaparral`, `evt_share_coastal_scrub`, `evt_share_sagebrush`,
  `evt_share_desert_scrub`, `evt_share_other_shrub`, `evt_share_oak_woodland`, `evt_share_other_hardwood`,
  `evt_share_conifer_oak`, `evt_share_dry_conifer`, `evt_share_mesic_conifer`, `evt_share_juniper_pinyon`,
  `evt_share_grass`, `evt_share_riparian`, `evt_share_sparse`, `evt_share_agriculture`, `evt_share_developed`.
- **Source on `ca`**: `~/spaceapps/lanes/ML/staged/landfire-evt/ca/landfire-evt__ca__<date>__lf2016|lf2022|lf2025/
  evt_share_<class>.tif` (float32, 0–1, nodata −9999, the `ca` grid). They are **not registered in the fuel-lake
  catalog**: `LakeClient.search("landfire-evt")` needs a dataset config (`config/datasets/landfire-evt.yaml` in
  data-aggregation) and STAC items in a lake root, as was done for the other LANDFIRE layers
  (`research/ca_statics/register.py`). Until then they can only be read by path.
- **Table**: the cell value from the release valid on the row's date, by the rule `ml.data.LANDFIRE_FROM` already
  holds; it fits `ml.series.statics_at_points` once the collection is searchable (add it to `data.STATIC_ASSETS`).
  No window averaging on `ca`: the rasters are already cell shares. Rows of 2016 stay empty.
- **Map on `ca`**: the same rasters through `ml.grid.statics`, release valid on the map day (LF 2025 for
  2026-10-20).
- **Map on `smm`**: there is no EVT raster. Needed: EVT at 30 m on the AOI grid for the release valid on the day
  (services `LF<year>_EVT_CONUS`; download as in `research/veg/evt.py`), the code-to-class table
  (`research/veg/classes.py` with LANDFIRE's attribute CSVs `evt_2016.csv`, `evt_2022.csv`, `evt_2025.csv`), one
  0/1 layer per class, then the mean over the 200 m window as `ml.grid.to_cells` does for the other static inputs.
- **Models**: retrain `full` on the table with the 16 columns; nothing else changes. `core` is unaffected.

## Limits

- One comparison on one out-of-time sample of 80 large fires from a different source than the training labels;
  40 % of WFIGS ignitions have no size and are left out.
- The rule was written for ROC-AUC because that is where the earlier study saw the effect; by the repo's own
  headline rule (PR-AUC, interval excluding zero) this is still "no difference" for large fires.
- In the protocol years 2022–2024 PR-AUC is lower with EVT (−0.004, interval includes zero), as the earlier study
  saw on v2.
- Seed spread of PR-AUC rises from 0.0006 to 0.0015.
- EVT releases differ in their class mapping only through LANDFIRE's own tables; LF 2023 and LF 2024 exist and
  were not used (the rule follows the fuel layers' releases).

## Files

`points_v31_evt.parquet` (training table), `variant.py`, `train.sh`, `train.log`, `models/full_evt/` (models,
`metrics.json`, out-of-year predictions), `compare_v31_evt.json` (`ml.compare`), `protocol.py`, `protocol.json`,
`oot_evt.py`, `ignitions_2025_2026_evt.parquet`, `evaluate_oot.py`, `oot.json`, `pred_oot.parquet`,
`gain_share.json`, `evt_cols.json`.
