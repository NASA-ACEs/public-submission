# fuel-lake — tests for the GEDI L4B connector (CMR granule selection, token hosts, windowed clip, nearest upsampling)
# Licensed under Apache-2.0.
from __future__ import annotations

import contextlib
import json
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml
from affine import Affine

from conftest import TINY_AOI
from fuellake import catalog, pipeline
from fuellake.connectors import gedi_l4b
from fuellake.connectors._earthdata import parse_granules
from fuellake.connectors.base import Period
from fuellake.connectors.gedi_l4b import GediL4bConnector, https_url, select_granules, wants_token
from fuellake.contracts import validate_collection
from fuellake.rasters import read_cog
from fuellake.registry import load_aoi, load_dataset

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
FIX = Path(__file__).parent / "fixtures" / "gedi_l4b"
DS = "gedi-l4b"
# EASE-Grid 2.0 global 1 km grid of the L4B files (user guide; confirmed on the live files 2026-11-14)
EASE = Affine(1000.8950233496674, 0.0, -17367530.45, 0.0, -1000.8950233496674, 7314540.83)
SUFFIXES = ["MU", "SE", "QF", "NS"]


def _cmr_items() -> list[dict]:
    return json.loads((FIX / "cmr_gedi_l4b_v21_granules.json").read_text())["items"]


# ---------------------------------------------------------------------------- CMR metadata
def test_select_granules_from_real_cmr_record():
    gr = parse_granules(_cmr_items())
    assert len(gr) == 10
    sel = select_granules(gr, SUFFIXES)
    assert set(sel) == set(SUFFIXES)
    assert sel["MU"].ur == "GEDI_L4B_Gridded_Biomass_V2_1.GEDI04_B_MW019MW223_02_002_02_R01000M_MU.tif"
    url = https_url(sel["SE"])
    assert url.startswith("https://data.ornldaac.earthdata.nasa.gov/protected/gedi/GEDI_L4B_Gridded_Biomass_V2_1/")
    assert sel["MU"].start.date() == date(2019, 4, 18) and sel["MU"].end.date() == date(2023, 3, 16)
    with pytest.raises(ValueError, match="not found"):
        select_granules(gr, ["MU", "XX"])


def test_token_only_for_configured_hosts():
    hosts = ["data.ornldaac.earthdata.nasa.gov"]
    assert wants_token("https://data.ornldaac.earthdata.nasa.gov/protected/gedi/x_MU.tif", hosts)
    assert not wants_token("https://d3o6w55j8uz1ro.cloudfront.net/x_MU.tif?Signature=1", hosts)
    assert not wants_token("http://data.ornldaac.earthdata.nasa.gov/protected/x.tif", hosts)
    assert not wants_token("https://cmr.earthdata.nasa.gov/search/granules.umm_json", hosts)


# ---------------------------------------------------------------------------- synthetic global-grid windows
def _write_sources(tmp: Path) -> dict[str, Path]:
    """Small rasters on the real EASE 1 km grid around the tiny AOI (aligned to the global origin)."""
    from rasterio.warp import transform_bounds

    w, s, e, n = transform_bounds("EPSG:4326", "EPSG:6933", *TINY_AOI["bbox"])
    inv = ~EASE
    c0, r0 = (int(np.floor(v)) - 6 for v in inv @ (w, n))
    c1, r1 = (int(np.ceil(v)) + 6 for v in inv @ (e, s))
    width, height = c1 - c0, r1 - r0
    tr = EASE @ Affine.translation(c0, r0)
    rows, cols = np.mgrid[0:height, 0:width]
    mu = (10.0 * cols + rows).astype("float32")
    mu[0, :] = -9999.0                                     # no hybrid estimate
    data = {"MU": (mu, -9999.0), "SE": ((mu * 0.2).astype("float32"), -9999.0),
            "QF": (np.where(mu < 0, 0, 1 + (cols % 2)).astype("uint8"), 0),
            "NS": (np.where(mu < 0, 0, 5 + rows).astype("uint32"), 0)}
    data["SE"][0][0, :] = -9999.0
    out = {}
    for sfx, (arr, nod) in data.items():
        p = tmp / f"GEDI04_B_MW019MW223_02_002_02_R01000M_{sfx}.tif"
        with rasterio.open(p, "w", driver="GTiff", width=width, height=height, count=1, dtype=arr.dtype.name,
                           crs="EPSG:6933", transform=tr, nodata=nod) as ds:
            ds.write(arr, 1)
        out[sfx] = p
    return out


