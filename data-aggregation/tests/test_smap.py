# fuel-lake — tests for the SMAP L4 (SPL4SMGP) connector: offline fixtures + live CMR/OPeNDAP checks
# Licensed under Apache-2.0.
"""Fixtures in ``tests/fixtures/smap`` are real: two CMR ``granules.umm_json`` responses (2026-06-01..02, all
slots; the 2023-10-09T13:30 slot that exists in two VersionID streams) and the two OPeNDAP DAP4 subsets for
AOI ``smm`` (2026-06-01 and 2026-06-02 at 13:30 UTC, variables sm_surface, sm_rootzone, sm_profile)."""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import unquote

import numpy as np
import pytest
import yaml

from fuellake import catalog, pipeline
from fuellake.config import Settings
from fuellake.connectors.base import Period
from fuellake.connectors.nisar_sme2 import EASE2_UL_X, EASE2_UL_Y, ease2_centre
from fuellake.connectors.smap import (
    EASE2_9KM_CELL,
    EASE2_9KM_COLS,
    EASE2_9KM_ROWS,
    SmapAuthError,
    SmapL4Connector,
    aoi_window,
    check_subset,
    dap4_constraint,
    dap4_url,
    ease2_window,
    granule_digest,
    layer_values,
    parse_gph_item,
    parse_gph_name,
    read_dap_subset,
    sample_time_tokens,
    select_granules,
    subset_time,
    wants_token,
    window_bounds_wgs84,
    window_centres,
)
from fuellake.contracts import validate_collection
from fuellake.rasters import open_zarr
from fuellake.registry import load_aoi, load_dataset
from fuellake.storage import Lake

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
FIX = Path(__file__).parent / "fixtures" / "smap"
CMR_JUNE = FIX / "cmr_spl4smgp_20260601_02.json"
CMR_DUP = FIX / "cmr_spl4smgp_20231009T1330_duplicates.json"
SUBSETS = {"20260601": FIX / "SMAP_L4_SM_gph_20260601T133000_Vv8013_001__smm.nc4",
           "20260602": FIX / "SMAP_L4_SM_gph_20260602T133000_Vv8013_001__smm.nc4"}
SOURCES = ["Geophysical_Data/sm_surface", "Geophysical_Data/sm_rootzone", "Geophysical_Data/sm_profile"]
SMM_WINDOW = (354, 359, 652, 660)      # global EASE-Grid 2.0 9 km rows / cols (stop exclusive), checked live


def cmr_items(path: Path) -> list[dict]:
    return json.loads(path.read_text())["items"]


class FakeResponse:
    def __init__(self, status: int = 200, content: bytes = b"", payload: dict | None = None,
                 headers: dict | None = None):
        self.status_code, self.content, self._payload = status, content, payload
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> dict:
        return self._payload if self._payload is not None else json.loads(self.content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeCmr:
    """Serves the June fixture for every CMR granule query (search and newest-first)."""

    def __init__(self, items: list[dict] | None = None):
        self.items = items if items is not None else cmr_items(CMR_JUNE)
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append(dict(params or {}))
        items = list(self.items)
        if (params or {}).get("sort_key") == "-start_date":
            items = items[::-1]
        return FakeResponse(200, payload={"items": items}, headers={"CMR-Hits": str(len(items))})


class FakeOpendap:
    """Serves the fixture subset matching the granule time in the URL (404 for anything else)."""

    def __init__(self, status: int = 200, body: bytes | None = None):
        self.status, self.body, self.urls = status, body, []

    def get(self, url, timeout=None, **kw):
        self.urls.append(unquote(url))
        if self.status != 200:
            return FakeResponse(self.status, b"error")
        if self.body is not None:
            return FakeResponse(200, self.body)
        for day, path in SUBSETS.items():
            if f"_{day}T133000_" in url:
                return FakeResponse(200, path.read_bytes())
        return FakeResponse(404, b"not found")


@pytest.fixture
def smap_config(tmp_path, monkeypatch) -> Path:
    """Config dir with the real smm AOI and the real smap-l4.yaml (no lake region)."""
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "aois" / "smm.yaml").write_text((REPO_CONFIG / "aois" / "smm.yaml").read_text())
    (root / "datasets" / "smap-l4.yaml").write_text((REPO_CONFIG / "datasets" / "smap-l4.yaml").read_text())
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


