# fuel-lake — tests for the gold datacube builder on synthetic offline lakes
# Licensed under Apache-2.0.
from __future__ import annotations

from datetime import date

import numpy as np
import pytest
import yaml

from fixtures.features.synthlake import GOLD_CONFIG_DIR, SynthLake, at, write_config
from fuellake import catalog, events
from fuellake.contracts import MixedMaturityError, validate_item, validate_item_properties
from fuellake.features._common import GOLD_CONFIGS, gold_dataset
from fuellake.features.cube import build_cube, cube_path
from fuellake.rasters import open_zarr
from fuellake.registry import DatasetConfig

START, END = date(2026, 6, 1), date(2026, 6, 30)


@pytest.fixture
def synth(tmp_path, monkeypatch, lake) -> SynthLake:
    root = write_config(tmp_path / "config")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return SynthLake(lake)


def _ndvi(sl: SynthLake) -> np.ndarray:
    return sl.field(lambda r, c: (r * 100 + c) / 10000)   # unique per pixel


def _fuel(sl: SynthLake) -> np.ndarray:
    fm = sl.const(145, "int16")
    fm[0, 0] = -9999
    fm[5, 5] = 102
    return fm


def _standard_lake(sl: SynthLake) -> list[dict]:
    items = [
        sl.raster("s2-test", date(2026, 6, 10), {"ndvi": _ndvi(sl), "ndmi": sl.const(0.2)}),
        sl.raster("s2-test", date(2026, 6, 20), {"ndvi": sl.const(0.5), "ndmi": sl.const(0.3)}),
        sl.raster("nisar-sm", date(2026, 6, 12), {"soil_moisture": sl.const(0.25)}, maturity="PROVISIONAL"),
        sl.raster("nisar-sm", date(2026, 6, 20), {"soil_moisture": sl.field(lambda r, c: 0.1 + 0.001 * c)},
                  maturity="PROVISIONAL"),
        sl.raster("dem-test", date(2021, 1, 1), {"elevation": sl.field(lambda r, c: 100.0 + r + c),
                                                 "slope": sl.const(10.0), "aspect": sl.const(350.0)}),
        sl.raster("fuel-test", date(2022, 1, 1), {"fuel_model": _fuel(sl)}),
    ]
    sl.raster("s2-test", date(2026, 7, 5), {"ndvi": sl.const(0.9), "ndmi": sl.const(0.9)})   # outside period
    return items


