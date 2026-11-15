# fuel-lake — tests for the label x feature matchup table builder on synthetic offline lakes
# Licensed under Apache-2.0.
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from fixtures.features.synthlake import GOLD_CONFIG_DIR, SynthLake, write_config
from fuellake import catalog, events
from fuellake.contracts import MixedMaturityError, validate_item
from fuellake.features._common import GOLD_CONFIGS, gold_dataset
from fuellake.features.matchups import build_matchups, matchups_path, pick_nearest
from fuellake.registry import DatasetConfig
from fuellake.tables import LFMC_COLUMNS, read_parquet

START, END = date(2026, 6, 1), date(2026, 6, 30)
D = date(2026, 6, 15)          # sample date used by most tests
SITE = (20, 30)                # AOI-grid pixel of the main site


@pytest.fixture
def synth(tmp_path, monkeypatch, lake) -> SynthLake:
    root = write_config(tmp_path / "config")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return SynthLake(lake)


def _site(sl: SynthLake, site_id: str = "A", rc=SITE, day: date = D, **extra) -> dict:
    lon, lat = sl.pixel_lonlat(*rc)
    return {"site_id": site_id, "lon": lon, "lat": lat, "date": day, **extra}


def _read(lake, aoi="tiny") -> pd.DataFrame:
    return read_parquet(lake, matchups_path(aoi))


def _sm(sl: SynthLake, day: date, value: float, maturity: str = "PROVISIONAL") -> dict:
    return sl.raster("nisar-sm", day, {"soil_moisture": sl.const(value)}, maturity=maturity)


