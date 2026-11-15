# fuel-lake — tests: NISAR GCOV connector (discovery rules, AOI-window streaming, linear->dB averaging)
# Licensed under Apache-2.0.
"""Offline tests use the saved CMR records in ``fixtures/earthdata`` and a synthetic GCOV HDF5 file that
follows the product specification layout (JPL D-102274 Rev E, section 5). They are not real data. The
``network`` test streams the public NISAR sample product (pre-launch, CRID D00404, ALOS PALSAR input); the
``earthdata`` test needs ``EARTHDATA_TOKEN`` and reads a real PROVISIONAL (P05023) granule."""

from __future__ import annotations

import contextlib
import copy
import json
import math
import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import h5py
import numpy as np
import pytest

from fuellake import catalog, pipeline
from fuellake.config import Settings
from fuellake.connectors import _earthdata as ed
from fuellake.connectors import nisar_gcov as gc
from fuellake.connectors.base import Period, SourceRef
from fuellake.contracts import validate_collection, validate_item, validate_raster_a
from fuellake.rasters import read_cog
from fuellake.registry import AOI, load_aoi, load_dataset, make_connector
from fuellake.storage import Lake

FIX = Path(__file__).parent / "fixtures" / "earthdata"
REPO = Path(__file__).resolve().parents[1]
SAMPLE_URL = ("https://nisar.asf.earthdatacloud.nasa.gov/NISAR-SAMPLE-DATA/GCOV/"
              "NISAR_L2_PR_GCOV_001_030_A_019_2800_SHNA_A_20081012T060911_20081012T060925_D00404_N_F_J_001/"
              "NISAR_L2_PR_GCOV_001_030_A_019_2800_SHNA_A_20081012T060911_20081012T060925_D00404_N_F_J_001.h5")
STRIPES = (0.02, 0.05, 0.11)          # linear gamma0 by absolute 10 m column index mod 3


def fixture_items() -> list[dict]:
    return json.loads((FIX / "cmr_gcov_provisional_smm.json").read_text())["items"]


# ---------------------------------------------------------------------------- synthetic product
def make_gcov_h5(path: Path, grid, *, posting: float = 10.0, pad: float = 300.0, pols=("HH", "HV"),
                 zero_block=None, mask_value: int = 1) -> None:
    """Synthetic GCOV file with the documented group layout over ``grid`` (EPSG:32611)."""
    minx, miny, maxx, maxy = grid.bounds()
    x0 = math.floor((minx - pad) / posting) * posting
    x1 = math.ceil((maxx + pad) / posting) * posting
    y0 = math.floor((miny - pad) / posting) * posting
    y1 = math.ceil((maxy + pad) / posting) * posting
    xc = np.arange(x0, x1, posting) + posting / 2
    yc = np.arange(y1, y0, -posting) - posting / 2
    col_abs = np.floor((xc - posting / 2) / posting).astype(int) % 3
    hh = np.broadcast_to(np.array(STRIPES, dtype="float32")[col_abs], (yc.size, xc.size)).copy()
    mask = np.full(hh.shape, mask_value, dtype="uint8")
    mask[:3, :] = 255                                       # outside the RSLC image
    hh[:3, :] = np.nan
    if zero_block is not None:                              # (xmin, ymin, xmax, ymax) marked invalid
        bx0, by0, bx1, by1 = zero_block
        cols = (xc > bx0) & (xc < bx1)
        rows = (yc > by0) & (yc < by1)
        mask[np.ix_(rows, cols)] = 0
    with h5py.File(path, "w") as f:
        g = f.create_group("/science/LSAR/GCOV/grids/frequencyA")
        g["listOfPolarizations"] = np.array([p.encode() for p in pols])
        g["listOfCovarianceTerms"] = np.array([(p + p).encode() for p in pols])
        for p in pols:
            arr = hh if p == "HH" else hh * np.float32(0.1)
            ds = g.create_dataset(p + p, data=arr.astype("float32"), chunks=(64, 64), compression="gzip")
            ds.attrs["_FillValue"] = np.float32(np.nan)
            ds.attrs["valid_min"] = 0.0
        g.create_dataset("mask", data=mask, chunks=(64, 64), compression="gzip").attrs["_FillValue"] = np.uint8(255)
        g["xCoordinates"] = xc
        g["yCoordinates"] = yc
        g["xCoordinateSpacing"] = posting
        g["yCoordinateSpacing"] = -posting
        pr = g.create_dataset("projection", data=np.uint32(32611))
        pr.attrs["epsg_code"] = 32611
        pr.attrs["utm_zone_number"] = 11
        i = f.create_group("/science/LSAR/identification")
        i["trackNumber"] = np.uint32(13)
        i["frameNumber"] = np.uint16(71)
        i["orbitPassDirection"] = np.bytes_(b"Descending")
        i["compositeReleaseId"] = np.bytes_(b"P05023")
        i["granuleId"] = np.bytes_(b"SYNTHETIC")


