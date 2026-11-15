# fuel-lake — AOI target grid (CRS, snapped transform, shape)
# Licensed under Apache-2.0.
"""The analysis grid every form A raster is staged on.

The grid is the AOI WGS84 bbox projected to the AOI CRS (densified edges), snapped **outward** to
multiples of the resolution counted from a snap ``origin`` (default ``(0, 0)``). Pixel (0, 0) is the
north-west corner; ``transform`` is north-up.

Reference grids (:data:`REFERENCE_GRIDS`) are named global grids an AOI can adopt with ``reference_grid:``
in its YAML, so its cells coincide with a product's cells. ``ease2-200m`` is EASE-Grid 2.0 Global
(EPSG:6933) at the NISAR SME2 cell size: NSIDC's upper-left corner and the 173,520 x 73,080 cells of the
SME2 200 m grid, i.e. ``2 * 17367530.44516138 / 173520 = 200.17900466991`` m (not 200 m). These constants
were verified against real SME2 files (column/row indices and centre coordinates) in
:mod:`fuellake.connectors.nisar_sme2`, which imports them from here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from affine import Affine
from rasterio.crs import CRS
from rasterio.warp import transform_bounds

# EASE-Grid 2.0 Global (EPSG:6933). Corner from NSIDC (rounded there to 1 cm); cell count from the SME2 spec.
EASE2_EPSG = 6933
EASE2_UL_X = -17367530.44516138
EASE2_UL_Y = 7314540.83011508
EASE2_200M_COLS = 173520
EASE2_200M_ROWS = 73080
EASE2_200M_CELL = 2.0 * abs(EASE2_UL_X) / EASE2_200M_COLS   # 200.17900466991 m


@dataclass(frozen=True)
class ReferenceGrid:
    """A named global grid: CRS, cell size and the corner the cells are counted from."""

    crs: str
    resolution: float
    origin: tuple[float, float]       # (x, y) of a cell corner (upper-left corner of the global grid)
    description: str = ""


REFERENCE_GRIDS: dict[str, ReferenceGrid] = {
    "ease2-200m": ReferenceGrid(
        crs=f"EPSG:{EASE2_EPSG}", resolution=EASE2_200M_CELL, origin=(EASE2_UL_X, EASE2_UL_Y),
        description="EASE-Grid 2.0 Global at the NISAR L3 SME2 200 m cell size (200.179 m), SME2 cell edges"),
}


@dataclass(frozen=True)
class Grid:
    crs: str            # e.g. "EPSG:32611"
    transform: Affine   # north-up, pixel size = resolution
    width: int
    height: int
    resolution: float

    # ------------------------------------------------------------------ geometry
    @property
    def shape(self) -> tuple[int, int]:
        """(height, width) — numpy / ``proj:shape`` order."""
        return (self.height, self.width)

    @property
    def epsg(self) -> int | None:
        return CRS.from_user_input(self.crs).to_epsg()

    def bounds(self) -> tuple[float, float, float, float]:
        """(minx, miny, maxx, maxy) in the grid CRS."""
        t = self.transform
        minx, maxy = t.c, t.f
        maxx = minx + t.a * self.width
        miny = maxy + t.e * self.height
        return (minx, miny, maxx, maxy)

    def bounds_wgs84(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) in EPSG:4326 of the full grid extent."""
        return tuple(transform_bounds(self.crs, "EPSG:4326", *self.bounds(), densify_pts=21))  # type: ignore[return-value]

    def x_coords(self) -> np.ndarray:
        """Pixel-centre x coordinates (length ``width``)."""
        t = self.transform
        return t.c + t.a * (np.arange(self.width) + 0.5)

    def y_coords(self) -> np.ndarray:
        """Pixel-centre y coordinates (length ``height``, decreasing)."""
        t = self.transform
        return t.f + t.e * (np.arange(self.height) + 0.5)

    def same_as(self, crs, transform, width: int, height: int, tol: float = 1e-6) -> bool:
        """True if (crs, transform, width, height) equal this grid (transform compared with tolerance)."""
        if (int(width), int(height)) != (self.width, self.height):
            return False
        if CRS.from_user_input(crs) != CRS.from_user_input(self.crs):
            return False
        a = list(Affine(*list(transform)[:6]))[:6]
        b = list(self.transform)[:6]
        return all(abs(x - y) <= tol * max(1.0, abs(y)) for x, y in zip(a, b, strict=True))

    # ------------------------------------------------------------------ serialisation
    def to_dict(self) -> dict:
        return {
            "crs": self.crs,
            "transform": list(self.transform)[:6],
            "width": self.width,
            "height": self.height,
            "resolution": self.resolution,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Grid:
        return cls(
            crs=d["crs"],
            transform=Affine(*d["transform"][:6]),
            width=int(d["width"]),
            height=int(d["height"]),
            resolution=float(d["resolution"]),
        )

    def stac_proj(self) -> dict:
        """``proj:*`` fields for a STAC asset on this grid (projection extension v1.1.0)."""
        return {
            "proj:epsg": self.epsg,
            "proj:transform": list(self.transform)[:6],
            "proj:shape": [self.height, self.width],
        }


def crs_to_str(crs) -> str:
    """Normalise a CRS to ``EPSG:<code>`` when possible, else WKT."""
    c = CRS.from_user_input(crs)
    code = c.to_epsg()
    return f"EPSG:{code}" if code else c.to_wkt()


def grid_from_bbox(bbox_wgs84: tuple[float, float, float, float] | list[float], crs: str,
                   resolution: float, origin: tuple[float, float] | None = None) -> Grid:
    """Project a WGS84 bbox to ``crs`` and snap it outward to whole cells of ``resolution`` counted from
    ``origin`` (a cell corner; default ``(0, 0)``, i.e. multiples of ``resolution``). Cell edges are then exactly
    ``origin + k * resolution``."""
    west, south, east, north = (float(v) for v in bbox_wgs84)
    if not (west < east and south < north):
        raise ValueError(f"invalid bbox {bbox_wgs84}")
    res = float(resolution)
    if not res > 0:
        raise ValueError(f"resolution must be > 0, got {resolution}")
    ox, oy = (0.0, 0.0) if origin is None else (float(origin[0]), float(origin[1]))
    minx, miny, maxx, maxy = transform_bounds("EPSG:4326", crs, west, south, east, north, densify_pts=21)
    c0 = math.floor((minx - ox) / res)          # cell columns counted east from the origin
    c1 = math.ceil((maxx - ox) / res)
    r0 = math.floor((oy - maxy) / res)          # cell rows counted south from the origin
    r1 = math.ceil((oy - miny) / res)
    return Grid(crs=crs_to_str(crs), transform=Affine(res, 0.0, ox + c0 * res, 0.0, -res, oy - r0 * res),
                width=int(c1 - c0), height=int(r1 - r0), resolution=res)
