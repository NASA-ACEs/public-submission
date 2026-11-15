# Fuel age by vegetation and period (T2)

Run 2026-11-15. Follow-up of `time-since-fire.md` and `out-of-time.md`: the fuel-age modifier held for scars younger
than about 3.5 years and failed for older ones in 2025–2026. Hypothesis tested: forest scars of the 2020–2021
megafires reburn easily, young chaparral does not. Definitions were written before any number was computed
(`PREREG.md` next to the scripts in `~/spaceapps/ml-work/research/tsf2/`).

## Verdict

- **The hypothesis is refuted, and reversed.** Forest scars protect most and longest; grass and other open land
  protect for under two years. Both tests agree.
- **Where the modifier failed in 2025–2026:** 7 of the 10 large fires that started in scars of 3.5–10 years were on
  open land (LF 2016 canopy cover < 10 %), where the model without the modifier expected 2.3. Forest and shrub scars
  of that age had 2 large fires against 5.2 expected.
- **Recommended odds modifier:** for every scar younger than 3.5 years, exp(−12·exp(−years)); for scars that were
  forest before the fire, additionally exp(−3.4·exp(−years / 3)) up to 7 years; nothing else. On 2025–2026 it has
  the lowest log loss inside scars of all forms tried (45.9 against 47.7 with no modifier and 48.5 for the form of
  v3). The forest part was defined after seeing tests 1 and 2, so it is exploratory; the part for young scars is
  the pre-registered form (b).
- **By the pre-registered rule alone the result is a tie between form (b) and no modifier**, and the tie-break
  (lower risk least) picks no modifier: 2025–2026 has only 83 ignitions in scars younger than 3.5 years (1.0 large
  fire expected, none observed) and cannot show what form (b) does. I recommend (b) all the same because the
  perimeter test confirms young-scar protection in 2025–2026 itself (16 fires, burned share 0.34 [0.06, 0.77] of
  the reference, lower in the scar in 15).
- **Permeability should depend on vegetation:** 1 − exp(−years / τ) with τ = 7 years for forest, 2.3 for shrub,
  1.3 for grass, 3.1 where the vegetation before the fire is unknown (today: 3.5 for all).

## Definitions (as pre-registered)

Vegetation before the first recorded fire: LANDFIRE LF 2016 FBFM40 fuel model of the 200 m cell (mode). Forest =
TU, TL, SB; shrub = SH, GS; grass = GR; "other" = any other code (mostly non-burnable or agricultural mode).
"Unknown" = the cell first burned in 2015–2016, so LF 2016 already shows the scar. Secondary: LF 2016 canopy
cover ≥ 40 % or < 10 %. Severity (forest only): canopy cover lost between the LANDFIRE release before and after the
fire, high = at least half. Scar age: years between the end of the earlier fire and the start of the later one.

## Test 1: cells of scars against cells with no recorded fire, 1 km band at the scar edge

Later fires ≥ 400 ha, 2017–2026 (FRAP, WFIGS for 2026), California 200 m grid, reference cells of the same
vegetation class. Pooled ratio = burned share of scar cells / burned share of reference cells, bootstrap over
fires; "lower / higher" counts fires where the scar burned less / more.

