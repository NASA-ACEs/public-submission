# fuel-lake — tests: OPERA RTC-S1 connector (burst grouping, dedupe, mosaic in linear power, dB, masks)
# Licensed under Apache-2.0.
"""Offline tests use saved CMR records (real) and synthetic burst GeoTIFFs (not real data): no RTC-S1 file
could be read without an Earthdata token."""

from __future__ import annotations

import copy
import json
import math
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine

from fuellake import catalog, pipeline
from fuellake.config import Settings
from fuellake.connectors import _earthdata as ed
from fuellake.connectors import opera_rtc_s1 as op
from fuellake.connectors.base import Period, SourceRef
from fuellake.contracts import validate_collection, validate_raster_a
from fuellake.rasters import read_cog
from fuellake.registry import load_aoi, load_dataset, make_connector
from fuellake.storage import Lake

FIX = Path(__file__).parent / "fixtures" / "earthdata"
REPO = Path(__file__).resolve().parents[1]


def fixture_items() -> list[dict]:
    return json.loads((FIX / "cmr_opera_rtc_s1_smm.json").read_text())["items"]


def connector(lake: Lake, **options):
    ds = load_dataset("opera-rtc-s1")
    ds.options.update(options)
    return make_connector(ds, lake, Settings.from_env())


# ---------------------------------------------------------------------------- names, grouping
def test_parse_opera_name():
    n = op.parse_opera_name("OPERA_L2_RTC-S1_T137-292315-IW3_20260602T015835Z_20260711T101639Z_S1A_30_v1.0")
    assert n["burst"] == "T137-292315-IW3" and n["track"] == 137 and n["sensor"] == "S1A"
    assert n["production"] == "20260711T101639Z" and n["posting"] == 30
    assert op.parse_opera_name("NISAR_L2_PR_GCOV_022_013_D_071") is None


def test_group_bursts_dedupes_reprocessed():
    groups = op.group_bursts(ed.parse_granules(fixture_items()))
    assert set(groups) == {("20260602", "S1A", 144, "descending"), ("20260603", "S1C", 64, "ascending")}
    g144 = groups[("20260602", "S1A", 144, "descending")]
    assert sorted(n["burst"] for _, n in g144) == ["T144-308025-IW1", "T144-308026-IW1", "T144-308027-IW1"]
    assert {n["production"] for _, n in g144} == {"20260711T105314Z"}        # newest of two productions
    assert len(groups[("20260603", "S1C", 64, "ascending")]) == 3


def test_missing_credentials_per_unit(lake, monkeypatch):
    monkeypatch.setattr(ed, "search_granules", lambda *a, **k: copy.deepcopy(fixture_items()))
    conn, aoi = connector(lake, min_aoi_coverage=0.0), load_aoi("smm")
    ref = conn.discover(aoi, Period(date(2026, 6, 1), date(2026, 6, 3)))[0]
    assert conn.missing_credentials(aoi) == [] and conn.missing_credentials(aoi, ref) == ["EARTHDATA_TOKEN"]
    for _url, rel in conn._raw_files(ref):
        lake.write_bytes(rel, b"cached burst file")
    assert conn.missing_credentials(aoi, ref) == []                    # every burst file already in raw/


def test_discover_refs(lake, monkeypatch):
    seen = []
    monkeypatch.setattr(ed, "search_granules", lambda s, cid, **k: seen.append(cid) or copy.deepcopy(fixture_items()))
    refs = connector(lake, min_aoi_coverage=0.0).discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 6, 3)))
    assert seen == ["C2777436413-ASF"] and len(refs) == 2
    r = refs[0]
    assert r.key.startswith("S1A_20260602_T144D_") and r.meta["platform"] == "sentinel-1a"
    assert len(r.meta["bursts"]) == 3 and len(r.urls) == 9
    assert all(set(b["files"]) == {"VV", "VH", "mask"} for b in r.meta["bursts"])
    again = connector(lake, min_aoi_coverage=0.0).discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 6, 3)))
    assert [x.key for x in again] == [x.key for x in refs]                   # stable keys


