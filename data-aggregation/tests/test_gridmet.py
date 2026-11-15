# fuel-lake — tests for the gridMET connector (offline fixtures + live NKN THREDDS checks)
# Licensed under Apache-2.0.
from __future__ import annotations

import gzip
import re
import shutil
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest
import yaml

from fuellake import catalog, pipeline
from fuellake.config import Settings
from fuellake.connectors.base import Period
from fuellake.connectors.gridmet import (
    GridmetConnector,
    buffered_bbox,
    check_band_coverage,
    clip_lonlat,
    month_starts,
    mosaic_bands,
    parse_timespan_end,
    raw_netcdf_size,
    read_ncss_variable,
    subset_problem,
)
from fuellake.contracts import validate_collection
from fuellake.features.matchups import config_lag_variables
from fuellake.rasters import open_zarr
from fuellake.region import zarr_inside_mask
from fuellake.registry import lake_region, load_aoi, load_dataset
from fuellake.storage import Lake

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
FIX = Path(__file__).parent / "fixtures" / "weather_fire"
SMM_BBOX = (-118.95, 33.98, -118.50, 34.20)
RES = 1 / 24
GRIDMET_LON0, GRIDMET_LAT0 = -124.76666666666667, 49.4   # centres of the first column / row of the NKN grid
# Fixtures gridmet_<short>_20260601_03.nc: NCSS subsets for the smm bbox + 2 cells, 2026-06-01..03 (fetched live).
FIXTURE_VARS = {"vs": "daily_mean_wind_speed", "fm100": "dead_fuel_moisture_100hr",
                "pr": "precipitation_amount", "tmmx": "daily_maximum_temperature",
                "pet": "daily_mean_reference_evapotranspiration_grass", "bi": "daily_mean_burning_index_g"}
LAG_VARIABLES = {"pr", "vpd", "pet", "tmmx", "fm100", "fm1000"}


def dataset_xml(end: str) -> str:
    return (f"<gridDataset><TimeSpan><begin>1979-01-01T00:00:00Z</begin><end>{end}</end></TimeSpan>"
            "</gridDataset>")


class FakeResponse:
    def __init__(self, status: int = 200, content: bytes = b"", headers: dict | None = None):
        self.status_code, self.content, self.headers = status, content, headers or {}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class ConcurrencyProbe:
    """Counts requests in flight (in total and per NCSS variable) from several threads."""

    def __init__(self, delay_s: float = 0.02):
        self.delay_s, self.lock = delay_s, threading.Lock()
        self.active: dict[str, int] = {}
        self.max_active = self.max_same = 0

    def enter(self, var: str) -> None:
        with self.lock:
            self.active[var] = self.active.get(var, 0) + 1
            self.max_same = max(self.max_same, self.active[var])
            self.max_active = max(self.max_active, sum(self.active.values()))

    def leave(self, var: str) -> None:
        with self.lock:
            self.active[var] -= 1


class FakeGridmetSession:
    """Serves NCSS dataset.xml and the fixture subsets for any time range.

    ``script`` maps an NCSS variable name to responses served first, one per request: an int is an HTTP status,
    bytes are a 200 response with that body (e.g. a damaged subset)."""

    def __init__(self, end: str = "2026-06-03T00:00:00Z", script: dict | None = None):
        self.end, self.calls = end, []
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.probe = ConcurrencyProbe()

    def get(self, url, timeout=None, **kw):
        with self.probe.lock:
            self.calls.append(url)
        if url.endswith("/dataset.xml"):
            return FakeResponse(200, dataset_xml(self.end).encode())
        var = parse_qs(urlparse(url).query).get("var", [""])[0]
        self.probe.enter(var)
        try:
            time.sleep(self.probe.delay_s)
            with self.probe.lock:
                queued = self.script.get(var)
                scripted = queued.pop(0) if queued else None
            if isinstance(scripted, int):
                return FakeResponse(scripted, b"scripted error")
            if isinstance(scripted, bytes):
                return FakeResponse(200, scripted)
            short = {v: k for k, v in FIXTURE_VARS.items()}.get(var)
            if short is None:
                return FakeResponse(400, b"unknown var")
            return FakeResponse(200, (FIX / f"gridmet_{short}_20260601_03.nc").read_bytes())
        finally:
            self.probe.leave(var)

    def var_calls(self, var: str) -> int:
        return sum(1 for u in self.calls if parse_qs(urlparse(u).query).get("var", [""])[0] == var)