| Vegetation | Scar age (mean) | Fires | Scar cells | Burned: scar / reference | Pooled ratio [95 %] | Ratio of means [95 %] | Lower / higher |
|---|---|---|---|---|---|---|---|
| all | 0.5–2 (1.3) | 79 | 27,751 | 0.175 / 0.529 | 0.33 [0.24, 0.43] | 0.31 [0.22, 0.41] | 68 / 6 |
| all | 2–3.5 (2.7) | 56 | 18,351 | 0.355 / 0.481 | 0.74 [0.42, 1.10] | 0.57 [0.44, 0.72] | 45 / 7 |
| all | 3.5–5 (4.3) | 60 | 21,293 | 0.280 / 0.445 | 0.63 [0.47, 0.81] | 0.69 [0.52, 0.87] | 39 / 18 |
| all | 5–7 (5.9) | 54 | 18,007 | 0.375 / 0.461 | 0.81 [0.63, 1.03] | 0.76 [0.57, 0.98] | 35 / 16 |
| all | 7–10 (8.6) | 39 | 11,169 | 0.362 / 0.319 | 1.13 [0.78, 1.43] | 0.96 [0.73, 1.17] | 23 / 14 |
| forest | 0.5–2 (1.4) | 38 | 14,695 | 0.164 / 0.635 | 0.26 [0.14, 0.35] | 0.17 [0.10, 0.23] | 37 / 0 |
| forest | 2–3.5 (2.8) | 18 | 7,713 | 0.294 / 0.642 | 0.46 [0.31, 0.62] | 0.43 [0.29, 0.57] | 18 / 0 |
| forest | 3.5–5 (4.0) | 21 | 5,483 | 0.158 / 0.577 | 0.27 [0.12, 0.50] | 0.42 [0.22, 0.64] | 17 / 3 |
| forest | 5–7 (6.0) | 13 | 2,289 | 0.233 / 0.528 | 0.44 [0.15, 0.81] | 0.55 [0.25, 0.85] | 9 / 3 |
| forest | 7–10 (8.4) | 5 | 1,147 | 0.428 / 0.247 | 1.73 [0.40, 2.29] | 1.10 [0.34, 2.06] | 3 / 2 |
| shrub | 0.5–2 (1.2) | 33 | 6,888 | 0.160 / 0.551 | 0.29 [0.18, 0.43] | 0.26 [0.14, 0.40] | 31 / 2 |
| shrub | 2–3.5 (2.5) | 29 | 4,809 | 0.422 / 0.489 | 0.86 [0.43, 1.14] | 0.54 [0.40, 0.68] | 23 / 5 |
| shrub | 3.5–5 (4.2) | 21 | 3,263 | 0.341 / 0.430 | 0.79 [0.45, 1.15] | 0.77 [0.49, 1.09] | 12 / 7 |
| shrub | 5–7 (5.9) | 21 | 4,394 | 0.411 / 0.509 | 0.81 [0.57, 1.10] | 0.72 [0.50, 0.94] | 15 / 6 |
| shrub | 7–10 (8.2) | 9 | 1,350 | 0.350 / 0.254 | 1.38 [0.44, 2.89] | 0.96 [0.54, 1.79] | 6 / 3 |
| grass | 0.5–2 (1.3) | 16 | 1,455 | 0.214 / 0.408 | 0.53 [0.21, 0.75] | 0.39 [0.16, 0.69] | 14 / 1 |
| grass | 2–3.5 (2.4) | 15 | 3,002 | 0.528 / 0.334 | 1.58 [0.75, 1.98] | 0.94 [0.64, 1.36] | 9 / 5 |
| grass | 3.5–5 (4.2) | 7 | 1,032 | 0.454 / 0.255 | 1.78 [1.27, 6.13] | 1.94 [1.19, 3.04] | 1 / 5 |
| grass | 5–7 (6.1) | 14 | 1,287 | 0.293 / 0.360 | 0.81 [0.31, 1.44] | 0.95 [0.48, 1.50] | 9 / 5 |
| grass | 7–10 (8.0) | 12 | 1,345 | 0.430 / 0.351 | 1.22 [0.61, 1.78] | 1.10 [0.87, 1.28] | 5 / 4 |

- The canopy-cover split says the same: cover ≥ 40 %: 0.26, 0.45, 0.24, 0.36 up to 7 years; cover < 10 %: 0.38
  under 2 years, then 1.05, 1.35, 0.93.
- Severity in forest: high canopy loss 0.17 [0.05, 0.25] under 2 years (15 fires) against 0.27 [0.14, 0.38] for
  low loss (37 fires); at 2–3.5 years 0.62 against 0.42. Too few fires beyond that. No clear effect of severity.
- Scars first burned in 2015–2016 ("unknown"): 0.57, 0.60, 0.65, 0.73, 1.02 by age class.

By period of the later fire (pooled ratio, strata with at least 5 fires):