# ---------------------------------------------------------------------------- synthetic bursts
def write_burst(dirpath: Path, name: str, grid, cols: slice, value: float, *, layover_cols: slice | None = None):
    """Write <name>_VV.tif, _VH.tif, _mask.tif on a 30 m UTM grid aligned with ``grid`` (wider margin)."""
    dirpath.mkdir(parents=True, exist_ok=True)
    margin = 10
    h, w = grid.height + 2 * margin, grid.width + 2 * margin
    t = grid.transform
    tr = Affine(30.0, 0.0, t.c - margin * 30.0, 0.0, -30.0, t.f + margin * 30.0)
    vv = np.full((h, w), np.nan, "float32")
    mask = np.full((h, w), 255, "uint8")
    c = slice(cols.start + margin, cols.stop + margin)
    vv[:, c] = value
    mask[:, c] = 0
    if layover_cols is not None:
        mask[:, slice(layover_cols.start + margin, layover_cols.stop + margin)] = 2
    prof = dict(driver="GTiff", width=w, height=h, count=1, crs="EPSG:32611", transform=tr)
    paths = {}
    for layer, arr, dtype, nodata in (("VV", vv, "float32", np.nan), ("VH", vv * np.float32(0.1), "float32", np.nan),
                                      ("mask", mask, "uint8", 255)):
        p = dirpath / f"{name}_{layer}.tif"
        with rasterio.open(p, "w", dtype=dtype, nodata=nodata, **prof) as ds:
            ds.write(arr.astype(dtype), 1)
        paths[layer] = p
    return paths


