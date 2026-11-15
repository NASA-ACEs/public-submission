# fuel-lake — tests for landfire-topo (LANDFIRE LF 2020 elevation, slope, aspect: encodings, resampling, pipeline)
# Licensed under Apache-2.0.
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import yaml
from affine import Affine

from conftest import TINY_AOI
from fuellake import catalog, pipeline
from fuellake.connectors.base import Period
from fuellake.connectors.landfire import parse_metadata
from fuellake.connectors.landfire_topo import (
    LandfireTopoConnector,
    circular_mean_reproject,
    codes_to_values,
    default_urls,
)
from fuellake.contracts import validate_collection
from fuellake.grid import Grid
from fuellake.rasters import read_cog
from fuellake.registry import load_aoi, load_dataset
from landfire_fakes import LF_XML, FakeLandfire

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
DS = "landfire-topo"
PERIOD = Period(date(2026, 6, 1), date(2026, 6, 2))


@pytest.fixture
def topo_config(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    (root / "aois" / "tiny90.yaml").write_text(yaml.safe_dump({**TINY_AOI, "id": "tiny90", "resolution": 90}))
    shutil.copy(REPO_CONFIG / "datasets" / f"{DS}.yaml", root / "datasets" / f"{DS}.yaml")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


# ---------------------------------------------------------------------------- facts and encodings
def test_default_urls_and_config():
    u = default_urls("SlpD")
    assert u["service"] == "https://lfps.usgs.gov/arcgis/rest/services/Landfire_Topo/LF2020_SlpD_CONUS/ImageServer"
    assert u["metadata_xml"] == "https://landfire.gov/sites/default/files/metadata/lf2020/CONUS/LC20_SlpD_220.xml"
    ds = load_dataset(DS)
    assert ds.is_static and [v.name for v in ds.variables] == ["elevation", "slope", "aspect"]
    assert {v.name: v.unit for v in ds.variables} == {"elevation": "m", "slope": "degree", "aspect": "degree"}
    assert ds.license.redistributable and ds.scales == ["local"]


@pytest.mark.parametrize("name, title", [("LC20_Elev_220.xml", "LANDFIRE 2020 Elevation (Elev) CONUS"),
                                         ("LC20_SlpD_220.xml", "LANDFIRE 2020 Slope Degrees (SlpD) CONUS"),
                                         ("LC20_Asp_220.xml", "LANDFIRE 2020 Aspect (Asp) CONUS")])
def test_real_fgdc_metadata(name, title):
    text = (LF_XML / name).read_text()
    m = parse_metadata(text)
    assert (m["title"], m["edition"], m["pubdate"], m["ground_condition"], m["use_constraints"]) == \
        (title, "LF 2020", "2022-01-31", "2020 ground condition", "None")
    if "Asp" in name:   # the encodings this connector relies on are stated in the abstract
        assert "slope less than or equal to two (2) degrees were assigned a value of negative one (-1)" in text
        assert "to reflect true north" in text
    if "Elev" in name:
        assert "meters above sea level" in text and "<vertdef/>" in text   # vertical datum not stated


def test_codes_to_values():
    codes = np.array([[-9999, -1, 0, 359], [100, 2, 270, -9999]], dtype="int16")
    asp = codes_to_values(codes, "aspect")
    assert np.isnan(asp[0, 0]) and np.isnan(asp[0, 1]) and asp[0, 2] == 0 and asp[0, 3] == 359
    elev = codes_to_values(codes, "continuous")
    assert np.isnan(elev[0, 0]) and elev[0, 1] == -1 and elev[1, 0] == 100 and elev.dtype == np.float32


def test_circular_mean_wraps_at_north():
    src = np.array([[350, 10], [355, 5]], dtype="float32")
    grid = Grid(crs="EPSG:32611", transform=Affine(60, 0, 0, 0, -60, 60), width=1, height=1, resolution=60)
    out = circular_mean_reproject(src, "EPSG:32611", Affine(30, 0, 0, 0, -30, 60), grid)
    assert out.shape == (1, 1) and (out[0, 0] < 1 or out[0, 0] > 359)          # not 180
    src[:] = np.nan
    assert np.isnan(circular_mean_reproject(src, "EPSG:32611", Affine(30, 0, 0, 0, -30, 60), grid)[0, 0])


# ---------------------------------------------------------------------------- offline pipeline
def test_pipeline_offline_topo(lake, topo_config, monkeypatch):
    fake = FakeLandfire().patch(monkeypatch, LandfireTopoConnector)
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", d.error or [u.error for u in d.units]
    item = next(catalog.iter_items(lake, DS))
    assert item["id"] == "landfire-topo__tiny__20200101__lf2020"
    p = item["properties"]
    assert p["fuellake:source_key"] == ("landfire:Landfire_Topo/LF2020_Asp_CONUS+Landfire_Topo/LF2020_Elev_CONUS+"
                                        "Landfire_Topo/LF2020_SlpD_CONUS")
    assert p["fuellake:resampling"] == {"elevation": "bilinear", "slope": "bilinear", "aspect": "nearest"}
    assert p["fuellake:landfire_layers"]["elevation"]["edition"] == "LF 2020"
    assert p["fuellake:landfire_layers"]["aspect"]["use_constraints"] == "None"
    assert "true north" in p["fuellake:aspect_convention"] and "bare-earth" in p["fuellake:surface_model"]
    assert p["start_datetime"] == "2020-01-01T00:00:00Z"
    assert 0 < p["fuellake:qc_masked_fraction_by_variable"]["slope"] < 0.05    # the 32767 corner
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))
    for k in ("elevation", "slope", "aspect"):
        assert item["assets"][k]["raster:bands"][0]["data_type"] == "float32"

    grid = load_aoi("tiny").grid
    elev, g = read_cog(lake, catalog.asset_rel(item, "elevation"))
    slope, _ = read_cog(lake, catalog.asset_rel(item, "slope"))
    asp, _ = read_cog(lake, catalog.asset_rel(item, "aspect"))
    assert g.shape == grid.shape and elev.dtype == np.float32
    land = np.isfinite(elev) & (elev > 0)
    assert land.mean() > 0.5 and np.nanmax(elev) > np.nanmin(elev[land]) + 30     # the eastward ramp survives
    assert (elev[np.isfinite(elev)] >= 0).all()                                    # 0 = coastal fill kept, not NaN
    s = slope[land & np.isfinite(slope)]
    assert np.median(s) == pytest.approx(2.0) and s.min() >= 0 and s.max() <= 2.0 + 1e-4   # 0 only near the sea fill
    vals = set(np.unique(asp[np.isfinite(asp)]).tolist())
    assert vals <= {270.0, 359.0} and 270.0 in vals                                # nearest: no invented directions
    assert np.isnan(asp).mean() > 0.2                                              # flat (-1) and nodata -> NaN
    assert fake.calls_to("/exportImage") == 3 and fake.calls_to(".xml") == 3

    n = len(fake.calls)
    rep2 = pipeline.run([DS], "tiny", Period(date(2026, 7, 1), date(2026, 7, 2)), lake=lake)
    assert (rep2.datasets[0].skipped, len(rep2.datasets[0].units)) == (1, 0)
    assert len(fake.calls) == n + 3                                                # the three service checks only


