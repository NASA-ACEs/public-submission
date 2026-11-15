# fuel-lake — tests for the statewide point-feature table builder on synthetic offline lakes
# Licensed under Apache-2.0.
from __future__ import annotations

import json
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from fixtures.features.synthlake import SynthLake
from fixtures.points.synthpoints import ignitions, lf_release, s2_scene, write_config
from fuellake import catalog, events
from fuellake.contracts import validate_item
from fuellake.features import _point_sources as ps_mod
from fuellake.features._point_sources import (
    ArcGisImageSampler,
    PointCache,
    copdem_tile,
    parse_samples,
    terrain_from_window,
)
from fuellake.features._s2points import S2PointSampler, footprint_window
from fuellake.features.points import (
    BLOCK_SIZE_M,
    BUILTIN_CONFIG,
    block_ids,
    build_point_features,
    choose_release,
    point_config,
    points_path,
)
from fuellake.registry import DatasetConfig
from fuellake.tables import read_parquet

REPO_CONFIG = __import__("pathlib").Path(__file__).resolve().parents[1] / "config" / "datasets"
D = date(2026, 6, 15)
SITE = (20, 30)


@pytest.fixture
def lakes(tmp_path, monkeypatch, lake):
    root = write_config(tmp_path / "config")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return SynthLake(lake, "tiny"), SynthLake(lake, "wide")


def _read(lake, aoi="wide") -> pd.DataFrame:
    return read_parquet(lake, points_path(aoi))


def _item(lake) -> dict:
    (item,) = list(catalog.iter_items(lake, "point-features"))
    return item


def _build(lake, **kw):
    kw.setdefault("sentinel2", False)
    kw.setdefault("remote", False)
    kw.setdefault("cache", PointCache(lake.root + "/../point-cache.sqlite"))
    return build_point_features("wide", kw.pop("start", date(2016, 1, 1)), kw.pop("end", date(2026, 11, 14)),
                                lake=lake, **kw)


def _outside(wide: SynthLake) -> tuple[float, float]:
    """A point inside 'wide' but outside 'tiny'."""
    return wide.pixel_lonlat(10, 10)


# ---------------------------------------------------------------------------- rows and blocks
def test_rows_labels_dedupe_and_shared_point_dates(lakes, lake):
    tiny, wide = lakes
    lon, lat = tiny.pixel_lonlat(*SITE)
    olon, olat = _outside(wide)
    tiny.labels([{"site_id": "A", "lon": lon, "lat": lat, "date": D, "species": "chamise", "lfmc_pct": 70.0},
                 {"site_id": "A", "lon": lon, "lat": lat, "date": D, "species": "black sage", "lfmc_pct": 90.0},
                 {"site_id": "OLD", "lon": lon, "lat": lat, "date": date(2015, 6, 1)}])
    ignitions(tiny, [{"ignition_id": "i1", "lon": lon, "lat": lat, "discovery_date": date(2023, 6, 1),
                      "fire_size_ha": 500.0},
                     {"ignition_id": "i2", "lon": olon, "lat": olat, "discovery_date": date(2022, 6, 1),
                      "fire_size_ha": 0.1},
                     {"ignition_id": "i3", "lon": olon, "lat": olat, "discovery_date": date(2021, 6, 1),
                      "fire_size_ha": None},
                     {"ignition_id": "far", "lon": -120.5, "lat": 38.5, "discovery_date": date(2021, 6, 1),
                      "fire_size_ha": 1.0}], key="p1")
    ignitions(tiny, [{"ignition_id": "i2", "lon": olon, "lat": olat, "discovery_date": date(2022, 6, 1),
                      "fire_size_ha": 450.0, "ingested_at": pd.Timestamp("2026-11-15", tz="UTC")}], key="p2")
    assert _build(lake, statics=False) == "features/wide/point_features.parquet"
    df = _read(lake)
    assert list(df.columns[:6]) == ["point_type", "point_id", "date", "lon", "lat", "block_50km"]
    assert sorted(df.point_id[df.point_type == "fire_ignition"]) == ["i1", "i2", "i3"]   # bbox filter, dedupe
    assert (df.point_type == "lfmc_sample").sum() == 2                                    # period filter
    i = df.set_index("point_id")
    assert bool(i.loc["i1", "large_fire"]) and bool(i.loc["i2", "large_fire"])          # newest part wins
    assert pd.isna(i.loc["i3", "large_fire"]) and i.loc["i2", "fire_size_ha"] == 450.0
    assert df.loc[df.point_type == "lfmc_sample", "large_fire"].isna().all()
    assert i.loc["i1", "date"] == date(2023, 6, 1) and i.loc["i1", "discovery_date"] == date(2023, 6, 1)
    lf = df[df.point_type == "lfmc_sample"].set_index("species")
    assert lf.loc["chamise", "lfmc_pct"] == 70.0 and lf.loc["black sage", "site_id"] == "A"
    props = _item(lake)["properties"]
    assert props["fuellake:n_rows"] == 5 and props["fuellake:n_point_dates"] == 4    # two species share one
    assert props["fuellake:rows_by_table"] == {"fire_ignitions": 3, "lfmc_samples": 2}
    assert props["fuellake:missing_tables"] == []
    expected_ids, _, _ = block_ids(df.lon, df.lat)
    assert list(df.block_50km) == list(expected_ids)


