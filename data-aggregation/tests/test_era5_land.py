# fuel-lake — tests for ERA5-Land (credential errors, CDS request, job polling, daily aggregation on real CDS files)
# Licensed under Apache-2.0.
"""Offline tests use real CDS responses for smm (``tests/fixtures/weather_fire/era5_land_smm_*``, downloaded
2026-11-14): hourly 2026-06-29..30 split into the request groups ``surface`` and ``soil``, and ``tp`` at 00/23 UTC
on 2026-06-30 and 2026-07-01 (a ``date`` range across the month boundary). The full pipeline test uses a
synthetic month in the same layout."""

from __future__ import annotations

import sys
import types
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fuellake import catalog, pipeline
from fuellake.config import Settings
from fuellake.connectors.base import Period
from fuellake.connectors.era5_land import (
    CdsJobError,
    Era5CredentialsError,
    Era5LandDailyConnector,
    daily_aggregate,
    merge_groups,
    read_cds_netcdf,
    snapped_area,
)
from fuellake.contracts import validate_collection
from fuellake.rasters import open_zarr
from fuellake.registry import load_aoi, load_dataset
from fuellake.storage import Lake

FIX = Path(__file__).parent / "fixtures" / "weather_fire"
SURFACE = FIX / "era5_land_smm_20260629_30_surface.nc"
SOIL = FIX / "era5_land_smm_20260629_30_soil.nc"
TP_BOUNDARY = FIX / "era5_land_smm_tp_20260630_0701.nc"
VARIABLES = ["t2m", "t2m_max", "d2m", "tp", "u10", "v10", "ws10", "ws10_max", "swvl1", "swvl2", "swvl3", "swvl4"]


def synthetic_cds_netcdf(path: Path, names: list[str], first: str, last: str) -> None:
    """Hourly file in the new-CDS layout (valid_time x latitude x longitude, expver/number coords) for
    ``first`` 00 UTC .. ``last`` 23 UTC. tp accumulates from 00 UTC: 0.001 m * h at hour h (1..23), the 00 UTC
    value is the previous day's total (0.024 m; 0.050 m on the very first hour). t2m mean of day d is 280 + d."""
    import xarray as xr

    times = pd.date_range(first, pd.Timestamp(last) + pd.Timedelta(hours=23), freq="h")
    lat = np.round(np.arange(34.5, 33.69, -0.1), 1)
    lon = np.round(np.arange(-119.3, -118.19, 0.1), 1)
    k = np.arange(times.size)
    hour, day = k % 24, k // 24
    series = {"tp": np.where(hour > 0, 0.001 * hour, np.where(k > 0, 0.024, 0.050)),
              "t2m": 280.0 + day + (hour - 11.5) * 0.1, "d2m": np.full(times.size, 270.0),
              "u10": np.full(times.size, 3.0), "v10": np.full(times.size, -4.0)}
    shape = (times.size, lat.size, lon.size)
    data = {}
    for n in names:
        arr = np.broadcast_to(series.get(n, np.full(times.size, 0.2))[:, None, None], shape).astype("float32").copy()
        arr[:, -2:, :3] = np.nan           # "ocean" corner
        data[n] = (("valid_time", "latitude", "longitude"), arr)
    ds = xr.Dataset(data, coords={"valid_time": times, "latitude": lat, "longitude": lon,
                                  "expver": ("valid_time", np.full(times.size, "0001")), "number": 0})
    ds.to_netcdf(path, engine="netcdf4")


@pytest.fixture
def cds_env(monkeypatch):
    monkeypatch.setenv("CDSAPI_URL", "https://cds.example.invalid/api")
    monkeypatch.setenv("CDSAPI_KEY", "test-key-not-real")


class FakeRemote:
    def __init__(self, request, states, fail_message=None):
        self.request, self.request_id = request, f"job-{id(self):x}"
        self._states, self._fail = list(states), fail_message

    @property
    def status(self):
        return self._states.pop(0) if len(self._states) > 1 else self._states[0]

    def get_results(self):
        if self._fail:
            raise RuntimeError(self._fail)

    def download(self, target):
        names = [n for n, long in CDS_LONG.items() if long in self.request["variable"]]
        first, end = self.request["date"].split("/")
        synthetic_cds_netcdf(Path(target), names, first, end)
        return target


