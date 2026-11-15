# Statewide LANDFIRE layers on the `ca` grid

Run 2026-11-15, 01:14–01:45. Scripts and intermediate files are in `~/spaceapps/ml-work/research/ca_statics/` on
the server. Nothing in the main lake, lanes A–E, `~/spaceapps/training` or any repository was changed.

## Verdict

- Terrain, fuel model, fuel loads and canopy are staged for all of California on the `ca` grid (EPSG:6933,
  200.18 m, 4827 × 4956) for LF 2016, LF 2022 and LF 2025 (terrain: LF 2020). `ml.grid.statics` returns every static
  feature of the `full` models for `ca` through the lake view, so `ml.map --aoi ca` can use them.
- The layers agree with the connector-made `smm` layers aggregated to the same cells: r ≥ 0.98 for every continuous
  layer except canopy base height (0.95), the modal fuel code is the same in 95 % of cells.
- The training table reads these layers at the 30 m pixel of the ignition; the map reads the 200 m cell. The two
  differ a lot for fuels: 62–70 % of ignitions lie on a non-burnable pixel (the physics study traced these to
  roads), but only 34–44 % of them in a cell that is mostly non-burnable. Training on the cell values is at least as good as training on the pixel
  values (PR-AUC in years never seen 0.053 against 0.045, gain +0.008 [−0.003, +0.022]; +0.011 [+0.001, +0.023]
  with the burnable share added) and makes the map consistent with the model. A table of cell values for every
  training row is ready (`points_statics_cell200m.parquet`).

## What was staged

Own lake root `~/spaceapps/lanes/ML` (one writer), 10 items, 1.4 GB staged, 3.9 GB raw; `fuellake validate`:
0 errors, 0 warnings.

| Collection | Items on `ca` | Assets |
|---|---|---|
| `landfire-topo` | `__ca__20200101__lf2020` | `elevation` (m), `slope` (degree), `aspect` (degree) |
| `landfire-fbfm40` | `lf2016`, `lf2022`, `lf2025` | `fuel_model` (code), `fuel_model_burnable` (auxiliary), `classes` |
| `landfire-fuel-load` | `lf2016`, `lf2022`, `lf2025` | `total_load`, `dead_1h_load`, `dead_10h_load`, `dead_100h_load`, `live_herb_load`, `live_woody_load` (kg/m²), `fuel_bed_depth` (m), `moisture_of_extinction` (%), `burnable` (0/1), `burnable_share` (auxiliary), `parameters` |
| `landfire-canopy` | `lf2016`, `lf2022`, `lf2025` | `canopy_cover` (%), `canopy_height` (m), `canopy_base_height` (m), `canopy_bulk_density` (kg/m³) |

Asset keys, dtypes, nodata and units are those of the `smm` items. Items carry `fuellake:aggregation` (how they
were made) and went through the same region clip as pipeline items (cells outside California are nodata).

The lake view `~/spaceapps/ml-work/lake` now serves the four collections from two roots: its `catalog/<collection>`
and `staged/<collection>` symlinks point to merged directories under `lake/.merged/` (a merged `collection.json`,
item links to `lanes/D` for `smm` and to `lanes/ML` for `ca`). The symlinks were swapped by renaming a new symlink
over the old one, which is atomic; a symlink cannot be replaced by a directory in one rename.

## Source and method

- **Source.** The same keyless LANDFIRE ArcGIS ImageServers the connectors use
  (`https://lfps.usgs.gov/arcgis/rest/services/Landfire_Topo/LF2020_{Elev,SlpD,Asp}_CONUS`,
  `Landfire_LF{2016,2022,2025}/LF…_{FBFM40,CC,CH,CBH,CBD}_CONUS`), `exportImage` on the native 30 m EPSG:5070
  lattice, nearest neighbour, int16, LZW. Fuel model parameters: `config/tables/fbfm40_parameters.csv` of fuel-lake
  (Scott and Burgan 2005, Table 7) through the connector's own `map_codes`.
- **Why not the connectors.** They request one native 30 m clip per AOI: for California about 1 Gpx per layer. Here
  the `ca` grid is cut into 60 blocks of 512 × 512 cells that touch the state; each block's 30 m window (about
  16 Mpx) is downloaded and aggregated on its own. 18 layers × 60 windows = 1,080 requests, 4.1 GB, under 10 minutes
  with 8 parallel requests, no failed request.