def tiny_aoi() -> AOI:
    return AOI(id="tiny", name="tiny", bbox=(-118.80, 34.05, -118.78, 34.07), crs="EPSG:32611", resolution=30,
               nisar={"track": 13, "frame": 71, "direction": "descending"})


def synthetic_ref(url: str) -> SourceRef:
    return SourceRef("nisar-gcov", "NISAR_L2_PR_GCOV_023_013_D_071_SYNTH", datetime(2026, 6, 15, 2, 40, tzinfo=UTC),
                     urls=(url,), meta=dict(granule_ur="NISAR_L2_PR_GCOV_023_013_D_071_SYNTH", maturity="PROVISIONAL",
                                            crid="P05023", track=13, frame=71, orbit_state="descending", mode="4005",
                                            pole="DHDH", polarizations=["HH", "HV"], aoi_coverage=1.0))


def connector(lake: Lake, **options):
    ds = load_dataset("nisar-gcov")
    ds.options.update(options)
    return make_connector(ds, lake, Settings.from_env())


# ---------------------------------------------------------------------------- discovery
@pytest.fixture
def fake_cmr(monkeypatch):
    calls = []

    def fake_search(session, concept_id, **kw):
        calls.append({"concept_id": concept_id, **kw, "auth": session.headers.get("Authorization")})
        return copy.deepcopy(fixture_items())

    monkeypatch.setattr(ed, "search_granules", fake_search)
    return calls


def test_discover_preferred_stack_and_properties(lake, fake_cmr):
    refs = connector(lake).discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 11, 14)))
    assert [r.datetime.date().isoformat() for r in refs] == ["2026-06-15", "2026-10-13"]
    assert fake_cmr[0]["concept_id"] == "C2854338529-ASF" and fake_cmr[0]["auth"] is None
    m = refs[0].meta
    assert (m["track"], m["frame"], m["orbit_state"], m["crid"], m["maturity"]) == (13, 71, "descending", "P05023",
                                                                                     "PROVISIONAL")
    assert m["polarizations"] == ["HH", "HV"] and m["aoi_coverage"] > 0.999 and m["size_bytes"] > 4e9
    assert refs[0].key == m["granule_ur"] and refs[0].urls[0].endswith(m["granule_ur"] + ".h5")
    props = ed.nisar_item_properties(refs[0], "GCOV", ["HH", "HV"])
    assert props["sat:orbit_state"] == "descending" and props["sar:frequency_band"] == "L"
    assert props["fuellake:processing_id"] == "P05023" and props["sat:relative_orbit"] == 13
    assert props["fuellake:frame"] == 71 and props["fuellake:maturity"] == "PROVISIONAL"
    assert {"sar:instrument_mode", "sar:product_type", "sar:polarizations"} <= set(props)
    assert ed.item_discriminator(refs[0]) == "t013d071"


def test_discover_all_stacks_dedupes_and_drops_urgent_response(lake, fake_cmr, monkeypatch):
    items = fixture_items()
    ur = copy.deepcopy(next(i for i in items if "013_D_071_4005_DHDH_A_20260615" in i["umm"]["GranuleUR"]))
    ur["umm"]["GranuleUR"] = ur["umm"]["GranuleUR"].replace("_L2_PR_", "_L2_UR_")   # synthetic UR copy
    monkeypatch.setattr(ed, "search_granules", lambda *a, **k: copy.deepcopy(items) + [ur])
    refs = connector(lake, stack="all").discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 11, 14)))
    stacks = sorted((r.meta["stack_id"], r.meta["counter"]) for r in refs)
    # 013_D_072 covers ~1% of the bbox (< min_aoi_coverage); 114_D_071 _001/_002 -> _002; UR copy dropped
    assert stacks == [("013_D_071", 1), ("013_D_071", 1), ("034_A_019", 1), ("114_D_071", 2)]
    assert all("_PR_" in r.key for r in refs)


def test_discover_one_collection_per_maturity(lake, fake_cmr):
    connector(lake, maturity="VALIDATED").discover(load_aoi("smm"), Period(date(2026, 10, 21), date(2026, 11, 14)))
    assert fake_cmr[-1]["concept_id"] == "C3622241360-ASF"
    with pytest.raises(ValueError, match="no collection configured"):
        connector(lake, maturity="NOPE").discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 6, 2)))


def test_validated_query_filters_provisional_crid(lake, fake_cmr):
    refs = connector(lake, maturity="VALIDATED").discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 11, 14)))
    assert refs == []          # every fixture granule is P05023, VALIDATED expects P05036: never mixed in