| Vegetation | Scar age | 2017–2021 | 2022–2024 | 2025–2026 |
|---|---|---|---|---|
| all | < 3.5 | 0.53 [0.35, 0.77] (84 fires) | 0.28 [0.11, 0.55] (26) | 0.34 [0.06, 0.77] (16) |
| all | 3.5–10 | 0.62 [0.41, 0.81] (39) | 0.89 [0.64, 1.17] (46) | 0.95 [0.62, 1.22] (38) |
| forest | < 3.5 | 0.38 [0.28, 0.49] (27) | 0.07 [0.02, 0.12] (16) | 0.04 [0.00, 0.06] (6) |
| forest | 3.5–10 | 0.21 [0.04, 0.56] (6) | 0.49 [0.23, 0.76] (17) | 0.62 [0.13, 1.38] (10) |
| shrub | < 3.5 | 0.53 [0.28, 0.81] (39) | 0.66 [0.21, 1.13] (11) | 0.11 [0.04, 0.21] (8) |
| shrub | 3.5–10 | 0.57 [0.30, 0.94] (7) | 0.99 [0.71, 1.39] (15) | 0.79 [0.48, 1.24] (23) |
| grass | < 3.5 | 1.16 [0.47, 1.66] (18) | 0.92 [0.12, 2.12] (6) | 0.19 [0.02, 0.66] (5) |
| grass | 3.5–10 | no fires | 1.45 [1.06, 2.04] (10) | 0.93 [0.38, 1.49] (21) |
| all | 7–10 | none | 0.71 [0.26, 1.24] (16) | 1.41 [1.06, 1.75] (23) |

Young scars protect in every period. Old scars protected in 2017–2021 (when "old" meant at most 6 years, the record
starting in 2015) and no longer do on average in 2022–2026; forest scars are the exception.

## Test 2: ignitions inside scars, observed large fires against expected without the modifier

Expected = sum of the model's probabilities with the modifier taken out (for 2016–2024 approximately: the
out-of-year predictions carry a β fitted per held-out year, removed here with the overall β). Intervals: exact
Poisson for the observed count.

| Period | Vegetation | Scar age | Ignitions | Observed / expected | Ratio [95 %] |
|---|---|---|---|---|---|
| 2016–2026 | all | 0–2 | 632 | 0 / 12.6 | 0.00 [0.00, 0.29] |
| 2016–2026 | all | 2–3.5 | 511 | 2 / 5.8 | 0.34 [0.04, 1.24] |
| 2016–2026 | all | 3.5–5 | 796 | 5 / 9.3 | 0.54 [0.18, 1.26] |
| 2016–2026 | all | 5–7 | 594 | 4 / 5.8 | 0.69 [0.19, 1.76] |
| 2016–2026 | all | 7–10 | 468 | 9 / 5.7 | 1.58 [0.72, 3.00] |
| 2016–2026 | forest | < 3.5 | 337 | 1 / 7.9 | 0.13 [0.00, 0.71] |
| 2016–2026 | forest | 3.5–10 | 574 | 1 / 5.8 | 0.17 [0.00, 0.95] |
| 2016–2026 | shrub | < 3.5 | 314 | 0 / 4.2 | 0.00 [0.00, 0.88] |
| 2016–2026 | shrub | 3.5–10 | 371 | 3 / 5.3 | 0.57 [0.12, 1.66] |
| 2016–2026 | grass | < 3.5 | 154 | 1 / 2.1 | 0.48 [0.01, 2.65] |
| 2016–2026 | grass | 3.5–10 | 235 | 3 / 2.3 | 1.33 [0.27, 3.89] |
| 2016–2026 | other | 3.5–10 | 182 | 4 / 0.8 | 4.88 [1.33, 12.5] |
| 2016–2026 | unknown | 3.5–10 | 496 | 7 / 6.6 | 1.06 [0.43, 2.19] |
| 2016–2026 | cover ≥ 40 % | 3.5–10 | 384 | 1 / 4.1 | 0.25 [0.01, 1.38] |
| 2016–2026 | cover < 10 % | 3.5–10 | 443 | 8 / 5.1 | 1.56 [0.67, 3.08] |
| 2025–2026 | forest | 3.5–10 | 350 | 1 / 2.8 | 0.36 [0.01, 2.02] |
| 2025–2026 | shrub | 3.5–10 | 190 | 1 / 2.4 | 0.42 [0.01, 2.32] |
| 2025–2026 | grass | 3.5–10 | 127 | 3 / 1.1 | 2.70 [0.56, 7.90] |
| 2025–2026 | other | 3.5–10 | 91 | 4 / 0.4 | 9.87 [2.69, 25.3] |
| 2025–2026 | cover < 10 % | 3.5–10 | 223 | 7 / 2.3 | 3.03 [1.22, 6.25] |
| 2025–2026 | all | < 3.5 | 83 | 0 / 1.0 | 0.00 [0.00, 3.81] |
| 2016–2024 | forest | 3.5–10 | 224 | 0 / 3.1 | 0.00 [0.00, 1.20] |
| 2016–2024 | unknown | 3.5–10 | 390 | 6 / 4.9 | 1.24 [0.45, 2.69] |

