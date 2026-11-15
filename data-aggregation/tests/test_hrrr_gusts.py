# fuel-lake — tests for the HRRR gust connector (idx parsing, gridMET-day sampling, GRIB crop, monthly Zarr)
# Licensed under Apache-2.0.
from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest
import rasterio
import rasterio.shutil
import yaml
from affine import Affine
from rasterio.warp import transform_bounds

from conftest import TINY_AOI
from fuellake import catalog, pipeline
from fuellake.connectors.base import Period
from fuellake.connectors.hrrr_gusts import (
    HrrrGustsConnector,
    decode_crop,
    find_message,
    parse_idx,
    sample_times,
    sampling_tag,
)
from fuellake.contracts import validate_collection
from fuellake.rasters import open_zarr
from fuellake.registry import load_dataset

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
FIX = Path(__file__).parent / "fixtures" / "hrrr_gusts"
DS = "hrrr-gusts"
# HRRR CONUS grid as decoded by GDAL from a real 2026-06-01 00Z message (sphere R = 6371229 m)
HRRR_CRS = "+proj=lcc +lat_0=38.5 +lon_0=-97.5 +lat_1=38.5 +lat_2=38.5 +x_0=0 +y_0=0 +R=6371229 +units=m +no_defs"
HRRR_T = Affine(3000.0, 0.0, -2699020.14252193, 0.0, -3000.0, 1588193.8474433357)


# ---------------------------------------------------------------------------- idx files (real, 2026-06-01 00Z)
def test_parse_real_idx_and_find_messages():
    f00 = parse_idx((FIX / "hrrr.20260601.t00z.wrfsfcf00.grib2.idx").read_text())
    f01 = parse_idx((FIX / "hrrr.20260601.t00z.wrfsfcf01.grib2.idx").read_text())
    assert len(f00) == 170 and len(f01) == 170
    g = find_message(f00, "GUST", "surface", "anl")
    assert (g.number, g.offset, g.end, g.length) == (9, 3471277, 4581565, 1110289)
    assert g.range_header() == "bytes=3471277-4581565" and g.ref_time == "2026060100"
    w = find_message(f01, "WIND", "10 m above ground", "0-1 hour max fcst")
    assert (w.offset, w.end) == (56230746, 57372715)
    assert f00[-1].end is None and f00[-1].range_header().endswith("-")
    with pytest.raises(KeyError):               # REFD:263 K level appears twice in f01 (instant and 1-h max)
        find_message(f01, "REFD", "263 K level")
    assert find_message(f01, "REFD", "263 K level", "0-1 hour max fcst").number == 44


def test_parse_idx_rejects_bad_lines():
    with pytest.raises(ValueError):
        parse_idx("1:0:d=2026060100:GUST\n")
    with pytest.raises(ValueError, match="not increasing"):
        parse_idx("1:100:d=2026060100:A:surface:anl:\n2:50:d=2026060100:B:surface:anl:\n")


def test_sample_times_follow_the_gridmet_day():
    t = sample_times(date(2026, 6, 30), [18, 0, 12, 6])
    assert t == [datetime(2026, 6, 30, 12, tzinfo=UTC), datetime(2026, 6, 30, 18, tzinfo=UTC),
                 datetime(2026, 7, 1, 0, tzinfo=UTC), datetime(2026, 7, 1, 6, tzinfo=UTC)]
    assert sample_times(date(2026, 6, 1), [7]) == [datetime(2026, 6, 1, 7, tzinfo=UTC)]


# ---------------------------------------------------------------------------- synthetic GRIB2 on the HRRR grid
def _window_origin(bbox, pad: int = 6) -> tuple[int, int, int, int]:
    from pyproj import CRS

    w, s, e, n = transform_bounds("EPSG:4326", CRS.from_user_input(HRRR_CRS), *bbox)
    inv = ~HRRR_T
    c0, r0 = (int(np.floor(v)) - pad for v in inv @ (w, n))
    c1, r1 = (int(np.ceil(v)) + pad for v in inv @ (e, s))
    return c0, r0, c1 - c0, r1 - r0


def make_grib(value_fn, bbox=TINY_AOI["bbox"]) -> bytes:
    """A small GRIB2 message on a window of the real HRRR grid; ``value_fn(rows, cols)`` gives the field."""
    c0, r0, w, h = _window_origin(bbox)
    rows, cols = np.mgrid[0:h, 0:w]
    arr = np.asarray(value_fn(rows, cols), dtype="float32")
    with tempfile.TemporaryDirectory() as tmp:
        tif, grb = Path(tmp) / "f.tif", Path(tmp) / "f.grib2"
        with rasterio.open(tif, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32", crs=HRRR_CRS,
                           transform=HRRR_T @ Affine.translation(c0, r0)) as ds:
            ds.write(arr, 1)
        with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(tif) as src:
            rasterio.shutil.copy(src, grb, driver="GRIB")
        return grb.read_bytes()