def test_known_values_schema_and_item(synth, lake):
    sl = synth
    ndvi = sl.field(lambda r, c: (r * 100 + c) / 10000)
    db = sl.field(lambda r, c: np.where((r + c) % 2 == 0, -10.0, -20.0))
    aspect = sl.field(lambda r, c: np.where(c % 2 == 0, 359.0, 1.0))
    fm = sl.const(145, "int16")
    fm[SITE[0], SITE[1] + 1] = 102                     # neighbour: categorical uses the centre pixel only
    fm[40, 40] = -9999                                  # nodata at site B
    sl.raster("s2-test", D - timedelta(days=2), {"ndvi": ndvi, "ndmi": sl.const(0.25)})
    sl.raster("nisar-bsc", D - timedelta(days=1), {"gamma0_hh_db": db})
    sl.raster("dem-test", date(2021, 1, 1), {"elevation": sl.field(lambda r, c: 100.0 + r + c),
                                             "slope": sl.const(12.5), "aspect": aspect})
    sl.raster("fuel-test", date(2022, 1, 1), {"fuel_model": fm})
    labels = sl.labels([_site(sl, "A", lfmc_pct=71.5, species="chamise", qc_flags="method_missing"),
                        _site(sl, "B", (40, 40), species="black sage", fuel_category="shrub", outlier_flag=True)])

    # point sampling: exact pixel values
    assert build_matchups("tiny", START, END, lake=lake, sample_window_px=1) == "features/tiny/matchups.parquet"
    df = _read(lake)
    assert list(df.columns[: len(LFMC_COLUMNS)]) == LFMC_COLUMNS
    a = df[df.site_id == "A"].iloc[0]
    b = df[df.site_id == "B"].iloc[0]
    assert (a.grid_row, a.grid_col) == SITE and (b.grid_row, b.grid_col) == (40, 40)
    assert a.ndvi == np.float32((20 * 100 + 30) / 10000) and a.ndvi_dt_days == -2
    assert a.ndmi == np.float32(0.25)
    assert a.gamma0_hh_db == -10.0 and a.gamma0_hh_db_dt_days == -1
    assert a.elevation == 150.0 and a.slope == 12.5 and a.aspect == 359.0
    assert a.fuel_model == 145 and pd.isna(b.fuel_model)
    assert str(df["fuel_model"].dtype) == "Int32" and str(df["ndvi_dt_days"].dtype) == "Int32"
    assert a.lfmc_pct == 71.5 and a.qc_flags == "method_missing" and a.date == D and a.species == "chamise"
    assert bool(b.outlier_flag) and b.species == "black sage"
    raw = read_parquet(lake, catalog.asset_rel(labels, "table"))
    assert sorted(df.sample_id) == sorted(raw.sample_id)

    # 3 x 3 window (default): mean of valid pixels; dB in linear power; aspect circular; categorical centre
    build_matchups("tiny", START, END, lake=lake)
    a = _read(lake).set_index("site_id").loc["A"]
    window = [(20 + dr) * 100 + (30 + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)]
    assert a.ndvi == pytest.approx(np.mean(np.float32(np.array(window) / 10000)), abs=1e-7)
    lin = np.mean([10 ** (-1.0 if (dr + dc) % 2 == 0 else -2.0) for dr in (-1, 0, 1) for dc in (-1, 0, 1)])
    assert a.gamma0_hh_db == pytest.approx(10 * np.log10(lin))
    assert min(a.aspect, 360 - a.aspect) < 1.0                  # not the arithmetic mean (~180)
    assert a.fuel_model == 145
    assert a.elevation == pytest.approx(150.0)

    # STAC item and events
    cfg = gold_dataset("matchups")
    (item,) = list(catalog.iter_items(lake, "matchups"))
    assert item["id"] == "matchups__tiny__20260601__20260630"
    props = item["properties"]
    assert props["fuellake:n_rows"] == 2 and props["fuellake:causal"] is True
    assert props["fuellake:window_days"] == 6 and props["fuellake:lags_days"] == [0, 15, 30, 45, 60, 90]
    assert props["fuellake:sample_window_px"] == 3 and props["fuellake:nearest_valid"] is True
    assert labels["id"] in props["fuellake:source_ids"] and labels["id"] in props["fuellake:label_items"]
    assert props["fuellake:columns"]["ndvi_dt_days"]["column"] == "dt_days"
    results = validate_item(lake, item, cfg)
    assert all(r.ok for r in results), [r.to_dict() for r in results]
    types = [e["type"] for e in events.read_events(lake) if e["collection"] == "matchups"]
    assert types == ["item.created", "table.updated", "item.updated", "table.updated"]


def test_window_and_causal_rules(synth, lake):
    sl = synth
    _sm(sl, D - timedelta(days=4), 0.11)
    _sm(sl, D + timedelta(days=2), 0.12)
    _sm(sl, D + timedelta(days=8), 0.13)
    sl.raster("s2-test", D - timedelta(days=3), {"ndvi": sl.const(0.3), "ndmi": sl.const(0.3)})
    sl.raster("s2-test", D + timedelta(days=3), {"ndvi": sl.const(0.7), "ndmi": sl.const(0.7)})
    sl.labels([_site(sl), _site(sl, "S", day=D + timedelta(days=2))])

    def row(**kw):
        build_matchups("tiny", START, END, lake=lake, **kw)
        return _read(lake).set_index("site_id")

    r = row()                                       # causal, window 6
    assert (r.loc["A", "soil_moisture"], r.loc["A", "soil_moisture_dt_days"]) == (np.float32(0.11), -4)
    # same-day scene is allowed under the causal rule
    assert (r.loc["S", "soil_moisture"], r.loc["S", "soil_moisture_dt_days"]) == (np.float32(0.12), 0)
    assert r.loc["A", "ndvi"] == np.float32(0.3)
    r = row(causal=False)
    assert (r.loc["A", "soil_moisture"], r.loc["A", "soil_moisture_dt_days"]) == (np.float32(0.12), 2)
    assert (r.loc["A", "ndvi"], r.loc["A", "ndvi_dt_days"]) == (np.float32(0.3), -3)   # tie -> earlier scene
    r = row(causal=False, window_days=1)
    assert np.isnan(r.loc["A", "soil_moisture"]) and pd.isna(r.loc["A", "soil_moisture_dt_days"])
    r = row(window_days=3)
    assert np.isnan(r.loc["A", "soil_moisture"]) and pd.isna(r.loc["A", "soil_moisture_dt_days"])
    props = catalog.search(lake, ["matchups"])[0]["properties"]
    assert props["fuellake:causal"] is True and props["fuellake:window_days"] == 3


