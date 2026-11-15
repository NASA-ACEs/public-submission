# fuel-lake — tests for the Copernicus DEM connector (slope/aspect math, offline tiles, live run)
# Licensed under Apache-2.0.
from __future__ import annotations

import math
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml
from affine import Affine

from conftest import TINY_AOI
from fuellake import catalog, pipeline
from fuellake.connectors.base import Period
from fuellake.connectors.cop_dem import CopDemConnector, padded_grid, slope_aspect
from fuellake.contracts import validate_collection
from fuellake.rasters import read_cog
from fuellake.registry import load_aoi, load_dataset

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
DS = "cop-dem"


@pytest.fixture
def dem_config(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    shutil.copy(REPO_CONFIG / "datasets" / f"{DS}.yaml", root / "datasets" / f"{DS}.yaml")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


# ---------------------------------------------------------------------------- slope / aspect on planes
def _plane(fx, fy, n=7, res=30.0):
    """z = fx * east + fy * north (metres) on a north-up grid (row 0 = north)."""
    rows, cols = np.mgrid[0:n, 0:n]
    east, north = cols * res, (n - 1 - rows) * res
    return fx * east + fy * north


@pytest.mark.parametrize("fx,fy,aspect", [
    (0.1, 0.0, 270.0),     # rises to the east -> faces west
    (-0.1, 0.0, 90.0),     # rises to the west -> faces east
    (0.0, 0.2, 180.0),     # rises to the north -> faces south
    (0.0, -0.2, 0.0),      # rises to the south -> faces north
    (1.0, 1.0, 225.0),     # rises to the north-east -> faces south-west
])
def test_slope_aspect_known_planes(fx, fy, aspect):
    slope, asp = slope_aspect(_plane(fx, fy), 30.0)
    inner = (slope[1:-1, 1:-1], asp[1:-1, 1:-1])
    assert np.allclose(inner[0], math.degrees(math.atan(math.hypot(fx, fy))), atol=1e-4)
    diff = (inner[1] - aspect + 180.0) % 360.0 - 180.0
    assert np.allclose(diff, 0.0, atol=1e-4)
    assert np.isnan(slope[0]).all() and np.isnan(slope[:, -1]).all()   # border needs a full 3x3


def test_slope_aspect_flat_and_nan():
    z = np.zeros((5, 5))
    slope, asp = slope_aspect(z, 30.0)
    assert np.allclose(slope[1:-1, 1:-1], 0.0) and np.isnan(asp).all()
    z = _plane(0.1, 0.0, n=6)
    z[2, 2] = np.nan
    slope, _ = slope_aspect(z, 30.0)
    assert np.isnan(slope[1:4, 1:4]).all() and np.isfinite(slope[4, 4])


def test_aspect_never_reaches_360():
    """A slope facing a hair west of north gives 359.99999..., which float32 rounds to 360.0."""
    fy = -1.0                                       # rises to the south -> faces north
    fx = math.tan(math.radians(1e-6))               # rises a hair to the east -> faces a hair west of north
    raw = math.degrees(math.atan2(-fx, -fy)) % 360.0
    assert raw > 359.99 and np.float32(raw) == np.float32(360.0)    # the case the guard exists for
    _, asp = slope_aspect(_plane(fx, fy), 30.0)
    inner = asp[1:-1, 1:-1]
    assert (inner < 360.0).all() and (inner >= 0.0).all()
    assert np.allclose(inner, 0.0)                  # wrapped to north


def test_padded_grid():
    g = load_aoi_tiny_grid()
    p = padded_grid(g, 1)
    assert p.shape == (g.height + 2, g.width + 2)
    assert p.transform.c == g.transform.c - g.resolution and p.transform.f == g.transform.f + g.resolution


def load_aoi_tiny_grid():
    from fuellake.registry import AOI

    return AOI(**TINY_AOI).grid


# ---------------------------------------------------------------------------- offline pipeline
SPLIT_LAT = 34.06       # tile seam through the tiny AOI
GRADIENT = 0.1          # metres of rise per metre northward -> slope 5.71 deg facing south


def _tile(path: Path, south: float, north: float) -> str:
    """Synthetic 1 arc-second EPSG:4326 DEM tile (pixel-is-area) holding a north-rising plane."""
    res = 1 / 3600
    west, east = -118.85, -118.73
    w, h = int(round((east - west) / res)), int(round((north - south) / res))
    lat = north - (np.arange(h) + 0.5) * res
    z = 100.0 + GRADIENT * (lat - 34.0) * 111_320.0
    arr = np.repeat(z[:, None], w, axis=1).astype("float32")
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32",
                       crs="EPSG:4326", transform=Affine(res, 0, west, 0, -res, north)) as dst:
        dst.write(arr, 1)
    return str(path)