def test_decode_crop_cuts_the_aoi_window():
    data = make_grib(lambda r, c: 100.0 * r + c)
    arr, tr, crs = decode_crop(data, TINY_AOI["bbox"], buffer_cells=1)
    assert arr.dtype == np.float32 and 2 <= arr.shape[0] <= 6 and 2 <= arr.shape[1] <= 6
    assert abs(tr.a - 3000.0) < 1e-6 and tr.e == pytest.approx(-3000.0)
    w, s, e, n = transform_bounds("EPSG:4326", crs, *TINY_AOI["bbox"])
    assert tr.c <= w and tr.f >= n and tr.c + arr.shape[1] * 3000 >= e and tr.f - arr.shape[0] * 3000 <= s
    with pytest.raises(ValueError, match="not a GRIB"):
        decode_crop(b"<html>", TINY_AOI["bbox"], 1)


# ---------------------------------------------------------------------------- offline pipeline
def _idx_for(var: str, level: str, forecast: str, msg_len: int) -> bytes:
    return (f"1:0:d=2026060100:REFC:entire atmosphere:{forecast}:\n"
            f"2:100:d=2026060100:{var}:{level}:{forecast}:\n"
            f"3:{100 + msg_len}:d=2026060100:TMP:2 m above ground:{forecast}:\n").encode()


class FakeBucket:
    """Serves synthetic idx + GRIB bytes; the field value encodes the valid hour (gust) or day (wind)."""

    def __init__(self, missing: set[str] | None = None):
        self.calls: list[str] = []
        self.missing = missing or set()

    def value(self, url: str) -> float:
        name = url.rsplit("/", 1)[-1]
        cycle = datetime.strptime(url.split("/hrrr.")[1][:8] + name.split(".t")[1][:2], "%Y%m%d%H")
        fh = int(name.split("wrfsfcf")[1][:2])
        hour = (cycle.hour + fh) % 24
        return (20.0 if fh == 0 else 30.0) + hour / 10.0 + cycle.day / 100.0

    def get(self, conn, url, range_header=None):
        self.calls.append(url)
        if any(m in url for m in self.missing):
            return None
        fh = int(url.rsplit("wrfsfcf", 1)[1][:2])
        grib = make_grib(lambda r, c, v=self.value(url.removesuffix(".idx")): np.full(r.shape, v))
        if url.endswith(".idx"):
            var, level, fc = (("GUST", "surface", "anl") if fh == 0 else
                              ("WIND", "10 m above ground", "0-1 hour max fcst"))
            return _idx_for(var, level, fc, len(grib))
        assert range_header == f"bytes=100-{100 + len(grib) - 1}"
        return grib

    def exists(self, conn, url):
        return not any(m in url for m in self.missing)


@pytest.fixture
def hrrr_config(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    shutil.copy(REPO_CONFIG / "datasets" / f"{DS}.yaml", root / "datasets" / f"{DS}.yaml")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


@pytest.fixture
def bucket(monkeypatch):
    b = FakeBucket()
    monkeypatch.setattr(HrrrGustsConnector, "_get", lambda self, url, range_header=None: b.get(self, url, range_header))
    monkeypatch.setattr(HrrrGustsConnector, "_exists", lambda self, url: b.exists(self, url))
    return b


JUNE = Period(date(2026, 6, 10), date(2026, 6, 12))


def test_pipeline_offline_month(lake, hrrr_config, bucket):
    opts = load_dataset(DS).options["messages"]
    rep = pipeline.run([DS], "tiny", JUNE, lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", d.error or [u.error for u in d.units]
    item = next(catalog.iter_items(lake, DS))
    assert item["id"] == "hrrr-gusts__tiny__20260601"
    assert item["properties"]["fuellake:source_key"] == "hrrr-gusts-202606|" + sampling_tag(opts)
    p = item["properties"]
    assert p["hrrr:first_day"] == "2026-06-01" and p["hrrr:last_day"] == "2026-06-30"
    assert p["hrrr:samples_per_day"]["gust_max"] == {"min": len(opts["gust_max"]["hours_utc"]),
                                                     "max": len(opts["gust_max"]["hours_utc"])}
    assert p["fuellake:bytes_downloaded"] > 0
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))

    ds = open_zarr(lake, catalog.asset_rel(item, "data"))
    assert ds["gust_max"].dims == ("time", "y", "x") and ds.sizes["time"] == 30
    assert str(ds["time"].values[0])[:10] == "2026-06-01" and ds.rio.crs is not None
    # day D = 07Z D .. 07Z D+1: expected max of the sampled valid hours, with the day of the valid time
    day = date(2026, 6, 10)
    exp_g = max(20.0 + t.hour / 10.0 + t.day / 100.0 for t in sample_times(day, opts["gust_max"]["hours_utc"]))
    got = float(ds["gust_max"].sel(time="2026-06-10").values[0, 0])
    assert got == pytest.approx(exp_g, abs=1e-4)
    exp_w = max(30.0 + t.hour / 10.0 + (t.day if t.hour >= 1 else t.day - 1) / 100.0
                for t in sample_times(day, opts["wind10_max"]["hours_utc"]))
    assert float(ds["wind10_max"].sel(time="2026-06-10").values[0, 0]) == pytest.approx(exp_w, abs=1e-4)
    assert int(ds["n_gust_samples"].sel(time="2026-06-10")) == len(opts["gust_max"]["hours_utc"])

    n = len(bucket.calls)
    rep2 = pipeline.run([DS], "tiny", JUNE, lake=lake)
    assert (rep2.datasets[0].skipped, len(rep2.datasets[0].units)) == (1, 0)
    assert len(bucket.calls) == n                 # discover uses HEAD (_exists) only