- Forest scars of 3.5–10 years: 1 large fire in 574 ignitions over both periods, 5.8 expected.
- The excess in 2025–2026 sits on grass and "other" land: 7 large fires, 1.5 expected. On "other" land (cells
  whose dominant LF 2016 code is not a vegetated fuel model) the model itself expects too little, scar or not.

## Test 3: forms of the odds modifier, fitted on 2016–2024, judged on 2025–2026

947 ignitions in scars in 2025–2026, 10 large. Log loss inside scars; expected = summed probability inside scars.

| Form | Parameters (fit 2016–2024) | Log loss 2025–2026 | Expected in scars (10 observed) | Observed / expected, scars 3.5–10 y | Whole model PR-AUC / ROC-AUC | Change against no modifier: PR-AUC, ROC-AUC | Log loss 2016–2024 (in sample) |
|---|---|---|---|---|---|---|---|
| (n) none | | 47.72 | 9.4 | 10 / 8.4 | 0.0585 / 0.8392 | | 64.74 |
| (a) current, fade 2 y | β 6.18 | 48.52 | 6.5 | 10 / 6.4 | 0.0583 / 0.8356 | −0.0002 [−0.0024, 0.0014], −0.0036 [−0.0136, 0.0034] | 49.49 |
| (b) truncated at 3.5 y, fade 1 y | β 11.7 | **46.91** | 8.6 | 10 / 8.4 | 0.0590 / 0.8406 | +0.0005 [0.0001, 0.0011], +0.0014 [0.0009, 0.0021] | 52.39 |
| (c) by vegetation, fade 2 y | β forest 6.10, rest 6.24 | 48.55 | 6.5 | 10 / 6.4 | 0.0583 / 0.8355 | −0.0002, −0.0037 | 49.49 |
| (d) non-forest only, fade 2 y | β 6.24 | 49.59 | 7.6 | 10 / 7.4 | 0.0580 / 0.8330 | −0.0005, −0.0062 | 56.68 |

- Rule (written in advance): lowest log loss on 2025–2026 is (b); (n) is within 1.0 of it, so the two are tied;
  the tie-break takes the form that lowers risk least: (n). The rule therefore does not separate "young scars
  only" from "no modifier" on ignitions, for lack of young scars in 2025–2026.
- Forms (a), (c) and (d), which lower risk in scars older than 3.5 years regardless of vegetation, are worse than
  no modifier on 2025–2026.
- Fitted on 2016–2026 the parameters barely move for (a) (β 5.2) and (b) (β 12, the bound); for (c) they split:
  forest 7.2, rest 4.4.

Exploratory, defined after tests 1 and 2 (same fit and evaluation):

| Form | Parameters (fit 2016–2024) | Log loss 2025–2026 | Expected in scars | By vegetation, observed / expected: forest, shrub, grass, other, unknown | PR-AUC / ROC-AUC | Log loss 2016–2024 |
|---|---|---|---|---|---|---|
| (b) | β 11.7 | 46.91 | 8.6 | 1/2.8, 1/2.4, 3/1.2, 4/0.4, 1/1.7 | 0.0590 / 0.8406 | 52.39 |
| (e) = (b) + forest scars keep exp(−β_f·exp(−years/3)) up to 7 y | β 12, β_f 3.4 | **45.88** | 7.6 | 1/1.8, 1/2.4, 3/1.2, 4/0.4, 1/1.7 | 0.0594 / 0.8429 | 50.61 |
| (f) = (b) + forest and shrub scars keep it | β 10.8, β_w 4.4 | 45.62 | 6.7 | 1/1.6, 1/1.8, 3/1.2, 4/0.4, 1/1.7 | 0.0591 / 0.8447 | 49.64 |
| (g) = (b) for forest, shrub and unknown only | β 12 | 47.19 | 8.8 | 1/2.8, 1/2.4, 3/1.4, 4/0.4, 1/1.7 | 0.0589 / 0.8401 | 53.09 |

