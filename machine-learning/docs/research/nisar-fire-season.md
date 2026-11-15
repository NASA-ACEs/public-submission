# NISAR SME2 and the 2026 fire season (experiment N2)

**Final result** for the protocol's window (2026-06-01 to 2026-10-20); run 2026-11-15 03:37.

The protocol was written on 2026-11-15 00:30, before any scene of June–October 2026 was staged, and amended at 01:15
before any was analysed (`docs/EXPERIMENTS.md`, N2). This file is generated: `~/spaceapps/ml-work/research/n2/run.sh`
reads every staged scene again and rewrites it.

## Protocol in short

- **Sample.** WFIGS ignitions in California, 2026-06-01 to 2026-10-20, with a reported final size and an SME2 scene at
  the ignition pixel within 12 days before discovery.
- **Primary predictor.** The newest scene minus the mean of the earlier scenes of the same track at that pixel (at
  least 6), divided by their standard deviation. Lower = drier than the pixel's own record.
- **Outcome.** Final size ≥ 4 ha (primary); ≥ 40 ha and ≥ 400 ha with counts, no claim below 30 events.
- **Tests.** (1) AUC of the predictor alone, next to SMAP's 12-month anomalies at the same ignitions; (2) its odds
  ratio with weather and day of season controlled; (3) resolution: the part finer than 9 km added to the SMAP anomaly;
  (4) burned against unburned: pre-fire anomaly inside 2026 perimeters ≥ 40 ha against a ring 0.4–2 km outside;
  (5) the term the map uses (`ml.grid.nisar_fine`); (6) fuel age on 2026 ignitions.
- **Decision.** A claim of skill for the NISAR term only if (1) excludes 0.5 and (3) or (4) is positive with an
  interval excluding zero.

AUC is oriented so that above 0.5 means drier pixels gave larger fires. Intervals are 95 %: bootstrap over calendar
weeks for ignitions (1,000 resamples), over fires for perimeters.

## Deviations and clarifications

1. None in the sample window.
2. Soil-moisture values on the retrieval's bounds (0.02 and 0.60 m³/m³) are dropped from scenes and from their
   history: study N1 found they are not retrievals. Fixed before outcomes were looked at.
3. "The newest scene" is the newest pass within 12 days (pass day on or before the discovery day) whose track has at
   least 6 earlier valid scenes at the pixel. If two tracks qualify, the newer pass is used.
4. Z-scores per cell are limited to ±5 before they are averaged or regressed on (a pixel whose history barely varies
   would otherwise dominate).