def synthetic_subset(q: dict) -> bytes:
    """NetCDF-3 subset on the gridMET lattice for an NCSS query: every cell whose extent meets the bbox, lat from
    north to south (as NKN serves it), every value finite (10 + day index). Pure Python writer (scipy), so it is
    safe in the connector's download threads."""
    import xarray as xr

    w, e, s, n = (float(q[k]) for k in ("west", "east", "south", "north"))
    i = np.arange(np.ceil((w - GRIDMET_LON0) / RES - 0.5), np.floor((e - GRIDMET_LON0) / RES + 0.5) + 1)
    j = np.arange(np.ceil((GRIDMET_LAT0 - n) / RES - 0.5), np.floor((GRIDMET_LAT0 - s) / RES + 0.5) + 1)
    lon, lat = GRIDMET_LON0 + i * RES, GRIDMET_LAT0 - j * RES
    base = date(1900, 1, 1)
    first, last = date.fromisoformat(q["time_start"][:10]), date.fromisoformat(q["time_end"][:10])
    day = np.arange((first - base).days, (last - base).days + 1, dtype="float64")
    vals = np.broadcast_to((10.0 + np.arange(day.size))[:, None, None], (day.size, lat.size, lon.size))
    ds = xr.Dataset({q["var"]: (("day", "lat", "lon"), vals.astype("float32"))},
                    coords={"day": ("day", day, {"units": "days since 1900-01-01 00:00:00"}),
                            "lat": ("lat", lat), "lon": ("lon", lon)})
    return bytes(ds.to_netcdf(engine="scipy", format="NETCDF3_64BIT"))


class SyntheticNcss:
    """NCSS stand-in for any bbox (region-scale tests)."""

    def __init__(self, end: str = "2026-06-03T00:00:00Z"):
        self.end, self.calls = end, []
        self.probe = ConcurrencyProbe(delay_s=0.01)

    def get(self, url, timeout=None, **kw):
        if url.endswith("/dataset.xml"):
            return FakeResponse(200, dataset_xml(self.end).encode())
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.probe.enter(q["var"])
        try:
            with self.probe.lock:
                self.calls.append(q)
            time.sleep(self.probe.delay_s)
            return FakeResponse(200, synthetic_subset(q))
        finally:
            self.probe.leave(q["var"])


def write_gridmet_config(root: Path, names) -> None:
    ds = yaml.safe_load((REPO_CONFIG / "datasets" / "gridmet.yaml").read_text())
    ds["variables"] = [v for v in ds["variables"] if v["name"] in names]
    ds["options"]["retry_backoff_s"] = 0
    (root / "datasets" / "gridmet.yaml").write_text(yaml.safe_dump(ds))


@pytest.fixture
def gridmet_config(tmp_path, monkeypatch) -> Path:
    """Config dir with the real smm AOI and the real gridmet.yaml reduced to the six fixture variables (no lake
    region, no retry delay)."""
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "aois" / "smm.yaml").write_text((REPO_CONFIG / "aois" / "smm.yaml").read_text())
    write_gridmet_config(root, FIXTURE_VARS)
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


@pytest.fixture
def ca_config(tmp_path, monkeypatch) -> Path:
    """Config dir with the real ca AOI, the California region (lake.yaml) and gridmet.yaml reduced to vs, fm100."""
    root = tmp_path / "config"
    for d in ("aois", "datasets", "regions"):
        (root / d).mkdir(parents=True)
    shutil.copy(REPO_CONFIG / "lake.yaml", root / "lake.yaml")
    for f in ("california.yaml", "california.geojson"):
        shutil.copy(REPO_CONFIG / "regions" / f, root / "regions" / f)
    (root / "aois" / "ca.yaml").write_text((REPO_CONFIG / "aois" / "ca.yaml").read_text())
    write_gridmet_config(root, ("vs", "fm100"))
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