def test_cube_layout_known_values_and_item(synth, lake):
    sl = synth
    inputs = _standard_lake(sl)
    assert build_cube("tiny", START, END, lake=lake) == cube_path("tiny") == "features/tiny/cube.zarr"
    ds = open_zarr(lake, cube_path("tiny"))
    grid = sl.grid

    assert dict(ds.sizes) == {"time": 3, "y": grid.height, "x": grid.width}
    assert [str(t)[:10] for t in ds["time"].values] == ["2026-06-10", "2026-06-12", "2026-06-20"]
    np.testing.assert_array_equal(ds["x"].values, grid.x_coords())
    np.testing.assert_array_equal(ds["y"].values, grid.y_coords())
    assert ds.rio.crs.to_epsg() == 32611
    assert list(ds.rio.transform())[:6] == list(grid.transform)[:6]

    dynamic = {"ndvi", "ndmi", "soil_moisture"}
    static = {"elevation", "slope", "aspect", "fuel_model"}
    assert set(ds.data_vars) == dynamic | static
    for name in dynamic:
        assert ds[name].dims == ("time", "y", "x") and ds[name].dtype == np.float32
        assert ds[name].encoding["chunks"] == (3, grid.height, grid.width)
        assert type(ds[name].encoding["compressors"][0]).__name__ == "BloscCodec"
    for name in static:
        assert ds[name].dims == ("y", "x") and ds[name].dtype == np.float32

    # known values at known pixels and dates come back exactly; NaN where a variable has no scene that day
    ndvi = ds["ndvi"].values
    np.testing.assert_array_equal(ndvi[0], _ndvi(sl))
    assert np.isnan(ndvi[1]).all() and (ndvi[2] == np.float32(0.5)).all()
    sm = ds["soil_moisture"].values
    assert np.isnan(sm[0]).all() and (sm[1] == np.float32(0.25)).all()
    np.testing.assert_array_equal(sm[2], sl.field(lambda r, c: 0.1 + 0.001 * c))
    np.testing.assert_array_equal(ds["elevation"].values, sl.field(lambda r, c: 100.0 + r + c))
    fm = ds["fuel_model"].values
    assert np.isnan(fm[0, 0]) and fm[5, 5] == 102 and fm[1, 1] == 145

    # CF-style attributes and provenance
    assert ds["soil_moisture"].attrs["units"] == "m3/m3"
    assert ds["soil_moisture"].attrs["fuellake:maturity"] == "PROVISIONAL"
    assert ds["ndvi"].attrs["fuellake:items_by_date"] == {"2026-06-10": ["s2-test__tiny__20260610"],
                                                          "2026-06-20": ["s2-test__tiny__20260620"]}
    assert ds["ndvi"].attrs["fuellake:role"] == "dynamic" and ds["elevation"].attrs["fuellake:role"] == "static"
    assert ds["fuel_model"].attrs["fuellake:categorical"] is True
    assert ds["aspect"].attrs["fuellake:aggregation"] == "circular"
    assert ds["elevation"].attrs["fuellake:source_items"] == ["dem-test__tiny__20210101"]
    assert ds.attrs["Conventions"] == "CF-1.8" and ds.attrs["fuellake:aoi"] == "tiny"
    assert set(ds.attrs["fuellake:missing_datasets"]) == {"nisar-bsc", "wx-test", "biomass"}

    # STAC item
    cfg = gold_dataset("features-cube")
    (item,) = list(catalog.iter_items(lake, "features-cube"))
    assert item["id"] == "features-cube__tiny__20260601__20260630"
    props = item["properties"]
    assert props["fuellake:source_ids"] == sorted(i["id"] for i in inputs)
    assert props["fuellake:maturity"] == "PROVISIONAL"
    assert props["fuellake:license"] == "CC0-1.0" and props["fuellake:redistributable"] is True
    assert props["fuellake:dims"] == {"time": 3, "y": grid.height, "x": grid.width}
    assert props["fuellake:variables"]["elevation"]["dims"] == ["y", "x"]
    assert props["start_datetime"] == "2026-06-01T00:00:00Z" and props["end_datetime"].startswith("2026-06-30T23")
    assert catalog.asset_rel(item, "data") == cube_path("tiny")
    assert validate_item_properties(item, cfg).ok
    results = validate_item(lake, item, cfg)          # contracts.validate_cube: static (y, x) variables are fine
    assert all(r.ok for r in results) and results[1].check == "cube", [r.to_dict() for r in results]
    import pystac

    assert pystac.Item.from_dict(item).id == item["id"]
    assert [e["type"] for e in events.read_events(lake, types=["item.created", "item.updated"])
            if e["collection"] == "features-cube"] == ["item.created"]


def test_rebuild_is_idempotent_and_replaces_the_item(synth, lake):
    _standard_lake(synth)
    build_cube("tiny", START, END, lake=lake)
    first = open_zarr(lake, cube_path("tiny"))["ndvi"].values
    build_cube("tiny", START, END, lake=lake)
    np.testing.assert_array_equal(open_zarr(lake, cube_path("tiny"))["ndvi"].values, first)
    assert catalog.item_ids(lake, "features-cube") == ["features-cube__tiny__20260601__20260630"]
    cube_events = [e for e in events.read_events(lake) if e["collection"] == "features-cube"]
    assert [e["type"] for e in cube_events] == ["item.created", "item.updated"]
    # a different period overwrites the store and replaces the AOI's single item
    build_cube("tiny", START, date(2026, 6, 15), lake=lake)
    ds = open_zarr(lake, cube_path("tiny"))
    assert [str(t)[:10] for t in ds["time"].values] == ["2026-06-10", "2026-06-12"]
    assert catalog.item_ids(lake, "features-cube") == ["features-cube__tiny__20260601__20260615"]
    item = catalog.get_item(lake, "features-cube", "features-cube__tiny__20260601__20260615")
    assert item["properties"]["fuellake:replaced_items"] == ["features-cube__tiny__20260601__20260630"]


