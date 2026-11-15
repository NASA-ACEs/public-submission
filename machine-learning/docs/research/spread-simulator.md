# Fire growth from an ignition point (`spreadsim`)

Run 2026-11-15 on the lake view `~/spaceapps/ml-work/lake` (read only). Files are in
`~/spaceapps/ml-work/research/spreadsim/` on the server: `spreadsim.py`, `test_spreadsim.py`, a copy of
`rothermel.py` with one added function, the validation (`val.py`, `val_an.py`) and the GCOV case (`case_gcov.py`).

## Verdict

- **The simulator works and is fast**: it turns the model's head-fire layers into the area a fire would cover
  after N hours, for any wind, in 0.25 s per ignition from a result bundle.
- **It reproduces Rothermel exactly** (head fire for any wind from stored components: within 6·10⁻⁷ of
  `ml.rothermel`) and the ellipse with a known discretisation error (area −1 % with no wind, −7 % at a
  length-to-width ratio of 2, with 32 neighbours).
- **It is not a better predictor of which ignitions become large fires than the v1 model.** Ranking fires
  ≥ 400 ha among 8,727 ignitions of 2017–2024: simulated 72 h area AUC 0.737, v1 `p_large_fire` 0.831
  (difference −0.093 [−0.114, −0.070]). It beats the head rate of spread at the ignition point (0.610) and
  roughly equals flame length there (0.720). Adding it to `p_large_fire` gains nothing (+0.004 [−0.003, 0.011]).
- **Simulated areas are not fire sizes.** With no suppression and one day's wind held for 72 h, the median
  simulated area is 1,118 ha for fires that in reality stayed below 4 ha. Use the output as growth potential and
  shape, not as a forecast of size.
- **Shape and direction carry modest, measurable skill.** The simulated area lies in the direction of the real
  perimeter within 45° for 45.6 % of 649 fires (chance 25 %; the gridMET downwind direction alone 42.1 %;
  difference +3.5 points [0.2, 6.6]). Grown to the final area, it overlaps the real perimeter better than a circle
  of the same area: IoU 0.187 against 0.155 (mean difference +0.030 [0.020, 0.041]); no better than a circle for
  fires of 40–400 ha, clearly better above 4,000 ha (0.356 against 0.265).
- **NISAR GCOV changes the simulated growth where fuel is regrowing.** In the 2018 Woolsey scar the area after
  6 h under a 12 m/s north-east wind falls from 35 to 19 ha with the GCOV fuel state (16 ha with fuel age as
  well); at the edge of the 2025 scar from 324 to 215 ha (20 ha with fuel age). In mature chaparral nothing
  changes (2,168 against 2,190 ha). This is the model's assumption made visible, not a validation: no fire
  occurred in the area during the GCOV record.

## 1. Method

`spreadsim.py`. `components`, `head` and `arrival` need numpy and scipy only (`components` also the sibling
`rothermel.py`); `isochrones` and `simulate` read GeoTIFFs and reproject with rasterio, which the backend has.

| Function | What it does |
|---|---|
| `components(code, rmin, tmmx, fm100, lfmc, green, canopy_cover, canopy_height, fuel_state)` | per cell, everything of Rothermel's surface spread that does not depend on wind or slope (table below) |
| `head(components, wind_10m, wind_from, slope, aspect)` | head-fire rate, direction, length-to-width ratio, intensity and flame length for any wind; the vector combination of wind and slope and the wind limit are those of `rothermel.spread` |
| `directional_rate(ros, direction, lw, bearing)` | rate towards any bearing: ellipse with the ignition at the rear focus, eccentricity e = √(LW² − 1)/LW, rate = head · (1 − e)/(1 − e·cos θ) (Andrews 2018, RMRS-GTR-371) |
| `arrival(ros, direction, lw, cell_m, starts, limit_min, neighbours, permeability, gap_cells)` | minutes to each cell: graph of 8, 16 or 32 neighbours, travel time between two cells = half the distance at each cell's rate towards the other, Dijkstra (`scipy.sparse.csgraph`) with a time limit |
| `isochrones(arrival, hours, transform, crs)` | GeoJSON (WGS84) MultiPolygon per hour with `area_ha` |
| `simulate(layers, lon, lat, hours, wind)` | the whole chain from a bundle directory or a dict of arrays; `wind=(m/s at 10 m, degrees from)` overrides the wind of the map day |
| `ground_cell(transform, crs, lat)` | ground size of a cell north-south and east-west: EPSG:6933 is equal-area, not conformal (at 37° N a 200 m cell is 217 m by 185 m) |