def raw_files(lake: Lake) -> list[Path]:
    return sorted(p for p in Path(lake.root, "raw").rglob("*") if p.is_file())


def lattice(bbox) -> tuple[np.ndarray, np.ndarray]:
    """gridMET cell centres (lon west to east, lat north to south) inside ``bbox``."""
    w, s, e, n = bbox
    i = np.arange(np.ceil((w - GRIDMET_LON0) / RES), np.floor((e - GRIDMET_LON0) / RES) + 1)
    j = np.arange(np.ceil((GRIDMET_LAT0 - n) / RES), np.floor((GRIDMET_LAT0 - s) / RES) + 1)
    return GRIDMET_LON0 + i * RES, GRIDMET_LAT0 - j * RES


def lonlat_grid(lon, lat):
    import rioxarray  # noqa: F401
    import xarray as xr

    ds = xr.Dataset(coords={"x": lon, "y": lat})
    return ds.rio.set_spatial_dims(x_dim="x", y_dim="y").rio.write_crs("EPSG:4326")


# ---------------------------------------------------------------------------- config
def test_config_declares_verified_variables():
    ds = load_dataset("gridmet")
    assert ds.connector == "fuellake.connectors.gridmet:GridmetConnector"
    names = {v.name: v.model_extra.get("source_name") for v in ds.variables}
    assert names == {"vs": "daily_mean_wind_speed", "th": "daily_mean_wind_direction",
                     "fm100": "dead_fuel_moisture_100hr", "fm1000": "dead_fuel_moisture_1000hr",
                     "rmin": "daily_minimum_relative_humidity", "vpd": "daily_mean_vapor_pressure_deficit",
                     "erc": "daily_mean_energy_release_component-g", "pr": "precipitation_amount",
                     "tmmx": "daily_maximum_temperature", "pet": "daily_mean_reference_evapotranspiration_grass",
                     "bi": "daily_mean_burning_index_g"}
    # the four training variables, as specified and checked live (THREDDS units mm, K, mm, Unitless)
    for name, unit, valid in (("pr", "mm", (0.0, 500.0)), ("tmmx", "K", (200.0, 340.0)),
                              ("pet", "mm", (0.0, 30.0)), ("bi", "1", (0.0, 400.0))):
        v = ds.variable(name)
        assert (v.unit, tuple(v.valid_range), v.dtype, v.resampling) == (unit, valid, "float32", "bilinear"), name
        assert np.isnan(v.nodata)
    assert ds.variable("th").resampling == "nearest"   # circular quantity
    assert ds.license.id == "CC0-1.0" and ds.license.redistributable is True


def test_lag_flags_reach_the_features_layer():
    ds = load_dataset("gridmet")
    assert {v.name for v in ds.variables if v.lag} == LAG_VARIABLES
    assert LAG_VARIABLES <= set(config_lag_variables())
    assert any("Lagged variables" in c for c in ds.caveats) and any("single days" in c for c in ds.caveats)


def test_month_starts_cover_whole_months():
    assert month_starts(Period(date(2026, 6, 15), date(2026, 8, 2))) == [date(2026, 6, 1), date(2026, 7, 1),
                                                                         date(2026, 8, 1)]
    assert month_starts(Period(date(2025, 12, 31), date(2026, 1, 1))) == [date(2025, 12, 1), date(2026, 1, 1)]


def test_parse_timespan_end():
    assert parse_timespan_end(dataset_xml("2026-11-12T00:00:00Z")) == date(2026, 11, 12)
    with pytest.raises(ValueError):
        parse_timespan_end("<gridDataset/>")


