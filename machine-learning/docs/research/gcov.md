# NISAR GCOV (L-band HH/HV) for fire-fuel mapping: what it can and cannot do

Study area: Santa Monica Mountains AOI (`smm`, 30 m, EPSG:32611, 837 × 1398 cells, 75 % land).
Run on 2026-11-15 on the fuel-lake data staged at that time. Scripts are next to this file
(`prep.py`, `q1.py`, `q1g.py`, `q2.py`, `q2b.py`, `q2c.py`, `q3.py`, `q4.py`, `common.py`); they read the lake
and write only here.

## Summary

- **GCOV's defensible use is a fuel-state layer.** Time-mean L-band HV separates post-fire regrowth from mature
  chaparral for at least 7.5 years after a fire. Burned pixels are 3.1 dB lower 1.4 years after fire and still
  2.0 dB lower after 7.5 years, against matched unburned pixels.
- **It sees what optical data and LANDFIRE 2025 no longer see.** For the 2018 Woolsey scar the detection AUC is
  0.89 for L-HV, 0.65 for NDVI, 0.58 for LANDFIRE 2025 fuel load and 0.81 for Sentinel-1 C-VH.
- **It is not a fuel-moisture sensor in shrubland.** HV changes in time follow rain and surface soil moisture;
  the link to Sentinel-2 NDMI is weak (fine-scale r 0.08 in shrub, 0.20 in grass).
- **It is a weak stand-alone mapper of LANDFIRE fuel classes** (block-CV R² 0.14–0.21), but adds 0.03–0.09 R² on top
  of optical and terrain inputs.
- **Terrain orientation is a first-order artefact** (2.4–4.0 dB between slopes of opposite orientation) and must be
  removed or matched.
- **Not shown:** biomass calibration, within-year regrowth, burn detection within days, any effect on fire risk.

## Data

| Layer | Content | Used as |
|---|---|---|
| `nisar-gcov` | 47 scenes, 2025-11-23 → 2026-10-20; tracks `t013d071` (25) and `t114d071` (22); γ0 HH, HV in dB at 30 m; PROVISIONAL; no layover/shadow mask | signal |
| `landfire-*` LF2016 / LF2022 / LF2025 | FBFM40 codes, fuel loads, canopy cover/height, slope, aspect, elevation | targets, strata |
| `sentinel2-indices` | 765 scenes, monthly medians 2017-12 → 2026-11 | comparison, balance check |
| `opera-rtc-s1` | 115 Sentinel-1 scenes, 2025-04-09 → 2025-11-22, 4 tracks, VV/VH | C-band comparison |
| `gedi-l4b` | AGBD at 1 km (2019–2023 mission composite) | biomass comparison |
| `nisar-sme2` | 25 scenes of track 13 | dynamics |
| `gridmet` | daily precipitation, AOI mean | dynamics |
| `fire_perimeters` | 99 perimeters intersecting the AOI, 2015–2026 | scars |

Fire perimeters used: Woolsey + Hill (2018-11, 35,400 ha in the AOI), Palisades + Kenneth (2025-01, 9,900 ha),
Franklin (2024-12, 1,660 ha), Palisades (2021-05, 490 ha), Old/Sherwood (2016, 220 ha), Bryson (2026-10-15, 63 ha).
46 % of the burnable AOI burned in Woolsey/Hill; 35 % has no perimeter since 2015 ("unburned" below; it may have
burned before 2015).

## Methods

- **Time means.** Per track, mean γ0 in linear power over all scenes, then dB.
- **Terrain.** `u = sin(slope) · cos(aspect − φ)`, with φ the aspect of maximum brightness found per track inside
  LANDFIRE shrub pixels. "Flattened" HV/HH has the quadratic fit on `u` removed; the two tracks are then averaged in
  linear power.
- **Spatial validation.** 90 m cells (3 × 3 mean), 5-fold cross-validation over ~3 km blocks; gradient boosting,
  30,000–40,000 sampled cells. Nothing is scored on cells of a training block.
- **Scars.** Burned pixels against unburned pixels within 5 km of the perimeter, in the same stratum: LF2016 fuel
  code × slope class (10°, 20°, 30°) × `u` class (5) × elevation tercile, strata with ≥ 30 controls. 95 %
  intervals from a bootstrap over 1.5 km blocks.