- **Aggregation** (GDAL warp from the 30 m window to the 200 m cells):
  - elevation, slope, canopy cover, height, bulk density: mean of the 30 m values (non-forested pixels count as 0);
  - canopy base height: mean over forested pixels, NaN where the cell has none;
  - aspect: circular mean of the 30 m aspects that are defined (LANDFIRE codes slopes ≤ 2° as flat), NaN where none;
  - `fuel_model`: most frequent 30 m code, non-burnable codes included; `fuel_model_burnable`: most frequent
    burnable code;
  - loads and bed depth: each 30 m pixel takes the table value of its fuel model (0 where non-burnable), the cell
    takes their mean, so they are per unit of cell area;
  - `moisture_of_extinction`: mean over the burnable pixels;
  - `burnable`: 1 where at least half of the 30 m pixels are burnable; `burnable_share`: that share.
- **No change to fuel-lake.** Rasters are written with fuel-lake's `write_cog`, items with its `build_stac_item`,
  `clip_staged_item`, `upsert_collection` and `add_item`, run from the data-aggregation venv with
  `LAKE_ROOT=~/spaceapps/lanes/ML`. The working tree of data-aggregation was only read.

## Checks

### (a) `ml.grid.statics` on `ca`

All 16 layers behind `ml.data.STATIC` are returned at 4827 × 4956 (aspect gives `northness`, `eastness`). Share of
finite cells 0.4275 (the state inside its bounding grid) for every layer except `aspect` 0.318,
`moisture_of_extinction` 0.390 and `canopy_base_height` 0.220, which are undefined on flat, non-burnable and
non-forested land. `ml.gcov.layer` also reads the layers the spread physics needs (`fuel_model`, `slope`, `aspect`,
`canopy_cover`, `canopy_height`); `ml.gcov.fuel_state` returns `None` on `ca` (no GCOV there), as intended.

### (b) `ca` 200 m against `smm` 30 m aggregated to the same cells (116 × 216 cells)

| Layer | n | r | mean `ca` | mean `smm` | mean abs. difference |
|---|---|---|---|---|---|
| elevation (m) | 19,128 | 0.9999 | 339.60 | 339.64 | 1.23 |
| slope (degree) | 19,128 | 0.996 | 14.79 | 14.80 | 0.51 |
| total_load (kg/m²) | 19,128 | 0.996 | 0.781 | 0.781 | 0.038 |
| live_woody_load | 19,128 | 0.996 | 0.288 | 0.288 | 0.015 |
| dead_1h_load | 19,128 | 0.996 | 0.179 | 0.179 | 0.010 |
| fuel_bed_depth (m) | 19,128 | 0.997 | 0.355 | 0.356 | 0.017 |
| moisture_of_extinction (%) | 17,356 | 0.970 | 16.64 | 16.63 | 0.31 |
| canopy_cover (%) | 19,128 | 0.985 | 3.90 | 3.89 | 0.65 |
| canopy_height (m) | 19,128 | 0.983 | 2.37 | 2.37 | 0.41 |
| canopy_base_height (m) | 10,281 | 0.946 | 4.92 | 4.92 | 0.47 |
| canopy_bulk_density (kg/m³) | 19,128 | 0.983 | 0.0052 | 0.0052 | 0.001 |
| burnable share, LF 2025 | 19,128 | 0.997 | 0.699 | 0.700 | 0.017 |
| burnable share, LF 2016 | 19,128 | 0.996 | 0.731 | 0.732 | 0.019 |

- Aspect: mean circular difference 6.3°, median 4.1°, 95.8 % of 17,942 cells within 20°.
- Modal fuel code: the same in 94.9 % of cells (LF 2025) and 95.7 % (LF 2016); the same burnable / non-burnable
  status in 98.1 % and 98.2 %.
- The remaining differences come from resampling twice (the `smm` layers were first put on a UTM 30 m grid).

### (c) The map's cell values against the table's pixel values at 74,568 ignitions

Each ignition is compared with the layer of the release its table row used (LF 2016: 49,270 rows, LF 2022: 17,404;
the 7,894 rows of 2016 have no release).