# ---------------------------------------------------------------------------- reading
def test_read_ncss_variable_decodes_packing(lake):
    lake.write_bytes("raw/x/vs.nc", (FIX / "gridmet_vs_20260601_03.nc").read_bytes())
    lake.write_bytes("raw/x/fm100.nc", (FIX / "gridmet_fm100_20260601_03.nc").read_bytes())
    vs = read_ncss_variable(lake, "raw/x/vs.nc", "daily_mean_wind_speed", "vs")
    fm = read_ncss_variable(lake, "raw/x/fm100.nc", "dead_fuel_moisture_100hr", "fm100")
    assert vs.dims == ("time", "y", "x") and vs.dtype == np.float32
    assert vs.sizes["time"] == 3 and str(vs.time.values[0])[:10] == "2026-06-01"
    assert np.all(np.diff(vs.y.values) < 0) and np.all(np.diff(vs.x.values) > 0)   # north-up, west-east
    assert np.isnan(vs.values).any()                     # ocean cells: fill value decoded to NaN
    assert 0.0 < np.nanmin(vs.values) and np.nanmax(vs.values) < 20.0      # m/s after scale_factor 0.1
    assert 3.0 < np.nanmin(fm.values) and np.nanmax(fm.values) < 40.0      # percent after scale_factor 0.1
    assert not vs.encoding and not fm.encoding           # no int16 packing leaks into the Zarr store


def test_read_ncss_variable_decodes_new_variables(lake):
    out = {}
    for short in ("vs", "pr", "tmmx", "pet", "bi"):
        lake.write_bytes(f"raw/x/{short}.nc", (FIX / f"gridmet_{short}_20260601_03.nc").read_bytes())
        out[short] = read_ncss_variable(lake, f"raw/x/{short}.nc", FIXTURE_VARS[short], short)
    vs = out.pop("vs")
    for short, da in out.items():
        assert da.dims == ("time", "y", "x") and da.dtype == np.float32 and not da.encoding, short
        assert np.array_equal(np.isnan(da.values), np.isnan(vs.values)), short   # same ocean cells
    # tmmx is int16 with scale 0.1 and add_offset 220: early-June maxima in the Santa Monica Mountains, in Kelvin
    assert 285.0 < np.nanmin(out["tmmx"].values) and np.nanmax(out["tmmx"].values) < 315.0
    assert 0.0 <= np.nanmin(out["pr"].values) and np.nanmax(out["pr"].values) < 50.0      # mm/day, dry season
    assert 1.0 < np.nanmin(out["pet"].values) and np.nanmax(out["pet"].values) < 15.0     # mm/day
    assert 5.0 < np.nanmin(out["bi"].values) and np.nanmax(out["bi"].values) < 100.0      # BI-G


def test_read_ncss_variable_reads_gzip_raw(lake):
    import xarray as xr

    body = (FIX / "gridmet_tmmx_20260601_03.nc").read_bytes()
    lake.write_bytes("raw/x/t.nc", body)
    lake.write_bytes("raw/x/t.nc.gz", gzip.compress(body, mtime=0))
    a = read_ncss_variable(lake, "raw/x/t.nc", "daily_maximum_temperature", "tmmx")
    b = read_ncss_variable(lake, "raw/x/t.nc.gz", "daily_maximum_temperature", "tmmx")
    xr.testing.assert_identical(a, b)
    assert raw_netcdf_size(lake, "raw/x/t.nc.gz") == raw_netcdf_size(lake, "raw/x/t.nc") == len(body)


def test_clip_lonlat_keeps_one_buffer_cell(lake):
    lake.write_bytes("raw/x/vs.nc", (FIX / "gridmet_vs_20260601_03.nc").read_bytes())
    da = read_ncss_variable(lake, "raw/x/vs.nc", "daily_mean_wind_speed", "vs")
    clipped = clip_lonlat(da.to_dataset(), SMM_BBOX, RES, buffer_cells=1)
    x, y = clipped.x.values, clipped.y.values
    w, s, e, n = SMM_BBOX
    # the outermost kept centres lie within one cell outside the bbox, cells further out are dropped
    assert w - 1.5 * RES < x.min() < w and e < x.max() < e + 1.5 * RES
    assert s - 1.5 * RES < y.min() < s and n < y.max() < n + 1.5 * RES
    assert da.sizes["x"] > clipped.sizes["x"] and da.sizes["y"] > clipped.sizes["y"]
    assert np.allclose(np.diff(x), RES, atol=1e-6)


