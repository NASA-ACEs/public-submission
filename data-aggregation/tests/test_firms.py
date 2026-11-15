# fuel-lake — tests for the FIRMS connector (archive and API CSV layouts, key handling, live archive check)
# Licensed under Apache-2.0.
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from fuellake import catalog, pipeline
from fuellake.config import Settings
from fuellake.connectors.base import Period
from fuellake.connectors.fire_perimeters import buffer_bbox_m
from fuellake.connectors.firms import (
    DETECTION_SCHEMA,
    FirmsConnector,
    confidence_class,
    detections_frame,
    stream_filter_csv,
)
from fuellake.contracts import validate_collection, validate_table
from fuellake.registry import load_aoi, load_dataset
from fuellake.storage import Lake
from fuellake.tables import read_table, write_geoparquet

FIX = Path(__file__).parent / "fixtures" / "weather_fire"
MODIS = (FIX / "firms_modis_archive.csv").read_text()
VIIRS = (FIX / "firms_viirs_archive.csv").read_text()
API = (FIX / "firms_api_viirs_synthetic.csv").read_text()
SMM = (-118.95, 33.98, -118.50, 34.20)
BUF = buffer_bbox_m(SMM, 1000)   # the connector default (options.buffer_m)


class FakeResponse:
    def __init__(self, status: int = 200, text: str = "", headers: dict | None = None):
        self.status_code, self.text, self.headers = status, text, headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_lines(self):
        yield from (ln.encode() for ln in self.text.splitlines())

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class FakeFirms:
    """Country archive files exist for 2018 only; everything else is 404."""

    def __init__(self):
        self.gets: list[str] = []

    def head(self, url, timeout=None, allow_redirects=True):
        if "/2018/" in url:
            return FakeResponse(200, headers={"Last-Modified": "Wed, 04 Dec 2024 21:27:08 GMT",
                                              "Content-Length": "10488759"})
        return FakeResponse(404)

    def get(self, url, stream=False, timeout=None):
        self.gets.append(url)
        if url.endswith("modis_2018_United_States.csv"):
            return FakeResponse(200, MODIS)
        if url.endswith("viirs-snpp_2018_United_States.csv"):
            return FakeResponse(200, VIIRS)
        return FakeResponse(404, "not found")


def test_stream_filter_csv_keeps_only_aoi_rows():
    text, n_in, n_total = stream_filter_csv(MODIS.splitlines(), BUF)
    assert (n_in, n_total) == (4, 5)
    assert stream_filter_csv(MODIS.splitlines(), SMM)[1] == 2   # two rows lie only inside the 1 km buffer
    assert text.splitlines()[0].startswith("latitude,longitude,brightness")
    assert "-90.2127" not in text
    with pytest.raises(ValueError):
        stream_filter_csv(["a,b,c", "1,2,3"], SMM)


def test_confidence_classes():
    assert [confidence_class(v, "MODIS") for v in ("25", "30", "79", "80", "100")] == \
        ["low", "nominal", "nominal", "high", "high"]
    assert [confidence_class(v, "VIIRS") for v in ("l", "n", "h", "nominal")] == ["low", "nominal", "high", "nominal"]
    assert confidence_class("", "VIIRS") is None


def test_modis_archive_rows():
    gdf = detections_frame(MODIS, "modis", "archive-country", BUF)
    assert list(gdf.columns) == list(DETECTION_SCHEMA) + ["geometry"]
    assert len(gdf) == 4 and gdf.detection_id.is_unique
    r = gdf.iloc[0]
    assert r.acq_time == "0517" and r.acq_datetime == datetime(2018, 11, 9, 5, 17, tzinfo=UTC)
    assert r.acq_date == date(2018, 11, 9) and r.instrument == "MODIS" and r.version == "6.03"
    assert set(gdf.confidence_class) == {"high", "low"} and set(gdf.detection_type.dropna()) == {0, 2}
    assert gdf.geometry.x.tolist() == gdf.lon.tolist()


def test_viirs_archive_and_api_layouts():
    v = detections_frame(VIIRS, "viirs-snpp", "archive-country", BUF)
    assert len(v) == 3 and set(v.confidence_class) == {"nominal"} and v.brightness_k.notna().all()
    a = detections_frame(API, "VIIRS_NOAA20_NRT", "api-area", BUF)
    assert len(a) == 2                                  # the Quebec row is outside the AOI
    assert a.detection_type.isna().all()                # NRT layout has no 'type' column
    assert a.instrument.tolist() == ["VIIRS", "VIIRS"]  # nor 'instrument'
    assert a.confidence_class.tolist() == ["nominal", "high"] and a.version.tolist() == ["2.0NRT"] * 2