5. Test 3: the fine part is the pixel's z-score minus the mean z-score of the same pass over the 45 × 45 cells
   (9 km) around it, where at least a quarter of them are valid. The decision uses the SMAP root-zone anomaly
   (`rz_z12`, the one the model's evidence rests on); the surface anomaly is reported next to it.
6. Test 4: for each perimeter the newest pass within 12 days before the start with at least 20 valid cells inside
   and 20 in the ring. The ring is built in California Albers (the grid's CRS is not conformal). Cells of other
   fires' perimeters inside a ring are not removed. "Positive" = mean of (inside − ring) below zero, interval
   excluding zero.
7. Test 2: standard errors are clustered on calendar weeks (sandwich estimator written out; `statsmodels` is not
   installed).
8. Test 5: the term is computed from 45 × 45-cell windows around each ignition instead of on the whole grid for
   every day. The arithmetic is that of `ml.grid.track_fine`; the last section compares the two.
9. Test 6: the amendment says "beta fitted on 2016–2024 only". That is v2's modifier (fade 3 years). v2.1's
   (fade 2 years) was chosen with the 2025–2026 ignitions in view and is shown next to it, not as a test.

## Data

- Scenes: 492 items on 10 descending tracks, 2025-10-29 to 2026-10-20.
- Ignitions with a reported size since 2025-11-01 on the grid: 5,317. In the sample window
  (2026-06-01 to 2026-10-20): 3,040; with the primary predictor: **2,113**
  (213 of ≥ 4 ha, 61 of ≥ 40 ha, 23 of ≥ 400 ha). Median age of the scene
  4.0 days; 50 % of the pixels are not flagged "recommended".
- Model for tests 5 and 6: v2.1 `full` (`v2.1/full`), trained on 2016–2024.

## Test 1: the anomaly alone

| Outcome | Events | SME2 anomaly | SMAP root zone, 12 months | SMAP surface, 12 months | Model v2.1 | SME2 − SMAP root zone |
|---|---|---|---|---|---|---|
| ≥ 4 ha | 213 | 0.550 [0.520, 0.576] | 0.447 [0.417, 0.487] | 0.463 [0.427, 0.501] | 0.579 [0.541, 0.620] | [0.049, 0.145] |
| ≥ 40 ha | 61 | 0.545 [0.465, 0.619] | 0.448 [0.388, 0.522] | 0.497 [0.436, 0.562] | 0.610 [0.548, 0.690] | [-0.022, 0.201] |
| ≥ 400 ha (no claim: under 30 events) | 23 | 0.529 [0.422, 0.629] | 0.393 [0.299, 0.504] | 0.432 [0.344, 0.543] | 0.632 [0.482, 0.805] | [-0.054, 0.293] |

Share of ignitions that grew to ≥ 4 ha by tercile of the anomaly, dry to wet: 11.1 % (78 of 705),
11.5 % (81 of 704), 7.7 % (54 of 704).

## Test 2: odds ratio with weather controlled (≥ 4 ha)

Logistic regression; odds ratio per unit of the z-score (below 1 = wetter pixels grow less often).

| Model | Odds ratio | 95 % interval | Per standard deviation |
|---|---|---|---|
| SME2 anomaly alone | 0.864 | [0.793, 0.941] | 0.850 |
| SME2 anomaly + VPD, ERC, 100-h fuel moisture, wind, day of year | 0.886 | [0.812, 0.968] | 0.875 |
| the same + SMAP root-zone anomaly | 0.892 | [0.816, 0.976] | 0.881 |
| for comparison: SMAP root-zone anomaly + the same weather | 0.675 | [0.400, 1.138] | 0.873 |

## Test 3: resolution (what SMAP cannot see)

2,107 ignitions, 213 of ≥ 4 ha. The fine part alone: AUC 0.496 [0.467, 0.522].

| SMAP anomaly | AUC, SMAP alone | AUC, SMAP + SME2 fine part | Change | 95 % interval |
|---|---|---|---|---|
| root zone (decision) | 0.446 | 0.473 | +0.0272 | [-0.012, 0.059] |
| surface | 0.463 | 0.493 | +0.0304 | [0.002, 0.058] |

## Test 4: burned against unburned

75 perimeters ≥ 40 ha started in the window; 45 have a pass within 12 days before the start with enough valid cells inside and in the ring (20 of them ≥ 400 ha).

| | Value |
|---|---|
| Fires where the burned area was drier than the ring / wetter | 21 / 24 |
| Sign test, two-sided p | 0.77 |
| Mean of (inside − ring), z-score | +0.028 [-0.049, 0.104] |
| Median of (inside − ring), z-score | +0.009 [-0.022, 0.078] |
| Mean of (inside − ring), m³/m³ | +0.0016 [-0.0004, 0.0037] |
| Fires ≥ 400 ha only (20): drier / mean difference | 10 / -0.016 [-0.098, 0.061] |

## Test 5: the term the map uses

2,089 ignitions have the term (mean size 0.0058 m³/m³, 0.13 z-score units as fed to the model; 51 % in cells not flagged "recommended"). It changes `p_large_fire` by 5.7 % of its mean on average.

| Outcome | Events | Term alone, AUC | Model: AUC without → with | Change in AUC | PR-AUC without → with | Change in PR-AUC | Added to SMAP root zone: change in AUC | Added to SMAP surface |
|---|---|---|---|---|---|---|---|---|
| ≥ 4 ha | 212 | 0.516 [0.475, 0.567] | 0.5791 → 0.5829 | +0.0038 [0.0005, 0.0083] | 0.1425 → 0.1430 | +0.0005 [-0.0031, 0.0041] | +0.0256 [0.011, 0.042] | +0.0281 [0.010, 0.050] |
| ≥ 40 ha | 60 | 0.478 [0.387, 0.568] | 0.6080 → 0.6071 | -0.0008 [-0.0052, 0.0046] | 0.0752 → 0.0762 | +0.0010 [-0.0070, 0.0078] | +0.0129 [-0.033, 0.052] | -0.0047 [-0.057, 0.045] |
| ≥ 400 ha (no claim) | 23 | 0.509 [0.399, 0.624] | 0.6328 → 0.6340 | +0.0012 [-0.0050, 0.0121] | 0.0655 → 0.0662 | +0.0007 [-0.0014, 0.0050] | +0.0243 [-0.016, 0.078] | +0.0172 [-0.030, 0.069] |

## Test 6: fuel age in 2026

Large fires observed against the sum of the model's probabilities, for ignitions inside a perimeter of the last
10 years.

2026-06-01 to the end of the sample: 3,040 ignitions, 31 large fires.

| Years since fire | Ignitions | Large fires | Expected, no modifier | Expected, v2 (fade 3 y, β from 2016–2024) | Expected, v2.1 (fade 2 y) |
|---|---|---|---|---|---|
| 0-2 | 18 | 0 | 0.21 | 0.02 | 0.01 |
| 2-3.5 | 8 | 0 | 0.08 | 0.01 | 0.01 |
| 3.5-5 | 50 | 2 | 0.42 | 0.18 | 0.23 |
| 5-7 | 81 | 2 | 0.93 | 0.54 | 0.68 |
| 7-10 | 93 | 3 | 1.09 | 0.85 | 0.99 |
| all scars | 250 | 7 | 2.71 | 1.60 | 1.92 |

all 2026 ignitions with a size: 4,961 ignitions, 42 large fires.

| Years since fire | Ignitions | Large fires | Expected, no modifier | Expected, v2 (fade 3 y, β from 2016–2024) | Expected, v2.1 (fade 2 y) |
|---|---|---|---|---|---|
| 0-2 | 25 | 0 | 0.23 | 0.02 | 0.01 |
| 2-3.5 | 11 | 0 | 0.09 | 0.01 | 0.01 |
| 3.5-5 | 73 | 2 | 0.45 | 0.20 | 0.25 |
| 5-7 | 125 | 3 | 1.04 | 0.60 | 0.76 |
| 7-10 | 142 | 3 | 1.22 | 0.96 | 1.11 |
| all scars | 376 | 8 | 3.04 | 1.79 | 2.15 |

## Decision by the protocol

| Condition | Met |
|---|---|
| (1) AUC of the anomaly for ≥ 4 ha excludes 0.5 | yes |
| (3) fine part added to the SMAP root-zone anomaly: gain with an interval above zero | no |
| (4) burned area drier than its ring, interval excluding zero | no |
| **Claim of skill for the NISAR term** | **no** |

By the rule fixed in advance the NISAR term stays on the map as an **experimental layer**, described as such: no claim that it improves fire prediction.

## Outside the protocol (exploratory)

Not part of the decision. The same tests 1, 3 and 5 on other samples; with several samples and outcomes some
intervals will exclude zero by chance.

### All ignitions since 2025-11-01

3,155 ignitions with the predictor; events {'ge4': 280, 'ge40': 86, 'ge400': 34}.

Test 1:

| Outcome | Events | SME2 anomaly | SMAP root zone, 12 months | SMAP surface, 12 months | Model v2.1 | SME2 − SMAP root zone |
|---|---|---|---|---|---|---|
| ≥ 4 ha | 280 | 0.555 [0.531, 0.579] | 0.540 [0.497, 0.597] | 0.548 [0.505, 0.600] | 0.632 [0.593, 0.673] | [-0.045, 0.067] |
| ≥ 40 ha | 86 | 0.549 [0.490, 0.609] | 0.526 [0.464, 0.591] | 0.551 [0.491, 0.611] | 0.659 [0.605, 0.714] | [-0.072, 0.111] |
| ≥ 400 ha | 34 | 0.546 [0.458, 0.625] | 0.492 [0.409, 0.579] | 0.513 [0.435, 0.592] | 0.693 [0.590, 0.790] | [-0.081, 0.182] |

Test 3: 3,143 ignitions, 280 of ≥ 4 ha. The fine part alone: AUC 0.501 [0.478, 0.524].

| SMAP anomaly | AUC, SMAP alone | AUC, SMAP + SME2 fine part | Change | 95 % interval |
|---|---|---|---|---|
| root zone (decision) | 0.539 | 0.543 | +0.0035 | [-0.023, 0.026] |
| surface | 0.548 | 0.550 | +0.0024 | [-0.023, 0.025] |

Test 5: 3,112 ignitions have the term (mean size 0.0056 m³/m³, 0.13 z-score units as fed to the model; 49 % in cells not flagged "recommended"). It changes `p_large_fire` by 5.1 % of its mean on average.

| Outcome | Events | Term alone, AUC | Model: AUC without → with | Change in AUC | PR-AUC without → with | Change in PR-AUC | Added to SMAP root zone: change in AUC | Added to SMAP surface |
|---|---|---|---|---|---|---|---|---|
| ≥ 4 ha | 277 | 0.520 [0.486, 0.561] | 0.6318 → 0.6340 | +0.0022 [-0.0001, 0.0051] | 0.1392 → 0.1396 | +0.0004 [-0.0025, 0.0033] | +0.0135 [0.003, 0.025] | +0.0156 [0.004, 0.030] |
| ≥ 40 ha | 83 | 0.495 [0.424, 0.572] | 0.6566 → 0.6558 | -0.0008 [-0.0038, 0.0035] | 0.0678 → 0.0684 | +0.0006 [-0.0050, 0.0066] | +0.0105 [-0.011, 0.031] | +0.0053 [-0.021, 0.033] |
| ≥ 400 ha | 34 | 0.508 [0.414, 0.610] | 0.6938 → 0.6934 | -0.0004 [-0.0040, 0.0051] | 0.0525 → 0.0529 | +0.0004 [-0.0012, 0.0031] | +0.0136 [-0.008, 0.043] | +0.0132 [-0.011, 0.043] |

### Protocol sample, pixels flagged "recommended"

1,047 ignitions with the predictor; events {'ge4': 156, 'ge40': 40, 'ge400': 15}.

Test 1:

| Outcome | Events | SME2 anomaly | SMAP root zone, 12 months | SMAP surface, 12 months | Model v2.1 | SME2 − SMAP root zone |
|---|---|---|---|---|---|---|
| ≥ 4 ha | 156 | 0.548 [0.519, 0.577] | 0.459 [0.425, 0.500] | 0.498 [0.463, 0.540] | 0.545 [0.496, 0.596] | [0.035, 0.135] |
| ≥ 40 ha | 40 | 0.541 [0.456, 0.621] | 0.452 [0.367, 0.544] | 0.523 [0.452, 0.610] | 0.582 [0.514, 0.681] | [-0.053, 0.221] |
| ≥ 400 ha (no claim: under 30 events) | 15 | 0.584 [0.496, 0.681] | 0.355 [0.221, 0.495] | 0.471 [0.337, 0.628] | 0.624 [0.436, 0.906] | [0.053, 0.405] |

Test 3: 1,044 ignitions, 156 of ≥ 4 ha. The fine part alone: AUC 0.485 [0.448, 0.525].

| SMAP anomaly | AUC, SMAP alone | AUC, SMAP + SME2 fine part | Change | 95 % interval |
|---|---|---|---|---|
| root zone (decision) | 0.458 | 0.466 | +0.0086 | [-0.036, 0.052] |
| surface | 0.498 | 0.501 | +0.0040 | [-0.036, 0.039] |

Test 5: 1,030 ignitions have the term (mean size 0.0060 m³/m³, 0.18 z-score units as fed to the model; 1 % in cells not flagged "recommended"). It changes `p_large_fire` by 7.6 % of its mean on average.

| Outcome | Events | Term alone, AUC | Model: AUC without → with | Change in AUC | PR-AUC without → with | Change in PR-AUC | Added to SMAP root zone: change in AUC | Added to SMAP surface |
|---|---|---|---|---|---|---|---|---|
| ≥ 4 ha | 155 | 0.521 [0.471, 0.573] | 0.5451 → 0.5467 | +0.0016 [-0.0044, 0.0080] | 0.1701 → 0.1708 | +0.0007 [-0.0031, 0.0051] | +0.0277 [0.006, 0.053] | +0.0225 [-0.006, 0.053] |
| ≥ 40 ha | 39 | 0.505 [0.411, 0.597] | 0.5780 → 0.5733 | -0.0048 [-0.0138, 0.0047] | 0.0609 → 0.0619 | +0.0010 [-0.0061, 0.0114] | +0.0301 [-0.031, 0.077] | +0.0008 [-0.064, 0.068] |
| ≥ 400 ha (no claim) | 15 | 0.494 [0.360, 0.643] | 0.6251 → 0.6198 | -0.0053 [-0.0102, 0.0101] | 0.0322 → 0.0310 | -0.0012 [-0.0149, 0.0056] | +0.0349 [-0.026, 0.116] | +0.0100 [-0.066, 0.105] |

### Protocol sample, pixels not flagged "recommended"

1,066 ignitions with the predictor; events {'ge4': 57, 'ge40': 21, 'ge400': 8}.

Test 1:

| Outcome | Events | SME2 anomaly | SMAP root zone, 12 months | SMAP surface, 12 months | Model v2.1 | SME2 − SMAP root zone |
|---|---|---|---|---|---|---|
| ≥ 4 ha | 57 | 0.539 [0.460, 0.621] | 0.455 [0.396, 0.524] | 0.466 [0.397, 0.541] | 0.637 [0.585, 0.692] | [-0.040, 0.197] |
| ≥ 40 ha (no claim: under 30 events) | 21 | 0.545 [0.438, 0.660] | 0.464 [0.375, 0.559] | 0.492 [0.395, 0.605] | 0.646 [0.484, 0.795] | [-0.081, 0.236] |
| ≥ 400 ha (no claim: under 30 events) | 8 | 0.436 [0.262, 0.640] | 0.483 [0.286, 0.686] | 0.414 [0.240, 0.619] | 0.633 [0.361, 0.845] | [-0.333, 0.275] |

Test 3: 1,063 ignitions, 57 of ≥ 4 ha. The fine part alone: AUC 0.506 [0.437, 0.585].

| SMAP anomaly | AUC, SMAP alone | AUC, SMAP + SME2 fine part | Change | 95 % interval |
|---|---|---|---|---|
| root zone (decision) | 0.454 | 0.487 | +0.0330 | [-0.044, 0.117] |
| surface | 0.465 | 0.495 | +0.0298 | [-0.044, 0.110] |

Test 5: 1,059 ignitions have the term (mean size 0.0056 m³/m³, 0.08 z-score units as fed to the model; 100 % in cells not flagged "recommended"). It changes `p_large_fire` by 3.8 % of its mean on average.

| Outcome | Events | Term alone, AUC | Model: AUC without → with | Change in AUC | PR-AUC without → with | Change in PR-AUC | Added to SMAP root zone: change in AUC | Added to SMAP surface |
|---|---|---|---|---|---|---|---|---|
| ≥ 4 ha | 57 | 0.460 [0.387, 0.538] | 0.6373 → 0.6393 | +0.0020 [-0.0026, 0.0063] | 0.1429 → 0.1401 | -0.0029 [-0.0109, 0.0009] | -0.0066 [-0.041, 0.025] | -0.0024 [-0.042, 0.033] |
| ≥ 40 ha (no claim) | 21 | 0.398 [0.257, 0.531] | 0.6464 → 0.6497 | +0.0033 [-0.0030, 0.0113] | 0.1175 → 0.1153 | -0.0022 [-0.0157, 0.0019] | -0.0332 [-0.090, 0.020] | -0.0442 [-0.110, 0.000] |
| ≥ 400 ha (no claim) | 8 | 0.512 [0.282, 0.694] | 0.6331 → 0.6434 | +0.0103 [-0.0029, 0.0261] | 0.1385 → 0.1391 | +0.0007 [-0.0009, 0.0031] | -0.0007 [-0.076, 0.063] | -0.0011 [-0.059, 0.053] |

### Since 2025-11-01, pixels flagged "recommended"

1,623 ignitions with the predictor; events {'ge4': 211, 'ge40': 63, 'ge400': 25}.

Test 1:

| Outcome | Events | SME2 anomaly | SMAP root zone, 12 months | SMAP surface, 12 months | Model v2.1 | SME2 − SMAP root zone |
|---|---|---|---|---|---|---|
| ≥ 4 ha | 211 | 0.554 [0.527, 0.581] | 0.550 [0.501, 0.600] | 0.567 [0.525, 0.612] | 0.610 [0.561, 0.658] | [-0.053, 0.058] |
| ≥ 40 ha | 63 | 0.532 [0.459, 0.606] | 0.508 [0.437, 0.583] | 0.542 [0.474, 0.605] | 0.628 [0.576, 0.686] | [-0.083, 0.127] |
| ≥ 400 ha (no claim: under 30 events) | 25 | 0.555 [0.474, 0.629] | 0.458 [0.352, 0.551] | 0.508 [0.399, 0.593] | 0.664 [0.543, 0.796] | [-0.028, 0.222] |

Test 3: 1,616 ignitions, 211 of ≥ 4 ha. The fine part alone: AUC 0.504 [0.471, 0.536].

| SMAP anomaly | AUC, SMAP alone | AUC, SMAP + SME2 fine part | Change | 95 % interval |
|---|---|---|---|---|
| root zone (decision) | 0.549 | 0.550 | +0.0006 | [-0.022, 0.025] |
| surface | 0.567 | 0.562 | -0.0047 | [-0.027, 0.018] |

Test 5: 1,596 ignitions have the term (mean size 0.0059 m³/m³, 0.17 z-score units as fed to the model; 1 % in cells not flagged "recommended"). It changes `p_large_fire` by 6.5 % of its mean on average.

| Outcome | Events | Term alone, AUC | Model: AUC without → with | Change in AUC | PR-AUC without → with | Change in PR-AUC | Added to SMAP root zone: change in AUC | Added to SMAP surface |
|---|---|---|---|---|---|---|---|---|
| ≥ 4 ha | 208 | 0.530 [0.490, 0.572] | 0.6094 → 0.6104 | +0.0009 [-0.0030, 0.0050] | 0.1701 → 0.1705 | +0.0004 [-0.0028, 0.0042] | +0.0135 [-0.001, 0.030] | +0.0115 [-0.004, 0.030] |
| ≥ 40 ha | 60 | 0.519 [0.447, 0.588] | 0.6236 → 0.6202 | -0.0034 [-0.0093, 0.0032] | 0.0604 → 0.0607 | +0.0004 [-0.0038, 0.0089] | +0.0186 [-0.008, 0.043] | +0.0100 [-0.021, 0.040] |
| ≥ 400 ha (no claim) | 25 | 0.488 [0.379, 0.603] | 0.6648 → 0.6603 | -0.0045 [-0.0081, 0.0009] | 0.0313 → 0.0302 | -0.0010 [-0.0089, 0.0026] | +0.0153 [-0.018, 0.056] | +0.0080 [-0.031, 0.053] |

### Perimeters of all 2026 (test 4 without the season limit)

98 perimeters ≥ 40 ha started in the window; 60 have a pass within 12 days before the start with enough valid cells inside and in the ring (27 of them ≥ 400 ha).

| | Value |
|---|---|
| Fires where the burned area was drier than the ring / wetter | 27 / 33 |
| Sign test, two-sided p | 0.52 |
| Mean of (inside − ring), z-score | +0.051 [-0.042, 0.139] |
| Median of (inside − ring), z-score | +0.016 [-0.015, 0.086] |
| Mean of (inside − ring), m³/m³ | +0.0015 [-0.0014, 0.0039] |
| Fires ≥ 400 ha only (27): drier / mean difference | 13 / -0.034 [-0.183, 0.076] |


## The point-wise term against the map's code

`ml.grid.nisar_fine` on the whole `ca` grid for a day, against the windows used here, at pixels of ignitions:

| Day | Pixels | With a term: map / here / both | Median, 90th, 99th percentile of the difference, m³/m³ | Largest difference, m³/m³ | Correlation | Flag differs |
|---|---|---|---|---|---|---|
| 2026-07-22 | 500 | 369 / 369 / 369 | 3.9e-09, 1.2e-08, 1.7e-04 | 5.8e-04 | 0.999987 | 0 |
| 2026-08-31 | 500 | 367 / 367 / 367 | 4.2e-09, 1.5e-08, 3.2e-04 | 5.4e-04 | 0.999976 | 0 |

The two give a term at the same pixels, with the same flag. Nine pixels in ten agree to rounding; about one in a hundred differs by up to 0.0006 m³/m³ (a tenth of the term's mean size, 2 % of its limit). The cause of those few differences was not found; they are not from scenes arriving in between (the comparison was repeated right after an extraction) and are too small to move any test here.


## How to repeat

`~/spaceapps/ml-work/research/n2/run.sh` reads every staged SME2 scene (about 3 minutes), runs the tests (about
2 minutes) and rewrites `results.json`, `RESULTS.md`, `ignitions_n2.parquet` and `perimeters_n2.csv`. `CHECK=1`
adds the comparison with the map's code. Scripts: `extract.py`, `core.py`, `analyze.py`, `report.py`, `check.py`.
Inputs: the lake view `~/spaceapps/ml-work/lake`, the 2025–2026 ignitions of `~/spaceapps/ml-work/research/oot/`,
model v2.1 in `~/spaceapps/ml-work/models/v2.1/full`.

## Added after the protocol: does the anomaly carry information beyond the full model? (exploratory)

Not part of the pre-registered tests. Logistic regression of the outcome on the logit of model v3.1's prediction
(trained on 2016–2024; it includes weather, HRRR gusts, terrain, fuels, SMAP dryness and fuel age) and on the SME2
anomaly of the primary predictor; intervals by bootstrap over calendar weeks; AUC gains are in-sample.

| Sample | Outcome | Events | Odds ratio of the SME2 anomaly per z-unit, model controlled | AUC model → model + SME2 |
|---|---|---|---|---|
| Fire season 2026 (protocol window), 2,113 ignitions | ≥ 4 ha | 213 | 0.84 [0.76, 0.90] | 0.593 → 0.606 (gain [0.002, 0.027]) |
| | ≥ 40 ha | 61 | 0.91 [0.68, 1.15] | 0.643 → 0.646 |
| | ≥ 400 ha | 23 | 0.81 [0.55, 1.06] | 0.688 → 0.690 |
| All dates with SME2 (Nov 2025 – Oct 2026), 3,213 ignitions | ≥ 4 ha | 280 | 0.83 [0.78, 0.89] | 0.648 → 0.655 (gain [0.000, 0.016]) |
| | ≥ 40 ha | 86 | 0.87 [0.70, 1.04] | 0.697 → 0.699 |
| | ≥ 400 ha | 34 | 0.75 [0.60, 1.00] | 0.768 → 0.767 |

A pixel drier than its own NISAR record had higher odds of a fire of 4 ha or more than the model predicts. For
larger fires the direction is the same and the events too few. The coefficient is fitted on the only season NISAR
has: it is the basis of the experimental layers `sme2_anomaly`, `p_large_fire_sme2` and `risk_index_sme2` of the
map (odds ratio 0.84 per unit), not of `risk_index`. The test that can confirm it is the 2027 season.

### The same with model v3.3 controlled (2026-11-15, 08:00)

v3.3 ranks the ignitions of 2026 much better than v3.1 did (vegetation type and the surroundings of the cell). If
the SME2 anomaly had only stood in for something the model lacked about the land, its odds ratio would move
towards 1. It does not:

| Sample | Outcome | Events | Odds ratio per z-unit, v3.3 controlled | AUC v3.3 → v3.3 + SME2 (in-sample) |
|---|---|---|---|---|
| Fire season 2026 (protocol window), 2,113 ignitions | ≥ 4 ha | 213 | 0.82 [0.75, 0.88] | 0.648 → 0.656 (gain [0.002, 0.022]) |
| | ≥ 40 ha | 61 | 0.88 [0.65, 1.12] | 0.758 → 0.760 |
| | ≥ 400 ha | 23 | 0.78 [0.53, 1.08] | 0.834 → 0.838 |
| All dates with SME2 (Nov 2025 – Oct 2026), 3,213 ignitions | ≥ 4 ha | 280 | 0.82 [0.77, 0.88] | 0.690 → 0.697 (gain [0.001, 0.014]) |
| | ≥ 40 ha | 86 | 0.86 [0.68, 1.02] | 0.787 → 0.787 |
| | ≥ 400 ha | 34 | 0.75 [0.61, 0.98] | 0.861 → 0.862 |

The association is the same size with the better model controlled (0.82 against 0.84 for fires ≥ 4 ha). The
experimental layers keep the coefficient 0.84: the two estimates are within each other's intervals and both come
from the one season there is. `docs/versions/v3.3.2/sme2_beyond_model_v3.3_exploratory.json`.

## Sensitivity: the same tests with the ascending passes (2026-11-15, 09:27)

The final run above (03:37) had the descending passes; the lake then finished the ascending ones (989 scenes on
13 tracks to 2026-10-21, from 492 on 10). The protocol's result stays the one of 03:37. Run again on everything,
as a check of how much it depends on which passes were there:

| Test | Final run (descending) | With the ascending passes |
|---|---|---|
| Ignitions with the predictor; fires ≥ 4 ha | 2,113; 213 | 2,478; 241 |
| (1) AUC of the SME2 anomaly alone | 0.550 [0.520, 0.576] | 0.552 [0.516, 0.584] |
| SMAP root-zone / surface anomaly, same ignitions | 0.447 / 0.463 | 0.446 / 0.468 |
| (2) Odds ratio with weather controlled | 0.89 [0.81, 0.97] | 0.88 [0.78, 1.00] |
| with weather and SMAP controlled | 0.89 [0.82, 0.98] | 0.89 [0.78, 1.005] |
| (3) Resolution: fine part added to SMAP root zone | +0.027 [−0.012, 0.059] | +0.009 [−0.026, 0.042] |
| added to SMAP surface | +0.030 [0.002, 0.058] | +0.004 [−0.025, 0.032] |
| the fine part alone | 0.496 [0.467, 0.522] | 0.473 [0.444, 0.498] |
| (4) Perimeters: drier inside / wetter inside | 21 / 24 | 25 / 24 |
| (5) The map's term through the model, ROC-AUC | +0.004 [0.000, 0.008] | +0.003 [−0.001, 0.007] |
| Beyond model v3.3 (exploratory), ≥ 4 ha | 0.82 [0.75, 0.88] | 0.78 [0.69, 0.89] |

- The decision does not change: test 1 passes, tests 3 and 4 do not, no claim of skill for the fine-scale term.
- The anomaly itself is as before: the same AUC, the same odds ratio; with SMAP controlled the upper end of the
  interval now reaches 1.005, with model v3.3 controlled it stays clear of 1.
- The part finer than 9 km looks weaker with more passes, not stronger: the one positive interval of the final run
  (added to SMAP's surface anomaly) is gone, and alone the fine part is slightly on the wrong side of 0.5. This
  supports keeping the fine-scale term small and the whole anomaly as the experimental layer.

`docs/versions/v3.3.2/nisar_test_rerun_with_ascending_passes.json`.