# ---------------------------------------------------------------------------- extract + transform
def test_extract_window_and_transform_linear_average(lake, tmp_path):
    aoi = tiny_aoi()
    grid = aoi.grid
    src = tmp_path / "synthetic_gcov.h5"
    bx = (grid.bounds()[0] + 300, grid.bounds()[1] + 300, grid.bounds()[0] + 390, grid.bounds()[1] + 390)
    make_gcov_h5(src, grid, zero_block=bx)
    con = connector(lake)
    ref = synthetic_ref(str(src))
    raw = con.extract(ref, aoi)
    assert raw == [con.raw_rel(ref, aoi)] and lake.exists(raw[0])
    with lake.local_path(raw[0]) as p, h5py.File(p) as h:
        shape = h["HHHH"].shape
    with h5py.File(src) as h:
        full = h["/science/LSAR/GCOV/grids/frequencyA/HHHH"].shape
    assert shape[0] < full[0] and shape[1] < full[1]            # only the AOI window (+ pad) was read
    items = con.transform(ref, raw, aoi)
    assert len(items) == 1
    it = items[0]
    assert it.item_id == "nisar-gcov__tiny__20260615__t013d071"
    assert [a.key for a in it.assets] == ["gamma0_hh_db", "gamma0_hv_db"]
    hh, g = read_cog(lake, it.assets[0].path)
    hv, _ = read_cog(lake, it.assets[1].path)
    assert g.same_as(grid.crs, grid.transform, grid.width, grid.height)
    lin_mean = float(np.mean(STRIPES))
    db_mean = float(np.mean(10 * np.log10(STRIPES)))
    interior = hh[10:20, 30:40]
    np.testing.assert_allclose(interior, 10 * math.log10(lin_mean), atol=1e-3)   # averaged in linear power
    assert abs(float(np.nanmean(interior)) - db_mean) > 0.5                    # not averaged in dB
    np.testing.assert_allclose(hv[10:20, 30:40], 10 * math.log10(lin_mean * 0.1), atol=1e-3)
    # the mask==0 block (90 m x 90 m = 3x3 AOI pixels) is nodata
    r0 = grid.height - 1 - int((bx[3] - grid.bounds()[1]) // 30) + 1
    c0 = int((bx[0] - grid.bounds()[0]) // 30)
    assert np.isnan(hh[r0:r0 + 3, c0:c0 + 3]).all()
    assert np.isfinite(hh).mean() > 0.95
    p = it.properties
    assert p["fuellake:qc_masked_fraction"] == 0.0 and p["sar:polarizations"] == ["HH", "HV"]
    assert p["nisar:native_posting_m"] == 10.0 and p["fuellake:processing_id"] == "P05023"
    for a in it.assets:
        assert validate_raster_a(lake, a.path, grid, con.variable(a.key)).ok
    # cached raw subset is reused, not re-read from the source
    src.unlink()
    assert con.extract(ref, aoi) == raw


def test_mask_policy_and_reject_all(lake, tmp_path):
    aoi = tiny_aoi()
    src = tmp_path / "all_zero_mask.h5"
    make_gcov_h5(src, aoi.grid, pols=("HH",), mask_value=0)      # like the D00404 sample: mask only 0/255
    con = connector(lake)
    ref = synthetic_ref(str(src))
    raw = con.extract(ref, aoi)
    with pytest.raises(gc.MaskRejectsAllError, match="rejects all"):
        con.transform(ref, raw, aoi)
    items = connector(lake, mask_policy="fill_only").transform(ref, raw, aoi)
    assert [a.key for a in items[0].assets] == ["gamma0_hh_db"]
    props = items[0].properties
    assert props["sar:polarizations"] == ["HH"] and props["nisar:mask_policy"] == "fill_only"
    with pytest.raises(ValueError, match="mask_policy"):
        gc.mask_invalid(np.zeros(3), "bogus")


def test_extract_needs_token_for_real_urls(lake):
    ref = synthetic_ref("https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/x/x.h5")
    with pytest.raises(ed.EarthdataAuthError, match="EARTHDATA_TOKEN is not set"):
        connector(lake).extract(ref, tiny_aoi())


# ---------------------------------------------------------------------------- pipeline end to end
def test_pipeline_end_to_end_is_idempotent(test_config, tmp_path, monkeypatch):
    shutil.copy(REPO / "config" / "datasets" / "nisar-gcov.yaml", test_config / "datasets" / "nisar-gcov.yaml")
    aoi = load_aoi("tiny")
    src = tmp_path / "synthetic_gcov.h5"
    make_gcov_h5(src, aoi.grid)
    monkeypatch.setattr(ed, "search_granules", lambda *a, **k: copy.deepcopy(fixture_items()))
    real_open = ed.open_h5

    @contextlib.contextmanager
    def fake_open(url, settings, **kw):
        assert url.startswith("https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/")
        with real_open(str(src), settings) as f:
            yield f

    monkeypatch.setattr(ed, "open_h5", fake_open)
    lake = Lake.from_env()
    period = Period(date(2026, 6, 1), date(2026, 11, 14))
    rep = pipeline.run(["nisar-gcov"], "tiny", period, lake=lake)
    d = rep.datasets[0]
    assert rep.ok, d.units
    assert d.discovered == 2 and d.count("created") == 2
    items = list(catalog.iter_items(lake, "nisar-gcov"))
    assert {i["properties"]["fuellake:maturity"] for i in items} == {"PROVISIONAL"}
    assert "sar" in " ".join(items[0]["stac_extensions"]) and items[0]["properties"]["fuellake:status"] == "search-only"
    results = validate_collection(lake, load_dataset("nisar-gcov"))
    assert all(r.ok for r in results), [r.errors for r in results if not r.ok]
    rep2 = pipeline.run(["nisar-gcov"], "tiny", period, lake=lake)
    assert rep2.ok and rep2.datasets[0].skipped == 2 and not rep2.datasets[0].units


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_discover_smm_stack_013_d_071(lake):
    refs = connector(lake).discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 11, 14)))
    days = [r.datetime.date() for r in refs]
    assert len(refs) >= 8, days                                 # notes: 8-11 PROVISIONAL dates per stack
    assert all(r.meta["stack_id"] == "013_D_071" and r.meta["crid"] == "P05023" for r in refs)
    assert not [d for d in days if date(2026, 7, 28) <= d <= date(2026, 8, 9)]   # instrument gap
    assert date(2026, 6, 15) in days and date(2026, 10, 13) in days


