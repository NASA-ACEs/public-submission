# Model v2 on 2025–2026: years after the training period

Run 2026-11-15. The models were trained on 2016–2024 (FPA-FOD). Here they predict the ignitions of 2025 and 2026
(NIFC WFIGS, a different source), with features computed by the code the map uses (`ml.series`, the `ca` LANDFIRE
cells, fire perimeters). Scripts and predictions: `~/spaceapps/ml-work/research/oot/` on the server.

Sample: 13,296 ignitions with a reported size, 2025-01-01 to 2026-11-10 (8,335 in 2025, 4,961 in 2026); 80 became
fires ≥ 400 ha, 239 ≥ 40 ha, 875 ≥ 4 ha. Another 8,955 WFIGS ignitions have no size and are left out; if those
are mostly small fires, base rates here are too high and the ranking figures are unaffected only if missingness
does not depend on the predictors. Intervals: bootstrap over calendar weeks.

## Ranking and calibration

| Outcome | | PR-AUC [95 %] | ROC-AUC [95 %] | Burning index PR-AUC / ROC-AUC | Gain over burning index, PR-AUC |
|---|---|---|---|---|---|
| ≥ 400 ha, 2025–2026 | v2 `full` | 0.0479 [0.021, 0.099] | 0.801 [0.743, 0.847] | 0.0123 / 0.628 | [0.010, 0.085] |
| ≥ 400 ha, 2025 | | 0.0497 [0.019, 0.177] | 0.854 [0.801, 0.908] | 0.0094 / 0.565 | [0.012, 0.166] |
| ≥ 400 ha, 2026 | | 0.0460 [0.014, 0.100] | 0.739 [0.664, 0.808] | 0.0238 / 0.696 | [−0.019, 0.073] |
| ≥ 40 ha, 2025–2026 | | 0.0592 [0.044, 0.082] | 0.736 [0.707, 0.769] | 0.0284 / 0.616 | [0.016, 0.050] |
| ≥ 4 ha, 2025–2026 | | 0.1192 [0.104, 0.142] | 0.663 [0.638, 0.690] | 0.0842 / 0.598 | [0.022, 0.053] |
| ≥ 400 ha, 2025–2026 | v2 `core` (no terrain and fuel) | 0.0146 [0.009, 0.024] | 0.707 [0.645, 0.762] | | |

- Base rate of large fires 0.0060. Large fires among the top-ranked ignitions (`full`): 10 % in the top 1 %, 28 %
  in the top 5 %, 45 % in the top 10 %, 65 % in the top 20 %.
- **Calibration holds two years out:** the model expects 91.9 large fires, 80 occurred. By decile of predicted
  probability: predicted 0.0007 … 0.0304, observed 0.0000 … 0.0271.
- The model beats the burning index in 2025 and overall; in 2026 alone the interval of the gain includes zero
  (42 large fires).
- Terrain and fuel are confirmed: without them PR-AUC is 0.015.

## Fuel age: confirmed for young scars, not for old ones

Large fires observed against expected by the model without the fuel-age modifier, for ignitions inside a perimeter
of the last 10 years:

| Years since fire | 2016–2024 (out-of-year predictions) | 2025–2026 | Pooled | Modifier of v2 (fade 3 y) | Modifier from the pooled fit (fade 2 y) |
|---|---|---|---|---|---|
| 0–1 | 0 / 6.6 (275 ignitions) | 0 / 0.5 (26) | 0 / 7.1 | 0.04 | 0.02 |
| 1–2 | 0 / 3.9 (308) | 0 / 0.2 (23) | 0 / 4.2 | 0.09 | 0.09 |
| 2–3.5 | 2 / 5.0 (477) | 0 / 0.2 (34) | 2 / 5.3 | 0.21 | 0.26 |
| 3.5–5 | 2 / 6.7 (499) | 3 / 2.6 (297) | 5 / 9.3 | 0.39 | 0.49 |
| 5–7 | 1 / 4.4 (359) | 3 / 2.0 (235) | 4 / 6.4 | 0.59 | 0.74 |
| 7–10 | 5 / 1.6 (136) | 4 / 3.3 (332) | 9 / 4.9 | 0.79 | 0.93 |

- Scars younger than 2 years: no large fire in either period, 11.3 expected.
- Scars of 3.5–10 years in 2025–2026: 10 large fires, 7.9 expected without the modifier, 5.0 with v2's modifier.
  The modifier of v2 understated risk there; as a change in PR-AUC it is [−0.004, 0.002] over both years, positive
  in 2025 and negative in 2026.
