# fuel-lake — data-contract validation for staged rasters, tables and STAC items
# Licensed under Apache-2.0.
"""Validators return :class:`ValidationResult` objects (never raise for a failed check).

- :func:`validate_raster_a` — COG on the AOI grid: CRS, transform, shape, dtype, nodata, valid range,
  COG layout.
- :func:`validate_raster_b` — Zarr basics: opens, dims ``(time, y, x)``, variables present, time sorted.
- :func:`validate_cube` — the gold datacube (collection ``features-cube``): ``(time, y, x)`` and static ``(y, x)``
  variables, float32, exactly the AOI grid at the cube resolution, an empty time axis only without
  time-dependent variables.
- :func:`validate_table` — GeoParquet metadata and the column schema of known tables (``lfmc_samples``,
  ``matchups``, ``fire_detections``, ``fire_perimeters``, ``fire_ignitions``, ``point_features``).
- :func:`validate_item_properties` — required STAC properties incl. the NISAR-specific ones; for interface
  datasets also the declared ``item_properties`` and the required variables.
- :func:`validate_item` — all of the above for one item; :func:`validate_collection` for a dataset.
- :func:`check_single_maturity` — refuse NISAR time series that mix maturities.

Region checks (when the lake has a region, see :mod:`fuellake.region`): the item bbox lies inside the region;
no valid form A pixel / cube pixel has its centre, and no finite form B cell touches, outside the polygon
buffered by ``tolerance_m``; every table row lies inside the region (``Region.rows_inside``). ``region``
arguments default to the lake region (``REGION_FROM_CONFIG``); pass ``None`` to skip the region checks.
"""

from __future__ import annotations

import io
import json
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from fuellake.catalog import asset_rel, iter_items, to_utc
from fuellake.grid import Grid
from fuellake.rasters import COG_BLOCKSIZE, COG_MEDIA_TYPE, ZARR_MEDIA_TYPE, cog_info
from fuellake.registry import AOI, MATURITIES, DatasetConfig, Variable, lake_region, load_aoi
from fuellake.storage import Lake

if TYPE_CHECKING:
    from fuellake.region import Region
from fuellake.tables import FUEL_CATEGORIES, PARQUET_MEDIA_TYPE, lfmc_sample_id

REQUIRED_PROPERTIES = (
    "datetime", "fuellake:aoi", "fuellake:dataset", "fuellake:license", "fuellake:redistributable",
    "fuellake:status", "fuellake:source_ids", "fuellake:run_id", "fuellake:pipeline_version",
)
NISAR_PROPERTIES = (
    "fuellake:maturity", "sat:relative_orbit", "fuellake:frame", "sat:orbit_state",
    "fuellake:processing_id", "sar:polarizations", "sar:frequency_band",
)
CUBE_DATASET = "features-cube"        # gold datacube collection (see fuellake.features.cube)
ITEM_ID_RE = re.compile(
    r"^(?P<dataset>[a-z0-9][a-z0-9-]*)__(?P<aoi>[a-z0-9][a-z0-9-]*)__(?P<date>\d{8})(__[A-Za-z0-9._-]+)?$"
)


REGION_FROM_CONFIG: Any = object()      # sentinel: use registry.lake_region()


def _region(region: Any) -> Region | None:
    return lake_region() if region is REGION_FROM_CONFIG else region


@dataclass
class ValidationResult:
    target: str                      # lake-relative path or item id
    check: str                       # raster-a | raster-b | table | item | collection
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def to_dict(self) -> dict:
        return {"target": self.target, "check": self.check, "ok": self.ok, "errors": self.errors,
                "warnings": self.warnings, "details": self.details}


class MixedMaturityError(ValueError):
    pass


def all_ok(results: Iterable[ValidationResult]) -> bool:
    return all(r.ok for r in results)