def test_subset_problem_flags_damaged_responses(tmp_path):
    import netCDF4

    body = (FIX / "gridmet_vs_20260601_03.nc").read_bytes()
    box = buffered_bbox(SMM_BBOX, 2, RES)
    d1, d3 = date(2026, 6, 1), date(2026, 6, 3)
    assert subset_problem(body, d1, d3, box, RES) is None
    assert "does not match" in subset_problem(body, date(2026, 6, 2), date(2026, 6, 4), box, RES)
    assert "expected 4 days" in subset_problem(body, d1, date(2026, 6, 4), box, RES)
    assert "outside the requested" in subset_problem(body, d1, d3, (-121.0, 33.9, -120.5, 34.3), RES)
    assert subset_problem(b"CDF\x01 not really", d1, d3, box, RES).startswith("unreadable NetCDF")
    # what NKN returned for 1 of 44 concurrent same-dataset requests: a day axis of denormal floats and NaN
    p = tmp_path / "damaged.nc"
    p.write_bytes(body)
    with netCDF4.Dataset(p, "r+") as d:
        d["day"][:] = [8.3e-308, 7.8e-308, np.nan]
    assert "finite" in subset_problem(p.read_bytes(), d1, d3, box, RES)


# ---------------------------------------------------------------------------- region bands
def test_mosaic_bands_lattice_overlap_and_gaps(lake):
    lake.write_bytes("raw/x/vs.nc", (FIX / "gridmet_vs_20260601_03.nc").read_bytes())
    da = read_ncss_variable(lake, "raw/x/vs.nc", "daily_mean_wind_speed", "vs")
    north, south = da.isel(y=slice(0, 6)), da.isel(y=slice(4, None))           # rows 4-5 in both
    south = south.assign_coords(x=south.x + 1e-7 * RES)                         # float jitter: placed by index
    m, cov = mosaic_bands([south, north], RES)
    assert cov.all() and np.array_equal(m.values, da.values, equal_nan=True)
    assert np.allclose(m.x, da.x, atol=1e-6) and np.allclose(m.y, da.y, atol=1e-9)
    gap, gcov = mosaic_bands([da.isel(y=slice(0, 3)), da.isel(y=slice(6, None))], RES)
    assert gap.sizes == da.sizes and gcov[:3].all() and gcov[6:].all() and not gcov[3:6].any()
    assert np.isnan(gap.values[:, 3:6]).all() and np.allclose(gap.y, da.y, atol=1e-6)
    with pytest.raises(RuntimeError, match="disagree"):
        mosaic_bands([north, south.copy(data=south.values + 1)], RES)
    with pytest.raises(RuntimeError, match="time axes"):
        mosaic_bands([north, south.isel(time=slice(0, 2))], RES)
    one, ocov = mosaic_bands([da], RES)
    assert one is da and ocov.all()


def test_request_boxes_cover_california(lake):
    conn = GridmetConnector(load_dataset("gridmet"), lake, Settings.from_env())
    region, ca = lake_region(), load_aoi("ca")
    outer = buffered_bbox(ca.bbox, conn.request_buffer_cells, RES)
    boxes = conn.request_boxes(ca)
    assert len(boxes) == 4
    lon, lat = lattice(outer)
    inside = zarr_inside_mask(lonlat_grid(lon, lat), region, tolerance_m=region.tolerance_m)
    lo, la = np.meshgrid(lon, lat)
    requested = np.zeros(lo.shape, dtype=bool)
    for w, s, e, n in boxes:
        assert outer[0] <= w < e <= outer[2] and outer[1] <= s < n <= outer[3]
        requested |= (lo >= w) & (lo <= e) & (la >= s) & (la <= n)
    assert requested[inside].all()            # every cell touching (buffered) California is requested
    assert 0.5 < requested.mean() < 0.7       # about 64 % of the rectangle instead of all of it
    # local AOIs keep one box: the bbox plus request_buffer_cells
    assert conn.request_boxes(load_aoi("smm")) == [buffered_bbox(SMM_BBOX, conn.request_buffer_cells, RES)]


