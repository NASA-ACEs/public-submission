# E3: the surroundings of the cell in the large-fire model

2026-11-15. Question: v3.2's largest single input is the built-up share of the 200 m cell. Does it help to
describe what lies around the cell (fuel continuity, buildings nearby, relief within a few km)?

**Verdict by the rule written before the run: accepted.** All three conditions hold, and for the first time the
repo's own regression test (`ml.compare`, PR-AUC in unseen years) says "better".

| Condition | Result | Met |
|---|---|---|
| 2025–2026, fires ≥ 400 ha (80 of 13,296 ignitions): paired ROC-AUC change against v3.2 has a 95 % interval above zero | +0.030 [0.011, 0.049] | yes |
| 2025–2026, ≥ 400 ha: PR-AUC point change ≥ 0 | 0.0668 → 0.0799 (+0.013; interval [−0.025, 0.034]) | yes |
| Unseen year 2016–2024 (477 large of 74,568): PR-AUC not significantly worse | 0.0712 → 0.0824 (+0.0112 [0.0003, 0.0215]) | yes |

Caveats, before the detail:

- **The years 2025–2026 have now decided two changes** (vegetation shares in E2, this set in E3). They are no
  longer an untouched test for whatever comes next, and the second use is weaker evidence than the first.
- The unseen-year PR-AUC interval starts at 0.0003. Refitting v3.2 with five other seeds moves its PR-AUC by
  0.0013 and the candidate's by 0.0023, so "better" is narrow. The ROC-AUC gain is not narrow.
- In 2022–2024 alone PR-AUC is lower (0.114 → 0.105, interval [−0.032, 0.023]) and the share of large fires in
  the top 1 % falls from 28 % to 23 %; ROC-AUC rises there too.
- In 2025 alone neither change for ≥ 400 ha excludes zero; 2026 carries the ROC-AUC result.
- The set was fixed in advance and used whole. Nothing was selected on 2025–2026.

## The features (17, fixed before any result)

Means over a disc around the cell on the `ca` grid (EPSG:6933, 200 m), radius 5 cells (`_r1km`) and 15 cells
(`_r3km`), over the cells of the disc that have data, defined only where the centre cell has data:

