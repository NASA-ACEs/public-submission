# LANDFIRE vegetation type (EVT) as a stand-in for species

Run 2026-11-15 against model v2 (`model-v2`, table `points_v2.parquet`). Exploratory; scripts and intermediate
files are on the server in `~/spaceapps/ml-work/research/veg/`.

## Verdict

- **EVT does not close the species gap of the LFMC map.** In the repo protocol (unseen 50 km block and unseen
  year) no form of EVT improves v2 `full`: the best is −0.01 RMSE, two forms are significantly worse. Knowing the
  species the crews sampled improves the same model by 3.09 [2.16, 3.93].
- **Why.** EVT names the dominant cover of the pixel; crews sample a shrub. Of 99 locations where manzanita or
  ceanothus is sampled, 74 lie in cells EVT calls conifer or oak forest: the shrub is the understory. EVT calls
  66 % of the chamise locations chaparral. And what EVT does know is already in v2: LANDFIRE's fuel models and
  canopy layers are derived from EVT.
- **Large-fire model:** class shares of the cell add +0.0019 [−0.0070, +0.0097] PR-AUC in unseen years, no gain by
  the repo's rule. ROC-AUC rises by 0.011 [0.004, 0.020]; on test years 2022–2024 PR-AUC falls (0.112 → 0.102).
- **Not adopted.** Nothing to deploy for LFMC. The statewide 200 m layers made for the test are kept (below) in
  case the fire model's ROC-AUC gain is wanted later.

## Data

LANDFIRE Existing Vegetation Type, 30 m, EPSG:5070, from the same public image services as the fuel layers of
`docs/research/ca-statics.md`:
`https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF<year>/LF<year>_EVT_CONUS/ImageServer` for LF 2016, LF 2022
and LF 2025 (LF 2023 and LF 2024 exist too and were not fetched). Native windows for the 60 blocks of the `ca`
grid: 180 requests, 654 MB, 117 s, no failures. The services carry no attribute table; names, lifeform, physiognomy
and group come from `https://landfire.gov/sites/default/files/CSV/LF2025/LF2025_EVT.csv` (1,069 rows), completed
with `…/CSV/LF2016/LF16_EVT_200.csv` for codes absent from it. Every code met at the LFMC locations is in the table.

Each code is assigned to one of 16 vegetation classes by rule from its group name and physiognomy (`classes.py`;
no labels used): chaparral, coastal scrub, sagebrush, desert scrub, other shrub, oak woodland, other hardwood,
conifer-oak, dry conifer, mesic conifer, juniper-pinyon, grass, riparian, sparse, agriculture, developed.

On the `ca` grid (200 m), per release: the mode of the code, the mode among vegetated pixels, and the share of each
class among the cell's 30 m pixels (aggregated from the blocks as the fuel layers were). Table rows take the cell's
values from the release valid on their date (LF 2016 from 2017-01-01, LF 2022 from 2022-10-01), as v2 does for
fuels; rows of 2016 have none. Fill on LFMC rows: 88.5 %.

## 1. Does EVT describe what is sampled at a location?

295 LFMC locations with at least 5 site-dates; field class from the species the crews sampled there.

| Field class (locations) | Mean LFMC | Most frequent EVT class of the 200 m cell |
|---|---|---|
| chamise (115) | 81 | chaparral 76, coastal scrub 12, mesic conifer 8 |
| manzanita / ceanothus (99) | 104 | mesic conifer 28, dry conifer 21, chaparral 15, oak woodland 14, conifer-oak 11 |
| other shrub (32) | 109 | chaparral 9, coastal scrub 6, oak woodland 5 |
| sage (18) | 123 | sagebrush 6, chaparral 3 |
| tree (31) | 127 | mesic conifer 17, dry conifer 6, conifer-oak 5 |

- Chamise locations called chaparral by EVT: 45 % at the 30 m pixel, 70 % in the 210 m window, 66 % in the cell.
  At the pixel, 22 of the 115 are "developed" (roads). Of the cells EVT calls chaparral, 74 % are chamise locations.
