# fuel-lake — publish model outputs (Systems A and B) into interface collections: check, clip, catalog, announce
# Licensed under Apache-2.0.
"""Write the outputs of the team's systems back into the lake, so the other systems find them in the catalog.

Use it through :meth:`fuellake.client.LakeClient.publish_raster` / :meth:`~fuellake.client.LakeClient.
publish_table` (direct mode only; the HTTP API is read-only). The interface collections are dataset configs with
``status: interface`` and no connector, e.g. ``fuel-state`` (System A) and ``spread-risk`` (System B). Only such
datasets accept publishes: ETL and gold collections are written by the pipeline and ``build-features``.

``publish_raster(lake, dataset_id, aoi, datetime, arrays, *, properties, source_items, discriminator)``
  1. ``arrays`` maps variable name -> 2-D array on the AOI target grid: a NumPy array of exactly
     ``aoi.grid.shape``, or an ``xarray.DataArray`` with a CRS (e.g. from ``LakeClient.open_asset``) whose CRS,
     transform and shape equal the AOI grid. Anything else is rejected (no silent reprojection).
  2. Every variable must be declared in the dataset config; every ``required`` variable must be present.
     Float variables become ``float32`` with NaN nodata; integer (class) variables accept integers or floats
     with NaN for nodata. A value outside the variable's ``valid_range`` (or ``inf``) is **rejected**, not
     masked.
  3. ``properties``: the dataset's ``item_properties`` (full names such as ``spread_risk:valid_from`` or the
     short names after the colon), checked for presence and type; extra properties are kept. Pipeline-owned
     properties (``fuellake:aoi``, ``fuellake:run_id``, ...) are refused. ``source_items`` (item dicts or ids,
     each must be cataloged) become ``fuellake:source_items``, ``fuellake:source_ids`` and ``derived_from``
     links; an ``item`` property restricted to a collection (``spread_risk:fuel_state_item``) is filled from
     them when exactly one source item belongs to that collection. A ``maps_to`` property sets the item's
     ``start_datetime`` / ``end_datetime``.
  4. The COGs are written to a temporary directory next to the item, clipped to the lake region (pixels outside
     become nodata), turned into a STAC item by the same code as the pipeline and validated against the dataset
     contract and the region. Only then are they copied to ``staged/<dataset>/<aoi>/<item_id>/<variable>.tif``,
     cataloged and announced (``item.created`` / ``item.updated`` with ``origin: publish``). A refused publish
     leaves the lake (and an earlier version of the item) untouched.

  Item id ``<dataset>__<aoi>__<yyyymmdd>[__<discriminator>]``; the discriminator defaults to ``HHMMSS`` of a
  datetime that is not midnight. Publishing the same dataset, AOI, datetime and discriminator again replaces the
  item (``item.updated``).

``publish_table(lake, dataset_id, aoi, df, key, *, properties, datetime)``
  A (Geo)DataFrame with ``lon``/``lat`` or a ``geometry`` column becomes the GeoParquet part
  ``staged/tables/<table>/<dataset>/part-<aoi>__<key>.parquet``, checked against the table schema (when the table
  has one), clipped to the region (rows outside dropped) and cataloged with ``fuellake:n_rows``. One item per
  dataset, AOI and ``key``: publishing the same key again replaces it, even when its dates changed. The item
  datetime is ``datetime`` or the earliest value of a ``datetime`` / ``date`` column.

Publishing is a lake write. The lake supports one writer at a time and has no locks: do not publish into a lake
root while another process writes to it.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import numpy as np

from fuellake import catalog, events
from fuellake.catalog import JSON_TYPE, item_rel, relative_href, to_utc
from fuellake.connectors.base import SourceRef, StagedAsset, StagedItem
from fuellake.contracts import ITEM_ID_RE, REQUIRED_PROPERTIES, check_property_value, validate_item, validate_table
from fuellake.rasters import COG_MEDIA_TYPE, write_cog
from fuellake.region import clip_staged_item
from fuellake.registry import AOI, GOLD_DATASETS, DatasetConfig, Variable, lake_region, load_aoi, load_dataset
from fuellake.storage import Lake
from fuellake.tables import PARQUET_MEDIA_TYPE, safe_key, table_part_path, to_geodataframe, write_geoparquet

if TYPE_CHECKING:
    import pandas as pd

    from fuellake.grid import Grid

RESERVED_PROPERTIES = frozenset(REQUIRED_PROPERTIES) | {
    "fuellake:source_key", "fuellake:region", "fuellake:region_clip", "fuellake:n_rows", "start_datetime",
    "end_datetime", "fuellake:maturity",
}
ORIGIN = "publish"


class PublishError(ValueError):
    """A publish was refused (wrong grid, unknown or missing variable, value out of range, bad property...)."""


# ---------------------------------------------------------------------------- shared checks
def publishable_dataset(dataset_id: str, kind: str) -> DatasetConfig:
    """The config of an interface dataset of ``kind`` (``raster-a`` or ``table``), else :class:`PublishError`."""
    try:
        dataset = load_dataset(dataset_id)
    except KeyError as exc:
        raise PublishError(str(exc.args[0])) from None
    if dataset.connector is not None or dataset.id in GOLD_DATASETS or not dataset.is_interface:
        raise PublishError(f"dataset {dataset.id} is not an interface collection (status {dataset.status!r}"
                           f"{', connector ' + dataset.connector if dataset.connector else ''}); only datasets with "
                           "status 'interface' and no connector accept publishes")
    if dataset.kind != kind:
        raise PublishError(f"dataset {dataset.id} is kind {dataset.kind}; use "
                           f"{'publish_raster' if dataset.kind == 'raster-a' else 'publish_table'}")
    return dataset


def _aoi(aoi: AOI | str, dataset: DatasetConfig) -> AOI:
    try:
        aoi = load_aoi(aoi) if isinstance(aoi, str) else aoi
    except KeyError as exc:
        raise PublishError(str(exc.args[0])) from None
    region = lake_region()
    if region is not None:
        region.check_aoi(aoi)                     # OutsideRegionError is a ValueError too
    if not dataset.supports(aoi):
        raise PublishError(f"{dataset.id} supports AOI scales {dataset.scales}; AOI {aoi.id} is {aoi.scale!r}")
    return aoi


def resolve_source_items(lake: Lake, source_items: Iterable[Any] | None) -> list[dict]:
    """Cataloged items for item dicts, item ids or ``"collection/item_id"`` strings (order kept, no repeats)."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for s in source_items or []:
        if isinstance(s, Mapping):
            cid, iid = s.get("collection"), s.get("id")
        elif isinstance(s, str):
            cid, _, iid = s.rpartition("/") if "/" in s else ("", "", s)
            m = ITEM_ID_RE.match(iid)
            cid = cid or (m["dataset"] if m else None)
        else:
            raise PublishError(f"source item {s!r}: pass STAC item dicts or item ids")
        if not cid or not iid:
            raise PublishError(f"source item {s!r}: cannot tell its collection; pass the item dict")
        if (cid, iid) in seen:
            continue
        item = catalog.get_item(lake, str(cid), str(iid))
        if item is None:
            raise PublishError(f"source item {cid}/{iid} is not in the catalog")
        seen.add((cid, iid))
        out.append(item)
    return out


