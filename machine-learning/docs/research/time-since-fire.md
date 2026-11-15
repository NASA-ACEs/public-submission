# Time since fire and fire spread (TSF)

Run 2026-11-15. Question: does land that burned recently limit the spread of a later fire in California, and by how
much? The GCOV study (`gcov.md`) showed that NISAR L-band HV sees post-fire regrowth for at least 7.5 years, but no
fire occurred in its area during the GCOV period, so the effect of that fuel state on spread could not be tested
there. This study measures it from fire history alone, independently of GCOV.

Scripts, tables and intermediate files: `~/spaceapps/ml-work/research/tsf/` on the server.

## Verdict

- **Young burn scars limit spread, strongly, and the effect fades with age.** Two independent tests agree.
- **Ignitions** (74,568, 2016–2024): inside a scar younger than 2 years, 0 of 583 ignitions became a fire ≥ 400 ha
  where the weather-terrain-fuel baseline expected 8; inside scars of 2–5 years, 4 against 11 expected (ratio 0.36
  [0.08, 0.81]); inside scars of 5–10 years, 6 against 5.5 (no effect).
- **Perimeters** (410 fires ≥ 400 ha, 2017–2025): within 1 km of the edge of an earlier scar, the scar side burned
  0.39 [0.30, 0.49] times as often as the unburned side for scars of 1–3 years, 0.68 [0.52, 0.85] for 3–6 years and
  0.82 [0.61, 1.03] for 6–9 years.
- **Recovery curve.** Relative burn probability `r(a) = 1 − 1.08·exp(−a / 3.2)`, `a` = years since the fire:
  0.42 [0.34, 0.53] at 2 years, 0.77 [0.62, 0.89] at 5, 0.91 [0.75, 0.98] at 8. One-parameter form used in
  `tsf.py`: `1 − exp(−a / 3.5)`.
- **LANDFIRE does not carry this.** The odds ratios are the same with and without the fuel layers as controls.
- **As a model feature the gain in ranking is not significant** (PR-AUC 0.0415 → 0.0449, gain +0.0034
  [−0.0008, 0.0097]): only 2.75 % of ignitions lie inside a recorded scar. The effect is large locally and small
  in the statewide score.
- **Against GCOV.** Direction agrees (HV-based fuel state is below 1 inside every scar). Scale does not: HV recovers
  more slowly than burnability and differs between fires, so `gcov_fuel_state` cannot be used as the multiplier
  directly (section 4).

## Data and definitions

- Perimeters: fuel-lake table `fire_perimeters`, 4,927 valid polygons (CAL FIRE FRAP 2015–2025, NIFC WFIGS 2026).
  End of a fire = containment date, else the start date (also where the containment date precedes the start or
  follows it by more than 400 days). **The record starts in 2015**: "no scar" means no recorded fire since 2015.
- Ignitions: `ml.data.ignition_rows` on `points_v1b_series.parquet`: 74,568 FPA-FOD ignitions 2016–2024;
  4,136 reached ≥ 4 ha, 1,364 ≥ 40 ha, 477 ≥ 400 ha.
- `years_since_fire`: years between the ignition date and the end of the most recent fire whose perimeter contains
  the point and that ended at least 30 days earlier; 10 where there is none (`tsf.years_since_fire`).
- Baseline (control): LightGBM with the settings of `ml.train.CLF`, each year predicted by a model that did not see
  it, one model per size threshold. Two feature sets: gridMET weather of the day + terrain (`wx_terrain`), and the
  same + LANDFIRE fuel loads and canopy (`wx_terrain_fuel`). `v0-base/full/oof_fire.parquet` did not exist yet.
- Intervals: 95 %. Odds ratios: logistic regression on the logit of the baseline, errors clustered by 50 km block.
  Observed/expected: bootstrap over blocks (500). Perimeter test: bootstrap over fires (2,000).

## 1. Ignitions inside earlier scars

2,054 ignitions (2.75 %) lie inside a scar: 583 younger than 2 years, 976 of 2–5 years, 495 of 5–10 years.