- Shrub sampled against a shrub class in EVT: agreement 53 % (pixel), 61 % (window), 58 % (cell).
- Correlation of the field chamise share with the cell's chaparral share: 0.54; of the field shrub share with the
  cell's share of shrub classes: 0.37.
- EVT changes between releases at the same pixel: 20 % of locations LF 2016 → LF 2022, 19 % LF 2022 → LF 2025.

Site-mean LFMC explained by static descriptors alone (block cross-validation over locations, R²):

| Predictors | Ridge | Trees |
|---|---|---|
| EVT class shares of the cell | 0.20 | 0.29 |
| LANDFIRE fuels, canopy, terrain of the cell | 0.24 (236 locations) | 0.18 |
| both | 0.23 | 0.29 |
| field species composition | 0.61 | 0.60 |

## 2. LFMC model, repo protocol

15,591 site-dates, 322 locations, 102 blocks; every (block fold, year) cell predicted by a model that saw neither
(`ml.train` settings, median model). Gain = RMSE of v2 minus RMSE of the variant, bootstrap over blocks (500).
Site statistics over 295 locations with ≥ 5 rows; within-site correlation over locations with ≥ 10 rows.

| Variant | RMSE | Gain over v2 | 95 % interval | R² | R² of site means | Within-site r |
|---|---|---|---|---|---|---|
| v2 `full` | 25.51 | | | 0.456 | 0.425 | 0.687 |
| (i) + EVT code as a category (classes under 150 rows merged) | 25.86 | −0.34 | [−0.61, −0.07] | 0.441 | 0.428 | 0.674 |
| (ii) + vegetation class as a category | 25.53 | −0.02 | [−0.31, 0.28] | 0.455 | 0.452 | 0.680 |
| (ii) + EVT group as a category | 25.64 | −0.13 | [−0.41, 0.16] | 0.450 | 0.455 | 0.676 |
| (iii) + class shares of the 200 m cell | 25.65 | −0.13 | [−0.73, 0.33] | 0.450 | 0.450 | 0.677 |
| (iv) + mean LFMC of the EVT code, training folds only | 26.07 | −0.56 | [−0.87, −0.28] | 0.431 | 0.422 | 0.667 |
| (iv) + mean LFMC of the vegetation class | 25.53 | −0.01 | [−0.20, 0.15] | 0.455 | 0.436 | 0.685 |
| (iii) + (iv) | 26.02 | −0.51 | [−1.05, −0.07] | 0.434 | 0.436 | 0.669 |
| oracle: + field species composition | 22.42 | +3.09 | [2.16, 3.93] | 0.579 | 0.633 | 0.759 |
| oracle + class shares | 22.51 | +3.01 | [2.15, 3.86] | 0.576 | 0.631 | 0.754 |
| without LANDFIRE layers (`core` + rain sums) | 26.66 | −1.15 | [−1.81, −0.52] | 0.406 | 0.334 | 0.653 |
| `core` + rain sums + class shares | 25.99 | −0.47 | [−1.23, 0.31] | 0.435 | 0.443 | 0.668 |

- EVT lifts the R² of site means a little (0.425 → 0.45) and gives nothing in RMSE or in the within-site
  correlation.
- Without the LANDFIRE layers, class shares recover 0.67 [−0.33, 1.72] of the 1.15 [0.52, 1.81] those layers give:
  the same information by another route, not a new one.
- With the class shares in the model the species oracle still adds 3.14 [2.18, 4.10].
- 90 % interval (block folds): raw coverage 72.9 % for v2 and 72.2 % with shares; widened on 2016–2023 and checked
  on 2024: 85.1 % and 85.3 %. No change.

## 3. Large-fire model

v2 `full` plus the 16 class shares of the ignition's cell, same settings and protocol (`ml.train`, 5 seeds, each
year predicted by models that did not see it; `ml.compare` against `models/v2/full`; 74,568 ignitions, 477 large).
One variant, no tuning.

