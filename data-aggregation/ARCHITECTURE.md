# fuel-lake architecture and contracts


This file is the contract every contributor (human or agent) codes against. Change it only through the coordinator.
It describes the system as built (integration pass of 2026-11-14), including where the build deviated from the
first draft.

## Purpose

`fuel-lake` collects the data behind the project "soil moisture → live fuel moisture (LFMC) → fire spread risk" into one lake, catalogs it with STAC, and gives processing microservices one simple way to find and read it. The lake holds data about one **monitored region**, the State of California (`config/lake.yaml`), on two kinds of AOI: local high-resolution AOIs such as the Santa Monica Mountains (`smm`, 30 m UTM) and the whole state on the NISAR SME2 grid (`ca`, EASE-Grid 2.0, 200.179 m). The team's two downstream systems publish their outputs back into the lake (interface collections `fuel-state` and `spread-risk`) and find each other's outputs through the catalog.

## Principles

1. **Local first, S3-shaped.** All storage goes through `fsspec` URLs under `LAKE_ROOT`. Default is a local directory outside the source tree (`../fuel-lake-data`). An `s3://` root (MinIO or AWS) must work without code changes. No cloud resources are created by this project.
2. **Analysis-ready on the AOI grid.** Scene rasters are staged already reprojected to the AOI target grid, so consumers stack them without reprojection.
3. **Everything is cataloged.** Nothing in `staged/` or `features/` exists without a STAC item. Consumers discover data only through the catalog (or the API over it).
4. **Honest status.** Every dataset declares what was actually tested: `tested`, `search-only`, `stub`, or `unverified` (see "Validation levels"). Never claim a source works when it was not run.
5. **No secrets, no personal data.** Credentials only from environment variables (`EARTHDATA_TOKEN`, `FIRMS_MAP_KEY`, `CDSAPI_URL`/`CDSAPI_KEY`). Never write them to files, logs, exceptions or test output; error texts pass through `config.redact`. The Earthdata bearer token goes only over https to the hosts in `connectors._earthdata.DATA_HOSTS` (`opendap.earthdata.nasa.gov`) and `DATA_HOST_SUFFIXES` (`*.earthdatacloud.nasa.gov`), never to CMR, URS or redirect targets (`wants_token`, shared by NISAR, OPERA and SMAP). HTTP `User-Agent: fuel-lake/0.1`.
6. **One monitored region.** Every AOI lies inside the region, every staged or published output is clipped to it, and `fuellake validate` checks it (see "Region"). Source files much larger than the region are cached outside the lake; `raw/` keeps only region subsets.
7. **Licences travel with the data.** Each dataset config states its licence and whether derived data may be redistributed. OlmoEarth outputs and the Rao et al. 250 m LFMC maps are excluded (licence terms conflict with the Space Apps rules).

## Runtime

- Python 3.12 via `uv` (`uv sync`, `uv run …`). Package `fuellake` in `src/fuellake`.
- Docker is not available on the development machine: `docker/` files and `.dockerignore` are provided but have never been run.

## Repository layout

```
fuel-lake/
  pyproject.toml  uv.lock  LICENSE (Apache-2.0)  README.md  ARCHITECTURE.md  .env.example  .gitignore  .dockerignore
  config/
    lake.yaml                           lake-level settings: `region: california`
    regions/california.yaml + .geojson  the monitored region (US Census Bureau boundary, see "Region")
    aois/smm.yaml  aois/ca.yaml         local AOI (30 m UTM) and the statewide region AOI (SME2 grid, 200.179 m)
    datasets/<dataset-id>.yaml          one file per dataset (owned by that connector's author)
    datasets/features-cube.yaml         gold collections (no connector; written by `fuellake build-features`)
    datasets/matchups.yaml
    datasets/fuel-state.yaml            interface collections (no connector; written by LakeClient.publish_*)
    datasets/spread-risk.yaml
    schemas/biomass_drop.schema.json    drop-zone sidecar schema
  src/fuellake/
    config.py  storage.py  registry.py  grid.py  rasters.py  tables.py
    catalog.py  contracts.py  events.py  pipeline.py  cli.py
    region.py                           region predicates and the central clip
    publish.py                          publishing into interface collections
    client.py                           consumer SDK
    connectors/base.py + one module per source (+ private helpers prefixed with _)
    features/_common.py  features/cube.py  features/matchups.py
    services/api.py  services/example_consumer.py
  docs/microservices.md                 guide for processing-microservice developers
  scripts/probe_earthdata.py            checks EARTHDATA_TOKEN access without printing it
  scripts/build_region.py               rebuilds config/regions/<id>.geojson from a Census boundary file
  tests/                                pytest; markers: network, earthdata, slow
  docker/Dockerfile  docker/compose.yaml
```

## Lake layout (relative to LAKE_ROOT)

```
raw/<dataset>/<yyyy>/<mm>/<file>                  source files or region/AOI subsets as fetched (and connector caches)
staged/<dataset>/<aoi>/<item_id>/<asset>.tif      form A rasters (COG, AOI grid)
staged/<dataset>/<aoi>/<item_id>/data.zarr        form B rasters (native-grid time series, Zarr v3)
staged/tables/<table>/<dataset>/part-<key>.parquet  GeoParquet 1.1, one part per source unit and AOI
features/<aoi>/cube.zarr                          gold datacube (Zarr v3)
features/<aoi>/matchups.parquet                   gold label × feature table (GeoParquet)
catalog/catalog.json                              static STAC root (self-contained, relative hrefs)
catalog/<collection>/collection.json
catalog/<collection>/<item_id>/<item_id>.json
events/<yyyy-mm-dd>.jsonl                         append-only event log
runs/<run_id>.json                                run manifests
inbox/biomass/                                    drop zone for the biomass subsystem
```