def test_weather_regridded_and_sampled_on_cube_dates(synth, lake):
    sl = synth
    for d in (date(2026, 6, 5), date(2026, 6, 8), date(2026, 6, 10)):
        sl.raster("s2-test", d, {"ndvi": sl.const(0.4), "ndmi": sl.const(0.1)})

    def gradient(k, lon, lat):
        return 0.2 + 0.5 * (lon + 118.8) + 0.3 * (lat - 34.05) + 0 * k

    # south-up store, day 2026-06-08 (k=7) not staged
    sl.weather("wx-test", date(2026, 6, 1), 30, {"t2m": lambda k, lon, lat: 280.0 + k + 0 * lon, "swvl1": gradient},
               skip_days=(7,), south_up=True)
    build_cube("tiny", START, END, lake=lake)
    ds = open_zarr(lake, cube_path("tiny"))
    assert [str(t)[:10] for t in ds["time"].values] == ["2026-06-05", "2026-06-08", "2026-06-10"]
    assert ds["t2m"].dims == ("time", "y", "x") and ds["t2m"].dtype == np.float32
    t2m = ds["t2m"].values
    np.testing.assert_allclose(t2m[0], 284.0, rtol=0, atol=1e-4)     # 2026-06-05 = day index 4
    assert np.isnan(t2m[1]).all()                                     # not staged that day
    np.testing.assert_allclose(t2m[2], 289.0, rtol=0, atol=1e-4)
    assert ds["t2m"].attrs["fuellake:dates_with_data"] == ["2026-06-05", "2026-06-10"]
    # bilinear regridding reproduces a linear field at the pixel centres
    sw = ds["swvl1"].values[0]
    for r, c in ((0, 0), (10, 20), (sl.grid.height - 1, sl.grid.width - 1)):
        lon, lat = sl.pixel_lonlat(r, c)
        assert sw[r, c] == pytest.approx(gradient(0, lon, lat), abs=2e-4)


def test_weather_axis_without_scenes(synth, lake):
    """A region AOI such as ca has no form A scene datasets: weather gets a daily (or N-day) axis of its own."""
    sl = synth
    sl.weather("wx-test", date(2026, 6, 1), 30, {"t2m": lambda k, lon, lat: 280.0 + k + 0 * lon,
                                                 "swvl1": lambda k, lon, lat: 0.2 + 0 * lon}, skip_days=(9,))
    build_cube("tiny", START, END, lake=lake)
    ds = open_zarr(lake, cube_path("tiny"))
    days = [str(t)[:10] for t in ds["time"].values]
    assert len(days) == 29 and days[0] == "2026-06-01" and "2026-06-10" not in days     # day 9 not staged
    assert ds.attrs["fuellake:time_axis"] == "weather" and ds["t2m"].dims == ("time", "y", "x")
    np.testing.assert_allclose(ds["t2m"].values[days.index("2026-06-15")], 294.0, atol=1e-4)
    (item,) = catalog.search(lake, ["features-cube"])
    assert item["properties"]["fuellake:time_axis"] == "weather" and item["properties"]["fuellake:dims"]["time"] == 29
    assert all(r.ok for r in validate_item(lake, item, gold_dataset("features-cube"))), "cube must validate"
    build_cube("tiny", START, END, lake=lake, weather_cadence_days=7)                    # 1, 8, 15, 22, 29 June
    days = [str(t)[:10] for t in open_zarr(lake, cube_path("tiny"))["time"].values]
    assert days == ["2026-06-01", "2026-06-08", "2026-06-15", "2026-06-22", "2026-06-29"]
    # as soon as a scene exists, the axis is the scene dates again
    sl.raster("s2-test", date(2026, 6, 12), {"ndvi": sl.const(0.4), "ndmi": sl.const(0.1)})
    build_cube("tiny", START, END, lake=lake)
    ds = open_zarr(lake, cube_path("tiny"))
    assert [str(t)[:10] for t in ds["time"].values] == ["2026-06-12"] and ds.attrs["fuellake:time_axis"] == "scenes"