def make_connector(lake: Lake, cmr: FakeCmr | None = None, opendap: FakeOpendap | None = None) -> SmapL4Connector:
    conn = SmapL4Connector(load_dataset("smap-l4"), lake, Settings.from_env())
    conn.cmr_session = lambda: cmr or FakeCmr()
    conn.opendap_session = lambda: opendap or FakeOpendap()
    return conn


# ---------------------------------------------------------------------------- config
def test_config_declares_verified_paths_and_options():
    ds = load_dataset("smap-l4")
    assert ds.connector == "fuellake.connectors.smap:SmapL4Connector" and ds.kind == "raster-b"
    assert {v.name: v.model_extra.get("source_name") for v in ds.variables} == {
        "sm_surface": SOURCES[0], "sm_rootzone": SOURCES[1], "sm_profile": SOURCES[2], "smap_sm": None}
    assert ds.variable("smap_sm").model_extra["alias_of"] == "sm_surface"      # team feature, provisional
    assert all(v.unit == "m3/m3" and tuple(v.valid_range) == (0.0, 0.9) for v in ds.variables)
    assert all(v.lag is True for v in ds.variables)          # read by features.matchups.config_lag_variables
    assert ds.options["concept_id"] == "C3480440870-NSIDC_CPRD" and ds.options["version"] == "008"
    assert ds.access.auth == "earthdata" and ds.access.env == ["EARTHDATA_TOKEN"]
    assert set(ds.scales) == {"local", "region"}
    assert ds.license.redistributable is True and "10.5067/T5RUATAQREF8" in ds.license.attribution


# ---------------------------------------------------------------------------- EASE-Grid 2.0 9 km
def test_ease2_9km_grid_definition():
    assert EASE2_9KM_CELL == pytest.approx(9008.055210146, abs=1e-6)
    # the 1624 rows span the NSIDC y extent of the global EASE-Grid 2.0 (corners rounded there to 1 cm)
    assert EASE2_9KM_ROWS * EASE2_9KM_CELL == pytest.approx(2 * EASE2_UL_Y, abs=0.01)
    assert EASE2_9KM_COLS * EASE2_9KM_CELL == pytest.approx(2 * abs(EASE2_UL_X), abs=1e-6)
    x0, y0 = ease2_centre(0, 0, EASE2_9KM_CELL)
    assert float(x0) == pytest.approx(-17363026.418, abs=1e-3) and float(y0) == pytest.approx(7310036.803, abs=1e-3)
    # nested with the 1 km EASE-Grid 2.0 (34704 columns): one 9 km cell = 9 x 9 cells of 1 km
    assert 34704 % EASE2_9KM_COLS == 0 and 34704 // EASE2_9KM_COLS == 9


def test_window_for_smm_matches_live_check():
    aoi = load_aoi("smm")
    assert aoi_window(aoi, buffer_cells=1) == SMM_WINDOW
    assert aoi_window(aoi, buffer_cells=0) == (355, 358, 653, 659)
    x, y = window_centres(SMM_WINDOW)
    assert x.shape == (8,) and y.shape == (5,)
    assert np.allclose(np.diff(x), EASE2_9KM_CELL) and np.allclose(np.diff(y), -EASE2_9KM_CELL)
    w, s, e, n = window_bounds_wgs84(SMM_WINDOW)
    bw, bs, be, bn = aoi.bbox
    # buffer: one 9 km cell (0.0934 degree of longitude) beyond the bbox on every side, never two
    assert bw - 2 * 0.0934 < w < bw - 0.0934 + 1e-6 and be + 0.0934 - 1e-6 < e < be + 2 * 0.0934
    assert s < bs and n > bn


