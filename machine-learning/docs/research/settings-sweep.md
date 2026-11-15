# E4: settings of the large-fire model on the features of v3.3

2026-11-15. The settings of the large-fire model (7 leaves, at least 200 ignitions per leaf, learning rate 0.03,
300 trees, 80 % of the columns per tree, 5 seeds, monotone constraints on weather and dryness) were chosen in a
study of 269 variants on 38 features. v3.3 has 71, of which 33 are new and unconstrained. Question: does a short
list of settings, fixed before the run, do better on the features of v3.3?

**Verdict by the rule written before the run: no variant is accepted. The settings of v3.3.1 stay.**

Rule: accept a variant only if, in the unseen-year protocol, the paired change in PR-AUC against v3.3.1 (same
rows, bootstrap over calendar weeks, 500 draws) has a 95 % interval above zero **and** the point change exceeds
0.005 (twice the seed noise of 0.0025). The years 2025–2026 do not decide (they have decided two changes
already); they are reported for the best variant that did not pass, as information.

## Unseen year 2016–2024 (74,568 ignitions, 477 large), with fuel age, against v3.3.1

| Variant | Change (reference value) | PR-AUC | Paired change [95 %] | ROC-AUC (paired change [95 %]) | Large fires in top 1 / 5 / 10 % | 2017–2024 (431 large): PR-AUC change | 2022–2024 (114 large): PR-AUC change | Accepted |
|---|---|---|---|---|---|---|---|---|
| v3.3.1 (reference) | 7 leaves, min 200 per leaf, lr 0.03, 300 trees, colsample 0.8, 5 seeds | 0.0824 | | 0.8845 | 19.1 / 47.4 / 63.7 | (0.0956) | (0.1050) | |
| `leaves15` | 15 leaves (7) | 0.0819 | -0.0004 [-0.0044, +0.0038] | 0.8858 (+0.0013 [-0.0011, +0.0037]) | 18.7 / 47.6 / 63.3 | +0.0020 [-0.0043, +0.0081] | -0.0071 [-0.0184, +0.0039] | no |
| `child100` | min 100 ignitions per leaf (200) | 0.0838 | +0.0014 [-0.0025, +0.0060] | 0.8832 (-0.0013 [-0.0026, -0.0001]) | 19.7 / 47.6 / 62.9 | +0.0013 [-0.0034, +0.0076] | +0.0018 [-0.0065, +0.0114] | no |
| `child400` | min 400 ignitions per leaf (200) | 0.0864 | +0.0040 [+0.0004, +0.0088] | 0.8873 (+0.0028 [+0.0012, +0.0040]) | 18.9 / 47.8 / 64.2 | +0.0027 [-0.0011, +0.0073] | +0.0078 [-0.0021, +0.0193] | no |
| `slow` | learning rate 0.015, 600 trees (0.03, 300) | 0.0834 | +0.0010 [-0.0012, +0.0037] | 0.8854 (+0.0008 [-0.0003, +0.0019]) | 20.1 / 47.0 / 63.9 | +0.0010 [-0.0019, +0.0042] | +0.0032 [-0.0017, +0.0085] | no |
| `colsample` | colsample_bytree 0.5 (0.8) | 0.0862 | +0.0038 [-0.0002, +0.0087] | 0.8852 (+0.0007 [-0.0007, +0.0018]) | 19.3 / 48.4 / 63.1 | +0.0053 [-0.0005, +0.0104] | +0.0080 [-0.0011, +0.0188] | no |
| `mono` | 9 monotone constraints on new inputs | 0.0848 | +0.0024 [-0.0009, +0.0067] | 0.8855 (+0.0010 [-0.0008, +0.0027]) | 20.1 / 47.2 / 64.2 | +0.0028 [-0.0019, +0.0081] | +0.0037 [-0.0043, +0.0118] | no |
| `seeds10` | 10 seeds (5) | 0.0837 | +0.0013 [-0.0005, +0.0032] | 0.8848 (+0.0003 [-0.0003, +0.0008]) | 20.1 / 46.5 / 63.5 | +0.0011 [-0.0019, +0.0034] | -0.0007 [-0.0080, +0.0059] | no |