def test_check_band_coverage_raises_on_a_missed_cell():
    import xarray as xr

    region = lake_region()
    lon, lat = lattice(buffered_bbox(load_aoi("ca").bbox, 1, RES))
    ds = xr.Dataset(coords={"x": lon, "y": lat})
    inside = zarr_inside_mask(lonlat_grid(lon, lat), region)
    check_band_coverage(ds, inside.copy(), region)
    missed = inside.copy()
    missed[np.argwhere(inside)[0][0], np.argwhere(inside)[0][1]] = False
    with pytest.raises(RuntimeError, match="miss 1 cells"):
        check_band_coverage(ds, missed, region)


def test_region_bands_offline_pipeline(ca_config, lake, monkeypatch):
    """ca on a synthetic NCSS server: 4 bands per variable, never two requests for one variable at once, and
    after the region clip exactly the cells touching California hold data."""
    fake = SyntheticNcss()
    monkeypatch.setattr(GridmetConnector, "http_session", lambda self, bearer_env=None: fake)
    rep = pipeline.run(["gridmet"], "ca", Period(date(2026, 6, 1), date(2026, 6, 3)), lake=lake)
    assert rep.ok, rep.to_dict()
    assert len(fake.calls) == 2 * 4 and fake.probe.max_same == 1
    (item,) = list(catalog.iter_items(lake, "gridmet"))
    props = item["properties"]
    assert len(props["gridmet:request_boxes"]) == 4 and props["gridmet:shape"][0] == 3
    files = raw_files(lake)
    assert len(files) == 8 and all(re.search(r"ca_(vs|fm100)_202606_b[0-3]\.nc\.gz$", p.name) for p in files)
    assert props["gridmet:download_bytes"] == sum(len(gzip.decompress(p.read_bytes())) for p in files)
    assert props["gridmet:raw_stored_bytes"] == sum(p.stat().st_size for p in files)
    ds = open_zarr(lake, catalog.asset_rel(item, "data"))
    inside = zarr_inside_mask(ds, lake_region())
    for name in ("vs", "fm100"):
        finite = np.isfinite(ds[name].values)
        assert np.array_equal(finite[0], inside) and (finite == finite[0]).all(), name
    assert np.allclose(ds["vs"].values[:, inside], np.array([10.0, 11.0, 12.0])[:, None])
    assert all(r.ok for r in validate_collection(lake, load_dataset("gridmet")))


# ---------------------------------------------------------------------------- pipeline (smm fixtures)
def test_discover_keys_complete_and_open_months(gridmet_config, lake):
    conn = GridmetConnector(load_dataset("gridmet"), lake, Settings.from_env())
    fake = FakeGridmetSession(end="2026-11-12T00:00:00Z")
    conn.http_session = lambda bearer_env=None: fake
    refs = conn.discover(load_aoi("smm"), Period(date(2026, 10, 15), date(2026, 12, 31)))
    assert [r.key for r in refs] == ["gridmet-202610", "gridmet-202611-through-20261112"]   # December skipped
    assert refs[1].meta["last_day"] == "2026-11-12" and refs[1].meta["complete"] is False
    assert len(refs[0].urls) == len(FIXTURE_VARS)                     # one box for a local AOI
    assert all("ncss/agg_met_" in u and "accept=netcdf" in u for u in refs[0].urls)