| Scar age | n | median ha | ≥ 4 ha | ≥ 40 ha | ≥ 400 ha |
|---|---|---|---|---|---|
| < 2 yr | 583 | 0.04 | 35 (6.00 %) | 5 (0.86 %) | 0 (0.00 %) |
| 2–5 yr | 976 | 0.04 | 66 (6.76 %) | 22 (2.25 %) | 4 (0.41 %) |
| 5–10 yr | 495 | 0.04 | 44 (8.89 %) | 13 (2.63 %) | 6 (1.21 %) |
| no scar | 72,514 | 0.04 | 3,991 (5.50 %) | 1,324 (1.83 %) | 467 (0.64 %) |

Raw rates mislead: scars lie in fire-prone terrain, so their ignitions start with a higher expected risk. Observed
against expected from the unseen-year baseline (`wx_terrain_fuel`; `wx_terrain` gives the same within 0.04):

| Scar age | ≥ 4 ha | ≥ 40 ha | ≥ 400 ha |
|---|---|---|---|
| < 2 yr | 35 / 48.4 = 0.72 [0.48, 1.02] | 5 / 18.1 = 0.28 [0.06, 0.56] | 0 / 8.4 = 0 (upper bound 0.36) |
| 2–5 yr | 66 / 78.4 = 0.84 [0.60, 1.23] | 22 / 27.2 = 0.81 [0.51, 1.20] | 4 / 11.0 = 0.36 [0.08, 0.81] |
| 5–10 yr | 44 / 35.6 = 1.24 [0.96, 1.62] | 13 / 12.8 = 1.02 [0.55, 1.57] | 6 / 5.5 = 1.10 [0.30, 2.05] |
| no scar | 1.00 [0.93, 1.10] | 1.01 [0.90, 1.15] | 1.03 [0.89, 1.20] |

The upper bound for "< 2 yr, ≥ 400 ha" is the Poisson 95 % bound for zero events (3.0 / 8.4).

Odds ratios, baseline controlled (`wx_terrain_fuel` / `wx_terrain`):

| Term | ≥ 4 ha | ≥ 40 ha | ≥ 400 ha |
|---|---|---|---|
| inside any scar (< 10 yr) | 0.88 [0.68, 1.13] / 0.94 | 0.66 [0.49, 0.89] / 0.66 [0.49, 0.87] | 0.37 [0.20, 0.71] / 0.37 [0.20, 0.69] |
| scar < 2 yr | 0.69 [0.47, 1.02] | 0.26 [0.11, 0.64] | no events |
| scar 2–5 yr | 0.82 [0.57, 1.18] | 0.78 [0.49, 1.25] | 0.34 [0.12, 0.93] |
| scar 5–10 yr | 1.26 [0.95, 1.69] | 1.00 [0.59, 1.68] | 1.06 [0.48, 2.36] |
| per year since fire (0–10, linear) | 1.03 [0.99, 1.07] | 1.08 [1.03, 1.14] | 1.24 [1.11, 1.39] |

- **With and without fuel control the estimates coincide** (0.373 against 0.368 for "any scar", ≥ 400 ha), so the
  LANDFIRE release chosen for each row does not encode the scar in a way the model uses.
- **Without any control** the effect is hidden or reversed (≥ 4 ha: 1.30 [1.02, 1.68]): the scars' higher base risk.
- **Ignition years 2020–2024 only** (where at least five years of history exist): the same picture (any scar,
  ≥ 400 ha: 0.40 [0.20, 0.79]; < 2 yr, ≥ 40 ha: 0.24 [0.08, 0.74]).

Finer age bins (observed / expected, `wx_terrain_fuel`):

| Scar age | n | ≥ 4 ha | ≥ 40 ha | ≥ 400 ha |
|---|---|---|---|---|
| 1–6 months | 58 | 7 / 3.9 = 1.81 | 2 / 1.5 | 0 / 0.8 |
| 6–24 months | 525 | 28 / 44.5 = 0.63 | 3 / 16.6 = 0.18 | 0 / 7.6 |
| 2–3.5 yr | 477 | 30 / 37.6 = 0.80 | 10 / 12.5 = 0.80 | 2 / 4.8 = 0.42 |
| 3.5–5 yr | 499 | 36 / 40.8 = 0.88 | 12 / 14.7 = 0.82 | 2 / 6.2 = 0.32 |
| 5–7 yr | 359 | 29 / 26.8 = 1.08 | 8 / 9.6 = 0.83 | 1 / 3.9 = 0.25 |
| 7–10 yr | 136 | 15 / 8.8 = 1.70 | 5 / 3.2 = 1.56 | 5 / 1.5 = 3.28 |

