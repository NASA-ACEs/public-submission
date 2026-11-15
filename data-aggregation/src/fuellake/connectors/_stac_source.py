# fuel-lake — shared helpers for open raster sources: STAC search, GDAL HTTP settings, windowed reads
# Licensed under Apache-2.0.
"""Small helpers used by the Sentinel-2, Copernicus DEM, LANDFIRE and biomass connectors.

- :func:`search_items` — STAC API item search (pagination handled by ``pystac-client``), returns dicts.
- :func:`gdal_env` — ``rasterio.Env`` for remote COG reads: ``User-Agent: fuel-lake/0.1``, no directory
  listing, merged range requests, retries, anonymous S3.
- :func:`https_href` — ``s3://bucket/key`` -> public ``https://`` URL (no AWS credentials involved).
- :func:`read_window` — read only the part of a raster that covers a target grid (plus a pad).
- :func:`metres_per_pixel` — source pixel size in metres (geographic CRSs approximated at a latitude),
  used with :func:`fuellake.rasters.choose_resampling`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.warp import transform_bounds
from rasterio.windows import Window

from fuellake.config import USER_AGENT
from fuellake.grid import Grid


# ---------------------------------------------------------------------------- STAC
def search_items(api_url: str, collection: str, bbox: Iterable[float], datetime: str | None = None,
                 user_agent: str = USER_AGENT, limit: int = 100) -> list[dict]:
    """All items of ``collection`` intersecting ``bbox`` (WGS84) and ``datetime`` (RFC 3339 interval)."""
    from pystac_client import Client

    client = Client.open(api_url, headers={"User-Agent": user_agent})
    kwargs: dict[str, Any] = dict(collections=[collection], bbox=list(bbox), limit=limit)
    if datetime:
        kwargs["datetime"] = datetime
    return list(client.search(**kwargs).items_as_dicts())


# ---------------------------------------------------------------------------- GDAL / HTTP
def gdal_env(user_agent: str = USER_AGENT, **extra: Any) -> rasterio.Env:
    """GDAL settings for reading public COGs over HTTPS with as few requests as possible."""
    opts: dict[str, Any] = dict(
        GDAL_HTTP_USERAGENT=user_agent,
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.tiff,.TIFF",
        GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
        GDAL_HTTP_MULTIPLEX="YES",
        GDAL_HTTP_MAX_RETRY="4",
        GDAL_HTTP_RETRY_DELAY="2",
        VSI_CACHE="TRUE",
        AWS_NO_SIGN_REQUEST="YES",
    )
    opts.update(extra)
    return rasterio.Env(**opts)


def https_href(href: str, region: str | None = None) -> str:
    """Turn ``s3://bucket/key`` into the public virtual-hosted HTTPS URL; other hrefs pass through."""
    if not href.startswith("s3://"):
        return href
    bucket, _, key = href[len("s3://"):].partition("/")
    host = f"{bucket}.s3.{region}.amazonaws.com" if region else f"{bucket}.s3.amazonaws.com"
    return f"https://{host}/{key}"


# ---------------------------------------------------------------------------- windows
@dataclass
class WindowRead:
    """A block of source pixels: ``array`` with its own ``transform`` in ``crs``."""

    array: np.ndarray
    transform: Affine
    crs: str
    window: Window


def grid_bounds_in(grid: Grid, crs: Any, pad_m: float = 0.0) -> tuple[float, float, float, float]:
    """Bounds of ``grid`` (padded by ``pad_m`` metres of the grid CRS) expressed in ``crs``."""
    minx, miny, maxx, maxy = grid.bounds()
    minx, miny, maxx, maxy = minx - pad_m, miny - pad_m, maxx + pad_m, maxy + pad_m
    if CRS.from_user_input(crs) == CRS.from_user_input(grid.crs):
        return (minx, miny, maxx, maxy)
    return tuple(transform_bounds(grid.crs, crs, minx, miny, maxx, maxy, densify_pts=21))  # type: ignore[return-value]


def window_for_bounds(src, bounds: tuple[float, float, float, float], pad_px: int = 1) -> Window | None:
    """Integer window of ``src`` covering ``bounds`` (in ``src.crs``) plus ``pad_px``, clipped to the
    dataset. ``None`` if there is no overlap."""
    inv = ~src.transform
    xs, ys = [], []
    for x, y in ((bounds[0], bounds[1]), (bounds[0], bounds[3]), (bounds[2], bounds[1]), (bounds[2], bounds[3])):
        c, r = inv @ (x, y)
        xs.append(c)
        ys.append(r)
    col0 = max(0, math.floor(min(xs) + 1e-6) - pad_px)
    row0 = max(0, math.floor(min(ys) + 1e-6) - pad_px)
    col1 = min(src.width, math.ceil(max(xs) - 1e-6) + pad_px)
    row1 = min(src.height, math.ceil(max(ys) - 1e-6) + pad_px)
    if col1 <= col0 or row1 <= row0:
        return None
    return Window(col0, row0, col1 - col0, row1 - row0)


def read_window(src, bounds: tuple[float, float, float, float] | None = None, *, window: Window | None = None,
                band: int = 1, pad_px: int = 1) -> WindowRead | None:
    """Read ``band`` of an open dataset over ``bounds`` (or an explicit ``window``). Only the needed COG
    blocks are fetched. Returns ``None`` when the bounds do not overlap the dataset."""
    if window is None:
        if bounds is None:
            raise ValueError("pass bounds or window")
        window = window_for_bounds(src, bounds, pad_px=pad_px)
        if window is None:
            return None
    arr = src.read(band, window=window)
    return WindowRead(array=arr, transform=src.window_transform(window), crs=src.crs.to_string(), window=window)


# ---------------------------------------------------------------------------- resolution
def metres_per_pixel(crs: Any, transform: Affine, lat: float | None = None) -> float:
    """Geometric-mean pixel size in metres. Geographic CRSs are converted at latitude ``lat``."""
    c = CRS.from_user_input(crs)
    dx, dy = abs(transform.a), abs(transform.e)
    if c.is_geographic:
        lat = 0.0 if lat is None else lat
        m_per_deg = 111_320.0
        dx *= m_per_deg * math.cos(math.radians(lat))
        dy *= m_per_deg
    return math.sqrt(dx * dy)