- **Dynamics.** Per-cell anomalies from the track's own time mean; monthly means Nov 2025 – Oct 2026; "fine scale" =
  after subtracting the 3 km neighbourhood mean of the same month. Pooled correlations with a block bootstrap.

## 1. Fuel structure

### Terrain artefact (LANDFIRE shrub pixels, time-mean γ0)

| Layer | n | Aspect of maximum brightness | r with `u` | dB per unit `u` | Bright-facing minus opposite (\|u\| > 0.3) | Spatial sd |
|---|---|---|---|---|---|---|
| HV, t013 | 280,633 | 70° | 0.38 | 3.0 | 2.4 dB | 2.2 dB |
| HV, t114 | 234,623 | 90° | 0.57 | 5.0 | 4.0 dB | 2.3 dB |
| HH, t013 | 280,633 | 305° | 0.27 | 2.5 | 2.1 dB | 2.4 dB |
| HH, t114 | 234,620 | 75° | 0.23 | 1.7 | 1.3 dB | 2.0 dB |

The orientation effect is as large as the fire-scar signal. The two tracks differ by −0.6 ± 1.2 dB per pixel in
shrub, and that difference correlates with `u` (r = −0.36). The aspect of maximum brightness was found from the
data; it was not checked against the orbit geometry. East–west trend in unburned shrub: +0.16 to +0.20 dB per 10 km.

### Relation to LANDFIRE, NDVI (burnable 90 m cells, n = 68,352)

Spearman correlation:

| Target | HV, one track, raw | HV, flattened, both tracks | HH, flattened | HV − HH | NDVI |
|---|---|---|---|---|---|
| Canopy cover | 0.38 | 0.38 | 0.34 | 0.08 | 0.45 |
| Canopy height | 0.38 | 0.38 | 0.34 | 0.07 | 0.45 |
| Live woody load | 0.13 | 0.14 | −0.05 | 0.35 | 0.39 |
| Total fuel load | 0.33 | 0.34 | 0.12 | 0.39 | 0.54 |
| Fuel bed depth | 0.24 | 0.24 | 0.07 | 0.32 | 0.09 |
| NDVI (year) / NDVI (Jul–Sep) | 0.51 / 0.59 | 0.49 / 0.59 | 0.32 / 0.38 | 0.32 / 0.37 | – |

Mean by LANDFIRE 2025 fuel group:

| Group | Cells | HV (dB) | HV sd | HH (dB) | HV − HH | NDVI | Canopy cover % |
|---|---|---|---|---|---|---|---|
| GR grass | 6,221 | −15.5 | 2.3 | −9.0 | −6.6 | 0.47 | 1.4 |
| GS grass-shrub | 23,898 | −14.4 | 1.9 | −8.3 | −6.1 | 0.49 | 4.3 |
| SH shrub | 34,524 | −14.0 | 1.6 | −8.2 | −5.7 | 0.60 | 3.5 |
| TL timber litter | 2,108 | −12.7 | 1.8 | −6.5 | −6.2 | 0.67 | 27 |
| TU timber understory | 1,601 | −12.9 | 1.4 | −7.2 | −5.7 | 0.71 | 33 |
| NB non-burnable (mostly urban) | 29,739 | −11.6 | 2.1 | −4.6 | −7.0 | 0.34 | 1.1 |

Groups are ordered as expected (grass < shrub < trees), but they overlap: the group means span 2.8 dB and the
within-group sd is 1.4–2.3 dB. Urban land is the brightest class.

Block-CV R² (30,000 burnable cells):

| Target | GCOV, one track, raw | GCOV flattened | Terrain only | GCOV + terrain | NDVI only | NDVI + terrain | NDVI + terrain + GCOV |
|---|---|---|---|---|---|---|---|
| Canopy cover | 0.13 | 0.14 | 0.02 | 0.20 | 0.26 | 0.29 | 0.39 |
| Canopy height | 0.13 | 0.14 | 0.02 | 0.19 | 0.25 | 0.28 | 0.36 |
| Live woody load | 0.10 | 0.17 | 0.19 | 0.34 | 0.25 | 0.41 | 0.50 |
| Total fuel load | 0.14 | 0.21 | 0.17 | 0.36 | 0.39 | 0.52 | 0.55 |
| NDVI | 0.29 | 0.29 | 0.09 | 0.38 | – | – | – |

Block-CV AUC:

