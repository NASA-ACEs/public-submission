# fuel-lake — raster helpers: reprojection to the AOI grid, COG and Zarr IO
# Licensed under Apache-2.0.
"""Raster helpers shared by connectors, the feature layer and the API.

Form A rules (ARCHITECTURE.md): one band per COG, exactly the AOI grid, continuous data ``float32`` with
NaN nodata, categorical data integer with explicit nodata. Resampling: continuous downsampling
``average``, upsampling ``bilinear``; categorical ``nearest`` (or ``mode`` when downsampling) — see
:func:`choose_resampling`.

Form B (coarse time series) is a Zarr v3 store; use :func:`write_zarr` / :func:`open_zarr`.

Aligned sources are never resampled: when a source array has the grid's CRS and cell size and its cell edges
coincide with grid cell edges (within :data:`ALIGN_TOLERANCE` of a cell), :func:`reproject_array` copies the
cells into place instead of warping. That is how NISAR SME2 (EASE-Grid 2.0, 200.179 m) lands on the region AOI
``ca``, whose grid is the SME2 grid (``reference_grid: ease2-200m``).

Thread safety: :func:`reproject_array` may be called from many threads at once (the cube builder reads in
dask threads). Each call works on its own georeferenced in-memory GeoTIFFs (unique ``/vsimem/`` files) inside
its own ``rasterio.Env``, with no shared handles. Passing NumPy arrays straight to
``rasterio.warp.reproject`` is avoided on purpose: rasterio then wraps them in ``MemoryDataset``, which uses
``warnings.catch_warnings()``. That context manager mutates the process-wide warning filters and is not
thread-safe: concurrent calls emitted spurious ``NotGeoreferencedWarning`` and could leave a permanent
"ignore every warning" filter behind (seen with rasterio 1.5 / Python 3.12).
"""

from __future__ import annotations

import contextlib
import math
from typing import TYPE_CHECKING, Any

import numpy as np
import rasterio
import rasterio.shutil
from affine import Affine
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject

from fuellake.grid import Grid, crs_to_str

if TYPE_CHECKING:
    import xarray as xr

    from fuellake.storage import Lake

COG_MEDIA_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
ZARR_MEDIA_TYPE = "application/vnd+zarr"
COG_BLOCKSIZE = 512
ALIGN_TOLERANCE = 1e-3        # fraction of a cell: offsets within it count as coincident cell edges


def _resampling(name: str | Resampling) -> Resampling:
    if isinstance(name, Resampling):
        return name
    try:
        return Resampling[name.lower()]
    except KeyError as exc:
        raise ValueError(f"unknown resampling {name!r}; use one of {[r.name for r in Resampling]}") from exc


def _is_float(dtype: Any) -> bool:
    return np.issubdtype(np.dtype(dtype), np.floating)


def _is_nan(v: Any) -> bool:
    return isinstance(v, float) and math.isnan(v)


def choose_resampling(categorical: bool, src_resolution: float, dst_resolution: float) -> str:
    """Contract rule: continuous down=average, up=bilinear; categorical down=mode, up=nearest."""
    downsampling = abs(dst_resolution) > abs(src_resolution) * 1.0001
    if categorical:
        return "mode" if downsampling else "nearest"
    return "average" if downsampling else "bilinear"


# ---------------------------------------------------------------------------- reprojection
def _mem_raster(stack: contextlib.ExitStack, arr: np.ndarray, crs: CRS, transform: Affine, nodata):
    """A private, georeferenced single-band in-memory GeoTIFF holding ``arr`` (closed by ``stack``)."""
    mem = stack.enter_context(MemoryFile())
    ds = stack.enter_context(mem.open(driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
                                      dtype=arr.dtype.name, crs=crs, transform=transform, nodata=nodata))
    ds.write(arr, 1)
    return ds


def aligned_offset(src_crs, src_transform, grid: Grid) -> tuple[int, int] | None:
    """``(row, col)`` of the source's first cell in grid cell indices when the source cells coincide with the
    grid's (same CRS and cell size, north-up, edges within :data:`ALIGN_TOLERANCE` of a cell); else None."""
    t = Affine(*list(src_transform)[:6])
    g = grid.transform
    if t.b != 0 or t.d != 0 or t.e >= 0 or g.e >= 0:
        return None
    if abs(t.a - g.a) > 1e-6 * abs(g.a) or abs(t.e - g.e) > 1e-6 * abs(g.e):
        return None
    if CRS.from_user_input(src_crs) != CRS.from_user_input(grid.crs):
        return None
    fc, fr = (t.c - g.c) / g.a, (t.f - g.f) / g.e
    c, r = round(fc), round(fr)
    if abs(fc - c) > ALIGN_TOLERANCE or abs(fr - r) > ALIGN_TOLERANCE:
        return None
    return int(r), int(c)