@pytest.mark.network
@pytest.mark.slow
def test_public_sample_product_streamed(lake):
    """Real NISAR-format file (public sample, pre-launch D00404, HH only, 20 m), streamed by range reads."""
    aoi = AOI(id="gcovsample", name="inside the GCOV sample footprint", bbox=(-118.20, 34.40, -118.12, 34.46),
              crs="EPSG:32611", resolution=30)
    con = connector(lake)
    ur = SAMPLE_URL.rsplit("/", 1)[-1][:-3]
    ref = SourceRef("nisar-gcov", ur, datetime(2008, 10, 12, 6, 9, 11, tzinfo=UTC), urls=(SAMPLE_URL,),
                    meta=dict(granule_ur=ur, maturity="BETA", crid="D00404", track=30, frame=19,
                              orbit_state="ascending", mode="2800", pole="SHNA", polarizations=["HH"]))
    raw = con.extract(ref, aoi)
    with lake.local_path(raw[0]) as p, h5py.File(p) as h:
        ident = json.loads(h.attrs["identification"])
        assert ident["compositeReleaseId"] == "D00404" and ident["missionId"] == "ALOS"
        assert json.loads(h.attrs["polarizations"]) == ["HH"] and float(h.attrs["dx"]) == 20.0
        assert set(np.unique(h["mask"][()]).tolist()) <= {0, 255}
    with pytest.raises(gc.MaskRejectsAllError):
        con.transform(ref, raw, aoi)                            # spec rule: no sub-swath numbers in the sample
    con2 = connector(lake, mask_policy="fill_only")
    items = con2.transform(ref, raw, aoi)
    assert [a.key for a in items[0].assets] == ["gamma0_hh_db"]
    db, _ = read_cog(lake, items[0].assets[0].path)
    med = float(np.nanmedian(db))
    assert -25.0 < med < 0.0 and np.isfinite(db).mean() > 0.9   # plausible L-band HH gamma0 over land
    item = pipeline.build_stac_item(lake, con2.dataset, aoi, ref, items[0], "test")
    assert all(r.ok for r in validate_item(lake, item, con2.dataset, aoi))


@pytest.mark.earthdata
@pytest.mark.skipif(not os.environ.get("EARTHDATA_TOKEN"), reason="needs EARTHDATA_TOKEN")
def test_real_provisional_granule_window(lake):
    """Real P05023 GCOV: discover on the tiny AOI inside smm, stream one window, stage HH/HV."""
    aoi = tiny_aoi()
    con = connector(lake)
    refs = con.discover(aoi, Period(date(2026, 10, 13), date(2026, 10, 13)))
    assert refs, "expected the 2026-10-13 granule on 013/D/071"
    raw = con.extract(refs[0], aoi)
    items = con.transform(refs[0], raw, aoi)
    assert {a.key for a in items[0].assets} == {"gamma0_hh_db", "gamma0_hv_db"}
    item = pipeline.build_stac_item(lake, con.dataset, aoi, refs[0], items[0], "test")
    assert all(r.ok for r in validate_item(lake, item, con.dataset, aoi))
