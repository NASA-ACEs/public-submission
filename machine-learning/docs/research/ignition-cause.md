# The model by cause of ignition: power lines

2026-11-15, after v3.3.1. Exploratory: this split was not planned in advance. Question: the map is meant for a
utility deciding where a spark from its lines must not happen. Does the model rank the ignitions that power lines
actually caused?

Data: the 74,568 ignitions of 2016–2024 with their out-of-year predictions (each year predicted by models that did
not see it; `oof_fire.parquet` of v3.3.1) and the cause recorded in FPA-FOD (`cause_general`). The cause is
missing or undetermined for 64 % of the ignitions, and how often a power-line cause is recorded varies by year
(207 and 220 in 2019 and 2020; 13 to 15 in 2022–2024), so the subsets are what was recorded, not a census.
Classes are those of `risk_index` in v3.3.1. Intervals: bootstrap over calendar weeks.

| Cause recorded | Ignitions | Large fires | Per 1,000 | ROC-AUC [95 %] | Burning index | Expected by the model | In classes 4–5: share of ignitions, large fires |
|---|---|---|---|---|---|---|---|
| **Power generation, transmission, distribution** | 761 | 12 | 15.8 | **0.954 [0.922, 0.982]** | 0.813 | 5.8 | 15.8 %, **12 of 12** |
| Equipment and vehicle use | 5,237 | 47 | 9.0 | 0.843 [0.791, 0.890] | 0.632 | 37.1 | 14.9 %, 29 of 47 |
| Natural (lightning) | 4,618 | 159 | 34.4 | 0.751 [0.689, 0.800] | 0.583 | 97.4 | 51.7 %, 131 of 159 |
| Arson | 4,833 | 17 | 3.5 | 0.841 [0.768, 0.916] | 0.602 | 24.1 | 9.4 %, 7 of 17 |
| Debris and open burning | 5,249 | 9 | 1.7 | 0.737 [0.461, 0.940] | 0.514 | 16.5 | 4.0 %, 2 of 9 |
| Missing or undetermined | 47,758 | 214 | 4.5 | 0.883 [0.857, 0.910] | 0.685 | 235.9 | 9.1 %, 141 of 214 |
| All | 74,568 | 477 | 6.4 | 0.885 [0.867, 0.900] | 0.591 | 460.4 | 12.5 %, 335 of 477 |

## Power lines

- All 12 large fires attributed to power lines started in cells of class 4 (5 fires, 73 ignitions) or class 5
  (7 fires, 47 ignitions). The 641 power-line ignitions in classes 1 to 3, 84 % of them, produced none.
- PR-AUC 0.376 against 0.211 for the burning index (base rate 0.016).
- The ignition of 2018-11-08 that became the largest of them (62,053 ha): `p_large_fire` 28.7 %, class 5, gusts
  of 16 m/s. Of the eight largest, six are in class 5 and two in class 4 (both with 1.2 %).
- **The model understates these fires by half**: it expects 5.8 large fires among the 761 ignitions and 12
  occurred. Lines fail in wind, at exposed places, often at night; the model's probabilities are for an average
  ignition under the same conditions. With 12 events the factor of two is uncertain (a Poisson count of 12
  against 5.8 expected has p ≈ 0.02), so it is not applied to the map.

## The other causes

- Lightning: 159 large fires against 97 expected, and the weakest ranking (0.75). Lightning starts fires in
  remote country, several at once; reach and resources decide their size, and the model sees neither.
- Arson and debris burning: fewer large fires than expected (17 against 24; 9 against 16): started near people,
  found at once.
- So `p_large_fire` is right on average over the mix of causes of 2016–2024 and should be read higher for a
  power-line or lightning ignition and lower for one started by a person on the spot.

## Limits

Twelve events. Causes recorded for a minority of ignitions, unevenly over the years. The split was looked at
after the model was final; nothing was fitted to it. The ignitions of 2025–2026 (WFIGS) carry no cause in the
lake, so this cannot be repeated out of time.
