# machine-learning

Models for **soil moisture → live fuel moisture (LFMC) → large-fire risk** in California (NASA Space Apps 2026),
and the statewide map the app shows. Data come from [fuel-lake](https://github.com/NASA-ACEs/public-submission/tree/main/data-aggregation);
see [docs/DATA.md](docs/DATA.md).

| Part | Predicts | Label | Trained on |
|---|---|---|---|
| A | LFMC (%) with a 90 % interval | field samples (Globe-LFMC, FEMS) | 2016–2024 |
| B | P(fire ≥ 400 ha \| ignition) | FPA-FOD final fire size | 2016–2024 |
| fuel age | how much less likely a recently burned cell is to carry a large fire | ignitions and later perimeters inside earlier ones | 2016–2025 |
| spread | head-fire rate of spread, flame length, direction (Rothermel) | none: physics, checked against pyretechnics | — |

The model in one page, with the evidence for each part: [docs/MODEL.md](docs/MODEL.md).

**Where NISAR enters.** The models learn from 2016–2024, when only SMAP soil moisture existed (9 km).

- **SME2 soil moisture (200 m)** adds to SMAP what SMAP cannot resolve: a cell's anomaly against its own track's
  earlier scenes, minus the 9 km mean of that anomaly. Two independent tracks agree on it with r = 0.39 (0.50 in
  cells flagged "recommended"), almost as well as on the 9 km mean. It is added to SMAP's surface soil moisture and
  to the dryness anomalies the large-fire model uses. A map is not produced without it.
- **GCOV L-band backscatter (10 m)**, where staged, gives the fuel state after fire: HV power against unburned
  cells of the same pre-fire fuel model and terrain. It separates regrowth from mature chaparral 7.5 years after a
  fire (scar detection AUC 0.89; NDVI 0.65, the LANDFIRE 2025 fuel load 0.58). It is a layer and scales the woody
  fuel of the spread physics.

Method, validation and limits: [docs/METHODS.md](docs/METHODS.md). Studies behind each choice:
[docs/EXPERIMENTS.md](docs/EXPERIMENTS.md).

## Run

The data are downloaded from the providers (NASA Earthdata, gridMET, LANDFIRE,
FEMS, USFS). Nothing is copied by hand.

```bash
git clone https://github.com/NASA-ACEs/public-submission && cd public-submission/machine-learning
nohup scripts/run.sh > ~/aces-run.log 2>&1 &        # setup, data, train, map
```

Needs `git`, [`uv`](https://docs.astral.sh/uv/) and
`~/.config/fuel-lake/secrets.env` with a line `EARTHDATA_TOKEN=...` (never commit it).

| Step | What it does |
|---|---|
| `setup` | installs fuel-lake (`../data-aggregation`) and this project with `uv` |
| `data` | downloads the labels, fire perimeters, gridMET and SMAP L4 into the lake; builds the training table and adds the features of `ml.series` to it; stages NISAR SME2 for the 110 days before the map date (the term needs 6 earlier scenes of a track) |
| `train` | `ml.data` (row counts, fill rates), `ml.train` (both models, `metrics.json`) |
| `map` | `ml.grid --check` (map features equal table features?), `ml.map` (the output bundle) |
| `nisar` (optional, after `map`) | stages more SME2, builds a 2026 table and runs `ml.nisar_check`: NISAR-fed against SMAP-only LFMC at 2026 field samples |

Run steps one at a time with `scripts/run.sh train map`; `DRY_RUN=1` prints the commands only. Variables:
`WORK` (default `~/aces`: table, models, outputs), `LAKE_ROOT` (`~/fuel-lake-data`), `MODELS`
(`core` or `full`), `MAP_DATE` (default `2026-10-20`), `STATICS` (an AOI with LANDFIRE staged at 200 m, e.g. `ca`:
the table then takes terrain, fuels and canopy as cell means from it, as v2 does, and, when the collection
`landfire-evt` is in the lake, the vegetation shares of v3.2 and the surroundings of v3.3; fuel-lake's standard run
cannot stage either on `ca`, see `docs/DATA.md`).

**Map date.** NISAR SME2 is published about 3.5 weeks after acquisition: on 2026-11-14 the newest scenes over
California were from 2026-10-21. A later date has no NISAR data and `ml.map` refuses it.

**Threads.** Fits use one thread each and run side by side (`ML_WORKERS`, default 32). LightGBM's default of one
thread per core made a single fit of these small tables take minutes on a 128-core machine.

The data step takes hours; the rest takes minutes. No GPU; the California map needs about 16 GB of memory.
`ml.map` stops if gridMET, SMAP L4 or a NISAR scene pair is missing for the date and says what is missing.

`models/core` has no LANDFIRE inputs; `models/full` adds fuels, canopy, terrain, vegetation type and the
surroundings of the cell, together the largest gain of the large-fire model: use it wherever those layers are
staged on the AOI. `ml.map --soil smap` writes the map without
any NISAR input, `--aoi smm` maps the Santa Monica Mountains (30 m, the AOI where GCOV and LANDFIRE are staged).
`python -m ml.compare <reference models> <candidate models>` is the regression test between two model versions.

## Output bundle

GeoTIFFs on the AOI grid (California: EPSG:6933, 200 m), float32, nodata −9999:

| File | Content |
|---|---|
| `p_large_fire.tif` | calibrated P(final size ≥ 400 ha \| ignition that day), fuel age applied |
| `risk_index.tif` | that probability as a 0–1 rank among the predictions for 2016–2024 California ignitions on open land (cells at most 10 % built up) |
| `susceptibility_index.tif` | the same rank with every weather input set to its median over the training ignitions: what the land contributes (terrain, fuels, vegetation, surroundings, fuel age). A copy of `risk_index.tif` for models older than v3.3.1 |
| `weather_factor.tif` | the odds of the day divided by the odds of that ordinary day: above 1 the day's weather raises the cell's risk |
| `risk_class.tif` | classes 1–5 at rank 0.2 / 0.4 / 0.6 / 0.8, uint8, nodata 0 |
| `lfmc.tif`, `lfmc_p05.tif`, `lfmc_p95.tif` | LFMC, % of dry mass, with the 90 % interval |
| `dryness_anomaly.tif` | root-zone soil moisture as a z-score against the cell's previous 12 months, NISAR term included |
| `soil_moisture.tif` | surface soil moisture: SMAP L4 plus the NISAR term |
| `nisar_term.tif` | the fine-scale SME2 term, m³/m³ |
| `risk_index_smap.tif`, `risk_index_nisar_minus_smap.tif`, `lfmc_nisar_minus_smap.tif` | the map without the SME2 term, and what the term changes |
| `sme2_anomaly.tif`, `p_large_fire_sme2.tif`, `risk_index_sme2.tif`, `risk_index_sme2_minus_main.tif` | **experimental**: the newest SME2 pass as a z-score against the cell's own track record, and the risk with the odds scaled by 0.84 per unit of it (`docs/research/nisar-fire-season.md`) |
| `years_since_fire.tif`, `spread_permeability.tif` | years since the last recorded fire (10 = none); relative chance that the cell burns when a large fire reaches it, by the vegetation that grew there before |
| `fuel_state.tif` | where GCOV is staged: L-band HV power against unburned cells of the same pre-fire fuel model (1 = mature) |
| `rate_of_spread.tif`, `flame_length.tif`, `fireline_intensity.tif`, `spread_direction.tif` | where LANDFIRE is staged: head fire of Rothermel's surface model, m/min, m, kW/m, degrees |
| `spread_ros0.tif`, `spread_wind_a.tif`, `spread_wind_b.tif`, `spread_slope_k.tif`, `spread_waf.tif`, `spread_wind_cap.tif`, `spread_intensity_k.tif`, `slope.tif`, `aspect.tif`, `wind_speed.tif`, `wind_from.tif` | the wind-independent parts of the head fire and the wind of the map day: with them `ml.spreadsim.simulate(bundle, lon, lat, hours, wind)` grows a fire from any point for any wind, without the lake |
| `spread_ros0_plain.tif` … `spread_intensity_k_plain.tif` | where GCOV is staged: the same fire-growth components from the fuel map alone, to show what the GCOV fuel state changes |
| `vectors.geojson` | one line per km (sparser on large grids): the distance the head fire would run in one hour |
| `result.json`, `provenance.json` | `backend-result-1` manifest: NISAR share and scene ids, `nisar_effect`, `fuel_age` (β, share of cells in scars, GCOV statistics), units per layer |

The backend serves these bundles from `RESULTS_DIR` (`backend` branch `feat/real-results`).

## Results

Model v3.3 on the real table (74,568 ignitions with 477 large fires; 15,591 LFMC site-dates), data the models did
not see. Every version, with its regression test against the one before: [docs/versions](docs/versions/README.md).

| | v3.3 | For comparison |
|---|---|---|
| Large fire, PR-AUC in a year never seen | 0.0824 | burning index 0.0142, random ranking 0.0064; v3.2 0.0712, v3.1 0.0703 by the same scoring ([docs/versions/v3.2.md](docs/versions/v3.2.md)) |
| ROC-AUC | 0.885 | burning index 0.591 |
| Large fires among the top 1 % / 10 % of ignitions | 18 % / 64 % | |
| **Two years after training** (2025–2026, another source): PR-AUC / ROC-AUC | 0.080 / 0.901 | burning index 0.012 / 0.628; 87 large fires expected, 80 observed; the two highest risk classes hold 14.5 % of the ignitions and 64 of the 80 large fires |
| LFMC RMSE at places and in years never seen | 25.5 | fold mean 34.7, linear 33.0, v0 27.1 |

Gains whose 95 % interval excludes zero: the model over the burning index (+0.065 [0.042, 0.090] PR-AUC), the
surroundings of the cell within 1 and 3 km (+0.011 [0.0002, 0.021] PR-AUC; +0.030 [0.011, 0.049] ROC-AUC on
2025–2026),
terrain and fuel (+0.047 [0.028, 0.066] in v3), HRRR gusts in ROC-AUC (+0.017 [0.009, 0.027]; +0.031 [0.011, 0.055]
on 2025–2026), vegetation type of the cell in ROC-AUC (+0.0065 [0.0022, 0.0117]; +0.028 [0.011, 0.047] on
2025–2026, where it also raises PR-AUC for fires ≥ 4 ha), 200 m cell means for LFMC (−1.19 [−1.72, −0.61] RMSE),
rain sums for LFMC. Not significant: dryness
z-scores, antecedent sums, the PR-AUC gains of gusts and of vegetation type; `lfmc_hat` adds nothing and is not an input. Fuel age holds
for scars younger than 3.5 years and, in former forest, for about 7. NISAR is judged in
[docs/MODEL.md](docs/MODEL.md): GCOV gives a fuel-state layer nothing else shows; the SME2 anomaly passed a
pre-registered test of association with the fires of 2026 (AUC 0.55 [0.52, 0.58], SMAP's anomaly 0.45), its 200 m
detail did not, so it enters `risk_index` only as a small term and is otherwise shown as experimental layers.

`python -m ml.grid --check <table> --aoi ca` compares what the map feeds the models with the table at the table's
points: static inputs identical, weather and soil moisture within interpolation differences (median 0.0–0.02 of
their units, dryness z-scores within 0.008).

Code Apache-2.0; data keep their own licences.
