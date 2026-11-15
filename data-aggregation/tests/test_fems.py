# fuel-lake — tests for the FEMS connector (offline on fixtures; live tests marked network)
# Licensed under Apache-2.0.
"""Fixtures in ``tests/fixtures/labels``: ``fems_samples_2026-10.csv`` holds real October 2026 rows of the
Santa Monica Mountains sites (Sample Id < 900000) plus synthetic edge cases (Sample Id 9000xx);
``fems_site_metadata.csv`` and ``fems_getsites.json`` are cut from the live responses of 2026-11-14."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from fuellake import catalog, pipeline
from fuellake.config import Settings
from fuellake.connectors import fems
from fuellake.connectors._lfmc_common import RawSample, collapse_samples
from fuellake.connectors.base import Period
from fuellake.contracts import validate_collection, validate_table
from fuellake.registry import load_aoi, load_dataset
from fuellake.tables import lfmc_sample_id, read_table

FIX = Path(__file__).parent / "fixtures" / "labels"
SAMPLES_TEXT = (FIX / "fems_samples_2026-10.csv").read_text()
METADATA_TEXT = (FIX / "fems_site_metadata.csv").read_text()
SITES_PAYLOAD = json.loads((FIX / "fems_getsites.json").read_text())
SMM_BBOX = (-118.95, 33.98, -118.50, 34.20)


def connector(lake, today=date(2026, 11, 14), **options):
    ds = load_dataset("fems-lfmc")
    if options:
        ds = ds.model_copy(update={"options": {**ds.options, **options}})
    c = fems.FemsConnector(ds, lake, Settings.from_env())
    c.today = lambda: today
    return c


@pytest.fixture
def offline(monkeypatch):
    """Replace the three HTTP calls with the fixtures; count calls."""
    calls = {"sites": 0, "metadata": 0, "samples": 0}

    def fake_sites(self):
        calls["sites"] += 1
        return fems.parse_site_list(SITES_PAYLOAD)

    def fake_metadata(self, site_ids):
        calls["metadata"] += 1
        return [METADATA_TEXT]

    def fake_samples(self, urls):
        calls["samples"] += 1
        return [SAMPLES_TEXT]

    monkeypatch.setattr(fems.FemsConnector, "fetch_sites", fake_sites)
    monkeypatch.setattr(fems.FemsConnector, "fetch_site_metadata", fake_metadata)
    monkeypatch.setattr(fems.FemsConnector, "fetch_samples", fake_samples)
    return calls


# ---------------------------------------------------------------------------- pure helpers
@pytest.mark.parametrize("fuel_type, sub, expected", [
    ("Chamise", "", ("chamise", "")),
    ("Chamise, New Growth", "", ("chamise", "new")),
    ("Chamise, Old Growth", None, ("chamise", "old")),
    ("Sage, Black", "", ("black sage", "")),
    ("Mahogany, Birchleaf Mountain", "", ("birchleaf mountain mahogany", "")),
    ("Douglas-Fir, Rocky Mountain", "Lower", ("rocky mountain douglas-fir", "lower")),
    ("Sumac, Laurel", "New", ("laurel sumac", "new")),
    ("Chamise, New Growth", "New", ("chamise", "new")),
    ("Black Hawthorn", "", ("black hawthorn", "")),
    ("  Pine,  Ponderosa ", "", ("ponderosa pine", "")),
    ("", "", ("unknown", "")),
])
def test_parse_fuel_type(fuel_type, sub, expected):
    assert fems.parse_fuel_type(fuel_type, sub) == expected


@pytest.mark.parametrize("category, expected", [
    ("Shrub", "shrub"), ("Tree", "tree"), ("Grass", "grass"), ("Forb", "herb"), ("Moss", "other"),
    ("", "unknown"), (None, "unknown"),
])
def test_fuel_category(category, expected):
    assert fems.fuel_category(category) == expected


def test_fix_longitude():
    assert fems.fix_longitude(118.7037, 34.5894) == (-118.7037, True)
    assert fems.fix_longitude(-118.7, 34.6) == (-118.7, False)
    assert fems.fix_longitude(144.8, 13.4) == (144.8, False)   # Guam stays east


def test_select_sites_buffer_and_sign_fix():
    sites = fems.parse_site_list(SITES_PAYLOAD)
    assert all(s["site_id"] != "99999" for s in sites)              # no coordinates -> skipped
    inside = {s["site_id"] for s in fems.select_sites(sites, SMM_BBOX, 0)}
    assert inside == {"214", "221", "728", "1069", "1188", "1271"}
    near = {s["site_id"] for s in fems.select_sites(sites, SMM_BBOX, 25)}
    assert near == inside | {"1343", "1230", "691"}
    wide = fems.select_sites(sites, SMM_BBOX, 50)
    fisher = [s for s in wide if s["site_id"] == "4937"]
    assert fisher and fisher[0]["lon"] < 0 and fisher[0]["lon_sign_corrected"]
    assert [s["site_id"] for s in wide] == sorted((s["site_id"] for s in wide), key=int)


def test_samples_params_keep_the_tested_filters(lake):
    p = connector(lake).samples_params(["221", "1271"], date(2026, 9, 29), date(2026, 11, 2))
    assert p["filterByCategory"] == p["filterBySubCategory"] == p["filterByMethod"] == "All"
    assert p["sortBy"] == "sample" and p["siteId"] == "221,1271" and p["filterByStatus"] == "Submitted"
    assert p["startDate"] == "2026-09-29T00:00:00.000Z" and p["endDate"] == "2026-11-02T23:59:59.999Z"


# ---------------------------------------------------------------------------- collapse rule
def _raw(value, component="", site="1", day=date(2026, 10, 1), species="chamise", **kw):
    return RawSample(site_id=site, site_name="s", lon=-118.6, lat=34.1, date=day, species=species, value=value,
                     component=component, fuel_category=kw.pop("fuel_category", "shrub"), **kw)


def test_collapse_plain_row_wins_over_growth_phases():
    df = collapse_samples([_raw(68.71), _raw(80.0, "new"), _raw(57.42, "old")], "fems")
    assert len(df) == 1
    row = df.iloc[0]
    assert row.lfmc_pct == pytest.approx(68.71) and row.n_raw_rows == 3 and row.qc_flags == ""


def test_collapse_flags():
    rows = [
        _raw(70.0, "new", site="a"), _raw(60.0, "old", site="a"),                      # mean of phases
        _raw(90.0, "new", site="b"),                                                    # one phase only
        _raw(51.0, site="c"), _raw(53.0, site="c"),                                     # replicates
        _raw(55.0, site="d"), _raw(120.0, "new", site="d"), _raw(60.0, "old", site="d"),  # plain != mean
        _raw(80.0, site="e"), _raw(599999.0, site="e"),                                 # hidden sentinel
        _raw(100.0, site="f", outlier=True), _raw(110.0, site="f"),
        _raw(67.8, site="g"), _raw(72.7, "new", site="g"),                     # plain + one phase: no check
    ]
    df = collapse_samples(rows, "globe-lfmc").set_index("site_id")
    assert df.loc["g", "qc_flags"] == "" and df.loc["g", "lfmc_pct"] == pytest.approx(67.8)
    assert df.loc["a", "lfmc_pct"] == pytest.approx(65.0)
    assert df.loc["a", "qc_flags"] == "components_averaged"
    assert df.loc["b", "qc_flags"] == "single_component" and df.loc["b", "lfmc_pct"] == 90.0
    assert df.loc["c", "qc_flags"] == "replicates_averaged" and df.loc["c", "lfmc_pct"] == pytest.approx(52.0)
    assert df.loc["d", "qc_flags"] == "component_mean_mismatch" and df.loc["d", "lfmc_pct"] == 55.0
    assert "value_out_of_range" in df.loc["e", "qc_flags"].split(";")
    assert bool(df.loc["f", "outlier_flag"]) and "outlier_partial" in df.loc["f", "qc_flags"]
    assert df["n_raw_rows"].sum() == len(rows)


def test_collapse_is_deterministic_and_typed():
    rows = [_raw(70.0, "new", site="a"), _raw(60.0, "old", site="a"), _raw(51.0, site="c")]
    a = collapse_samples(rows, "fems")
    b = collapse_samples(list(reversed(rows)), "fems")
    cols = [c for c in a.columns if c != "ingested_at"]
    assert a[cols].equals(b[cols])
    assert str(a["n_raw_rows"].dtype) == "int32" and str(a["outlier_flag"].dtype) == "bool"
    assert list(a["sample_id"]) == [lfmc_sample_id("fems", s, d, sp)
                                    for s, d, sp in zip(a.site_id, a.date, a.species, strict=True)]


# ---------------------------------------------------------------------------- transform
def _transform_fixture(lake, month=date(2026, 10, 1)):
    c = connector(lake)
    aoi = load_aoi("smm")
    sites = fems.select_sites(fems.parse_site_list(SITES_PAYLOAD), tuple(aoi.bbox), 25)
    ref = c.ref("fems:2026-10:sites-test", fems.utc_midnight(month), urls=["https://example.invalid/samples"],
                month=month.isoformat(), closed=True, as_of="2026-12-31", site_buffer_km=25,
                site_ids=[s["site_id"] for s in sites], sites=sites)
    raw = [c.raw_path(ref, "smm__sites-000.csv"), c.raw_path(ref, "smm__samples-000.csv")]
    lake.write_text(raw[0], METADATA_TEXT)
    lake.write_text(raw[1], SAMPLES_TEXT)
    items = c.transform(ref, raw, aoi)
    return c, items


def test_transform_fixture_rows_and_flags(lake):
    c, items = _transform_fixture(lake)
    assert len(items) == 1
    item = items[0]
    assert item.item_id == "fems-lfmc__smm__20261001"
    part = item.assets[0].path
    assert part == "staged/tables/lfmc_samples/fems-lfmc/part-smm__2026-10.parquet"
    res = validate_table(lake, part, "lfmc_samples")
    assert res.ok, res.errors
    df = read_table(lake, "lfmc_samples", "fems-lfmc")
    assert len(df) == 22
    assert not df.duplicated(["source", "site_id", "date", "species"]).any()
    assert set(df["source"]) == {"fems"} and set(df["status"]) == {"Submitted"}
    assert df["date"].min() == date(2026, 10, 1) and df["date"].max() == date(2026, 10, 27)

    def row(site, day, species):
        hit = df[(df.site_id == site) & (df.date == date(2026, 10, day)) & (df.species == species)]
        assert len(hit) == 1, (site, day, species)
        return hit.iloc[0]

    def flags(r):
        return set(filter(None, r.qc_flags.split(";")))

    r = row("221", 27, "chamise")
    assert r.lfmc_pct == pytest.approx(68.71) and r.n_raw_rows == 3 and flags(r) == {"method_missing"}
    r = row("1188", 20, "chamise")
    assert r.lfmc_pct == pytest.approx(65.0) and flags(r) == {"method_missing", "components_averaged"}
    assert "single_component" in flags(row("1271", 20, "chamise"))
    r = row("214", 14, "chamise")
    assert r.lfmc_pct == pytest.approx(52.0) and "replicates_averaged" in flags(r)
    r = row("728", 14, "chamise")
    assert r.lfmc_pct == pytest.approx(55.0) and "component_mean_mismatch" in flags(r)
    assert "value_out_of_range" in flags(row("221", 13, "laurel sumac"))
    r = row("1271", 13, "eastern mojave buckwheat")
    assert flags(r) == {"single_component"}                     # Sub-Category New, method Gravimetric
    r = row("214", 1, "ponderosa pine")
    assert r.fuel_category == "tree" and "method_non_gravimetric" in flags(r)
    r = row("4937", 8, "chamise")
    assert r.lon == pytest.approx(-118.7037) and "lon_sign_corrected" in flags(r)
    assert row("1271", 27, "kentucky bluegrass").fuel_category == "grass"
    assert row("1271", 27, "arrowleaf balsamroot").fuel_category == "herb"
    assert row("1271", 27, "black sage").lfmc_pct == pytest.approx(206.08)
    assert "1000-hour" not in set(df["species"])

    p = item.properties
    assert p["fems:n_rows_in_month"] == 31
    assert p["fems:n_rows_excluded_category"] == 1
    assert p["fems:n_rows_invalid_value"] == 1
    assert p["fems:n_rows_no_coordinates"] == 1
    assert p["fems:n_raw_rows_collapsed"] == int(df["n_raw_rows"].sum()) == 28
    assert p["fems:n_raw_rows_collapsed"] + 3 == p["fems:n_rows_in_month"]   # every in-month row accounted


def test_transform_without_samples_writes_an_empty_part(lake):
    c = connector(lake)
    aoi = load_aoi("smm")
    ref = c.ref("fems:2026-02:sites-none", fems.utc_midnight(date(2026, 2, 1)), month="2026-02-01", closed=True,
                as_of="2026-11-14", site_buffer_km=0, site_ids=[], sites=[])
    assert c.extract(ref, aoi) == []
    items = c.transform(ref, [], aoi)
    res = validate_table(lake, items[0].assets[0].path, "lfmc_samples")
    assert res.ok and res.details["n_rows"] == 0
    assert items[0].bbox == tuple(aoi.bbox)


# ---------------------------------------------------------------------------- discover and pipeline
def test_discover_keys_open_and_closed_months(lake, offline):
    c = connector(lake, today=date(2026, 11, 14))
    refs = c.discover(load_aoi("smm"), Period(date(2026, 8, 15), date(2026, 12, 31)))
    months = [r.meta["month"] for r in refs]
    assert months == ["2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01"]   # whole months, none in future
    keys = {r.meta["month"]: r.key for r in refs}
    assert keys["2026-08-01"].startswith("fems:2026-08:sites-") and ":asof-" not in keys["2026-08-01"]
    # 2026-09-30 + 45 days = 2026-11-14: still open today
    assert keys["2026-09-01"].endswith(":asof-20261114")
    assert keys["2026-11-01"].endswith(":asof-20261114")
    assert all(r.datetime.day == 1 for r in refs)
    assert offline["sites"] == 1
    # the site hash follows the buffer
    other = connector(lake, site_buffer_km=0).discover(load_aoi("smm"), Period(date(2026, 8, 1), date(2026, 8, 31)))
    assert other[0].key != keys["2026-08-01"]


def test_discover_future_period_is_empty(lake, offline):
    assert connector(lake).discover(load_aoi("smm"), Period(date(2027, 1, 1), date(2027, 2, 1))) == []


def test_pipeline_idempotent_and_open_month_refresh(lake, offline, monkeypatch):
    period = Period(date(2026, 9, 1), date(2026, 11, 14))
    monkeypatch.setattr(fems.FemsConnector, "today", lambda self: date(2026, 11, 14))
    rep = pipeline.run(["fems-lfmc"], "smm", period, lake=lake)
    d = rep.datasets[0]
    assert rep.ok and d.discovered == 3 and d.count("created") == 3, d
    assert offline["samples"] == 3

    rep = pipeline.run(["fems-lfmc"], "smm", period, lake=lake)           # same day: no-op
    d = rep.datasets[0]
    assert rep.ok and d.skipped == 3 and not d.units
    assert offline["samples"] == 3

    monkeypatch.setattr(fems.FemsConnector, "today", lambda self: date(2026, 11, 15))
    rep = pipeline.run(["fems-lfmc"], "smm", period, lake=lake)           # next day: open months refresh
    d = rep.datasets[0]
    assert rep.ok and d.count("updated") == 3 and d.skipped == 0          # Sep settles today: last refresh
    rep = pipeline.run(["fems-lfmc"], "smm", period, lake=lake)
    assert rep.datasets[0].skipped == 3

    keys = catalog.source_keys(lake, "fems-lfmc", aoi="smm")
    assert len(keys) == 3 and sum(len(v) for v in keys.values()) == 3      # one item per month, replaced
    assert "fems:2026-09:sites-" in next(k for k in keys if k.startswith("fems:2026-09"))
    assert not any(k.startswith("fems:2026-09") and "asof" in k for k in keys)
    assert all(r.ok for r in validate_collection(lake, load_dataset("fems-lfmc")))
    df = read_table(lake, "lfmc_samples", "fems-lfmc")
    # 22 October rows + the 2026-09-30 and 2026-11-01 boundary rows, each in its own month's part only
    assert len(df) == 24 and not df.duplicated(["source", "site_id", "date", "species"]).any()
    assert set(df[df.date == date(2026, 9, 30)].site_id) == {"1188"}


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_site_list(lake):
    sites = connector(lake).fetch_sites()
    assert len(sites) > 1000
    near = fems.select_sites(sites, SMM_BBOX, 0)
    assert {"221", "1188", "1271"} <= {s["site_id"] for s in near}


@pytest.mark.network
def test_live_samples_and_metadata_closed_month(lake):
    c = connector(lake, request_pause_s=0)
    texts = c.fetch_samples(c.samples_urls(["221", "1271"], date(2026, 10, 1)))
    df = fems.read_samples_csv(texts[0])
    assert len(df) > 0 and set(df["site_id"]) <= {"221", "1271"}
    coords = fems.parse_site_metadata(c.fetch_site_metadata(["221", "1271"])[0])
    assert coords["221"]["lat"] == pytest.approx(34.0844, abs=1e-3)
    rows, counts = fems.samples_to_raw(df, date(2026, 10, 1), coords, ["Dead"])
    assert rows and counts["n_rows_no_coordinates"] == 0
