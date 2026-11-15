# fuel-lake — tests for reprojection, COG write/read and Zarr helpers
# Licensed under Apache-2.0.
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import rasterio
import xarray as xr
from affine import Affine

from fuellake import rasters
from fuellake.grid import grid_from_bbox
from fuellake.registry import load_aoi


@pytest.fixture
def smm_grid():
    return load_aoi("smm").grid


def test_write_read_cog_roundtrip_is_valid_cog(lake, smm_grid):
    arr = np.random.default_rng(0).random(smm_grid.shape).astype("float32") * 0.7
    arr[:5, :5] = np.nan
    rel = "staged/x/smm/x__smm__20260601/soil_moisture.tif"
    rasters.write_cog(lake, rel, arr, smm_grid, np.nan, "float32", description="soil_moisture", unit="m3/m3")
    back, grid = rasters.read_cog(lake, rel)
    assert grid == smm_grid
    np.testing.assert_array_equal(np.isnan(back), np.isnan(arr))
    np.testing.assert_allclose(back[~np.isnan(back)], arr[~np.isnan(arr)])
    info = rasters.cog_info(lake, rel)
    assert info["layout"] == "COG" and info["tiled"] and info["count"] == 1
    assert info["block_shapes"] == [(512, 512)]
    assert info["overviews"] == [2, 4]
    assert info["description"] == "soil_moisture"
    with rasterio.open(lake.url(rel)) as src:
        assert np.isnan(src.nodata)
        assert src.tags(ns="IMAGE_STRUCTURE")["COMPRESSION"] == "DEFLATE"


def test_write_cog_integer_and_errors(lake, smm_grid):
    arr = np.full(smm_grid.shape, 102, dtype="int16")
    arr[0, 0] = -9999
    rasters.write_cog(lake, "c.tif", arr, smm_grid, -9999, "int16")
    back, _ = rasters.read_cog(lake, "c.tif")
    assert back.dtype == np.int16 and back[0, 0] == -9999
    with pytest.raises(ValueError):
        rasters.write_cog(lake, "d.tif", arr, smm_grid, None, "int16")
    with pytest.raises(ValueError):
        rasters.write_cog(lake, "e.tif", arr[:-1], smm_grid, -9999, "int16")


def test_reproject_array_and_file_agree(tmp_path):
    grid = grid_from_bbox((-118.80, 34.05, -118.78, 34.07), "EPSG:32611", 30)
    # source: 0.001-degree EPSG:4326 raster covering the AOI with a smooth gradient
    src_t = Affine(0.001, 0, -118.85, 0, -0.001, 34.10)
    yy, xx = np.mgrid[0:100, 0:100]
    src = (xx * 0.01 + yy * 0.001).astype("float32")
    out = rasters.reproject_array(src, "EPSG:4326", src_t, grid, "bilinear")
    assert out.shape == grid.shape and out.dtype == np.float32
    assert np.isfinite(out).all()
    path = tmp_path / "src.tif"
    with rasterio.open(path, "w", driver="GTiff", width=100, height=100, count=1, dtype="float32",
                       crs="EPSG:4326", transform=src_t, nodata=np.nan) as ds:
        ds.write(src, 1)
        ds.set_band_description(1, "gradient")
    from_file = rasters.reproject_to_grid(str(path), grid, band="gradient", resampling="bilinear")
    np.testing.assert_allclose(from_file, out, atol=1e-3)
    # categorical needs nodata and keeps dtype
    cat = (xx % 3).astype("uint8")
    out_cat = rasters.reproject_array(cat, "EPSG:4326", src_t, grid, "nearest", src_nodata=255)
    assert out_cat.dtype == np.uint8 and set(np.unique(out_cat)) <= {0, 1, 2}
    with pytest.raises(ValueError):
        rasters.reproject_array(cat, "EPSG:4326", src_t, grid, "nearest")