| Task | n | GCOV | NDVI | Terrain | GCOV + NDVI | All |
|---|---|---|---|---|---|---|
| Burnable vs non-burnable | 40,000 | 0.89 | 0.95 | 0.93 | 0.96 | 0.97 |
| Shrub (SH) vs grass (GR + GS) | 26,388 | 0.66 | 0.81 | 0.73 | 0.83 | 0.88 |
| Tree (TU + TL) vs shrub (SH) | 15,602 | 0.72 | 0.77 | 0.68 | 0.84 | 0.86 |

GCOV alone does not reproduce the LANDFIRE fuel map; Sentinel-2 NDVI does it better. GCOV adds 0.03–0.09 R² to
optical plus terrain. LANDFIRE is itself a Landsat-based model, so agreement with an optical index is partly built
in, and a low R² against LANDFIRE is not proof that GCOV is wrong.

### GEDI L4B biomass (1 km cells ≥ 70 % burnable, n = 502, 32 blocks of 5 km)

AGBD mean 35.7 Mg/ha, sd 17.8, median standard error 5.8.

| Predictor | Spearman with AGBD | Block-CV R² (linear) |
|---|---|---|
| L-HV + L-HH (flattened) | 0.32 / 0.39 | 0.09 |
| C-VH (Sentinel-1) | 0.54 | 0.18 |
| NDVI | 0.41 | 0.11 |
| LANDFIRE canopy cover + height | 0.34 | 0.09 |
| NDVI + L | – | 0.18 |
| NDVI + canopy + L | – | 0.20 |

No usable biomass calibration comes out of this. GEDI L4B itself shows no difference between cells burned since
2015 and the rest (35.4 vs 36.4 Mg/ha), so it does not resolve the fire history that GCOV resolves.

## 2. Fire scars and regrowth

### Burned minus matched unburned (time-mean, flattened, both tracks)

| Cohort | Years since fire | Matched pixels (blocks) | HV, dB | HH, dB | HV − HH, dB | NDVI | NDMI |
|---|---|---|---|---|---|---|---|
| Palisades + Kenneth 2025-01 | 1.4 | 90,403 (72) | −3.06 [−3.22, −2.87] | −1.70 [−1.90, −1.50] | −1.36 [−1.44, −1.27] | −0.08 [−0.09, −0.07] | −0.08 |
| Franklin 2024-12 | 1.5 | 15,462 (17) | −3.40 [−3.78, −3.04] | −2.06 [−2.51, −1.71] | −1.33 [−1.45, −1.19] | −0.09 [−0.10, −0.08] | −0.07 |
| Palisades 2021-05 | 5.0 | 3,508 (6) | −1.72 [−1.92, −1.52] | −1.22 [−1.48, −1.04] | −0.51 [−0.61, −0.28] | −0.04 [−0.06, −0.03] | −0.02 |
| Woolsey + Hill 2018-11 | 7.5 | 282,382 (183) | −1.97 [−2.08, −1.86] | −1.54 [−1.63, −1.46] | −0.43 [−0.49, −0.36] | −0.03 [−0.04, −0.02] | −0.01 |
| Old / Sherwood 2016 | 10.0 | 1,320 (7) | −1.39 [−3.20, −0.66] | −1.48 [−2.53, −1.10] | +0.09 [−0.67, +0.46] | +0.02 [−0.08, +0.09] | −0.06 |

Spatial sd among unburned pixels: 2.2 dB for HV, 0.16 for NDVI. In those units the Woolsey scar is −0.9 sd in HV
and −0.2 sd in NDVI; the 2025 scars are −1.4 sd and −0.5 sd.

- **Both tracks agree.** Woolsey: −1.98 dB (t013) and −1.85 dB (t114); Palisades 2025: −2.99 and −2.91 dB.
- **By pre-fire fuel group (Woolsey):** GR −1.84 dB, GS −2.04 dB, SH −1.50 dB.
- **The deficit is not monotonic in age across fires.** Palisades 2021 (5.0 y, 6 blocks) is smaller than
  Woolsey (7.5 y). Fires differ in severity and vegetation; only Woolsey and the 2025 fires have many blocks.
- **Balance check with pre-fire NDVI.** Before the fire the same matched comparison gives −0.014 for Woolsey
  (Dec 2017 – Oct 2018, 38 scenes), +0.030 for Palisades 2025 and +0.012 for Franklin (2024). The groups were
  similar in optical terms before burning. There is no pre-fire L-band image, so a pre-existing L-band difference
  cannot be excluded.