def test_nearest_valid_option(synth, lake):
    sl = synth
    cloudy = sl.const(0.9)
    cloudy[SITE[0] - 2 : SITE[0] + 3, SITE[1] - 2 : SITE[1] + 3] = np.nan     # masked around the site
    sl.raster("s2-test", D - timedelta(days=1), {"ndvi": cloudy, "ndmi": cloudy})
    sl.raster("s2-test", D - timedelta(days=5), {"ndvi": sl.const(0.4), "ndmi": sl.const(0.4)})
    sl.labels([_site(sl)])
    build_matchups("tiny", START, END, lake=lake)
    a = _read(lake).iloc[0]
    assert (a.ndvi, a.ndvi_dt_days) == (np.float32(0.4), -5)
    build_matchups("tiny", START, END, lake=lake, nearest_valid=False)
    a = _read(lake).iloc[0]
    assert np.isnan(a.ndvi) and a.ndvi_dt_days == -1
    assert catalog.search(lake, ["matchups"])[0]["properties"]["fuellake:nearest_valid"] is False


def test_lag_features_pick_the_right_scene(synth, lake):
    sl = synth
    first = date(2026, 3, 1)
    scene_days = [first + timedelta(days=4 * k) for k in range(31)]      # 2026-03-01 .. 2026-06-30
    for d in scene_days:
        _sm(sl, d, (d - first).days / 1000)                              # value encodes the scene date
    sl.labels([_site(sl)])
    build_matchups("tiny", START, END, lake=lake)
    a = _read(lake).iloc[0]
    for lag in (0, 15, 30, 45, 60, 90):
        target = D - timedelta(days=lag)
        cands = [s for s in scene_days if abs((s - target).days) <= 6 and s <= D]
        best = min(cands, key=lambda s: (abs((s - target).days), (s - target).days > 0))
        assert a[f"soil_moisture_lag{lag}d"] == pytest.approx((best - first).days / 1000), lag
        assert a[f"soil_moisture_lag{lag}d_dt_days"] == (best - target).days, lag
    assert a.soil_moisture == a.soil_moisture_lag0d
    # lags reach before `start`; the realised lag of lag 90 is 90 - dt
    assert scene_days[0] <= D - timedelta(days=90 + 6)
    props = catalog.search(lake, ["matchups"])[0]["properties"]
    assert props["fuellake:search_period"] == ["2026-02-25", "2026-06-30"]
    assert props["fuellake:lagged_variables"] == ["soil_moisture"]
    build_matchups("tiny", START, END, lake=lake, lags_days=(30,))
    cols = [c for c in _read(lake).columns if "_lag" in c]
    assert cols == ["soil_moisture_lag30d", "soil_moisture_lag30d_dt_days"]