# ---------------------------------------------------------------------------- form A
def validate_raster_a(lake: Lake, rel: str, grid: Grid, variable: Variable | None = None,
                      region: Region | None = None) -> ValidationResult:
    """Validate a form A COG against the AOI grid, (optionally) the variable spec and the region."""
    r = ValidationResult(target=rel, check="raster-a")
    if not lake.exists(rel):
        r.error("file does not exist")
        return r
    try:
        info = cog_info(lake, rel)
    except Exception as exc:  # unreadable file
        r.error(f"cannot open as raster: {exc}")
        return r
    r.details.update({k: info[k] for k in ("crs", "width", "height", "dtype", "layout", "overviews")})
    if info["driver"] != "GTiff":
        r.error(f"driver {info['driver']} is not GTiff")
    if info["count"] != 1:
        r.error(f"{info['count']} bands; form A requires exactly one band per asset")
    on_grid = info["crs"] is not None and grid.same_as(info["crs"], info["transform"], info["width"],
                                                       info["height"])
    if not on_grid:
        r.error(
            f"grid mismatch: file crs={info['crs']} shape=({info['height']}, {info['width']}) "
            f"transform={info['transform']} vs AOI grid crs={grid.crs} shape={grid.shape} "
            f"transform={list(grid.transform)[:6]}"
        )
    if info["layout"] != "COG":
        r.error("not a Cloud-Optimized GeoTIFF (IMAGE_STRUCTURE LAYOUT != COG); write it with rasters.write_cog")
    if not info["tiled"]:
        r.error("not tiled")
    if max(info["width"], info["height"]) > COG_BLOCKSIZE and not info["overviews"]:
        r.warn("no internal overviews")

    dtype = np.dtype(info["dtype"])
    nodata = info["nodata"]
    is_float = np.issubdtype(dtype, np.floating)
    if is_float:
        if dtype != np.float32:
            r.error(f"continuous data must be float32, got {dtype}")
        if nodata is None or not math.isnan(nodata):
            r.error(f"continuous data must use NaN nodata, got {nodata}")
    elif nodata is None:
        r.error("categorical (integer) data needs an explicit nodata value")

    arr = valid = None
    if (variable is not None and variable.valid_range is not None) or (region is not None and on_grid):
        from fuellake.rasters import read_cog

        arr, _ = read_cog(lake, rel)
        valid = np.isfinite(arr) if is_float else (arr != nodata)
    if region is not None and on_grid and valid is not None:
        outside = int((valid & ~region.pixel_mask(grid, tolerance_m=region.tolerance_m)).sum())
        r.details["pixels_outside_region"] = outside
        if outside:
            r.error(f"{outside} valid pixels lie outside the {region.id} region (clip them to nodata)")
    if variable is not None:
        if dtype.name != variable.dtype:
            r.error(f"dtype {dtype.name} != variable spec {variable.dtype}")
        if not is_float and variable.nodata is not None and nodata is not None and nodata != variable.nodata:
            r.error(f"nodata {nodata} != variable spec {variable.nodata}")
        if variable.valid_range is not None and arr is not None and valid is not None:
            lo, hi = variable.valid_range
            tol = 1e-6 * max(1.0, abs(hi - lo))
            bad = valid & ((arr < lo - tol) | (arr > hi + tol))
            n_valid = int(valid.sum())
            r.details["valid_fraction"] = n_valid / arr.size if arr.size else 0.0
            if n_valid:
                r.details["min"] = float(arr[valid].min())
                r.details["max"] = float(arr[valid].max())
            if bad.any():
                r.error(f"{int(bad.sum())} pixels outside valid_range [{lo}, {hi}] (mask them to nodata)")
            if n_valid == 0:
                r.warn("raster has no valid pixels")
    return r