def test_ease2_window_edges_and_clipping():
    cell = EASE2_9KM_CELL
    # a box covering exactly cells 10..11 (edges on cell boundaries) does not pull in 9 or 12
    bounds = (EASE2_UL_X + 10 * cell, EASE2_UL_Y - 12 * cell, EASE2_UL_X + 12 * cell, EASE2_UL_Y - 10 * cell)
    assert ease2_window(bounds, buffer_cells=0) == (10, 12, 10, 12)
    assert ease2_window(bounds, buffer_cells=1) == (9, 13, 9, 13)
    # a point-sized box inside one cell
    inner = (EASE2_UL_X + 10.2 * cell, EASE2_UL_Y - 10.8 * cell, EASE2_UL_X + 10.3 * cell, EASE2_UL_Y - 10.7 * cell)
    assert ease2_window(inner, buffer_cells=0) == (10, 11, 10, 11)
    # clipped at the grid edge
    corner = (EASE2_UL_X + 1, EASE2_UL_Y - 2 * cell, EASE2_UL_X + 2 * cell, EASE2_UL_Y - 1)
    assert ease2_window(corner, buffer_cells=3) == (0, 5, 0, 5)
    with pytest.raises(ValueError):
        ease2_window((0, 0, 0, 0))


# ---------------------------------------------------------------------------- names, slots, selection
def test_parse_gph_name_with_and_without_suffix():
    a = parse_gph_name("SMAP_L4_SM_gph_20261110T223000_Vv8013_001.h5_u9loH5HD")
    b = parse_gph_name("https://x/SMAP/SPL4SMGP/008/2019/07/01/SMAP_L4_SM_gph_20190701T133000_Vv8010_001.h5")
    assert a["stem"] == "SMAP_L4_SM_gph_20261110T223000_Vv8013_001" and a["version"] == "Vv8013"
    assert a["dt"] == datetime(2026, 11, 10, 22, 30, tzinfo=UTC) and a["suffix"] == "_u9loH5HD"
    assert b["dt"] == datetime(2019, 7, 1, 13, 30, tzinfo=UTC) and b["suffix"] is None and b["counter"] == 1
    assert parse_gph_name("SMAP_L4_SM_aup_20190701T120000_Vv8010_001.h5") is None
    assert parse_gph_name("SMAP_L4_SM_gph_20190701T133000_Vv8010_001.h5.iso.xml") is None


def test_sample_time_tokens_validate_slots():
    assert sample_time_tokens(["13:30"]) == ["133000"]
    assert sample_time_tokens(["22:30", "01:30", "13:30"]) == ["013000", "133000", "223000"]
    for bad in (["12:00"], ["14:30"], ["1330"], []):
        with pytest.raises(ValueError):
            sample_time_tokens(bad)


def test_parse_gph_item_keeps_the_opendap_link():
    g = parse_gph_item(cmr_items(CMR_JUNE)[5])
    assert g.stem == "SMAP_L4_SM_gph_20260601T133000_Vv8013_001" and g.ur.endswith(".h5_u9loH5HD")
    assert g.opendap == ("https://opendap.earthdata.nasa.gov/collections/C3480440870-NSIDC_CPRD/granules/"
                         "SMAP_L4_SM_gph_20260601T133000_Vv8013_001.h5_u9loH5HD")
    assert g.https.endswith("/SPL4SMGP/008/2026/06/01/SMAP_L4_SM_gph_20260601T133000_Vv8013_001.h5")
    assert g.day == date(2026, 6, 1) and g.version == "Vv8013"


def test_select_granules_one_slot_per_day_and_date_filter():
    items = cmr_items(CMR_JUNE)          # 17 records: 2026-05-31T22:30 + 8 slots on each of two days
    grans = select_granules(items, ["13:30"])
    assert [g.time for g in grans] == ["2026-06-01T13:30:00+00:00", "2026-06-02T13:30:00+00:00"]
    two = select_granules(items, ["13:30", "22:30"], first=date(2026, 6, 1))
    assert [g.dt.strftime("%m%dT%H%M") for g in two] == ["0601T1330", "0601T2230", "0602T1330", "0602T2230"]
    assert select_granules(items, ["13:30"], last=date(2026, 5, 31)) == []


def test_select_granules_keeps_highest_version_id():
    grans = select_granules(cmr_items(CMR_DUP), ["13:30"])
    assert len(grans) == 1 and grans[0].version == "Vv8012"
    assert grans[0].ur == "SMAP_L4_SM_gph_20231009T133000_Vv8012_001.h5_u9loH5HD"
    # order of the CMR records does not matter
    assert select_granules(cmr_items(CMR_DUP)[::-1], ["13:30"])[0].version == "Vv8012"


