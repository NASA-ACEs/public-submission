# The model

What is predicted, how the parts fit, what each part rests on, and where NISAR enters. Method details and limits:
`docs/METHODS.md`. Every number below was measured on data the model did not see; the studies are in
`docs/research/`, the numbers of each model version in `docs/versions/`.

## What it answers

For every cell and day: **if a fire started here today, how likely is it to become a large fire (≥ 400 ha), and
how would it spread?** It does not predict where fires start.

## Structure

```
p_large_fire = fuel_age( calibrate( mean of 5 boosted-tree models (
                   weather of the day, HRRR gusts, terrain, fuel model, vegetation type of the cell,
                   the surroundings of the cell within 1 and 3 km (buildings, fuel continuity, relief),
                   dryness of the cell against its own last 12 months  + NISAR SME2 fine-scale term,
                   antecedent rain and VPD ) ),
               years since the last fire, vegetation before it )

risk_index   = rank of p_large_fire among the predictions for 2016–2024 ignitions on open land

susceptibility_index = the same rank with the weather of an ordinary ignition day: what the land contributes
weather_factor       = odds of the day / odds of that ordinary day
```

Next to it, on the same grid and not an input of `p_large_fire`:

- **LFMC** with a 90 % interval (weather, soil moisture, season, rain sums);
- **head-fire spread**: rate of spread, flame length, fireline intensity and direction from Rothermel's surface
  model for the LANDFIRE fuel model, with LFMC as live woody moisture and woody loads scaled by the GCOV fuel state;
- **spread permeability**: how likely a cell is to burn when a large fire reaches it, from its fuel age;
- **fire growth from a point** (`ml.spreadsim`): the area reached after 1, 3, 6 hours for any wind, by minimum
  travel time over those head-fire ellipses, slowed by fuel age. A potential for direction and shape, not a
  predicted size: it ranks large fires worse than `p_large_fire` (AUC 0.74 against 0.83).

## What each part rests on

| Part | Evidence (unseen data) | Source |
|---|---|---|
| Weather of the day | PR-AUC 0.021 against 0.014 for the gridMET burning index and 0.0064 for a random ranking | `fire-model.md` |
| HRRR gusts and maximum wind (3 km) | carry what the daily mean wind does not: odds ratio 1.35 [1.22, 1.49] per standard deviation with the model's prediction controlled; ROC-AUC +0.019 [0.012, 0.028] in unseen years and +0.031 [0.011, 0.055] on 2025–2026; PR-AUC gain positive, interval includes zero | `wind.md`, `versions/v3.md` |
| Terrain and fuel model, as means over 200 m cells | the one large, certain step: +0.043 [0.024, 0.060] PR-AUC in v2. Cell means instead of the pixel at the point: LFMC RMSE −1.19 [−1.72, −0.61], fire +0.010 [−0.003, 0.021] | `fire-model.md`, `ca-statics.md`, `versions/v2.md` |
| Vegetation type of the cell (16 LANDFIRE EVT shares, 200 m) | accepted by a rule written before the test on 2025–2026: ROC-AUC +0.028 [0.011, 0.047] for fires ≥ 400 ha, PR-AUC +0.013 [0.007, 0.018] for fires ≥ 4 ha; unseen years ROC-AUC +0.0065 [0.0022, 0.0117], PR-AUC +0.0009 [−0.004, 0.008]. The built-up share is the largest single input: ignitions in cells that are mostly built up (more than half of all) become large fires one-eleventh as often as those on land with no buildings | `vegetation-type-out-of-time.md`, `versions/v3.2.md` |
| The surroundings of the cell (17 means over discs of 1 and 3 km, terrain roughness and position) | by a rule written before the run: PR-AUC +0.011 [0.0002, 0.021] and ROC-AUC +0.017 [0.011, 0.025] in unseen years (the first change the regression test calls "better"); on 2025–2026 ROC-AUC +0.030 [0.011, 0.049], large fires in the top 10 % of ignitions from 54 % to 68 %. They hold 41 % of the trees' gain | `landscape-context.md`, `versions/v3.3.md` |
| Small trees, monotone responses, mean of 5 seeds | +0.014 [0.002, 0.031] over the first version's model; one seed alone moves PR-AUC by ±0.004 | `fire-model.md` |
| Dryness against the last 12 months (SMAP root zone) | large fires per quintile, dry to wet: 1.17 / 0.78 / 0.65 / 0.48 / 0.17 %; odds ratio 0.51 [0.32, 0.79] per unit with weather, terrain, fuel and season controlled; as a ranking gain 0.000–0.004, not significant | `2026-11-14-findings.md`, `fire-model.md` |
| Fuel age (years since fire) | no ignition inside a scar younger than 2 years became a large fire in 2016–2026 (632 ignitions, 12.6 expected); cells of such scars burned 0.33 [0.24, 0.43] times as often as neighbouring land in later large fires, also in 2025–2026. Former forest stays protected about 7 years (0.26–0.46), grass and open land less than 2. LANDFIRE fuels do not carry it | `time-since-fire.md`, `fuel-age-by-vegetation.md`, `out-of-time.md` |
| The whole model, two years after training | 2025–2026 ignitions from another source (v3.3): ROC-AUC 0.90 [0.88, 0.93] against 0.63 for the burning index, PR-AUC 0.080 against 0.012; 68 % of the large fires in the top 10 % of ignitions; the two highest classes are 14.5 % of ignitions, 64 of the 80 large fires and 76 % of the area burned; 87 large fires expected, 80 observed. These years have decided two model changes, so they are no longer an untouched test | `out-of-time.md`, `versions/v3.3.md` |
| LFMC | has an effect beyond weather (odds ratio 0.86 per +10 points) but adds nothing to the ranking (−0.0007 [−0.0036, +0.0020]); kept as a map and as live fuel moisture of the spread physics, not as a model input | `fire-model.md` |
| Spread physics | flame length alone ranks large fires 2.3 times better than the burning index; adds nothing on top of terrain and fuel; kept as output layers | `physics.md` |
| Fire growth simulator | direction within 45° of the real perimeter in 45.6 % of 649 fires (wind alone 42.1 %); shape IoU 0.187 against 0.155 for a circle; adds nothing to the ranking | `spread-simulator.md` |