| Column | Release | n | r | table (30 m pixel) | map (200 m cell) | mean abs. difference |
|---|---|---|---|---|---|---|
| elevation (m) | – | 74,458 | 1.000 | 485.6 | 486.0 | 4.7 |
| slope (degree) | – | 74,458 | 0.893 | 5.06 | 5.55 | 1.78 |
| canopy_cover (%) | LF 2025 layer, all rows | 66,579 | 0.722 | 6.98 | 8.68 | 6.16 |
| canopy_height (m) | LF 2025 layer, all rows | 66,579 | 0.685 | 3.75 | 4.34 | 3.39 |
| total_load (kg/m²) | LF 2016 | 49,197 | 0.778 | 0.395 | 0.503 | 0.292 |
| dead_1h_load | LF 2016 | 49,197 | 0.749 | 0.104 | 0.134 | 0.087 |
| live_herb_load | LF 2016 | 49,197 | 0.585 | 0.036 | 0.055 | 0.041 |
| live_woody_load | LF 2016 | 49,197 | 0.680 | 0.071 | 0.089 | 0.072 |
| fuel_bed_depth (m) | LF 2016 | 49,197 | 0.631 | 0.133 | 0.179 | 0.119 |
| total_load | LF 2022 | 17,382 | 0.739 | 0.343 | 0.546 | 0.357 |
| live_herb_load | LF 2022 | 17,382 | 0.394 | 0.025 | 0.061 | 0.054 |
| fuel_bed_depth | LF 2022 | 17,382 | 0.590 | 0.115 | 0.215 | 0.160 |

(The canopy rows compare the table's LF 2016 / LF 2022 pixel with the LF 2025 cell, so part of that difference is
time; the fuel rows compare like with like.)

| | LF 2016 rows | LF 2022 rows |
|---|---|---|
| ignitions | 49,197 | 17,382 |
| table: the 30 m pixel is burnable | 38.4 % | 30.2 % |
| map: the 200 m cell is mostly burnable | 56.1 % | 65.7 % |
| pixel not burnable, cell burnable | 20.5 % | 37.4 % |
| pixel burnable, cell not | 2.7 % | 1.9 % |
| same fuel code | 63.6 % | 54.2 % |
| large fires (≥ 400 ha) | 340 | 91 |
| … whose ignition pixel is not burnable | 89 | 24 |
| … whose cell is mostly not burnable | 34 | 7 |

The divergence is large for everything derived from the fuel model. Most ignitions are recorded on a non-burnable
pixel; a model that learns "non-burnable pixel, zero load" from them is then asked on the map about cells that are
burnable with positive loads.

### What to do about it: train on the cell values

`points_statics_cell200m.parquet` holds, for all 120,428 rows of `points_v1b_series.parquet` (key `point_id`), the
cell values of every static column with the table's release rule (LF 2016 from 2017-01-01, LF 2022 from
2022-10-01, LF 2025 from 2025-10-01, none before 2017; `cell_release` says which), plus `burnable_share`,
`fuel_model` and `fuel_model_burnable`. Fill: 89.2 % for fuels and canopy (as in the table), 99.9 % for terrain.

Quick test (`quicktest.py`): large-fire model, weather of the day + statics, leave-one-year-out on the 66,674
ignitions of 2017–2024 that have a release (431 large), LightGBM with the settings of `ml.train.CLF`, no tuning,
paired bootstrap over calendar weeks (500).

| Statics used | PR-AUC | ROC-AUC | large fires in the top 10 % | gain over pixel statics |
|---|---|---|---|---|
| none (weather only) | 0.0225 | 0.692 | 28.1 % | −0.0224 [−0.0407, −0.0072] |
| table, 30 m pixel | 0.0449 | 0.811 | 50.6 % | – |
| map, 200 m cell | 0.0532 | 0.822 | 51.3 % | +0.0084 [−0.0031, +0.0215] |
| map, 200 m cell + `burnable_share` | 0.0560 | 0.821 | 52.0 % | +0.0111 [+0.0009, +0.0229] |
| trained on pixel, predicted on cell (the map as it is today) | 0.0479 | 0.808 | 48.0 % | +0.0030 [−0.0052, +0.0129] |
| table, 30 m pixel, another seed | 0.0429 | 0.810 | 49.7 % | −0.0019 [−0.0067, +0.0046] |

- Training on cell values does not cost accuracy and probably gains some; with the burnable share the interval
  excludes zero, by little. One configuration, one protocol: confirm it in `ml.train` before claiming it.
- Feeding cell values to a model trained on pixel values (what `ml.map --aoi ca` with the current `full` models
  does) ranks ignitions about as well as the pixel model itself, so the map is usable before a retrain; its
  probabilities are not calibrated for that input.