| | v2 `full` | + class shares | Change [95 %, weeks] |
|---|---|---|---|
| PR-AUC, fuel age applied | 0.0675 | 0.0694 | +0.0019 [−0.0070, +0.0097] |
| PR-AUC before fuel age | 0.0636 | 0.0652 | |
| ROC-AUC | 0.835 | 0.846 | +0.0108 [0.0044, 0.0197] |
| Large fires in the top 10 % | 52.4 % | 56.4 % | +4.0 points [0.0, 6.6] |
| Large fires in the top 1 % | 17.0 % | 17.2 % | |
| PR-AUC, test years 2022–2024 | 0.112 | 0.102 | |
| Unseen blocks, PR-AUC | 0.0616 ± 0.0047 | 0.0661 ± 0.0048 | |
| Single-seed sd of PR-AUC | 0.0009 | 0.0020 | |

The ordering of the bulk of ignitions improves (ROC-AUC, top 10 %); the top of the ranking, which PR-AUC measures
and the map's upper classes depend on, does not, and is worse in the three latest years. By the repo's rule
(PR-AUC, interval excluding zero) this is not a gain.

## 4. Recommendation

- **LFMC:** do not add EVT. The level of LFMC at a place stays limited by not knowing the sampled species; a map
  of dominant cover does not supply it. What would: species or understory maps, or field species composition where
  crews sample (it is in FEMS), used to draw the map per species.
- **Large fire:** leave v2 as it is. If a later version optimises ROC-AUC or the top decile, the class shares are
  a candidate; they need the layers below registered in the lake first.
- No map for the Santa Monica Mountains was made: there is no LFMC gain to show.

## Files

- `~/spaceapps/lanes/ML/raw/landfire-ca30m/evt_lf2016|evt_lf2022|evt_lf2025/`: 60 native 30 m windows each (630 MB).
- `~/spaceapps/lanes/ML/staged/landfire-evt/ca/landfire-evt__ca__<date>__lf<year>/`: `evt_mode.tif`,
  `evt_mode_veg.tif` (int16 codes), `evt_share_<class>.tif` (16 layers, 0–1), `ca` grid, nodata −9999 (176 MB).
  **Not registered** in the fuel-lake catalog: that needs a dataset file `config/datasets/landfire-evt.yaml` in
  `data-aggregation` (static raster, releases as for `landfire-fbfm40`, variables = the 18 layers, licence as the
  other LANDFIRE sets), and its config directory is shared and has uncommitted changes. Nothing existing in
  `lanes/ML` was touched.
- Research folder: `evt.py` (download), `classes.py` (code → class), `agg_evt.py`, `assemble.py` (200 m layers,
  `points_evt.parquet`: EVT columns for all 120,428 table rows), `sites.py`, `sites_evt.npz` (pixel and 7×7 window
  codes at 329 locations, three releases), `test1.py`, `test1.json`, `lfmc_evt.py`, `lfmc_evt.json`,
  `lfmc_evt_oof.parquet`, `compare_v2_veg_shares.json`, `fire_shares.log`, `evt_2025.csv`, `evt_2016.csv`.
- Also written: `~/spaceapps/ml-work/tables/points_v2_evt.parquet` (v2 table plus the EVT columns) and
  `~/spaceapps/ml-work/models/x/veg_shares/` (the fire variant).

## Limits

- One class scheme, chosen by rule; a different grouping of the 1,069 EVT types was not tried.
- Class shares are at 200 m, as v2's static inputs are; the 30 m pixel is "developed" (mostly a road) at 17 % of
  the 295 locations.
- LF 2016 is used for 2017 to September 2022 rows, so vegetation after later fires is described by the older map.
- EVC and EVH (cover and height of existing vegetation) were not tested; LANDFIRE's canopy cover and height, which
  v2 has, are derived from them for forest.
- The oracle uses the species sampled over all years at a location: an upper bound, not a deployable input.
