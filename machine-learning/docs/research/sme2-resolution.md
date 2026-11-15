# Is the 200 m information in NISAR SME2 signal or noise?

Run 2026-11-15 on the lake view `~/spaceapps/ml-work/lake` (read only). Scripts are in this directory.

**Answer.** The fine-scale part of SME2 (a cell's temporal anomaly minus the 9 km mean of that anomaly) is a
repeatable signal, not noise: two independent tracks, 5 to 7 days apart and at different incidence angles, agree at
r = 0.35 at 200 m and 0.39 at 600 m (0.46 and 0.50 where the product flags the retrieval as recommended). It is
almost as repeatable as the 9 km mean itself (0.48). It is weaker in the Santa Monica Mountains chaparral (0.22), it
fades within 12 to 24 days, and nothing here shows that it is soil moisture: there is no ground truth in the lake.
It predicts the later change of the Sentinel-2 moisture index better than terrain and season alone, but adds
nothing once the current Sentinel-2 state is known.

## Data

- `nisar-sme2` on `ca` (EPSG:6933, 200 m): 226 scenes, 2025-11-01 to 2026-05-24, six descending tracks (013, 042,
  085, 114, 143, 172), 16 track-frames, 8 to 17 scenes each. The download was still running; June to October 2026
  was not staged. Only the DSG algorithm is staged on `ca` (`soil_moisture` equals `soil_moisture_dsg`).
- `nisar-sme2` on `smm`: 25 scenes of track 013, 2025-11-23 to 2026-10-13, upsampled to 30 m; sampled back at the
  centres of the native 200 m cells (r = 0.997 against the `ca` item of the same day). 116 x 215 cells, about
  13,100 burnable land cells. PMI is present in 19 scenes, TSR in 21.
- Values at the retrieval bounds (0.02 or 0.60 m3/m3) were dropped everywhere: 10.3 % of valid cells statewide, up
  to 37 % in frame 085_071.
- Adjacent tracks see the same cell 5 days apart (178 scene pairs) or 7 days apart (41).

Definitions. Temporal anomaly `a` = value minus the mean of the same cell over scenes of the same track.
Fine-scale term `f_w` = mean of `a` over a w x w window minus the mean over 45 x 45 cells (9 km). All r values are
Pearson over cells and scene pairs; intervals are a bootstrap over 10 km blocks (5 km in SMM).

## 1. Agreement of independent observations

### 1a. Between tracks, statewide (31 M cell pairs, 1,149 blocks, 219 scene pairs)

| Window | all cells | recommended | not recommended | SNR = r / (1 - r), all |
|---|---|---|---|---|
| 200 m (1 cell) | 0.354 [0.329, 0.375] | 0.460 [0.434, 0.482] | 0.227 [0.217, 0.238] | 0.55 |
| 600 m (3) | 0.391 [0.366, 0.415] | 0.500 [0.471, 0.523] | 0.258 [0.246, 0.271] | 0.64 |
| 1 km (5) | 0.384 [0.357, 0.410] | 0.492 [0.461, 0.516] | 0.256 [0.241, 0.271] | 0.62 |
| 2 km (11) | 0.357 [0.331, 0.383] | 0.456 [0.422, 0.489] | 0.246 [0.231, 0.263] | 0.56 |
| 3 km (15) | 0.347 [0.318, 0.377] | 0.441 [0.408, 0.470] | 0.243 [0.226, 0.261] | 0.53 |

- Averaging beyond 600 m does not help: the disagreement is spatially correlated, not cell noise. 600 m is the best
  window; 200 m loses about 10 % of the agreement.
- For comparison, on the same pairs: the 9 km mean of the anomaly agrees at 0.477 [0.457, 0.494]; the unfiltered
  anomaly at 0.409.
- Gap without rain (< 0.5 mm): 0.33 to 0.36. Gap with at least 5 mm: 0.37 to 0.43.
- By month (median over scene pairs, 200 m / 1 km): Nov 0.52 / 0.64, Dec 0.40 / 0.56, Jan 0.39 / 0.48,
  Feb 0.30 / 0.41, Mar 0.38 / 0.54, Apr 0.27 / 0.35, May 0.46 / 0.72.
- The time-mean map (static fine-scale pattern) agrees at 0.89 to 0.91, but both tracks share the retrieval's static
  ancillary inputs, so this is not independent evidence.
- Static offset between tracks at one cell: mean absolute 0.015, sd 0.023 m3/m3, as large as the temporal anomaly
  itself (sd 0.022). Raw values of different tracks must never be mixed.

### 1a. Between tracks, Santa Monica Mountains (track 013 against 114, 13,373 cells, about 180,000 cell pairs)

| Stratum | 200 m | 600 m | 1 km | 2 km |
|---|---|---|---|---|
| all | 0.219 [0.186, 0.251] | 0.227 [0.174, 0.281] | 0.214 [0.145, 0.285] | 0.158 [0.067, 0.251] |
| shrub | 0.217 | 0.224 | 0.200 | 0.125 |
| grass and grass-shrub | 0.254 | 0.255 | 0.247 | 0.211 |
| timber (n = 3,607) | 0.178 | 0.139 | 0.101 | 0.063 |
| non-burnable | 0.187 | 0.240 | 0.261 | 0.237 |
| slope < 8 deg | 0.151 | 0.163 | 0.172 | 0.166 |
| slope 8 to 20 deg | 0.197 | 0.213 | 0.200 | 0.144 |
| slope > 20 deg | 0.260 | 0.263 | 0.244 | 0.176 |
| recommended | 0.242 | 0.238 | 0.229 | 0.205 |
| not recommended | 0.207 | 0.221 | 0.206 | 0.136 |

In this chaparral area the agreement is about half the statewide value and does not depend much on cover or slope.

### 1b. Between algorithms (SMM, same backscatter, so the noise is shared)

| Pair | 200 m | 600 m | 1 km |
|---|---|---|---|
| DSG, TSR | 0.567 | 0.559 | 0.548 |
| DSG, PMI | 0.238 | 0.258 | 0.255 |
| PMI, TSR | 0.310 | 0.337 | 0.337 |

The three algorithms do not agree on the fine-scale dynamics. PMI differs most.

### 1c. Persistence on the same track (statewide, 100 M cell pairs, 3,600 blocks)

| Lag | 200 m | 600 m | 1 km | recommended, 600 m | not recommended, 600 m |
|---|---|---|---|---|---|
| 12 days | 0.296 | 0.322 | 0.317 | 0.421 | 0.164 |
| 24 days | 0.111 | 0.132 | 0.131 | | |
| 36 days | 0.080 | 0.086 | 0.083 | | |
| 48 days | 0.076 | 0.091 | 0.099 | | |

White noise would give -0.08 (mean removal over 13.6 scenes). The term has a memory of 12 to 24 days.
In SMM over the full year (track 013, 21 intervals): mean 12-day autocorrelation 0.46 in November to May and 0.34 in
June to October, from -0.43 to +0.91 between intervals; it turns negative in and right after intervals with rain
(-0.14, -0.19, -0.43, -0.43).
The scene of 2026-06-03 is an outlier (fine-term sd 0.043 against a typical 0.006).

## 2. Does the fine-scale term say anything about vegetation? (SMM, 600 m, burnable land)

Target: fine-scale change of Sentinel-2 NDMI between the SME2 day and L days later. LightGBM, 5 folds over 36
spatial blocks of 5 km, 21 SME2 dates, 209,000 to 262,000 rows per lead. Values are R2.

| Inputs | 12 d | 24 d | 36 d | 60 d |
|---|---|---|---|---|
| terrain, cover, season | 0.095 | 0.128 | 0.136 | 0.147 |
| + SME2 (causal anomaly, 12-day change) | 0.118 | 0.160 | 0.176 | 0.206 |
| gain of SME2 over terrain | +0.023 [0.005, 0.042] | +0.032 [0.009, 0.056] | +0.040 [0.018, 0.067] | +0.059 [0.029, 0.093] |
| terrain, cover, season, NDMI and NDVI state | 0.312 | 0.448 | 0.547 | 0.712 |
| + SME2 | 0.308 | 0.450 | 0.552 | 0.719 |
| gain of SME2 over the Sentinel-2 state | -0.004 [-0.013, 0.007] | +0.002 [-0.006, 0.011] | +0.005 [-0.003, 0.013] | +0.007 [0.001, 0.013] |
| gain of SMAP (9 km, bilinear) over the Sentinel-2 state | +0.009 [0.001, 0.019] | +0.004 [-0.004, 0.012] | +0.004 [-0.004, 0.010] | +0.004 [-0.001, 0.008] |
| SME2 alone | 0.035 | 0.053 | 0.060 | 0.070 |

- Correlation of the causal SME2 term with the later NDMI change: 0.08, 0.11, 0.11, 0.23 at 12, 24, 36, 60 days;
  partial correlation given the Sentinel-2 state: 0.04, 0.06, 0.05, 0.13.
- Shrub and grass cells behave alike (60 days: 0.703 to 0.712 for shrub, 0.698 to 0.701 for grass and grass-shrub).
- With the full-year anomaly, the correlation between the SME2 term at t and the NDMI anomaly at t + L rises from
  0.02 (L = 0) to 0.27 (L = 60 days).
- SMAP has no within-area detail; its small gain comes from acting as a date variable.

So the term carries information about where vegetation will gain or lose moisture, beyond terrain, and more at
longer leads. Sentinel-2 already contains it. Caveat: if the retrieval uses an optical vegetation correction, part
of this is not independent of Sentinel-2.

## 3. Response to rain (SMM, 21 twelve-day intervals, 4 with at least 10 mm, 14 dry)

- Similarity of the fine-scale change pattern between events: wetting with wetting 0.26 (sd 0.41, 5 pairs); drying
  with drying 0.00 (83 pairs); wetting with drying -0.08.
- Terrain and cover explain 12 % of the fine-scale 12-day change on average (block CV, from -0.02 to 0.32), but the
  sign of the relation with elevation, slope and aspect changes from event to event.
- The same 12-day change seen by track 114 agrees with track 013 at -0.15 to +0.41 (8 intervals, mean 0.11).

There is no repeatable, terrain-dependent wetting or drying pattern in these data.

## 4. Normalisation

Cross-track r of the fine-scale term, statewide, all scene pairs:

| Anomaly | 200 m | 600 m | 1 km |
|---|---|---|---|
| minus mean of all scenes of the track | 0.354 | 0.391 | 0.384 |
| minus mean of the earlier scenes only (causal) | 0.349 | 0.388 | 0.382 |
| z-score per cell | 0.226 | 0.311 | 0.320 |
| rank within the cell | 0.224 | 0.363 | 0.390 |
| difference from the previous pass (12 days) | 0.163 | 0.192 | 0.193 |

How many earlier scenes are needed (evaluation scenes with at least 8 earlier scenes on both tracks, 11 M pairs):

| Reference | 200 m | 1 km |
|---|---|---|
| last 1 scene | 0.128 | 0.139 |
| last 2 | 0.176 | 0.186 |
| last 3 | 0.205 | 0.218 |
| last 4 | 0.228 | 0.243 |
| last 6 | 0.283 | 0.304 |
| last 8 | 0.305 | 0.326 |
| all earlier | 0.326 | 0.342 |
| all scenes, including later ones | 0.288 | 0.301 |
| z-score against earlier scenes | 0.150 | 0.187 |

- The causal mean is as good as the full-period mean. Per-cell z-scores and ranks are worse at 200 m.
- A two-pass difference is 2.5 times less repeatable than a reference of 8 scenes. Six scenes (72 days) is the
  practical minimum.
- Anomalies must be formed per track (section 1a: static offset between tracks).
- The term as recommended below (causal, at least 6 earlier scenes, 15.6 M pairs): 0.348 / 0.377 / 0.371 at
  200 m / 600 m / 1 km; recommended cells 0.450 / 0.481 / 0.471; not recommended 0.221 / 0.248 / 0.247.
- Clipping to +-0.03 m3/m3 raises the agreement at 600 m from 0.377 to 0.457; +-0.02 gives 0.470.
- Averaging the term over two consecutive passes of a track (24 days) raises it from 0.313 to 0.507 at 600 m (9 M pairs).

## 5. Quality flag

- Not recommended (bit 0 set): 44.7 % of valid cells statewide; by frame from 16 % (085_071) to 99 % (172_067,
  north coast forest). SMM: 62.6 % not recommended; recommended share is 29 % in shrub, 69 % in grass and
  grass-shrub, 1 % in timber.
- Recommended cells are about twice as repeatable as the others (sections 1a, 1c). In SMM the difference is small
  (0.24 against 0.21).

## Scale against SMAP (9 km blocks, 67,248 block-dates, 4,924 blocks, November to May)

- The 9 km mean SME2 anomaly follows the SMAP L4 surface anomaly in time: median r per block 0.68 (IQR 0.40 to
  0.84), positive in 87 % of blocks; pooled r 0.48.
- Its amplitude is much smaller: slope 0.16 m3/m3 of SME2 per m3/m3 of SMAP (0.19 where at least 70 % of cells are
  recommended). Standard deviations: SME2 9 km anomaly 0.017, SMAP surface 0.052, SMAP root zone 0.035.
- Level: SME2 is 0.049 m3/m3 above SMAP surface on average, with an sd of 0.079 between blocks.
- Size of the fine-scale term: sd 0.0165 at 200 m, 0.0122 at 600 m; 1st and 99th percentiles at 600 m -0.036 and
  +0.036.

## Recommended fine-scale term for the map

For a map day D and a 200 m cell:

1. **Scenes.** Per track, keep values strictly inside (0.02, 0.60). Drop a scene whose fine-term sd exceeds three
   times the median of its track.
2. **Anomaly.** `a` = value minus the mean of the earlier scenes of the same track at that cell. Require at least 6
   earlier scenes. No per-cell z-score. Never pool tracks before this step.
3. **Fine-scale part.** `f` = mean of `a` over 3 x 3 cells minus mean over 45 x 45 cells, with at least 50 % and 25 %
   valid coverage.
4. **Time.** Average `f` over the passes of the last 24 days (two per track, all tracks). The newest pass must be at
   most 12 days old; otherwise set the term to zero.
5. **Range.** Clip `f` to +-0.03 m3/m3.
6. **Units and weight.** `z_fine` = rho x `f` / sigma_c, where sigma_c is the temporal sd of the 9 km mean SME2
   anomaly at that place (0.017 statewide; floor 0.010) and rho is the measured reliability: 0.5 for recommended
   cells, 0.25 for the others, 0.2 in rugged chaparral such as SMM. Clip `z_fine` to +-1.
7. **Combination.** z = z of SMAP root zone (9 km) + `z_fine`. Where step 1 to 4 cannot be met, z = SMAP only.

With these numbers a typical `z_fine` is about 0.3; with a coefficient of -0.7 per z it changes the risk by a factor
of about 0.8 to 1.2, and never by more than a factor of 2.

## What can and cannot be claimed

Can be claimed:
- The 200 m term is repeatable between independent tracks (0.38 at 600 m, 0.48 to 0.50 in recommended cells) and
  nearly as repeatable as the 9 km mean (0.48).
- It needs a reference of at least 6 passes; with 8 it reaches its ceiling.
- It predicts the later fine-scale change of vegetation moisture better than terrain and season alone.

Cannot be claimed:
- That it is soil moisture. No in-situ data; the three algorithms disagree; the amplitude is one sixth of SMAP's.
- That it adds to Sentinel-2 for vegetation state.
- That it holds in summer statewide: cross-track data end in May. Only same-track persistence in SMM covers June to
  October.
- That it shows terrain-driven wetting and drying.
- Any link to fire outcomes at fine scale.

## Notes outside this task

- `ml.grid.nisar_change` uses the difference between two passes minus the SMAP change. The two-pass difference is
  the least repeatable form measured here (0.13 to 0.19). SME2 changes are about one sixth of SMAP's, so subtracting
  the SMAP change mostly adds minus 0.84 times SMAP's own change to the map. Subtracting SME2's own 9 km mean avoids
  both.
- `ml.grid` accepts values from 0 to 0.7; values at 0.02 and 0.60 are retrieval bounds and should be masked.

## Files

| File | Content |
|---|---|
| `common.py` | grid constants, window means, block sums, bootstrap, gridMET rain |
| `a1_index.py`, `items.parquet`, `items/` | index and cropped arrays of the `ca` scenes |
| `a2_xtrack.py`, `a2b_report.py`, `xtrack.pkl`, `a2_report.txt` | cross-track agreement, persistence, normalisation, flags |
| `a5_nscenes.py`, `nscenes.pkl`, `a5.log` | number of earlier scenes |
| `a8_spec.py`, `spec.pkl` | the term as recommended, clipping, two-pass average |
| `a6_coarse.py`, `coarse.pkl` | SME2 against SMAP at 9 km, size of the term |
| `a3_smmcube.py`, `smm_cube.npz` | SMM cube on native cells |
| `a4_smm.py`, `a4.log`, `smm_tests.json` | SMM: cover and slope, algorithms, NDMI, rain |
| `a7_season.py`, `season_persistence.csv` | SMM persistence through the year |