# ---------------------------------------------------------------------------- form B
def validate_raster_b(lake: Lake, rel: str, variables: Iterable[Variable] | None = None,
                      aoi: AOI | None = None, region: Region | None = None) -> ValidationResult:
    """Validate a form B Zarr store: opens, dims ``(time, y, x)``, declared variables present, no finite cell
    outside the region (cells must touch the buffered polygon)."""
    from fuellake.rasters import open_zarr

    r = ValidationResult(target=rel, check="raster-b")
    if not lake.exists(rel):
        r.error("store does not exist")
        return r
    try:
        ds = open_zarr(lake, rel)
    except Exception as exc:
        r.error(f"cannot open as Zarr: {exc}")
        return r
    r.details["variables"] = sorted(map(str, ds.data_vars))
    r.details["sizes"] = {str(k): int(v) for k, v in ds.sizes.items()}
    if not ds.data_vars:
        r.error("no data variables")
    for name, da in ds.data_vars.items():
        if da.ndim == 0:   # scalar metadata variable (e.g. an undecoded grid mapping)
            continue
        if da.dims != ("time", "y", "x"):
            r.error(f"variable {name} has dims {da.dims}; expected ('time', 'y', 'x')")
    if "time" not in ds.coords:
        r.error("missing time coordinate")
    else:
        t = ds["time"].values
        if t.size == 0:
            r.error("empty time axis")
        elif t.size > 1 and not bool((np.diff(t.astype("datetime64[ns]")) > np.timedelta64(0, "ns")).all()):
            r.error("time coordinate not strictly increasing")
    for v in variables or []:
        if v.name not in ds.data_vars:
            r.error(f"declared variable {v.name} missing")
    has_crs = "spatial_ref" in ds.coords or "crs" in ds.attrs or any("grid_mapping" in da.attrs or
                                                                       "grid_mapping" in da.encoding
                                                                       for da in ds.data_vars.values())
    if not has_crs:
        r.warn("no CRS information (spatial_ref coordinate, grid_mapping or crs attribute)")
    if aoi is not None and has_crs and "x" in ds.coords and "y" in ds.coords:
        try:
            import rioxarray  # noqa: F401
            from rasterio.warp import transform_bounds

            crs = ds.rio.crs
            if crs is not None:
                w, s, e, n = transform_bounds("EPSG:4326", crs, *aoi.bbox, densify_pts=21)
                xs, ys = ds["x"].values, ds["y"].values
                if xs.min() > w or xs.max() < e or ys.min() > s or ys.max() < n:
                    r.warn("store does not fully cover the AOI bbox (expected bbox + one native cell)")
        except Exception as exc:  # coverage is advisory
            r.warn(f"could not check AOI coverage: {exc}")
    if region is not None and not r.errors:
        _check_grid_region(ds, r, region, cube=False)
    return r


def _check_grid_region(ds, r: ValidationResult, region: Region, cube: bool) -> None:
    """Finite values of ``(..., y, x)`` variables outside the region are errors (form B: all-touched cells;
    cube: pixel centres; both against the polygon buffered by the region tolerance)."""
    try:
        import rioxarray  # noqa: F401

        from fuellake.region import _grid_of_dataset, zarr_inside_mask

        if cube:
            grid, flipped = _grid_of_dataset(ds)
            inside = region.pixel_mask(grid, tolerance_m=region.tolerance_m)
            inside = inside[::-1] if flipped else inside
        else:
            inside = zarr_inside_mask(ds, region, tolerance_m=region.tolerance_m)
    except Exception as exc:
        r.error(f"cannot check the {region.id} region: {exc}")
        return
    outside = 0
    for da in ds.data_vars.values():
        if da.ndim < 2 or tuple(da.dims[-2:]) != ("y", "x") or not np.issubdtype(da.dtype, np.floating):
            continue
        outside += int((np.isfinite(np.asarray(da.values)) & ~inside).sum())
    r.details["values_outside_region"] = outside
    if outside:
        r.error(f"{outside} finite values lie outside the {region.id} region (clip them to NaN)")


# ---------------------------------------------------------------------------- gold datacube
CUBE_DIMS = (("time", "y", "x"), ("y", "x"))