The first months after containment (58 ignitions) show more small fires than expected: probably rekindles and
reports from the same incident. The 7–10 year bin has 136 ignitions; its excess rests on 5 fires.

Recovery form on the odds, `odds multiplier = exp(−β·exp(−a / τ))`, τ by profile likelihood:

| Threshold | τ (yr) | β | Multiplier at 1 / 3 / 5 / 8 yr |
|---|---|---|---|
| ≥ 400 ha | 3 | 3.82 ± 1.22 (p = 0.002) | 0.06 / 0.25 / 0.49 / 0.77 |
| ≥ 40 ha | 2 | 1.82 ± 0.64 (p = 0.004) | 0.33 / 0.67 / 0.86 / 0.97 |
| ≥ 4 ha | 2 | 0.66 ± 0.33 (p = 0.046) | 0.67 / 0.86 / 0.95 / 0.99 |

The likelihood is flat in τ between 2 and 5 years. The larger the fire, the stronger the effect: young scars barely
change whether an ignition escapes initial attack, and strongly change whether it becomes large.

## 2. Spread at the scale of perimeters

For each of 410 FRAP fires ≥ 400 ha (2017–2025): the perimeter plus a 2 km buffer on a 200 m grid (EPSG:3310).
Cells burned by another fire within the year before, or concurrently, are excluded. Scar cells = inside an
earlier perimeter of the stated age at the start of the fire; reference cells = no recorded fire. A fire enters a
row if it has at least 25 scar cells and 25 reference cells (1 km² each).

| Test | Scar age | Fires | Burned, scar | Burned, reference | Ratio of means | Median difference | Fires with scar lower / higher |
|---|---|---|---|---|---|---|---|
| Whole region | 1–3 yr | 85 | 0.132 | 0.439 | 0.30 [0.22, 0.39] | −0.30 [−0.37, −0.24] | 78 / 7 |
| Whole region | 3–6 yr | 81 | 0.211 | 0.417 | 0.51 [0.38, 0.67] | −0.27 [−0.34, −0.20] | 64 / 17 |
| Whole region | 6–9 yr | 42 | 0.231 | 0.314 | 0.74 [0.50, 0.99] | −0.13 [−0.21, −0.01] | 27 / 15 (p = 0.09) |
| 1 km band at the scar edge | 1–3 yr | 85 | 0.148 | 0.382 | 0.39 [0.30, 0.49] | −0.22 [−0.28, −0.16] | 71 / 8 |
| 1 km band at the scar edge | 3–6 yr | 81 | 0.227 | 0.335 | 0.68 [0.52, 0.85] | −0.14 [−0.20, −0.04] | 60 / 20 |
| 1 km band at the scar edge | 6–9 yr | 42 | 0.240 | 0.294 | 0.82 [0.61, 1.03] | −0.04 [−0.12, 0.01] | 26 / 15 (p = 0.12) |

Sign tests for 1–3 and 3–6 years: p < 0.001. The band test compares scar cells within 1 km inside the scar edge
with never-burned cells within 1 km outside it, so both sides are equally close to the fire and to the same roads
and ridges.

Yearly bins, band test, with the fitted curve `r(a) = 1 − 1.08·exp(−a / 3.2)` (weights = fires per bin; bootstrap
over fires: A [0.66, 1.50], τ [1.9, 7.9] years):

