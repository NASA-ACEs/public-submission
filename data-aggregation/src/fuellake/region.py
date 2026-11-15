# fuel-lake — the monitored region: test AOIs, bboxes, rows and pixels against it; clip staged outputs to it
# Licensed under Apache-2.0.
"""The lake holds data about one monitored region only (``config/lake.yaml`` -> ``region: california``).

A region is ``config/regions/<id>.yaml`` (id, name, source, vintage, licence, ``tolerance_m``, ``metric_crs``)
plus the GeoJSON polygon it names (EPSG:4326). Load it with :func:`fuellake.registry.lake_region`.

What "inside the region" means (one definition, used by the pipeline, the publish API and validation)
----------------------------------------------------------------------------------------------------
- **AOI** (:meth:`Region.aoi_problem`): its WGS84 bbox lies within the region envelope widened by
  ``tolerance_m`` and intersects the polygon. A bbox cannot lie inside a coastline: ``smm`` reaches into the
  Pacific and ``ca`` is the box around the state. Parts of an AOI outside the polygon simply hold nodata.
- **Item bbox** (:meth:`Region.bbox_problem`): the same test. The pipeline clamps item bboxes to the region
  envelope (:meth:`Region.clamp_bbox`), because grid and cell bounds overshoot it (the snapped ``ca`` grid,
  form B buffer cells, perimeter polygons crossing the state line).
- **Table rows** (:meth:`Region.rows_inside`): a point row is inside when it lies within ``tolerance_m`` of the
  polygon (the 1:500,000 outline is generalised, so coastal sites can sit just outside it); any other geometry
  is inside when it intersects the polygon buffered by ``tolerance_m`` (fire perimeters are kept whole). Rows
  without a geometry are outside.
- **Form A pixels** (:meth:`Region.pixel_mask`): a pixel is inside when its centre lies inside the polygon.
- **Form B cells** (native weather grids of 4-11 km): a cell is inside when it touches the polygon
  (``all_touched``), so the coastal cells that carry the land values of coastal AOIs survive.
- **Validation** uses the same rules with the polygon buffered by ``tolerance_m`` for pixels and cells, so a
  correctly clipped output always passes.

Clipping (:func:`clip_staged_item`): raster pixels / cells outside become nodata, table rows outside are
dropped. COGs and parquet parts are rewritten only when something changes; Zarr variables are rewritten in
place. Tables are filtered with pyarrow, so the column types and GeoParquet metadata stay exactly as written.
Item properties ``fuellake:region`` (region id) and ``fuellake:region_clip`` (``masked_pixels``,
``dropped_rows``) record what happened.
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import shapely
import yaml
from shapely.geometry import box, mapping, shape

from fuellake.grid import Grid, crs_to_str

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

    from fuellake.storage import Lake

DENSIFY_DEG = 0.01        # max segment length before projecting the polygon (straight in lon/lat)
DENSIFY_M = 1000.0        # max segment length before projecting a buffered polygon back to EPSG:4326


class OutsideRegionError(ValueError):
    """An AOI (or a published output) lies outside the lake region."""


def _transform(geom: BaseGeometry, src: str, dst: str) -> BaseGeometry:
    from pyproj import Transformer

    if src == dst:
        return geom
    tr = Transformer.from_crs(src, dst, always_xy=True)

    def fn(coords: np.ndarray) -> np.ndarray:
        x, y = tr.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geom, fn)


@dataclass(eq=False)
class Region:
    """A monitored region (EPSG:4326 polygon) with cached buffers and raster masks."""

    id: str
    name: str
    geometry: Any                       # shapely (Multi)Polygon, EPSG:4326
    tolerance_m: float = 1000.0
    metric_crs: str = "EPSG:3310"
    meta: dict = field(default_factory=dict)
    _cache: dict = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ geometry
    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """Envelope of the polygon (west, south, east, north)."""
        return tuple(float(v) for v in self.geometry.bounds)  # type: ignore[return-value]

    @property
    def fingerprint(self) -> str:
        """8 hex chars over id, tolerance and polygon (part of connector source keys)."""
        if "fp" not in self._cache:
            h = hashlib.sha1(f"{self.id}|{self.tolerance_m}|".encode())
            h.update(shapely.to_wkb(self.geometry))
            self._cache["fp"] = h.hexdigest()[:8]
        return self._cache["fp"]

    def _tol(self, tolerance_m: float | None) -> float:
        return float(self.tolerance_m if tolerance_m is None else tolerance_m)

    def in_crs(self, crs: str, tolerance_m: float = 0.0) -> BaseGeometry:
        """The polygon (buffered by ``tolerance_m`` metres) in ``crs``."""
        crs = crs_to_str(crs)
        key = ("crs", crs, float(tolerance_m))
        if key not in self._cache:
            if tolerance_m > 0:
                metric = _transform(shapely.segmentize(self.geometry, DENSIFY_DEG), "EPSG:4326", self.metric_crs)
                g = shapely.segmentize(metric.buffer(tolerance_m), DENSIFY_M)
                g = _transform(g, self.metric_crs, crs)
            else:
                g = self.geometry if crs == "EPSG:4326" else _transform(
                    shapely.segmentize(self.geometry, DENSIFY_DEG), "EPSG:4326", crs)
            if not g.is_valid:
                g = shapely.make_valid(g)
            shapely.prepare(g)
            self._cache[key] = g
        return self._cache[key]

    def buffered(self, tolerance_m: float | None = None) -> BaseGeometry:
        """The polygon widened by ``tolerance_m`` (default: the region tolerance), EPSG:4326."""
        return self.in_crs("EPSG:4326", self._tol(tolerance_m))

    def envelope(self, tolerance_m: float | None = None) -> tuple[float, float, float, float]:
        return tuple(float(v) for v in self.buffered(tolerance_m).bounds)  # type: ignore[return-value]

    # ------------------------------------------------------------------ predicates
    def bbox_problem(self, bbox, tolerance_m: float | None = None) -> str | None:
        """Why a WGS84 bbox is not inside the region (module docstring), or None."""
        w, s, e, n = (float(v) for v in bbox)
        ew, es, ee, en = self.envelope(tolerance_m)
        eps = 1e-9
        if w < ew - eps or s < es - eps or e > ee + eps or n > en + eps:
            return (f"bbox {[round(v, 5) for v in (w, s, e, n)]} extends beyond the {self.id} region envelope "
                    f"{[round(v, 5) for v in (ew, es, ee, en)]} (incl. {self._tol(tolerance_m):g} m tolerance)")
        if not shapely.intersects(self.buffered(tolerance_m), box(w, s, e, n)):
            return f"bbox {[round(v, 5) for v in (w, s, e, n)]} does not intersect the {self.id} region"
        return None

    def aoi_problem(self, aoi) -> str | None:
        """Why an AOI (``.bbox``) is not inside the region, or None."""
        return self.bbox_problem(aoi.bbox)

    def check_aoi(self, aoi) -> None:
        problem = self.aoi_problem(aoi)
        if problem:
            raise OutsideRegionError(f"AOI {aoi.id!r} lies outside the lake region {self.id!r}: {problem}")

    def clamp_bbox(self, bbox) -> tuple[float, float, float, float]:
        """``bbox`` intersected with the region envelope (unchanged if they do not overlap)."""
        w, s, e, n = (float(v) for v in bbox)
        rw, rs, re_, rn = self.bbox
        out = (max(w, rw), max(s, rs), min(e, re_), min(n, rn))
        return out if out[0] <= out[2] and out[1] <= out[3] else (w, s, e, n)

    def contains_points(self, lon, lat, tolerance_m: float | None = None) -> np.ndarray:
        """Boolean array: point within ``tolerance_m`` of the polygon (boundary counts as inside)."""
        lon = np.asarray(lon, dtype="float64")
        lat = np.asarray(lat, dtype="float64")
        ok = np.isfinite(lon) & np.isfinite(lat)
        out = np.zeros(lon.shape, dtype=bool)
        if ok.any():
            out[ok] = shapely.intersects_xy(self.buffered(tolerance_m), lon[ok], lat[ok])
        return out

    def rows_inside(self, geoms, tolerance_m: float | None = None) -> np.ndarray:
        """Boolean array over shapely geometries (None = outside): intersects the buffered polygon."""
        arr = np.asarray(geoms, dtype=object)
        if arr.size == 0:
            return np.zeros(0, dtype=bool)
        return np.asarray(shapely.intersects(self.buffered(tolerance_m), arr), dtype=bool)

    def pixel_mask(self, grid: Grid, *, all_touched: bool = False, tolerance_m: float = 0.0) -> np.ndarray:
        """``(height, width)`` boolean array, True where the pixel is inside (centre rule, or ``all_touched``)."""
        key = ("mask", crs_to_str(grid.crs), tuple(round(v, 9) for v in list(grid.transform)[:6]), grid.width,
               grid.height, bool(all_touched), float(tolerance_m))
        if key not in self._cache:
            from rasterio.features import geometry_mask

            geom = self.in_crs(grid.crs, float(tolerance_m))
            outline = box(*grid.bounds())
            if shapely.contains(geom, outline):
                mask = np.ones(grid.shape, dtype=bool)
            elif not shapely.intersects(geom, outline):
                mask = np.zeros(grid.shape, dtype=bool)
            else:
                mask = geometry_mask([mapping(geom)], out_shape=grid.shape, transform=grid.transform,
                                     all_touched=all_touched, invert=True)
            if len(self._cache) > 256:
                self._cache = {k: v for k, v in self._cache.items() if k[0] != "mask"}
            self._cache[key] = mask
        return self._cache[key]

    def to_geojson(self) -> dict:
        return {"type": "Feature", "properties": {"id": self.id, "name": self.name},
                "geometry": mapping(self.geometry), "bbox": list(self.bbox)}


# ---------------------------------------------------------------------------- loading
@functools.lru_cache(maxsize=8)
def _load_cached(yaml_path: str, yaml_mtime: int, geom_path: str, geom_mtime: int) -> Region:
    with open(yaml_path, encoding="utf-8") as f:
        meta = yaml.safe_load(f) or {}
    with open(geom_path, encoding="utf-8") as f:
        gj = json.load(f)
    feats = gj.get("features") if gj.get("type") == "FeatureCollection" else [gj]
    geoms = [shape(ft["geometry"] if ft.get("type") == "Feature" else ft) for ft in feats or []]
    if not geoms:
        raise ValueError(f"{geom_path}: no geometry")
    geom = shapely.union_all(geoms) if len(geoms) > 1 else geoms[0]
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"{geom_path}: region geometry must be a (Multi)Polygon, got {geom.geom_type}")
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    shapely.prepare(geom)
    return Region(id=str(meta["id"]), name=str(meta.get("name") or meta["id"]), geometry=geom,
                  tolerance_m=float(meta.get("tolerance_m", 1000.0)),
                  metric_crs=str(meta.get("metric_crs") or "EPSG:3310"), meta=meta)


def load_region_file(yaml_path: Path) -> Region:
    """Load ``<id>.yaml`` and its ``geometry`` file (re-read when either file changes)."""
    yaml_path = Path(yaml_path)
    with open(yaml_path, encoding="utf-8") as f:
        meta = yaml.safe_load(f) or {}
    if not isinstance(meta, dict) or "id" not in meta:
        raise ValueError(f"{yaml_path}: expected a mapping with 'id'")
    if meta["id"] != yaml_path.stem:
        raise ValueError(f"{yaml_path}: id {meta['id']!r} does not match file name")
    geom_path = yaml_path.parent / str(meta.get("geometry") or f"{yaml_path.stem}.geojson")
    if not geom_path.is_file():
        raise FileNotFoundError(f"{yaml_path}: geometry file {geom_path} not found")
    return _load_cached(str(yaml_path), yaml_path.stat().st_mtime_ns, str(geom_path), geom_path.stat().st_mtime_ns)


# ---------------------------------------------------------------------------- clipping
def _grid_of_dataset(ds) -> tuple[Grid, bool]:
    """AOI-style grid of a form B dataset with regular 1-D ``x``/``y`` (``flipped`` if ``y`` increases)."""
    from affine import Affine

    crs = ds.rio.crs
    if crs is None:
        raise ValueError("form B store has no CRS (spatial_ref); cannot clip it to the region")
    x = np.asarray(ds["x"].values, dtype="float64")
    y = np.asarray(ds["y"].values, dtype="float64")
    if x.size < 1 or y.size < 1:
        raise ValueError("form B store has empty x/y coordinates")
    dx = float(np.median(np.diff(x))) if x.size > 1 else float(ds.rio.resolution()[0])
    dy = float(np.median(np.diff(y))) if y.size > 1 else -abs(float(ds.rio.resolution()[1]))
    flipped = dy > 0
    top = float(y.max())
    res_y = abs(dy)
    transform = Affine(dx, 0.0, float(x[0]) - dx / 2, 0.0, -res_y, top + res_y / 2)
    return Grid(crs=crs_to_str(crs), transform=transform, width=x.size, height=y.size, resolution=abs(dx)), flipped


def zarr_inside_mask(ds, region: Region, tolerance_m: float = 0.0) -> np.ndarray:
    """``(y, x)`` boolean mask of the cells of a form B dataset that touch the region (module docstring)."""
    grid, flipped = _grid_of_dataset(ds)
    mask = region.pixel_mask(grid, all_touched=True, tolerance_m=tolerance_m)
    return mask[::-1] if flipped else mask


def clip_cog(lake: Lake, rel: str, region: Region) -> int:
    """Set the valid pixels of a single-band COG whose centre is outside the region to nodata; returns the
    number of pixels masked (the file is rewritten only if that is > 0)."""
    import rasterio

    from fuellake.rasters import write_cog

    with lake.local_path(rel, "r") as path, rasterio.open(path) as src:
        arr = src.read(1)
        nodata = src.nodata
        grid = Grid(crs=crs_to_str(src.crs), transform=src.transform, width=src.width, height=src.height,
                    resolution=abs(src.transform.a))
        description = src.descriptions[0]
        unit = src.units[0] if src.units else None
        tags = src.tags()
        dtype = src.dtypes[0]
    is_float = np.issubdtype(np.dtype(dtype), np.floating)
    valid = np.isfinite(arr) if is_float else (arr != nodata)
    bad = valid & ~region.pixel_mask(grid)
    n = int(bad.sum())
    if n:
        arr = arr.copy()
        arr[bad] = np.nan if is_float else nodata
        tags.pop("AREA_OR_POINT", None)
        write_cog(lake, rel, arr, grid, np.nan if is_float else nodata, dtype, description=description,
                  unit=unit or None, tags=tags or None)
    return n


def clip_zarr(lake: Lake, rel: str, region: Region) -> int:
    """Set the finite values of form B cells that do not touch the region to NaN; returns the number of values
    masked. Only the changed variables are rewritten (in place, ``mode="r+"``, encodings kept)."""
    import rioxarray  # noqa: F401  (registers the .rio accessor)
    import xarray as xr

    from fuellake.rasters import open_zarr

    ds = open_zarr(lake, rel)
    if "x" not in ds.dims or "y" not in ds.dims:
        return 0
    inside = zarr_inside_mask(ds, region)
    if inside.all():
        return 0
    changed: dict[str, xr.DataArray] = {}
    n = 0
    for name, da in ds.data_vars.items():
        if da.ndim < 2 or tuple(da.dims[-2:]) != ("y", "x") or not np.issubdtype(da.dtype, np.floating):
            continue
        vals = np.asarray(da.values)
        bad = np.isfinite(vals) & ~inside
        if bad.any():
            n += int(bad.sum())
            vals = vals.copy()
            vals[bad] = np.nan
            changed[str(name)] = xr.DataArray(vals, dims=da.dims, attrs=da.attrs)
    ds.close()
    if changed:
        kwargs: dict[str, Any] = dict(mode="r+", consolidated=False)
        if not lake.is_local:
            kwargs["storage_options"] = lake.storage_options
        xr.Dataset(changed).to_zarr(lake.url(rel), **kwargs)
    return n


def _geo_meta(schema) -> tuple[dict, str]:
    meta = schema.metadata or {}
    if b"geo" not in meta:
        raise ValueError("parquet part has no GeoParquet 'geo' metadata")
    geo = json.loads(meta[b"geo"])
    return geo, geo.get("primary_column") or "geometry"


def table_geometries(table) -> np.ndarray:
    """Shapely geometries (None for nulls) of the primary geometry column of a GeoParquet pyarrow table."""
    _, col = _geo_meta(table.schema)
    wkb = table.column(col).to_pylist()
    return np.asarray(shapely.from_wkb(np.asarray(wkb, dtype=object)), dtype=object) if wkb else np.zeros(0, object)


def clip_table(lake: Lake, rel: str, region: Region) -> tuple[int, tuple | None]:
    """Drop the rows of a GeoParquet part that lie outside the region (pyarrow filter: types and metadata are
    kept). Returns ``(rows dropped, bounds of the remaining geometries or None)``."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(io.BytesIO(lake.read_bytes(rel)))
    if table.num_rows == 0:
        return 0, None
    geoms = table_geometries(table)
    keep = region.rows_inside(geoms)
    n = int((~keep).sum())
    kept = geoms[keep]
    bounds = tuple(float(v) for v in shapely.total_bounds(kept)) if keep.any() else None
    if n:
        geo, col = _geo_meta(table.schema)
        table = table.filter(pa.array(keep))
        colmeta = (geo.get("columns") or {}).get(col)
        if isinstance(colmeta, dict) and "bbox" in colmeta:
            if bounds is None:
                colmeta.pop("bbox")
            else:
                colmeta["bbox"] = list(bounds)
        meta = dict(table.schema.metadata or {})
        meta[b"geo"] = json.dumps(geo).encode()
        table = table.replace_schema_metadata(meta)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="zstd")
        lake.write_bytes(rel, buf.getvalue())
    return n, bounds