| Source layer (release valid on the row's date) | Features |
|---|---|
| `landfire-evt` `evt_share_developed` | `evt_share_developed_r1km`, `_r3km` |
| `landfire-evt` `evt_share_agriculture` | `evt_share_agriculture_r1km`, `_r3km` |
| `landfire-evt`, sum of oak_woodland, other_hardwood, conifer_oak, dry_conifer, mesic_conifer, juniper_pinyon | `forest_share_r1km`, `_r3km` |
| `landfire-evt`, sum of chaparral, coastal_scrub, sagebrush, desert_scrub, other_shrub | `shrub_share_r1km`, `_r3km` |
| `landfire-fuel-load` `burnable_share` (the share of burnable 30 m pixels, not the 0/1 `burnable`) | `burnable_r1km`, `_r3km` |
| `landfire-fuel-load` `total_load` | `total_load_r1km`, `_r3km` |
| `landfire-canopy` `canopy_cover` | `canopy_cover_r1km`, `_r3km` |
| `landfire-topo` `elevation` | `elevation_sd_r1km` (standard deviation in the disc), `tpi_r1km`, `tpi_r3km` (cell minus disc mean) |

- Releases as everywhere else (`ml.data.LANDFIRE_FROM`): LF 2016 from 2017-01-01, LF 2022 from 2022-10-01, LF 2025
  from 2025-10-01. Rows of 2016 have the three terrain features and none of the other fourteen; those fourteen
  are in `DATED`, so the 2016 fold is scored by models fitted without them (`ml.train.fire_cv`).
- "1 km" and "3 km" are nominal. EPSG:6933 is equal-area, not conformal: at 34° N a cell is 191 m east–west by
  209 m north–south, at 42° N 172 by 233 m, so the disc is an ellipse (0.96 × 1.05 km at 34° N for `_r1km`).
- Computed by FFT (`context.py`); values rounded to 5 decimals to remove FFT noise around zero.
- Check (`check.py`, `check.json`): at 9 ignitions (2 each of 2016, 2019, 2023; 3 of 2025–2026 on both sides of
  the LF 2025 date) 69 values recomputed with plain loops from the source rasters; largest difference 9 × 10⁻⁶.
  The 2016 rows have no dated features, as intended.
- Fill: 99.9 % of training rows from 2017 on, 99.8 % of the 2025–2026 ignitions (the rest lie outside the region
  mask, where no LANDFIRE layer has values).

Candidate = v3.2 code and table plus these 17 columns in the large-fire model (71 inputs instead of 54), no
monotone constraints on the new ones; LFMC models unchanged. Code: worktree `~/spaceapps/ml-work/wt/e3` (tag
`model-v3.2` with the list added to `ml/data.py`); models `models/full`.

## Unseen years (each year predicted by models that did not see it), candidate against v3.2

Paired, bootstrap over calendar weeks, predictions after calibration and fuel age (`protocol.py`,
`protocol.json`, `compare_v32_e3.json`).

| Test years | Ignitions | Large | PR-AUC (change [95 %]) | ROC-AUC (change [95 %]) | Large fires in top 1 / 5 / 10 / 20 % |
|---|---|---|---|---|---|
| 2016–2024 | 74,568 | 477 | 0.0712 → 0.0824 (**+0.0112 [0.0003, 0.0215]**) | 0.8671 → 0.8847 (**+0.0176 [0.0112, 0.0251]**) | 17.8 / 44.2 / 61.0 / 76.9 → 19.1 / 47.0 / 63.7 / 82.4 |
| 2017–2024 | 66,676 | 431 | 0.0814 → 0.0956 (**+0.0142 [0.0007, 0.0284]**) | 0.8695 → 0.8874 (**+0.0179 [0.0105, 0.0254]**) | 20.0 / 45.2 / 62.2 / 76.8 → 20.6 / 48.7 / 65.4 / 82.1 |
| 2022–2024 | 23,493 | 114 | 0.1142 → 0.1048 (−0.0094 [−0.0321, 0.0233]) | 0.8805 → 0.9021 (**+0.0217 [0.0085, 0.0382]**) | 28.1 / 49.1 / 67.5 / 76.3 → 22.8 / 55.3 / 66.7 / 84.2 |
| 2016 (terrain features only) | 7,892 | 46 | 0.0508 → 0.0448 (−0.0060 [−0.0224, 0.0119]) | 0.8493 → 0.8627 (**+0.0134 [0.0039, 0.0237]**) | 6.5 / 34.8 / 47.8 / 71.7 → 8.7 / 39.1 / 52.2 / 71.7 |

- Share in the top 10 %, 2016–2024: +2.7 points [0.2, 6.4].
- `ml.compare`: fire "better", LFMC "no difference" (same LFMC models).
- Before fuel age: PR-AUC 0.0681 → 0.0790, ROC-AUC 0.8642 → 0.8821. Single seeds: 0.0673 ± 0.0018 → 0.0777 ±
  0.0018. Refit with five other seeds: 0.0013 and 0.0023 away.
- Whole blocks held out (secondary; leaks the fire episode): PR-AUC 0.0696 → 0.0820.
- Gain over the burning index: +0.065 [0.042, 0.090] (v3.2: +0.054 [0.032, 0.083]).
- Brier score 0.006142 → 0.006098.

**What the trees use.** The 17 features hold 41 % of the total gain; the 16 vegetation shares of the cell fall
from 22 % to 8 %. Largest inputs of the candidate: `elevation_sd_r1km` 6.4 %, `vpd` 6.2 %,
`evt_share_developed_r3km` 6.2 %, `evt_share_developed_r1km` 6.0 %, `wind10_max` 5.9 %, `slope` 4.2 %,
`elevation` 4.2 %, `gust_max` 3.5 %, `evt_share_developed` 3.2 %, `total_load_r3km` 2.8 %. The five largest of
the new set: `elevation_sd_r1km`, `evt_share_developed_r3km`, `evt_share_developed_r1km`, `total_load_r3km`
(2.8 %), `shrub_share_r3km` (2.8 %). Smallest: `evt_share_agriculture_r1km` (0.4 %).

## Years after training: 2025–2026 (13,296 WFIGS ignitions), candidate against v3.2

Saved models on the same rows, paired, bootstrap over calendar weeks (`oot.py`, `oot.json`,
`pred_oot.parquet`). The v3.2 predictions recomputed here equal the stored ones exactly.

| Sample | Outcome | Events | PR-AUC (change [95 %]) | ROC-AUC (change [95 %]) | Events in top 1 / 5 / 10 / 20 % |
|---|---|---|---|---|---|
| 2025–2026 | ≥ 400 ha | 80 | 0.0668 → 0.0799 (+0.0131 [−0.0253, 0.0339]) | 0.8714 → 0.9013 (**+0.0300 [0.0113, 0.0490]**) | 15.0 / 37.5 / 53.8 / 73.8 → 18.8 / 42.5 / 67.5 / 83.8 |
| | ≥ 40 ha | 239 | 0.0795 → 0.0978 (**+0.0183 [0.0032, 0.0334]**) | 0.7845 → 0.8154 (**+0.0309 [0.0156, 0.0463]**) | 8.4 / 25.9 / 38.9 / 57.7 → 11.3 / 25.5 / 46.9 / 65.3 |
| | ≥ 4 ha | 875 | 0.1362 → 0.1467 (**+0.0105 [0.0011, 0.0198]**) | 0.6967 → 0.7124 (**+0.0157 [0.0077, 0.0238]**) | 3.7 / 13.9 / 22.9 / 39.7 → 4.3 / 13.7 / 25.6 / 42.7 |
| 2025 (8,335) | ≥ 400 ha | 38 | 0.1007 → 0.1024 (+0.0017 [−0.0773, 0.0357]) | 0.8949 → 0.9126 (+0.0177 [−0.0063, 0.0427]) | 26.3 / 55.3 / 68.4 / 73.7 → 23.7 / 55.3 / 65.8 / 81.6 |
| | ≥ 40 ha | 126 | +0.0082 [−0.0183, 0.0293] | 0.7959 → 0.8153 (**+0.0194 [0.0019, 0.0377]**) | |
| | ≥ 4 ha | 507 | +0.0095 [−0.0010, 0.0202] | 0.6975 → 0.7171 (**+0.0196 [0.0073, 0.0321]**) | |
| 2026 (4,961) | ≥ 400 ha | 42 | 0.0583 → 0.0665 (+0.0082 [−0.0127, 0.0477]) | 0.8425 → 0.8839 (**+0.0414 [0.0105, 0.0713]**) | 7.1 / 19.0 / 40.5 / 71.4 → 11.9 / 28.6 / 64.3 / 85.7 |
| | ≥ 40 ha | 113 | 0.0737 → 0.0973 (**+0.0237 [0.0094, 0.0460]**) | 0.7659 → 0.8096 (**+0.0437 [0.0193, 0.0695]**) | |
| | ≥ 4 ha | 368 | +0.0096 [−0.0059, 0.0258] | 0.6921 → 0.7007 (+0.0087 [−0.0037, 0.0215]) | |

Calibration: 86.9 large fires expected against 80 observed (v3.2: 86.0). By year: 2025 51.0 expected, 38 observed
(v3.2: 52.4); 2026 35.9 expected, 42 observed (v3.2: 33.6).

## Exploratory, after the main result: which part carries it (unseen years only)

Two more models, each with one part of the set, in the unseen-year protocol only (`parts.py`, `parts.json`; the
2025–2026 ignitions were not scored with them, and the decision above does not depend on this).

| Comparison (2016–2024, 477 large) | PR-AUC (change [95 %]) | ROC-AUC (change [95 %]) |
|---|---|---|
| v3.2 → + 3 terrain features | 0.0712 → 0.0766 (+0.0054 [−0.0008, 0.0123]) | 0.8671 → 0.8736 (+0.0066 [0.0026, 0.0108]) |
| v3.2 → + 14 land-cover and fuel features | 0.0712 → 0.0806 (+0.0094 [−0.0016, 0.0192]) | 0.8671 → 0.8826 (+0.0155 [0.0091, 0.0228]) |
| v3.2 → all 17 | 0.0712 → 0.0824 (+0.0112 [0.0003, 0.0215]) | 0.8671 → 0.8847 (+0.0176 [0.0112, 0.0251]) |
| terrain only → all 17 | +0.0057 [−0.0045, 0.0152] | +0.0111 [0.0053, 0.0175] |
| land cover only → all 17 | +0.0018 [−0.0043, 0.0077] | +0.0021 [−0.0006, 0.0051] |

Most of the gain is in the land-cover and fuel surroundings (built-up share and fuel load around the cell); the
terrain features add a smaller part of their own, and on top of the land-cover set their addition is not
resolved. Neither part alone reaches a PR-AUC interval above zero; the whole set does, narrowly. This is not a
reason to drop the terrain features: the set was fixed in advance, and choosing a subset now would be selection
on the same years.

## What to add to the repo for v3.3

1. **`ml.data`**: `CONTEXT_DATED` (the 14 names from fuel, canopy and vegetation layers), `CONTEXT = CONTEXT_DATED +
   ["elevation_sd_r1km", "tpi_r1km", "tpi_r3km"]`; `FEATURES["full"]["fire"] += CONTEXT`; `DATED += CONTEXT_DATED`
   (so the 2016 fold is scored without them). No entries in `MONOTONE`. The exact lines are in
   `~/spaceapps/ml-work/wt/e3/src/ml/data.py`.
2. **One code path for table and map** (a module of about 40 lines; `context.py` and `attach.py` here are the
   working version):
   - `Disc(r)`: mean over the disc by FFT, over cells with data, NaN where the centre has none;
   - the 17 arrays on the `ca` grid for the releases valid on a day, from `landfire-evt` (`evt_share_developed`,
     `evt_share_agriculture`, the forest sum, the shrub sum), `landfire-fuel-load` (`burnable_share`, `total_load`),
     `landfire-canopy` (`canopy_cover`), `landfire-topo` (`elevation`), read with `grid.releases` / `grid.read`,
     rounded to 5 decimals;
   - table rows: the value of the row's cell from the release valid on its date, as `ml.series.statics_at_points`
     does (call it from `add_to_table` when `statics` is given). All rows need the columns, LFMC rows too:
     `ml.train` keeps a feature only if both parts of the table have it;
   - map cells: on `ca` the arrays themselves; on any other AOI the `ca` arrays sampled at the AOI's cell centres
     (nearest cell). Add the columns to `ml.grid.arrays` and to `ml.grid --check`.
3. **`smm` (30 m)**: take the values from the `ca` rasters, not from the `smm` layers. All 1,170,126 `smm` cells
   fall inside the `ca` grid and 75.4 % have values (the rest is sea). Computing discs on the `smm` layers would
   cut them at the AOI edge (the AOI is about 25 × 42 km, the larger disc is 6 km across) and would use a true
   circle where training used the EPSG:6933 ellipse. The context is then constant over the 6–7 `smm` pixels of a
   200 m cell, which is what the model was trained on.
4. **Cost** (measured): the three terrain rasters 7 s; one release (14 rasters) 30 s with 16 FFT threads; 3.7 GB
   of memory at the peak. A statewide map of one day needs one release: 38 s on top of the 10 minutes it takes
   now. The table needs all three releases: about 100 s. A rerun gives identical rasters. Kept on disk the 45 rasters take 1.3 GB (float32,
   deflate); they are in `rasters/` and can be reused as a cache.
5. **Train** the `full` set (5.5 minutes with 24 workers; `core` is unchanged), `ml.compare` against v3.2,
   `research/oot/evaluate.py` on ignitions that carry the 17 columns (`ignitions_2025_2026_e3.parquet` here, or
   `prep.py` once `add_to_table` adds them).

## Files (`~/spaceapps/ml-work/research/e3/`)

`context.py` (rasters), `attach.py` (columns at table rows), `check.py`, `check.json`, `rasters/{topo,lf2016,lf2022,lf2025}/`,
`points_e3.parquet`, `ignitions_2025_2026_e3.parquet`, `models/full/` (candidate), `compare_v32_e3.json`,
`protocol.py`, `protocol.json`, `oot.py`, `oot.json`, `pred_oot.parquet`, `parts.py`, `parts.json`,
`models_terrain_only/`, `models_cover_only/`. Nothing was changed in the repositories, the lake or
`~/spaceapps/training`; nothing was committed.