## Limits

- 200 m cells mix fuel models; `fuel_model` is the mode and can be a non-burnable code in a cell that is 40 %
  burnable. For spread physics on `ca` the mode of the burnable codes (`fuel_model_burnable`) with the loads scaled
  by `burnable_share` may be the better input; not tested.
- Slope is the mean 30 m slope, not the slope of the 200 m surface. Aspect is defined wherever any 30 m pixel of
  the cell is steeper than 2°, so fewer cells are "flat" than pixels: at the ignitions the pixel aspect is defined
  in 47.3 % of rows and the cell aspect in 66.2 % (LFMC rows: 89.9 % and 95.3 %); on the grid it is defined in
  74 % of land cells. `northness` and `eastness` therefore have fewer gaps on the map than in the table.
- LF 2016 and LF 2022 canopy were converted with the scales of the LF 2025 tables (the services report the same
  code ranges); their own class CSVs were not checked.
- LANDFIRE LF 2023 and LF 2024 fuel services exist and were not staged; the table does not use them either.
- The items were not made by the registered connectors. Running `fuellake run landfire-… --aoi ca --force-scale`
  later would write items with the same ids from a single nearest-neighbour clip (if the server allowed it).

## HRRR gusts for California, 2016–2024 (estimate only, nothing downloaded)

- The archive has what the connector needs back to 2016 (checked `hrrr.20160101` and `hrrr.20160815`: the idx files
  list `GUST:surface:anl` and `WIND:10 m above ground:0-1 hour max fcst`).
- Each message is the whole CONUS grid, about 1.17 MB, whatever the AOI. The configured sampling (two hours a day,
  two variables) is 4.7 MB per day: 3,288 days ≈ 15.4 GB. The `smm` run of 683 days took 1,378 s on one thread
  (2.3 MB/s), so 2016–2024 would take about 1 h 50 min sequentially, 15–20 minutes split over 8 lake roots by year.
- That sampling recovers on average 82 % of the hourly daily-maximum gust (fuel-lake's own calibration). Six-hourly
  sampling doubles the volume (31 GB, about 30 minutes in 8 lanes); hourly is 185 GB.
- The dataset is `scales: [local]`; a `ca` run needs `--force-scale`, its own lake root, and
  `build-points`-style sampling at the ignitions afterwards.

## Reproduce

```bash
cd ~/spaceapps/NASA-ACEs/data-aggregation        # read only; its venv has fuel-lake and GDAL
export LAKE_ROOT=~/spaceapps/lanes/ML PYTHONPATH=~/spaceapps/ml-work/research/ca_statics
.venv/bin/fuellake init
.venv/bin/python $PYTHONPATH/dl_all.py                      # 600 windows: topo, FBFM40 x 3, canopy LF 2025
.venv/bin/python $PYTHONPATH/dl_all.py cc_lf2016 ch_lf2016 cbh_lf2016 cbd_lf2016 cc_lf2022 ch_lf2022 cbh_lf2022 cbd_lf2022
.venv/bin/python $PYTHONPATH/agg.py && .venv/bin/python $PYTHONPATH/agg2.py      # blocks/, blocks2/
.venv/bin/python $PYTHONPATH/register.py && .venv/bin/python $PYTHONPATH/register2.py
python3 $PYTHONPATH/merge_view.py                           # point the lake view at lanes/D + lanes/ML
cd ~/spaceapps/NASA-ACEs/machine-learning
export FUELLAKE_CONFIG_DIR=~/spaceapps/NASA-ACEs/data-aggregation/config LAKE_ROOT=~/spaceapps/ml-work/lake
.venv/bin/python ~/spaceapps/ml-work/research/ca_statics/checks.py          # checks.json
.venv/bin/python ~/spaceapps/ml-work/research/ca_statics/cellstatics.py     # points_statics_cell200m.parquet
.venv/bin/python ~/spaceapps/ml-work/research/ca_statics/quicktest.py       # quicktest.json
```

Files: `lf.py` (windows, download), `dl_all.py`, `agg.py`, `agg2.py`, `register.py`, `register2.py`,
`merge_view.py`, `checks.py`, `cellstatics.py`, `quicktest.py`, logs `dl.log`, `dl2.log`, `agg.log`,
`checks.log`, results `checks.json`, `quicktest.json`, `points_statics_cell200m.parquet`.