def _copy_aligned(src: np.ndarray, offset: tuple[int, int], dst: np.ndarray) -> np.ndarray:
    r0, c0 = offset
    h, w = src.shape
    gr0, gr1 = max(0, r0), min(dst.shape[0], r0 + h)
    gc0, gc1 = max(0, c0), min(dst.shape[1], c0 + w)
    if gr0 < gr1 and gc0 < gc1:
        dst[gr0:gr1, gc0:gc1] = src[gr0 - r0 : gr1 - r0, gc0 - c0 : gc1 - c0]
    return dst


def reproject_array(arr, src_crs, src_transform, grid: Grid, resampling: str = "bilinear",
                    src_nodata=None) -> np.ndarray:
    """Reproject a 2-D array onto ``grid``. Safe to call from several threads at once (module docstring).

    Float input returns ``float32`` with NaN outside the source / at nodata. Integer input keeps its
    dtype and uses ``src_nodata`` (required for integer data) as the fill value. A source whose cells coincide
    with the grid's (:func:`aligned_offset`) is copied cell by cell, whatever ``resampling`` says.
    """
    src = np.asarray(arr)
    if src.ndim != 2:
        raise ValueError(f"expected a 2-D array, got shape {src.shape}")
    if _is_float(src.dtype):
        src = src.astype("float32", copy=True)
        if src_nodata is not None and not _is_nan(float(src_nodata)):
            src[src == src_nodata] = np.nan
        dst = np.full(grid.shape, np.nan, dtype="float32")
        nodata = np.nan
    else:
        if src_nodata is None:
            raise ValueError("integer (categorical) data needs an explicit src_nodata")
        dst = np.full(grid.shape, src_nodata, dtype=src.dtype)
        nodata = src_nodata
    offset = aligned_offset(src_crs, src_transform, grid)
    if offset is not None:
        return _copy_aligned(src, offset, dst)
    with rasterio.Env(), contextlib.ExitStack() as stack:
        src_ds = _mem_raster(stack, src, CRS.from_user_input(src_crs), Affine(*list(src_transform)[:6]), nodata)
        dst_ds = _mem_raster(stack, dst, CRS.from_user_input(grid.crs), grid.transform, nodata)
        reproject(
            source=rasterio.band(src_ds, 1),
            destination=rasterio.band(dst_ds, 1),
            src_nodata=nodata,
            dst_nodata=nodata,
            resampling=_resampling(resampling),
        )
        return dst_ds.read(1)


def _band_index(src, band: int | str) -> int:
    if isinstance(band, int):
        if not 1 <= band <= src.count:
            raise IndexError(f"band {band} out of range 1..{src.count}")
        return band
    for i, desc in enumerate(src.descriptions, start=1):
        if desc and desc.lower() == str(band).lower():
            return i
    raise KeyError(f"no band with description {band!r}; descriptions: {src.descriptions}")


def reproject_to_grid(src_path_or_url: str, grid: Grid, band: int | str = 1, resampling: str = "bilinear",
                      src_nodata=None) -> np.ndarray:
    """Read one band of any GDAL-readable source (path, ``https://``, ``s3://``, ``/vsicurl/``,
    ``NETCDF:...`` subdataset) warped onto ``grid``. Reads only the needed windows (WarpedVRT).

    Float sources return ``float32`` with NaN nodata; integer sources keep their dtype and nodata.
    """
    with rasterio.open(src_path_or_url) as src:
        bidx = _band_index(src, band)
        nodata = src_nodata if src_nodata is not None else src.nodatavals[bidx - 1]
        is_float = _is_float(src.dtypes[bidx - 1])
        if not is_float and nodata is None:
            raise ValueError("integer source has no nodata; pass src_nodata")
        vrt_kwargs: dict[str, Any] = dict(
            crs=CRS.from_user_input(grid.crs),
            transform=grid.transform,
            width=grid.width,
            height=grid.height,
            resampling=_resampling(resampling),
        )
        if is_float:
            vrt_kwargs.update(dtype="float32", nodata=np.nan)
            if nodata is not None:
                vrt_kwargs["src_nodata"] = nodata
        else:
            vrt_kwargs.update(src_nodata=nodata, nodata=nodata)
        with WarpedVRT(src, **vrt_kwargs) as vrt:
            data = vrt.read(bidx)
    if is_float:
        data = data.astype("float32", copy=False)
    return data


def mask_out_of_range(arr: np.ndarray, valid_range, nodata=np.nan) -> tuple[np.ndarray, float]:
    """Set values outside ``valid_range`` to ``nodata``.

    Returns ``(array, masked_fraction)``, where the fraction is the number of masked pixels divided by
    the number of pixels that had data before masking (0.0 if none). Store it as the item property
    ``fuellake:qc_masked_fraction``.
    """
    out = np.array(arr, copy=True)
    if valid_range is None:
        return out, 0.0
    lo, hi = valid_range
    if _is_float(out.dtype):
        has_data = np.isfinite(out) if _is_nan(float(nodata)) else (out != nodata) & np.isfinite(out)
    else:
        has_data = out != nodata
    bad = has_data & ((out < lo) | (out > hi))
    out[bad] = nodata
    n = int(has_data.sum())
    return out, (float(bad.sum()) / n if n else 0.0)