def test_transform_mosaic_linear_mean_and_masks(test_config, tmp_path):
    shutil.copy(REPO / "config" / "datasets" / "opera-rtc-s1.yaml", test_config / "datasets" / "opera-rtc-s1.yaml")
    aoi = load_aoi("tiny")
    grid = aoi.grid
    lake = Lake.from_env()
    con = connector(lake)
    w = grid.width
    a = write_burst(tmp_path / "A", "BURST_A", grid, slice(0, 2 * w // 3), 0.1, layover_cols=slice(0, 5))
    b = write_burst(tmp_path / "B", "BURST_B", grid, slice(w // 3, w), 0.2)
    bursts = [{"ur": n, "burst_id": n, "files": {k: str(v) for k, v in p.items()}} for n, p in (("BURST_A", a),
                                                                                               ("BURST_B", b))]
    ref = SourceRef("opera-rtc-s1", "S1C_20260603_T064A_x", datetime(2026, 6, 3, 1, 49, tzinfo=UTC),
                    urls=tuple(u for bb in bursts for u in bb["files"].values()),
                    meta={"sensor": "S1C", "platform": "sentinel-1c", "track": 64, "orbit_state": "ascending",
                          "bursts": bursts, "aoi_coverage": 1.0})
    raw = con.extract(ref, aoi)                              # local copies through the download helper
    assert len(raw) == 6 and all(lake.exists(r) for r in raw)
    items = con.transform(ref, raw, aoi)
    it = items[0]
    assert it.item_id == "opera-rtc-s1__tiny__20260603__s1c_t064a"
    vv, _ = read_cog(lake, con.cog_path(aoi, it.item_id, "gamma0_vv_db"))
    vh, _ = read_cog(lake, con.cog_path(aoi, it.item_id, "gamma0_vh_db"))
    mid = slice(w // 3 + 2, 2 * w // 3 - 2)
    np.testing.assert_allclose(vv[5:-5, mid], 10 * math.log10(0.15), atol=1e-4)      # linear mean of 0.1, 0.2
    np.testing.assert_allclose(vv[5:-5, 6: w // 3 - 2], -10.0, atol=1e-4)             # burst A only
    np.testing.assert_allclose(vv[5:-5, 2 * w // 3 + 2:], 10 * math.log10(0.2), atol=1e-4)
    assert np.isnan(vv[:, :4]).all()                                                   # layover masked
    np.testing.assert_allclose(vh[5:-5, mid], 10 * math.log10(0.015), atol=1e-4)
    p = it.properties
    assert p["sar:frequency_band"] == "C" and p["sar:polarizations"] == ["VV", "VH"]
    assert p["sat:orbit_state"] == "ascending" and p["sat:relative_orbit"] == 64
    assert p["opera:layover_shadow_fraction"] > 0 and p["fuellake:source_ids"] == ["BURST_A", "BURST_B"]
    for asset in it.assets:
        assert validate_raster_a(lake, asset.path, grid, con.variable(asset.key)).ok
    # layover/shadow kept when the option is off
    keep = connector(lake, mask_layover_shadow=False).transform(ref, raw, aoi)[0]
    vv2, _ = read_cog(lake, keep.assets[0].path)
    assert np.isfinite(vv2[5:-5, :4]).all()


def test_transform_refuses_db_input(test_config, tmp_path):
    shutil.copy(REPO / "config" / "datasets" / "opera-rtc-s1.yaml", test_config / "datasets" / "opera-rtc-s1.yaml")
    aoi = load_aoi("tiny")
    lake = Lake.from_env()
    con = connector(lake)
    a = write_burst(tmp_path / "A", "BURST_A", aoi.grid, slice(0, aoi.grid.width), -12.0)   # already dB
    bursts = [{"ur": "BURST_A", "burst_id": "BURST_A", "files": {k: str(v) for k, v in a.items()}}]
    ref = SourceRef("opera-rtc-s1", "k", datetime(2026, 6, 3, tzinfo=UTC), meta={
        "sensor": "S1C", "platform": "sentinel-1c", "track": 64, "orbit_state": "ascending", "bursts": bursts})
    raw = con.extract(ref, aoi)
    with pytest.raises(ValueError, match="linear power"):
        con.transform(ref, raw, aoi)


def test_pipeline_end_to_end_is_idempotent(test_config, tmp_path, monkeypatch):
    shutil.copy(REPO / "config" / "datasets" / "opera-rtc-s1.yaml", test_config / "datasets" / "opera-rtc-s1.yaml")
    aoi = load_aoi("tiny")
    w = aoi.grid.width
    a = write_burst(tmp_path / "A", "A", aoi.grid, slice(0, 2 * w // 3), 0.1)
    b = write_burst(tmp_path / "B", "B", aoi.grid, slice(w // 3, w), 0.2)
    monkeypatch.setattr(ed, "search_granules", lambda *a_, **k: copy.deepcopy(fixture_items()))
    n_downloads = []

    def fake_download(url, dest, settings, **kw):
        assert url.startswith("https://cumulus.asf.earthdatacloud.nasa.gov/OPERA/")
        layer = url.rsplit("_", 1)[-1].split(".")[0]
        src = (a if "-308025-" in url or "-135519-" in url else b)[layer]
        shutil.copyfile(src, dest)
        n_downloads.append(url)
        return src.stat().st_size

    monkeypatch.setattr(ed, "download", fake_download)
    ds_yaml = test_config / "datasets" / "opera-rtc-s1.yaml"
    ds_yaml.write_text(ds_yaml.read_text().replace("min_aoi_coverage: 0.5", "min_aoi_coverage: 0.0"))
    lake = Lake.from_env()
    period = Period(date(2026, 6, 1), date(2026, 6, 3))
    rep = pipeline.run(["opera-rtc-s1"], "tiny", period, lake=lake)
    d = rep.datasets[0]
    assert rep.ok and d.count("created") == 2, d.units
    assert len(n_downloads) == 18                             # 2 passes x 3 bursts x (VV, VH, mask)
    assert all(r.ok for r in validate_collection(lake, load_dataset("opera-rtc-s1")))
    assert {i["properties"]["platform"] for i in catalog.iter_items(lake, "opera-rtc-s1")} == {"sentinel-1a",
                                                                                                 "sentinel-1c"}
    rep2 = pipeline.run(["opera-rtc-s1"], "tiny", period, lake=lake)
    assert rep2.datasets[0].skipped == 2 and len(n_downloads) == 18


@pytest.mark.network
def test_live_discover_smm(lake):
    refs = connector(lake).discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 11, 14)))
    assert len(refs) > 50
    assert {r.meta["platform"] for r in refs} >= {"sentinel-1c", "sentinel-1d"}
    assert not [r for r in refs if r.meta["platform"] == "sentinel-1a" and r.datetime.date() > date(2026, 6, 30)]
    keys = [r.key for r in refs]
    assert len(keys) == len(set(keys))