def test_lag_variables_come_from_the_dataset_configs(synth, lake, tmp_path, monkeypatch):
    """``lag: true`` in a dataset config adds lag columns (SMAP L4 style); an explicit list replaces it."""
    from fuellake.features.matchups import DEFAULT_LAG_VARIABLES, config_lag_variables

    sl = synth
    cfg = tmp_path / "config" / "datasets" / "wx-test.yaml"
    d = yaml.safe_load(cfg.read_text())
    for v in d["variables"]:
        v["lag"] = v["name"] == "t2m"                  # flag t2m only; swvl1 stays a built-in default
    cfg.write_text(yaml.safe_dump(d))
    assert config_lag_variables() == (*DEFAULT_LAG_VARIABLES, "t2m")
    sl.weather("wx-test", date(2026, 5, 1), 31, {"t2m": lambda k, lon, lat: 250.0 + k + 0 * lon,
                                                 "swvl1": lambda k, lon, lat: 0.100 + k / 1000 + 0 * lon})
    sl.weather("wx-test", date(2026, 6, 1), 30, {"t2m": lambda k, lon, lat: 280.0 + k + 0 * lon,
                                                 "swvl1": lambda k, lon, lat: 0.200 + k / 1000 + 0 * lon})
    sl.labels([_site(sl)])
    build_matchups("tiny", START, END, lake=lake, lags_days=(0, 30))
    a = _read(lake).iloc[0]
    assert a.t2m_lag30d == pytest.approx(265.0, abs=1e-4)          # 2026-05-16 = May day index 15
    assert a.swvl1_lag30d == pytest.approx(0.115, abs=1e-6)        # the default is kept
    props = catalog.search(lake, ["matchups"])[0]["properties"]
    assert props["fuellake:lag_variables"] == ["soil_moisture", "swvl1", "t2m"]
    assert set(props["fuellake:lagged_variables"]) == {"t2m", "swvl1"}
    build_matchups("tiny", START, END, lake=lake, lags_days=(30,), lag_variables=("t2m",))
    cols = [c for c in _read(lake).columns if "_lag" in c]
    assert cols == ["t2m_lag30d"]
    # the CLI option replaces the list ("none" switches lags off)
    runner = CliRunner()
    from fuellake.cli import app

    for opt, expected in (("swvl1", ["swvl1_lag30d"]), ("none", [])):
        res = runner.invoke(app, ["build-features", "--what", "matchups", "--aoi", "tiny", "--start", "2026-06-01",
                                  "--end", "2026-06-30", "--lags-days", "30", "--lag-variables", opt])
        assert res.exit_code == 0, res.output
        assert [c for c in _read(lake).columns if "_lag" in c] == expected


def test_weather_on_sample_date_and_lags(synth, lake):
    sl = synth
    sl.weather("wx-test", date(2026, 5, 1), 31, {"t2m": lambda k, lon, lat: 250.0 + k + 0 * lon,
                                                 "swvl1": lambda k, lon, lat: 0.100 + k / 1000 + 0 * lon})
    sl.weather("wx-test", date(2026, 6, 1), 30, {"t2m": lambda k, lon, lat: 280.0 + k + 0 * lon,
                                                 "swvl1": lambda k, lon, lat: 0.200 + k / 1000 + 0 * lon},
               skip_days=(4,))
    sl.labels([_site(sl), _site(sl, "Z", day=date(2026, 6, 5))])
    build_matchups("tiny", START, END, lake=lake)
    df = _read(lake).set_index("site_id")
    a = df.loc["A"]
    assert a.t2m == pytest.approx(294.0, abs=1e-4)                  # 2026-06-15 = June day index 14
    assert a.swvl1 == pytest.approx(0.214, abs=1e-6)
    assert a.swvl1_lag15d == pytest.approx(0.130, abs=1e-6)         # 2026-05-31 = May day index 30
    assert a.swvl1_lag30d == pytest.approx(0.115, abs=1e-6)         # 2026-05-16 = May day index 15
    assert a.swvl1_lag45d == pytest.approx(0.100, abs=1e-6)         # 2026-05-01
    assert np.isnan(a.swvl1_lag60d) and np.isnan(a.swvl1_lag90d)    # April and March not staged
    assert np.isnan(df.loc["Z", "t2m"])                              # 2026-06-05 not staged
    assert "t2m_lag30d" not in df.columns and "swvl1_lag30d_dt_days" not in df.columns
    assert "soil_moisture" not in df.columns
    props = catalog.search(lake, ["matchups"])[0]["properties"]
    assert set(props["fuellake:missing_datasets"]) == {"nisar-sm", "nisar-bsc", "s2-test", "dem-test", "fuel-test",
                                                       "biomass"}
    assert any("soil_moisture" in n for n in props["fuellake:notes"])