def test_missing_message_lowers_the_sample_count(lake, hrrr_config, bucket):
    hour = load_dataset(DS).options["messages"]["gust_max"]["hours_utc"][0]
    t = sample_times(date(2026, 6, 10), [hour])[0]
    bucket.missing = {f"hrrr.{t:%Y%m%d}/conus/hrrr.t{t:%H}z.wrfsfcf00.grib2.idx"}
    rep = pipeline.run([DS], "tiny", JUNE, lake=lake)
    assert rep.datasets[0].status == "ok", [u.error for u in rep.datasets[0].units]
    ds = open_zarr(lake, catalog.asset_rel(next(catalog.iter_items(lake, DS)), "data"))
    n_hours = len(load_dataset(DS).options["messages"]["gust_max"]["hours_utc"])
    assert int(ds["n_gust_samples"].sel(time="2026-06-10")) == n_hours - 1
    assert int(ds["n_gust_samples"].sel(time="2026-06-11")) == n_hours


def test_extract_resumes_from_cached_days(lake, hrrr_config, bucket):
    from fuellake.config import Settings
    from fuellake.registry import load_aoi

    conn = HrrrGustsConnector(load_dataset(DS), lake, Settings.from_env())
    ref = conn.discover(load_aoi("tiny"), JUNE)[0]
    budget = {"bytes": 0}
    conn.fetch_day(ref, load_aoi("tiny"), date(2026, 6, 3), budget)
    n = len(bucket.calls)
    conn.fetch_day(ref, load_aoi("tiny"), date(2026, 6, 3), budget)
    assert len(bucket.calls) == n and budget["bytes"] > 0


def test_budget_guard(lake, hrrr_config, bucket):
    cfg = yaml.safe_load((hrrr_config / "datasets" / f"{DS}.yaml").read_text())
    cfg["options"]["max_unit_mb"] = 0.01
    (hrrr_config / "datasets" / f"{DS}.yaml").write_text(yaml.safe_dump(cfg))
    rep = pipeline.run([DS], "tiny", JUNE, lake=lake)
    u = rep.datasets[0].units[0]
    assert u.status == "failed" and "budget" in u.error


def test_open_month_key_uses_last_complete_day(lake, hrrr_config, bucket, monkeypatch):
    from fuellake.config import Settings
    from fuellake.registry import load_aoi

    conn = HrrrGustsConnector(load_dataset(DS), lake, Settings.from_env())
    monkeypatch.setattr(HrrrGustsConnector, "last_complete_day", lambda self, upto, lookback_days=4: date(2026, 6, 12))
    refs = conn.discover(load_aoi("tiny"), Period(date(2026, 6, 1), date(2026, 7, 5)))
    assert [r.key.split("|")[0] for r in refs] == ["hrrr-gusts-202606-through-20260612"]
    assert refs[0].key.split("|")[1] == "gust_max@00,12+wind10_max@00,12"
    assert refs[0].meta["last_day"] == "2026-06-12"


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_one_day_tiny_aoi(lake, hrrr_config):
    from fuellake.config import Settings
    from fuellake.registry import load_aoi

    conn = HrrrGustsConnector(load_dataset(DS), lake, Settings.from_env())
    ref = conn.ref("hrrr-gusts-202610", datetime(2026, 10, 1, tzinfo=UTC), first_day="2026-10-02",
                   last_day="2026-10-02")
    budget = {"bytes": 0}
    rel = conn.fetch_day(ref, load_aoi("tiny"), date(2026, 10, 2), budget)
    n_msgs = sum(len(m["hours_utc"]) for m in load_dataset(DS).options["messages"].values())
    assert budget["bytes"] < n_msgs * 1.5e6
    with lake.local_path(rel) as path, rasterio.open(path) as src:
        a = src.read()
        assert src.count == n_msgs and np.isfinite(a).all() and 0 <= a.min() and a.max() < 60
