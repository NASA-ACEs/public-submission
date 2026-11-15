# Running and extending the backend

See README for setup. Default URL `http://127.0.0.1:8000`; interactive OpenAPI `/docs`, schema `/openapi.json`. Backend Python code uses FastAPI and rasterio; no external requests during startup or serving.

## Implemented endpoints

Prefix `R=/v1/burnability/results/forest-demo-v1`.

| Method / endpoint | Returns |
|---|---|
| GET `/health` | Readiness and synthetic flag |
| GET `/v1/burnability/results` | Result discovery |
| GET `R` | Layer IDs, legends, units, URLs, bounds, zoom/source resolution and notices |
| GET `R/tilejson/{layer}` | TileJSON, XYZ URL template and bounds |
| GET `R/tiles/{layer}/{z}/{x}/{y}.png` | 256×256 RGBA Web Mercator XYZ tile; z=0..18 |
| GET `R/preview/{layer}.png?width=512` | Whole dataset PNG; width=64..2048, proportional height |
| GET `R/assets/{layer}` | Tiled float32 GeoTIFF with internal overviews and nodata=-9999 |
| GET `R/vectors/illustrative_pathways?bbox=-120,37,-119.5,37.5&zoom=10` | WGS84 GeoJSON; viewport filtered and thinned by zoom |
| GET `R/grid/susceptibility_index` | Numeric Web Mercator grid of the demo index |
| POST `R/assess-lines` | Line spans classified against the index |
| POST `/v1/burnability/results/rescan` | Re-read `RESULTS_DIR` (real results, see below) |
| GET `R/point?lon=-119.75&lat=37.25` | Nearest source-cell values, units from metadata; null for nodata |
| GET `R/provenance` | Synthetic source assumptions and formula |
| POST `/v1/burnability/jobs` with `{"mode":"synthetic_demo"}` | Existing completed demo job, HTTP 202; no new computation |
| GET `/v1/burnability/jobs/demo-job` | Completed demo job status/result |

Unknown result/layer: 404. Invalid coordinates, dimensions, tile addresses or mode: 422. Vector query `limit=1..5000`, `offset>=0`; response `next_offset` is null at end. Bbox must be ordered WGS84 west,south,east,north; split antimeridian extents. Pagination is against the chosen zoom/bbox, so reset offset when either changes. Vectors include crossing lines by bounding-box intersection and are not clipped.

## Image size and zoom

Map scroll changes z and the geographic extent represented by each 256px tile. The frontend loads only visible tiles; this is preferable to downloading a full larger raster on every wheel event. z=0..18 matches the existing frontend wheel limit. Source detail is approximately 200m; useful native zoom is about 10 in California. Beyond that, the same information is enlarged, not made more precise. Tile responses have fixed colours across zooms and transparent outside/nodata pixels. Values are resampled for display only. Previews explicitly vary pixel width for cards/export, not for georeferenced map overlay.

Source TIFFs include factors 2/4/8/16 average overviews. Rendering uses rasterio/GDAL reprojection to EPSG:3857; source TIFF remains EPSG:6933. A bounded 128-entry in-memory cache stores tile PNGs. Preview renders are not cached in memory. PNGs include ETag, conditional 304, Cache-Control max-age=3600 and X-Synthetic. Metadata and vector URLs identify the fixed result version. Downloads support FileResponse HTTP range handling but are **not certified COGs**.

## Data and limitations

Startup generates six procedural fields with seed 42: soil moisture, live surface fuel load, LFMC, air temperature, simplified energy and susceptibility. A curved lake is nodata. Energy and susceptibility follow the legacy documented formulas, using synthetic inputs; no trained model is run. Sparse arrows are a separate visual mockup with fictional west-to-east wind, 600m display lengths and no time/speed meaning. There is no probability output. `as_of` is a fixed scenario timestamp, not observation freshness.

The service is intended for local development. It has no auth, persistence for jobs, rate limiting or production acquisition. Configure a public origin/HTTPS proxy appropriately when deployed; URL generation uses the incoming request base URL, so configure trusted proxy headers in that deployment. Do not expose arbitrary filesystem roots or model paths to callers.

## Real results from machine-learning (`RESULTS_DIR`)