def _bbox_geometry(bbox) -> dict | None:
    w, s, e, n = bbox
    return {"type": "Point", "coordinates": [w, s]} if (w == e and s == n) else None


def clip_staged_item(lake: Lake, staged, region: Region, default_bbox=None) -> dict:
    """Clip every data asset of a :class:`~fuellake.connectors.base.StagedItem` to the region and fix its
    bbox / geometry and region properties in place. Returns ``{"masked_pixels": n, "dropped_rows": n}``."""
    from fuellake.rasters import COG_MEDIA_TYPE, ZARR_MEDIA_TYPE
    from fuellake.tables import PARQUET_MEDIA_TYPE

    stats = {"masked_pixels": 0, "dropped_rows": 0}
    table_bounds: list[tuple | None] = []
    for a in staged.assets:
        if "data" not in (a.roles or []):
            continue
        media = (a.media_type or "").lower()
        if media == COG_MEDIA_TYPE or a.path.lower().endswith((".tif", ".tiff")):
            stats["masked_pixels"] += clip_cog(lake, a.path, region)
        elif media == ZARR_MEDIA_TYPE or a.path.endswith(".zarr"):
            stats["masked_pixels"] += clip_zarr(lake, a.path, region)
        elif media == PARQUET_MEDIA_TYPE or a.path.endswith(".parquet"):
            dropped, bounds = clip_table(lake, a.path, region)
            stats["dropped_rows"] += dropped
            if dropped:
                table_bounds.append(bounds)
    old = tuple(float(v) for v in (staged.bbox or default_bbox or region.bbox))
    bbox = old
    if table_bounds:
        remaining = [b for b in table_bounds if b is not None]
        if remaining:
            bbox = (min(b[0] for b in remaining), min(b[1] for b in remaining),
                    max(b[2] for b in remaining), max(b[3] for b in remaining))
        staged.geometry = _bbox_geometry(region.clamp_bbox(bbox))
    bbox = region.clamp_bbox(bbox)
    if bbox != old and staged.geometry is not None and staged.geometry.get("type") != "Point":
        g = shape(staged.geometry).intersection(box(*bbox))
        staged.geometry = mapping(g) if not g.is_empty else None
    staged.bbox = bbox
    staged.properties["fuellake:region"] = region.id
    staged.properties["fuellake:region_clip"] = stats
    return stats