def validate_cube(lake: Lake, rel: str, grid: Grid | None = None, item: dict | None = None,
                  region: Region | None = None) -> ValidationResult:
    """Validate the gold datacube ``features/<aoi>/cube.zarr`` (layout: :mod:`fuellake.features.cube`).

    - Every data variable is ``float32`` with dims ``(time, y, x)`` (dynamic, weather) or ``(y, x)`` (static).
    - ``time`` is strictly increasing. It may be empty (a cube of static layers only); time-dependent
      variables on an empty time axis are reported as a warning.
    - ``x`` / ``y`` are the pixel centres of ``grid`` (the AOI grid at the cube resolution) and the store
      carries a CRS equal to the grid CRS.
    - With ``item``: the variables match ``fuellake:variables`` (names and dims) and ``fuellake:dims``.
    """
    from fuellake.rasters import open_zarr

    r = ValidationResult(target=rel, check="cube")
    if not lake.exists(rel):
        r.error("store does not exist")
        return r
    try:
        ds = open_zarr(lake, rel)
    except Exception as exc:
        r.error(f"cannot open as Zarr: {exc}")
        return r
    r.details["variables"] = sorted(map(str, ds.data_vars))
    r.details["sizes"] = {str(k): int(v) for k, v in ds.sizes.items()}
    data_vars = {str(k): v for k, v in ds.data_vars.items() if v.ndim > 0}
    if not data_vars:
        r.error("no data variables")
    timed = []
    for name, da in data_vars.items():
        if da.dims not in CUBE_DIMS:
            r.error(f"variable {name} has dims {da.dims}; expected ('time', 'y', 'x') or static ('y', 'x')")
        elif da.dims[0] == "time":
            timed.append(name)
        if da.dtype != np.float32:
            r.error(f"variable {name} has dtype {da.dtype}; the cube stores float32 with NaN fill")
    if "time" not in ds.coords:
        r.error("missing time coordinate")
    else:
        t = ds["time"].values
        if t.size == 0 and timed:
            r.warn(f"empty time axis but time-dependent variables {sorted(timed)}")
        elif t.size > 1 and not bool((np.diff(t.astype("datetime64[ns]")) > np.timedelta64(0, "ns")).all()):
            r.error("time coordinate not strictly increasing")
    try:
        import rioxarray  # noqa: F401

        crs = ds.rio.crs
    except Exception:
        crs = None
    if crs is None:
        r.error("no CRS information (spatial_ref grid mapping)")
    if grid is not None:
        xs = ds["x"].values if "x" in ds.coords else np.array([])
        ys = ds["y"].values if "y" in ds.coords else np.array([])
        tol = 1e-6 * max(1.0, abs(grid.resolution))
        if xs.shape != (grid.width,) or ys.shape != (grid.height,):
            r.error(f"shape (y={ys.size}, x={xs.size}) != AOI grid {grid.shape} at {grid.resolution} m")
        elif (np.abs(xs - grid.x_coords()).max() > tol) or (np.abs(ys - grid.y_coords()).max() > tol):
            r.error("x/y coordinates are not the pixel centres of the AOI grid")
        if crs is not None:
            from rasterio.crs import CRS

            if CRS.from_user_input(crs) != CRS.from_user_input(grid.crs):
                r.error(f"CRS {crs} != AOI grid CRS {grid.crs}")
    if item is not None:
        props = item.get("properties", {})
        declared = props.get("fuellake:variables")
        if isinstance(declared, dict):
            if set(declared) != set(data_vars):
                r.error(f"store variables {sorted(data_vars)} != item fuellake:variables {sorted(declared)}")
            for name, spec in declared.items():
                dims = spec.get("dims") if isinstance(spec, dict) else None
                if name in data_vars and dims is not None and tuple(dims) != data_vars[name].dims:
                    r.error(f"variable {name}: dims {data_vars[name].dims} != item {tuple(dims)}")
        dims = props.get("fuellake:dims")
        if isinstance(dims, dict):
            for d, n in dims.items():
                if d in ds.sizes and int(ds.sizes[d]) != int(n):
                    r.error(f"dimension {d} has size {ds.sizes[d]} but fuellake:dims says {n}")
    if region is not None and crs is not None and not r.errors:
        _check_grid_region(ds, r, region, cube=True)
    return r


def cube_grid_for(item: dict, aoi: AOI) -> Grid:
    """AOI grid at the resolution recorded in a cube item (``fuellake:resolution``; default the AOI's)."""
    res = item.get("properties", {}).get("fuellake:resolution")
    if res is None or abs(float(res) - aoi.resolution) < 1e-9:
        return aoi.grid
    return aoi.grid_at(float(res))


# ---------------------------------------------------------------------------- tables
def _is_string(t: pa.DataType) -> bool:
    return pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_string_view(t)


def _is_binary(t: pa.DataType) -> bool:
    return pa.types.is_binary(t) or pa.types.is_large_binary(t) or pa.types.is_binary_view(t)


def _is_ts_us_utc(t: pa.DataType) -> bool:
    return pa.types.is_timestamp(t) and t.unit == "us" and t.tz in ("UTC", "+00:00", "Etc/UTC", "Z")


TypeCheck = tuple[str, Callable[[pa.DataType], bool]]
STRING: TypeCheck = ("string", _is_string)
FLOAT64: TypeCheck = ("float64", pa.types.is_float64)
INT32: TypeCheck = ("int32", pa.types.is_int32)
BOOL: TypeCheck = ("bool", pa.types.is_boolean)
DATE32: TypeCheck = ("date32", pa.types.is_date32)
TS_US_UTC: TypeCheck = ("timestamp[us, UTC]", _is_ts_us_utc)
WKB: TypeCheck = ("binary (WKB)", _is_binary)