def _iso(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return catalog.iso(value)
    return value


def build_properties(lake: Lake, dataset: DatasetConfig, properties: Mapping[str, Any] | None,
                     sources: list[dict]) -> tuple[dict, list[dict], dict]:
    """Normalised properties, the (possibly extended) source items and the STAC times set by ``maps_to``."""
    by_short = {p.short_name: p.name for p in dataset.item_properties}
    props: dict[str, Any] = {}
    for key, value in dict(properties or {}).items():
        full = key if (dataset.item_property(key) or ":" in key) else by_short.get(key, key)
        if full in RESERVED_PROPERTIES and full != "fuellake:source_items":
            raise PublishError(f"property {full} is set by fuel-lake, not by the publisher")
        if full in props:
            raise PublishError(f"property {full} given twice ({key!r})")
        spec = dataset.item_property(full)
        props[full] = _iso(value) if (spec is not None and spec.type == "datetime") else value

    source_ids = [s["id"] for s in sources]
    for spec in dataset.item_properties:
        if spec.type != "item" or not spec.collection or props.get(spec.name) is not None:
            continue
        hits = [i for i in source_ids if ITEM_ID_RE.match(i)["dataset"] == spec.collection]
        if len(hits) == 1:
            props[spec.name] = hits[0]
    for spec in dataset.item_properties:                 # referenced items must exist and be lineage sources
        if spec.type in ("item", "items") and spec.name != "fuellake:source_items" and props.get(spec.name):
            value = props[spec.name]
            ids = [value] if isinstance(value, str) else list(value) if isinstance(value, list) else []
            for extra in resolve_source_items(lake, [i for i in ids if i not in source_ids]):
                sources.append(extra)
                source_ids.append(extra["id"])
    if "fuellake:source_items" in props:
        extra_ids = props["fuellake:source_items"]
        if not isinstance(extra_ids, list):
            raise PublishError("fuellake:source_items must be a list of item ids (or use source_items=...)")
        for extra in resolve_source_items(lake, [i for i in extra_ids if i not in source_ids]):
            sources.append(extra)
            source_ids.append(extra["id"])
    if source_ids:
        props["fuellake:source_items"] = list(source_ids)

    errors = []
    times: dict[str, datetime] = {}
    for spec in dataset.item_properties:
        value = props.get(spec.name)
        if value is None:
            if spec.required:
                hint = " (pass source_items=[...])" if spec.type in ("item", "items") else ""
                errors.append(f"missing required property {spec.name}: {spec.description or spec.type}{hint}")
            continue
        problem = check_property_value(spec, value)
        if problem:
            errors.append(f"property {spec.name}: {problem}")
        elif spec.maps_to:
            times[spec.maps_to] = to_utc(value)
    if "start_datetime" in times and "end_datetime" in times and times["end_datetime"] < times["start_datetime"]:
        errors.append("the validity window ends before it starts (end_datetime < start_datetime)")
    if errors:
        raise PublishError(f"{dataset.id}: " + "; ".join(errors))
    try:
        json.dumps(props, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PublishError(f"properties must be JSON-serialisable (no NaN): {exc}") from None
    return props, sources, times


def _grid_array(name: str, value: Any, grid: Grid, aoi: AOI) -> np.ndarray:
    """2-D values of ``value`` if it is on ``grid`` (see the module docstring), else :class:`PublishError`."""
    where = f"AOI {aoi.id} grid {grid.crs} {grid.resolution:g} m, shape {grid.shape}"
    if hasattr(value, "dims") and hasattr(value, "values"):          # xarray.DataArray
        import rioxarray  # noqa: F401  (registers the .rio accessor)

        da = value.squeeze("band", drop=True) if ("band" in value.dims and value.sizes["band"] == 1) else value
        if da.ndim != 2:
            raise PublishError(f"variable {name}: expected a 2-D (y, x) array, got dims {da.dims}")
        crs = da.rio.crs
        if crs is None:
            raise PublishError(f"variable {name}: the DataArray has no CRS; pass a NumPy array on the {where} or a "
                               "DataArray on that grid")
        transform = da.rio.transform()
        if not grid.same_as(crs, transform, da.rio.width, da.rio.height):
            raise PublishError(f"variable {name}: grid mismatch (crs {crs}, shape {da.shape}, transform "
                               f"{list(transform)[:6]}) vs the {where}, transform {list(grid.transform)[:6]}; "
                               "reproject onto the AOI grid first")
        return np.asarray(da.values)
    arr = np.asarray(value)
    if arr.ndim != 2 or arr.shape != grid.shape:
        raise PublishError(f"variable {name}: array shape {arr.shape} does not match the {where}")
    return arr


def _coerce(name: str, arr: np.ndarray, var: Variable) -> np.ndarray:
    """Values in the variable's dtype; :class:`PublishError` for non-numeric input or out-of-range values."""
    if arr.dtype == bool or not np.issubdtype(arr.dtype, np.number):
        raise PublishError(f"variable {name}: numeric values expected, got dtype {arr.dtype}")
    vals = arr.astype("float64")
    if np.isinf(vals).any():
        raise PublishError(f"variable {name}: {int(np.isinf(vals).sum())} infinite values")
    nan = np.isnan(vals)
    valid = ~nan
    if not var.is_float:
        if (np.abs(vals[valid] - np.round(vals[valid])) > 0).any():
            raise PublishError(f"variable {name}: {var.dtype} class values must be integers")
        valid &= vals != float(var.nodata)
    if var.valid_range is not None:
        lo, hi = var.valid_range
        bad = valid & ((vals < lo) | (vals > hi))
        if bad.any():
            raise PublishError(f"variable {name}: {int(bad.sum())} values outside valid_range [{lo:g}, {hi:g}] "
                               f"(min {vals[bad].min():g}, max {vals[bad].max():g}); fix them or set them to "
                               f"nodata ({'NaN' if var.is_float else var.nodata})")
    if var.is_float:
        return vals.astype("float32")
    out = np.where(nan, float(var.nodata), vals)
    return out.astype(var.dtype)


def check_arrays(dataset: DatasetConfig, aoi: AOI, arrays: Mapping[str, Any]) -> dict[str, np.ndarray]:
    """Variable name -> array in the variable's dtype on the AOI grid (module docstring, steps 1-2)."""
    if not isinstance(arrays, Mapping) or not arrays:
        raise PublishError("arrays must be a non-empty mapping {variable name: 2-D array}")
    declared = [v.name for v in dataset.variables]
    unknown = sorted(set(arrays) - set(declared))
    if unknown:
        raise PublishError(f"{dataset.id}: unknown variables {unknown}; declared: {declared}")
    missing = [v.name for v in dataset.variables if v.required and v.name not in arrays]
    if missing:
        raise PublishError(f"{dataset.id}: missing required variables {missing}")
    grid = aoi.grid
    out = {}
    for v in dataset.variables:                          # config order
        if v.name in arrays:
            out[v.name] = _coerce(v.name, _grid_array(v.name, arrays[v.name], grid, aoi), v)
    return out


def _lineage_links(item: dict, sources: list[dict]) -> None:
    rel = item_rel(item["collection"], item["id"])
    for s in sources:
        item["links"].append({"rel": "derived_from", "href": relative_href(rel, item_rel(s["collection"], s["id"])),
                              "type": JSON_TYPE, "title": s["id"]})


def _finish(lake: Lake, dataset: DatasetConfig, aoi: AOI, staged: StagedItem, key: str, sources: list[dict],
            final_paths: dict[str, str], tmp_root: str, replace_dir: str | None = None) -> dict:
    """Clip, build and validate the item on its temporary files, then move the files to ``final_paths`` (asset key
    -> lake path), register and announce it. Shared by raster and table publishes."""
    from fuellake.pipeline import build_stac_item, new_run_id

    region = lake_region()
    run_id = f"publish-{new_run_id()}"
    ref = SourceRef(dataset_id=dataset.id, key=key, datetime=staged.datetime, urls=tuple(s["id"] for s in sources))
    try:
        if region is not None:
            clip_staged_item(lake, staged, region, default_bbox=aoi.grid.bounds_wgs84())
        item = build_stac_item(lake, dataset, aoi, ref, staged, run_id)
        failing = [r for r in validate_item(lake, item, dataset, aoi, region=region) if not r.ok]
        if failing:
            raise PublishError(f"{dataset.id}: the item fails its contract: "
                               + "; ".join(f"{r.target}: {', '.join(r.errors)}" for r in failing))
        if replace_dir is not None:                    # a republish may drop an optional variable
            keep = set(final_paths.values())
            for old_rel in lake.find(replace_dir):
                if old_rel not in keep and not old_rel.startswith(tmp_root):
                    lake.rm(old_rel)
        for a in staged.assets:
            lake.write_bytes(final_paths[a.key], lake.read_bytes(a.path))
            a.path = final_paths[a.key]
    finally:
        lake.rm(tmp_root, recursive=True)
    item = build_stac_item(lake, dataset, aoi, ref, staged, run_id)
    _lineage_links(item, sources)
    catalog.upsert_collection(lake, dataset, bbox=aoi.bbox)
    for old in catalog.find_by_source_key(lake, dataset.id, key):     # same key, other id: replaced
        if old["id"] != item["id"]:
            for k in old.get("assets") or {}:
                old_rel = catalog.asset_rel(old, k)
                if old_rel not in final_paths.values():
                    lake.rm(old_rel, recursive=True)
            catalog.remove_item(lake, dataset.id, old["id"])
    rel, created = catalog.add_item(lake, item)
    ev = dict(dataset=dataset.id, aoi=aoi.id, collection=dataset.id, item_id=item["id"], href=rel, run_id=run_id,
              origin=ORIGIN)
    events.emit(lake, "item.created" if created else "item.updated", **ev)
    if dataset.kind == "table":
        events.emit(lake, "table.updated", table=dataset.table, n_rows=item["properties"].get("fuellake:n_rows"),
                    **ev)
    return item


# ---------------------------------------------------------------------------- raster
def publish_raster(lake: Lake, dataset_id: str, aoi: AOI | str, datetime_: datetime | date | str,
                   arrays: Mapping[str, Any], *, properties: Mapping[str, Any] | None = None,
                   source_items: Iterable[Any] | None = None, discriminator: str | None = None) -> dict:
    """Publish one form A item into an interface collection; returns the cataloged STAC item (module docstring)."""
    dataset = publishable_dataset(dataset_id, "raster-a")
    aoi = _aoi(aoi, dataset)
    dt = to_utc(datetime_)
    data = check_arrays(dataset, aoi, arrays)
    sources = resolve_source_items(lake, source_items)
    props, sources, times = build_properties(lake, dataset, properties, sources)

    if discriminator is None and (dt.hour, dt.minute, dt.second, dt.microsecond) != (0, 0, 0, 0):
        discriminator = f"{dt:%H%M%S}"
    item_id = f"{dataset.id}__{aoi.id}__{dt:%Y%m%d}" + (f"__{discriminator}" if discriminator else "")
    if not ITEM_ID_RE.match(item_id):
        raise PublishError(f"item id {item_id!r} is not valid; the discriminator may use only A-Z a-z 0-9 . _ -")
    key = f"{ORIGIN}:{dataset.id}:{aoi.id}:{catalog.iso(dt)}:{discriminator or ''}"
    staged_dir = f"staged/{dataset.id}/{aoi.id}/{item_id}"
    tmp_root = f"staged/{dataset.id}/{aoi.id}/.publish-{uuid.uuid4().hex[:12]}"
    assets, final = [], {}
    try:
        for name, arr in data.items():
            var = dataset.variable(name)
            path = f"{tmp_root}/{name}.tif"
            write_cog(lake, path, arr, aoi.grid, np.nan if var.is_float else var.nodata, var.dtype,
                      description=name, unit=var.unit)
            assets.append(StagedAsset(key=name, path=path, media_type=COG_MEDIA_TYPE, roles=["data"],
                                      raster_band=var.stac_band()))
            final[name] = f"{staged_dir}/{name}.tif"
    except BaseException:
        lake.rm(tmp_root, recursive=True)
        raise
    props["fuellake:qc_masked_fraction"] = 0.0           # out-of-range values are rejected, never masked
    staged = StagedItem(item_id=item_id, datetime=dt, properties=props, assets=assets,
                        start_datetime=times.get("start_datetime"), end_datetime=times.get("end_datetime"))
    return _finish(lake, dataset, aoi, staged, key, sources, final, tmp_root, replace_dir=staged_dir)


# ---------------------------------------------------------------------------- table
def _table_datetime(df: pd.DataFrame, value: Any) -> tuple[datetime, datetime | None, datetime | None]:
    import pandas as pd

    if value is not None:
        return to_utc(value), None, None
    for col in ("datetime", "date", "acq_datetime", "valid_from"):
        if col in df.columns and len(df):
            vals = pd.to_datetime(df[col], utc=True, errors="coerce").dropna()
            if len(vals):
                lo, hi = vals.min().to_pydatetime(), vals.max().to_pydatetime()
                end = hi if col != "date" else catalog.to_utc(hi.date(), end_of_day=True)
                return lo, lo, end
    raise PublishError("cannot date the table: pass datetime=... or include a 'datetime' or 'date' column")


def publish_table(lake: Lake, dataset_id: str, aoi: AOI | str, df: pd.DataFrame, key: str, *,
                  properties: Mapping[str, Any] | None = None, datetime_: datetime | date | str | None = None,
                  source_items: Iterable[Any] | None = None) -> dict:
    """Publish one GeoParquet part into an interface table collection; returns the STAC item (module docstring)."""
    dataset = publishable_dataset(dataset_id, "table")
    aoi = _aoi(aoi, dataset)
    if not str(key).strip():
        raise PublishError("key must be a non-empty string")
    dt, start, end = _table_datetime(df, datetime_)
    sources = resolve_source_items(lake, source_items)
    props, sources, times = build_properties(lake, dataset, properties, sources)
    try:
        gdf = to_geodataframe(df)
    except ValueError as exc:
        raise PublishError(f"{dataset.id}: {exc}") from None
    item_id = f"{dataset.id}__{aoi.id}__{dt:%Y%m%d}__{safe_key(key)}"
    if not ITEM_ID_RE.match(item_id):
        raise PublishError(f"item id {item_id!r} is not valid; use A-Z a-z 0-9 . _ - in the key")
    part = table_part_path(dataset.table or "", dataset.id, safe_key(f"{aoi.id}__{key}"))
    tmp_root = f"{part.rsplit('/', 1)[0]}/.publish-{uuid.uuid4().hex[:12]}"     # not matched by part-*.parquet
    tmp = f"{tmp_root}/part.parquet"
    write_geoparquet(lake, tmp, gdf)
    schema = validate_table(lake, tmp, dataset.table or "", region=None)
    if not schema.ok:
        lake.rm(tmp_root, recursive=True)
        raise PublishError(f"{dataset.id}: table {dataset.table} schema: " + "; ".join(schema.errors))
    bbox = tuple(float(v) for v in gdf.total_bounds) if len(gdf) and not gdf.geometry.isna().all() else None
    if bbox is not None and any(math.isnan(v) for v in bbox):
        bbox = None
    staged = StagedItem(item_id=item_id, datetime=dt, properties=props,
                        assets=[StagedAsset(key="table", path=tmp, media_type=PARQUET_MEDIA_TYPE, roles=["data"])],
                        bbox=bbox, start_datetime=times.get("start_datetime", start),
                        end_datetime=times.get("end_datetime", end))
    return _finish(lake, dataset, aoi, staged, f"{ORIGIN}:{dataset.id}:{aoi.id}:{key}", sources,
                   {"table": part}, tmp_root)


__all__ = ["PublishError", "publish_raster", "publish_table", "publishable_dataset"]
