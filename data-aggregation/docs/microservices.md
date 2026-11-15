<!-- fuel-lake — guide for processing-microservice developers -->
<!-- Licensed under Apache-2.0. -->

# Writing a processing microservice

A processing microservice is a small process that **finds** lake data, **reads** it, **writes** its own
result (into its own storage, or **publishes** it into an interface collection of the lake when other systems
need it), and **reacts to events** when new data arrives. It talks to the lake only through
`fuellake.client.LakeClient` and never needs a lake path.

`LakeClient` has two modes with the same methods and return types:

- **direct**: reads the lake root itself (a local directory, or `s3://` on MinIO or AWS).
- **http**: reads through the read-only API (`fuellake serve`).

`LakeClient.from_env()` picks the mode from the environment. It uses `FUELLAKE_API_URL` if that is set,
and otherwise `LAKE_ROOT` (plus `FUELLAKE_S3_ENDPOINT_URL` for MinIO).

## The whole pattern in about 20 lines

```python
import json, time
from pathlib import Path
from fuellake.client import LakeClient

OUT = Path("sm-mean-out"); OUT.mkdir(exist_ok=True)       # 3. your own output location, never the lake
client = LakeClient.from_env()                              # FUELLAKE_API_URL, else LAKE_ROOT
cursor = None                                               # persist it (and what you did) in real services

while True:
    events = client.events(since=cursor, types=["item.created", "item.updated"])  # 4. react to events
    for ev in events:
        if ev["collection"] != "nisar-sme2":
            continue
        item = client.get_item(ev["collection"], ev["item_id"])                   # 1. find
        da = client.open_asset(item, "soil_moisture")                             # 2. read: DataArray (y, x)
        row = {"item_id": item["id"], "datetime": item["properties"]["datetime"],
               "maturity": item["properties"].get("fuellake:maturity"), "mean": float(da.mean())}
        (OUT / f"{item['id']}.json").write_text(json.dumps(row))                  # idempotent per item
    if events:
        cursor = events[-1]["time"]            # `since` is inclusive: the last event comes back, harmlessly
    time.sleep(60)
```

For a complete version, see `fuellake/services/example_consumer.py` (about 100 lines). It adds a
checkpoint file (exactly-once processing across restarts), a catalog backfill on the first run, Parquet
output and a CLI. Run it once with
`uv run python -m fuellake.services.example_consumer --once --out ./consumer-out`.

## Find

```python
client.collections()                                   # STAC collections (collection id = dataset id)
client.datasets()                                      # status, licence, variables, caveats per dataset
client.search(collections=["sentinel2-indices"], aoi="smm", start="2026-06-01", end="2026-09-30")
client.search(collections=["nisar-sme2"], query={"fuellake:maturity": "PROVISIONAL"})
client.search(bbox=(-118.8, 34.0, -118.6, 34.1), query={"eo:cloud_cover": {"lt": 20}}, limit=10)
client.get_item("nisar-sme2", "nisar-sme2__smm__20260615")
```

`search` returns plain STAC item dicts sorted by `(datetime, id)`. A bare `end` date includes that whole
day. Query operators are `eq`, `neq`, `lt`, `lte`, `gt`, `gte`, `in` and `contains`.

Static layers ignore the time filter. `cop-dem` (dated 2021) and `landfire-fbfm40` (LF 2025) are
time-invariant (their collections have `fuellake:static: true`), so `search(aoi="smm", start="2026-06-01",
end="2026-09-30")` returns them too. Dated biomass drops are not static.

## Read

```python
da = client.open_asset(item, "soil_moisture")   # form A: 2-D (y, x), da.rio.crs, da.rio.nodata (NaN)
t2m = client.open_asset(era5_item, "t2m")       # form B: variable of the item's Zarr store, (time, y, x)
df = client.read_table("lfmc_samples", sources=["fems"], start="2026-06-01", end="2026-11-13")
mu = client.read_table("matchups")              # gold label x feature table (every built AOI)
cube = client.open_cube("smm")                  # gold datacube (xarray.Dataset)
```

- Rasters are loaded into memory. An AOI-sized COG is a few MB.
- `read_table` returns a `GeoDataFrame` in EPSG:4326, or an empty `DataFrame` when the table has no parts.
  Table parts are written per AOI, so a site inside two AOIs would appear twice: `read_table` drops the repeats
  on `sample_id` (`detection_id` for `fire_detections`, `perimeter_id` for `fire_perimeters`) and keeps the
  latest `ingested_at`. Pass `dedupe=False` to get every row. `matchups` is not deduplicated across AOIs.
- The cube's dynamic and weather variables are `(time, y, x)`, static layers (`elevation`, `fuel_model`, ...)
  are `(y, x)`, everything is `float32` with NaN fill (layout: `fuellake/features/cube.py`).
- In HTTP mode, a Zarr store (a form B item or the cube) is downloaded to a temporary directory and loaded.
  In direct mode it is opened lazily.

## Write

- Write working files and private results to **your own location**: a local directory, a volume, or your own
  bucket or prefix. Never write files into the lake directly.