def test_empty_part_validates(lake):
    rel = "staged/tables/fire_detections/firms/part-empty.parquet"
    write_geoparquet(lake, rel, detections_frame("", "modis", "archive-country"))
    res = validate_table(lake, rel, "fire_detections")
    assert res.ok and res.details["n_rows"] == 0


def test_discover_archive_years_and_api_months(lake, monkeypatch):
    conn = FirmsConnector(load_dataset("firms"), lake, Settings.from_env())
    fake = FakeFirms()
    conn.http_session = lambda bearer_env=None: fake
    refs = conn.discover(load_aoi("smm"), Period(date(2018, 12, 1), date(2019, 2, 10)))
    keys = [r.key for r in refs]
    assert keys[:2] == ["country-modis-smm-2018-v20241204", "country-viirs-snpp-smm-2018-v20241204"]
    api = [r for r in refs if r.meta["kind"] == "api"]
    assert {r.meta["product"] for r in api} == {"VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT"}
    assert {r.datetime.month for r in api} == {1, 2}    # whole months of 2019 touched by the period
    assert all("[MAP_KEY]" in r.urls[0] for r in api)   # never a real key in catalogued URLs


def test_only_api_units_need_the_map_key(lake, monkeypatch):
    conn = FirmsConnector(load_dataset("firms"), lake, Settings.from_env())
    conn.http_session = lambda bearer_env=None: FakeFirms()
    aoi = load_aoi("smm")
    refs = conn.discover(aoi, Period(date(2018, 12, 1), date(2019, 2, 10)))
    assert conn.missing_credentials(aoi) == []                          # discovery is keyless
    by_kind = {k: [conn.missing_credentials(aoi, r) for r in refs if r.meta["kind"] == k] for k in ("archive", "api")}
    assert by_kind["archive"] and all(m == [] for m in by_kind["archive"])
    assert by_kind["api"] and all(m == ["FIRMS_MAP_KEY"] for m in by_kind["api"])
    monkeypatch.setenv("FIRMS_MAP_KEY", "map-key-for-tests")
    assert all(conn.missing_credentials(aoi, r) == [] for r in refs)


def test_discover_ignores_future_part_of_period(lake):
    conn = FirmsConnector(load_dataset("firms"), lake, Settings.from_env())
    conn.http_session = lambda bearer_env=None: FakeFirms()
    today = date.today()
    future = Period(date(today.year + 1, 1, 1), date(today.year + 1, 3, 1))
    assert conn.discover(load_aoi("smm"), future) == []
    refs = conn.discover(load_aoi("smm"), Period(date(today.year, today.month, 1), date(today.year + 1, 2, 1)))
    assert refs and all(r.datetime.date() <= today for r in refs)


def test_api_unit_without_key_fails_clearly(lake, monkeypatch):
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
    conn = FirmsConnector(load_dataset("firms"), lake, Settings.from_env())
    conn.http_session = lambda bearer_env=None: FakeFirms()
    ref = next(r for r in conn.discover(load_aoi("smm"), Period(date(2019, 1, 1), date(2019, 1, 31)))
               if r.meta["kind"] == "api")
    with pytest.raises(RuntimeError, match="FIRMS_MAP_KEY not set"):
        conn.extract(ref, load_aoi("smm"))


def test_api_error_text_is_redacted(lake, monkeypatch):
    monkeypatch.setenv("FIRMS_MAP_KEY", "SECRETKEY123456")
    conn = FirmsConnector(load_dataset("firms"), lake, Settings.from_env())

    class EchoKey(FakeFirms):
        def get(self, url, stream=False, timeout=None):
            return FakeResponse(400, f"Invalid MAP_KEY. url={url}")

    conn.http_session = lambda bearer_env=None: EchoKey()
    ref = next(r for r in conn.discover(load_aoi("smm"), Period(date(2019, 1, 1), date(2019, 1, 31)))
               if r.meta["kind"] == "api")
    with pytest.raises(RuntimeError) as exc:
        conn.extract(ref, load_aoi("smm"))
    assert "SECRETKEY123456" not in str(exc.value) and "***" in str(exc.value)


def test_api_unit_with_key_merges_chunks(lake, monkeypatch):
    monkeypatch.setenv("FIRMS_MAP_KEY", "SECRETKEY123456")
    conn = FirmsConnector(load_dataset("firms"), lake, Settings.from_env())
    seen: list[str] = []

    class Api(FakeFirms):
        def get(self, url, stream=False, timeout=None):
            seen.append(url)
            return FakeResponse(200, API)

    conn.http_session = lambda bearer_env=None: Api()
    aoi = load_aoi("smm")
    ref = next(r for r in conn.discover(aoi, Period(date(2019, 1, 1), date(2019, 1, 31)))
               if r.meta["kind"] == "api" and r.meta["product"] == "VIIRS_NOAA20_NRT")
    raw = conn.extract(ref, aoi)
    assert len(seen) == 7 and seen[0].endswith("/5/2019-01-01") and seen[-1].endswith("/1/2019-01-31")
    assert "SECRETKEY123456" not in lake.read_bytes(raw[0]).decode()
    (item,) = conn.transform(ref, raw, aoi)
    assert item.item_id == "firms__smm__20190101__viirs_noaa20_nrt"
    assert len(read_table(lake, "fire_detections", "firms")) == 2   # duplicates across chunks removed