def test_dap4_constraint_uses_inclusive_ranges():
    ce = dap4_constraint(SOURCES[:1], SMM_WINDOW)
    assert ce == "/x[652:659];/y[354:358];/time;/Geophysical_Data/sm_surface[354:358][652:659]"
    url = dap4_url("https://opendap.earthdata.nasa.gov/collections/C/granules/G.h5_s", SOURCES[:1], SMM_WINDOW)
    assert url.startswith("https://opendap.earthdata.nasa.gov/collections/C/granules/G.h5_s.dap.nc4?dap4.ce=")
    assert "%5B652%3A659%5D" in url and "%3B" in url and "/Geophysical_Data/sm_surface" in url


def test_token_only_for_data_hosts():
    assert wants_token("https://opendap.earthdata.nasa.gov/collections/C/granules/G")
    assert wants_token("https://data.nsidc.earthdatacloud.nasa.gov/nsidc-cumulus-prod-protected/x.h5")
    assert not wants_token("https://cmr.earthdata.nasa.gov/search/granules.umm_json")
    assert not wants_token("http://opendap.earthdata.nasa.gov/collections/C/granules/G")
    assert not wants_token("https://opendap.earthdata.nasa.gov.evil.example/x")


# ---------------------------------------------------------------------------- HDF5 / netCDF-4 parsing
def test_read_real_subset_and_check_georeferencing():
    sub = read_dap_subset(SUBSETS["20260601"], SOURCES)
    assert sub["x"].shape == (8,) and sub["y"].shape == (5,)
    assert sub["attrs"]["Geophysical_Data/sm_rootzone"] == {"_FillValue": -9999.0, "valid_min": 0.0,
                                                            "valid_max": pytest.approx(0.9), "units": "m3 m-3"}
    check_subset(sub, SMM_WINDOW, SOURCES)                  # file centres within 5 m of the analytic ones
    xe, ye = window_centres(SMM_WINDOW)
    assert 0.1 < float(np.max(np.abs(sub["x"] - xe))) < 1.0   # the file's float32-rounded centres
    with pytest.raises(ValueError, match="georeferencing mismatch"):
        check_subset(sub, (354, 359, 653, 661), SOURCES)    # one cell east: 9 km off
    with pytest.raises(ValueError, match="shape"):
        check_subset(sub, (354, 360, 652, 660), SOURCES)
    assert subset_time(sub["time"]) == datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    sm = layer_values(sub, SOURCES[1])
    assert sm.dtype == np.float32 and np.isnan(sm).any()     # ocean cells in Santa Monica Bay: fill -> NaN
    finite = sm[np.isfinite(sm)]
    assert 0.0 < finite.min() and finite.max() < 0.6
    with pytest.raises(KeyError):
        read_dap_subset(SUBSETS["20260601"], ["Geophysical_Data/sm_rootzone_pctl"])


# ---------------------------------------------------------------------------- discovery and month units
def test_discover_groups_months_and_marks_the_open_month(smap_config, lake):
    conn = make_connector(lake)
    refs = conn.discover(load_aoi("smm"), Period(date(2026, 5, 20), date(2026, 7, 10)))
    # newest fixture granule is 2026-06-02: June is the open month, May has no granule of the slot, July is later
    assert len(refs) == 1
    ref = refs[0]
    grans = select_granules(cmr_items(CMR_JUNE), ["13:30"])
    assert ref.key == f"smap-l4-202606-through-20260602-{granule_digest(grans)}"
    assert ref.meta["complete"] is False and ref.meta["first_day"] == "2026-06-01"
    assert ref.meta["last_day"] == "2026-06-02" and ref.meta["n_granules"] == 2
    assert ref.meta["expected_granules"] == 2 and ref.meta["window"] == list(SMM_WINDOW)
    assert ref.datetime == datetime(2026, 6, 1, tzinfo=UTC) and len(ref.urls) == 2
    assert all(u.startswith("https://opendap.earthdata.nasa.gov/") for u in ref.urls)