TABLE_SCHEMAS: dict[str, dict[str, TypeCheck]] = {
    "lfmc_samples": {
        "sample_id": STRING, "source": STRING, "site_id": STRING, "site_name": STRING,
        "lon": FLOAT64, "lat": FLOAT64, "geometry": WKB, "date": DATE32, "species": STRING,
        "fuel_category": STRING, "lfmc_pct": FLOAT64, "n_raw_rows": INT32, "outlier_flag": BOOL,
        "qc_flags": STRING, "status": STRING, "ingested_at": TS_US_UTC,
    },
    # Only the geometry is required; the connectors document their columns.
    "fire_detections": {"geometry": WKB},
    "fire_perimeters": {"geometry": WKB},
    # fuellake.connectors.fire_ignitions.IGNITION_SCHEMA documents the other columns; ignition_id is the row key
    # (fuellake.tables.TABLE_KEYS) and must be unique within a part.
    "fire_ignitions": {"ignition_id": STRING, "geometry": WKB},
    # Gold point-feature table (fuellake.features.points): the common columns; feature columns depend on what is
    # staged and are listed in the item property fuellake:columns.
    "point_features": {"point_type": STRING, "point_id": STRING, "date": DATE32, "lon": FLOAT64, "lat": FLOAT64,
                       "block_50km": STRING, "geometry": WKB},
}
# Gold label x feature table (fuellake.features.matchups): the label columns, then the grid columns. Feature
# columns depend on what is staged and are listed in the item property fuellake:columns.
TABLE_SCHEMAS["matchups"] = {
    **TABLE_SCHEMAS["lfmc_samples"],
    "grid_x": FLOAT64, "grid_y": FLOAT64, "grid_row": INT32, "grid_col": INT32,
}
LFMC_SOURCES = ("fems", "globe-lfmc")


def _crs_is_wgs84(crs: Any) -> bool:
    if crs is None:   # GeoParquet default is OGC:CRS84
        return True
    if isinstance(crs, str):
        return crs.upper() in ("EPSG:4326", "OGC:CRS84")
    ident = crs.get("id") or {}
    return (ident.get("authority"), ident.get("code")) in (("EPSG", 4326), ("OGC", "CRS84"))


def validate_table(lake: Lake, rel: str, table: str, region: Region | None = None) -> ValidationResult:
    """Validate a GeoParquet table part: ``geo`` metadata, the registered schema for ``table`` and (with a
    region) that every row lies inside the region."""
    r = ValidationResult(target=rel, check="table")
    if not lake.exists(rel):
        r.error("file does not exist")
        return r
    try:
        data = lake.read_bytes(rel)
        pf = pq.ParquetFile(io.BytesIO(data))
        schema = pf.schema_arrow
    except Exception as exc:
        r.error(f"cannot read parquet: {exc}")
        return r
    r.details["n_rows"] = pf.metadata.num_rows
    meta = schema.metadata or {}
    if b"geo" not in meta:
        r.error("missing GeoParquet 'geo' metadata")
    else:
        geo = json.loads(meta[b"geo"])
        primary = geo.get("primary_column")
        col = (geo.get("columns") or {}).get(primary or "", {})
        r.details["geoparquet_version"] = geo.get("version")
        if primary != "geometry":
            r.error(f"GeoParquet primary_column is {primary!r}, expected 'geometry'")
        if col.get("encoding") != "WKB":
            r.error(f"geometry encoding {col.get('encoding')!r}, expected WKB")
        if not _crs_is_wgs84(col.get("crs")):
            r.error("geometry CRS is not EPSG:4326 / OGC:CRS84")
        if region is not None and not r.errors and pf.metadata.num_rows:
            from fuellake.region import table_geometries

            try:
                inside = region.rows_inside(table_geometries(pf.read(columns=[primary])))
            except Exception as exc:
                r.error(f"cannot read geometries to check the {region.id} region: {exc}")
            else:
                outside = int((~inside).sum())
                r.details["rows_outside_region"] = outside
                if outside:
                    r.error(f"{outside} rows lie outside the {region.id} region (or have no geometry)")

    expected = TABLE_SCHEMAS.get(table)
    if expected is None:
        r.warn(f"no schema registered for table {table!r}; only GeoParquet metadata checked")
        return r
    names = set(schema.names)
    empty = pf.metadata.num_rows == 0
    for col_name, (type_name, pred) in expected.items():
        if col_name not in names:
            r.error(f"missing column {col_name}")
        elif not pred(schema.field(col_name).type):
            if empty and pa.types.is_null(schema.field(col_name).type):
                continue        # an empty pandas object column is written as type null: no data to check
            r.error(f"column {col_name} has type {schema.field(col_name).type}, expected {type_name}")
    if table in ("lfmc_samples", "matchups") and not r.errors:
        _check_lfmc_rows(pf, r, unique_sample_id=table == "matchups")
    if table == "fire_ignitions" and not r.errors and pf.metadata.num_rows:
        ids = pf.read(columns=["ignition_id"]).column("ignition_id").to_pandas()
        if ids.isna().any():
            r.error(f"{int(ids.isna().sum())} rows without ignition_id")
        if ids.dropna().duplicated().any():
            r.error(f"{int(ids.dropna().duplicated().sum())} duplicate ignition_id values")
    return r