### What GCOV adds beyond LANDFIRE 2025

Matching on the LF2025 fuel code and LF2025 canopy-cover class instead of LF2016:

| Cohort | HV, dB | HH, dB | NDVI |
|---|---|---|---|
| Palisades + Kenneth 2025-01 | −2.70 [−2.93, −2.43] | −1.82 [−2.03, −1.58] | −0.04 |
| Franklin 2024-12 | −2.59 [−3.04, −2.12] | −1.67 [−2.19, −1.17] | −0.01 |
| Woolsey + Hill 2018-11 | −1.84 [−1.95, −1.73] | −1.29 [−1.38, −1.20] | −0.04 |

Inside one LF2025 fuel class, burned pixels are still 1.8–2.7 dB lower. LF2025 did register the 2025 fires
(fuel code changed since LF2022 in 91–92 % of scar pixels against 13 % of controls; total load −0.5 to −0.9),
but for Woolsey it shows almost no load deficit (−0.12).

### Detecting a scar (90 m cells, stratum-demeaned, lower = burned; 95 % block bootstrap)

| Cohort | Burned / unburned cells | L-HV | L-HH | L HV − HH | NDVI | NDMI | LF2025 total load | C-VH (2025) |
|---|---|---|---|---|---|---|---|---|
| Palisades + Kenneth 2025 | 8,777 / 15,292 | 0.93 [0.91, 0.95] | 0.74 | 0.90 | 0.77 [0.74, 0.81] | 0.78 | 0.93 | 0.79 |
| Franklin 2024 | 1,529 / 15,292 | 0.94 [0.92, 0.96] | 0.81 | 0.88 | 0.78 [0.75, 0.81] | 0.73 | 0.92 | 0.87 |
| Woolsey + Hill 2018 | 26,442 / 15,292 | 0.89 [0.87, 0.92] | 0.83 | 0.65 | 0.65 [0.62, 0.70] | 0.58 | 0.58 | 0.81 |

### Within-year regrowth and a fire during the record

- **Within-year trend: not detectable.** Scar minus control HV trend over Nov 2025 – Oct 2026 on track 13 is
  +0.01 dB/yr [−0.15, +0.15] for Palisades 2025 and +0.07 [−0.15, +0.19] for Franklin; on track 114 it is negative
  (−0.34, −0.41). The control trend itself is −0.7 to −1.9 dB/yr (wet winter to dry autumn). Regrowth is visible
  across fires of different age, not within one year.
- **Bryson fire (2026-10-15, 696 pixels).** Track 114, 2026-10-08 → 2026-10-20: HV −1.09 dB inside, −0.16 dB in a
  0.3–1.5 km ring, difference −0.93 dB; HH −0.51 dB. The same difference over 20 earlier 12-day pairs has sd
  0.75 dB (HV), so z = −1.2: the right sign, not distinguishable from noise. Track 13 is quieter (sd 0.37 dB), but
  its first post-fire scene (2026-10-25) is not in the lake yet. FIRMS has no detection within 1.5 km.

## 3. Dynamics

Rain over the year: 579 mm, of which 492 mm fell in November–February. Five scenes had more than 1 mm in the three
days before.

Scene-mean HV anomaly per group (track 13, n = 25 scenes):

| Group | sd over scenes, dB | r with rain, previous 3 d | r with rain, previous 12 d | r with SME2 (HV / HH) |
|---|---|---|---|---|
| GR | 0.89 | 0.64 | 0.50 | 0.77 / 0.98 |
| GS | 0.85 | 0.78 | 0.59 | 0.87 / 0.99 |
| SH | 0.89 | 0.82 | 0.61 | 0.90 / 0.99 |
| TU / TL | 0.90 | 0.70 | 0.55 | 0.88 / 0.97 |
| NB (urban) | 0.40 | 0.40 | 0.24 | 0.54 / 0.82 |
| Burned 2024–25 | 1.07 | 0.82 | 0.54 | 0.82 / 0.98 |

SME2 is retrieved from the same HH acquisitions, so its correlation with HH is not independent evidence.

- **Track 114 is not usable for dynamics.** Its scene-mean anomaly has an sd of 1.7–2.5 dB in every group,
  including urban land (2.3 dB against 0.4 dB on track 13), and correlates weakly with rain (0.2–0.4). That points
  to scene-level radiometric offsets in this PROVISIONAL stack; the AOI is also only 64 % covered per scene.
