# fuel-lake — Copernicus DEM GLO-30: elevation, slope and aspect on the AOI grid
# Licensed under Apache-2.0.
"""Copernicus DEM GLO-30 (public COGs on AWS, cataloged by Element 84 Earth Search).

Source: Earth Search collection ``cop-dem-glo-30``; each 1x1 degree tile has one asset ``data``
(float32 heights in metres, EGM2008 geoid, pixel-is-point, ``s3://copernicus-dem-30m/...`` read
anonymously over HTTPS). It is a **surface** model (DSM: buildings and canopy included).

One static unit per AOI (period ignored), key ``cop-dem-glo-30:<sorted tile ids>``, datetime of the
Earth Search items (2021-04-22, the Copernicus DEM 2021 release).

Processing
----------
1. Read only the windows of the tiles that cover the AOI grid padded by one cell, mosaic them on the
   native 1 arc-second grid (``rasterio.merge`` with bounds, so tile edges are seamless).
2. One reprojection onto the padded AOI grid. Resampling follows the contract rule
   (:func:`fuellake.rasters.choose_resampling`) with the geometric-mean source pixel size in metres
   (~28 m at 34 N vs 30 m target -> ``average``); the choice is stored in ``fuellake:resampling``.
3. Slope and aspect with Horn's (1981) 3x3 method on the projected 30 m grid, then crop the pad so
   every AOI pixel has a full neighbourhood.

Conventions
-----------
- ``slope``: degrees, 0 = flat, from ``atan(hypot(dz/dx, dz/dy))``.
- ``aspect``: compass direction the slope **faces** (downslope), degrees clockwise from **grid north**
  of the AOI CRS (EPSG:32611 grid north differs from true north by about 1 degree over the
  Santa Monica Mountains); 0/360 = north, 90 = east, 180 = south, 270 = west; range [0, 360).
  ``NaN`` where the cell is exactly flat (e.g. sea at 0 m), because aspect is undefined there.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import numpy as np
import rasterio
from affine import Affine
from rasterio.merge import merge

from fuellake.connectors._stac_source import (
    gdal_env,
    grid_bounds_in,
    https_href,
    metres_per_pixel,
    search_items,
)
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.grid import Grid
from fuellake.rasters import choose_resampling, mask_out_of_range, reproject_array, write_cog
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.cop_dem")

DEFAULT_API = "https://earth-search.aws.element84.com/v1"
DEFAULT_COLLECTION = "cop-dem-glo-30"
ASPECT_CONVENTION = ("degrees clockwise from grid north of the AOI CRS, direction the slope faces "
                     "(downslope); NaN where flat")


# ---------------------------------------------------------------------------- terrain math
def padded_grid(grid: Grid, cells: int = 1) -> Grid:
    """``grid`` grown by ``cells`` on every side (same resolution and alignment)."""
    t = grid.transform
    return Grid(crs=grid.crs, transform=Affine(t.a, t.b, t.c - cells * t.a, t.d, t.e, t.f - cells * t.e),
                width=grid.width + 2 * cells, height=grid.height + 2 * cells, resolution=grid.resolution)


def slope_aspect(z: np.ndarray, xres: float, yres: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Horn (1981) slope and aspect in degrees for a north-up array ``z`` (row 0 = north).

    Returns float32 arrays of the same shape; the one-pixel border and any cell with a NaN neighbour
    are NaN. Aspect is the downslope azimuth, clockwise from grid north, in [0, 360); NaN where flat.
    """
    z = np.asarray(z, dtype="float64")
    yres = abs(yres if yres is not None else xres)
    xres = abs(xres)
    slope = np.full(z.shape, np.nan, dtype="float32")
    aspect = np.full(z.shape, np.nan, dtype="float32")
    if z.shape[0] < 3 or z.shape[1] < 3:
        return slope, aspect
    a, b, c = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]      # north row (west, centre, east)
    d, f = z[1:-1, :-2], z[1:-1, 2:]                      # middle row (west, east)
    g, h, i = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]          # south row
    dz_east = ((c + 2 * f + i) - (a + 2 * d + g)) / (8.0 * xres)
    dz_north = ((a + 2 * b + c) - (g + 2 * h + i)) / (8.0 * yres)
    grad = np.hypot(dz_east, dz_north)
    slope[1:-1, 1:-1] = np.degrees(np.arctan(grad)).astype("float32")
    # downslope direction = minus the gradient; azimuth = atan2(east component, north component)
    az = np.degrees(np.arctan2(-dz_east, -dz_north)) % 360.0
    az = np.where(grad > 0.0, az, np.nan).astype("float32")
    az[az >= 360.0] = 0.0                                 # 359.99999 rounds to 360.0 in float32: wrap to north
    aspect[1:-1, 1:-1] = az
    no_elev = ~np.isfinite(z)                             # Horn ignores the centre cell; a cell without
    slope[no_elev] = np.nan                               # elevation gets no slope or aspect either
    aspect[no_elev] = np.nan
    return slope, aspect