def _check_lfmc_rows(pf: pq.ParquetFile, r: ValidationResult, unique_sample_id: bool = False) -> None:
    df = pf.read(columns=["sample_id", "source", "site_id", "date", "species", "fuel_category",
                          "lon", "lat", "lfmc_pct"]).to_pandas()
    if df.empty:
        r.warn("table part has no rows")
        return
    dup = df.duplicated(subset=["source", "site_id", "date", "species"]).sum()
    if dup:
        r.error(f"{int(dup)} duplicate rows on (source, site_id, date, species)")
    if unique_sample_id and df["sample_id"].duplicated().any():
        r.error(f"{int(df['sample_id'].duplicated().sum())} duplicate sample_id values")
    bad_src = sorted(set(df["source"].dropna()) - set(LFMC_SOURCES))
    if bad_src:
        r.error(f"unknown source values {bad_src}; expected {LFMC_SOURCES}")
    bad_cat = sorted(set(df["fuel_category"].dropna()) - set(FUEL_CATEGORIES))
    if bad_cat:
        r.error(f"unknown fuel_category values {bad_cat}; expected {FUEL_CATEGORIES}")
    if ((df["lon"] < -180) | (df["lon"] > 180) | (df["lat"] < -90) | (df["lat"] > 90)).any():
        r.error("lon/lat outside EPSG:4326 range")
    if (df["lfmc_pct"] < 0).any():
        r.error("negative lfmc_pct values")
    expected_ids = [lfmc_sample_id(s, i, d, sp) for s, i, d, sp in
                    zip(df["source"], df["site_id"], df["date"], df["species"], strict=True)]
    n_bad = int((df["sample_id"].to_numpy() != np.array(expected_ids)).sum())
    if n_bad:
        r.error(f"{n_bad} sample_id values are not sha1('source|site_id|date|species')")


# ---------------------------------------------------------------------------- items
def _is_float_type(data_type: Any) -> bool:
    try:
        return bool(np.issubdtype(np.dtype(str(data_type)), np.floating))
    except TypeError:
        return True     # unknown type name: keep the unit warning