- **The seasonal cycles differ.** HV peaks in November–December with the rain; NDMI peaks in February–March.

Monthly anomalies, fine scale (3 km neighbourhood mean removed), pooled r with 95 % block bootstrap:

| Pair | GR | GS | SH | TU / TL |
|---|---|---|---|---|
| HV ~ NDMI | 0.20 [0.17, 0.24] | 0.17 [0.15, 0.20] | 0.08 [0.05, 0.10] | 0.01 [−0.02, 0.05] |
| HV ~ NDVI | 0.21 [0.17, 0.26] | 0.21 [0.18, 0.24] | 0.08 [0.06, 0.10] | −0.02 [−0.04, 0.02] |
| HH ~ NDMI | 0.07 | 0.07 | 0.04 | −0.03 |
| HV ~ SME2 | 0.12 [0.07, 0.18] | 0.22 [0.21, 0.25] | 0.30 [0.28, 0.31] | 0.30 [0.27, 0.33] |
| HH ~ SME2 | 0.41 | 0.52 | 0.59 | 0.57 |
| NDMI ~ SME2 | −0.09 [−0.19, 0.01] | −0.04 [−0.06, −0.02] | −0.01 [−0.02, 0.01] | −0.06 [−0.08, −0.04] |
| HV ~ NDMI one month later | 0.14 | 0.19 | 0.07 | −0.01 |

Standardised regression of the fine-scale HV anomaly on NDMI and SME2 anomalies:

| Group | n | β NDMI | β SME2 | sd of fine-scale HV anomaly |
|---|---|---|---|---|
| GR | 70,008 | +0.21 [0.16, 0.25] | +0.14 [0.10, 0.19] | 0.57 dB |
| GS | 279,987 | +0.18 [0.16, 0.21] | +0.23 [0.22, 0.26] | 0.48 dB |
| SH | 413,166 | +0.08 [0.06, 0.11] | +0.30 [0.28, 0.32] | 0.41 dB |
| Burned 2024–25 | 148,978 | +0.10 [0.07, 0.13] | +0.28 [0.25, 0.32] | 0.52 dB |

In shrub, HV changes follow surface wetness four times more strongly than vegetation water; in grass the
vegetation term is the larger one, but still small. Fine-scale SME2 and NDMI anomalies of the same month are
uncorrelated.

## 4. L-band against C-band

Sentinel-1 scenes are from April–November 2025, GCOV from November 2025 – October 2026: one overlapping date, so
only time means are compared. L-HV and C-VH correlate at r = 0.73 across burnable 90 m cells; L-HH and C-VV at 0.53.

| Target | Spearman L-HV | Spearman C-VH | Block-CV R², L (HV + HH) | C (VH + VV) | L + C |
|---|---|---|---|---|---|
| Canopy cover | 0.38 | 0.30 | 0.14 | 0.11 | 0.17 |
| Canopy height | 0.38 | 0.30 | 0.14 | 0.10 | 0.16 |
| Live woody load | 0.14 | 0.22 | 0.17 | 0.13 | 0.25 |
| Total fuel load | 0.34 | 0.35 | 0.21 | 0.25 | 0.31 |
| NDVI | 0.49 | 0.55 | 0.29 | 0.45 | 0.48 |

Scar deficit, burned minus matched unburned (dB; in units of the unburned spatial sd):

| Cohort | L-HV | L-HH | C-VH | C-VV |
|---|---|---|---|---|
| Palisades + Kenneth 2025 | −3.06 (−1.37 sd) | −1.70 (−0.67) | −1.22 (−0.60) | −0.07 (−0.04) |
| Franklin 2024 | −3.40 (−1.53) | −2.06 (−0.81) | −1.73 (−0.85) | −0.56 (−0.28) |
| Palisades 2021 | −1.72 (−0.77) | −1.22 (−0.48) | −0.96 (−0.47) | −0.33 (−0.17) |
| Woolsey + Hill 2018 | −1.96 (−0.88) | −1.54 (−0.60) | −1.08 (−0.53) | −0.81 (−0.41) |