Rules of the graph: cells with no rate (not burnable, unknown fuel) block the fire; a diagonal move is not allowed
through a closed corner; a longer move needs every cell of the box it spans to burn; an ignition on a
non-burnable cell starts at the nearest burnable one within 10 cells. `gap_cells` lets the fire cross
non-burnable strips up to that width at half the neighbouring rate: at 30 m roads are one or two cells wide and
would otherwise stop every fire (a stand-in for short-range spotting; `simulate` uses 60 m). `permeability`
(fuel age) multiplies the rate. That is a simplification: it was measured as the chance that a cell burns when a
large fire reaches it, not as a rate.

`rothermel.py` here is the repo's module with one refactoring: the wind-independent part of `spread()` is the
new function `bed()`. `spread()` returns bit-identical arrays (20,000 random cases, all 40 fuel models, fuel
state 0.3–1.2).

## 2. Tests (`test_spreadsim.py`, 6 passed)

- `head()` from stored components against `ml.rothermel.grid_layers`: largest relative difference 6·10⁻⁷ in rate
  of spread, intensity and flame length, 0.0001° in direction; zeros and NaN in the same cells.
- No wind, uniform fuel: a circle of radius R·t. Ellipse with wind: head run exact (1.000), backing run 0.975,
  length-to-width 2.06 for a nominal 2.0.
- A wall across the grid stops the fire; a gap in it is found (arrival more than 5 times the straight-line
  time); a one-cell strip stops it without bridging and is crossed with `gap_cells=2`.
- Ignition on a non-burnable cell starts beside it; permeability slows the fire only where it applies.
- Isochrones: area within 5 % of the circle, coordinates in WGS84.

Discretisation error of the reached area against the exact ellipse (uniform fuel, radius 80 cells):

| Length-to-width | 8 neighbours | 16 neighbours | 32 neighbours |
|---|---|---|---|
| 1.0 (no wind) | −10.0 % | −2.8 % | −1.0 % |
| 1.25 | −17.0 % | −5.0 % | −2.1 % |
| 1.5 | −23.6 % | −8.5 % | −3.3 % |
| 2.0 | −35.9 % | −15.6 % | −6.8 % |
| 3.0 | −52.7 % | −30.6 % | −16.6 % |