def validate_item_properties(item: dict, dataset: DatasetConfig) -> ValidationResult:
    """Required STAC properties, NISAR extras, asset metadata and id conventions (no file IO)."""
    r = ValidationResult(target=item.get("id", "?"), check="item")
    props = item.get("properties", {})
    for key in REQUIRED_PROPERTIES:
        if props.get(key) is None:
            r.error(f"missing property {key}")
    try:
        to_utc(props.get("datetime"))
    except Exception:
        r.error(f"datetime {props.get('datetime')!r} is not an ISO UTC timestamp")
    if props.get("fuellake:dataset") not in (None, dataset.id):
        r.error(f"fuellake:dataset {props.get('fuellake:dataset')!r} != {dataset.id!r}")
    if item.get("collection") != dataset.id:
        r.error(f"collection {item.get('collection')!r} != dataset id {dataset.id!r}")
    ids = props.get("fuellake:source_ids")
    if ids is not None and (not isinstance(ids, list) or not ids):
        r.error("fuellake:source_ids must be a non-empty list")
    if not isinstance(props.get("fuellake:redistributable", False), bool):
        r.error("fuellake:redistributable must be a boolean")
    m = ITEM_ID_RE.match(item.get("id", ""))
    if not m:
        r.error(f"item id {item.get('id')!r} does not match <dataset>__<aoi>__<yyyymmdd>[__<discriminator>]")
    else:
        if m["dataset"] != dataset.id:
            r.error(f"item id prefix {m['dataset']!r} != dataset id {dataset.id!r}")
        if props.get("fuellake:aoi") and m["aoi"] != props["fuellake:aoi"]:
            r.error(f"item id AOI {m['aoi']!r} != fuellake:aoi {props['fuellake:aoi']!r}")

    if dataset.is_nisar:
        for key in NISAR_PROPERTIES:
            if props.get(key) is None:
                r.error(f"missing NISAR property {key}")
        if props.get("fuellake:maturity") is not None and props["fuellake:maturity"] not in MATURITIES:
            r.error(f"fuellake:maturity {props['fuellake:maturity']!r} not in {MATURITIES}")
        if props.get("sar:frequency_band") not in (None, "L"):
            r.error(f"sar:frequency_band must be 'L', got {props['sar:frequency_band']!r}")
        if props.get("sat:orbit_state") not in (None, "ascending", "descending"):
            r.error(f"sat:orbit_state must be ascending|descending, got {props['sat:orbit_state']!r}")
        pols = props.get("sar:polarizations")
        if pols is not None and (not isinstance(pols, list) or not pols):
            r.error("sar:polarizations must be a non-empty list")

    assets = item.get("assets") or {}
    if not assets:
        r.error("item has no assets")
    for key, a in assets.items():
        href = a.get("href", "")
        if "://" in href or href.startswith("/"):
            r.error(f"asset {key}: href must be relative, got {href!r}")
    data_assets = {k: a for k, a in assets.items() if "data" in (a.get("roles") or [])}
    if dataset.kind == "raster-a":
        if "fuellake:qc_masked_fraction" not in props:
            r.warn("missing fuellake:qc_masked_fraction (set it from rasters.mask_out_of_range)")
        for key, a in data_assets.items():
            for f in ("proj:epsg", "proj:transform", "proj:shape", "raster:bands"):
                if a.get(f) is None:
                    r.error(f"asset {key}: missing {f}")
            bands = a.get("raster:bands") or [{}]
            for f in ("data_type", "nodata"):
                if f not in bands[0]:
                    r.error(f"asset {key}: raster:bands[0] missing {f}")
            # A unit is expected for continuous (float) data only: categorical integer codes have none.
            if "unit" not in bands[0] and _is_float_type(bands[0].get("data_type", "float32")):
                r.warn(f"asset {key}: raster:bands[0] missing unit")
            if dataset.variables and dataset.variable(key) is None:
                r.error(f"asset key {key!r} is not a declared variable of {dataset.id}")
    elif dataset.kind == "table":
        a = assets.get("table")
        if a is None:
            r.error("table item needs an asset with key 'table'")
        elif a.get("type") != PARQUET_MEDIA_TYPE:
            r.error(f"table asset media type {a.get('type')!r} != {PARQUET_MEDIA_TYPE}")
        if not isinstance(props.get("fuellake:n_rows"), int):
            r.error("table item needs integer property fuellake:n_rows")
    elif dataset.kind == "raster-b" and not any(a.get("type") == ZARR_MEDIA_TYPE for a in assets.values()):
        r.error(f"form B item needs a Zarr asset ({ZARR_MEDIA_TYPE})")
    if dataset.is_interface:
        _check_interface(props, assets, dataset, r)
    return r


def check_property_value(spec, value: Any) -> str | None:
    """Why ``value`` does not fit the declared :class:`~fuellake.registry.ItemProperty` type, or None."""
    t = spec.type
    if t == "string":
        ok = isinstance(value, str) and value.strip() != ""
    elif t == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    elif t == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif t == "boolean":
        ok = isinstance(value, bool)
    elif t == "datetime":
        try:
            to_utc(value)
            ok = isinstance(value, str)
        except Exception:
            ok = False
    elif t in ("item", "items"):
        ids = [value] if t == "item" else value
        ok = isinstance(ids, list) and bool(ids) and all(isinstance(i, str) and ITEM_ID_RE.match(i) for i in ids)
        if ok and spec.collection:
            bad = [i for i in ids if ITEM_ID_RE.match(i)["dataset"] != spec.collection]
            if bad:
                return f"items {bad} are not in collection {spec.collection!r}"
    else:
        return f"unknown property type {t!r}"
    return None if ok else f"{value!r} is not a valid {t}"