One writer at a time per lake root (the pipeline, `build-features` and publishers alike). Concurrent writers are not supported; document, do not lock.

Outside the lake, `$FUELLAKE_CACHE_DIR` (default `~/.cache/fuel-lake`) caches source files whose coverage is much larger than the region: the global Globe-LFMC workbook (`globe-lfmc/`) and the nationwide FIRMS archive CSVs (`firms/<product>/`, one file per `Last-Modified` version). Only their region-filtered subsets are written to `raw/`.

## Configuration

### Lake and region (`config/lake.yaml`, `config/regions/<id>.yaml`)

```yaml
# config/lake.yaml
region: california          # absent file or `region: null` = no region (nothing checked or clipped)
```

`config/regions/california.yaml` records `id`, `name`, `geometry: california.geojson` (EPSG:4326, one
MultiPolygon), `metric_crs: EPSG:3310` (buffers and distances), `tolerance_m: 1000`, the `source` (provider,
URL, vintage, feature, md5, processing) and the `license`. The polygon is the US Census Bureau 2025 cartographic
boundary file `cb_2025_us_state_500k` (https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_500k.zip),
feature STATEFP 06, transformed from NAD83 to WGS84, **not simplified** (5,134 vertices, 15 parts; error 0 m
against the source). It is a US Government work; the metadata asks for acknowledgement of the Census Bureau and
says the outline is generalised (1:500,000) and clipped to the shoreline. `scripts/build_region.py` rebuilds it.

### AOI (`config/aois/<id>.yaml`)

```yaml
id: smm
name: Santa Monica Mountains
bbox: [-118.95, 33.98, -118.50, 34.20]   # WGS84 lon/lat
crs: EPSG:32611
resolution: 30                            # metres, target grid for form A
scale: local                              # local (default) | region
nisar: {track: 13, frame: 71, direction: descending}   # preferred stack, coverage to verify
```

The target grid is the bbox projected to `crs`, snapped outward to whole cells of `resolution` counted from the
snap `origin` (a cell corner, map units; default `(0, 0)`), so every cell edge is `origin + k × resolution`.
`reference_grid: <name>` takes `crs`, `resolution` and `origin` from a named global grid in
`fuellake.grid.REFERENCE_GRIDS` (giving them as well is allowed only if they match). `ease2-200m` is EASE-Grid 2.0
Global (EPSG:6933) with the NISAR SME2 cell, `2 × 17367530.44516138 / 173520 = 200.17900466991` m, counted from the
NSIDC upper-left corner `(-17367530.44516138, 7314540.83011508)`; these are the constants verified against real
SME2 files, defined once in `fuellake/grid.py` and re-exported by `connectors/nisar_sme2.py`.

`ca` is the region AOI: bbox `[-124.41, 32.53, -114.13, 42.01]` (the region envelope rounded outward),
`reference_grid: ease2-200m`, `scale: region`. Its grid is 4956 × 4827 cells (23.9 M; global EASE-Grid 2.0
columns 26794–31749, rows 12053–16879), every edge an SME2 cell edge, and 10,227,225 cells (409,822 km² on the
equal-area grid) lie inside California. `smm` keeps its 30 m EPSG:32611 grid. `aoi.grid_at(r)` keeps the origin, so
a coarser cube on `ca` with `r = 5 × 200.179` m is the EASE-Grid 2.0 1 km grid. `load_aoi` raises `OutsideRegionError` (a `ValueError`) for an AOI that is not
inside the region (see "Region").

### Dataset (`config/datasets/<id>.yaml`)

```yaml
id: nisar-sme2                 # also the STAC collection id
title: NISAR L3 soil moisture (SME2)
kind: raster-a | raster-b | table
table: lfmc_samples            # tables only
connector: fuellake.connectors.nisar_sme2:NisarSme2Connector   # null for the gold collections
status: tested | search-only | stub | unverified
provider: NASA/ISRO NISAR via ASF DAAC
homepage: https://...
access: {method: cmr+https, auth: none | earthdata | api_key | unknown, env: [EARTHDATA_TOKEN]}
license: {id: ..., url: ..., redistributable: true|false, attribution: "...", notes: "..."}
cadence: 12 days               # "static ..." marks a time-invariant layer (see STAC, static collections)
native_resolution: 200 m
maturity: PROVISIONAL           # NISAR only
scales: [local]                 # AOI scales the dataset supports (default [local])
variables:
  - {name: soil_moisture, unit: m3/m3, dtype: float32, nodata: NaN, valid_range: [0.0, 0.7], resampling: bilinear}
    # interface datasets also use `required: false` for optional variables
item_properties: [...]          # interface datasets: properties publishers must set (see "Interface collections")
caveats: ["...", "..."]
options: {...}                  # connector-specific settings (free form)
```