CDS_LONG = {"t2m": "2m_temperature", "d2m": "2m_dewpoint_temperature", "tp": "total_precipitation",
            "u10": "10m_u_component_of_wind", "v10": "10m_v_component_of_wind",
            "swvl1": "volumetric_soil_water_layer_1", "swvl2": "volumetric_soil_water_layer_2",
            "swvl3": "volumetric_soil_water_layer_3", "swvl4": "volumetric_soil_water_layer_4"}


@pytest.fixture
def fake_cdsapi(monkeypatch):
    """A stand-in ``cdsapi`` module: ``retrieve`` (no target) returns a job that goes accepted -> running ->
    successful and downloads a synthetic hourly file for the requested variables and dates."""
    calls: list[tuple[str, dict]] = []
    states = {"seq": ["accepted", "running", "successful"], "fail": None}

    class Client:
        def __init__(self, url=None, key=None, wait_until_complete=True, **kw):
            assert wait_until_complete is False
            self.url, self.key = url, key

        def retrieve(self, name, request, target=None):
            assert target is None
            calls.append((name, request))
            return FakeRemote(request, states["seq"], states["fail"])

    mod = types.ModuleType("cdsapi")
    mod.Client = Client
    monkeypatch.setitem(sys.modules, "cdsapi", mod)
    monkeypatch.setattr("fuellake.connectors.era5_land._time.sleep", lambda s: None)
    return calls, states


def connector(lake, **options):
    ds = load_dataset("era5-land-daily")
    if options:
        ds = ds.model_copy(update={"options": {**ds.options, **options}})
    return Era5LandDailyConnector(ds, lake, Settings.from_env())


def test_config_is_tested_with_caveat():
    ds = load_dataset("era5-land-daily")
    assert ds.status == "tested" and ds.connector == "fuellake.connectors.era5_land:Era5LandDailyConnector"
    assert [v.name for v in ds.variables] == VARIABLES
    assert ds.license.id == "CC-BY-4.0" and set(ds.access.env) == {"CDSAPI_URL", "CDSAPI_KEY"}
    assert any("2026-10" in c and "ONE month" in c for c in ds.caveats)
    assert [v.name for v in ds.variables if v.lag] == ["swvl1", "swvl2", "swvl3"]


def test_discover_without_credentials_is_one_clear_error(lake):
    with pytest.raises(Era5CredentialsError, match="CDSAPI_URL, CDSAPI_KEY not set"):
        connector(lake).discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 6, 30)))


def test_missing_credentials_names_only(lake, monkeypatch):
    conn, aoi = connector(lake), load_aoi("smm")
    assert conn.missing_credentials(aoi) == ["CDSAPI_URL", "CDSAPI_KEY"]     # discover already needs them
    monkeypatch.setenv("CDSAPI_URL", "https://cds.example.invalid/api")
    assert conn.missing_credentials(aoi) == ["CDSAPI_KEY"]
    monkeypatch.setenv("CDSAPI_KEY", "test-key-not-real")
    assert conn.missing_credentials(aoi) == []


def test_lenient_run_skips_missing_credentials(lake):
    rep = pipeline.run(["era5-land-daily"], "smm", Period(date(2026, 6, 1), date(2026, 6, 30)), lake=lake,
                       skip_stubs=True, skip_missing_credentials=True)
    assert rep.ok and rep.datasets[0].reason == "skipped: missing credentials (CDSAPI_KEY, CDSAPI_URL)"


def test_missing_cdsapi_package_is_reported(lake, cds_env, monkeypatch):
    monkeypatch.setitem(sys.modules, "cdsapi", None)     # makes `import cdsapi` raise ImportError
    with pytest.raises(Era5CredentialsError, match="cdsapi") as exc:
        connector(lake).discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 6, 30)))
    assert "test-key-not-real" not in str(exc.value)


def test_snapped_area_is_outward_on_the_lattice():
    assert snapped_area((-119.15, 33.78, -118.30, 34.40), 0.1) == [34.4, -119.2, 33.7, -118.3]
    assert snapped_area((-124.61, 32.33, -113.93, 42.21), 0.1) == [42.3, -124.7, 32.3, -113.9]