def test_pipeline_offline_run_is_idempotent(gridmet_config, lake, monkeypatch):
    fake = FakeGridmetSession()
    monkeypatch.setattr(GridmetConnector, "http_session", lambda self, bearer_env=None: fake)
    period = Period(date(2026, 6, 1), date(2026, 6, 3))
    rep = pipeline.run(["gridmet"], "smm", period, lake=lake)
    assert rep.ok, rep.to_dict()
    assert rep.counts()["created"] == 1
    assert 1 < fake.probe.max_active <= 4 and fake.probe.max_same == 1   # parallel, but politely
    items = list(catalog.iter_items(lake, "gridmet"))
    assert [i["id"] for i in items] == ["gridmet__smm__20260601"]
    props = items[0]["properties"]
    assert props["fuellake:source_key"] == "gridmet-202606-through-20260603"
    assert props["gridmet:last_day"] == "2026-06-03" and props["gridmet:complete_month"] is False
    assert props["gridmet:download_bytes"] == sum((FIX / f"gridmet_{s}_20260601_03.nc").stat().st_size
                                                  for s in FIXTURE_VARS)
    assert props["gridmet:qc_masked_cells"] == {s: 0 for s in FIXTURE_VARS}
    assert sorted(p.name for p in raw_files(lake)) == sorted(f"smm_{s}_202606.nc.gz" for s in FIXTURE_VARS)
    ds = open_zarr(lake, catalog.asset_rel(items[0], "data"))
    assert set(ds.data_vars) == set(FIXTURE_VARS) and ds["vs"].dims == ("time", "y", "x")
    assert ds["vs"].attrs["units"] == "m/s" and ds["tmmx"].attrs["units"] == "K" and ds.rio.crs.to_epsg() == 4326
    assert 285.0 < float(ds["tmmx"].min()) and float(ds["tmmx"].max()) < 315.0
    assert all(r.ok for r in validate_collection(lake, load_dataset("gridmet")))
    rep2 = pipeline.run(["gridmet"], "smm", period, lake=lake)
    assert rep2.ok and rep2.counts()["skipped"] == 1 and rep2.counts()["created"] == 0


def test_server_errors_are_retried(gridmet_config, lake, monkeypatch):
    fake = FakeGridmetSession(script={"daily_mean_wind_speed": [503, 502]})
    monkeypatch.setattr(GridmetConnector, "http_session", lambda self, bearer_env=None: fake)
    rep = pipeline.run(["gridmet"], "smm", Period(date(2026, 6, 1), date(2026, 6, 3)), lake=lake)
    assert rep.ok, rep.to_dict()
    assert fake.var_calls("daily_mean_wind_speed") == 3 and fake.var_calls("precipitation_amount") == 1


def test_client_errors_fail_without_retry_and_write_no_raw(gridmet_config, lake, monkeypatch):
    fake = FakeGridmetSession(script={"daily_mean_wind_speed": [404]})
    monkeypatch.setattr(GridmetConnector, "http_session", lambda self, bearer_env=None: fake)
    rep = pipeline.run(["gridmet"], "smm", Period(date(2026, 6, 1), date(2026, 6, 3)), lake=lake)
    assert not rep.ok and rep.counts()["failed"] == 1
    assert "HTTP 404" in rep.datasets[0].units[0].error
    assert fake.var_calls("daily_mean_wind_speed") == 1
    assert raw_files(lake) == [] and list(catalog.iter_items(lake, "gridmet")) == []


def _short_fm100(tmp_path) -> bytes:
    """The fm100 fixture with only 2 of its 3 days (a variable lagging behind dataset.xml)."""
    import xarray as xr

    src = tmp_path / "fm100.nc"
    src.write_bytes((FIX / "gridmet_fm100_20260601_03.nc").read_bytes())
    with xr.open_dataset(src) as ds:
        ds.isel(day=slice(0, 2)).load().to_netcdf(tmp_path / "fm100_2days.nc")
    return (tmp_path / "fm100_2days.nc").read_bytes()


@pytest.mark.filterwarnings("ignore:.*_Unsigned attribute")
def test_damaged_response_is_fetched_again(gridmet_config, lake, monkeypatch, tmp_path):
    fake = FakeGridmetSession(script={"dead_fuel_moisture_100hr": [_short_fm100(tmp_path)]})
    monkeypatch.setattr(GridmetConnector, "http_session", lambda self, bearer_env=None: fake)
    rep = pipeline.run(["gridmet"], "smm", Period(date(2026, 6, 1), date(2026, 6, 3)), lake=lake)
    assert rep.ok, rep.to_dict()
    assert fake.var_calls("dead_fuel_moisture_100hr") == 2 and fake.var_calls("daily_mean_wind_speed") == 1
    ds = open_zarr(lake, catalog.asset_rel(next(catalog.iter_items(lake, "gridmet")), "data"))
    assert ds.sizes["time"] == 3