@pytest.fixture
def gedi_config(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    shutil.copy(REPO_CONFIG / "datasets" / f"{DS}.yaml", root / "datasets" / f"{DS}.yaml")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


@pytest.fixture
def offline_gedi(tmp_path, monkeypatch):
    srcs = _write_sources(tmp_path)
    monkeypatch.setattr(gedi_l4b, "search_granules", lambda *a, **k: _cmr_items())
    orig = GediL4bConnector.open_raster

    @contextlib.contextmanager
    def fake_open(self, url):
        sfx = url.rsplit("_", 1)[-1].removesuffix(".tif")
        with orig(self, str(srcs[sfx])) as res:
            yield res

    monkeypatch.setattr(GediL4bConnector, "open_raster", fake_open)
    return srcs


PERIOD = Period(date(2026, 6, 1), date(2026, 6, 30))


def test_pipeline_offline_gedi(lake, gedi_config, offline_gedi, monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "test-token-not-real")
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", d.error or [u.error for u in d.units]
    item = next(catalog.iter_items(lake, DS))
    assert item["id"] == "gedi-l4b__tiny__20190418__v2-1"
    p = item["properties"]
    assert p["start_datetime"] == "2019-04-18T00:00:00Z" and p["end_datetime"].startswith("2023-03-16T23:59:59")
    assert p["fuellake:native_crs"] == "EPSG:6933" and p["fuellake:native_resolution_m"] == pytest.approx(1000.895)
    assert p["fuellake:resampling"] == "nearest" and p["fuellake:upsampling_factor"] == pytest.approx(33.36, abs=0.01)
    assert any(s.endswith("_MU.tif") for s in p["fuellake:source_ids"])
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))

    # nearest: every AOI pixel equals the source cell that contains its centre
    agbd, grid = read_cog(lake, catalog.asset_rel(item, "agbd"))
    qf, _ = read_cog(lake, catalog.asset_rel(item, "quality_flag"))
    assert qf.dtype == np.uint8 and set(np.unique(qf).tolist()) <= {0, 1, 2}
    xs, ys = grid.x_coords(), grid.y_coords()
    from pyproj import Transformer

    tr = Transformer.from_crs(grid.crs, "EPSG:6933", always_xy=True)
    with rasterio.open(offline_gedi["MU"]) as src:
        mu = src.read(1)
        for r, c in [(0, 0), (grid.height // 2, grid.width // 2), (grid.height - 1, grid.width - 1)]:
            ex, ey = tr.transform(xs[c], ys[r])
            sr, sc = src.index(ex, ey)
            assert agbd[r, c] == pytest.approx(mu[sr, sc])
    assert len(np.unique(agbd[np.isfinite(agbd)])) < agbd.size / 100          # blocks of identical values

    rep2 = pipeline.run([DS], "tiny", Period(date(2026, 7, 1), date(2026, 7, 2)), lake=lake)
    assert (rep2.datasets[0].skipped, len(rep2.datasets[0].units)) == (1, 0)


def test_nodata_becomes_nan(lake, gedi_config, offline_gedi, monkeypatch, tmp_path):
    """Make the whole first source row -9999 cover the AOI: those pixels must be NaN, never 0."""
    monkeypatch.setenv("EARTHDATA_TOKEN", "test-token-not-real")
    with rasterio.open(offline_gedi["MU"], "r+") as ds:
        a = ds.read(1)
        a[:, :] = -9999.0
        a[a.shape[0] // 2, a.shape[1] // 2] = 42.0
        ds.write(a, 1)
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    assert rep.datasets[0].status == "ok", [u.error for u in rep.datasets[0].units]
    item = next(catalog.iter_items(lake, DS))
    agbd, _ = read_cog(lake, catalog.asset_rel(item, "agbd"))
    assert set(np.unique(agbd[np.isfinite(agbd)]).tolist()) <= {42.0} and np.isnan(agbd).any()
    assert not (agbd == 0).any()


def test_lenient_run_without_token_is_skipped(lake, gedi_config, offline_gedi):
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake, skip_missing_credentials=True)
    d = rep.datasets[0]
    assert d.units and d.units[0].status == "missing_credentials" and d.missing_credentials == ["EARTHDATA_TOKEN"]
    assert not list(catalog.iter_items(lake, DS))


def test_remote_open_without_token_raises(lake, gedi_config):
    from fuellake.config import Settings
    from fuellake.connectors._earthdata import EarthdataAuthError

    conn = GediL4bConnector(load_dataset(DS), lake, Settings.from_env())
    with pytest.raises(EarthdataAuthError, match="EARTHDATA_TOKEN is not set"):
        with conn.open_raster("https://data.ornldaac.earthdata.nasa.gov/protected/gedi/x_MU.tif"):
            pass


# ---------------------------------------------------------------------------- live
@pytest.mark.earthdata
def test_live_gedi_tiny_aoi(lake, gedi_config):
    from fuellake.config import Settings

    if not Settings.has_secret("EARTHDATA_TOKEN"):
        pytest.skip("EARTHDATA_TOKEN not set")
    rep = pipeline.run([DS], "tiny", Period(date(2026, 11, 1), date(2026, 11, 14)), lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", d.error or [u.error for u in d.units]
    item = next(catalog.iter_items(lake, DS))
    agbd, grid = read_cog(lake, catalog.asset_rel(item, "agbd"))
    assert grid.shape == load_aoi("tiny").grid.shape
    vals = agbd[np.isfinite(agbd)]
    assert vals.size and 0 <= vals.min() and vals.max() < 400          # Santa Monica Mountains: shrubland/oak
    assert sum(item["properties"]["fuellake:bytes_transferred"].values()) < 5e6
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))