| Scar age (mean, yr) | Fires | Burned, scar | Burned, outside | Ratio | Fit |
|---|---|---|---|---|---|
| 1.7 | 51 | 0.126 | 0.397 | 0.32 | 0.35 |
| 2.6 | 43 | 0.212 | 0.375 | 0.57 | 0.51 |
| 3.7 | 46 | 0.253 | 0.355 | 0.71 | 0.65 |
| 4.6 | 32 | 0.238 | 0.341 | 0.70 | 0.74 |
| 5.5 | 28 | 0.280 | 0.377 | 0.74 | 0.81 |
| 6.5 | 18 | 0.296 | 0.356 | 0.83 | 0.86 |
| 7.8 | 19 | 0.189 | 0.290 | 0.65 | 0.90 |
| 8.4 | 7 | 0.328 | 0.206 | 1.59 | 0.92 |

Fitted values: r(1) = 0.21 [0.03, 0.43], r(2) = 0.42 [0.34, 0.53], r(3) = 0.57 [0.48, 0.69], r(5) = 0.77
[0.62, 0.89], r(8) = 0.91 [0.75, 0.98], r(10) = 0.95 [0.81, 0.99]. The whole-region version gives a slower recovery
(`1 − 1.03·exp(−a / 5.0)`: 0.31 at 2 years, 0.62 at 5, 0.79 at 8).

## 3. `years_since_fire` as a model feature

Unseen year, LightGBM `ml.train.CLF`, 500 paired bootstrap resamples over ISO weeks. Feature: years since the last
fire, capped at 10, 10 where none.

| Label | Base features | PR-AUC base | PR-AUC + `years_since_fire` | Gain | Seed noise |
|---|---|---|---|---|---|
| ≥ 400 ha | weather + terrain + fuel | 0.0415 | 0.0449 | +0.0034 [−0.0008, 0.0097] | 0.0017 |
| ≥ 400 ha | weather + terrain | 0.0388 | 0.0409 | +0.0021 [−0.0018, 0.0063] | 0.0003 |
| ≥ 40 ha | weather + terrain + fuel | 0.0598 | 0.0615 | +0.0016 [−0.0009, 0.0054] | |
| ≥ 40 ha | weather + terrain | 0.0549 | 0.0575 | +0.0025 [0.0005, 0.0053] | |
| ≥ 4 ha | weather + terrain + fuel | 0.1238 | 0.1244 | +0.0006 [−0.0012, 0.0022] | |

- ROC-AUC is unchanged (0.813 → 0.812 for ≥ 400 ha).
- Among the 2,054 ignitions inside scars the ranking improves (PR-AUC 0.012 → 0.035 for ≥ 400 ha, 10 events;
  0.041 → 0.046 for ≥ 40 ha, 40 events); too few events for an interval.
- Logit coefficient of `years_since_fire` (linear, 0–10) with the baseline controlled: +0.216 ± 0.059 per year for
  ≥ 400 ha, +0.080 ± 0.024 for ≥ 40 ha, +0.030 ± 0.018 for ≥ 4 ha. The linear form overstates the effect of old
  scars (it implies 0.5 at 7 years, the class estimate is 1.06); the recovery form of section 1 fits better.
- No gain in the headline metric can be claimed. The feature is cheap (one rasterisation: 0.5 s on `smm`, 0.9 s on
  `ca`) and matters where it applies: 5.1 % of California's 200 m cells and 38 % of the Santa Monica Mountains AOI
  burned in the last ten years (11 % in the last two).

## 4. Proposed fuel modifier and its relation to GCOV

Two multipliers, both 1 where no fire is recorded or it is 10 or more years old (`tsf.py`):

| Use | Formula | 1 yr | 2 yr | 3 yr | 5 yr | 8 yr |
|---|---|---|---|---|---|---|
| Probability that a cell burns when a large fire reaches it (spread layer, rate-of-spread scaling) | `burn_modifier = max(0.1, 1 − exp(−a / 3.5))` | 0.25 | 0.44 | 0.58 | 0.76 | 0.90 |
| Measured band estimate, for reference | `1 − 1.08·exp(−a / 3.2)` | 0.21 [0.03, 0.43] | 0.42 [0.34, 0.53] | 0.57 [0.48, 0.69] | 0.77 [0.62, 0.89] | 0.91 [0.75, 0.98] |
| Odds that an ignition there becomes ≥ 400 ha | `odds_modifier = exp(−3.8·exp(−a / 3))` | 0.07 | 0.14 | 0.25 | 0.49 | 0.77 |
| Odds, ≥ 40 ha | `exp(−1.8·exp(−a / 2))` | 0.34 | 0.52 | 0.67 | 0.86 | 0.97 |