def test_mixed_nisar_maturity_is_refused_unless_allowed(synth, lake):
    sl = synth
    sl.raster("nisar-sm", date(2026, 6, 10), {"soil_moisture": sl.const(0.2)}, maturity="PROVISIONAL")
    sl.raster("nisar-sm", date(2026, 6, 22), {"soil_moisture": sl.const(0.3)}, maturity="VALIDATED")
    with pytest.raises(MixedMaturityError):
        build_cube("tiny", START, END, lake=lake)
    assert not lake.exists(cube_path("tiny")) and catalog.item_ids(lake, "features-cube") == []

    build_cube("tiny", START, END, lake=lake, allow_mixed_maturity=True)
    ds = open_zarr(lake, cube_path("tiny"))
    assert ds["soil_moisture"].attrs["fuellake:maturity"] == "PROVISIONAL;VALIDATED"
    props = catalog.search(lake, ["features-cube"])[0]["properties"]
    assert props["fuellake:allow_mixed_maturity"] is True and "fuellake:maturity" not in props
    assert props["fuellake:maturity_by_dataset"] == {"nisar-sm": ["PROVISIONAL", "VALIDATED"]}

    build_cube("tiny", START, END, lake=lake, maturity="VALIDATED")
    ds = open_zarr(lake, cube_path("tiny"))
    assert [str(t)[:10] for t in ds["time"].values] == ["2026-06-22"]
    assert ds["soil_moisture"].attrs["fuellake:maturity"] == "VALIDATED"
    assert catalog.search(lake, ["features-cube"])[0]["properties"]["fuellake:maturity"] == "VALIDATED"


def test_cli_mixed_maturity_is_a_one_line_error(synth, lake):
    from typer.testing import CliRunner

    from fuellake.cli import app

    sl = synth
    sl.raster("nisar-sm", date(2026, 6, 10), {"soil_moisture": sl.const(0.2)}, maturity="PROVISIONAL")
    sl.raster("nisar-sm", date(2026, 6, 22), {"soil_moisture": sl.const(0.3)}, maturity="VALIDATED")
    args = ["build-features", "--what", "cube", "--aoi", "tiny", "--start", "2026-06-01", "--end", "2026-06-30"]
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 1 and "Traceback" not in res.output
    assert [ln for ln in res.output.splitlines() if ln.strip()] == [
        "error: time series mixes maturities ['PROVISIONAL', 'VALIDATED']; choose one with --maturity "
        "(e.g. PROVISIONAL) or pass --allow-mixed-maturity"]
    assert not lake.exists(cube_path("tiny"))
    res = CliRunner().invoke(app, [*args, "--maturity", "VALIDATED"])
    assert res.exit_code == 0 and res.output.strip() == cube_path("tiny")
    res = CliRunner().invoke(app, [*args, "--allow-mixed-maturity"])
    assert res.exit_code == 0, res.output


def test_missing_datasets_and_thin_inputs(synth, lake):
    sl = synth
    with pytest.raises(LookupError, match="no cube inputs"):
        build_cube("tiny", START, END, lake=lake)
    # static layers only: an empty time axis
    sl.raster("dem-test", date(2021, 1, 1), {"elevation": sl.const(50.0), "slope": sl.const(1.0),
                                             "aspect": sl.const(90.0)})
    build_cube("tiny", START, END, lake=lake)
    ds = open_zarr(lake, cube_path("tiny"))
    assert ds.sizes["time"] == 0 and set(ds.data_vars) == {"elevation", "slope", "aspect"}
    assert (ds["elevation"].values == 50.0).all() and ds.attrs["fuellake:time_axis"] == "none"
    # weather without any scene date: weather gets its own daily axis (see test_weather_axis_without_scenes)
    sl.weather("wx-test", date(2026, 6, 1), 30, {"t2m": lambda k, lon, lat: 280.0 + 0 * lon,
                                                 "swvl1": lambda k, lon, lat: 0.2 + 0 * lon})
    build_cube("tiny", START, END, lake=lake)
    ds = open_zarr(lake, cube_path("tiny"))
    assert ds.sizes["time"] == 30 and set(ds.data_vars) == {"elevation", "slope", "aspect", "t2m", "swvl1"}
    assert set(ds.attrs["fuellake:missing_datasets"]) == {"nisar-sm", "nisar-bsc", "s2-test", "fuel-test", "biomass"}
    assert any("weather on its own time axis" in n for n in ds.attrs["fuellake:notes"])
    item = catalog.search(lake, ["features-cube"])[0]
    assert set(item["properties"]["fuellake:missing_datasets"]) == set(ds.attrs["fuellake:missing_datasets"])
    # restricting the inputs; unknown dataset ids are an error
    sl.raster("s2-test", date(2026, 6, 10), {"ndvi": sl.const(0.4), "ndmi": sl.const(0.1)})
    build_cube("tiny", START, END, lake=lake, datasets=["s2-test"])
    assert set(open_zarr(lake, cube_path("tiny")).data_vars) == {"ndvi", "ndmi"}
    with pytest.raises(KeyError):
        build_cube("tiny", START, END, lake=lake, datasets=["no-such-dataset"])