def test_cds_request_shape(lake):
    conn = connector(lake)
    groups = conn.active_groups()
    assert groups == {"surface": ["t2m", "d2m", "tp", "u10", "v10"], "soil": ["swvl1", "swvl2", "swvl3", "swvl4"]}
    req = conn.request(load_aoi("smm"), date(2026, 6, 1), date(2026, 6, 30), groups["surface"])
    assert req["date"] == "2026-06-01/2026-07-01" and len(req["time"]) == 24   # runs to 00 UTC of July 1
    n, w, s, e = req["area"]                       # CDS order: North, West, South, East
    assert n > 34.20 and w < -118.95 and s < 33.98 and e > -118.50
    assert all(abs(c * 10 - round(c * 10)) < 1e-9 for c in req["area"])
    assert req["variable"] == ["2m_temperature", "2m_dewpoint_temperature", "total_precipitation",
                               "10m_u_component_of_wind", "10m_v_component_of_wind"]
    assert req["data_format"] == "netcdf" and req["download_format"] == "unarchived"
    open_month = conn.request(load_aoi("smm"), date(2026, 11, 1), date(2026, 11, 8), through_next_00=False)
    assert open_month["date"] == "2026-11-01/2026-11-08" and len(open_month["variable"]) == 9


def _real_hourly(lake):
    for p in (SURFACE, SOIL):
        lake.write_bytes(f"raw/{p.name}", p.read_bytes())
    conn = connector(lake)
    parts = [read_cds_netcdf(lake, f"raw/{p.name}", names)
             for p, names in zip((SURFACE, SOIL), conn.active_groups().values(), strict=True)]
    return conn, merge_groups(parts)


def test_read_real_cds_file_layout(lake):
    _, hourly = _real_hourly(lake)
    assert hourly.sizes == {"time": 48, "y": 6, "x": 9}
    assert hourly["y"].values[0] == pytest.approx(34.3) and hourly["x"].values[0] == pytest.approx(-119.2)
    assert np.allclose(np.diff(hourly["x"].values), 0.1) and np.allclose(np.diff(hourly["y"].values), -0.1)
    assert "expver" not in hourly.coords and "number" not in hourly.coords
    assert np.isnan(hourly["t2m"].values[0]).sum() == 27          # Santa Monica Bay is NaN (land-only product)


def test_real_tp_accumulation_resets_after_00_utc(lake):
    _, hourly = _real_hourly(lake)
    tp = hourly["tp"].values
    hours = pd.DatetimeIndex(hourly["time"].values).hour
    inc = np.diff(tp, axis=0)[hours[1:] >= 2]
    assert np.nanmin(inc) > -1e-7                                 # non-decreasing within the day
    wet = np.nanargmax(tp[24])                                    # a cell with rain on 06-29
    assert tp[24].ravel()[wet] > 0 and tp[25].ravel()[wet] < tp[24].ravel()[wet]   # 01 UTC restarts


def test_daily_aggregate_on_real_file(lake):
    conn, hourly = _real_hourly(lake)
    days = [date(2026, 6, 29), date(2026, 6, 30)]
    daily, last_23h = daily_aggregate(hourly, conn.rules(), days)
    assert list(daily.data_vars) == VARIABLES and daily.sizes["time"] == 2
    t = hourly["t2m"].values
    assert np.allclose(daily["t2m"].values[0], t[:24].mean(axis=0), equal_nan=True)
    assert np.allclose(daily["t2m_max"].values[1], t[24:].max(axis=0), equal_nan=True)
    ws = np.hypot(hourly["u10"].values, hourly["v10"].values)
    assert np.allclose(daily["ws10"].values[0], ws[:24].mean(axis=0), equal_nan=True, atol=1e-5)
    assert np.allclose(daily["ws10_max"].values[0], ws[:24].max(axis=0), equal_nan=True, atol=1e-5)
    assert np.nanmin(daily["ws10"].values - np.hypot(daily["u10"].values, daily["v10"].values)) > -1e-5
    tp = hourly["tp"].values
    assert np.allclose(daily["tp"].values[0], tp[24] * 1000, equal_nan=True)        # 06-29 = 06-30 00 UTC, mm
    assert np.allclose(daily["tp"].values[1], tp[47] * 1000, equal_nan=True)        # 06-30: only 23 UTC here
    assert last_23h is True
    assert np.isnan(daily["swvl3"].values[:, 0, 0]).all() == np.isnan(t[0, 0, 0])


