# fuel-lake — tests for the Globe-LFMC 2.0 connector (offline on a fixture cut from the real file)
# Licensed under Apache-2.0.
"""``tests/fixtures/labels/globe_lfmc_sample.xlsx`` holds 16 real rows of ``Globe-LFMC-2.0 final.xlsx``
(CC0) with their original cell types (dates as datetimes, ``Isolated data point`` as the strings
``TRUE``/``FALSE``); the Contact, Reference and picture-file columns are blanked. Rows: 2 France, 1 Idaho,
5 Montana and 8 California (Blackberry Hill, Trippet Ranch). The real config's lake region is California, so
only the 8 California rows are in scope; the Montana/Idaho rows test the no-region fallback (``region_bbox``)."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path

import pytest

from fuellake import pipeline
from fuellake.config import Settings
from fuellake.connectors import globe_lfmc as gl
from fuellake.connectors.base import Period
from fuellake.contracts import validate_collection, validate_table
from fuellake.registry import load_aoi, load_dataset
from fuellake.tables import read_table

FIXTURE = Path(__file__).parent / "fixtures" / "labels" / "globe_lfmc_sample.xlsx"
FIXTURE_MD5 = hashlib.md5(FIXTURE.read_bytes(), usedforsecurity=False).hexdigest()
WEST = (-125.0, 31.0, -102.0, 49.0)


def connector(lake, **options):
    ds = load_dataset("globe-lfmc")
    if options:
        ds = ds.model_copy(update={"options": {**ds.options, **options}})
    return gl.GlobeLfmcConnector(ds, lake, Settings.from_env())


@pytest.fixture
def cached_fixture(lake, monkeypatch):
    """Put the fixture in the local download cache (outside the lake) and make the md5 check accept it; no
    downloads. Returns the cache path."""
    path = connector(lake).cache_file()
    path.write_bytes(FIXTURE.read_bytes())
    monkeypatch.setattr(gl.GlobeLfmcConnector, "expected_md5", lambda self: FIXTURE_MD5)

    def no_download(self, path, md5):
        raise AssertionError("must not download")

    monkeypatch.setattr(gl.GlobeLfmcConnector, "_download", no_download)
    return path


@pytest.fixture
def no_region(tmp_path, monkeypatch):
    """The real config without config/lake.yaml: a lake without region (region_bbox fallback)."""
    import shutil

    real = Path(__file__).resolve().parents[1] / "config"
    root = tmp_path / "config-no-region"
    shutil.copytree(real / "aois", root / "aois")
    shutil.copytree(real / "datasets", root / "datasets")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


# ---------------------------------------------------------------------------- helpers
def test_is_isolated_reads_strings():
    assert gl.is_isolated("TRUE") and gl.is_isolated(" true ") and gl.is_isolated(True)
    assert not gl.is_isolated("FALSE") and not gl.is_isolated(None) and not gl.is_isolated(False)


@pytest.mark.parametrize("value, expected", [
    (datetime(2019, 4, 1), date(2019, 4, 1)), (20190401, date(2019, 4, 1)), ("20190401", date(2019, 4, 1)),
    ("2019-04-01", date(2019, 4, 1)), (None, None), ("garbage", None),
])
def test_parse_sampling_date(value, expected):
    assert gl.parse_sampling_date(value) == expected


@pytest.mark.parametrize("ftype, expected", [
    ("Tree", "tree"), ("Small tree", "tree"), ("Shrub/Small tree", "shrub"), ("Subshrub", "shrub"),
    ("Grass", "grass"), ("Sedge", "grass"), ("Forb", "herb"), ("Moss and litter", "other"),
    (float("nan"), "unknown"), (None, "unknown"),
])
def test_functional_type_category(ftype, expected):
    assert gl.functional_type_category(ftype) == expected


def test_read_sheet_filters_region_and_keeps_types():
    df = gl.read_lfmc_sheet(str(FIXTURE), "LFMC data", WEST)
    assert len(df) == 14                                   # the two French rows are outside the region
    assert set(df["isolated"]) == {"TRUE", "FALSE"}
    assert isinstance(df["date"].iloc[0], datetime)
    with pytest.raises(KeyError):
        gl.read_lfmc_sheet(str(FIXTURE), "No such sheet", WEST)


# ---------------------------------------------------------------------------- discover
def test_discover_years_and_region_key(lake):
    c = connector(lake)
    refs = c.discover(load_aoi("ca"), Period(date(2017, 3, 1), date(2023, 12, 31)))
    assert [r.meta["year"] for r in refs] == list(range(2017, 2024))
    assert refs[0].key.startswith("globe-lfmc:45049786:2017:region-")
    assert refs[0].meta["scope"]["region"] == "california"
    assert refs[0].datetime == datetime(2017, 1, 1, tzinfo=refs[0].datetime.tzinfo)
    assert c.discover(load_aoi("ca"), Period(date(2024, 1, 1), date(2026, 11, 14))) == []
    # with a lake region, region_bbox is ignored (the polygon selects the rows)
    same = connector(lake, region_bbox=[-125.0, 24.0, -66.0, 50.0])
    assert same.discover(load_aoi("ca"), Period(date(2017, 1, 1), date(2017, 12, 31)))[0].key == refs[0].key
    assert c.subset_rel().startswith("raw/globe-lfmc/2024/03/Globe-LFMC-2.0_final__57a2c99f__california-")


def test_discover_without_region_uses_region_bbox(lake, no_region):
    refs = connector(lake).discover(load_aoi("smm"), Period(date(2017, 1, 1), date(2017, 12, 31)))
    other = connector(lake, region_bbox=[-125.0, 24.0, -66.0, 50.0])
    assert other.discover(load_aoi("smm"), Period(date(2017, 1, 1), date(2017, 12, 31)))[0].key != refs[0].key
    with pytest.raises(ValueError):
        _ = connector(lake, region_bbox=[1, 2, 0, 3]).region_bbox


# ---------------------------------------------------------------------------- transform
def test_transform_2019_keeps_california_only(lake, cached_fixture):
    c = connector(lake)
    aoi = load_aoi("ca")
    ref = c.discover(aoi, Period(date(2019, 1, 1), date(2019, 12, 31)))[0]
    raw = c.extract(ref, aoi)
    assert raw == [c.subset_rel()] and lake.exists(raw[0])
    assert not any(p.endswith(".xlsx") for p in lake.find("raw"))     # the global workbook stays outside
    subset = gl.read_subset_csv(lake.read_text(raw[0]))
    assert len(subset) == 8 and set(subset["site"]) == {"Blackberry Hill", "Trippet Ranch, Topanga"}
    item = c.transform(ref, raw, aoi)[0]
    assert item.item_id == "globe-lfmc__ca__20190101"
    part = item.assets[0].path
    assert part == "staged/tables/lfmc_samples/globe-lfmc/part-ca__2019.parquet"
    res = validate_table(lake, part, "lfmc_samples")
    assert res.ok, res.errors
    df = read_table(lake, "lfmc_samples", "globe-lfmc").set_index(["site_id", "species"])
    assert len(df) == 5 and set(df["source"]) == {"globe-lfmc"}
    assert df["status"].isna().all()

    bh = df.loc[("Blackberry Hill", "adenostoma fasciculatum")]
    assert bh.n_raw_rows == 3 and bh.lfmc_pct == pytest.approx((97 + 151 + 204) / 3)
    assert bool(bh.outlier_flag) and set(bh.qc_flags.split(";")) == {"outlier_partial", "replicates_averaged"}
    trippet = df.loc[("Trippet Ranch, Topanga", "adenostoma fasciculatum")]
    assert sorted(trippet["lfmc_pct"]) == [128.0, 158.0] and set(trippet["qc_flags"]) == {""}

    p = item.properties
    assert p["globe:n_raw_rows_in_scope"] == 7 == p["globe:n_raw_rows_collapsed"]
    assert p["globe:n_isolated_raw_rows"] == 1 and p["globe:n_site_days"] == 3
    assert p["globe:region"] == "california" and p["globe:subset"] == raw[0]


def test_transform_without_region_uses_bbox(lake, cached_fixture, no_region):
    c = connector(lake)
    aoi = load_aoi("smm")
    ref = c.discover(aoi, Period(date(2019, 1, 1), date(2019, 12, 31)))[0]
    item = c.transform(ref, c.extract(ref, aoi), aoi)[0]
    df = read_table(lake, "lfmc_samples", "globe-lfmc").set_index(["site_id", "species"])
    assert len(df) == 7                            # Montana rows inside the western-US bbox count here
    arnica = df.loc[("HENA", "arnica montana")]
    assert arnica.fuel_category == "herb" and not arnica.outlier_flag
    assert set(arnica.qc_flags.split(";")) == {"single_component", "replicates_averaged", "value_out_of_range",
                                               "source_outlier_note"}
    fir = df.loc[("HENA", "pseudotsuga menziesii")]
    assert fir.fuel_category == "tree" and fir.lfmc_pct == pytest.approx(101.25)
    assert item.properties["globe:region"] is None and item.properties["globe:region_bbox"] == list(WEST)


def test_pipeline_idempotent_offline(lake, cached_fixture):
    period = Period(date(2017, 1, 1), date(2023, 12, 31))
    refused = pipeline.run(["globe-lfmc"], "smm", period, lake=lake)       # region-scale dataset on a local AOI
    assert not refused.ok and "refused" in refused.datasets[0].error
    rep = pipeline.run(["globe-lfmc"], "ca", period, lake=lake)
    d = rep.datasets[0]
    assert rep.ok and d.discovered == 7 and d.count("created") == 7, d
    rep = pipeline.run(["globe-lfmc"], "ca", period, lake=lake)
    assert rep.ok and rep.datasets[0].skipped == 7 and not rep.datasets[0].units
    assert all(r.ok for r in validate_collection(lake, load_dataset("globe-lfmc")))
    df = read_table(lake, "lfmc_samples", "globe-lfmc")
    assert len(df) == 6                          # 2017: 1, 2019: 5 (Idaho 2018 is outside California)
    assert set(df["site_id"]) == {"Blackberry Hill", "Trippet Ranch, Topanga"}
    item = next(i for i in __import__("fuellake").catalog.iter_items(lake, "globe-lfmc"))
    assert item["properties"]["fuellake:region"] == "california"


def test_bad_cached_file_is_downloaded_again(lake, monkeypatch):
    c = connector(lake)
    path = c.cache_file()
    path.write_bytes(b"truncated")
    calls = []
    monkeypatch.setattr(gl.GlobeLfmcConnector, "_download", lambda self, p, md5: calls.append((p, md5)))
    assert c.cached_workbook() == path
    assert calls == [(path, "57a2c99f82db84810cdf6acc13aef0e4")]


def test_workbook_left_in_the_lake_moves_to_the_cache(lake, monkeypatch):
    c = connector(lake)
    legacy = c.raw_rel()
    lake.write_bytes(legacy, FIXTURE.read_bytes())
    monkeypatch.setattr(gl.GlobeLfmcConnector, "expected_md5", lambda self: FIXTURE_MD5)
    monkeypatch.setattr(gl.GlobeLfmcConnector, "_download",
                        lambda self, p, md5: (_ for _ in ()).throw(AssertionError("must not download")))
    aoi = load_aoi("ca")
    ref = c.discover(aoi, Period(date(2019, 1, 1), date(2019, 1, 31)))[0]
    assert c.extract(ref, aoi) == [c.subset_rel()]
    assert not lake.exists(legacy) and c.cache_file().read_bytes() == FIXTURE.read_bytes()


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_figshare_record_and_download_url(lake):
    import requests

    c = connector(lake)
    s = c.http_session()
    rec = s.get(c.opt("api_record"), timeout=60).json()
    assert rec["license"]["name"] == "CC0" and rec["doi"] == "10.6084/m9.figshare.25413790.v1"
    f = next(f for f in rec["files"] if f["id"] == 45049786)
    assert f["computed_md5"] == c.opt("md5") and f["size"] == 72494093
    assert f["download_url"] == c.opt("download_url")
    r = s.get(c.opt("download_url"), headers={"Range": "bytes=0-3"}, timeout=60, allow_redirects=True)
    assert r.status_code in (200, 206), r.status_code
    assert r.content[:4] == b"PK\x03\x04"           # xlsx = zip container
    assert isinstance(r, requests.Response)