# ---------------------------------------------------------------------------- COG IO
def write_cog(lake: Lake, rel: str, arr: np.ndarray, grid: Grid, nodata, dtype: str,
              description: str | None = None, *, unit: str | None = None,
              overview_resampling: str | None = None, tags: dict | None = None) -> None:
    """Write a single-band Cloud-Optimized GeoTIFF on ``grid`` to ``lake/rel``.

    DEFLATE + predictor, 512 px tiles, internal overviews (``average`` for float, ``nearest`` for
    integer unless ``overview_resampling`` is given). ``nodata`` may be NaN (float only).
    """
    data = np.asarray(arr)
    if data.shape != grid.shape:
        raise ValueError(f"array shape {data.shape} != grid shape {grid.shape}")
    dtype = np.dtype(dtype).name
    if _is_float(dtype):
        nodata = np.nan if nodata is None else float(nodata)
    elif nodata is None or _is_nan(float(nodata)):
        raise ValueError("integer COGs need an explicit integer nodata value")
    data = data.astype(dtype, copy=False)
    ovr = overview_resampling or ("average" if _is_float(dtype) else "nearest")
    profile = dict(driver="GTiff", width=grid.width, height=grid.height, count=1, dtype=dtype,
                   crs=CRS.from_user_input(grid.crs), transform=grid.transform, nodata=nodata)
    with MemoryFile() as mem:
        with mem.open(**profile) as ds:
            ds.write(data, 1)
            if description:
                ds.set_band_description(1, description)
            if unit:
                ds.set_band_unit(1, unit)
            if tags:
                ds.update_tags(**{k: str(v) for k, v in tags.items()})
        with mem.open() as ds, lake.local_path(rel, "w") as path:
            rasterio.shutil.copy(
                ds, path, driver="COG",
                COMPRESS="DEFLATE", PREDICTOR="YES", BLOCKSIZE=str(COG_BLOCKSIZE),
                OVERVIEW_RESAMPLING=ovr.upper(), BIGTIFF="IF_SAFER",
            )


def _grid_of(src) -> Grid:
    return Grid(crs=crs_to_str(src.crs), transform=src.transform, width=src.width, height=src.height,
                resolution=abs(src.transform.a))


def read_cog(lake: Lake, rel: str) -> tuple[np.ndarray, Grid]:
    """Read band 1 of a lake COG. Returns ``(array, grid)``; float nodata is NaN in the array."""
    with lake.local_path(rel, "r") as path, rasterio.open(path) as src:
        return src.read(1), _grid_of(src)


def cog_info(lake: Lake, rel: str) -> dict:
    """Structural facts about a lake GeoTIFF (used by :mod:`fuellake.contracts`)."""
    with lake.local_path(rel, "r") as path, rasterio.open(path) as src:
        return {
            "driver": src.driver,
            "count": src.count,
            "crs": crs_to_str(src.crs) if src.crs else None,
            "transform": list(src.transform)[:6],
            "width": src.width,
            "height": src.height,
            "dtype": src.dtypes[0],
            "nodata": src.nodata,
            "layout": src.tags(ns="IMAGE_STRUCTURE").get("LAYOUT"),
            "tiled": src.profile.get("tiled", False),
            "block_shapes": src.block_shapes,
            "overviews": src.overviews(1),
            "description": src.descriptions[0],
        }


# ---------------------------------------------------------------------------- Zarr IO (form B, cube)
def write_zarr(lake: Lake, rel: str, ds: xr.Dataset) -> None:
    """Write (overwrite) an xarray Dataset as a Zarr v3 store at ``lake/rel`` (local or s3)."""
    kwargs: dict[str, Any] = dict(mode="w", consolidated=False, zarr_format=3)
    if not lake.is_local:
        kwargs["storage_options"] = lake.storage_options
    ds.to_zarr(lake.url(rel), **kwargs)


def open_zarr(lake: Lake, rel: str) -> xr.Dataset:
    """Open a lake Zarr store lazily (dask-backed). CF grid-mapping variables (``spatial_ref``) are
    decoded as coordinates (``decode_coords="all"``), so ``ds.rio.crs`` works after ``import rioxarray``."""
    import xarray as xr

    kwargs: dict[str, Any] = dict(consolidated=False, decode_coords="all")
    if not lake.is_local:
        kwargs["storage_options"] = lake.storage_options
    return xr.open_zarr(lake.url(rel), **kwargs)