@pytest.mark.filterwarnings("ignore:.*_Unsigned attribute")
def test_persistently_short_variable_fails_the_unit(gridmet_config, lake, monkeypatch, tmp_path):
    """A variable on a different day range (server mid-update) must not be staged ragged."""
    short = _short_fm100(tmp_path)
    fake = FakeGridmetSession(script={"dead_fuel_moisture_100hr": [short] * 4})
    monkeypatch.setattr(GridmetConnector, "http_session", lambda self, bearer_env=None: fake)
    rep = pipeline.run(["gridmet"], "smm", Period(date(2026, 6, 1), date(2026, 6, 3)), lake=lake)
    assert not rep.ok and rep.counts()["failed"] == 1
    err = rep.datasets[0].units[0].error
    assert "fm100: damaged response after 4 fetches" in err and "expected 3 days" in err
    assert raw_files(lake) == [] and list(catalog.iter_items(lake, "gridmet")) == []


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_latest_day_and_ncss_subset(tmp_path):
    lake = Lake(tmp_path / "live")
    conn = GridmetConnector(load_dataset("gridmet"), lake, Settings.from_env())
    latest = conn.latest_day()
    assert (date.today() - latest).days < 30, latest
    aoi = load_aoi("smm")
    first = date(latest.year, latest.month, 1)
    refs = conn.discover(aoi, Period(first, latest))
    assert len(refs) == 1
    r = conn.http_session().get(conn.ncss_url("vs", SMM_BBOX, first, first), timeout=120)
    assert r.status_code == 200 and r.content[:3] in (b"CDF", b"\x89HD")
    lake.write_bytes("raw/vs.nc", r.content)
    vs = read_ncss_variable(lake, "raw/vs.nc", "daily_mean_wind_speed", "vs")
    assert vs.sizes["time"] == 1 and 0.0 <= np.nanmax(vs.values) < 60.0


@pytest.mark.network
def test_live_variable_names_and_units(tmp_path):
    """Every configured source_name is a grid of its NCSS dataset; the new variables have the specified units."""
    conn = GridmetConnector(load_dataset("gridmet"), Lake(tmp_path / "live"), Settings.from_env())
    s = conn.http_session()
    units = {}
    for v in conn.dataset.variables:
        r = s.get(f"{conn.thredds}/ncss/{conn.source_dataset(v.name)}/dataset.xml", timeout=60)
        assert r.status_code == 200, v.name
        m = re.search(rf'<grid name="{re.escape(conn.source_name(v.name))}".*?'
                      r'<attribute name="units" value="([^"]*)"', r.text, re.S)
        assert m, f"{v.name}: grid {conn.source_name(v.name)} not in dataset.xml"
        units[v.name] = m.group(1)
    assert {k: units[k] for k in ("pr", "tmmx", "pet", "bi")} == {"pr": "mm", "tmmx": "K", "pet": "mm",
                                                                 "bi": "Unitless"}


@pytest.mark.network
def test_live_region_bands_cover_california(tmp_path):
    """One settled day of pr on ca: the 4 band subsets are well formed, mosaic, and cover every California cell."""
    import xarray as xr

    lake = Lake(tmp_path / "live")
    conn = GridmetConnector(load_dataset("gridmet"), lake, Settings.from_env())
    ca = load_aoi("ca")
    day = conn.latest_day() - timedelta(days=10)
    s = conn.http_session()
    parts = []
    for k, box in enumerate(conn.request_boxes(ca)):       # sequential: one request at a time
        body = conn.fetch(s, conn.ncss_url("pr", box, day, day), f"pr band {k}")
        assert subset_problem(body, day, day, box, RES) is None
        lake.write_bytes(f"raw/pr_{k}.nc", body)
        parts.append(read_ncss_variable(lake, f"raw/pr_{k}.nc", "precipitation_amount", "pr"))
    da, covered = mosaic_bands(parts, RES)
    sub = clip_lonlat(xr.Dataset({"pr": da, "covered": (("y", "x"), covered)}), ca.bbox, RES, 1)
    check_band_coverage(sub, sub["covered"].values, lake_region())
    assert 0.0 <= float(sub["pr"].min()) and float(sub["pr"].max()) < 500.0