The demo above is always served. Real model output is served next to it when `RESULTS_DIR` points at a directory
of result bundles. Without the variable nothing changes.

```bash
RESULTS_DIR=/path/to/bundles uvicorn app.main:app --host 127.0.0.1 --port 8000
curl -s localhost:8000/v1/burnability/results          # demo + real results, "latest", "rejected"
curl -s localhost:8000/v1/burnability/results/latest   # metadata of the newest real result
```

**Bundle.** One sub-directory per result, as written by `machine-learning` `python -m ml.map --out <dir>`:
`result.json` (`schema_version: "backend-result-1"`), one GeoTIFF per entry of `layers`
(`{"<layer_id>": {"path": "<file>.tif", "kind": "index|probability|class|continuous"}}`) and optionally
`vectors.geojson` and `provenance.json`. Any CRS and grid work (California: EPSG:6933 at 200 m; Santa Monica
Mountains: EPSG:32611 at 30 m). The bundle is never modified.

**Validation** (`app/results.py`). A bundle is registered under its `result_id` only if the manifest is valid
JSON with that schema version, `result_id` matches `[A-Za-z0-9._-]{1,100}` and is not `latest`, `rescan` or the
demo id, and every layer path is relative, stays inside the bundle after resolving symlinks, exists and opens
with a CRS. One bad layer rejects the whole bundle. Rejected bundles are listed with the reason under `rejected`
in `GET /v1/burnability/results` (directory name only, no server paths). Two bundles with the same `result_id`:
the newer `result.json` wins.

**Refresh.** The directory is rescanned at most every `RESULTS_RESCAN_S` seconds (default 15) when a result is
requested, and on `POST /v1/burnability/results/rescan`. A bundle is re-read when its `result.json` changes, so
write `result.json` last (`ml.map` does).

**Fetching bundles at startup** (`app/archive.py`; off by default). A host without a way to upload files gets
the bundles from an archive:

| Variable | Meaning |
|---|---|
| `RESULTS_ARCHIVE_URL` | one address or several separated by commas; each a `.tar` or `.tar.gz` whose top level holds bundle directories (a directory with a `result.json`). `http://` and `https://` only. Needs `RESULTS_DIR` |
| `RESULTS_ARCHIVE_TOKEN` | optional; sent as `Authorization: Bearer …`, never logged |
| `RESULTS_ARCHIVE_MAX_MB` | largest archive accepted, also the largest it may unpack to (default 4096) |

A background thread downloads each archive into `RESULTS_DIR/.incoming`, unpacks it there (links, special files
and paths that leave the directory are refused), moves the bundle directories into `RESULTS_DIR` and rescans. The
server starts and answers meanwhile; a bundle already present is kept. A failing host is tried four times; a
missing archive (4xx) is not retried. Nothing is fetched again while the bundles an archive delivered are on
disk (`RESULTS_DIR/.archives` records them): delete a bundle directory to have it fetched again at the next
start. `/health` reports `results_archive`: `downloading`, `ready` or `failed`, with `results_archive_detail`
(archive number and file name, never the address). A failure leaves the demo and `/v1/grid` running. The archive
must sit at an address the server can reach; where to host it is the team's choice.

**Endpoints.** Every `/v1/burnability/results/{result_id}/...` endpoint of the demo works for a real result,
with `synthetic: false` and `X-Synthetic: false`:

| Endpoint | Real result |
|---|---|
| `GET /v1/burnability/results` | demo first, then real results newest first with `valid_from`, `valid_to`, `as_of`, `model_version`, `bbox_wgs84`, `aoi` (if the manifest has it), `assessment_layer`, `layers`; plus `latest` and `rejected` |
| `GET R` | same shape as the demo plus `valid_from`, `valid_to`, `model_version`, `target`, `assessment_layer`, `class_breaks`, `default_threshold`, `rank_reference` (what `risk_index` is ranked among, when the bundle says: `ignitions`, `large`, `built_up_share_at_most`, `median_p`; else null), `ordinary_day` (the weather inputs at their medians over the training ignitions, against which `susceptibility_index` and `weather_factor` are computed; null in bundles older than ml.map v3.3.1, where `susceptibility_index` is a copy of `risk_index`), `soil_source`, `nisar_share`, `nisar_effect`, `source_item_count`, `source_items_sample` (first 5), `notes` (also as `notices`), and `extra` with every manifest key the backend does not know |
| `GET R/tilejson/{layer}`, `R/tiles/{layer}/{z}/{x}/{y}.png`, `R/preview/{layer}.png` | as the demo |
| `GET R/assets/{layer}` | the bundle's GeoTIFF |
| `GET R/point?lon=&lat=` | value of every layer at the cell, plus `units` per layer |
| `GET R/grid/{layer}?bbox=w,s,e,n&max_cells=65536` | numeric Web Mercator grid of any layer, see below |
| `POST R/assess-lines` | defaults to the manifest's `assessment_layer`; optional `layer` |
| `GET R/vectors/vectors` | `vectors.geojson` of the bundle (`illustrative_pathways` is accepted as an alias) |
| `GET R/vectors?bbox=w,s,e,n&n=400&hours=1` | head-fire arrows sampled for a map view, see "Arrows for a map view"; only for results with `rate_of_spread` and `spread_direction` |
| `GET R/provenance` | `provenance.json` with absolute paths reduced to file names |
| `GET R/scars?min_years=7.5&max_years=8.5` | outline of the land that last burned that many years before the map day, see "Outline of older fire scars"; only for results with `years_since_fire` |
| `GET R/spread?lon=&lat=&hours=1,3,6` | where a fire started at the point would be after those hours; only for results with component layers, see "Fire growth from a point" |

`result_id` may be `latest`: the real result with the newest `valid_to`, or the demo when there is none. URLs
inside the metadata always carry the concrete id, so tiles stay cacheable.

**Layer styles.** From `kind` and the layer name; a manifest layer entry may override with `title`, `units`,
`range` and `palette` (hex colours).