(e) and (f) improve on (b) by about 1.0–1.3 in log loss on 2025–2026. (f) lowers risk more (6.7 expected against
10 observed) on thinner evidence for shrub (perimeter ratio 0.79–0.81 at 3.5–7 years, intervals reaching 1.1), so
(e) is the cautious one of the two.

## Test 4: spread permeability by vegetation

Pooled ratios of test 1 against mean scar age, fitted as 1 − exp(−years / τ) (weights: square root of the number
of fires; ratios above 1 counted as 1):

| Vegetation | τ (years) | Observed at 1.3 / 2.6 / 4.2 / 6 / 8.3 y | Fitted |
|---|---|---|---|
| all | 3.1 | 0.33 / 0.74 / 0.63 / 0.81 / 1.13 | 0.35 / 0.58 / 0.75 / 0.85 / 0.94 |
| forest | 7.1 | 0.26 / 0.46 / 0.27 / 0.44 / 1.73 (5 fires) | 0.18 / 0.33 / 0.43 / 0.57 / 0.69 |
| shrub | 2.3 | 0.29 / 0.86 / 0.79 / 0.81 / 1.38 | 0.42 / 0.67 / 0.84 / 0.92 / 0.97 |
| grass | 1.3 | 0.53 / 1.58 / 1.78 / 0.81 / 1.22 | 0.63 / 0.84 / 0.96 / 0.99 / 1.00 |

Today's single curve (τ 3.5) is right on average and wrong by type: it understates how long forest scars resist
and overstates how long grass does.

## Recommendation for `ml.fuelage`

Vegetation before the fire, available for table rows and map cells alike: the FBFM40 fuel model of the earliest
LANDFIRE release staged on the AOI (LF 2016), mode of the 200 m cell; `forest` = TU, TL, SB. Where the cell first
burned before 2017 (the earliest fire recorded at it started before 2017-01-01) the class is unknown and counts as
not forest.

```python
def odds(years, forest):
    # scars younger than 3.5 years: any vegetation. Forest scars keep a slower-fading part up to 7 years.
    y = np.asarray(years, "float64")
    m = np.where(y < 3.5, np.exp(-12.0 * np.exp(-y)), 1.0)
    return np.where(forest & (y < 7), np.minimum(m, np.exp(-3.4 * np.exp(-y / 3))), m)

PERMEABILITY_TAU = {"forest": 7.0, "shrub": 2.3, "grass": 1.3, None: 3.1}
def permeability(years, tau):
    y = np.asarray(years, "float64")
    return np.where(y >= 10, 1.0, np.clip(1 - np.exp(-y / tau), 0.1, 1.0))
```

Multipliers of the odds: 0.01 at 1 year, 0.20 at 2, 0.55 at 3, 1 from 3.5 on; in former forest 0.41 at 4 years,
0.53 at 5, 0.63 at 6, 1 from 7 on. β = 12 is the bound of the fit (no large fire in 583 ignitions under 2 years in
2016–2024); β_f = 3.4 fitted on 2016–2024, 5.0 on 2016–2026. Cautious variant: the first line only (form (b)).

## Limits

- 20 large fires inside scars in eleven years; every ignition-level interval is wide. The perimeter test has the
  power (39–79 fires per age class, thousands of cells) and carries the conclusions about vegetation.
- The record of perimeters starts in 2015: "no recorded fire" includes land burned before, and scars of 7–10 years
  exist only for later fires of 2022–2026, mostly with unknown vegetation before the fire.
- Fire edges also follow suppression, and fresh scars serve as control lines; the fading with age and the
  difference between vegetation types are what speaks for fuel.
- Vegetation is the mode of a 200 m cell of a 2016 fuel map; mixed cells are assigned to one class.
- The forest part of the recommended modifier was specified after looking at the same data it is evaluated on.
- 2016–2024 expectations without the modifier are approximate (see Test 2).
