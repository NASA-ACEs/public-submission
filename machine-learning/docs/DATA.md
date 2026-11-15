# Data we rely on

All data come from **fuel-lake**, the [`data-aggregation`](../../data-aggregation) directory of this repository.
This repo never downloads raw data. The feature lists are in `ml.data`; the only features computed here are
the ones in `ml.data.add_derived` (season, aspect components), by one function for table rows and map cells.

## Read these first (fuel-lake docs)

| What | Link |
|---|---|
| Every dataset: variables, units, grid, period, status, licence | [README → Data inventory](https://github.com/NASA-ACEs/public-submission/blob/main/data-aggregation/README.md#data-inventory) |
| Caveats per dataset (read before modelling) | [README → Per-dataset notes](https://github.com/NASA-ACEs/public-submission/blob/main/data-aggregation/README.md#per-dataset-notes-read-before-modelling) |
| System A (LFMC / fuel state): inputs, labels, output contract | [README → Guide for System A](https://github.com/NASA-ACEs/public-submission/blob/main/data-aggregation/README.md#guide-for-system-a-fuel-state) |
| System B (large-fire / spread risk): inputs, labels, output contract | [README → Guide for System B](https://github.com/NASA-ACEs/public-submission/blob/main/data-aggregation/README.md#guide-for-system-b-fire-spread-risk) |
| Reading the lake (CLI, `LakeClient`, HTTP, files) | [README → Getting data out](https://github.com/NASA-ACEs/public-submission/blob/main/data-aggregation/README.md#getting-data-out) |
| Training-table rules (rows, lags, causality, releases, `block_50km`, missing values) | [`src/fuellake/features/points.py` docstring](https://github.com/NASA-ACEs/public-submission/blob/main/data-aggregation/src/fuellake/features/points.py) and [`config/datasets/point-features.yaml`](https://github.com/NASA-ACEs/public-submission/blob/main/data-aggregation/config/datasets/point-features.yaml) |
| What has run on real data | [README → Validation status](https://github.com/NASA-ACEs/public-submission/blob/main/data-aggregation/README.md#validation-status) |
| Known gaps | [README → Known gaps](https://github.com/NASA-ACEs/public-submission/blob/main/data-aggregation/README.md#known-gaps-and-limitations) |
| Licences and attribution text for the submission | [README → Required attribution](https://github.com/NASA-ACEs/public-submission/blob/main/data-aggregation/README.md#required-attribution) |
| Lake contract and consumer patterns | [ARCHITECTURE.md](https://github.com/NASA-ACEs/public-submission/blob/main/data-aggregation/ARCHITECTURE.md), [docs/microservices.md](https://github.com/NASA-ACEs/public-submission/blob/main/data-aggregation/docs/microservices.md) |

## Training data: one table, extended

1. `point_features` (`features/ca/point_features.parquet`, built by `fuellake build-points --aoi ca --start
   2016-01-01 --end 2024-12-31 --no-sentinel2`): one row per LFMC sample or fire ignition, every feature sampled at
   the point and only from data dated on or before the row's date. Copy the file out of the lake after the build
   (a later build overwrites it).
2. `python -m ml.series --table <that file> --out <table> --statics ca` adds what the models need beyond it, by
   the same code the map uses: the features of `ml.data.SERIES` from the daily gridMET and SMAP stores,
   `years_since_fire` from the fire perimeters, and terrain, fuels and canopy as means over the 200 m cell (they
   replace the table's 30 m point values). Pass the result to `ml.train`.

| | System A: LFMC | System B: large fire |
|---|---|---|
| Rows | `point_type == "lfmc_sample"`, then one row per site and date (mean over species) | `point_type == "fire_ignition"` |
| Label | `lfmc_pct` (% dry weight) | `large_fire` (final size ≥ 400 ha) |
| Period | 2016–2023 Globe-LFMC; FEMS from 2024 (they overlap 2017–2023, never use both) | 2016–2024, FPA-FOD only (`source == "fpa-fod"`) |
| Size | 15,591 site-dates at 322 locations | 74,568 ignitions, 477 positives (0.64 %) |
| QC | drop `outlier_flag == True` and rows flagged `value_out_of_range` | drop null `large_fire` |
| Metric | RMSE where block and year are new, 90 % interval coverage, within-site correlation | PR-AUC in a year never seen; Brier, reliability; ROC-AUC secondary |
| Baseline | fold mean; linear on `vpd`, `sm_surface` | gridMET `bi` on the ignition day |

### Features used (`ml.data.FEATURES`)

| Group | Columns | LFMC | Large fire |
|---|---|---|---|
| Weather, gridMET 4 km | `vs`, `rmin`, `erc`, `bi`, `fm100`, `fm1000`, `vpd`, `pr`, `tmmx`, `pet` | yes | yes |
| Wind, HRRR 3 km (`ml.series`) | `gust_max`, `wind10_max`: the larger of the 12 and 00 UTC analyses of the day | no | yes |
| Their single-day lags | `<var>_lag<L>d`, L = 15, 30, 45, 60, 90, on `fm100 fm1000 vpd pr tmmx pet` | yes | no |
| Soil moisture, SMAP L4 9 km | `sm_surface`, `sm_rootzone` and their five lags | yes | no |
| Season | `doy_sin`, `doy_cos` | yes | no |
| Rain sums (`ml.series`) | `pr_sum30`, `pr_sum90`: the 30 / 90 days before the date | yes | yes |
| Antecedent (`ml.series`) | `vpd_mean30`, `vs_max3` | no | yes |
| Dryness (`ml.series`) | `rz_z12`, `sf_z12`: root-zone / surface soil moisture of the day as a z-score against the cell's previous 365 days; `rz_z12_lag30d`, `_lag60d`, `_lag90d` | no | yes |
| `full` adds: LANDFIRE fuel, canopy, terrain, 200 m cell means | `total_load`, `dead_1h_load`, `dead_10h_load`, `dead_100h_load`, `live_herb_load`, `live_woody_load`, `fuel_bed_depth`, `moisture_of_extinction`, `burnable`, `canopy_cover`, `canopy_height`, `canopy_base_height`, `canopy_bulk_density`, `elevation`, `slope`, `northness`, `eastness` | yes | yes |
| `full` adds: vegetation type, LANDFIRE EVT, 200 m cell shares | `evt_share_<class>` for chaparral, coastal_scrub, sagebrush, desert_scrub, other_shrub, oak_woodland, other_hardwood, conifer_oak, dry_conifer, mesic_conifer, juniper_pinyon, grass, riparian, sparse, agriculture, developed | no | yes |
| `full` adds: surroundings of the cell (`ml.context`), discs of 1 and 3 km on the `ca` grid | `<v>_r1km`, `<v>_r3km` for `evt_share_developed`, `evt_share_agriculture`, `burnable`, `total_load`, `canopy_cover`, `forest_share`, `shrub_share`; `elevation_sd_r1km`, `tpi_r1km`, `tpi_r3km` | no | yes |
| Fuel age (`ml.series`) | `years_since_fire` | no | after the model, as an odds modifier |

Tested and not used, with the study: `lfmc_hat` as a large-fire input, single-day lags and raw soil moisture in
the large-fire model, auxiliary labels (`docs/research/fire-model.md`); Sentinel-2 NDVI / NDMI for LFMC
(`lfmc-s2.md`); LANDFIRE vegetation type for LFMC (`vegetation-type.md`); Rothermel outputs as features (`physics.md`);
GEDI (empty before 2023-03); `lon` / `lat` (they defeat the spatial split); every `soil_moisture*` / `sme2_*`
column of the table (SME2 starts in late 2025, so they are null in training rows).

A column that is empty in the table is dropped from the set at training time; `meta.json` records the columns
used and their fill rates. The map reads its feature lists from `meta.json`.

## Map inputs (`ml.grid`)

For a date D on an AOI grid (`ca`: EPSG:6933, 200 m, 4827 × 4956; `smm`: EPSG:32611, 30 m, 837 × 1398):

| Columns | Source in the lake | Rule |
|---|---|---|
| gridMET, SMAP L4, lags | native-grid daily stores | exact day D − L, bilinear: `WeatherSeries.grid_slice`, the same class whose `.points()` fills the table |
| `ml.series` features | the same daily stores | computed on the native grid, then bilinear; one code path with the table |
| `sm_surface`, `sf_z12`, `rz_z12` of the day | plus `nisar-sme2` items on the AOI grid | the NISAR term of `ml.grid.nisar_fine` is added (`docs/METHODS.md`) |
| LANDFIRE (`full` only) | `landfire-fuel-load`, `landfire-canopy`, `landfire-topo`, `landfire-evt` items on the AOI | the release valid on D; grids finer than 200 m are averaged over 200 m windows (a 0/1 class layer becomes the class's share) |
| Surroundings (`full` only) | the same LANDFIRE items on `ca` | `ml.context`: discs on the `ca` grid, release valid on D; a map of another AOI reads them at its cell centres |
| `years_since_fire` | `fire_perimeters` table | rasterised; newest fire that ended at least 30 days before D |
| GCOV fuel state | `nisar-gcov`, `landfire-fbfm40`, `landfire-topo` items on the AOI | `ml.gcov.fuel_state`; today on `smm` only |
| Spread physics | `landfire-fbfm40`, `landfire-topo`, `landfire-canopy`, gridMET of D | `ml.rothermel`, `ml.spreadsim`; at the grid's own resolution |

`python -m ml.grid --check <table> --aoi ca` compares table values with the map values at the same cell and day.
On the real table: static inputs identical; weather and soil moisture differ by interpolation only.

## Where the lake data are on the server (2026-11-15)

`~/spaceapps/ml-work/lake` is a read-only view (symlinks) over the lake roots the download lanes write to:
labels, fire tables and NISAR in `~/spaceapps/fuel-lake-data`; gridMET and SMAP in `lanes/B`; `smm` layers,
Sentinel-2, OPERA RTC-S1 and HRRR in `lanes/D`; ERA5-Land in `lanes/E`; LANDFIRE on the `ca` grid in `lanes/ML`;
HRRR gusts on `ca`, 2016–2026, in `lanes/W01` … `W27`.
Use it as `LAKE_ROOT` with `FUELLAKE_CONFIG_DIR=<data-aggregation>/config`.

## Wanted from fuel-lake

`scripts/run.sh` runs items 1 and 2 itself.

1. The training table (commands above).
2. NISAR SME2 on `ca` for the 110 days before the map date: the NISAR term needs 6 earlier scenes of a track. The
   newest scenes available on 2026-11-14 were from 2026-10-21, so the map date is 2026-10-20.
3. LANDFIRE fuel loads, canopy, terrain and FBFM40 staged on `ca` (200 m). The connectors refuse region-scale AOIs;
   on the server the layers were built from 30 m blocks by the scripts described in `docs/research/ca-statics.md`
   and registered in a separate lake root. This belongs in fuel-lake: block-wise download, mean for continuous
   layers, mode for the fuel model, loads computed per 30 m pixel before averaging.
4. HRRR gusts (`hrrr-gusts`) on `ca` for 2016 to the map date. The connector needs `--force-scale` and two fixes
   (skip days missing from the archive; tolerate the 53 m grid shift of 2016-08-23): patch in
   `docs/research/wind.md`. Without the dataset `ml.series` leaves the two wind features out and the models train
   without them (that is v2.1).
- `landfire-evt` (shares of 16 vegetation classes per cell; releases LF 2016, 2022, 2025) is not a dataset of
  fuel-lake's `main` yet. On the server it is staged in `~/spaceapps/lanes/ML` for `ca` and `smm` and described by
  `~/spaceapps/ml-work/config/datasets/landfire-evt.yaml`, a copy of fuel-lake's `config/` with that one file
  added (`FUELLAKE_CONFIG_DIR`). How it was built: `~/spaceapps/ml-work/research/evt_stage/RESULTS.md`. As a
  fuel-lake connector it is on branch `feat/landfire-evt` of data-aggregation (`fuellake run landfire-evt --aoi
  smm|ca`; on `smm` it reproduces the staged items exactly, the full `ca` run through it has not been made). Without
  the collection `ml.series` and `ml.grid` leave the shares out and the large-fire model is that of v3.1.
