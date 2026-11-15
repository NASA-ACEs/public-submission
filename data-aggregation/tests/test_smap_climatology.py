# fuel-lake — tests for the SMAP day-of-year climatology (smap-climatology): method and pipeline path
# Licensed under Apache-2.0.
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from fuellake import catalog, pipeline
from fuellake.config import Settings
from fuellake.connectors.base import Period
from fuellake.connectors.smap import SmapClimatologyConnector, climatology_step, doy_climatology
from fuellake.contracts import validate_collection
from fuellake.rasters import open_zarr
from fuellake.registry import load_aoi, load_dataset
from test_smap_l3e import fake_sources, write_smap_config


# ---------------------------------------------------------------------------- method
def test_climatology_step_uses_a_leap_reference_year():
    assert climatology_step(date(2019, 1, 1)) == 0
    assert climatology_step(date(2020, 2, 29)) == 59
    assert climatology_step(date(2019, 3, 1)) == climatology_step(date(2020, 3, 1)) == 60
    assert climatology_step(date(2019, 12, 31)) == climatology_step(date(2020, 12, 31)) == 365


def _brute(values, days, k, half):
    """Values whose circular day distance to step k is at most half (reference implementation)."""
    out = []
    for v, d in zip(values, days, strict=True):
        dist = abs(climatology_step(d) - k)
        if min(dist, 366 - dist) <= half and np.isfinite(v):
            out.append(v)
    return np.array(out)


def test_doy_climatology_matches_a_brute_force_window():
    rng = np.random.default_rng(7)
    days = [date(2015, 10, 1) + timedelta(days=i) for i in range((date(2019, 12, 31) - date(2015, 10, 1)).days + 1)]
    n = len(days)
    seasonal = np.array([0.25 + 0.1 * np.cos(2 * np.pi * climatology_step(d) / 366) for d in days])
    values = (seasonal + rng.normal(0, 0.02, n))[:, None, None] * np.ones((1, 2, 3))
    values[rng.random(n) < 0.4, 0, 0] = np.nan                    # an observation product with gaps
    values[:, 1, 2] = np.nan                                       # a cell that never has data
    mean, std, count = doy_climatology(values, days, window_days=31, min_count=5)
    assert mean.shape == std.shape == count.shape == (366, 2, 3) and mean.dtype == np.float32
    for k in (0, 59, 60, 180, 365):                                # wrap-around both ends, 29 Feb, 1 Mar
        for (i, j) in ((0, 0), (0, 1)):
            ref = _brute(values[:, i, j], days, k, 15)
            assert count[k, i, j] == len(ref)
            assert mean[k, i, j] == pytest.approx(ref.mean(), abs=1e-6)
            assert std[k, i, j] == pytest.approx(ref.std(ddof=1), abs=1e-6)
    assert np.all(count[:, 1, 2] == 0) and np.all(np.isnan(mean[:, 1, 2])) and np.all(np.isnan(std[:, 1, 2]))
    # 1 January pools 17 December .. 16 January: Oct-Dec 2015 exists, so its count exceeds a mid-year step's
    full = np.isfinite(values[:, 0, 1])
    assert full.all() and count[0, 0, 1] == 15 * 5 + 16 * 4 and count[180, 0, 1] == 31 * 4
    # the seasonal cycle survives: wet around step 0, dry around step 183
    assert mean[0, 0, 1] > 0.33 and mean[183, 0, 1] < 0.17
    # min_count: too few values -> NaN mean/std but the count stays
    m2, s2, c2 = doy_climatology(values, days, window_days=31, min_count=10_000)
    assert np.all(np.isnan(m2)) and np.all(np.isnan(s2)) and np.array_equal(c2, count)
    with pytest.raises(ValueError):
        doy_climatology(values, days, window_days=30)
    with pytest.raises(ValueError):
        doy_climatology(values[:-1], days)


def test_single_day_window_and_leap_day():
    days = [date(2016, 2, 28), date(2016, 2, 29), date(2016, 3, 1), date(2017, 2, 28), date(2017, 3, 1)]
    vals = np.array([1.0, 2.0, 3.0, 5.0, 7.0])[:, None, None]
    mean, std, count = doy_climatology(vals, days, window_days=1)
    assert mean[58, 0, 0] == pytest.approx(3.0) and count[58, 0, 0] == 2      # 28 Feb of both years
    assert mean[59, 0, 0] == pytest.approx(2.0) and count[59, 0, 0] == 1 and np.isnan(std[59, 0, 0])
    assert mean[60, 0, 0] == pytest.approx(5.0) and std[60, 0, 0] == pytest.approx(np.std([3.0, 7.0], ddof=1))


# ---------------------------------------------------------------------------- pipeline (offline fixtures)
OPTIONS = {"baseline": ["2026-06-01", "2026-06-02"], "min_count": 1}