def test_month_key_is_complete_when_data_reach_past_month_end(smap_config, lake):
    items = cmr_items(CMR_JUNE)
    later = json.loads(json.dumps(items[-1]))               # pretend a July granule exists (newest-first query)
    later["umm"]["GranuleUR"] = later["umm"]["GranuleUR"].replace("20260602T223000", "20260701T133000")
    for u in later["umm"]["RelatedUrls"]:
        u["URL"] = u["URL"].replace("20260602T223000", "20260701T133000")
    cmr = FakeCmr(items + [later])
    conn = make_connector(lake, cmr=cmr)
    refs = conn.discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 6, 30)))
    assert len(refs) == 1 and refs[0].meta["complete"] is True
    assert refs[0].key.startswith("smap-l4-202606-") and "through" not in refs[0].key
    # two months: June complete, July open through its only day; each unit holds only its own granules
    june, july = conn.discover(load_aoi("smm"), Period(date(2026, 6, 15), date(2026, 7, 31)))
    assert june.key == refs[0].key and june.meta["n_granules"] == 2 and june.meta["first_day"] == "2026-06-01"
    assert july.key.startswith("smap-l4-202607-through-20260701-") and july.meta["complete"] is False
    assert july.meta["first_day"] == "2026-07-01" and july.meta["n_granules"] == 1
    assert july.meta["granules"][0]["stem"] == "SMAP_L4_SM_gph_20260701T133000_Vv8013_001"
    assert july.datetime == datetime(2026, 7, 1, tzinfo=UTC)
    q = cmr.calls[-1]
    assert q["granule_ur[]"] == ["SMAP_L4_SM_gph_*T133000_*"] and q["options[granule_ur][pattern]"] == "true"
    assert "Authorization" not in json.dumps(q)


def test_digest_changes_with_the_granule_list():
    a = select_granules(cmr_items(CMR_JUNE), ["13:30"])
    assert granule_digest(a) == granule_digest(list(a))
    assert granule_digest(a) != granule_digest(a[:1])


# ---------------------------------------------------------------------------- extract / transform / pipeline
def test_missing_token_is_reported_and_refused(smap_config, lake):
    conn = make_connector(lake)
    aoi = load_aoi("smm")
    ref = conn.discover(aoi, Period(date(2026, 6, 1), date(2026, 6, 2)))[0]
    assert conn.missing_credentials(aoi) == []
    assert conn.missing_credentials(aoi, ref) == ["EARTHDATA_TOKEN"]
    with pytest.raises(SmapAuthError, match="EARTHDATA_TOKEN is not set"):
        conn.extract(ref, aoi)


def test_rejected_token_and_html_pages_raise(smap_config, lake, monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "dummy-offline-test-token")
    aoi = load_aoi("smm")
    for fake, msg in ((FakeOpendap(status=401), "token rejected"),
                      (FakeOpendap(body=b"<html>login</html>"), "HTML page")):
        conn = make_connector(lake, opendap=fake)
        ref = conn.discover(aoi, Period(date(2026, 6, 1), date(2026, 6, 2)))[0]
        with pytest.raises(SmapAuthError, match=msg):
            conn.extract(ref, aoi)