## Where NISAR enters

| Product | Use | What is measured | What is assumed |
|---|---|---|---|
| **SME2** soil moisture, 200 m | fine-scale term added to the SMAP dryness anomalies and to surface soil moisture (`ml.grid.nisar_fine`) | the term is repeatable between independent tracks: r 0.39 at 600 m, 0.50 in "recommended" cells, almost as much as the 9 km mean (0.48); the form used (anomaly from the track's own history) is 2–3 times more repeatable than a two-pass difference | that a fine-scale surface anomaly moves fire risk as the 9 km anomaly does. Pre-registered test on the 2026 fire season: `docs/EXPERIMENTS.md`, N2 |
| **GCOV** L-band backscatter, 10 m | fuel state after fire (`ml.gcov.fuel_state`): scales woody fuel in the spread physics, shown as a layer | L-HV separates regrowth from mature chaparral for at least 7.5 years: −2.0 dB at 7.5 y, −3.1 dB at 1.4 y; scar detection AUC 0.89 against 0.65 for NDVI, 0.58 for the LANDFIRE 2025 fuel load, 0.81 for Sentinel-1 C-band | that HV power scales with woody load; no fire occurred in the area during the GCOV record to test it. Using it to mark unburned islands inside perimeters was tested against Sentinel-2 and failed (`docs/EXPERIMENTS.md`, G2), so it does not change `p_large_fire` |

What NISAR is **not** used for, because the data did not support it: SME2's absolute values or spatial pattern
(wrong sign against fire size), SME2 as a predictor of LFMC (R² 0.03 statewide; the Santa Monica result did not
survive a causal reference), GCOV as fuel moisture (its change follows rain and soil moisture).

## Honest summary

- The ranking skill comes from weather including HRRR gusts, and from what the land is like at and around the
  ignition: relief, fuels, and above all how much of it is built up within a few kilometres. It holds two years
  after training.
- `p_large_fire` is about fire size, not consequence: near buildings a fire is less likely to grow and costs
  more when it does.
- It is an average over the causes of 2016–2024. Among the 761 ignitions recorded as caused by power lines, all
  12 large fires were in the two highest classes (ROC-AUC 0.95 [0.92, 0.98], each year predicted without it), and
  there were twice as many as the model expected (`docs/research/ignition-cause.md`; exploratory, 12 events).
- Fuel age is a real effect in young scars and in former forest; it changes the map where it applies and barely
  moves a statewide score, because few ignitions lie in scars. Dryness against the last 12 months has a clear odds
  ratio and adds nothing significant to the ranking.
- NISAR GCOV gives a fuel-state layer that optical data and the fuel map no longer show.
- NISAR SME2, judged by a test fixed before the fire-season scenes were staged: the anomaly of a pixel against its
  own record separates ignitions that grew from those that did not (AUC 0.55 [0.52, 0.58], better than SMAP's
  anomalies, odds ratio 0.89 [0.82, 0.98] beyond weather and SMAP). The part of it finer than 9 km did not pass
  the resolution test, and fire edges do not follow it. So the claim that stands is "NISAR's soil-moisture anomaly
  carries fire-relevant information that SMAP's does not, in its first season"; the claim that does not is "its
  200 m detail improves the prediction". The bundle keeps the fine-scale term small and carries the whole anomaly
  as experimental layers. With the prediction of v3.3 controlled the anomaly's odds ratio is 0.82 [0.75, 0.88]
  for fires ≥ 4 ha, the same as with v3.1 (0.84): what it carries is not something the model has since learned
  from the land (exploratory, one season).