def test_climatology_from_staged_items(tmp_path, monkeypatch, lake):
    write_smap_config(tmp_path, monkeypatch, OPTIONS)
    fake = fake_sources(monkeypatch)
    period = Period(date(2026, 6, 1), date(2026, 6, 2))
    # without inputs: nothing to discover, and that is not a failure
    rep0 = pipeline.run(["smap-climatology"], "smm", period, lake=lake)
    assert rep0.ok and rep0.counts()["discovered"] == 0
    assert pipeline.run(["smap-l4", "smap-l3e"], "smm", period, lake=lake).ok
    n_requests = len(fake.urls)
    # the run period does not matter (static layer); no request is made
    rep = pipeline.run(["smap-climatology"], "smm", Period(date(2030, 1, 1), date(2030, 1, 2)), lake=lake)
    assert rep.ok and rep.counts()["created"] == 1, rep.to_dict()
    assert len(fake.urls) == n_requests
    (item,) = catalog.iter_items(lake, "smap-climatology")
    assert item["id"] == "smap-climatology__smm__19040101"
    p = item["properties"]
    assert p["datetime"].startswith("1904-01-01") and p["end_datetime"].startswith("1904-12-31T23:59:59")
    assert p["smap_clim:baseline"] == ["2026-06-01", "2026-06-02"] and p["smap_clim:window_days"] == 31
    assert sorted(p["fuellake:source_ids"]) == ["smap-l3e__smm__20260601", "smap-l4__smm__20260601"]
    inputs = {i["prefix"]: i for i in p["smap_clim:inputs"]}
    assert inputs["l4_sm_surface"]["n_days"] == 2 and inputs["l3e_sm_am_all"]["n_valid_values"] == 13
    ds = open_zarr(lake, catalog.asset_rel(item, "data"))
    assert ds.sizes["time"] == 366 and int(ds["dayofyear"].values[-1]) == 366
    assert str(ds["time"].values[0])[:10] == "1904-01-01" and ds.rio.crs.to_epsg() == 6933
    assert set(ds.data_vars) == {f"{pre}_{s}" for pre in ("l4_sm_surface", "l3e_sm_am_all", "l3e_sm_am")
                                 for s in ("mean", "std", "count")}
    l4 = open_zarr(lake, catalog.asset_rel(next(catalog.iter_items(lake, "smap-l4")), "data"))
    k = climatology_step(date(2026, 6, 1))
    expected = l4["sm_surface"].values.mean(axis=0)
    np.testing.assert_allclose(ds["l4_sm_surface_mean"].values[k], expected, rtol=1e-6)
    assert np.nanmax(ds["l4_sm_surface_count"].values) == 2
    assert np.nanmax(ds["l3e_sm_am_all_count"].values) == 1 and np.nanmax(ds["l3e_sm_am_count"].values) == 0
    far = climatology_step(date(2026, 12, 1))                       # outside every window of the baseline
    assert np.all(ds["l4_sm_surface_count"].values[far] == 0) and np.all(np.isnan(ds["l4_sm_surface_mean"][far]))
    assert all(r.ok for r in validate_collection(lake, load_dataset("smap-climatology")))
    rep2 = pipeline.run(["smap-climatology"], "smm", period, lake=lake)
    assert rep2.ok and rep2.counts()["skipped"] == 1


def test_incomplete_baseline_discovers_nothing(tmp_path, monkeypatch, lake):
    write_smap_config(tmp_path, monkeypatch, {"baseline": ["2026-05-01", "2026-06-02"]})
    fake_sources(monkeypatch)
    assert pipeline.run(["smap-l4", "smap-l3e"], "smm", Period(date(2026, 6, 1), date(2026, 6, 2)), lake=lake).ok
    conn = SmapClimatologyConnector(load_dataset("smap-climatology"), lake, Settings.from_env())
    assert conn.discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 6, 2))) == []
    items, problems = conn._month_items(load_aoi("smm"), {"dataset": "smap-l4", "variable": "sm_surface"})
    assert problems == ["2026-05 missing"] and len(items) == 1


def test_features_layer_ignores_the_climatology(tmp_path, monkeypatch, lake):
    """Dated in the reference year 1904, the climatology is never a weather input of a normal build."""
    from fuellake.features._common import discover_inputs

    write_smap_config(tmp_path, monkeypatch, OPTIONS)
    fake_sources(monkeypatch)
    period = Period(date(2026, 6, 1), date(2026, 6, 2))
    assert pipeline.run(["smap-l4", "smap-l3e", "smap-climatology"], "smm", period, lake=lake).ok
    assert len(list(catalog.iter_items(lake, "smap-climatology"))) == 1
    inputs = discover_inputs(lake, load_aoi("smm"), period.start, period.end)
    assert "smap-climatology" in inputs.missing
    assert not [v for v in inputs.variables if v.dataset == "smap-climatology"]
    assert catalog.search(lake, ["smap-climatology"], aoi="smm")           # found without a time filter