def test_label_selection_bbox_period_and_duplicates(synth, lake):
    sl = synth
    w, s, e, n = sl.aoi.bbox
    lon, lat = sl.pixel_lonlat(*SITE)
    rows = [
        _site(sl, "IN"),
        {"site_id": "EDGE", "lon": w, "lat": lat, "date": D},                    # on the bbox edge: kept
        {"site_id": "OUT", "lon": e + 0.01, "lat": lat, "date": D},               # outside the bbox
        _site(sl, "EARLY", day=START - timedelta(days=1)),                         # before the period
        _site(sl, "LAST", day=END),                                                # on the period end: kept
    ]
    sl.labels(rows, key="p1")
    dup = _site(sl, "IN", lfmc_pct=99.0, ingested_at=pd.Timestamp("2026-11-15", tz="UTC"))
    sl.labels([dup], key="p2")                                                     # same sample_id, newer
    build_matchups("tiny", START, END, lake=lake)
    df = _read(lake)
    assert sorted(df.site_id) == ["EDGE", "IN", "LAST"]
    assert df.set_index("site_id").loc["IN", "lfmc_pct"] == 99.0
    assert catalog.search(lake, ["matchups"])[0]["properties"]["fuellake:buffer_m"] == 0


def test_mixed_maturity_refused(synth, lake):
    sl = synth
    _sm(sl, D - timedelta(days=2), 0.2, "PROVISIONAL")
    _sm(sl, D - timedelta(days=40), 0.3, "BETA")       # inside the lag search period
    sl.labels([_site(sl)])
    with pytest.raises(MixedMaturityError):
        build_matchups("tiny", START, END, lake=lake)
    assert not lake.exists(matchups_path("tiny"))
    build_matchups("tiny", START, END, lake=lake, maturity="PROVISIONAL")
    a = _read(lake).iloc[0]
    assert a.soil_moisture == np.float32(0.2) and np.isnan(a.soil_moisture_lag45d)
    build_matchups("tiny", START, END, lake=lake, allow_mixed_maturity=True)
    assert _read(lake).iloc[0].soil_moisture_lag45d == pytest.approx(0.3)


def test_thin_inputs(synth, lake):
    sl = synth
    # labels only: label + grid columns, every raster dataset recorded as missing
    sl.labels([_site(sl)])
    build_matchups("tiny", START, END, lake=lake)
    df = _read(lake)
    assert list(df.columns) == LFMC_COLUMNS + ["grid_x", "grid_y", "grid_row", "grid_col"] and len(df) == 1
    item = catalog.search(lake, ["matchups"])[0]
    assert len(item["properties"]["fuellake:missing_datasets"]) == 7
    assert all(r.ok for r in validate_item(lake, item, gold_dataset("matchups")))


def test_no_label_rows_gives_an_empty_typed_table(synth, lake):
    sl = synth
    _sm(sl, D, 0.2)
    build_matchups("tiny", START, END, lake=lake)
    df = _read(lake)
    assert len(df) == 0 and {"soil_moisture", "soil_moisture_dt_days", "soil_moisture_lag90d"} <= set(df.columns)
    item = catalog.search(lake, ["matchups"])[0]
    assert item["properties"]["fuellake:n_rows"] == 0
    assert all(r.ok for r in validate_item(lake, item, gold_dataset("matchups")))


def test_parameter_validation(synth, lake):
    for kw in ({"sample_window_px": 2}, {"sample_window_px": 0}, {"window_days": -1}, {"lags_days": (-5,)}):
        with pytest.raises(ValueError):
            build_matchups("tiny", START, END, lake=lake, **kw)


