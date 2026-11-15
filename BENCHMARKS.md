# Benchmarks and scientific results

All numbers are on data the models did not see. 95 % intervals by bootstrap over calendar weeks. Baseline: the gridMET burning index (BI) at the ignition. Full studies, protocols and per-version metrics: [`machine-learning/docs/research/`](machine-learning/docs/research/), [`machine-learning/docs/versions/`](machine-learning/docs/versions/) (JSON per version), [`machine-learning/docs/EXPERIMENTS.md`](machine-learning/docs/EXPERIMENTS.md), [`machine-learning/docs/MODEL.md`](machine-learning/docs/MODEL.md).

## Large-fire model (P(final size ≥ 400 ha | ignition), model v3.3)

| Test | Model | BI | Random |
|---|---|---|---|
| Unseen year 2016–2024 (74,568 ignitions, 477 large): PR-AUC | 0.082 | 0.014 | 0.0064 |
| Unseen year: ROC-AUC | 0.885 | 0.591 | 0.5 |
| 2025–2026, WFIGS (13,296 ignitions, 80 large): ROC-AUC | 0.90 [0.88, 0.93] | 0.63 | 0.5 |
| 2025–2026: PR-AUC | 0.080 | 0.012 | 0.006 |
| 2025–2026: large fires in the top 14.5 % of ignitions | 64 / 80 | 26 / 80 | 12 / 80 |
| 2025–2026: large fires in the top 10 % | 68 % | | 10 % |
| 2025–2026: large fires expected / observed | 87 / 80 | | |
| Power-line ignitions 2016–2024 (761, 12 large): ROC-AUC | 0.954 [0.922, 0.982] | 0.813 | 0.5 |
| Power-line ignitions: large fires in classes 4–5 | 12 / 12 | | |

## What each input adds (paired differences)

| Added | Gain |
|---|---|
| Weather of the day alone | PR-AUC 0.021 (BI 0.014) |
| Terrain and fuel model, 200 m cell means | PR-AUC +0.043 [0.024, 0.060] |
| Small trees, monotone responses, 5 seeds | PR-AUC +0.014 [0.002, 0.031] |
| HRRR gusts | ROC-AUC +0.019 [0.012, 0.028]; 2025–2026 +0.031 [0.011, 0.055] |
| Vegetation type (LANDFIRE EVT) | 2025–2026 ROC-AUC +0.028 [0.011, 0.047] |
| Surroundings within 1 and 3 km | PR-AUC +0.011 [0.0002, 0.021]; ROC-AUC +0.017 [0.011, 0.025] |
| SMAP dryness anomaly | odds ratio 0.51 [0.32, 0.79] per unit; ranking gain not significant |
| Fuel age | no ignition in a scar younger than 2 years became a large fire (632 ignitions, 12.6 expected) |

## NISAR

| Test | Result | Comparison |
|---|---|---|
| GCOV L-HV: burned vs unburned, 2018 Woolsey and Hill fires, 7.5 years later (AUC) | 0.89 [0.87, 0.92] | Sentinel-1 C-VH 0.81, optical NDVI 0.65 [0.62, 0.70], LANDFIRE 2025 fuel load 0.58 |
| GCOV L-HV backscatter deficit of regrowth | −3.1 dB at 1.4 years, −2.0 dB at 7.5 years | |
| SME2 anomaly vs own record, 2026 fire season, fires ≥ 4 ha (213), test fixed in advance (AUC) | 0.550 [0.520, 0.576] | SMAP root zone 0.447 [0.417, 0.487] |
| SME2 part finer than 9 km added to SMAP (AUC change) | +0.027 [−0.012, 0.059] | did not pass |
| SME2 anomaly odds ratio beyond weather and SMAP | 0.89 [0.82, 0.98] | |

## Physics and live fuel moisture

| Test | Result | Comparison |
|---|---|---|
| Spread direction within 45° of the real perimeter (649 fires) | 45.6 % | wind alone 42.1 %, chance 25 % |
| Spread shape IoU | 0.187 | circle 0.155 |
| Flame length alone, large-fire ranking (PR-AUC) | 0.033 | BI 0.014 |
| Live fuel moisture RMSE, unseen sites and years (15,591 samples) | about 25.5 percentage points | |

## Limits

The years 2025–2026 informed two modelling decisions. The power-line result rests on 12 events. NISAR data are provisional and one season long; scaling fuel in the spread physics by L-band power is an assumption no fire has tested yet. The burning index is a public baseline, not an operator's tool.