- Fitted on the pooled record the fade time is poorly determined (log loss inside scars 106.3 for 1.5–2 years,
  107.0 for 3, 107.9 for 4); 2 years fits best and leaves less reduction in old scars, which is the cautious side
  for a risk map. v2.1 uses a fade of 2 years. That choice used the 2025–2026 data, so those years no longer test
  the fuel-age modifier independently; they still test everything else.
- The independent evidence from perimeters (cells of scars burned 0.39, 0.68, 0.82 times as often as neighbouring
  land at 1–3, 3–6, 6–9 years; `time-since-fire.md`) agrees with the shorter fade.

## The largest fires of 2025–2026, as the model saw their ignition day (v3.3)

What the model, trained on 2016–2024, gave the ignition of each of the 20 largest fires on its day. Class is that
of `risk_index` (rank among ignitions on open land); the percentile is among all 13,296 ignitions of 2025–2026.

| Ignition | Fire | Size, ha | `p_large_fire` | Class | Percentile, model | Percentile, burning index |
|---|---|---|---|---|---|---|
| 2025-08-01 | Gifford | 53,261 | 0.8 % | 3 | 81 | 55 |
| 2026-08-08 | Bug | 37,932 | 2.3 % | 5 | 95 | 89 |
| 2025-07-02 | Madre | 32,693 | 3.2 % | 5 | 97 | 97 |
| 2026-07-18 | Biscar | 28,067 | 5.2 % | 5 | 98.7 | 88 |
| 2025-08-24 | Garnet | 24,218 | 60.9 % | 5 | 100 | 60 |
| 2026-08-26 | Plaskett | 12,138 | 19.5 % | 5 | 99.9 | 99 |
| 2025-09-02 | Salt | 10,377 | 9.7 % | 5 | 99.6 | 86 |
| 2026-08-08 | Timber | 10,259 | 2.0 % | 4 | 94 | 96 |
| 2025-01-07 | Palisades | 9,489 | 2.2 % | 4 | 94.5 | 77 |
| 2025-07-03 | Butler | 8,522 | 1.8 % | 4 | 93 | 26 |
| 2025-07-01 | Green | 7,698 | 3.1 % | 5 | 97 | 4 |
| 2026-05-15 | Santa Rosa Island | 7,438 | 1.2 % | 4 | 87 | 24 |
| 2025-01-07 | Eaton | 5,674 | 4.8 % | 5 | 98.6 | 9 |
| 2026-07-11 | Elephant | 5,637 | 11.9 % | 5 | 99.7 | 88 |
| 2026-07-20 | Rock | 4,859 | 0.7 % | 3 | 76 | 91 |
| 2025-08-25 | Dillon | 4,827 | 13.2 % | 5 | 99.8 | 56 |
| 2025-01-22 | Hughes | 4,219 | 15.2 % | 5 | 99.8 | 100 |
| 2026-08-03 | Gann | 4,184 | 3.3 % | 5 | 97 | 83 |
| 2026-05-14 | Range | 3,237 | 6.0 % | 5 | 99 | 47 |
| 2026-06-18 | Lost | 3,170 | 0.4 % | 2 | 62 | 85 |

- Thirteen of the twenty are in class 5, four in class 4, two in class 3, one in class 2 (v3.2.1: 11 / 5 / 3 / 1).
  The largest, Gifford, started in a mostly open cell on a day of ordinary wind: 81st percentile, class 3 (66th
  and class 2 in v3.2.1).
- The January 2025 fires in Los Angeles started at the edge of the city (the cells are 78 % and 100 % built up;
  32 % and 51 % of the land within 3 km), which lowers the probability, and are still in the top 6 % and 2 % of
  ignitions because of the gusts that day (17 and 24 m/s). The burning index, a summer dryness index, put the
  Eaton day in its lowest tenth.
- All 13,296 ignitions by class:

| Class | Share of ignitions | Large fires (of 80) | Large fires per 1,000 ignitions | Share of the area burned |
|---|---|---|---|---|
| 1 | 43.1 % | 1 | 0.17 | 1 % |
| 2 | 27.2 % | 4 | 1.1 | 3 % |
| 3 | 15.1 % | 11 | 5.5 | 19 % |
| 4 | 9.1 % | 25 | 21 | 17 % |
| 5 | 5.4 % | 39 | 54 | 59 % |

  An ignition in class 5 became a large fire 320 times as often as one in class 1. Classes 4 and 5 are 14.5 % of
  ignitions, 64 of the 80 large fires and 76 % of the 373,000 ha burned; the same number of ignitions ranked
  highest by the burning index holds 26 large fires and 47 % of the area. (v3.2.1: 13.5 % of ignitions, 54 fires,
  71 % of the area.)
