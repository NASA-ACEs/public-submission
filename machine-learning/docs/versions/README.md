# Model versions

Each version is a git tag (`model-v…`), its metrics (`metrics_<set>.json`, the output of `ml.train`) and the
regression test against the version before (`compare_*.json`, the output of `ml.compare`). Models and bundles are
on the server in `~/spaceapps/ml-work/models/` and `outputs/`, not in git. Table for all:
`point_features_v1b_noS2_canopy.parquet` (74,568 ignitions, 477 large; 15,591 LFMC site-dates), for v1 with the
features of `ml.series` added.

Headline figures (data the models did not see; PR-AUC of a random ranking is 0.0064):

| Version | Set | LFMC RMSE, new place and year | LFMC RMSE, FEMS 2024 | Fire PR-AUC, new year | ROC-AUC | Large fires in top 10 % | Gain over burning index [95 %] |
|---|---|---|---|---|---|---|---|
| v0 (`main` at `0dded3e`) | core | 27.02 | 29.83 | 0.0203 | 0.748 | 35 % | +0.006 [−0.005, 0.015] |
| v0 | full | 27.09 | 25.86 | 0.0466 (one seed; 0.0424 as a 5-seed mean) | 0.820 | 51 % | +0.032 [0.016, 0.050] |
| v1 | core | 26.66 | 29.63 | 0.0213 | 0.713 | 30 % | +0.007 [−0.003, 0.017] |
| v1 | full | 26.50 | 25.77 | 0.0574 (0.0548 before fuel age) | 0.831 | 56 % | +0.041 [0.021, 0.069] |
| v2 | full | 25.51 | 25.55 | 0.0675 (0.0636 before fuel age) | 0.835 | 52 % (17 % in the top 1 %) | +0.049 [0.026, 0.073] |
| v3 | full | 25.51 | 25.55 | 0.0751 (0.0721 before fuel age) | 0.856 | 57 % | +0.058 [0.035, 0.084] |
| v3.1, corrected protocol | full | 25.51 | 25.55 | 0.0703 (0.0672 before fuel age) | 0.861 | 60 % | +0.053 [0.032, 0.079] |
| v3.2 | full | 25.51 | 25.55 | 0.0712 (0.0681 before fuel age) | 0.867 | 61 % | +0.054 [0.032, 0.083] |
| **v3.3** | **full** | **25.51** | **25.55** | **0.0824** (0.0790 before fuel age) | **0.885** | **64 %** | **+0.065 [0.042, 0.090]** |

From v3.2 the 2016 fold, which has no fuel layers, is scored by models fitted without them ([v3.2](v3.2.md)); the
rows above "corrected protocol" use the earlier scoring and are comparable with each other, not with v3.2.

- [v1](v1.md): the model of `docs/MODEL.md`; v1.1 drops the GCOV gating of fuel age.
- [v2](v2.md): static inputs as 200 m cell means; statewide `full` map. v2.1: fuel age fades in 2 years after
  the test on 2025–2026 (PR-AUC 0.048 against 0.012 for the burning index, ROC-AUC 0.805, 93 large fires expected,
  80 observed).
- [v3](v3.md): HRRR gusts and maximum wind in the large-fire model. ROC-AUC +0.019 [0.012, 0.028] in unseen years
  and +0.031 [0.011, 0.055] on 2025–2026; PR-AUC gains positive, intervals include zero.
- [v3.1](v3.1.md): fuel age by vegetation (young scars everywhere, former forest for 7 years); final result of
  the pre-registered NISAR test; experimental SME2-anomaly layers. Same trees as v3.
- [v3.2](v3.2.md): vegetation type of the cell (16 LANDFIRE EVT shares; the built-up share carries most of it),
  accepted by a rule written before the test on 2025–2026: ROC-AUC +0.028 [0.011, 0.047] for fires ≥ 400 ha,
  PR-AUC +0.013 [0.007, 0.018] for fires ≥ 4 ha. Corrected scoring of the 2016 fold. v3.2.1: `risk_index` ranks
  against ignitions on open land, so the classes spread over the map (same probabilities).
- [v3.3](v3.3.md): the surroundings of the cell within 1 and 3 km (buildings, fuel continuity, relief), by a rule
  written before the run. The first version the regression test calls "better": PR-AUC +0.011 [0.0002, 0.021] in
  unseen years; on 2025–2026 ROC-AUC +0.030 [0.011, 0.049] and 68 % of the large fires in the top 10 %. v3.3.1:
  the map separates what the land contributes (`susceptibility_index`) from what the day's weather does
  (`weather_factor`); same probabilities. v3.3.2: fixes from an independent code review (140 coastal cells of
  the Santa Monica map); same models.

Years after training (2025–2026, 13,296 ignitions, 80 large; `docs/research/out-of-time.md`):

| Version | PR-AUC | ROC-AUC | Large fires in top 10 % | Expected / observed large fires |
|---|---|---|---|---|
| burning index | 0.012 | 0.628 | | |
| v2 `core` (no terrain and fuel) | 0.015 | 0.707 | 25 % | 80 / 80 |
| v2.1 | 0.048 | 0.805 | 45 % | 93 / 80 |
| v3 | 0.058 | 0.836 | 49 % | 91 / 80 |
| v3.1 | 0.059 | 0.843 | 50 % | 93 / 80 |
| v3.2 | 0.067 | 0.871 | 54 % | 86 / 80 |
| **v3.3** | **0.080** | **0.901** | **68 %** | **87 / 80** |