def test_block_ids_are_ease2_50km_cells():
    from pyproj import Transformer

    from fuellake.grid import EASE2_UL_X, EASE2_UL_Y

    assert BLOCK_SIZE_M == pytest.approx(50044.751, abs=1e-3)
    lon, lat = np.array([-118.8, -118.8, -120.5, np.nan]), np.array([34.06, 34.0601, 38.5, 34.0])
    ids, rows, cols = block_ids(lon, lat)
    x, y = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True).transform(-120.5, 38.5)
    assert (rows[2], cols[2]) == (int((EASE2_UL_Y - y) // BLOCK_SIZE_M), int((x - EASE2_UL_X) // BLOCK_SIZE_M))
    assert ids[2] == f"r{rows[2]:03d}c{cols[2]:03d}" and ids[0] == ids[1] and ids[3] is None
    # a block edge: the cells on both sides of x = UL_X + k * B fall into neighbouring columns
    k = cols[0] + 1
    edge_x = EASE2_UL_X + k * BLOCK_SIZE_M
    to_ll = Transformer.from_crs("EPSG:6933", "EPSG:4326", always_xy=True)
    (lw, la_), (le, lb) = to_ll.transform(edge_x - 10, 4.0e6), to_ll.transform(edge_x + 10, 4.0e6)
    _, _, c2 = block_ids([lw, le], [la_, lb])
    assert list(c2) == [k - 1, k]
    assert block_ids([-118.8], [34.06])[0][0] == "r064c117"        # fixed id: deterministic, data-independent


# ---------------------------------------------------------------------------- form B
def test_weather_on_date_lags_alias_and_month_chunks(lakes, lake):
    tiny, wide = lakes
    lon, lat = tiny.pixel_lonlat(*SITE)
    for m, first in ((4, date(2026, 4, 1)), (5, date(2026, 5, 1)), (6, date(2026, 6, 1))):
        nd = (date(2026, m + 1, 1) - first).days
        wide.weather("wx-test", first, nd, {"t2m": lambda k, lo, la, m=m: 200.0 + 100 * m + k + 0 * lo,
                                            "swvl1": lambda k, lo, la, m=m: m / 10 + k / 1000 + 0 * lo})
    # alias: the May store has only base_sm (no copy), the June store has the copy team_sm as well
    wide.weather("wx-alias", date(2026, 5, 1), 31, {"base_sm": lambda k, lo, la: 0.5 + k / 1000 + 0 * lo})
    wide.weather("wx-alias", date(2026, 6, 1), 30, {"base_sm": lambda k, lo, la: 0.6 + k / 1000 + 0 * lo,
                                                     "team_sm": lambda k, lo, la: 0.6 + k / 1000 + 0 * lo})
    tiny.labels([{"site_id": "A", "lon": lon, "lat": lat, "date": D},
                 {"site_id": "B", "lon": lon, "lat": lat, "date": date(2026, 5, 20)}])
    _build(lake, statics=False, tables=("lfmc_samples",), lags_days=(0, 15, 30, 60))
    df = _read(lake).set_index("site_id")
    a, b = df.loc["A"], df.loc["B"]
    assert a.t2m == pytest.approx(814.0) and b.t2m == pytest.approx(719.0)          # on the date (June 15 = k 14)
    assert a.swvl1_lag15d == pytest.approx(0.5 + 30 / 1000)                          # 2026-05-31 (May store)
    assert a.swvl1_lag30d == pytest.approx(0.5 + 15 / 1000)                          # 2026-05-16
    assert a.swvl1_lag60d == pytest.approx(0.4 + 15 / 1000)                          # 2026-04-16 (April store)
    assert np.isnan(b.swvl1_lag60d)                                                   # 2026-03-21: not staged
    assert "t2m_lag15d" not in df.columns and "swvl1_lag15d_dt_days" not in df.columns
    assert a.team_sm == pytest.approx(0.614) and b.team_sm == pytest.approx(0.519)   # alias read from base_sm
    assert a.team_sm_lag15d == pytest.approx(0.530)                                  # alias lag in the May store
    assert "base_sm_lag15d" not in df.columns                                         # base_sm is not flagged
    cols = _item(lake)["properties"]["fuellake:columns"]
    assert cols["team_sm"]["alias_of"] == "base_sm" and cols["swvl1_lag30d"]["lag_days"] == 30


# ---------------------------------------------------------------------------- dynamic + SME2 anomaly
def _sme2(wide: SynthLake, day: date, value: float) -> dict:
    return wide.raster("nisar-sme2", day, {"soil_moisture": wide.const(value)}, maturity="PROVISIONAL")


def test_sme2_causal_window_and_anomaly_maths(lakes, lake):
    from fuellake.connectors.smap import climatology_step

    tiny, wide = lakes
    lon, lat = tiny.pixel_lonlat(*SITE)
    _sme2(wide, D - timedelta(days=4), 0.30)
    _sme2(wide, D + timedelta(days=2), 0.35)
    _sme2(wide, date(2024, 2, 29), 0.25)
    tiny.labels([{"site_id": "A", "lon": lon, "lat": lat, "date": D},
                 {"site_id": "L", "lon": lon, "lat": lat, "date": date(2024, 3, 2)},
                 {"site_id": "N", "lon": lon, "lat": lat, "date": date(2025, 1, 10)}])
    # no climatology item yet: anomaly missing with its reason, never 0
    _build(lake, statics=False, tables=("lfmc_samples",), lags_days=())
    df = _read(lake).set_index("site_id")
    assert df.loc["A", "soil_moisture"] == pytest.approx(0.30) and df.loc["A", "soil_moisture_dt_days"] == -4
    assert np.isnan(df.loc["A", "sme2_anomaly"]) and "sme2_anomaly:no_climatology" in df.loc["A", "missing_inputs"]
    assert "nisar-sme2:no_scene" in df.loc["N", "missing_inputs"] and np.isnan(df.loc["N", "soil_moisture"])
    assert "sme2_anomaly:no_sme2" in df.loc["N", "missing_inputs"]

    from fixtures.points.synthpoints import climatology

    climatology(wide, lambda k: 0.10 + k / 10000, lambda k: 0.02 + k / 100000)
    _build(lake, statics=False, tables=("lfmc_samples",), lags_days=())
    df = _read(lake).set_index("site_id")
    k = climatology_step(D - timedelta(days=4))                       # the scene's month and day, not the point's
    assert df.loc["A", "sme2_anomaly"] == pytest.approx(0.30 - (0.10 + k / 10000), abs=1e-6)
    assert df.loc["A", "sme2_zscore"] == pytest.approx((0.30 - (0.10 + k / 10000)) / (0.02 + k / 100000), rel=1e-5)
    assert df.loc["L", "soil_moisture_dt_days"] == -2                 # 29 Feb scene: its own step (59)
    assert df.loc["L", "sme2_anomaly"] == pytest.approx(0.25 - (0.10 + 59 / 10000), abs=1e-6)
    assert "sme2_anomaly" not in df.loc["A", "missing_inputs"]
    # not causal: the later, nearer scene
    _build(lake, statics=False, tables=("lfmc_samples",), lags_days=(), causal=False)
    df = _read(lake).set_index("site_id")
    assert df.loc["A", "soil_moisture"] == pytest.approx(0.35) and df.loc["A", "soil_moisture_dt_days"] == 2
    _build(lake, statics=False, tables=("lfmc_samples",), lags_days=(), window_days=3)
    assert np.isnan(_read(lake).set_index("site_id").loc["A", "soil_moisture"])
    assert _item(lake)["properties"]["fuellake:window_days"] == 3


# ---------------------------------------------------------------------------- statics
class FakeRemote:
    """Remote factory: release-specific constants; ``nan_at`` lon values give nodata, ``fail`` products fail."""

    def __init__(self, nan_at=(), fail=()):
        self.calls: list[tuple[str, str, int]] = []
        self.nan_at = set(nan_at)
        self.fail = set(fail)

    def __call__(self, product, release, assets, _opts):
        def read(lon, lat):
            self.calls.append((product, release.label, len(lon)))
            n = len(lon)
            codes = {"LF 2016": 141.0, "LF 2022": 142.0, "LF 2025": 145.0}
            out = {}
            for a in assets:
                if a == "fuel_model":
                    v = np.full(n, codes[release.label])
                elif a == "elevation":
                    v = np.full(n, 1000.0)
                elif a == "slope":
                    v = np.full(n, 10.0)
                else:
                    v = np.full(n, 90.0)
                v[[x in self.nan_at for x in lon]] = np.nan
                out[a] = v
            failed = np.full(n, product in self.fail)
            return out, failed
        return read


def test_static_release_rule_lake_remote_and_derived_loads(lakes, lake):
    tiny, wide = lakes
    lon, lat = tiny.pixel_lonlat(*SITE)
    olon, olat = _outside(wide)
    lf_release(tiny, 2016, 122)
    lf_release(tiny, 2022, 102)          # LF 2025 is remote only
    tiny.raster("cop-dem", date(2021, 4, 22), {"elevation": tiny.const(55.0), "slope": tiny.const(3.0),
                                                "aspect": tiny.const(180.0)})
    rows = [("in23", lon, lat, date(2023, 6, 1)),          # LF 2022 valid from 2022-10-01 -> lake (tiny)
            ("in22", lon, lat, date(2022, 6, 1)),          # before LF 2022's window end -> LF 2016 (lake)
            ("out22", olon, olat, date(2022, 6, 1)),        # LF 2016, outside tiny -> remote
            ("out26", olon, olat, date(2026, 1, 15)),       # LF 2025 -> remote only
            ("out16", olon, olat, date(2016, 7, 1))]        # no release whose disturbances end before
    ignitions(tiny, [{"ignition_id": i, "lon": x, "lat": y, "discovery_date": d, "fire_size_ha": 1.0}
                     for i, x, y, d in rows])
    fake = FakeRemote(nan_at={lon})        # landfire-topo has no value at the tiny site -> cop-dem (lake)
    _build(lake, remote=True, remote_factory=fake, tables=("fire_ignitions",))
    df = _read(lake).set_index("point_id")
    assert df.loc["in23", "fuel_model"] == 102 and df.loc["in23", "fuel_release"] == "LF 2022"
    assert df.loc["in23", "fuel_source"] == "lake:tiny"
    assert df.loc["in22", "fuel_model"] == 122 and df.loc["in22", "fuel_release"] == "LF 2016"
    assert df.loc["out22", "fuel_model"] == 141 and df.loc["out22", "fuel_source"] == "remote"
    assert df.loc["out26", "fuel_model"] == 145 and df.loc["out26", "fuel_release"] == "LF 2025"
    assert pd.isna(df.loc["out16", "fuel_model"]) and pd.isna(df.loc["out16", "fuel_release"])
    assert "fuel:no_release" in df.loc["out16", "missing_inputs"]
    assert str(df["fuel_model"].dtype) == "Int32" and str(df["burnable"].dtype) == "Int32"
    # Scott and Burgan loads from the code: GR2 (102) = 0.10 + 1.00 t/ac; SH2 (142) = 1.35 + 2.4 + 0.75 + 3.85
    t = 0.22417023114334675
    assert df.loc["in23", "total_load"] == pytest.approx((0.10 + 1.00) * t, rel=1e-6)
    assert df.loc["in23", "burnable"] == 1 and np.isnan(df.loc["out16", "total_load"])
    # terrain: time-invariant (exempt from causal); LANDFIRE topo remote first, cop-dem where it has no value
    assert df.loc["out16", "elevation"] == 1000.0 and df.loc["out16", "terrain_release"] == "LF 2020"
    assert df.loc["in23", "elevation"] == 55.0 and df.loc["in23", "terrain_source"] == "lake:tiny"
    assert df.loc["in23", "terrain_release"] == "COP-DEM GLO-30 2021"
    # not causal: plain item dates (LF 2022 for a 2022 fire, the earliest release before the first one)
    _build(lake, remote=True, remote_factory=fake, tables=("fire_ignitions",), causal=False)
    df = _read(lake).set_index("point_id")
    assert df.loc["in22", "fuel_model"] == 102 and df.loc["out16", "fuel_model"] == 141
    assert df.loc["out16", "fuel_release"] == "LF 2016"
    # remote off: outside points are recorded as such
    _build(lake, tables=("fire_ignitions",))
    df = _read(lake).set_index("point_id")
    assert pd.isna(df.loc["out26", "fuel_model"]) and "fuel:outside" in df.loc["out26", "missing_inputs"]
    assert df.loc["in23", "fuel_model"] == 102


def test_choose_release_unit():
    from fuellake.features.points import Release

    rels = [Release("p", "A", date(2016, 1, 1), date(2017, 1, 1)), Release("p", "B", date(2022, 1, 1),
                                                                         date(2022, 10, 1))]
    days = [date(2016, 6, 1), date(2017, 1, 1), date(2022, 9, 30), date(2022, 10, 1)]
    assert list(choose_release(rels, days, True, False)) == [-1, 0, 0, 1]
    assert list(choose_release(rels, days, False, False)) == [0, 0, 1, 1]
    assert list(choose_release(rels, [date(2010, 1, 1)], True, True)) == [0]       # time-invariant: earliest
    assert list(choose_release([], days, True, False)) == [-1] * 4


def test_missing_inputs_are_null_never_zero(lakes, lake):
    tiny, wide = lakes
    lon, lat = tiny.pixel_lonlat(*SITE)
    olon, olat = _outside(wide)
    wide.weather("wx-test", date(2026, 6, 1), 30, {"t2m": lambda k, lo, la: 0.0 + 0 * lo,       # a real zero
                                                   "swvl1": lambda k, lo, la: 0.2 + 0 * lo})
    ignitions(tiny, [{"ignition_id": "a", "lon": lon, "lat": lat, "discovery_date": D, "fire_size_ha": 1.0},
                     {"ignition_id": "b", "lon": olon, "lat": olat, "discovery_date": date(2026, 8, 1),
                      "fire_size_ha": 1.0}])
    lf_release(tiny, 2025, 142)
    fake = FakeRemote(fail={"landfire-fbfm40"})
    _build(lake, remote=True, remote_factory=fake, tables=("fire_ignitions",), lags_days=(0,))
    df = _read(lake).set_index("point_id")
    assert df.loc["a", "t2m"] == 0.0 and "wx-test" not in df.loc["a", "missing_inputs"]
    assert np.isnan(df.loc["b", "t2m"]) and "wx-test:no_data" in df.loc["b", "missing_inputs"]
    assert "wx-alias:no_items" in df.loc["a", "missing_inputs"]
    assert pd.isna(df.loc["b", "fuel_model"]) and "fuel:error" in df.loc["b", "missing_inputs"]
    assert df.loc["a", "fuel_model"] == 142 and "fuel:" not in df.loc["a", "missing_inputs"]
    assert np.isnan(df.loc["b", "total_load"])
    assert df["n_missing_inputs"].tolist() == [len(m.split(";")) for m in df["missing_inputs"]]
    # every value of a missing input is null (never 0): weather and fuels of point b, SME2 and its anomaly of both
    for col in ("t2m", "swvl1", "swvl1_lag0d", "total_load", "dead_1h_load", "fuel_bed_depth", "sme2_anomaly"):
        assert pd.isna(df.loc["b", col]), col
    assert pd.isna(df.loc["b", "burnable"]) and df[["sme2_anomaly", "sme2_zscore"]].isna().all().all()
    props = _item(lake)["properties"]
    assert props["fuellake:fill_rates"]["t2m"] == 0.5 and "wx-alias" in props["fuellake:missing_datasets"]


# ---------------------------------------------------------------------------- Sentinel-2
def _s2_setup(tmp_path, lon, lat, days_dn):
    from pyproj import Transformer

    x, y = Transformer.from_crs("EPSG:4326", "EPSG:32611", always_xy=True).transform(lon, lat)
    items = []
    for sid, day, cloudy in days_dn:
        items.append(s2_scene(tmp_path / "s2", sid, day, (x, y),
                              {"red": 1500, "nir": 4000, "nir08": 3500, "swir16": 2500}, cloud_at_centre=cloudy))
    return items


def _fake_search(items):
    calls = []

    def search(collection, body):
        calls.append((collection, body["datetime"]))
        if collection != "sentinel-2-c1-l2a":
            return []
        t0, t1 = body["datetime"].split("/")
        return [it for it in items if t0[:10] <= it["properties"]["datetime"][:10] <= t1[:10]]
    return search, calls


def test_s2_point_sampler_values_mask_causal_and_cache(tmp_path):
    lon, lat = -118.79, 34.06
    items = _s2_setup(tmp_path, lon, lat, [("S2_m3", D - timedelta(days=3), True),
                                           ("S2_m6", D - timedelta(days=6), False),
                                           ("S2_p1", D + timedelta(days=1), False)])
    search, calls = _fake_search(items)
    cache = PointCache(tmp_path / "c.sqlite")
    s = S2PointSampler(search_fn=search, cache=cache, window_days=8, causal=True, max_workers=2)
    r = s.sample([lon, lon], [lat, lat + 0.5], [D, D])
    assert r["status"][0] == "ok" and r["scene"][0] == "S2_m6" and r["dt_days"][0] == -6     # cloudy D-3 skipped
    refl = lambda dn: dn * 1e-4 - 0.1  # noqa: E731
    assert r["ndvi"][0] == pytest.approx((refl(4000) - refl(1500)) / (refl(4000) + refl(1500)))
    assert r["ndmi"][0] == pytest.approx((refl(3500) - refl(2500)) / (refl(3500) + refl(2500)))
    assert r["status"][1] == "no_scene" and np.isnan(r["ndvi"][1])                            # not covered
    assert s.stats.point_scene_reads == 2                                                       # D-3 then D-6
    # cached: no new search or read
    s2 = S2PointSampler(search_fn=search, cache=cache, window_days=8, causal=True)
    n = len(calls)
    r2 = s2.sample([lon], [lat], [D])
    assert r2["scene"][0] == "S2_m6" and len(calls) == n and s2.stats.cache_hits == 1
    # not causal: the later scene one day after is the nearest clear one
    s3 = S2PointSampler(search_fn=search, window_days=8, causal=False)
    r3 = s3.sample([lon], [lat], [D])
    assert r3["scene"][0] == "S2_p1" and r3["dt_days"][0] == 1


def test_s2_footprint_window():
    from affine import Affine

    t = Affine(10, 0, 0, 0, -10, 1000)
    assert footprint_window(55, 945, t, 100, 100, 15) == (4, 6, 4, 6)          # 3 x 3 at 10 m
    t20 = Affine(20, 0, 0, 0, -20, 1000)
    assert footprint_window(50, 950, t20, 50, 50, 15) == (2, 2, 2, 2)          # point at a pixel centre: 1 px
    assert footprint_window(60, 940, t20, 50, 50, 15) == (2, 3, 2, 3)          # point at a corner: 2 x 2
    assert footprint_window(-5, 950, t20, 50, 50, 15) is None


def test_s2_cap_keeps_large_fires_and_stratifies(lakes, lake, tmp_path):
    tiny, wide = lakes
    lon, lat = tiny.pixel_lonlat(*SITE)
    rows = [{"ignition_id": f"big{i}", "lon": lon + i * 1e-4, "lat": lat, "discovery_date": date(2020, 7, 1 + i),
             "fire_size_ha": 1000.0} for i in range(3)]
    rows += [{"ignition_id": f"s{i}", "lon": lon - i * 1e-4, "lat": lat,
              "discovery_date": date(2019 + i % 3, 6, 1 + i % 20), "fire_size_ha": 1.0} for i in range(30)]
    ignitions(tiny, rows)

    class Rec:
        stats = {"x": 1}

        def __init__(self):
            self.n = 0

        def sample(self, lon, lat, days):
            self.n = len(lon)
            k = len(lon)
            return {"ndvi": np.full(k, 0.5), "ndmi": np.full(k, 0.1), "dt_days": np.full(k, -1.0),
                    "scene": np.full(k, "S", dtype=object), "status": np.full(k, "ok", dtype=object)}

    rec = Rec()
    _build(lake, statics=False, tables=("fire_ignitions",), sentinel2=True, s2_sampler=rec, s2_max_points=9, seed=3)
    df = _read(lake).set_index("point_id")
    assert rec.n == 9
    assert (df.loc[["big0", "big1", "big2"], "s2_status"] == "ok").all()
    small = df[~df.index.str.startswith("big")]
    assert (small.s2_status == "ok").sum() == 6 and (small.s2_status == "not_sampled").sum() == 24
    assert small.loc[small.s2_status == "not_sampled", "s2_ndvi"].isna().all()
    assert all("sentinel2:not_sampled" in m for m in small.loc[small.s2_status == "not_sampled", "missing_inputs"])
    # proportional strata (year): 10 rows per year, 6 picks -> 2 per year; reproducible with the seed
    years = pd.Series([d.year for d in small.loc[small.s2_status == "ok", "date"]]).value_counts().to_dict()
    assert years == {2019: 2, 2020: 2, 2021: 2}
    first = sorted(small.index[small.s2_status == "ok"])
    _build(lake, statics=False, tables=("fire_ignitions",), sentinel2=True, s2_sampler=Rec(), s2_max_points=9,
           seed=3)
    df2 = _read(lake).set_index("point_id")
    assert sorted(df2.index[(df2.s2_status == "ok") & ~df2.index.str.startswith("big")]) == first
    assert _item(lake)["properties"]["fuellake:sentinel2"]["max_points"] == 9


# ---------------------------------------------------------------------------- remote readers
def test_parse_samples_by_location_id():
    payload = {"samples": [{"locationId": 2, "value": "145"}, {"locationId": 0, "value": "NoData"},
                           {"locationId": 3, "value": "-9999"}, {"locationId": 9, "value": "5"}]}
    v = parse_samples(payload, 4, -9999)
    assert np.isnan(v[0]) and np.isnan(v[1]) and v[2] == 145 and np.isnan(v[3])
    with pytest.raises(ValueError):
        parse_samples({"error": {"code": 500}}, 1, None)


class FakeSession:
    def __init__(self, fail=False):
        self.posts = []
        self.fail = fail

    def post(self, url, data, timeout):
        self.posts.append(json.loads(data["geometry"]))
        if self.fail:
            raise ConnectionError("down")
        pts = self.posts[-1]["points"]

        class R:
            def raise_for_status(self):
                pass

            def json(self_inner):
                # locationId 1 left out (nodata), values = round(lon * 10)
                return {"samples": [{"locationId": i, "value": str(round(p[0] * 10))}
                                    for i, p in enumerate(pts) if i != 1]}
        return R()


def test_arcgis_sampler_batches_cache_and_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(ps_mod.time, "sleep", lambda s: None)
    cache = PointCache(tmp_path / "c.sqlite")
    sess = FakeSession()
    s = ArcGisImageSampler("https://example.invalid/X/ImageServer", cache=cache, batch=2, session=sess,
                           max_workers=2)
    lon, lat = np.array([-118.1, -118.2, -118.3]), np.array([34.0, 34.1, 34.2])
    v, failed = s.sample(lon, lat)
    assert len(sess.posts) == 2 and [len(p["points"]) for p in sess.posts] == [2, 1]
    assert v[0] == -1181 and np.isnan(v[1]) and v[2] == -1183 and not failed.any()
    v2, _ = ArcGisImageSampler("https://example.invalid/X/ImageServer", cache=cache, session=sess).sample(lon, lat)
    assert len(sess.posts) == 2 and np.array_equal(np.isnan(v2), np.isnan(v))             # nodata cached too
    bad = FakeSession(fail=True)
    s3 = ArcGisImageSampler("https://example.invalid/Y/ImageServer", cache=cache, session=bad, retries=1)
    v3, f3 = s3.sample(lon, lat)
    assert f3.all() and np.isnan(v3).all() and len(bad.posts) == 2                         # retried once
    assert cache.get_many(s3.source_id, ["-118100000,34000000"]) == {}                       # failures not cached


def test_copdem_tile_and_terrain_window():
    assert copdem_tile(-118.75, 34.1) == "Copernicus_DSM_COG_10_N34_00_W119_00_DEM"
    assert copdem_tile(2.5, -0.5) == "Copernicus_DSM_COG_10_S01_00_E002_00_DEM"
    from fuellake.features._point_sources import metres_per_degree

    lat = 34.0
    m_lon, m_lat = metres_per_degree(lat)
    res = 1 / 3600
    dx = res * m_lon
    z = np.array([[0, 1, 2], [0, 1, 2], [0, 1, 2]], dtype=float) * dx      # rises 1 m per metre to the east
    elev, slope, aspect = terrain_from_window(z, lat, res, -res)
    assert elev == pytest.approx(dx) and slope == pytest.approx(45.0, abs=1e-6) and aspect == pytest.approx(270.0)
    assert math.isnan(terrain_from_window(z[:2], lat, res, -res)[1])


# ---------------------------------------------------------------------------- config, CLI, item
def test_point_features_config_matches_builtin():
    with open(REPO_CONFIG / "point-features.yaml") as f:
        from_yaml = DatasetConfig(**yaml.safe_load(f))
    assert from_yaml.model_dump() == DatasetConfig(**BUILTIN_CONFIG).model_dump()


def test_cli_build_points_and_item_validates(lakes, lake):
    from fuellake.cli import app

    tiny, wide = lakes
    lon, lat = tiny.pixel_lonlat(*SITE)
    tiny.labels([{"site_id": "A", "lon": lon, "lat": lat, "date": D}])
    res = CliRunner().invoke(app, ["build-points", "--aoi", "wide", "--start", "2026-06-01", "--end", "2026-06-30",
                                   "--tables", "lfmc_samples,fire_ignitions", "--no-sentinel2", "--no-remote",
                                   "--lags-days", "0,30"])
    assert res.exit_code == 0, res.output
    assert res.output.strip() == "features/wide/point_features.parquet"
    item = _item(lake)
    assert item["id"] == "point-features__wide__20260601__20260630"
    props = item["properties"]
    assert props["fuellake:missing_tables"] == ["fire_ignitions"] and props["fuellake:lags_days"] == [0, 30]
    assert props["fuellake:n_rows"] == 1 and props["fuellake:causal"] is True
    results = validate_item(lake, item, point_config())
    assert all(r.ok for r in results), [r.to_dict() for r in results]
    types = [e["type"] for e in events.read_events(lake) if e["collection"] == "point-features"]
    assert types == ["item.created", "table.updated"]
    bad = CliRunner().invoke(app, ["build-points", "--aoi", "wide", "--tables", "nope", "--no-sentinel2"])
    assert bad.exit_code == 1 and "no point table spec" in bad.output
    bad = CliRunner().invoke(app, ["build-points", "--aoi", "nowhere", "--no-sentinel2"])
    assert bad.exit_code == 1


def test_empty_build_and_parameter_validation(lakes, lake):
    _build(lake, statics=False, tables=("lfmc_samples",))
    assert len(_read(lake)) == 0
    assert _item(lake)["properties"]["fuellake:missing_tables"] == ["lfmc_samples"]
    for kw in ({"lags_days": (-1,)}, {"window_days": -1}, {"s2_max_points": -1}, {"tables": ()}):
        with pytest.raises(ValueError):
            _build(lake, **kw)