def _items(tmp: Path) -> list[dict]:
    tiles = [("Copernicus_DSM_COG_10_N34_06_TEST", SPLIT_LAT, 34.12), ("Copernicus_DSM_COG_10_N34_00_TEST", 34.0,
                                                                         SPLIT_LAT)]
    return [{"id": tid, "properties": {"datetime": "2021-04-22T00:00:00Z", "storage:region": "eu-central-1"},
             "assets": {"data": {"href": _tile(tmp / f"{tid}.tif", s, n)}}} for tid, s, n in tiles]


def test_pipeline_offline_dem(lake, dem_config, tmp_path, monkeypatch):
    items = _items(tmp_path)
    monkeypatch.setattr(CopDemConnector, "_search", lambda self, bbox: items)
    period = Period(date(2026, 6, 1), date(2026, 6, 2))     # ignored by the static connector
    rep = pipeline.run([DS], "tiny", period, lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", [u.error for u in d.units]
    assert d.count("created") == 1
    item = next(catalog.iter_items(lake, DS))
    assert item["id"] == "cop-dem__tiny__20210422"
    assert set(item["assets"]) == {"elevation", "slope", "aspect"}
    assert item["properties"]["fuellake:resampling"] == "average"   # ~28 m source -> 30 m grid
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))

    slope, grid = read_cog(lake, catalog.asset_rel(item, "slope"))
    aspect, _ = read_cog(lake, catalog.asset_rel(item, "aspect"))
    elev, _ = read_cog(lake, catalog.asset_rel(item, "elevation"))
    assert grid.shape == load_aoi("tiny").grid.shape
    assert np.isfinite(slope).all() and np.isfinite(aspect).all()       # padding: no NaN edges, no seam
    assert np.allclose(slope, math.degrees(math.atan(GRADIENT)), atol=0.3)
    # grid north of UTM 11N is ~1 deg off true north here; the plane faces true south
    assert np.allclose(aspect, 180.0, atol=2.0)
    assert np.all(np.diff(elev, axis=0) < 0)                            # higher to the north (row 0)

    rep2 = pipeline.run([DS], "tiny", Period(date(2026, 7, 1), date(2026, 7, 2)), lake=lake)
    assert (rep2.datasets[0].skipped, len(rep2.datasets[0].units)) == (1, 0)


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_cop_dem_tiny_aoi(lake, dem_config):
    rep = pipeline.run([DS], "tiny", Period(date(2026, 11, 1), date(2026, 11, 14)), lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", d.error or [u.error for u in d.units]
    item = next(catalog.iter_items(lake, DS))
    assert item["properties"]["fuellake:dem_tiles"] == ["Copernicus_DSM_COG_10_N34_00_W119_00_DEM"]
    elev, _ = read_cog(lake, catalog.asset_rel(item, "elevation"))
    slope, _ = read_cog(lake, catalog.asset_rel(item, "slope"))
    assert np.isfinite(elev).all() and np.isfinite(slope).all()
    assert 0 < float(np.nanmin(elev)) and float(np.nanmax(elev)) < 1000      # Santa Monica Mountains
    assert 5 < float(np.nanmedian(slope)) < 45
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))