def test_daily_tp_uses_next_day_00_utc_across_the_month(lake):
    lake.write_bytes("raw/tp.nc", TP_BOUNDARY.read_bytes())
    hourly = read_cds_netcdf(lake, "raw/tp.nc", ["tp"])       # 06-30 00/23 UTC, 07-01 00/23 UTC
    daily, last_23h = daily_aggregate(hourly, {"tp": ("tp", "accum")}, [date(2026, 6, 30)])
    assert np.allclose(daily["tp"].values[0], hourly["tp"].values[2] * 1000, equal_nan=True)
    assert last_23h is False


def test_daily_aggregate_synthetic_rules(tmp_path):
    synthetic_cds_netcdf(tmp_path / "h.nc", ["t2m", "tp", "u10", "v10"], "2026-06-01", "2026-06-04")
    lake = Lake(tmp_path / "l")
    lake.write_bytes("raw/h.nc", (tmp_path / "h.nc").read_bytes())
    hourly = read_cds_netcdf(lake, "raw/h.nc", ["t2m", "tp", "u10", "v10"])
    days = [date(2026, 6, 1) + timedelta(days=i) for i in range(3)]
    rules = {"t2m": ("t2m", "mean"), "tp": ("tp", "accum"), "ws10": ("ws10", "mean"), "ws10_max": ("ws10", "max")}
    daily, last_23h = daily_aggregate(hourly, rules, days)
    assert np.allclose(daily["t2m"].values[:, 0, -1], [280.0, 281.0, 282.0], atol=1e-4)
    assert np.allclose(daily["tp"].values[:, 0, -1], [24.0, 24.0, 24.0], atol=1e-3)   # mm, from D+1 00 UTC
    assert np.allclose(daily["ws10"].values[:, 0, -1], 5.0) and last_23h is False
    assert np.isnan(daily["tp"].values[:, -1, 0]).all()                                # ocean stays NaN


def test_pipeline_offline_with_fake_cds_is_idempotent(lake, cds_env, fake_cdsapi, monkeypatch, caplog):
    calls, _ = fake_cdsapi
    monkeypatch.setattr(Era5LandDailyConnector, "data_end", lambda self: date(2026, 6, 3))
    period = Period(date(2026, 6, 1), date(2026, 6, 3))
    with caplog.at_level("INFO", logger="fuellake.connectors.era5_land"):
        rep = pipeline.run(["era5-land-daily"], "smm", period, lake=lake)
    assert rep.ok, rep.to_dict()
    assert "running" in caplog.text and "successful" in caplog.text
    (item,) = catalog.iter_items(lake, "era5-land-daily")
    assert item["id"] == "era5-land-daily__smm__20260601"
    assert item["properties"]["fuellake:source_key"] == "era5-land-202606-through-20260603"
    assert item["properties"]["era5_land:tp_last_day_23h"] is True        # data end: no 06-04 00 UTC yet
    ds = open_zarr(lake, catalog.asset_rel(item, "data"))
    assert set(ds.data_vars) == set(VARIABLES) and ds.sizes["time"] == 3
    assert ds["tp"].attrs["units"] == "mm" and float(np.nanmax(ds["tp"].values[:2])) == pytest.approx(24.0)
    x, y = ds.x.values, ds.y.values
    assert -119.1 < x.min() < -118.95 and -118.50 < x.max() < -118.35        # bbox + one 0.1 degree cell
    assert 33.8 < y.min() < 33.98 and 34.20 < y.max() < 34.35
    assert all(r.ok for r in validate_collection(lake, load_dataset("era5-land-daily")))
    assert [c[0] for c in calls] == ["reanalysis-era5-land"] * 2
    assert {c[1]["date"] for c in calls} == {"2026-06-01/2026-06-03"}
    rep2 = pipeline.run(["era5-land-daily"], "smm", period, lake=lake)
    assert rep2.ok and rep2.counts()["skipped"] == 1 and len(calls) == 2


def test_failed_cds_job_fails_the_unit_and_raw_cache_is_reused(lake, cds_env, fake_cdsapi, monkeypatch):
    calls, states = fake_cdsapi
    monkeypatch.setattr(Era5LandDailyConnector, "data_end", lambda self: date(2026, 6, 30))
    period = Period(date(2026, 6, 1), date(2026, 6, 30))
    states.update(seq=["accepted", "failed"], fail="the request you have submitted is not valid")
    rep = pipeline.run(["era5-land-daily"], "smm", period, lake=lake)
    unit = rep.datasets[0].units[0]
    assert not rep.ok and unit.status == "failed" and "CDS job failed" in unit.error
    assert "not valid" in unit.error and "test-key-not-real" not in unit.error
    states.update(seq=["successful"], fail=None)
    n = len(calls)
    assert pipeline.run(["era5-land-daily"], "smm", period, lake=lake).ok
    assert len(calls) == n + 2
    assert {c[1]["date"] for c in calls[n:]} == {"2026-06-01/2026-06-30"}   # month complete, data end 06-30
    conn = connector(lake)
    ref = conn.discover(load_aoi("smm"), period)[0]
    conn.extract(ref, load_aoi("smm"))                                      # raw files cached: no new job
    assert len(calls) == n + 2