def test_pipeline_offline_run_is_idempotent(smap_config, lake, monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "dummy-offline-test-token")
    fake = FakeOpendap()
    monkeypatch.setattr(SmapL4Connector, "cmr_session", lambda self: FakeCmr())
    monkeypatch.setattr(SmapL4Connector, "opendap_session", lambda self: fake)
    period = Period(date(2026, 6, 1), date(2026, 6, 2))
    rep = pipeline.run(["smap-l4"], "smm", period, lake=lake)
    assert rep.ok, rep.to_dict()
    assert rep.counts()["created"] == 1 and len(fake.urls) == 2
    assert all("/Geophysical_Data/sm_rootzone[354:358][652:659]" in u for u in fake.urls)
    items = list(catalog.iter_items(lake, "smap-l4"))
    assert [i["id"] for i in items] == ["smap-l4__smm__20260601"]
    p = items[0]["properties"]
    assert p["fuellake:source_ids"][0] == "SMAP_L4_SM_gph_20260601T133000_Vv8013_001.h5_u9loH5HD"
    assert p["smap:version_ids"] == ["Vv8013"] and p["smap:missing_days"] == []
    assert p["smap:shape"] == [2, 5, 8] and p["smap:window"] == list(SMM_WINDOW)
    assert p["smap:complete_month"] is False and p["smap:raw_bytes"] > 0
    assert items[0]["properties"]["start_datetime"].startswith("2026-06-01")
    assert items[0]["properties"]["end_datetime"].startswith("2026-06-02T23:59:59")
    ds = open_zarr(lake, catalog.asset_rel(items[0], "data"))
    assert set(ds.data_vars) == {"sm_surface", "sm_rootzone", "sm_profile", "smap_sm"}
    assert ds["sm_rootzone"].dims == ("time", "y", "x") and ds["sm_rootzone"].dtype == np.float32
    assert ds.rio.crs.to_epsg() == 6933 and ds["sm_rootzone"].attrs["units"] == "m3/m3"
    assert [str(t)[:16] for t in ds["time"].values] == ["2026-06-01T13:30", "2026-06-02T13:30"]
    assert np.allclose(ds["x"].values, window_centres(SMM_WINDOW)[0])
    assert all(r.ok for r in validate_collection(lake, load_dataset("smap-l4")))
    assert lake.exists("raw/smap-l4/2026/06/SMAP_L4_SM_gph_20260601T133000_Vv8013_001__smm.nc4")

    rep2 = pipeline.run(["smap-l4"], "smm", period, lake=lake)
    assert rep2.ok and rep2.counts()["skipped"] == 1 and rep2.counts()["created"] == 0
    # forced rerun reuses the raw subsets (no new requests)
    rep3 = pipeline.run(["smap-l4"], "smm", period, lake=lake, force=True)
    assert rep3.ok and len(fake.urls) == 2


def test_weather_series_reads_the_store_at_a_site(smap_config, lake, monkeypatch):
    from fuellake.features._common import WeatherSeries

    monkeypatch.setenv("EARTHDATA_TOKEN", "dummy-offline-test-token")
    monkeypatch.setattr(SmapL4Connector, "cmr_session", lambda self: FakeCmr())
    monkeypatch.setattr(SmapL4Connector, "opendap_session", lambda self: FakeOpendap())
    assert pipeline.run(["smap-l4"], "smm", Period(date(2026, 6, 1), date(2026, 6, 2)), lake=lake).ok
    items = list(catalog.iter_items(lake, "smap-l4"))
    series = WeatherSeries(lake, load_dataset("smap-l4"), items)
    assert series.variable_names() == ["sm_surface", "sm_rootzone", "sm_profile", "smap_sm"]
    assert series.days() == [date(2026, 6, 1), date(2026, 6, 2)]
    lon, lat = np.array([-118.80, -118.6]), np.array([34.10, 34.12])        # two chaparral sites in smm
    vals = series.points("sm_rootzone", date(2026, 6, 2), lon, lat)
    assert np.all(np.isfinite(vals)) and np.all((vals > 0.0) & (vals < 0.6))
    assert np.all(np.isnan(series.points("sm_rootzone", date(2026, 6, 3), lon, lat)))
    # the cube path: EPSG:6933 store regridded onto the AOI's UTM grid (1 km here to keep the test fast)
    grid = load_aoi("smm").grid_at(1000)
    field = series.grid_slice("sm_rootzone", date(2026, 6, 2), grid)
    assert field.shape == (grid.height, grid.width) and field.dtype == np.float32
    finite = field[np.isfinite(field)]
    assert finite.size > 0.5 * field.size and 0.0 < finite.min() and finite.max() < 0.6
    assert series.grid_slice("sm_rootzone", date(2026, 6, 3), grid) is None


def test_changed_window_invalidates_the_raw_cache(smap_config, lake, monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "dummy-offline-test-token")
    conn = make_connector(lake)
    aoi = load_aoi("smm")
    ref = conn.discover(aoi, Period(date(2026, 6, 1), date(2026, 6, 2)))[0]
    raw = conn.extract(ref, aoi)
    assert conn._cached_ok(raw[0], SMM_WINDOW, SOURCES)
    assert not conn._cached_ok(raw[0], (354, 359, 651, 659), SOURCES)
    assert not conn._cached_ok(raw[0], SMM_WINDOW, SOURCES + ["Geophysical_Data/surface_temp"])
    with io.BytesIO(SUBSETS["20260601"].read_bytes()[:5000]) as truncated:
        lake.write_bytes(raw[0], truncated.read())
    assert not conn._cached_ok(raw[0], SMM_WINDOW, SOURCES)