def test_pipeline_offline_archive_run_is_idempotent(lake, monkeypatch):
    fake = FakeFirms()
    monkeypatch.setattr(FirmsConnector, "http_session", lambda self, bearer_env=None: fake)
    period = Period(date(2018, 1, 1), date(2018, 12, 31))
    rep = pipeline.run(["firms"], "smm", period, lake=lake)
    assert rep.ok, rep.to_dict()
    ids = sorted(i["id"] for i in catalog.iter_items(lake, "firms"))
    assert ids == ["firms__smm__20180101__modis", "firms__smm__20180101__viirs-snpp"]
    assert all(r.ok for r in validate_collection(lake, load_dataset("firms")))
    assert len(read_table(lake, "fire_detections", "firms")) == 7
    n_gets = len(fake.gets)
    rep2 = pipeline.run(["firms"], "smm", period, lake=lake)
    assert rep2.ok and rep2.counts()["skipped"] == 2 and len(fake.gets) == n_gets


def test_archive_is_cached_outside_the_lake_and_region_filtered(lake, monkeypatch, tmp_path):
    """On the statewide AOI ca the bbox holds Nevada too: the region polygon removes it before raw/."""
    reno = "39.5296,-119.8138,320.0,1.0,1.0,2018-07-01,2030,Aqua,MODIS,90,6.03,290.0,20.0,D,0"
    offshore = "33.9000,-120.9000,320.0,1.0,1.0,2018-07-01,2030,Aqua,MODIS,90,6.03,290.0,20.0,D,0"
    fake = FakeFirms()
    text = MODIS.rstrip("\n") + f"\n{reno}\n{offshore}\n"
    fake.get = lambda url, stream=False, timeout=None: (fake.gets.append(url) or FakeResponse(200, text))
    conn = FirmsConnector(load_dataset("firms"), lake, Settings.from_env())
    conn.http_session = lambda bearer_env=None: fake
    ca = load_aoi("ca")
    ref = next(r for r in conn.discover(ca, Period(date(2018, 1, 1), date(2018, 12, 31)))
               if r.meta.get("product") == "modis")
    raw = conn.extract(ref, ca)
    kept = lake.read_text(raw[0]).splitlines()
    assert len(kept) == 1 + 4 and not any("-119.8138" in ln or "-120.9000" in ln or "-90.2127" in ln for ln in kept)
    cached = conn.archive_cache_path(ref)
    assert cached.is_file() and str(tmp_path / "download-cache") in str(cached)       # FUELLAKE_CACHE_DIR
    assert "-119.8138" in cached.read_text()                                          # the full file is there
    assert not [p for p in lake.find("raw") if p.endswith("United_States.csv")]
    n = len(fake.gets)
    other = FirmsConnector(load_dataset("firms"), lake, Settings.from_env())
    other.http_session = lambda bearer_env=None: fake
    other.extract(ref, load_aoi("smm"))                                               # second AOI: from the cache
    assert len(fake.gets) == n


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_country_archive_small_file(tmp_path):
    """MODIS 2000 is the smallest US archive file (about 0.3 MB): exercise the real extract + transform."""
    lake = Lake(tmp_path / "live")
    conn = FirmsConnector(load_dataset("firms"), lake, Settings.from_env())
    aoi = load_aoi("smm")
    refs = conn.discover(aoi, Period(date(2000, 1, 1), date(2000, 12, 31)))
    modis = [r for r in refs if r.meta.get("product") == "modis"]
    assert len(modis) == 1 and modis[0].meta["kind"] == "archive" and modis[0].meta["size_bytes"] < 2_000_000
    raw = conn.extract(modis[0], aoi)
    assert lake.read_bytes(raw[0]).decode().startswith("latitude,longitude,brightness")
    (item,) = conn.transform(modis[0], raw, aoi)
    assert item.item_id == "firms__smm__20000101__modis"


@pytest.mark.network
def test_live_api_rejects_missing_key():
    import requests

    r = requests.get("https://firms.modaps.eosdis.nasa.gov/api/area/csv/INVALIDKEY0000/VIIRS_SNPP_NRT/"
                     "-118.95,33.98,-118.50,34.20/1/2026-11-10", headers={"User-Agent": "fuel-lake/0.1"}, timeout=60)
    assert "Invalid MAP_KEY" in r.text