def test_same_day_items_are_mosaicked_db_in_linear_power(synth, lake):
    sl = synth
    w = sl.grid.width
    left = sl.const(0.4)
    left[:, w // 2 + 1:] = np.nan
    right = sl.const(0.6)
    right[:, : w // 2] = np.nan
    day = date(2026, 6, 10)
    sl.raster("s2-test", at(day, 18, 30), {"ndvi": left, "ndmi": sl.const(0.1)}, discriminator="T1")
    sl.raster("s2-test", at(day, 18, 31), {"ndvi": right, "ndmi": sl.const(0.1)}, discriminator="T2")
    sl.raster("nisar-bsc", at(day, 14, 0), {"gamma0_hh_db": sl.const(-10.0)}, discriminator="F1")
    sl.raster("nisar-bsc", at(day, 14, 1), {"gamma0_hh_db": sl.const(-20.0)}, discriminator="F2")
    build_cube("tiny", START, END, lake=lake)
    ds = open_zarr(lake, cube_path("tiny"))
    assert ds.sizes["time"] == 1
    ndvi = ds["ndvi"].values[0]
    assert (ndvi[:, : w // 2] == np.float32(0.4)).all() and (ndvi[:, w // 2 + 1:] == np.float32(0.6)).all()
    assert ndvi[0, w // 2] == pytest.approx(0.5)
    expected_db = 10 * np.log10((10 ** -1 + 10 ** -2) / 2)        # not the dB mean (-15)
    np.testing.assert_allclose(ds["gamma0_hh_db"].values[0], expected_db, rtol=0, atol=1e-5)
    assert ds["gamma0_hh_db"].attrs["fuellake:aggregation"] == "db"
    assert len(ds["ndvi"].attrs["fuellake:items_by_date"]["2026-06-10"]) == 2


def test_coarser_resolution_by_averaging(synth, lake):
    sl = synth
    aspect = sl.field(lambda r, c: np.where(c % 2 == 0, 359.0, 1.0))   # arithmetic mean would be ~180
    sl.raster("dem-test", date(2021, 1, 1), {"elevation": sl.const(100.0), "slope": sl.const(5.0),
                                             "aspect": aspect})
    sl.raster("fuel-test", date(2022, 1, 1), {"fuel_model": sl.const(145, "int16")})
    sl.raster("s2-test", date(2026, 6, 10), {"ndvi": sl.const(0.4), "ndmi": sl.field(lambda r, c: c % 2 * 1.0)})
    sl.raster("nisar-bsc", date(2026, 6, 10), {"gamma0_hh_db": sl.const(-12.0)})
    build_cube("tiny", START, END, lake=lake, resolution=90)
    ds = open_zarr(lake, cube_path("tiny"))
    coarse = sl.aoi.grid_at(90)
    assert (ds.sizes["y"], ds.sizes["x"]) == coarse.shape and ds.attrs["fuellake:resolution"] == 90.0
    np.testing.assert_array_equal(ds["x"].values, coarse.x_coords())
    inner = (slice(1, -1), slice(1, -1))     # cells fully inside the 30 m grid
    np.testing.assert_allclose(ds["elevation"].values[inner], 100.0, atol=1e-4)
    np.testing.assert_allclose(ds["ndvi"].values[0][inner], 0.4, atol=1e-6)
    np.testing.assert_allclose(ds["ndmi"].values[0][inner], 0.5, atol=0.2)   # area-weighted mix of 0 and 1
    np.testing.assert_allclose(ds["gamma0_hh_db"].values[0][inner], -12.0, atol=1e-4)
    a = ds["aspect"].values[inner]
    assert (np.minimum(a, 360 - a) < 1.0).all()
    assert (ds["fuel_model"].values[inner] == 145).all()
    (item,) = list(catalog.iter_items(lake, "features-cube"))
    results = validate_item(lake, item, gold_dataset("features-cube"))     # checked against the 90 m grid
    assert all(r.ok for r in results), [r.to_dict() for r in results]
    item = catalog.search(lake, ["features-cube"])[0]
    assert item["properties"]["fuellake:resolution"] == 90.0
    assert item["assets"]["data"]["proj:shape"] == [coarse.height, coarse.width]
    with pytest.raises(ValueError, match="finer"):
        build_cube("tiny", START, END, lake=lake, resolution=10)


def test_static_item_selection(synth, lake):
    sl = synth
    dem = {"slope": sl.const(1.0), "aspect": sl.const(1.0)}
    sl.raster("dem-test", date(2027, 1, 1), {"elevation": sl.const(300.0), **dem})   # after the period
    build_cube("tiny", START, END, lake=lake)
    assert (open_zarr(lake, cube_path("tiny"))["elevation"].values == 300.0).all()   # only one: earliest
    sl.raster("dem-test", date(2020, 1, 1), {"elevation": sl.const(100.0), **dem})
    sl.raster("dem-test", date(2021, 1, 1), {"elevation": sl.const(200.0), **dem})
    build_cube("tiny", START, END, lake=lake)
    ds = open_zarr(lake, cube_path("tiny"))
    assert (ds["elevation"].values == 200.0).all()                                    # latest on or before end
    assert ds["elevation"].attrs["fuellake:source_items"] == ["dem-test__tiny__20210101"]


def test_gold_cube_config_matches_builtin(tmp_path, monkeypatch):
    with open(GOLD_CONFIG_DIR / "features-cube.yaml") as f:
        from_yaml = DatasetConfig(**yaml.safe_load(f))
    assert from_yaml.model_dump() == DatasetConfig(**GOLD_CONFIGS["features-cube"]).model_dump()
    root = write_config(tmp_path / "cfg", with_gold_yaml=True)
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    assert gold_dataset("features-cube").model_dump() == from_yaml.model_dump()


def test_biomass_components_are_separate_static_series(synth, lake):
    sl = synth
    live = {"fuellake:biomass_component": "live"}
    dead = {"fuellake:biomass_component": "dead"}
    sl.raster("biomass", date(2025, 8, 1), {"biomass": sl.const(3.0)}, discriminator="live-0.1", properties=live)
    sl.raster("biomass", date(2026, 5, 1), {"biomass": sl.const(1.0)}, discriminator="live-0.2", properties=live)
    sl.raster("biomass", date(2026, 5, 1), {"biomass": sl.const(0.2)}, discriminator="dead-0.2", properties=dead)
    build_cube("tiny", START, END, lake=lake)
    ds = open_zarr(lake, cube_path("tiny"))
    assert set(ds.data_vars) == {"biomass_live", "biomass_dead"}            # never mosaicked together
    assert (ds["biomass_live"].values == 1.0).all() and (ds["biomass_dead"].values == np.float32(0.2)).all()
    assert ds["biomass_live"].dims == ("y", "x")
    assert ds["biomass_live"].attrs["fuellake:series"] == "biomass:live"
    assert ds["biomass_live"].attrs["fuellake:source_items"] == ["biomass__tiny__20260501__live-0.2"]


def test_weather_directions_are_interpolated_circularly(synth, lake):
    sl = synth
    sl.raster("s2-test", date(2026, 6, 10), {"ndvi": sl.const(0.4), "ndmi": sl.const(0.1)})
    sl.weather("wx-test", date(2026, 6, 1), 30,
               {"th": lambda k, lon, lat: np.where(np.round((lon + 118.9) / 0.01) % 2 == 0, 359.0, 1.0)})
    build_cube("tiny", START, END, lake=lake)
    ds = open_zarr(lake, cube_path("tiny"))
    th = ds["th"].values[0]
    assert np.isfinite(th).all() and (np.minimum(th, 360 - th) < 1.0 + 1e-4).all()   # never ~180
    assert ds["th"].attrs["fuellake:aggregation"] == "circular"