The graph always under-reaches (fire can only move along the graph's directions). At the ignitions of the
validation the median length-to-width ratio is 1.18 (90th percentile 1.36), so 32 neighbours keep the error
at 1–3 %; under strong wind the flanks are understated.

Run time, one thread, 12 × 12 km at 30 m (401 × 401 cells), random fuels: 0.10 / 0.23 / 0.46 s for 8 / 16 / 32
neighbours with a 6 h limit, 0.14 / 0.30 / 0.53 s for the whole window. `simulate()` from GeoTIFFs: 0.25 s.

## 3. Retrospective check on 8,727 ignitions

**Sample.** FPA-FOD ignitions 2017–2024 with gridMET weather: every fire ≥ 4 ha (3,727, of which 431 ≥ 400 ha) and
5,000 random smaller ones (weight 12.6, so that weighted figures refer to all 66,676 ignitions; weighted base
rate of large fires 0.65 %). Perimeters (CAL FIRE FRAP) matched to 800 of the 1,237 fires ≥ 40 ha: same year,
start within 5 days, ignition within 3 km, and the same name or an area within a factor of 2.

**Inputs.** The statewide 200 m rasters staged this night (`~/spaceapps/lanes/ML`: LANDFIRE fuel model as the
mode of the 30 m cells, slope, aspect, canopy cover and height), the release valid before the fire (LF 2016 until
2022-09-30, then LF 2022); gridMET of the ignition day (daily mean wind and direction, minimum humidity, maximum
temperature, 100-h fuel moisture), one value for the whole window; LFMC from the v1 model that did not see the
year; herbaceous greenness by season; years since fire from the perimeters as of the ignition day. Window
±20 km (±40 km for matched fires ≥ 4,000 ha), 32 neighbours, no bridging (at 200 m roads are not cells).

**Ranking large fires (≥ 400 ha).** AUC with sampling weights, 95 % bootstrap over fires; Spearman against the
final size among the 3,727 fires ≥ 4 ha.

| Predictor | AUC | 95 % | PR-AUC | Spearman with size |
|---|---|---|---|---|
| simulated area, 6 h | 0.691 | [0.665, 0.717] | 0.020 | 0.086 |
| simulated area, 24 h | 0.739 | [0.715, 0.760] | 0.019 | 0.122 |
| simulated area, 72 h | 0.737 | [0.716, 0.758] | 0.032 | 0.126 |
| 72 h, without fuel-age permeability | 0.736 | [0.716, 0.758] | 0.034 | 0.125 |
| 72 h, wind × 2 | 0.718 | [0.694, 0.738] | 0.067 | 0.102 |
| head rate of spread at the ignition | 0.610 | [0.584, 0.636] | 0.013 | 0.034 |
| flame length at the ignition | 0.720 | [0.697, 0.745] | 0.022 | 0.144 |
| **v1 `p_large_fire` (unseen year)** | **0.831** | [0.810, 0.852] | 0.057 | 0.259 |

| Difference in AUC | Value | 95 % |
|---|---|---|
| simulated 72 h − v1 `p_large_fire` | −0.093 | [−0.114, −0.070] |
| simulated 72 h − head rate at the ignition | +0.127 | [0.105, 0.148] |
| fuel-age permeability on − off | +0.002 | [0.000, 0.003] |
| wind × 2 − wind × 1 | −0.020 | [−0.025, −0.015] |
| `p_large_fire` + simulated area (rank average) − `p_large_fire` | +0.004 | [−0.003, 0.011] |

- The simulation adds the surroundings to the point: it ranks better than the rate of spread at the ignition
  cell. It does not reach the statistical model, and adds nothing to it.
- Doubling the wind (a crude stand-in for gusts) ranks worse in AUC; its PR-AUC is higher (0.067), i.e. it
  concentrates a few more large fires at the very top. One number each way; not a result to build on.
- Fuel-age permeability does not change the statewide ranking: only 319 of the 8,727 ignitions start inside a
  scar (12 of them large). There it cuts the median simulated 72 h area from 10,467 to 4,147 ha.

**Simulated area against reality.**

| Final size | n | Simulated median, 6 h (ha) | 24 h | 72 h | Window edge reached in 72 h |
|---|---|---|---|---|---|
| < 4 ha | 5,000 | 8 | 96 | 1,118 | 10 % |
| 4–40 ha | 2,490 | 52 | 958 | 9,451 | 21 % |
| 40–400 ha | 806 | 72 | 1,397 | 13,729 | 27 % |
| 400–4,000 ha | 294 | 52 | 1,324 | 12,162 | 25 % |
| ≥ 4,000 ha | 137 | 44 | 1,651 | 17,792 | 7 % (window ±40 km for matched fires) |

The simulated area separates fires below 4 ha from the rest and nothing above that; its log-log slope on the
final size is 0.19. Real fires are stopped by suppression, night, and weather that changes; the simulation has
none of these. Where the window edge was reached the 72 h area is truncated.

**Direction and shape** (758 matched fires whose perimeter lies inside the window; median size 306 ha).

| Bearing compared with ignition → centre of the real perimeter (649 fires with the centre ≥ 600 m away) | Median error | Within 45° (chance 25 %) | Within 90° (chance 50 %) |
|---|---|---|---|
| ignition → centre of the simulated area | 51.5° | 45.6 % [41.8, 49.3] | 70.3 % |
| head-fire direction at the ignition | 52.6° | 43.1 % | 67.6 % |
| downwind direction (gridMET, daily) | 58.0° | 42.1 % | 66.6 % |

Simulation against the downwind direction: +3.5 points within 45° [0.2, 6.6], median error −6.5° [−11.1, 0.0].
By size, within 45°: 42 % (40–400 ha), 47 % (400–4,000 ha), 52 % (≥ 4,000 ha).

Shape: the simulation is grown until it covers as many cells as the real perimeter, then compared with it and
with a circle of the same area around the ignition.

| Final size | n | IoU, simulation (median) | IoU, circle | Simulation better | Share of the perimeter reached within 72 h (median) |
|---|---|---|---|---|---|
| 40–400 ha | 416 | 0.110 | 0.111 | 38 % | 1.00 |
| 400–4,000 ha | 232 | 0.223 | 0.168 | 52 % | 0.97 |
| ≥ 4,000 ha | 110 | 0.356 | 0.265 | 61 % | 0.48 |
| all | 758 | 0.187 | 0.155 | 46 % | 0.99 |

Mean difference over all fires +0.030 [0.020, 0.041]. At 200 m a 40–400 ha fire is 10–100 cells, too few for
shape to mean much. In 1.6 % of fires the simulation could not fill the perimeter's area inside the window.

## 4. NISAR GCOV case: Santa Monica Mountains, fuels of 2026-10-20

30 m, LANDFIRE 2025 fuel models, gridMET of the day (wind 2.5 m/s from 153°), LFMC of the v1 map, bridging 60 m,
32 neighbours. GCOV: `ml.gcov.fuel_state` (47 scenes, two tracks); 25 % of the cells inside perimeters of the last
10 years show no backscatter deficit and count as unburned islands. Starts chosen by rule, not by eye
(`case_gcov.py`). Area in ha after 3 h / 6 h:

| Start | Wind | A: LANDFIRE fuel only | B: + GCOV fuel state | C: + fuel age from perimeters | D: + GCOV fuel state and fuel age within the GCOV extent |
|---|---|---|---|---|---|
| unburned side of the 2025 scar edge (−118.5184, 34.1287; years since fire 1.7 on the scar side) | map day | 70 / 461 | 71 / 466 | 1 / 258 | 1 / 266 |
| | 12 m/s from north-east | 39 / 324 | 17 / 215 | 1 / 40 | 1 / 20 |
| mature chaparral, no fire since 2015 (−118.6430, 34.1098; fuel state 0.95) | map day | 356 / 943 | 358 / 950 | 356 / 943 | 358 / 950 |
| | 12 m/s from north-east | 979 / 2,168 | 1,004 / 2,190 | 934 / 1,950 | 982 / 2,052 |
| inside the 2018 Woolsey scar (−118.8308, 34.0571; 7.9 years, fuel state 0.50) | map day | 0.4 / 2.3 | 0.1 / 0.4 | 0.4 / 1.6 | 0.1 / 0.4 |
| | 12 m/s from north-east | 9.5 / 34.6 | 4.1 / 19.1 | 7.8 / 28.7 | 2.9 / 16.3 |

- **Where GCOV matters:** in the eight-year-old scar the fuel map (shrub model SH2) and the fuel-age modifier
  (permeability 0.90) say the fuel is almost back; GCOV says half the woody structure is missing and the simulated
  area falls by 45 % (35 → 19 ha). Under the north-east wind the head rate at that start drops from 1.9 to
  0.7 m/min.
- **Where it does not:** mature chaparral (fuel state near 1) and the map-day run at the scar edge, where the
  wind carries the fire away from the scar.
- **Fuel age dominates in the young scar** (permeability 0.39 at 1.7 years): 324 → 40 ha, and 20 ha with GCOV.
  With GCOV extent (D) the area can also be larger than with perimeters alone (C), because islands without a
  deficit keep their fuel (map-day run: 258 → 266 ha).
- Figure: `case_gcov.png` (left: LANDFIRE only; right: with GCOV fuel state and fuel age; contours at 1, 3, 6 h,
  wind 12 m/s from the north-east). GeoJSON of one run: `case_gcov_isochrones.geojson`.

Nothing here is validated against a fire: no fire burned in the area while GCOV was recording. The head rates
in mature SH5 chaparral under 12 m/s (65–69 m/min) are Rothermel's, without crown fire or spotting, and are high.

## 5. Interface for integration

**(a) Layers `ml.map` should add to the bundle** so the backend can simulate for any wind without the lake. All
float32, nodata −9999, on the bundle grid; 18 MB for the Santa Monica AOI.

| Layer | Unit | Definition |
|---|---|---|
| `spread_ros0` | m/min | rate of spread with no wind on flat ground for the day's fuel moistures and the GCOV fuel state; 0 = not burnable |
| `spread_wind_a`, `spread_wind_b` | – | wind factor φw = a · U^b, U = midflame wind in m/s |
| `spread_slope_k` | – | slope factor φs = k · tan²(slope) |
| `spread_waf` | – | midflame wind = waf · wind at 10 m (includes the 10 m → 20 ft step and canopy sheltering) |
| `spread_wind_cap` | m/s | effective midflame wind above which the rate no longer grows (Rothermel's limit, 0.9 · reaction intensity) |
| `spread_intensity_k` | kW/m per m/min | fireline intensity = k · rate of spread; flame length = 0.0775 · intensity^0.46 |
| `slope`, `aspect` | degrees | LANDFIRE topography (aspect = downslope direction) |
| `wind_speed`, `wind_from` | m/s, degrees | gridMET wind of the map day at 10 m (the default wind) |
| `spread_permeability` | 0–1 | already in the bundle |

In `ml.map` after the LFMC prediction (same arguments as the `rothermel.grid_layers` call):
`c = spreadsim.components(code, df["rmin"], df["tmmx"], df["fm100"], lfmc, green, cover, height, state_now)`,
then write `c`, `slope`, `aspect`, `wind_speed` (`df["vs"]`) and `wind_from` (`th`). `rate_of_spread` and the
other physics layers stay as they are: `head(c, vs, th)` returns the same numbers. Needs this folder's
`rothermel.py` (`bed()`).

**(b) Call.** `spreadsim.simulate("<bundle dir>", lon, lat, hours=(1, 3, 6), wind=None | (speed, from))` returns a
GeoJSON FeatureCollection: one MultiPolygon per hour with `hours`, `area_ha`, `cells`; collection `properties`
with the wind used, head rate, direction and flame length at the start, `fuel_age_permeability`, `edge_reached`
(the window was too small for the last hour: raise `window_m`, default 6 km) and a note. Options: `window_m`,
`neighbours` (32), `use_permeability`, `gap_m` (60). `spreadsim.py` plus `rothermel.py` are self-contained; the
backend needs only `spreadsim.py` (`components` is not called there). A dict of arrays with `transform` and
`crs` works in place of the directory.

## Limits

- Surface fire only; no crown fire, no spotting beyond the 60 m bridging, no suppression, one wind and one set
  of fuel moistures for the whole run. Areas are potential, not forecasts; the check above shows they do not
  predict size.
- Wind is the gridMET daily mean on a 4 km grid: direction and speed of the hours that mattered are unknown.
  Doubling it did not help.
- The graph under-reaches the ellipse, more under strong wind (table in section 2).
- Permeability as a rate multiplier is an assumption; statewide it changes nothing measurable, locally a lot.
- Validation at 200 m with the mode fuel model per cell, one fuel release per period, perimeters matched by rule
  (800 of 1,237); 2016 ignitions left out (no fuel release before them).
- The GCOV case shows the size of the model's assumption, not its truth.