def test_coarser_grid_uses_average_and_circular_mean(lake, topo_config, monkeypatch):
    FakeLandfire().patch(monkeypatch, LandfireTopoConnector)
    rep = pipeline.run([DS], "tiny90", PERIOD, lake=lake)
    assert rep.datasets[0].status == "ok", [u.error for u in rep.datasets[0].units]
    item = next(catalog.iter_items(lake, DS))
    assert item["properties"]["fuellake:resampling"] == {"elevation": "average", "slope": "average",
                                                         "aspect": "circular-mean"}
    asp, _ = read_cog(lake, catalog.asset_rel(item, "aspect"))
    finite = asp[np.isfinite(asp)]
    assert finite.size and ((finite >= 0) & (finite < 360)).all()


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_topo_tiny_aoi(lake, topo_config):
    rep = pipeline.run([DS], "tiny", Period(date(2026, 11, 1), date(2026, 11, 14)), lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", d.error or [u.error for u in d.units]
    item = next(catalog.iter_items(lake, DS))
    layers = item["properties"]["fuellake:landfire_layers"]
    assert all(v["source_dtype"] == "int16" and v["source_nodata"] == -9999 for v in layers.values())
    elev, _ = read_cog(lake, catalog.asset_rel(item, "elevation"))
    slope, _ = read_cog(lake, catalog.asset_rel(item, "slope"))
    asp, _ = read_cog(lake, catalog.asset_rel(item, "aspect"))
    assert 0 <= np.nanmin(elev) and np.nanmax(elev) < 1000          # Santa Monica Mountains, below 1 km
    assert 5 < np.nanmean(slope) < 45 and np.nanmax(slope) <= 90
    assert np.isfinite(asp).mean() > 0.5 and np.nanmax(asp) < 360
    assert layers["elevation"]["edition"] == "LF 2020"
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))