| Layer | Range and colours |
|---|---|
| `lfmc*` | 30–200 % dry mass, red to green |
| `soil_moisture` | 0–0.5 m³/m³, yellow to blue |
| `kind: index` (`risk_index`, `susceptibility_index`) | 0–1, yellow to red |
| `kind: probability` (`p_large_fire`) | 0 to the 98th percentile of the layer (the values are small), yellow to red |
| `kind: class` (`risk_class`) | classes 1–5 as five discrete colours, nodata 0 |
| units `degrees` and a name with `direction`, `aspect` or `_from` (`spread_direction`, `aspect`, `wind_from`) | a compass direction: fixed 0–360 and a palette that closes on itself (dark blue at north, blue east, yellow south, red west), nearest-neighbour everywhere, `cyclic: true` in the layer's metadata so a client labels the legend N E S W |
| units `x` (`weather_factor`: the day's odds divided by those of an ordinary day) | range and palette from the manifest (0.25–4, white at 1, blue below, red above); the layer's metadata has `neutral: 1` so a client marks that value on the legend |
| `susceptibility_index` in a result with `ordinary_day` | titled "Land susceptibility": the rank with ordinary weather, what the land contributes |
| name contains `minus`, `change`, `anomaly`, `delta`, `diff` | diverging around zero, ± the larger of the 2nd and 98th percentile magnitudes; reversed when the name contains `dry` |
| anything else | 2nd–98th percentile of the layer |

**Large rasters.** Tiles, previews and grids read only the source window they cover, decimated to the output
resolution. A raster of 2 million cells or more without overviews gets a copy with overviews in
`RESULTS_CACHE_DIR` (default `<DATA_DIR>/results-cache/<result_id>/`) on first use (about 1.5 s for a
5000×5000 layer, then 20–70 ms per tile). `assess-lines` reads only the window under the submitted lines.

**Numeric grid.** `GET R/grid/{layer}` returns `width`, `height`, `transform`, row-major `values` (null = no
data) in EPSG:3857 over `bbox` (WGS84, default the whole result), never more than `max_cells` values (default
65 536, maximum 262 144). Beyond that the grid is decimated; the response says so in `decimated`, `decimation`
(source cells per output cell along one axis) and `cell_size_m`. For the demo the endpoint is unchanged and
exists for `susceptibility_index` only.

**Line assessment.** Body as for the demo. `region_bbox_wgs84` defaults to the result's bounds, `layer` to the
manifest's `assessment_layer` (`risk_index`), `threshold` to the lower bound of the upper classes: with
`class_breaks` 0.2/0.4/0.6/0.8 that is 0.6, so "higher" means classes 4 and 5. A probability layer needs an
explicit `threshold`.

**What the layers mean** is in the result's `notes` and in `machine-learning/docs/METHODS.md`. `risk_index` is
a rank of P(large fire | ignition), not a probability and not a forecast of an existing fire.

## Arrows for a map view (`R/vectors`)

`vectors.geojson` of a bundle has a fixed number of arrows (1,611 for California, about ten around Los Angeles),
so at most zooms there is nothing to see. `GET R/vectors?bbox=w,s,e,n&n=400&hours=1` samples the result's own
rasters for the view instead:

- an arrow starts at the centre of a cell and is the distance the head fire would run in `hours` (0–12, default
  1) along `spread_direction`, at most 5 km, as in the bundle; properties are those of `vectors.geojson`
  (`rate_of_spread_m_min`, `direction_deg`, `flame_length_m`, `run_hours`);
- cells are every `step`-th row and column of the result's grid, `step` the smallest power of two that gives at
  most `n` cells in `bbox` (`n` 1–2000, default 400). The lattice is fixed to the grid: panning does not move
  arrows, zooming in adds arrows between those shown. Cells without fuel (rate 0) or data give no arrow, so
  fewer than `n` come back; the answer carries `step_cells` and `spacing_m`;
- values are those of the sampled cell, never averages (a direction cannot be averaged). The run follows the
  true azimuth from the cell, not the grid's axes;
- without `bbox` the whole result; a `bbox` outside the result gives an empty collection; 404 for the synthetic
  demo and for results without the two layers (the metadata then has no `view_vectors_url`).

Windows up to 1.5 million cells are read from the files; for wider views of a large raster every 8th, 16th, ...
cell is kept in memory after the first such request (about 1 s once for California, a few milliseconds after).

## Outline of older fire scars (`R/scars`)

`GET R/scars?min_years=7.5&max_years=8.5` returns a GeoJSON `FeatureCollection` (WGS84) of polygons around the
cells of `years_since_fire` whose value lies in that range. machine-learning makes that layer from mapped fire
perimeters (CAL FIRE FRAP, NIFC), so the outline is where the fires of those years burned inside the result; the
perimeters themselves are not redistributed. One-cell gaps are closed, patches and holes under 4 ha are dropped
(roads and other unburnable cells have no fuel age), rings are simplified to about three quarters of a cell and
further if the answer would exceed 20,000 vertices; rasters over 4 million cells are read decimated. The answer
carries `area_ha`, `cell_m`, `simplified_to_m` and is cached per range. `min_years` and `max_years` are required,
0 to 100, `min_years` not above `max_years` (422 otherwise); 404 for the synthetic demo and for results without
the layer. Results that have it list `scars_url` in their metadata. For the Santa Monica bundle of 2026-10-20,
7.5 to 8.5 years (the Woolsey and Hill fires of November 2018): 8 polygons, 3,145 vertices, 80 KB, 0.2 s.

## Fire growth from a point (`R/spread`)

`GET R/spread?lon=&lat=&hours=1,3,6&wind_speed=&wind_from=&fuel_age=true&fuel_state=true` answers "if a fire started here,
where would it be after 1, 3 and 6 hours". It runs `app/spreadsim.py`, which is machine-learning's
`ml.spreadsim` copied unchanged (its header names the commit; update it from there, do not edit it here), on a
window of the bundle's **component layers**:

| Layer | Meaning |
|---|---|
| `spread_ros0` | rate of spread with no wind on flat ground, m/min (0 = not burnable) |
| `spread_wind_a`, `spread_wind_b` | wind factor `a × U^b`, U = midflame wind in m/s |
| `spread_slope_k`, `slope`, `aspect` | slope factor and the terrain it applies to |
| `spread_waf` | midflame wind = `waf` × wind at 10 m |
| `spread_wind_cap`, `spread_intensity_k` | Rothermel's wind limit; fireline intensity per m/min |
| `wind_speed`, `wind_from` | wind of the map day (used when the request gives none) |
| `spread_permeability` | fuel age: how readily a recently burned cell burns again (a displayed layer) |
| `spread_ros0_plain`, … (`*_plain` of the seven `spread_*` fuel components) | where NISAR's GCOV fuel state scaled the fuels (`ml.map` since v3.3.3): the same components from the fuel map alone |

A bundle that has them (`ml.map` since machine-learning commit `2704dec`) gets `capabilities: {spread: true,
spread_map_day_wind: …}`, `spread_url` and `spread_limits` in its metadata, and `capabilities` in the listing.
Layers of kind `component` are **left out of `layers`** (they are inputs, not maps) and named in
`component_layers`; `assets`, `point`, `grid` and tiles still serve them.

| Parameter | |
|---|---|
| `lon`, `lat` | the ignition, WGS84 |
| `hours` | 1 to 48 values (fractions allowed, e.g. every 10 minutes for an animation), each above 0 and at most 12; default `1,3,6` |
| `wind_speed`, `wind_from` | m/s at 10 m (0–40) and the direction it blows **from** (0–360); both or neither. Without them the wind of the map day is used |
| `fuel_age` | `false` leaves out the slowing inside recent fire scars; default `true` |
| `fuel_state` | `false` grows the fire on the fuel map alone (the `*_plain` components), to compare with the fire on fuels scaled by NISAR's L-band fuel state; default `true`. Only where the metadata has `capabilities.spread_fuel_state`; elsewhere 422 |

Response: a GeoJSON `FeatureCollection`, one `MultiPolygon` per hour (longest first) with `hours`, `area_ha`,
`cells`; and top-level `properties`: `wind_10m_m_s`, `wind_from_deg`, `wind_source` (`request` | `map_day`),
`head_rate_m_min`, `head_direction_deg`, `flame_length_m`, `fuel_age_permeability`, `nisar_fuel_state` (true
when the result has both sets of components and this fire used the one with the fuel state), `edge_reached` (the fire
reached the edge of the simulated window: the largest area is then a lower bound), `window_half_m`, `cell_m`,
`note`; plus `result_id`, `valid_from`, `valid_to`, `model_version`, `synthetic: false`.

- **404**: the result has no component layers (older bundles, the synthetic demo), with a message saying so.
- **422**: the point is outside the result; no burnable fuel within 10 cells of it (a point on a road starts
  beside it; a point in a town or on water is refused); `hours` or wind out of range; only one of the two wind
  parameters.
- The simulated window is 225 cells to each side (6.75 km at 30 m; at most 30 km on coarse grids). About 0.7 s
  on the 30 m Santa Monica bundle; the endpoint is a sync function, so FastAPI runs it in a worker thread, and
  the last 64 answers per result are kept.

What it is: surface-fire potential for **one wind and the fuels of the map day**, by minimum travel time over
Rothermel head-fire ellipses, slowed by fuel age. No suppression, spotting, crown fire or change of weather.
machine-learning validated it on 8,727 past ignitions: useful for direction and shape, **not a predicted fire
size** (it ranks large fires worse than the model's `p_large_fire`; `docs/research/spread-simulator.md` there).

## Real fuel risk in `/v1/grid` (`FUEL_RISK_RESULT`)

Off by default; without the variable the situation payload is byte-identical to before.

`FUEL_RISK_RESULT=<result_id|latest>` (needs `RESULTS_DIR`) samples the result's assessment layer and adds:

| Field | Meaning |
|---|---|
| `fuelRisk` | `resultId`, `layer`, `target`, `validFrom`, `validTo`, `modelVersion`, `classBreaks`, `threshold`, `drives`, `spreadMultiplier`, `tierScale` |
| `fires.clusters[].fuelRiskIndex`, `fuelRiskClass` | mean risk rank within 1 km of the fire centroid, and its class 1–5; null outside the map |
| `threats.lines[].fuelRisk` | over the line's threatened sections: `index` (length-weighted mean), `max`, `riskClass`, `highKm` (km at or above the threshold), `coveredKm`; null outside the map |
| `threats.lines[].sections[].fuelRiskIndex` | mean risk rank along that section |

`FUEL_RISK_DRIVES` (comma-separated, each opt-in) lets the result change or extend the analysis:

- `spread`: the fuel multiplier of a fire's 24 h run becomes `0.8 + 0.6 × risk rank` at the fire (0.8 to 1.4;
  reported as `spread[].fuelBoost`). Fires outside the map keep the default multiplier.
- `tiers`: in `threats.classify` the real map replaces the demo fuel score wherever it covers a line. The rank
  is scaled by `0.25 / threshold`, so a rank at the threshold counts as the demo's 0.25 and the existing cuts
  keep their meaning: with threshold 0.6, a line within twice the buffer of a fire becomes SHUT OFF when at
  least 35 % of its covered length is in the upper classes or its highest rank is 0.72 or more.

- `simulate`: adds `spread[].simulated`, the answer of `R/spread` for the fire's centroid with the mean forecast
  wind (isochrones after 1, 3 and 6 h), **next to** the ellipse: `ring`, `acres`, `headM` and the tiers are
  untouched. It is null for a fire outside the result, on no fuel, or when the result has no component layers.
  `fuelRisk.simulated` describes it. Each fire inside the result costs about 0.7 s once per situation.

`spread` and `tiers` are the same heuristics as before with a real input; they are not validated against outcomes. The map
day (`fuelRisk.validTo`) is weeks older than the live fires, because NISAR is published about 3.5 weeks after
acquisition. If the configured result is missing, the grid behaves as if the variable were unset.
`app/grid/fuel.py` reads the raster in 1024-cell chunks, so sampling a whole state stays small in memory.

## Replacing the demo with algorithm output

Step 1 (a result registry) and step 3 (same display API, `synthetic=false`) are covered by the registry above;
jobs, auth and storage are not.


1. Implement a versioned immutable result registry and load each result's GeoTIFF paths, layer definitions, grid, provenance and optional vector files from the acquisition/ML worker. Do not infer paths from unchecked user input.
2. Enqueue real acquisition/ML jobs rather than returning `demo-job`. Preserve per-stage state and actionable failures. Inputs/contracts are in `../machine-learning/docs/API_HANDOFF.md` and `RESULTS_API.md`.
3. Retain the display API shape. Real results must set `synthetic=false`, preserve units/nodata, expose actual source times/maturity, and publish only genuinely computed layers.
4. v0.2 requires temperature/humidity/rain history and forecast wind/temperature plus terrain and a source. Current ML v0.1 does not implement it. Do not reuse the mock arrows as real forecast vectors.
5. Add ownership/auth, storage/CDN, job queue and production budgets as deployment requires. Move startup generation into a one-time demo-seeding step before using multiple workers.

Tests check tile output across zooms, transparent noncoverage, sizes, ETags, downloads, invalid inputs, point values, vector detail filtering, job restrictions and CORS. These are engineering checks, not scientific validation.

## Numeric grid for power-line screening

`GET R/grid/susceptibility_index` provides the small demo raster in EPSG:3857 with `width,height,transform,values,threshold,synthetic,as_of`. Values are row-major numeric scores rounded to 5 decimals, with JSON null for nodata. Reprojection uses nearest source pixel. Threshold 0.25 is an exploratory display policy. This avoids deriving values from coloured PNG tiles or making thousands of point requests. Production regions require bounded grid windows, server-side line/raster intersection or another tiled numeric format.

## Regional map-based asset assessment (no distance rule)

`POST R/assess-lines` takes WGS84 GeoJSON features (LineString/MultiLineString), optional `region_bbox_wgs84` (defaults to demo bounds) and `threshold` (default 0.25). It assesses the submitted line geometry directly against the native susceptibility raster. It needs no hotspots and uses no 3km buffer. Metadata exposes `line_assessment_url`.

Response: classified GeoJSON subsegments and per-line length/coverage/score summaries. At most 100m geodesic spacing, sampled at subsegment midpoints; missing/outside-region data are unknown. Limits: 200 features, 20,000 vertices, 20,000 samples. Zero-length features have zero coverage and null aggregate score. Unknown source IDs/invalid geometry/ranges/budget excess return 422 (unknown result returns 404). Detailed request and frontend semantics: `FRONTEND.md`, preferred integration section.

This is spatial overlay against the selected map, not a fire simulation or damage assessment. The available map remains synthetic fuel susceptibility. To use operational prediction maps, replace demo-only storage with a versioned real-result registry and preserve each layer's event, forecast window, coverage and source maturity. Do not use illustrative wind arrows to infer risk values.
