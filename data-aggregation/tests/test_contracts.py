# fuel-lake — tests for contract validation (form A, form B, STAC properties incl. NISAR)
# Licensed under Apache-2.0.
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import rasterio
import xarray as xr

from fuellake import contracts, rasters
from fuellake.grid import Grid, grid_from_bbox
from fuellake.registry import DatasetConfig, Variable

GRID = grid_from_bbox((-118.80, 34.05, -118.78, 34.07), "EPSG:32611", 30)
SM = Variable(name="soil_moisture", unit="m3/m3", dtype="float32", nodata="NaN", valid_range=(0.0, 0.7))
NISAR = DatasetConfig(id="nisar-gcov", title="GCOV", kind="raster-a", license={"id": "other"},
                      maturity="PROVISIONAL",
                      variables=[{"name": "gamma0_hh_db", "unit": "dB", "dtype": "float32", "nodata": "NaN"}])


def _good(lake, rel="ok.tif", grid=GRID, value=0.3):
    arr = np.full(grid.shape, value, dtype="float32")
    rasters.write_cog(lake, rel, arr, grid, np.nan, "float32")
    return rel


def test_good_cog_passes(lake):
    res = contracts.validate_raster_a(lake, _good(lake), GRID, SM)
    assert res.ok, res.errors
    assert res.details["valid_fraction"] == 1.0


def test_wrong_crs_shape_range_dtype_layout(lake):
    wrong_crs = Grid("EPSG:32610", GRID.transform, GRID.width, GRID.height, 30)
    res = contracts.validate_raster_a(lake, _good(lake, "a.tif", wrong_crs), GRID, SM)
    assert any("grid mismatch" in e for e in res.errors)
    wrong_shape = Grid(GRID.crs, GRID.transform, GRID.width - 1, GRID.height, 30)
    res = contracts.validate_raster_a(lake, _good(lake, "b.tif", wrong_shape), GRID, SM)
    assert any("grid mismatch" in e for e in res.errors)
    res = contracts.validate_raster_a(lake, _good(lake, "c.tif", value=0.95), GRID, SM)
    assert any("outside valid_range" in e for e in res.errors)
    rasters.write_cog(lake, "d.tif", np.zeros(GRID.shape), GRID, np.nan, "float64")
    assert any("float32" in e for e in contracts.validate_raster_a(lake, "d.tif", GRID, SM).errors)
    with rasterio.open(lake.url("e.tif"), "w", driver="GTiff", width=GRID.width, height=GRID.height, count=1,
                       dtype="float32", crs=GRID.crs, transform=GRID.transform, nodata=np.nan) as ds:
        ds.write(np.full(GRID.shape, 0.2, "float32"), 1)
    assert any("Cloud-Optimized" in e for e in contracts.validate_raster_a(lake, "e.tif", GRID, SM).errors)
    assert contracts.validate_raster_a(lake, "missing.tif", GRID, SM).errors == ["file does not exist"]


def _nisar_item(**props):
    base = {
        "datetime": "2026-06-15T02:40:00Z", "fuellake:aoi": "smm", "fuellake:dataset": "nisar-gcov",
        "fuellake:license": "other", "fuellake:redistributable": False, "fuellake:status": "search-only",
        "fuellake:source_ids": ["NISAR_L2_PR_GCOV_X"], "fuellake:run_id": "r", "fuellake:pipeline_version": "0.1.0",
        "fuellake:qc_masked_fraction": 0.0,
    }
    base.update(props)
    return {
        "id": "nisar-gcov__smm__20260615__t013d071", "collection": "nisar-gcov", "properties": base,
        "assets": {"gamma0_hh_db": {"href": "../../../staged/x.tif", "type": rasters.COG_MEDIA_TYPE,
                                    "roles": ["data"], **GRID.stac_proj(),
                                    "raster:bands": [{"data_type": "float32", "nodata": "nan", "unit": "dB"}]}},
    }


NISAR_PROPS = {"fuellake:maturity": "PROVISIONAL", "sat:relative_orbit": 13, "fuellake:frame": 71,
               "sat:orbit_state": "descending", "fuellake:processing_id": "P05023",
               "sar:polarizations": ["HH", "HV"], "sar:frequency_band": "L"}


def test_nisar_properties_required():
    res = contracts.validate_item_properties(_nisar_item(), NISAR)
    missing = {e.rsplit(" ", 1)[-1] for e in res.errors if e.startswith("missing NISAR property")}
    assert missing == set(contracts.NISAR_PROPERTIES)
    assert contracts.validate_item_properties(_nisar_item(**NISAR_PROPS), NISAR).ok
    bad = contracts.validate_item_properties(_nisar_item(**{**NISAR_PROPS, "fuellake:maturity": "GOLD",
                                                            "sar:frequency_band": "C"}), NISAR)
    assert len(bad.errors) == 2


def test_required_properties_and_asset_metadata():
    item = _nisar_item(**NISAR_PROPS)
    del item["properties"]["fuellake:run_id"]
    item["assets"]["gamma0_hh_db"].pop("proj:epsg")
    item["assets"]["gamma0_hh_db"]["href"] = "/abs/path.tif"
    errors = contracts.validate_item_properties(item, NISAR).errors
    assert "missing property fuellake:run_id" in errors
    assert "asset gamma0_hh_db: missing proj:epsg" in errors
    assert any("href must be relative" in e for e in errors)
    item["id"] = "bad id"
    assert any("does not match" in e for e in contracts.validate_item_properties(item, NISAR).errors)