L-HV responds about twice as strongly as C-VH to post-fire fuel state, although the Sentinel-1 scenes saw the 2025
scars 3–10 months after the fire and GCOV saw them 10–21 months after. C-band follows NDVI more closely (leaves),
L-band follows canopy cover and height slightly better. The two bands together beat either alone.

## 5. Recommended features

| Feature | Definition (computable on the `smm` grid) | Measured | Limits |
|---|---|---|---|
| `gcov_hv_deficit_db` | (1) per track with ≥ 8 scenes in the trailing 12 months: mean γ0 HV in linear power, dB; (2) remove the quadratic fit on `u` (φ = 70° for t013, 90° for t114; fit inside LANDFIRE shrub; flat cells `u` = 0); (3) average the tracks in linear power; (4) subtract the mean of pixels without a fire perimeter since 2015 in the same stratum (LF2016 fuel code × slope class × `u` class × elevation tercile, ≥ 30 pixels); (5) 3 × 3 mean to 90 m | −3.1 dB at 1.4 y, −2.0 dB at 7.5 y; AUC 0.93 / 0.89 against NDVI 0.77 / 0.65 and LF2025 load 0.93 / 0.58 | needs the perimeter record for the reference; one region; no pre-fire L-band |
| `gcov_fuel_state` | `clip(10^(gcov_hv_deficit_db / 10), 0.3, 1.2)`: HV power relative to the unburned reference | median 0.52 in the 2025 scars, 0.61 in Woolsey, 1.05 where unburned (IQR 0.88–1.19); below 0.7 on 52 % of the burnable AOI | a relative index, not biomass; its effect on fire behaviour is untested |
| `gcov_hv_flat`, `gcov_hh_flat`, `gcov_hv_minus_hh` | steps 1–3 above for HV and HH, 90 m | adds 0.03–0.09 block-CV R² to NDVI + terrain for LANDFIRE canopy and loads; shrub-vs-grass AUC 0.81 → 0.83 | weak alone (R² 0.14–0.21) |
| `gcov_wet_anom_db` (not recommended as fuel moisture) | scene HV minus the pixel's 12-month mean, track 13 only, 90 m | scene mean r = 0.8 with 3-day rain; fine-scale β 0.30 on SME2 against 0.08 on NDMI in shrub | duplicates SME2; track 114 unusable |

Files written: `gcov_hv_deficit_db.tif` (30 m), `gcov_fuel_state_modifier.tif` (90 m values on the 30 m grid),
`gcov_means.npz` (time means, flattened layers, `u`).

**How `gcov_fuel_state` could enter a spread model.** As a multiplier on the woody fuel load of the LANDFIRE fuel
model in a physical rate-of-spread or intensity calculation, or as a displayed fuel-state layer. It should not be
used as a multiplier on a fire probability: no fire occurred in the AOI during the GCOV record that could test it,
and published work on chaparral reports that stand age matters little under extreme wind (general knowledge, not
checked in this study).

## What can be claimed

| Claim | Status |
|---|---|
| L-band HV separates post-fire regrowth from mature chaparral up to at least 7.5 years after fire | supported: −2.0 dB [−2.1, −1.9], AUC 0.89, both tracks, 183 blocks |
| GCOV carries fuel-state information that NDVI and LANDFIRE 2025 lack | supported for the 2018 scar (AUC 0.89 against 0.65 and 0.58) |
| L-band is more sensitive than C-band to that state | supported: about twice the deficit in sd units |
| Terrain orientation must be corrected | supported: 2.4–4.0 dB |
| GCOV improves a LANDFIRE-like fuel map built from optical data and terrain | modest: +0.03 to +0.09 R² |
| GCOV measures live fuel moisture | not supported |
| GCOV measures biomass | not shown: r 0.3–0.4 with GEDI L4B, which does not resolve the scars either |
| GCOV detects a new burn within days | inconclusive: −0.9 dB, z = −1.2, one fire, noisy track |
| Regrowth is measurable within one year | not detected |
| The fuel-state index changes fire risk | untested |

## Limitations

- One region, one year of GCOV, PROVISIONAL calibration, no layover/shadow mask.
- "Unburned" means no perimeter since 2015; older fires are not in the lake.
- The scar comparison is space-for-time with matching; pre-fire L-band data do not exist.
- LANDFIRE is a modelled product; agreement with it is not ground truth.
- Fuel-structure cross-validation used a 30,000–40,000 cell sample and untuned models.
- Sentinel-1 and GCOV time means come from different months.