- `status: interface` means: no connector, the team's systems publish into it (`LakeClient.publish_*`).
- `scales`: the pipeline refuses a dataset on an AOI whose `scale` is not listed (status `failed`, error
  `refused: ...`), skips it in `run all`, and runs it anyway with `--force-scale` (`force_scale=True`). Shipped:
  `[local]` for the high-resolution scene datasets (NISAR GCOV/SME2, OPERA RTC-S1, Sentinel-2, DEM, LANDFIRE
  FBFM40); `[local, region]` for NISAR SME2 (on `ca` its cells are copied, never resampled), FEMS, gridMET,
  ERA5-Land, FIRMS, fire perimeters, biomass and the interface and gold collections; `[region]` for Globe-LFMC (its rows cover the whole region whatever the AOI). A config
  without `scales` is local-only.

- `auth: unknown` means "not yet verified" (the inventory's "verify"); no shipped config uses it any more.
- `access.env` lists environment variable **names**, never values.
- A `raster-a` dataset whose `cadence` starts with `static` (cop-dem: "static (Copernicus DEM 2021 release)",
  landfire-fbfm40: "static (annual LANDFIRE releases; this config pins LF 2025)") is static (`DatasetConfig.is_static`).
  Tables are never static in this sense (Globe-LFMC's cadence "static (published 2024-03-25)" describes the release;
  its rows are dated samples).

## Data contracts

### Region (`fuellake.region`, applied to every output)

One definition, used by the pipeline, the publish API and `fuellake validate`:

| what | inside the region when | clip |
|---|---|---|
| AOI | its bbox lies within the region envelope widened by `tolerance_m` and intersects the polygon (a bbox cannot follow a coast: `smm` reaches into the Pacific, `ca` is the box around the state) | parts outside hold nodata |
| item bbox | same test | the pipeline clamps it to the region envelope |
| table row, point | within `tolerance_m` (1 km) of the polygon (the 1:500,000 shoreline is generalised) | dropped |
| table row, other geometry | intersects the polygon buffered by `tolerance_m`; kept whole (a fire crossing the state line keeps its full `area_acres`) | dropped |
| form A / cube pixel | its centre lies inside the polygon | set to nodata |
| form B cell (native 4–11 km grid) | it touches the polygon (`all_touched`), so coastal cells survive | set to NaN |

The pipeline clips every staged item after `transform` and before validation (`clip_staged_item`): COGs and parquet
parts are rewritten only when something changes, Zarr variables in place, tables with pyarrow (types and GeoParquet
metadata are kept). Items get `fuellake:region` (region id) and `fuellake:region_clip` (`masked_pixels`,
`dropped_rows`). Validation uses the same rules with the polygon buffered by `tolerance_m` for pixels and cells. The
gold cube masks pixels outside the region. Region-scale connectors also filter at the source, so raw subsets and
their item properties describe the region only (FEMS sites, Globe-LFMC rows, FIRMS rows, perimeter features).

### Form A raster (scene products on the AOI grid)

- Cloud-Optimized GeoTIFF, **one band per asset**, CRS and transform and shape exactly equal to the AOI target grid.
- Continuous data `float32` with `NaN` nodata; categorical data integer with an explicit nodata value.
- Downsampling of continuous data uses `average`; upsampling `bilinear`; categorical uses `nearest` (or `mode` when downsampling). A source whose cells coincide with the target grid (same CRS and cell size, edges within 0.1 % of a cell: `rasters.aligned_offset`) is copied cell by cell without resampling; that is how SME2 lands on `ca`.
- Region-scale form B datasets (gridMET, SMAP L4, ERA5-Land) are not put on the `ca` grid: they stay on their native grids, clipped to the region; labels and fire tables stay points and polygons.
- Values inside the variable's `valid_range` (out-of-range pixels set to nodata and counted in item property `fuellake:qc_masked_fraction`).
- `raster:bands` carries `data_type`, `nodata` and, for continuous data, `unit`. Categorical integer bands (LANDFIRE `fuel_model`) need no unit; validation warns about a missing unit only for float bands.
- Used by: NISAR GCOV (`gamma0_hh_db`, `gamma0_hv_db`), NISAR SME2 (`soil_moisture`, `soil_moisture_uncertainty`), OPERA RTC-S1 (`gamma0_vv_db`, `gamma0_vh_db`), Sentinel-2 (`ndvi`, `ndmi`), DEM (`elevation`, `slope`, `aspect`), LANDFIRE (`fuel_model`), biomass (`biomass`), and the interface collections `fuel-state` and `spread-risk`.

### Form B raster (coarse gridded time series)

- **Zarr v3** store (`rasters.write_zarr`, unconsolidated) clipped to AOI bbox + one native cell of buffer, native CRS and resolution, dims `(time, y, x)`, one item per dataset × AOI × calendar month. The item asset key is `data`.
- Used by weather sources (ERA5-Land, gridMET). The cube builder regrids them.

### Gold datacube (`features/<aoi>/cube.zarr`, collection `features-cube`)

- Zarr v3, CF-1.8 style, built by `fuellake build-features` (`features.cube.build_cube`); layout in the `cube.py` docstring.
- Every data variable is `float32` with NaN fill. Dynamic (form A scenes) and weather variables are `(time, y, x)`; static variables (`elevation`, `slope`, `aspect`, `fuel_model`, `biomass_<component>`) are `(y, x)`.
- `time` is the union of the UTC acquisition dates of the dynamic variables, strictly increasing (`fuellake:time_axis: scenes`). Without any dynamic date in the period (the statewide AOI `ca` has no scene datasets) but with weather, it is every `weather_cadence_days`-th day of the period that has weather data (`fuellake:time_axis: weather`; option of `features-cube.yaml` or `--weather-cadence-days`, default 1 = daily). It is empty when only static layers exist (`none`). Pixels outside the region are NaN.
- `x`/`y` are the pixel centres of the AOI grid at the cube resolution (`aoi.grid_at(resolution)`, coarser only), with a `spatial_ref` grid mapping.
- One item per AOI: `features-cube__<aoi>__<start>__<end>`, asset `data`, properties `fuellake:variables` (name → role, dims, aggregation), `fuellake:dims`, `fuellake:resolution`, `fuellake:missing_datasets`, `fuellake:input_licenses`. A rebuild replaces the AOI's previous item.
- Checked by `contracts.validate_cube` (the generic form B rule would reject the static `(y, x)` variables).

### Tables (GeoParquet, `staged/tables/<table>/...`)

`lfmc_samples` — unit is the **site-day per species**:

| column | type | notes |
|---|---|---|
| sample_id | string | sha1 of `source|site_id|date|species` |
| source | string | `fems` or `globe-lfmc` |
| site_id, site_name | string | source-native |
| lon, lat | float64 | EPSG:4326 |
| geometry | WKB point | GeoParquet metadata, EPSG:4326 |
| date | date32 | sampling date |
| species | string | normalised lower case |
| fuel_category | string | shrub, tree, herb, grass, other, unknown |
| lfmc_pct | float64 | % of dry weight |
| n_raw_rows | int32 | raw rows collapsed into this row |
| outlier_flag | bool | Globe-LFMC "Isolated data point" kept as a flag, never dropped |
| qc_flags | string | `;`-joined codes, e.g. `value_out_of_range;method_missing` |
| status | string | source status, e.g. FEMS `Submitted` |
| ingested_at | timestamp[us, UTC] | |

Rows are unique on `(source, site_id, date, species)` within a part. FEMS growth-phase rows and derived species averages are collapsed per this key (rule in the connector).

**Parts are written per source unit and AOI** (e.g. `part-smm__2026-06`), so runs for overlapping AOIs repeat the rows of sites inside both. `tables.read_table` (and `LakeClient.read_table`, `/tables/{name}`) drop repeated rows on the table key and keep the latest `ingested_at`, unless `dedupe=False`. Keys (`tables.TABLE_KEYS`): `lfmc_samples.sample_id`, `fire_detections.detection_id`, `fire_perimeters.perimeter_id`.

Other tables: `fire_detections` (FIRMS points) and `fire_perimeters` (FRAP / WFIGS polygons); the contract requires the WKB geometry, the connectors document their columns.

`matchups` (gold, `features/<aoi>/matchups.parquet`, collection `matchups`, one file per AOI): the `lfmc_samples` columns, then `grid_x`, `grid_y` (float64, AOI CRS metres), `grid_row`, `grid_col` (int32), then the feature columns listed in the item property `fuellake:columns`. Lag columns `<var>_lag<L>d` are built for `soil_moisture` (NISAR SME2), `swvl1` (ERA5-Land) and every variable flagged `lag: true` in a dataset config (SMAP L4 `sm_surface`, `sm_rootzone`, `sm_profile`); `--lag-variables a,b` replaces that list (`none` = no lags). `sample_id` is unique within a file. It is not deduplicated across AOIs (each AOI file is its own sample set). `read_table("matchups")` finds the files through the catalog (`fuellake:table: matchups`).

### Interface collections (outputs of Systems A and B)

Dataset configs with `kind: raster-a`, `connector: null`, `status: interface`, `scales: [local, region]`. Items are
written only by `LakeClient.publish_raster` (`fuellake.publish`), on the AOI grid, one COG per variable.

`fuel-state` (System A: flammable biomass and fuel state):

| variable | unit | dtype / nodata | valid range | required |
|---|---|---|---|---|
| `fuel_load` | kg/m² | float32 / NaN | 0–100 (plausibility bound) | yes |
| `lfmc` | % of dry weight | float32 / NaN | 0–400 | yes |
| `spread_potential` | 1 | float32 / NaN | 0–1 | yes |
| `fuel_load_p05`, `fuel_load_p95`, `lfmc_p05`, `lfmc_p95` | as above | float32 / NaN | as above | no |

`fuel_load` is the oven-dry mass per ground area of the fuel that can burn in a spreading fire. `spread_potential`
is a dimensionless relative index of the fuel alone (amount and moisture; no weather, no topography) to carry a
spreading fire: 0 = cannot carry fire, 1 = the highest potential System A can express; monotone in fuel
availability and dryness, not a probability. Item properties: `fuel_state:model_version` (string, required) and
`fuellake:source_items` (the input item ids, required).

`spread-risk` (System B: fire-spread risk map for an area and a validity window):

| variable | unit | dtype / nodata | valid range | required |
|---|---|---|---|---|
| `risk_index` | 1 | float32 / NaN | 0–1 | yes |
| `risk_class` | class | uint8 / 0 | 1–5 (1 lowest, 5 highest; breaks to be defined by the team) | yes |
| `rate_of_spread` | m/min | float32 / NaN | 0–1000 (plausibility bound) | no |
| `risk_p05`, `risk_p95` | 1 | float32 / NaN | 0–1 | no |

Item properties (all required): `spread_risk:valid_from` and `spread_risk:valid_to` (ISO datetimes; they also set
the item's `start_datetime` / `end_datetime`, so time searches return the maps valid in the searched period),
`spread_risk:model_version`, `spread_risk:fuel_state_item` (the `fuel-state` item id used; filled from
`source_items` when exactly one source is a `fuel-state` item), `spread_risk:weather_source` (e.g. `gridmet`), and
`fuellake:source_items`.

Every published item also has: `derived_from` links (relative hrefs) to each source item, `fuellake:source_ids` =
the source item ids, `fuellake:run_id` `publish-<timestamp>-<hex>`, `fuellake:qc_masked_fraction` 0.0 (out-of-range
values are refused, never masked) and the region properties. Interface collections are not cube inputs unless
named with `--input` (no feedback loop from model outputs into the training cube).

### STAC

- Collection id = dataset id. Item id = `<dataset>__<aoi>__<yyyymmdd>[__<discriminator>]`, e.g. `nisar-gcov__smm__20260615__t013d071`.
- Required item properties: `datetime` (UTC acquisition), `fuellake:aoi`, `fuellake:dataset`, `fuellake:license`, `fuellake:redistributable`, `fuellake:status`, `fuellake:source_ids` (original granule ids or URLs), `fuellake:run_id`, `fuellake:pipeline_version`.
- The pipeline also sets `fuellake:source_key` (the `SourceRef.key` of the unit). It is the idempotency key: a key already cataloged for the AOI is skipped unless `--force`.
- With a lake region, every item also carries `fuellake:region` and `fuellake:region_clip` (`masked_pixels`, `dropped_rows`), and its bbox lies inside the region envelope.
- NISAR items additionally: `fuellake:maturity` (BETA/PROVISIONAL/VALIDATED), `sat:relative_orbit` (track), `fuellake:frame`, `sat:orbit_state`, `fuellake:processing_id` (CRID), `sar:polarizations`, `sar:frequency_band` = `L`. A time series must not mix maturities: the cube builder and client refuse unless explicitly told to.
- Form A assets carry `proj:epsg`, `proj:transform`, `proj:shape`, and `raster:bands` (`data_type`, `nodata`, `unit` for continuous data).
- Table items: one item per table part, asset key `table`, media type `application/vnd.apache.parquet`, property `fuellake:n_rows`.
- Collections carry `fuellake:kind`, `fuellake:table`, `fuellake:status`, `fuellake:license`, `fuellake:variables`, `fuellake:caveats` and **`fuellake:static`**.
- **Static collections** (`fuellake:static: true`: cop-dem dated 2021-04-22, landfire-fbfm40 dated 2025-01-01) hold time-invariant layers. `catalog.search` ignores the time filter for them (`static_any_time=True`, the default), so client and API searches for a 2026 period still return the DEM and the fuel model. Their connectors find one unit per AOI whatever the run period. The cube builder searches every static dataset (cop-dem, landfire-fbfm40 and the dated `biomass` drops, option `static_datasets` of `features-cube`) without a time filter and uses the latest date on or before `end` (else the earliest).

### Events (`events/<date>.jsonl`)

```json
{"event_id": "uuid4", "time": "2026-11-14T20:00:00Z", "type": "item.created|item.updated|table.updated|run.completed|run.failed",
 "dataset": "nisar-sme2", "aoi": "smm", "collection": "nisar-sme2", "item_id": "...", "href": "catalog/...json", "run_id": "..."}
```

`table.updated` is emitted in addition to the item event for table parts (and the gold matchups). `run.completed` means no unit failed or was invalid (skipped stubs, credential-skipped units and scale-skipped datasets are not failures). Published items emit `item.created` / `item.updated` (and `table.updated`) with the extra field `"origin": "publish"` and no `run.*` event.

## Python interfaces (core)

```python
# storage.py
class Lake:
    root: str                                   # fsspec URL
    @classmethod
    def from_env(cls) -> "Lake"
    def url(self, rel: str) -> str              # absolute fsspec URL
    def exists(self, rel: str) -> bool
    def read_bytes(self, rel: str) -> bytes
    def write_bytes(self, rel: str, data: bytes) -> None
    def open(self, rel: str, mode: str = "rb")
    def ls(self, rel: str) -> list[str]         # also glob(), find(): lake-relative paths
    def local_path(self, rel: str, mode: str = "r")   # context manager: a real local file path; uploads on exit for remote roots

# registry.py
def load_aoi(aoi_id: str, *, check_region=True) -> AOI   # pydantic; .grid, .grid_at(resolution), .scale
def load_dataset(dataset_id: str) -> DatasetConfig   # .is_static, .is_nisar, .is_interface, .supports(aoi), .variable(name)
def list_datasets() -> list[DatasetConfig]
def load_lake_config() -> LakeConfig; def lake_region() -> Region | None; def load_region(region_id) -> Region
GOLD_DATASETS = ("features-cube", "matchups")

# region.py
class Region:                                   # .id .name .geometry (EPSG:4326) .tolerance_m .bbox .fingerprint
    def aoi_problem(aoi) / bbox_problem(bbox) -> str | None; def check_aoi(aoi)   # raises OutsideRegionError
    def contains_points(lon, lat, tolerance_m=None) -> np.ndarray; def rows_inside(geoms) -> np.ndarray
    def pixel_mask(grid, *, all_touched=False, tolerance_m=0) -> np.ndarray; def clamp_bbox(bbox)
def clip_staged_item(lake, staged: StagedItem, region, default_bbox=None) -> dict   # clip_cog / clip_zarr / clip_table

# publish.py (used by LakeClient.publish_*)
def publish_raster(lake, dataset_id, aoi, datetime, arrays, *, properties=None, source_items=None,
                   discriminator=None) -> dict
def publish_table(lake, dataset_id, aoi, df, key, *, properties=None, datetime_=None, source_items=None) -> dict
class PublishError(ValueError)

# grid.py
@dataclass(frozen=True)
class Grid: crs: str; transform: Affine; width: int; height: int; resolution: float
    def bounds(self) -> tuple; def bounds_wgs84(self) -> tuple
def grid_from_bbox(bbox_wgs84, crs, resolution, origin=None) -> Grid
REFERENCE_GRIDS = {"ease2-200m": ReferenceGrid(crs, resolution, origin)}; EASE2_UL_X, EASE2_UL_Y, EASE2_200M_CELL

# rasters.py
def reproject_to_grid(src_path_or_url: str, grid: Grid, band: int | str = 1, resampling: str = "bilinear",
                      src_nodata=None) -> np.ndarray
def reproject_array(arr, src_crs, src_transform, grid, resampling, src_nodata=None) -> np.ndarray   # thread-safe
def write_cog(lake: Lake, rel: str, arr: np.ndarray, grid: Grid, nodata, dtype: str, description: str | None = None) -> None
def read_cog(lake: Lake, rel: str) -> tuple[np.ndarray, Grid]
def write_zarr(lake, rel, ds) / open_zarr(lake, rel)                                                 # Zarr v3

# tables.py
def write_geoparquet(lake: Lake, rel: str, df: pandas.DataFrame) -> None   # adds geometry + GeoParquet metadata from lon/lat
def read_table(lake: Lake, table: str, dataset: str | None = None, *, dedupe: bool = True) -> pandas.DataFrame

# catalog.py
def search(lake, collections=None, *, aoi=None, bbox=None, start=None, end=None, datetime_range=None, query=None,
           ids=None, limit=None, static_any_time=True) -> list[dict]

# connectors/base.py
@dataclass(frozen=True)
class Period: start: date; end: date

@dataclass(frozen=True)
class SourceRef:
    dataset_id: str
    key: str                       # stable id of one unit of work (granule id, month, query hash)
    datetime: datetime             # UTC
    urls: tuple[str, ...]
    meta: Mapping[str, Any]        # e.g. track, frame, maturity, size_bytes

@dataclass
class StagedAsset: key: str; path: str; media_type: str; roles: list[str]; raster_band: dict | None = None

@dataclass
class StagedItem: item_id: str; datetime: datetime; properties: dict; assets: list[StagedAsset]; bbox: tuple | None = None
    # keyword extras: start_datetime, end_datetime, geometry

class BaseConnector(ABC):
    def __init__(self, dataset: DatasetConfig, lake: Lake, settings: Settings): ...
    @abstractmethod
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]: ...
    @abstractmethod
    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]: ...          # writes raw/, returns lake-relative paths (may be [])
    @abstractmethod
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]: ...
    def item_id(self, aoi: AOI, ref: SourceRef, discriminator: str | None = None) -> str: ...
    # optional hooks
    def cached_empty(self, ref: SourceRef, aoi: AOI) -> bool: ...          # cache says "stages nothing" (Sentinel-2 cloudy date)
    def missing_credentials(self, aoi: AOI, ref: SourceRef | None = None) -> list[str]: ...   # env var names, never values

# pipeline.py
def run(dataset_ids: list[str], aoi_id: str, period: Period, *, force: bool = False, limit: int | None = None,
        lake: Lake | None = None, settings: Settings | None = None, skip_stubs: bool = False,
        skip_missing_credentials: bool = False, force_scale: bool = False) -> RunReport
```

The pipeline, not the connector, handles: the AOI scale check, skipping units already cataloged with the same source key (idempotency), clipping every output to the region, contract and region validation of every output, catalog registration, events, the run manifest, and isolating one failed unit from the rest.

- `limit` counts units actually processed (extract called). Already cataloged units, units the connector's cache marks empty (`cached_empty`, recorded as `empty`) and credential-skipped units do not use it up.
- Unit statuses: `created`, `updated`, `empty`, `invalid`, `failed`, `missing_credentials`. Dataset statuses: `ok`, `partial`, `failed`, `no-connector`, `skipped`.
- Lenient mode (`skip_stubs`, `skip_missing_credentials`; used by `fuellake run all`): stub datasets are `skipped`; a dataset whose discovery needs missing credentials (ERA5-Land) is `skipped`; units whose files need a missing credential (NISAR/OPERA downloads without `EARTHDATA_TOKEN`, FIRMS area-API months without `FIRMS_MAP_KEY`) get status `missing_credentials` and are retried by the next run. None of these fail the run. Explicitly named datasets run with both options off and fail loudly.
- `rasters.reproject_array` is safe to call from several threads: each call uses its own georeferenced in-memory GeoTIFFs inside its own `rasterio.Env`. NumPy arrays are never handed to `rasterio.warp.reproject` directly, because rasterio's `MemoryDataset` uses the process-wide, non-thread-safe `warnings.catch_warnings()`.

## CLI (`fuellake …`)

Dataset ids are **positional** arguments; `all` (or no id) means every dataset with a connector.

```
fuellake init
fuellake datasets [--json]
fuellake discover [<dataset> ...|all] --aoi smm --start YYYY-MM-DD --end YYYY-MM-DD
fuellake run [<dataset> ...|all] --aoi smm --start YYYY-MM-DD --end YYYY-MM-DD [--force] [--limit N] [--force-scale] [--json]
fuellake validate [<dataset> ...] [--json]          # contracts and the region
fuellake build-features --aoi smm --start ... --end ... [--what cube|matchups|all] [--resolution M]
                        [--maturity PROVISIONAL] [--allow-mixed-maturity] [--input <dataset> ...]
                        [--causal/--no-causal] [--window-days 6] [--lags-days 0,15,30,45,60,90]
                        [--sample-window-px 3] [--nearest-valid/--nearest-any] [--lag-variables a,b|none]
                        [--weather-cadence-days N]
fuellake licenses [--markdown]
fuellake events [--since ISO] [--type T ...]
fuellake serve [--host 127.0.0.1] [--port 8000]
```

`--start` defaults to `--end` minus 30 days, `--end` to today. `run` exits 1 if any unit failed or was invalid, if a named dataset does not support the AOI's scale (unless `--force-scale`), or (one line) if the AOI is unknown or outside the region; `run all` / `discover all` skip stub datasets, scale-mismatched datasets and missing-credential datasets/units ("skipped: missing credentials (NAME)"), printing names only. `build-features` reports a mixed-maturity series, missing inputs or bad parameters on one line and exits 1.

## Consumer SDK (`fuellake.client`)

```python
lake = LakeClient.from_env()                    # FUELLAKE_API_URL -> HTTP mode, else LAKE_ROOT (local or s3://)
lake = LakeClient("/data/lake") ; LakeClient(api_url="http://api:8000") ; lake.mode   # "direct" | "http"
lake.collections() ; lake.datasets()            # STAC collections ; dataset configs (status, licence, variables)
lake.search(collections=[...], aoi="smm", start=..., end=..., bbox=..., query={"fuellake:maturity": "PROVISIONAL"},
            limit=None, allow_mixed_maturity=False)
lake.get_item(collection, item_id)              # e.g. from an event; None if absent
lake.open_asset(item, "soil_moisture") -> xarray.DataArray
lake.read_table("lfmc_samples", sources=["fems"], start=..., end=..., dedupe=True) -> pandas.DataFrame
lake.read_table("matchups")                     # gold label x feature table of every built AOI
lake.open_cube("smm") -> xarray.Dataset
lake.events(since=datetime, types=["item.created"]) -> list[dict]
# direct mode only (HTTP mode raises ReadOnlyClientError pointing to direct mode):
lake.publish_raster("fuel-state", "smm", "2026-08-01", {"fuel_load": a, "lfmc": b, "spread_potential": c},
                    properties={"model_version": "fuelstate-0.1"}, source_items=[item, ...]) -> dict
lake.publish_table(dataset_id, "smm", df, key, properties=None, datetime=None, source_items=None) -> dict
```

The same read calls return the same results in direct-local, direct-S3 and HTTP mode (tested). `publish_raster`
refuses (`PublishError`) an array off the AOI grid (NumPy shape, or a DataArray's CRS / transform / shape), an
unknown variable, a missing required variable, a value outside `valid_range` or `inf`, a missing or mistyped
property, a source item that is not cataloged and a non-interface dataset. It writes to a temporary directory,
clips to the region, validates, then copies into `staged/<dataset>/<aoi>/<item_id>/` and catalogs: a refused
publish leaves the lake untouched. Item id `<dataset>__<aoi>__<yyyymmdd>[__<HHMMSS or discriminator>]`; the same
key again replaces the item (`item.updated`). `publish_table` writes `part-<aoi>__<key>.parquet`; the same key
replaces the item even when its dates change. Tested publish → search → open in direct-local and direct-S3 (moto);
published items are found through the HTTP API too. See `docs/microservices.md`.

## HTTP API (`fuellake.services.api`, read-only, STAC-API-lite)

`GET /` (landing page, `conformsTo`) · `/health` · `/datasets` · `/collections` · `/collections/{cid}` · `/collections/{cid}/items?bbox=&datetime=&limit=&offset=` · `/collections/{cid}/items/{iid}` · `/search` (GET and POST: `collections`, `ids`, `bbox`, `datetime`, `query`, `aoi`, `limit`, `offset`) · `/assets/{cid}/{iid}/{key}` (streams the file with `Range` support; a Zarr asset returns a JSON file listing) · `/assets/{cid}/{iid}/{key}/{path}` (files inside a Zarr asset) · `/cubes/{aoi}` and `/cubes/{aoi}/{path}` (the gold datacube) · `/tables/{name}?sources=&start=&end=&dedupe=` (one (Geo)Parquet file; `/tables/matchups` is the gold table) · `/events?since=&type=`. It is not a full STAC API implementation (no `intersects`, `sortby`, `fields`, CQL2, transactions; paging by offset; no authentication). Static collections ignore `datetime` as in `catalog.search`.

## Dataset inventory

> Superseded: this table predates several datasets and status changes. The current inventory, statuses and
> caveats are in README.md ("Data inventory", generated from `config/datasets/*.yaml`), which is authoritative.

Target status from the project brief and the status actually reached (early configs of 2026-11-14):

| id | kind | source | auth | target status | status now |
|---|---|---|---|---|---|
| fems-lfmc | table | USDA FEMS CSV download (no login) | none | tested | tested |
| globe-lfmc | table | Globe-LFMC 2.0, figshare doi:10.6084/m9.figshare.25413790.v1, CC0 | none | tested | tested |
| nisar-sme2 | raster-a | NISAR L3 SME2, 200 m EASE-Grid 2.0, CMR + ASF | earthdata | search-only unless a token or public sample allows a transform test | search-only |
| nisar-gcov | raster-a | NISAR L2 GCOV HH/HV, 10 m, CMR + ASF; files 4.6–7.4 GB, never full-download by default | earthdata | search-only (same rule) | search-only |
| opera-rtc-s1 | raster-a | OPERA RTC-S1 VV/VH 30 m, CMR + ASF | earthdata | search-only (same rule) | search-only |
| sentinel2-indices | raster-a | Sentinel-2 L2A via Element 84 Earth Search STAC (public COGs) | none | tested | tested |
| cop-dem | raster-a | Copernicus DEM GLO-30 (public COGs) | none | tested | tested |
| landfire-fbfm40 | raster-a | LANDFIRE FBFM40 fuel models (ArcGIS ImageServer, keyless) | none | stub or tested | tested |
| biomass | raster-a | drop zone contract for the team's biomass subsystem | none | tested with a synthetic drop | tested (synthetic drops only) |
| era5-land-daily | raster-b | ERA5-Land via the CDS API (no keyless mirror found) | api_key | stub or tested | stub |
| gridmet | raster-b | gridMET wind and dead fuel moisture (THREDDS NCSS, keyless) | none | stub or tested | tested |
| firms | table | NASA FIRMS active fire detections (keyless yearly archive + MAP_KEY area API) | api_key | stub or tested | tested (archive path only) |
| fire-perimeters | table | CAL FIRE FRAP + NIFC WFIGS perimeters (ArcGIS, keyless) | none | stub or tested | tested |
| features-cube | raster-b | gold datacube (`fuellake build-features`) | none | — | unverified (synthetic lakes only) |
| matchups | table | gold label × feature table (`fuellake build-features`) | none | — | unverified (synthetic lakes only) |
| fuel-state | raster-a | interface: System A output (`LakeClient.publish_raster`) | none | — | interface (publish tested offline) |
| spread-risk | raster-a | interface: System B output (`LakeClient.publish_raster`) | none | — | interface (publish tested offline) |

Known facts to reuse (do not re-derive): NISAR GCOV PROVISIONAL collection `C2854338529-ASF` (CRID P05023), VALIDATED `C3622241360-ASF` (CRID P05036); OPERA RTC-S1 `C2777436413-ASF`; over California NISAR records only HH+HV in the 40 MHz mode since 2026-06-01; NISAR gap 2026-07-27 to 2026-08-10; SME2 is 200 m EASE-Grid 2.0 with DSG/PMI/TSR algorithms and an uncertainty layer, PROVISIONAL only. Details and the tested FEMS queries are in `../research_notes/Walidacja projektu LFMC NASA Space Apps/` (`nisar_data_reality.md`, `improvements_feasibility.md`).

Real runs on the region AOI `ca` (lake `dev-region`, 2026-11-14, with the region active): `globe-lfmc` 1977–2023
(47 items, 48,575 rows from 319 California sites), `fems-lfmc` 2026-01..2026-11 (11 items, 3,547 rows, 419 sites
queried), `fire-perimeters` 2020–2026 (7 items, 2,910 polygons; 341 WFIGS fires outside California dropped at the
source) and `gridmet` 2026-10 (1 item); each rerun was a no-op and `fuellake validate` passed. After `ca` moved from
EPSG:3310 at 1000 m to the SME2 grid, the tables and the gridMET item needed no rebuild (they do not depend on the
target grid: reruns were no-ops and they validate); the cube and matchups were rebuilt: `fuellake build-features
--aoi ca --start 2026-10-01 --end 2026-10-03` gave a weather-only cube of 3 daily steps × 4827 × 4956 cells (7
gridMET variables, 10.22 M finite cells per step; 17 s, 1.3 GB peak memory, 315 MB on disk) and 67 FEMS matchups,
both valid. An independent check
against the original Census shapefile found no LFMC row outside the state polygon and no perimeter farther than
530 m from it. `firms` and `era5-land-daily` were not run on `ca`.

## Validation levels

- `tested`: a real run for AOI `smm` (or, for region-scale datasets, `ca`) produced cataloged outputs, a second run was a no-op (idempotent), and `fuellake validate` passed. Exceptions are stated in the config comment and README: `biomass` was tested with synthetic drops only, `firms` on its keyless archive path only.
- `search-only`: discovery was run live; extract/transform are unit-tested on fixtures and marked untested on real data.
- `stub`: registry entry with verified endpoint facts; the connector is written but was never run against the source (ERA5-Land: no CDS credentials during the build). `fuellake run all` skips stubs; run them by name.
- `unverified`: not run on real data and not otherwise verified. Used for the gold collections, which were tested on synthetic lakes only.
- `interface`: no connector and no source data; a contract that the team's systems publish into (`fuel-state`, `spread-risk`). Publishing is tested offline (local and moto S3); no real model output has been published yet.

## Conventions

- English for code and docs. Each source file starts with a two-line header: `# fuel-lake — <module purpose>` and `# Licensed under Apache-2.0.`
- Tests must pass offline (`pytest -m "not network and not earthdata"`); live tests are marked. The test fixtures redirect the default lake root to a per-test directory, so no test touches `../fuel-lake-data`.
- Use `rtk proxy curl …` for raw HTTP checks from the shell.
- No `git init`, no commits.