- `child400` (larger leaves) is the only variant whose interval excludes zero, and its point gain of 0.0040 is
  below the threshold of 0.005. Its ROC-AUC gain (+0.0028) is also above zero.
- `colsample` 0.5 gains about as much (+0.0038) with an interval that touches zero.
- More leaves, smaller leaves, slower learning, ten seeds and the nine constraints on new inputs change PR-AUC by
  −0.0004 to +0.0024, all within their intervals. `child100` lowers ROC-AUC slightly (−0.0013 [−0.0026, −0.0001]).
- The direction is consistent: the variants that regularise more (`child400`, `colsample`, `mono`) are the ones
  with positive point changes; the ones that add capacity (`leaves15`, `child100`) are not.

## 2025–2026, information only: `child400` against v3.3 (13,296 ignitions)

| Outcome | Events | PR-AUC | ROC-AUC |
|---|---|---|---|
| ≥ 400 ha | 80 | 0.0799 → 0.0794 (-0.0005 [-0.0069, +0.0039]) | 0.9013 → 0.9003 (-0.0010 [-0.0048, +0.0024]) |
| ≥ 40 ha | 239 | 0.0978 → 0.0945 (-0.0033 [-0.0072, -0.0006]) | 0.8154 → 0.8140 (-0.0014 [-0.0049, +0.0023]) |
| ≥ 4 ha | 875 | 0.1467 → 0.1456 (-0.0011 [-0.0037, +0.0005]) | 0.7124 → 0.7125 (+0.0001 [-0.0019, +0.0019]) |

Large fires in the top 1 / 5 / 10 / 20 % of ignitions: 18.8 / 42.5 / 67.5 / 83.8 % → 17.5 / 42.5 / 66.3 / 85.0 %.
Expected large fires: 86.9 → 86.6 (80 observed). The best variant of the unseen-year protocol does not gain out
of time; for fires ≥ 40 ha its PR-AUC is lower with an interval below zero. This agrees with not accepting it.

## What was run

- Code: worktree `~/spaceapps/ml-work/wt/e4` (tag `model-v3.3.1`), with the settings read from the environment
  (`E4_LEAVES`, `E4_CHILD`, `E4_LR`, `E4_TREES`, `E4_COLSAMPLE`, `E4_SEEDS`, `E4_MONO`) and `E4_FAST=1` skipping
  what the comparison does not need (LFMC report and models, ablation variants, seed-noise refit, block folds,
  random split). Nothing in the repository was changed.
- Check of the reduced run: with no setting changed it reproduces v3.3.1 exactly (`oof_fire.parquet` identical,
  `fire_0.txt` identical, PR-AUC 0.0824).
- `mono`: risk may not rise with `evt_share_developed`, `evt_share_agriculture` and their means within 1 and 3 km;
  may not fall with `burnable_r1km`, `burnable_r3km`, `elevation_sd_r1km`.
- Table `~/spaceapps/ml-work/tables/points_v33.parquet`; each training about 1.5 minutes, two at a time.
- No combination of variants was run: none passed alone.

## If the settings are revisited

`child400` and `colsample` 0.5 together are the natural candidate (both regularise, both gain about 0.004 in
unseen years). It would be a new variant chosen after seeing these results, on years that have been used many
times; it needs data the models have not seen to be judged. The change would be one line in `ml/train.py`:
`CLF = dict(n_estimators=300, learning_rate=0.03, num_leaves=7, min_child_samples=400, ...)` and
`colsample_bytree=0.5` for the classifier only.

## Files (`~/spaceapps/ml-work/research/e4/`)

`RESULTS.md`, `evaluate.py`, `variants.json` (every figure above), `models/<variant>/full/` (`oof_fire.parquet`,
`metrics.json` with `settings`, `fire_*.txt`, `meta.json`), `models/base/full/` (the reproduction check),
`oot_child400.json`, `oot_paired_child400_vs_v3.3.json`, `research/oot/pred_child400_full.parquet`.