- Outputs that other systems need go into the lake **only** through `LakeClient.publish_raster` /
  `publish_table`, and only into interface collections (`status: interface`): `fuel-state` (System A) and
  `spread-risk` (System B). Publishing needs direct mode (a local or `s3://` lake root); in HTTP mode the publish
  methods raise `ReadOnlyClientError`.

  ```python
  fs = client.publish_raster("fuel-state", "smm", "2026-08-01",
                             {"fuel_load": fl, "lfmc": lfmc, "spread_potential": sp},  # 2-D arrays, AOI grid
                             properties={"model_version": "fuelstate-0.1"}, source_items=inputs)
  ```

  Arrays must be on the AOI target grid (NumPy arrays of `aoi.grid.shape`, or DataArrays from `open_asset`).
  Values outside a variable's `valid_range`, unknown or missing required variables and missing properties are
  refused with `PublishError`, and nothing is written. Pixels outside the region become nodata. The item gets
  `derived_from` links and `fuellake:source_items` for its inputs, and an `item.created` / `item.updated`
  event with `"origin": "publish"`. Variables and properties: ARCHITECTURE.md, "Interface collections".
- The lake has one writer at a time and no locks: do not publish while a pipeline run writes to the same lake.
- Other new lake data needs a dataset: a connector, or a drop zone such as `inbox/biomass/`.
- Make writes idempotent per item. Publishing the same dataset, AOI and datetime (and discriminator) again
  replaces the item. Events can be delivered more than once.

## React to events

`client.events(since=t, types=[...])` returns events with `time >= t`, oldest first.

- **Types**: `item.created`, `item.updated`, `table.updated`, `run.completed`, `run.failed`.
- **Item events** carry `collection` and `item_id`; pass them to `get_item`. `href` is lake-internal.
- **Polling**: keep the last event time as your cursor, and de-duplicate on `event_id` or item id.
- **Versions**: an `item.updated` (for example after `fuellake run --force`) has a new
  `properties["fuellake:run_id"]`. Use it as the version if you want to reprocess changed data.
- **First run**: backfill with `client.search(...)` before following events, as the example service does.

## What you can rely on (the data contract)

These are enforced by the pipeline's validation or tested in `tests/`:

- **Everything is cataloged.** Every file in `staged/` and `features/` has a STAC item. Item ids are
  `<dataset>__<aoi>__<yyyymmdd>[__<discriminator>]`.
- **Everything lies in the monitored region** (California, `config/lake.yaml`): pixels outside it are nodata,
  table rows outside it are dropped, item bboxes lie inside it (rules: ARCHITECTURE.md, "Region").
- **Every item has these properties:**
  - `datetime` (UTC)
  - `fuellake:aoi`, `fuellake:dataset`, `fuellake:status`
  - `fuellake:license`, `fuellake:redistributable`
  - `fuellake:source_ids`, `fuellake:run_id`, `fuellake:pipeline_version`
- **Form A rasters** are one-band COGs whose CRS, transform and shape exactly match the AOI target grid,
  so items of one AOI stack without reprojection. The asset key is the variable name.
  - Continuous data is `float32` with NaN nodata.
  - Categorical data is integer with an explicit nodata.
  - Values outside the variable's `valid_range` are already masked; the masked share is
    `fuellake:qc_masked_fraction`.
- **Form B rasters** are Zarr v3 stores with dims `(time, y, x)` on the native grid, one item per
  dataset × AOI × month.
- **Gold outputs** (`features-cube`, `matchups`) are validated by `fuellake validate` like everything else, but
  they were built on synthetic lakes only so far (`status: unverified`).
- **`lfmc_samples`** follows the schema in `ARCHITECTURE.md`.
  - It is unique on `(source, site_id, date, species)`.
  - `outlier_flag` rows are flagged, never dropped.
  - `date` is a calendar date.
- **NISAR maturity:** a time series never silently mixes BETA, PROVISIONAL and VALIDATED.
  `search` raises `MixedMaturityError` when the items of one collection mix maturities. Filter with
  `query={"fuellake:maturity": ...}`, or pass `allow_mixed_maturity=True` when you process items one by
  one, as the example service does, and record the maturity with your result.
- **Idempotent ETL:** re-running the pipeline never duplicates items or events. Reprocessing emits
  `item.updated`.
- **Same answers in both modes:** direct-local, direct-S3 and HTTP return the same items, arrays, tables
  and events. The same test suite runs in all three modes.

What is **not** guaranteed:

- **Dataset status.** Check `fuellake:status` and `client.datasets()`. `search-only`, `stub` and
  `unverified` datasets may have no items.
- **Licences.** Check `fuellake:redistributable` before publishing derived data.
- **The HTTP API is STAC-API-lite, not a complete STAC API.** It has no `intersects`, `sortby`,
  `fields` or CQL2 filter, and paging is by offset. It has no authentication, so do not expose it
  publicly.
- **The NISAR maturity guard lives in `LakeClient`, not in the raw API.** Other clients must check
  `fuellake:maturity` themselves.

## Other HTTP clients

Item JSON from the API has absolute asset hrefs (`/assets/{collection}/{item}/{key}`) that support HTTP
`Range`.

- **GDAL, rasterio, rioxarray and QGIS** can open these hrefs directly as Cloud-Optimized GeoTIFFs. Set
  `GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR` to skip sidecar probes.
- **`pystac-client`** can open the API root and search it (tested).
- **Endpoint list:** see `fuellake/services/api.py`, or `/docs` on a running server.

## Running it

| Where | How |
|---|---|
| Local lake | `LAKE_ROOT=/path/to/lake uv run python my_service.py` |
| Through the API | `uv run fuellake serve --port 8000`, then `FUELLAKE_API_URL=http://127.0.0.1:8000 uv run python my_service.py` |
| Docker compose | add a service like `example-consumer` in `docker/compose.yaml`, with `FUELLAKE_API_URL=http://api:8000` (the compose files have not been run: Docker is not available on the development machine) |