def test_time_mismatch_fails_the_unit(smap_config, lake, monkeypatch):
    """A subset whose /time disagrees with the file name (wrong granule served) is refused."""
    monkeypatch.setenv("EARTHDATA_TOKEN", "dummy-offline-test-token")
    conn = make_connector(lake, opendap=FakeOpendap(body=SUBSETS["20260601"].read_bytes()))
    aoi = load_aoi("smm")
    ref = conn.discover(aoi, Period(date(2026, 6, 1), date(2026, 6, 2)))[0]
    raw = conn.extract(ref, aoi)
    with pytest.raises(ValueError, match="does not match the file name"):
        conn.transform(ref, raw, aoi)


def test_l4_alias_variable_is_a_copy(smap_config, lake, monkeypatch):
    """The team feature smap_sm is ``alias_of: sm_surface`` in smap-l4.yaml (provisional mapping)."""
    monkeypatch.setenv("EARTHDATA_TOKEN", "dummy-offline-test-token")
    fake = FakeOpendap()
    conn = make_connector(lake, opendap=fake)
    assert "smap_sm" in [v.name for v in conn.dataset.variables]     # declared in smap-l4.yaml
    aoi = load_aoi("smm")
    ref = conn.discover(aoi, Period(date(2026, 6, 1), date(2026, 6, 2)))[0]
    assert ref.meta["sources"] == SOURCES and conn.request_paths() == SOURCES     # the alias is not requested
    (item,) = conn.transform(ref, conn.extract(ref, aoi), aoi)
    assert all("smap_sm" not in u for u in fake.urls)
    ds = open_zarr(lake, item.assets[0].path)
    np.testing.assert_array_equal(ds["smap_sm"].values, ds["sm_surface"].values)
    assert ds["smap_sm"].attrs["fuellake_alias_of"] == "sm_surface"
    assert item.properties["smap:variables"] == ["sm_surface", "sm_rootzone", "sm_profile", "smap_sm"]


def test_config_variables_are_written_in_yaml_order():
    data = yaml.safe_load((REPO_CONFIG / "datasets" / "smap-l4.yaml").read_text())
    assert [v["name"] for v in data["variables"]] == ["sm_surface", "sm_rootzone", "sm_profile", "smap_sm"]
    assert data["options"]["sample_times_utc"] == ["13:30"]


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_cmr_discovery_finds_one_granule_per_day(tmp_path):
    lake = Lake(tmp_path / "live")
    conn = SmapL4Connector(load_dataset("smap-l4"), lake, Settings.from_env())
    latest = conn.latest_day()
    assert latest is not None and (date.today() - latest).days < 15, latest
    refs = conn.discover(load_aoi("smm"), Period(date(2019, 7, 1), date(2019, 7, 31)))
    assert len(refs) == 1 and refs[0].meta["complete"] is True
    assert refs[0].meta["n_granules"] == 31 and refs[0].meta["versions"] == ["Vv8010"]
    assert all("opendap.earthdata.nasa.gov" in u for u in refs[0].urls)


@pytest.mark.earthdata
def test_live_opendap_subset_for_smm(tmp_path):
    if not Settings.has_secret("EARTHDATA_TOKEN"):
        pytest.skip("EARTHDATA_TOKEN not set")
    lake = Lake(tmp_path / "live")
    conn = SmapL4Connector(load_dataset("smap-l4"), lake, Settings.from_env())
    aoi = load_aoi("smm")
    ref = conn.discover(aoi, Period(date(2019, 7, 1), date(2019, 7, 31)))[0]
    g = ref.meta["granules"][0]
    body = conn.fetch_subset(g, aoi_window(aoi), conn.sources())
    assert body[:4] == b"\x89HDF" and len(body) < 200_000
    sub = read_dap_subset(io.BytesIO(body), conn.sources())
    assert subset_time(sub["time"]) == datetime(2019, 7, 1, 13, 30, tzinfo=UTC)
    sm = layer_values(sub, "Geophysical_Data/sm_surface")
    assert np.isfinite(sm).any() and np.nanmax(sm) < 0.6
