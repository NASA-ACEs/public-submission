# fuel-lake — tests for the Sentinel-2 NDVI/NDMI connector (offline synthetic scenes + live Earth Search)
# Licensed under Apache-2.0.
from __future__ import annotations

import copy
import json
import math
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
from fuellake.connectors.base import Period
from fuellake.connectors.sentinel2 import (
    DEFAULT_MASK_CLASSES,
    Sentinel2IndicesConnector,
    band_scale_offset,
    composite_first_valid,
    group_by_date,
    normalized_difference,
    scl_clear,
    to_reflectance,
    unit_key,
)
from fuellake.contracts import validate_collection
from fuellake.rasters import read_cog
from fuellake.registry import load_aoi

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
FIXTURES = Path(__file__).parent / "fixtures" / "open_rasters"
DS = "sentinel2-indices"


@pytest.fixture
def s2_config(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    shutil.copy(REPO_CONFIG / "datasets" / f"{DS}.yaml", root / "datasets" / f"{DS}.yaml")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


def _fixture_item(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------- offsets and index math
def test_offset_c1_item_uses_raster_bands():
    item = _fixture_item("s2_c1_item.json")
    assert band_scale_offset(item, "red") == (0.0001, -0.1)
    assert band_scale_offset(item, "swir16") == (0.0001, -0.1)


def test_offset_legacy_harmonised_item_is_zero():
    item = _fixture_item("s2_legacy_item.json")
    assert item["properties"]["earthsearch:boa_offset_applied"] is True
    assert item["assets"]["red"]["raster:bands"][0]["offset"] == -0.1   # misleading metadata
    assert band_scale_offset(item, "red") == (0.0001, 0.0)


@pytest.mark.parametrize("baseline,expected", [("05.12", -0.1), ("04.00", -0.1), ("03.01", 0.0), (None, 0.0)])
def test_offset_from_processing_baseline_without_raster_bands(baseline, expected):
    item = {"properties": {"s2:processing_baseline": baseline}, "assets": {"red": {"href": "x"}}}
    assert band_scale_offset(item, "red") == (0.0001, expected)


def test_same_ndvi_from_c1_and_legacy_pixels():
    """Pixel values measured on scene 2026-11-11 T11SLT: C1 DN = legacy DN + 1000 (offset handling)."""
    c1, legacy = _fixture_item("s2_c1_item.json"), _fixture_item("s2_legacy_item.json")
    red_c1, nir_c1 = np.array([1299], "uint16"), np.array([2873], "uint16")
    red_lg, nir_lg = red_c1 - 1000, nir_c1 - 1000
    ndvi_c1 = normalized_difference(to_reflectance(nir_c1, *band_scale_offset(c1, "nir")),
                                    to_reflectance(red_c1, *band_scale_offset(c1, "red")))
    ndvi_lg = normalized_difference(to_reflectance(nir_lg, *band_scale_offset(legacy, "nir")),
                                    to_reflectance(red_lg, *band_scale_offset(legacy, "red")))
    expected = (0.1873 - 0.0299) / (0.1873 + 0.0299)
    assert ndvi_c1[0] == pytest.approx(expected, abs=1e-5)
    assert ndvi_lg[0] == pytest.approx(expected, abs=1e-5)
    naive = (2873 - 1299) / (2873 + 1299)        # ignoring the offset gives a badly wrong index
    assert abs(naive - expected) > 0.3


def test_to_reflectance_nodata_and_negative_values():
    r = to_reflectance(np.array([0, 900, 1000, 11000], "uint16"), 1e-4, -0.1)
    assert math.isnan(r[0])
    assert r[1:] == pytest.approx([-0.01, 0.0, 1.0], abs=1e-6)


def test_normalized_difference_guards():
    a = np.array([0.3, 0.05, np.nan, -0.02], "float32")
    b = np.array([0.1, -0.05, 0.1, -0.01], "float32")
    out = normalized_difference(a, b)
    assert out[0] == pytest.approx(0.5)
    assert np.isnan(out[1:]).all()           # sum <= 0 or NaN input


def test_scl_mask_classes():
    scl = np.arange(12, dtype="uint8")
    clear = scl_clear(scl, [0, 1, 2, 3, 8, 9, 10, 11])
    assert scl[clear].tolist() == [4, 5, 6, 7]
    assert not scl_clear(np.array([0], "uint8"), [9])[0]   # no data is always masked
    # default: water (6) masked too; only vegetation, not-vegetated and unclassified remain
    assert scl[scl_clear(scl)].tolist() == [4, 5, 7]
    cfg = yaml.safe_load((REPO_CONFIG / "datasets" / f"{DS}.yaml").read_text())
    assert tuple(cfg["options"]["scl_mask_classes"]) == DEFAULT_MASK_CLASSES


def test_composite_first_valid():
    a = np.array([1.0, np.nan, np.nan], "float32")
    b = np.array([2.0, 3.0, np.nan], "float32")
    assert composite_first_valid([a, b]).tolist()[:2] == [1.0, 3.0]


def test_group_by_date_and_stable_key():
    def it(i, dt):
        return {"id": i, "properties": {"datetime": dt}}
    items = [it("B", "2026-10-29T18:55:03Z"), it("A", "2026-10-29T18:45:03Z"), it("C", "2026-10-24T18:45:02Z")]
    groups = group_by_date(items)
    assert list(groups) == [date(2026, 10, 24), date(2026, 10, 29)]
    assert [s["id"] for s in groups[date(2026, 10, 29)]] == ["A", "B"]
    k1 = unit_key("sentinel-2-c1-l2a", date(2026, 10, 29), ["B", "A"])
    assert k1 == unit_key("sentinel-2-c1-l2a", date(2026, 10, 29), ["A", "B"])
    assert k1 != unit_key("sentinel-2-c1-l2a", date(2026, 10, 29), ["A"])
    assert k1.startswith("sentinel-2-c1-l2a:20261029:")


# ---------------------------------------------------------------------------- synthetic scenes
RED_A, RED_B, NIR, NIR08, SWIR16 = 1299, 1399, 2873, 2908, 2396
NDVI_A = (0.1873 - 0.0299) / (0.1873 + 0.0299)
NDVI_B = (0.1873 - 0.0399) / (0.1873 + 0.0399)
NDMI = (0.1908 - 0.1396) / (0.1908 + 0.1396)


def _write(path: Path, arr: np.ndarray, transform: Affine) -> str:
    with rasterio.open(path, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
                       dtype=arr.dtype, crs="EPSG:32611", transform=transform, nodata=0, tiled=True,
                       blockxsize=64, blockysize=64) as dst:
        dst.write(arr, 1)
    return str(path)


def _scene(tmp: Path, sid: str, dt: str, grid, scl_fn, red_dn: int, boa_applied: bool | None = None) -> dict:
    """Synthetic S2 scene covering the grid + 300 m, aligned like a real tile (60 m multiples)."""
    minx, miny, maxx, maxy = grid.bounds()
    x0, y1 = math.floor((minx - 300) / 60) * 60, math.ceil((maxy + 300) / 60) * 60
    x1, y0 = math.ceil((maxx + 300) / 60) * 60, math.floor((miny - 300) / 60) * 60
    w20, h20 = int((x1 - x0) / 20), int((y1 - y0) / 20)
    t20, t10 = Affine(20, 0, x0, 0, -20, y1), Affine(10, 0, x0, 0, -10, y1)
    yy, xx = np.mgrid[0:h20, 0:w20]
    # pixel centres in metres, relative to the grid's centre
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    px, py = x0 + (xx + 0.5) * 20 - cx, y1 - (yy + 0.5) * 20 - cy
    scl = scl_fn(px, py).astype("uint8")
    d = tmp / sid
    d.mkdir()
    full10 = (2 * h20, 2 * w20)
    nodata20 = scl == 0
    nodata10 = np.repeat(np.repeat(nodata20, 2, 0), 2, 1)
    hrefs = {
        "red": _write(d / "B04.tif", np.where(nodata10, 0, np.full(full10, red_dn)).astype("uint16"), t10),
        "nir": _write(d / "B08.tif", np.where(nodata10, 0, np.full(full10, NIR)).astype("uint16"), t10),
        "nir08": _write(d / "B8A.tif", np.where(nodata20, 0, NIR08).astype("uint16"), t20),
        "swir16": _write(d / "B11.tif", np.where(nodata20, 0, SWIR16).astype("uint16"), t20),
        "scl": _write(d / "SCL.tif", scl, t20),
    }
    band = {"nodata": 0, "data_type": "uint16", "scale": 0.0001, "offset": -0.1}
    props = {"datetime": dt, "platform": "sentinel-2b", "s2:processing_baseline": "05.13", "eo:cloud_cover": 1.0}
    if boa_applied is not None:
        props["earthsearch:boa_offset_applied"] = boa_applied
    return {"type": "Feature", "id": sid, "properties": props,
            "assets": {k: {"href": h, "raster:bands": [dict(band)] if k != "scl" else [{"nodata": 0}]}
                       for k, h in hrefs.items()}}


@pytest.fixture
def scenes(tmp_path, s2_config):
    grid = load_aoi("tiny").grid
    src = tmp_path / "scenes"
    src.mkdir()
    # A: clear except a cloud over the north-west quadrant; B: other orbit, east half outside the swath.
    a = _scene(src, "S2B_A_20261108", "2026-11-08T18:45:03Z", grid,
               lambda x, y: np.where((x < 0) & (y > 0), 9, 4), RED_A)
    b = _scene(src, "S2A_B_20261108", "2026-11-08T18:55:10Z", grid,
               lambda x, y: np.where(x < 0, 4, 0), RED_B)
    c = _scene(src, "S2C_C_20261105", "2026-11-05T18:45:08Z", grid,
               lambda x, y: np.full(x.shape, 9), RED_A)       # fully cloudy date
    return [a, b, c]


def _run(lake, items, monkeypatch, **kw):
    monkeypatch.setattr(Sentinel2IndicesConnector, "_search", lambda self, bbox, interval: copy.deepcopy(items))
    return pipeline.run([DS], "tiny", Period(date(2026, 11, 1), date(2026, 11, 14)), lake=lake, **kw)


def test_discover_groups_dates_newest_first(lake, scenes, monkeypatch, s2_config):
    from fuellake.registry import load_dataset, make_connector

    monkeypatch.setattr(Sentinel2IndicesConnector, "_search", lambda self, bbox, interval: copy.deepcopy(scenes))
    conn = make_connector(load_dataset(DS), lake)
    refs = conn.discover(load_aoi("tiny"), Period(date(2026, 11, 1), date(2026, 11, 14)))
    assert [r.datetime.date() for r in refs] == [date(2026, 11, 8), date(2026, 11, 5)]
    assert len(refs[0].meta["scenes"]) == 2
    assert refs[0].urls[0].endswith("/collections/sentinel-2-c1-l2a/items/S2B_A_20261108")


def test_pipeline_offline_masking_compositing_idempotency(lake, scenes, monkeypatch, tmp_path):
    rep = _run(lake, scenes, monkeypatch)
    d = rep.datasets[0]
    assert d.status == "ok", [u.error for u in d.units]
    assert (d.count("created"), d.count("empty")) == (1, 1)

    items = list(catalog.iter_items(lake, DS))
    assert [i["id"] for i in items] == ["sentinel2-indices__tiny__20261108"]
    item = items[0]
    p = item["properties"]
    assert p["fuellake:aoi_masked_fraction"] == 0.0          # A's cloud is filled by B
    assert p["fuellake:s2_scenes"] == ["S2B_A_20261108", "S2A_B_20261108"]   # A has more clear cells
    assert p["fuellake:boa_offset"] == {"S2B_A_20261108": -0.1, "S2A_B_20261108": -0.1}
    assert p["start_datetime"] == "2026-11-08T18:45:03Z" and p["end_datetime"] == "2026-11-08T18:55:10Z"
    assert all(r.ok for r in validate_collection(lake, load_dataset_cfg()))

    ndvi, grid = read_cog(lake, catalog.asset_rel(item, "ndvi"))
    ndmi, _ = read_cog(lake, catalog.asset_rel(item, "ndmi"))
    assert grid.shape == load_aoi("tiny").grid.shape
    assert np.isfinite(ndvi).all() and np.isfinite(ndmi).all()
    h, w = ndvi.shape
    assert ndvi[h // 8, w // 8] == pytest.approx(NDVI_B, abs=1e-4)          # cloud hole of A, from B
    assert ndvi[-h // 8, -w // 8] == pytest.approx(NDVI_A, abs=1e-4)        # clear in A
    assert np.allclose(ndmi, NDMI, atol=1e-4)
    # every cell is exactly scene A or scene B: no cloud-contaminated or cross-scene mix
    near_a, near_b = np.isclose(ndvi, NDVI_A, atol=1e-4), np.isclose(ndvi, NDVI_B, atol=1e-4)
    assert (near_a | near_b).all() and near_a.any() and near_b.any()

    # cloudy date: decision cached; second run touches no pixels for it
    decisions = list((tmp_path / "lake" / "raw" / DS).rglob("*.decision.json"))
    assert len(decisions) == 1 and json.loads(decisions[0].read_text())["aoi_masked_fraction"] == 1.0
    shutil.rmtree(tmp_path / "scenes" / "S2C_C_20261105")
    rep2 = _run(lake, scenes, monkeypatch)
    d2 = rep2.datasets[0]
    assert d2.status == "ok", [u.error for u in d2.units]
    assert (d2.skipped, d2.count("created"), d2.count("updated"), d2.count("empty")) == (1, 0, 0, 1)
    assert d2.units[0].reason.startswith("cached")          # skipped from the cache before extract

    # --limit counts processed units: the cached cloudy date (newest first after --force) does not use it up
    newest_cloudy = [{**sc, "id": sc["id"].replace("20261105", "20261109"),
                      "properties": {**sc["properties"],
                                     "datetime": sc["properties"]["datetime"].replace("11-05", "11-09")}}
                     for sc in scenes if "20261105" in sc["id"]]
    from fuellake.registry import load_dataset, make_connector

    both = scenes + newest_cloudy
    monkeypatch.setattr(Sentinel2IndicesConnector, "_search", lambda self, bbox, i: copy.deepcopy(both))
    conn = make_connector(load_dataset(DS), lake)
    ref = next(r for r in conn.discover(load_aoi("tiny"), Period(date(2026, 11, 1), date(2026, 11, 14)))
               if r.datetime.date() == date(2026, 11, 9))
    assert not conn.cached_empty(ref, load_aoi("tiny"))
    o = conn._opts()
    lake.write_json(conn._decision_rel(ref), {"key": ref.key, "decision": "skip", "aoi_masked_fraction": 1.0,
                                              "max_masked_fraction": o["max_masked_fraction"],
                                              "scl_mask_classes": o["mask_classes"]})
    assert conn.cached_empty(ref, load_aoi("tiny"))
    rep3 = _run(lake, scenes + newest_cloudy, monkeypatch, force=True, limit=1)
    d3 = rep3.datasets[0]
    assert [(u.key.split(":")[1], u.status) for u in d3.units] == [("20261109", "empty"), ("20261108", "updated")]


def load_dataset_cfg():
    from fuellake.registry import load_dataset

    return load_dataset(DS)


def test_masked_threshold_and_partial_date(lake, scenes, monkeypatch):
    only_a = [scenes[0]]                    # 25 % of the AOI under cloud
    rep = _run(lake, only_a, monkeypatch)
    assert rep.datasets[0].count("created") == 1
    item = next(catalog.iter_items(lake, DS))
    frac = item["properties"]["fuellake:aoi_masked_fraction"]
    assert 0.2 < frac < 0.3
    ndvi, _ = read_cog(lake, catalog.asset_rel(item, "ndvi"))
    assert np.isnan(ndvi).mean() == pytest.approx(frac, abs=1e-6)


def test_threshold_option_skips_partial_date(lake, scenes, monkeypatch, s2_config):
    path = s2_config / "datasets" / f"{DS}.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["options"]["max_masked_fraction"] = 0.1
    path.write_text(yaml.safe_dump(cfg))
    rep = _run(lake, [scenes[0]], monkeypatch)
    assert (rep.datasets[0].count("created"), rep.datasets[0].count("empty")) == (0, 1)


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_earth_search_one_date_tiny_aoi(lake, s2_config):
    """Live: Earth Search discovery + windowed reads for one clear date over the tiny AOI."""
    period = Period(date(2026, 11, 11), date(2026, 11, 11))
    rep = pipeline.run([DS], "tiny", period, lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", d.error or [u.error for u in d.units]
    assert d.count("created") == 1
    item = next(catalog.iter_items(lake, DS))
    assert item["properties"]["fuellake:s2_collection"] == "sentinel-2-c1-l2a"
    offsets = item["properties"]["fuellake:boa_offset"]
    assert offsets and set(offsets.values()) == {-0.1}
    ndvi, _ = read_cog(lake, catalog.asset_rel(item, "ndvi"))
    valid = ndvi[np.isfinite(ndvi)]
    assert valid.size > 0.5 * ndvi.size
    assert -1 <= valid.min() and valid.max() <= 1
    assert 0.05 < float(np.median(valid)) < 0.9         # chaparral / grass hillsides, not water
    assert all(r.ok for r in validate_collection(lake, load_dataset_cfg()))