def test_open_month_raw_cache_is_refreshed_when_data_end_advances(lake, cds_env, fake_cdsapi, monkeypatch):
    calls, _ = fake_cdsapi
    period = Period(date(2026, 6, 1), date(2026, 6, 30))
    monkeypatch.setattr(Era5LandDailyConnector, "data_end", lambda self: date(2026, 6, 3))
    assert pipeline.run(["era5-land-daily"], "smm", period, lake=lake).ok and len(calls) == 2
    monkeypatch.setattr(Era5LandDailyConnector, "data_end", lambda self: date(2026, 6, 5))
    rep = pipeline.run(["era5-land-daily"], "smm", period, lake=lake)
    assert rep.ok and rep.counts()["updated"] == 1 and len(calls) == 4      # stale raw files requested again
    assert {c[1]["date"] for c in calls[2:]} == {"2026-06-01/2026-06-05"}
    (item,) = catalog.iter_items(lake, "era5-land-daily")
    assert item["properties"]["era5_land:last_day"] == "2026-06-05"
    assert item["properties"]["fuellake:source_key"] == "era5-land-202606-through-20260605"


def test_wait_times_out(lake, fake_cdsapi):
    conn = connector(lake, max_wait_s=-1)
    with pytest.raises(CdsJobError, match="not finished"):
        conn.wait({"surface": FakeRemote({}, ["accepted"])}, "smm 2026-06")


def test_fixtures_are_small_real_cds_files():
    for p in (SURFACE, SOIL, TP_BOUNDARY):
        assert p.stat().st_size < 100_000 and p.read_bytes()[:4] == b"\x89HDF"


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_cds_catalogue_licence_and_latency(tmp_path):
    import requests

    r = requests.get("https://cds.climate.copernicus.eu/api/catalogue/v1/collections/reanalysis-era5-land",
                     headers={"User-Agent": "fuel-lake/0.1"}, timeout=60)
    assert r.json()["license"] == "CC-BY-4.0"
    conn = Era5LandDailyConnector(load_dataset("era5-land-daily"), Lake(tmp_path / "live"), Settings.from_env())
    assert (date.today() - conn.data_end()).days < 30


@pytest.mark.network
def test_live_daily_statistics_lacks_precipitation():
    import requests

    r = requests.get("https://cds.climate.copernicus.eu/api/retrieve/v1/processes/derived-era5-land-daily-statistics",
                     headers={"User-Agent": "fuel-lake/0.1"}, timeout=60)
    names = r.json()["inputs"]["variable"]["schema"]["items"]["enum"]
    assert "total_precipitation" not in names and "10m_u_component_of_wind" in names


@pytest.mark.network
def test_live_cds_one_day_request(tmp_path, monkeypatch):
    """Needs CDSAPI_URL / CDSAPI_KEY in the environment (skipped otherwise); one tiny job (smm, one day)."""
    import os

    if not (os.environ.get("CDSAPI_URL") and os.environ.get("CDSAPI_KEY")):
        pytest.skip("CDSAPI_URL / CDSAPI_KEY not set")
    lake = Lake(tmp_path / "live")
    conn = Era5LandDailyConnector(load_dataset("era5-land-daily"), lake, Settings.from_env())
    ref = conn.ref("era5-land-202606-probe", pd.Timestamp("2026-06-30", tz="UTC").to_pydatetime(), [],
                   first_day="2026-06-30", last_day="2026-06-30", complete=False, through_next_00=True)
    raw = conn.extract(ref, load_aoi("smm"))
    (item,) = conn.transform(ref, raw, load_aoi("smm"))
    ds = open_zarr(lake, item.assets[0].path)
    assert ds.sizes["time"] == 1 and set(ds.data_vars) == set(VARIABLES)
    assert item.properties["era5_land:tp_last_day_23h"] is False