def test_check_single_maturity():
    items = [{"properties": {"fuellake:maturity": m}} for m in ("PROVISIONAL", "VALIDATED")]
    with pytest.raises(contracts.MixedMaturityError):
        contracts.check_single_maturity(items)
    assert contracts.check_single_maturity(items, allow_mixed=True) == {"PROVISIONAL", "VALIDATED"}


def test_validate_raster_b(lake):
    good = xr.Dataset({"t2m": (("time", "y", "x"), np.ones((2, 3, 4), "float32"))},
                      coords={"time": pd.date_range("2026-06-01", periods=2), "y": [3.0, 2.0, 1.0],
                              "x": [0.0, 1.0, 2.0, 3.0]})
    rasters.write_zarr(lake, "good.zarr", good)
    t2m = Variable(name="t2m", unit="K")
    res = contracts.validate_raster_b(lake, "good.zarr", [t2m])
    assert res.ok, res.errors
    assert any("CRS" in w for w in res.warnings)
    import rioxarray  # noqa: F401

    from fuellake.registry import AOI

    aoi = AOI(id="t", name="t", bbox=(0.5, 1.2, 2.5, 2.8), crs="EPSG:32631", resolution=30)
    rasters.write_zarr(lake, "crs.zarr", good.rio.write_crs("EPSG:4326"))
    back = rasters.open_zarr(lake, "crs.zarr")
    assert "spatial_ref" in back.coords and str(back.rio.crs) == "EPSG:4326"
    res = contracts.validate_raster_b(lake, "crs.zarr", [t2m], aoi)
    assert res.ok and not res.warnings, res.to_dict()
    far = AOI(id="f", name="f", bbox=(10.0, 10.0, 11.0, 11.0), crs="EPSG:32631", resolution=30)
    assert any("cover" in w for w in contracts.validate_raster_b(lake, "crs.zarr", [t2m], far).warnings)
    bad = good.rename({"y": "lat", "x": "lon"})
    rasters.write_zarr(lake, "bad.zarr", bad)
    res = contracts.validate_raster_b(lake, "bad.zarr", [t2m, Variable(name="tp")])
    assert any("expected ('time', 'y', 'x')" in e for e in res.errors)
    assert "declared variable tp missing" in res.errors


def test_categorical_bands_need_no_unit():
    fuel = DatasetConfig(id="landfire-x", title="fuel", kind="raster-a", license={"id": "CC0-1.0"},
                         variables=[{"name": "fuel_model", "dtype": "int16", "nodata": -9999}])
    item = _nisar_item(**{"fuellake:dataset": "landfire-x"})
    item.update(id="landfire-x__smm__20250101", collection="landfire-x")
    item["assets"] = {"fuel_model": {**item["assets"]["gamma0_hh_db"],
                                     "raster:bands": [{"data_type": "int16", "nodata": -9999}]}}
    res = contracts.validate_item_properties(item, fuel)
    assert res.ok and not res.warnings, res.to_dict()
    item["assets"]["fuel_model"]["raster:bands"] = [{"data_type": "float32", "nodata": "nan"}]
    assert any("missing unit" in w for w in contracts.validate_item_properties(item, fuel).warnings)


def _cube(grid, n_time=2, static_dims=("y", "x"), dtype="float32"):
    import rioxarray  # noqa: F401

    t = pd.date_range("2026-06-01", periods=n_time)
    ds = xr.Dataset(
        {"ndvi": (("time", "y", "x"), np.ones((n_time, *grid.shape), dtype)),
         "elevation": (static_dims, np.ones(grid.shape, "float32"))},
        coords={"time": t, "y": grid.y_coords(), "x": grid.x_coords()},
    )
    return ds.rio.write_crs(grid.crs).rio.write_transform(grid.transform)


def test_validate_cube_accepts_static_and_empty_time(lake):
    rasters.write_zarr(lake, "cube.zarr", _cube(GRID))
    item = {"properties": {"fuellake:variables": {"ndvi": {"dims": ["time", "y", "x"]},
                                                  "elevation": {"dims": ["y", "x"]}},
                           "fuellake:dims": {"time": 2, "y": GRID.height, "x": GRID.width}}}
    res = contracts.validate_cube(lake, "cube.zarr", GRID, item)
    assert res.ok and not res.warnings, res.to_dict()
    static_only = _cube(GRID, n_time=0).drop_vars("ndvi")
    rasters.write_zarr(lake, "static.zarr", static_only)
    res = contracts.validate_cube(lake, "static.zarr", GRID)
    assert res.ok and not res.warnings, res.to_dict()
    # the generic form B rule rejects the same store: that is why the cube has its own validator
    assert not contracts.validate_raster_b(lake, "cube.zarr").ok


def test_validate_cube_errors(lake):
    rasters.write_zarr(lake, "f64.zarr", _cube(GRID, dtype="float64"))
    assert any("float32" in e for e in contracts.validate_cube(lake, "f64.zarr", GRID).errors)
    coarse = grid_from_bbox((-118.80, 34.05, -118.78, 34.07), "EPSG:32611", 90)
    assert any("AOI grid" in e for e in contracts.validate_cube(lake, "f64.zarr", coarse).errors)
    rasters.write_zarr(lake, "ok.zarr", _cube(GRID))
    item = {"properties": {"fuellake:variables": {"ndvi": {"dims": ["time", "y", "x"]}}}}
    assert any("fuellake:variables" in e for e in contracts.validate_cube(lake, "ok.zarr", GRID, item).errors)
    assert contracts.validate_cube(lake, "missing.zarr", GRID).errors == ["store does not exist"]