def test_reproject_array_is_thread_safe():
    # The cube builder warps from dask threads. rasterio's ndarray path (MemoryDataset) uses the process-wide,
    # non-thread-safe warnings.catch_warnings(): concurrent calls used to emit NotGeoreferencedWarning and could
    # leave an "ignore all warnings" filter behind. reproject_array must give the single-threaded result,
    # emit no warning and leave the warning filters untouched.
    import warnings
    from concurrent.futures import ThreadPoolExecutor

    dst = grid_from_bbox((-118.80, 34.05, -118.75, 34.09), "EPSG:32611", 30)
    rng = np.random.default_rng(7)
    cases = []
    for res, crs, how, dtype, nodata in ((10, "EPSG:32611", "average", "float32", None),
                                         (0.0004, "EPSG:4326", "bilinear", "float32", None),
                                         (20, "EPSG:3310", "mode", "int16", -9999),
                                         (60, "EPSG:32611", "nearest", "uint8", 255)):
        src = grid_from_bbox((-118.81, 34.04, -118.74, 34.10), crs, res)
        arr = (rng.random(src.shape) * 100).astype(dtype)
        cases.append((arr, src, how, nodata))
    expected = [rasters.reproject_array(a, g.crs, g.transform, dst, how, nd) for a, g, how, nd in cases]
    filters = list(warnings.filters)

    def one(i):
        a, g, how, nd = cases[i % len(cases)]
        return i % len(cases), rasters.reproject_array(a, g.crs, g.transform, dst, how, nd)

    with warnings.catch_warnings(record=True) as caught:       # entered once, in the main thread
        warnings.simplefilter("always")
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(one, range(160)))
    assert [str(w.message) for w in caught] == []
    assert list(warnings.filters) == filters
    for k, out in results:
        assert out.dtype == expected[k].dtype and np.array_equal(out, expected[k], equal_nan=True)


def test_mask_out_of_range_and_resampling_choice():
    a = np.array([[0.1, 0.9, np.nan], [0.5, -0.2, 0.3]], dtype="float32")
    out, frac = rasters.mask_out_of_range(a, (0.0, 0.7))
    assert np.isnan(out[0, 1]) and np.isnan(out[1, 1]) and out[1, 2] == pytest.approx(0.3)
    assert frac == pytest.approx(2 / 5)
    assert rasters.choose_resampling(False, 10, 30) == "average"
    assert rasters.choose_resampling(False, 200, 30) == "bilinear"
    assert rasters.choose_resampling(True, 10, 30) == "mode"
    assert rasters.choose_resampling(True, 30, 30) == "nearest"


def test_zarr_roundtrip_local(lake):
    ds = xr.Dataset({"t2m": (("time", "y", "x"), np.arange(24, dtype="float32").reshape(2, 3, 4))},
                    coords={"time": pd.date_range("2026-06-01", periods=2), "y": [3.0, 2.0, 1.0],
                            "x": [0.0, 1.0, 2.0, 3.0]})
    rasters.write_zarr(lake, "staged/era/tiny/i/data.zarr", ds)
    back = rasters.open_zarr(lake, "staged/era/tiny/i/data.zarr")
    assert back["t2m"].dims == ("time", "y", "x")
    np.testing.assert_array_equal(back["t2m"].values, ds["t2m"].values)


def test_aligned_sources_are_copied_not_resampled():
    from fuellake.grid import EASE2_200M_CELL, EASE2_UL_X, EASE2_UL_Y
    from fuellake.rasters import aligned_offset

    grid = grid_from_bbox((-118.80, 34.05, -118.70, 34.12), "EPSG:6933", EASE2_200M_CELL,
                          origin=(EASE2_UL_X, EASE2_UL_Y))
    rng = np.random.default_rng(1)
    src = rng.random((grid.height + 6, grid.width + 4)).astype("float32")
    t = grid.transform
    # a source starting 3 rows above and 2 columns left of the grid, its corner 3 cm off (as in real SME2 files)
    src_t = Affine(t.a, 0, t.c - 2 * t.a - 0.03, 0, t.e, t.f - 3 * t.e)
    assert aligned_offset("EPSG:6933", src_t, grid) == (-3, -2)
    out = rasters.reproject_array(src, "EPSG:6933", src_t, grid, resampling="bilinear")
    np.testing.assert_array_equal(out, src[3 : 3 + grid.height, 2 : 2 + grid.width])     # bit-identical
    codes = (src * 100).astype("int16")
    out_i = rasters.reproject_array(codes, "EPSG:6933", src_t, grid, resampling="nearest", src_nodata=-9999)
    np.testing.assert_array_equal(out_i, codes[3 : 3 + grid.height, 2 : 2 + grid.width])
    half = Affine(t.a, 0, t.c + t.a / 2, 0, t.e, t.f)
    assert aligned_offset("EPSG:6933", half, grid) is None                               # half a cell: warped
    assert aligned_offset("EPSG:3310", src_t, grid) is None