# ---------------------------------------------------------------------------- connector
class CopDemConnector(BaseConnector):
    """Static elevation/slope/aspect item per AOI from Copernicus DEM GLO-30."""

    def _opts(self) -> dict:
        o = self.dataset.options
        return {"api": o.get("stac_api", DEFAULT_API), "collection": o.get("stac_collection", DEFAULT_COLLECTION)}

    def _search(self, bbox: tuple[float, float, float, float]) -> list[dict]:
        o = self._opts()
        return search_items(o["api"], o["collection"], bbox, user_agent=self.settings.user_agent)

    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        o = self._opts()
        pad = padded_grid(aoi.grid, 2)
        items = sorted(self._search(pad.bounds_wgs84()), key=lambda it: it["id"])
        if not items:
            log.warning("cop-dem: no tiles found for AOI %s", aoi.id)
            return []
        tiles = []
        for it in items:
            asset = it["assets"]["data"]
            region = it.get("properties", {}).get("storage:region")
            tiles.append({"id": it["id"], "href": https_href(asset["href"], region)})
        dt = min(datetime.fromisoformat(it["properties"]["datetime"].replace("Z", "+00:00")) for it in items)
        key = f"{o['collection']}:{'+'.join(t['id'] for t in tiles)}"
        return [self.ref(key, dt.astimezone(UTC), urls=[t["href"] for t in tiles], tiles=tiles,
                         collection=o["collection"])]

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        return []   # streamed: windowed reads of the public COGs in transform

    def elevation_on_grid(self, hrefs: list[str], grid: Grid) -> tuple[np.ndarray, str, float]:
        """Mosaic the tiles' windows on their native grid and reproject once onto ``grid``.
        Returns ``(elevation float32, resampling name, source pixel size in metres)``."""
        with gdal_env(self.settings.user_agent):
            srcs = [rasterio.open(h) for h in hrefs]
            try:
                crs = srcs[0].crs
                for s in srcs[1:]:
                    if s.crs != crs:
                        raise ValueError("DEM tiles have different CRSs")
                w, s_, e, n = grid_bounds_in(grid, crs, pad_m=2 * grid.resolution)
                res_x, res_y = srcs[0].res
                bounds = (w - 2 * res_x, s_ - 2 * res_y, e + 2 * res_x, n + 2 * res_y)
                mosaic, transform = merge(srcs, bounds=bounds, nodata=np.nan, dtype="float32")
                src_nodata = srcs[0].nodata
            finally:
                for s in srcs:
                    s.close()
        arr = mosaic[0].astype("float32")
        if src_nodata is not None and not np.isnan(src_nodata):
            arr[arr == src_nodata] = np.nan
        lat = (grid.bounds_wgs84()[1] + grid.bounds_wgs84()[3]) / 2
        src_m = metres_per_pixel(crs, transform, lat=lat)
        resampling = choose_resampling(False, src_m, grid.resolution)
        elev = reproject_array(arr, crs.to_string(), transform, grid, resampling)
        return elev, resampling, src_m

    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        grid = aoi.grid
        pad = padded_grid(grid, 1)
        elev_pad, resampling, src_m = self.elevation_on_grid(list(ref.urls), pad)
        slope_pad, aspect_pad = slope_aspect(elev_pad, grid.resolution)
        crop = (slice(1, -1), slice(1, -1))
        layers = {"elevation": elev_pad[crop], "slope": slope_pad[crop], "aspect": aspect_pad[crop]}
        if not np.isfinite(layers["elevation"]).any():
            raise ValueError(f"no DEM coverage for AOI {aoi.id} from tiles {ref.meta.get('tiles')}")

        iid = self.item_id(aoi, ref)
        assets, qc = [], {}
        for name, arr in layers.items():
            var = self.variable(name)
            arr, frac = mask_out_of_range(arr, var.valid_range)
            qc[name] = frac
            path = self.cog_path(aoi, iid, name)
            write_cog(self.lake, path, arr, grid, np.nan, var.dtype, description=name, unit=var.unit)
            assets.append(self.cog_asset(name, path))
        elev = layers["elevation"]
        props = {
            "fuellake:qc_masked_fraction": max(qc.values()),
            "fuellake:qc_masked_fraction_by_variable": qc,
            "fuellake:dem_tiles": [t["id"] for t in ref.meta.get("tiles", [])],
            "fuellake:dem_collection": ref.meta.get("collection"),
            "fuellake:resampling": resampling,
            "fuellake:source_pixel_size_m": round(src_m, 2),
            "fuellake:slope_method": "Horn (1981) 3x3 on the projected AOI grid",
            "fuellake:aspect_convention": ASPECT_CONVENTION,
            "fuellake:surface_model": "DSM (includes buildings and vegetation)",
            "fuellake:vertical_datum": "EGM2008",
            "fuellake:elevation_range_m": [float(np.nanmin(elev)), float(np.nanmax(elev))],
            "platform": "tandem-x",
        }
        return [StagedItem(item_id=iid, datetime=ref.datetime, properties=props, assets=assets)]