def test_pick_nearest_unit():
    values = np.array([[1.0, 2.0, np.nan, 4.0]])
    days = np.array([10, 13, 15, 17])
    v, dt = pick_nearest(values, days, np.array([15]), np.array([15]), 6, causal=True, nearest_valid=True)
    assert (v[0], dt[0]) == (2.0, -2)
    v, dt = pick_nearest(values, days, np.array([15]), np.array([15]), 6, causal=True, nearest_valid=False)
    assert np.isnan(v[0]) and dt[0] == 0
    v, dt = pick_nearest(values, days, np.array([15]), np.array([15]), 2, causal=False, nearest_valid=True)
    assert (v[0], dt[0]) == (2.0, -2)          # tie between 13 and 17 -> earlier
    v, dt = pick_nearest(values[:, :0], days[:0], np.array([15]), np.array([15]), 6, True, True)
    assert np.isnan(v[0]) and np.isnan(dt[0])


def test_gold_matchups_config_matches_builtin():
    with open(GOLD_CONFIG_DIR / "matchups.yaml") as f:
        from_yaml = DatasetConfig(**yaml.safe_load(f))
    assert from_yaml.model_dump() == DatasetConfig(**GOLD_CONFIGS["matchups"]).model_dump()


def test_cli_build_features_end_to_end(synth, lake):
    from fuellake.cli import app
    from fuellake.rasters import open_zarr
    from fuellake.storage import Lake

    sl = synth
    assert Lake.from_env().root == lake.root          # the CLI writes into the temporary lake
    sl.raster("s2-test", D - timedelta(days=1), {"ndvi": sl.const(0.4), "ndmi": sl.const(0.1)})
    _sm(sl, D - timedelta(days=3), 0.2)
    sl.raster("dem-test", date(2021, 1, 1), {"elevation": sl.const(10.0), "slope": sl.const(1.0),
                                             "aspect": sl.const(5.0)})
    sl.labels([_site(sl)])
    res = CliRunner().invoke(app, ["build-features", "--aoi", "tiny", "--start", "2026-06-01", "--end", "2026-06-30"])
    assert res.exit_code == 0, res.output
    assert res.output.split() == ["features/tiny/cube.zarr", "features/tiny/matchups.parquet"]
    assert set(open_zarr(lake, "features/tiny/cube.zarr").data_vars) == {"ndvi", "ndmi", "soil_moisture",
                                                                         "elevation", "slope", "aspect"}
    a = _read(lake).iloc[0]
    assert (a.ndvi, a.soil_moisture, a.elevation) == (np.float32(0.4), np.float32(0.2), 10.0)
    assert catalog.item_ids(lake, "features-cube") and catalog.item_ids(lake, "matchups")


def test_static_series_and_selection_are_recorded(synth, lake):
    sl = synth
    sl.raster("biomass", date(2026, 5, 1), {"biomass": sl.const(1.0)}, discriminator="live-1",
              properties={"fuellake:biomass_component": "live"})
    sl.raster("biomass", date(2026, 5, 1), {"biomass": sl.const(0.25)}, discriminator="dead-1",
              properties={"fuellake:biomass_component": "dead"})
    sl.raster("dem-test", date(2021, 1, 1), {"elevation": sl.const(10.0), "slope": sl.const(1.0),
                                             "aspect": sl.const(5.0)})
    sl.labels([_site(sl)])
    build_matchups("tiny", START, END, lake=lake)
    a = _read(lake).iloc[0]
    assert (a.biomass_live, a.biomass_dead) == (1.0, 0.25) and "biomass" not in _read(lake).columns
    static = catalog.search(lake, ["matchups"])[0]["properties"]["fuellake:static_items"]
    assert static["biomass_live"] == {"date": "2026-05-01", "items": ["biomass__tiny__20260501__live-1"]}
    assert static["elevation"]["date"] == "2021-01-01"