If part of the 6–9-year deficit (0.82) is the control line and not the fuel, the fuel-only effect of a 2-year scar
is 0.39 / 0.82 = 0.48 instead of 0.39; treat 0.4–0.5 as the range.

GCOV fuel state (`gcov_fuel_state`, HV power against the mature reference) by scar age in the Santa Monica
Mountains, 2026-10-20, next to the burn-probability ratio at the same age:

| Scar age | Cells (30 m) | Median `gcov_fuel_state` [quartiles] | Share below 0.7 | Burn-probability ratio at that age |
|---|---|---|---|---|
| < 2 yr (Palisades, Kenneth, Franklin) | 115,430 | 0.52 [0.43, 0.63] | 83 % | 0.36 |
| 5–7 yr (Palisades 2021) | 4,409 | 0.73 [0.62, 0.86] | 43 % | 0.80 |
| 7–9 yr (Woolsey, Hill) | 301,765 | 0.61 [0.50, 0.75] | 68 % | 0.90 |
| no fire since 2015 | 225,636 | 1.04 [0.84, 1.20] | 14 % | 1 |

- **Direction: consistent.** Every scar class is below the unburned reference in HV and in burn probability, and
  the youngest scars are lowest in both.
- **Scale: not consistent.** Burnability is back to 0.9 after eight years while HV power is still at 0.61, and the
  7–9-year scar is darker than the 5–7-year one. L-band HV follows woody structure, which takes decades to return
  and depends on what stood there before; spread needs fine fuel and canopy continuity, which return in 5–10 years.
- **Consequence.** `gcov_fuel_state` must not be used as the multiplier itself: it would under-penalise the
  youngest scars (0.52 against about 0.36) and over-penalise old ones (0.61 against 0.90).
- **Proposed combination (not validated: no fire in the AOI during the GCOV period).** Magnitude from fire history,
  footprint from GCOV: `modifier = 1 − (1 − burn_modifier(age))·w`, with `w = 1` where `gcov_fuel_state < 0.7`
  and 0 elsewhere. Inside young perimeters 17 % of cells are not below 0.7 (unburned islands, which perimeters
  overstate) and keep full fuel. Cells with a deficit but no recorded fire (14 % of the unburned land) get no
  modifier: their age is unknown and other land covers are dark in HV too.

## Limits

- The perimeter record starts in 2015. Land burned before 2015 counts as "no scar", which biases every effect
  towards zero, most for the early years; scars of 5–9 years exist only for ignitions and fires from 2020 on.
- Fire edges are set by suppression as well as by fuel, and crews use recent scars as control lines. The decline of
  the effect with age (0.39 → 0.68 → 0.82 in the band test) is the evidence that fuel matters; the 6–9-year value
  is an upper bound on a control-line component that does not fade.
- The reference cells of the whole-region test include water, cropland and urban land, which do not burn. That
  lowers the reference and so works against the finding; the band test is less exposed.
- Perimeters overstate burned area (unburned islands) and FRAP has duplicates and dating errors: 2,383 of 4,927
  perimeters have the same start and end date.
- Final size depends on suppression. The 8–9-year bin (7 fires) and the 7–10-year ignitions (136, 5 large) are
  too small to interpret.
- Baselines were not tuned; `lfmc_hat` is not among the controls.

## Files

- `tsf.py`: `load_perimeters`, `years_since_fire` (points), `years_since_fire_grid` (raster), `burn_modifier`,
  `odds_modifier`. Point and raster versions agree at 20,000 random cell centres on both grids.
- `s1_prep.py`, `s1_base.py`, `s1_an.py`, `s3.py`: ignitions (tables `t1_*.csv`, `t3_prauc.csv`).
- `s2.py`, `s2b.py`, `s5.py`: perimeter test (`t2_perimeter.csv`, `t2b_*.csv`).
- `s4.py`: raster check, `ysf_smm_20261020.tif`, `t4_gcov_by_age.csv`.