def _check_interface(props: dict, assets: dict, dataset: DatasetConfig, r: ValidationResult) -> None:
    for spec in dataset.item_properties:
        if props.get(spec.name) is None:
            if spec.required:
                r.error(f"missing required property {spec.name} ({spec.description or spec.type})")
            continue
        problem = check_property_value(spec, props[spec.name])
        if problem:
            r.error(f"property {spec.name}: {problem}")
    for v in dataset.variables:
        if v.required and v.name not in assets:
            r.error(f"missing required variable {v.name} (an asset of that name)")


def validate_item(lake: Lake, item: dict, dataset: DatasetConfig, aoi: AOI | None = None, *,
                  region: Any = REGION_FROM_CONFIG) -> list[ValidationResult]:
    """Validate properties and every data asset file of an item. ``aoi`` defaults to ``fuellake:aoi``;
    ``region`` to the lake region (``None`` skips the region checks)."""
    results = [validate_item_properties(item, dataset)]
    region = _region(region)
    if region is not None and item.get("bbox"):
        problem = region.bbox_problem(item["bbox"])
        if problem:
            results[0].error(f"item {problem}")
    if aoi is None:
        try:
            aoi = load_aoi(item["properties"]["fuellake:aoi"])
        except Exception as exc:
            results[0].error(f"cannot load AOI: {exc}")
            return results
    for key, a in (item.get("assets") or {}).items():
        if "data" not in (a.get("roles") or []):
            continue
        try:
            rel = asset_rel(item, key)
        except Exception as exc:
            results[0].error(f"asset {key}: {exc}")
            continue
        if dataset.kind == "raster-a" and a.get("type") == COG_MEDIA_TYPE:
            res = validate_raster_a(lake, rel, aoi.grid, dataset.variable(key), region=region)
        elif dataset.id == CUBE_DATASET and a.get("type") == ZARR_MEDIA_TYPE:
            res = validate_cube(lake, rel, cube_grid_for(item, aoi), item, region=region)
        elif dataset.kind == "raster-b" and a.get("type") == ZARR_MEDIA_TYPE:
            res = validate_raster_b(lake, rel, dataset.variables, aoi, region=region)
        elif dataset.kind == "table" and key == "table":
            res = validate_table(lake, rel, dataset.table or "", region=region)
            n = item["properties"].get("fuellake:n_rows")
            if res.ok and n is not None and n != res.details.get("n_rows"):
                res.error(f"fuellake:n_rows={n} but the part has {res.details.get('n_rows')} rows")
        else:
            res = ValidationResult(target=rel, check="asset")
            res.warn(f"asset {key} ({a.get('type')}) not covered by a contract check")
        results.append(res)
    return results


def check_single_maturity(items: Iterable[dict], allow_mixed: bool = False) -> set[str]:
    """Return the set of ``fuellake:maturity`` values; raise if more than one unless ``allow_mixed``."""
    found = {i["properties"].get("fuellake:maturity") for i in items}
    found.discard(None)
    if len(found) > 1 and not allow_mixed:
        raise MixedMaturityError(f"time series mixes maturities {sorted(found)}; pass allow_mixed=True to accept")
    return found


def validate_collection(lake: Lake, dataset: DatasetConfig, *,
                        region: Any = REGION_FROM_CONFIG) -> list[ValidationResult]:
    """Validate every cataloged item of ``dataset`` (and the region, see the module docstring); adds a
    collection-level maturity check."""
    results: list[ValidationResult] = []
    region = _region(region)
    items = list(iter_items(lake, dataset.id))
    for item in items:
        results.extend(validate_item(lake, item, dataset, region=region))
    coll = ValidationResult(target=dataset.id, check="collection", details={"n_items": len(items)})
    if dataset.is_nisar:
        by_stack: dict[tuple, list[dict]] = {}
        for item in items:
            p = item["properties"]
            by_stack.setdefault((p.get("fuellake:aoi"), p.get("sat:relative_orbit"), p.get("fuellake:frame")),
                                []).append(item)
        for stack, its in by_stack.items():
            try:
                check_single_maturity(its)
            except MixedMaturityError as exc:
                coll.warn(f"stack {stack}: {exc}")
    results.append(coll)
    return results
