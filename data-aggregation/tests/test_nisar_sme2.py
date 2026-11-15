# fuel-lake — tests: NISAR SME2 connector (EASE-Grid 2.0, verified flag bits, algorithm spread, QC, pipeline)
# Licensed under Apache-2.0.
"""Offline tests use saved CMR records, a 20 x 20 cell window of a real P05023 granule
(``fixtures/earthdata/sme2_p05023_sgm_window.json``) and a synthetic SME2 HDF5 file laid out like the real
P05023 files (product specification version 1.0.2). The ``network`` tests read the public NISAR SME2 sample
product (pre-launch simulation, CRID D00404); the ``earthdata`` tests read real PROVISIONAL files."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import h5py
import numpy as np
import pytest
from pyproj import Transformer
from rasterio.warp import transform_bounds

from fuellake import pipeline
from fuellake.config import Settings
from fuellake.connectors import _earthdata as ed
from fuellake.connectors import nisar_sme2 as sm
from fuellake.connectors.base import Period, SourceRef
from fuellake.contracts import validate_collection, validate_item, validate_raster_a
from fuellake.rasters import read_cog
from fuellake.registry import AOI, load_aoi, load_dataset, make_connector
from fuellake.storage import Lake

FIX = Path(__file__).parent / "fixtures" / "earthdata"
REPO = Path(__file__).resolve().parents[1]
SAMPLE_URL = ("https://nisar.asf.earthdatacloud.nasa.gov/NISAR-SAMPLE-DATA/SME2/"
              "NISAR_L3_PR_SME2_001_008_D_070_4000_QPNA_A_20190829T180759_20190829T180809_D00404_M_P_J_001/"
              "NISAR_L3_PR_SME2_001_008_D_070_4000_QPNA_A_20190829T180759_20190829T180809_D00404_M_P_J_001.h5")
# Observed in that public sample file (grids/xCoordinates[0], yCoordinates[0], EASEGrid{Column,Row}Index[0],
# xCoordinateSpacing): independent of this module's constants.
SAMPLE_COL0, SAMPLE_ROW0 = 43020, 14895
SAMPLE_X0, SAMPLE_Y0 = -8755729.607989222, 4332774.464914683
SAMPLE_DX = 200.17900466918945
ALG_VALUES = {"DSG": 0.25, "PMI": 0.17, "TSR": 0.07}   # like the San Gabriel medians on 2026-09-01


# ---------------------------------------------------------------------------- EASE-Grid 2.0
def test_ease2_grid_constants():
    assert sm.EASE2_200M_CELL == pytest.approx(200.17900467, abs=1e-8)
    assert sm.EASE2_200M_CELL == pytest.approx(SAMPLE_DX, abs=1e-6)
    assert 5 * sm.EASE2_200M_CELL == pytest.approx(1000.90, abs=0.01)            # NSIDC 1 km cell (rounded)
    assert 180 * sm.EASE2_200M_CELL == pytest.approx(36032.22, abs=0.01)         # NSIDC 36 km cell (rounded)
    assert sm.EASE2_200M_ROWS * sm.EASE2_200M_CELL / 2 == pytest.approx(sm.EASE2_UL_Y, abs=1e-3)
    assert sm.EASE2_UL_X == pytest.approx(-17367530.44, abs=0.01)        # NSIDC upper-left corner (rounded)
    assert sm.EASE2_UL_Y == pytest.approx(7314540.83, abs=0.01)


def test_ease2_centre_matches_real_sample_values():
    # The sample's centres imply a corner 3.3 cm west of NSIDC's (-17367530.478 vs -17367530.445): harmless
    # (0.02 % of a cell). The connector georeferences from the file's own coordinates anyway.
    x, y = sm.ease2_centre(SAMPLE_COL0, SAMPLE_ROW0)
    assert float(x) == pytest.approx(SAMPLE_X0, abs=0.05) and float(y) == pytest.approx(SAMPLE_Y0, abs=0.05)
    col, row = sm.ease2_index(SAMPLE_X0, SAMPLE_Y0)
    assert (int(col), int(row)) == (SAMPLE_COL0, SAMPLE_ROW0)


def test_ease2_origin_is_lat_lon_consistent():
    # global grid corner -> (180 W, ~85.04 N) in EPSG:6933
    lon, lat = Transformer.from_crs("EPSG:6933", "EPSG:4326", always_xy=True).transform(sm.EASE2_UL_X, sm.EASE2_UL_Y)
    assert lon == pytest.approx(-180.0, abs=1e-6) and 84.9 < lat < 85.1


# ---------------------------------------------------------------------------- flag bits (verified layout)
def test_decode_quality_flag_bits():
    # 15: not recommended, all three averaged; 14: recommended, all three; 12: recommended, DSG + TSR;
    # 8: recommended, DSG only; 9: not recommended, DSG only; -9999: fill
    dec = sm.decode_quality_flag(np.array([15, 14, 12, 8, 9, -9999], dtype="int16"))
    assert dec["valid"].tolist() == [True, True, True, True, True, False]
    assert dec["recommended"].tolist() == [False, True, True, True, False, False]
    assert dec["included_DSG"].tolist() == [True, True, True, True, True, False]
    assert dec["included_TSR"].tolist() == [True, True, True, False, False, False]
    assert dec["included_PMI"].tolist() == [True, True, False, False, False, False]
    assert dec["recommended_PMI"].tolist() == [False, True, False, False, False, False]
    assert dec["recommended_DSG"].tolist() == [False, True, True, True, False, False]
    assert sm.FLAG_NOT_RECOMMENDED == 1 and sm.FLAG_INCLUDED == {"PMI": 2, "TSR": 4, "DSG": 8}


def _real_window() -> dict[str, np.ndarray]:
    raw = json.loads((FIX / "sme2_p05023_sgm_window.json").read_text())
    return {k: np.array(v) for k, v in raw.items() if isinstance(v, list)}


def test_flag_layout_on_real_p05023_cells():
    """The verified meaning on real cells: bit 0 clear exactly where some algorithm's own flag bit 0 is clear,
    bit k set exactly where that algorithm entered the average, and the top-level soilMoisture is that average."""
    w = _real_window()
    top = w["retrievalQualityFlag"]
    dec = sm.decode_quality_flag(top)
    ok = dec["valid"]
    assert ok.sum() > 300 and len(np.unique(top[ok])) >= 6
    vals = {a: np.where(w[f"{a}/soilMoisture"] == -9999, np.nan, w[f"{a}/soilMoisture"]) for a in sm.ALGORITHMS}
    own_rec = {a: np.isfinite(vals[a]) & ((w[f"{a}/retrievalQualityFlag"] & 1) == 0) for a in sm.ALGORITHMS}
    any_rec = np.any(list(own_rec.values()), axis=0)
    np.testing.assert_array_equal(dec["recommended"][ok], any_rec[ok])
    for a in sm.ALGORITHMS:
        included = np.where(any_rec, own_rec[a], np.isfinite(vals[a]))
        np.testing.assert_array_equal(dec[f"included_{a}"][ok], included[ok])
    stack = np.stack([np.where(dec[f"included_{a}"], vals[a], np.nan) for a in sm.ALGORITHMS])
    mean_included = np.nanmean(stack[:, ok], axis=0)
    np.testing.assert_allclose(w["soilMoisture"][ok], mean_included, atol=2e-5)
    # bit 3 is "DSG used", set on nearly every valid cell, so it cannot mean "outside 0.02-0.60"
    assert dec["included_DSG"][ok].mean() > 0.99
    dsg = vals["DSG"][ok]
    assert np.all((dsg >= np.float32(0.02)) & (dsg <= np.float32(0.60)))


def test_dense_vegetation_is_never_recommended_on_real_cells():
    w = _real_window()
    dec = sm.decode_quality_flag(w["retrievalQualityFlag"])
    dense = dec["valid"] & ((w["surfaceQualityFlag"] & sm.SURFACE_DENSE_VEGETATION) != 0)
    assert dense.sum() > 20 and not dec["recommended"][dense].any()


# ---------------------------------------------------------------------------- spread and range checks
def test_algorithm_spread_max_minus_min_with_two_or_more():
    vals = {"DSG": np.array([0.25, 0.30, np.nan, np.nan]), "PMI": np.array([0.17, np.nan, 0.2, np.nan]),
            "TSR": np.array([0.07, 0.10, np.nan, 0.1])}
    spread, count = sm.algorithm_spread(vals)
    np.testing.assert_allclose(spread[:2], [0.18, 0.20], atol=1e-6)
    assert np.isnan(spread[2]) and np.isnan(spread[3])          # one algorithm only: no spread
    assert count.tolist() == [3, 2, 1, 1] and spread.dtype == np.float32


def test_value_stats_range_check_uses_float32_bounds():
    v = np.array([0.02, 0.60, 0.3, 0.01, 0.65], dtype="float32")    # float32 0.02 / 0.60 are 'at bound'
    s = sm.value_stats(v)
    assert s["n"] == 5 and s["at_bounds"] == pytest.approx(0.4)
    assert s["below_min"] == pytest.approx(0.2) and s["above_max"] == pytest.approx(0.2)
    assert sm.value_stats(np.array([np.nan])) is None


# ---------------------------------------------------------------------------- synthetic product
def tiny_aoi() -> AOI:
    return AOI(id="tiny", name="tiny", bbox=(-118.80, 34.05, -118.78, 34.07), crs="EPSG:32611", resolution=30,
               nisar={"track": 13, "frame": 71, "direction": "descending"})


def make_sme2_h5(path: Path, grid, *, zero_product=False, pad_cells=4, chunks=None, shuffle=False) -> None:
    """Synthetic SME2 file on the global 200 m EASE-Grid 2.0 around ``grid``, laid out like P05023 files."""
    minx, miny, maxx, maxy = transform_bounds(grid.crs, "EPSG:6933", *grid.bounds(), densify_pts=21)
    c0, r1 = sm.ease2_index(minx, miny)
    c1, r0 = sm.ease2_index(maxx, maxy)
    cols = np.arange(int(c0) - pad_cells, int(c1) + pad_cells + 1)
    rows = np.arange(int(r0) - pad_cells, int(r1) + pad_cells + 1)
    xc, _ = sm.ease2_centre(cols, 0)
    _, yc = sm.ease2_centre(0, rows)
    shape = (rows.size, cols.size)
    lon, _ = Transformer.from_crs("EPSG:6933", "EPSG:4326", always_xy=True).transform(xc, np.zeros_like(xc))
    _, lat = Transformer.from_crs("EPSG:6933", "EPSG:4326", always_xy=True).transform(np.zeros_like(yc), yc)

    def dataset(group, name, arr):
        ds = group.create_dataset(name, data=arr, compression="gzip", chunks=chunks, shuffle=shuffle)
        ds.attrs["_FillValue"] = np.float32(-9999) if arr.dtype.kind == "f" else arr.dtype.type(
            -127 if arr.dtype == np.int8 else -9999)
        if arr.dtype.kind == "f":
            v = arr[arr != -9999]
            for k, fn in (("min_value", np.min), ("max_value", np.max), ("mean_value", np.mean)):
                ds.attrs[k] = np.float32(fn(v))

    def coords(group):
        for name, val in (("xCoordinates", xc), ("yCoordinates", yc), ("xCoordinateSpacing", sm.EASE2_200M_CELL),
                          ("yCoordinateSpacing", -sm.EASE2_200M_CELL)):
            group[name] = val
        pr = group.create_dataset("projection", data=np.uint32(6933))
        pr.attrs["epsg_code"] = 6933

    top = np.full(shape, 14, "int16")               # recommended, PMI + TSR + DSG averaged
    top[0, :] = -9999
    top[1, :] = 15                                  # "retrieval not recommended" row
    own = np.zeros(shape, "int16")
    own[0, :] = -9999
    own[1, :] = 1
    with h5py.File(path, "w") as f:
        g = f.create_group("/science/LSAR/SME2/grids")
        coords(g)
        prod = np.full(shape, 0.0 if zero_product else float(np.mean(list(ALG_VALUES.values()))), "float32")
        prod[0, :] = -9999
        dataset(g, "soilMoisture", prod)
        dataset(g, "soilMoistureUncertainty", np.full(shape, 0.03, "float32"))
        dataset(g, "retrievalQualityFlag", top)
        sq = np.zeros(shape, "int16")
        sq[2, :] = 128                               # dense vegetation row
        dataset(g, "surfaceQualityFlag", sq)
        anc = g.create_group("ancillaryData")
        lc = np.full(shape, 2, "int8")
        lc[:, : shape[1] // 2] = 1
        dataset(anc, "landCover", lc)
        g["EASEGridColumnIndex"] = cols.astype("int32")
        g["EASEGridRowIndex"] = rows.astype("int32")
        g["longitude"] = lon.astype("float32")
        g["latitude"] = lat.astype("float32")
        for alg, val in ALG_VALUES.items():
            a = np.full(shape, val, "float32")
            a[0, :] = -9999
            grp = g.create_group(f"algorithmCandidates/{alg}")
            coords(grp)
            dataset(grp, "soilMoisture", a)
            dataset(grp, "soilMoistureUncertainty", np.full(shape, 0.04, "float32"))
            dataset(grp, "retrievalQualityFlag", own)
        i = f.create_group("/science/LSAR/identification")
        i["compositeReleaseId"] = np.bytes_(b"P05023")
        i["productVersion"] = np.bytes_(b"1.0.11")
        i["productSpecificationVersion"] = np.bytes_(b"1.0.2")


def synthetic_ref(url: str, key: str = "NISAR_L3_PR_SME2_023_013_D_071_SYNTH") -> SourceRef:
    return SourceRef("nisar-sme2", key, datetime(2026, 6, 15, 2, 40, tzinfo=UTC),
                     urls=(url,), meta=dict(granule_ur=key, maturity="PROVISIONAL",
                                            crid="P05023", track=13, frame=71, orbit_state="descending", mode="4005",
                                            pole="DHDH", polarizations=["HH", "HV"]))


def connector(lake: Lake, **options):
    ds = load_dataset("nisar-sme2")
    ds.options.update(options)
    return make_connector(ds, lake, Settings.from_env())


# ---------------------------------------------------------------------------- transform
def test_config_stages_three_algorithms_flag_spread_and_no_mask():
    ds = load_dataset("nisar-sme2")
    names = [v.name for v in ds.variables]
    assert names == ["soil_moisture", "soil_moisture_uncertainty", "soil_moisture_quality_flag", "soil_moisture_dsg",
                     "soil_moisture_pmi", "soil_moisture_tsr", "sm_algorithm_spread"]
    assert ds.options["quality_mask_bits"] == [] and sm.source_key(ds.options["sm_source"]) in sm.ALGORITHMS
    assert ds.variable("soil_moisture_quality_flag").resampling == "nearest"


def test_transform_assets_flag_and_spread(lake, tmp_path):
    aoi = tiny_aoi()
    src = tmp_path / "sme2.h5"
    make_sme2_h5(src, aoi.grid)
    con = connector(lake, sm_source="DSG")
    ref = synthetic_ref(str(src))
    it = con.transform(ref, con.extract(ref, aoi), aoi)[0]
    keys = [a.key for a in it.assets]
    assert keys == ["soil_moisture", "soil_moisture_uncertainty", "soil_moisture_quality_flag",
                    "soil_moisture_dsg", "soil_moisture_pmi", "soil_moisture_tsr", "sm_algorithm_spread"]
    smv, _ = read_cog(lake, con.cog_path(aoi, it.item_id, "soil_moisture"))
    dsg, _ = read_cog(lake, con.cog_path(aoi, it.item_id, "soil_moisture_dsg"))
    np.testing.assert_array_equal(smv, dsg)
    np.testing.assert_allclose(smv, 0.25, atol=1e-6)
    spread, _ = read_cog(lake, con.cog_path(aoi, it.item_id, "sm_algorithm_spread"))
    np.testing.assert_allclose(spread[np.isfinite(spread)], 0.18, atol=1e-6)
    unc, _ = read_cog(lake, con.cog_path(aoi, it.item_id, "soil_moisture_uncertainty"))
    np.testing.assert_allclose(unc, 0.04, atol=1e-6)                          # DSG's own uncertainty layer
    flag, _ = read_cog(lake, con.cog_path(aoi, it.item_id, "soil_moisture_quality_flag"))
    assert flag.dtype == np.int16 and set(np.unique(flag)) <= {14, 15}         # the top-level flag
    for a in it.assets:
        assert validate_raster_a(lake, a.path, aoi.grid, con.variable(a.key)).ok, a.key
    p = it.properties
    assert p["nisar:sm_source"].endswith("algorithmCandidates/DSG/soilMoisture")
    assert p["nisar:asset_sources"]["soil_moisture_quality_flag"] == "product/retrievalQualityFlag"
    assert p["nisar:asset_sources"]["sm_algorithm_spread"] == "spread(DSG,PMI,TSR)"
    assert 0 < p["nisar:recommended_fraction"] <= 1 and p["nisar:quality_mask_bits"] == []
    assert p["nisar:algorithm_stats"]["TSR"]["median"] == pytest.approx(0.07, abs=1e-6)
    assert p["nisar:spread_algorithm_count"]["3"] == pytest.approx(1.0)
    assert p["nisar:product_specification_version"] == "1.0.2" and p["nisar:product_version"] == "1.0.11"
    assert p["sar:product_type"] == "SME2" and p["fuellake:processing_id"] == "P05023"


def test_raw_subset_reads_only_the_needed_layers(lake, tmp_path):
    aoi = tiny_aoi()
    src = tmp_path / "sme2.h5"
    make_sme2_h5(src, aoi.grid)
    con = connector(lake, sm_source="DSG")
    ref = synthetic_ref(str(src))
    with lake.local_path(con.extract(ref, aoi)[0]) as p:
        sub = sm.read_subset(p)
    data = {k for k in sub["arrays"] if not k.startswith("EASEGrid")}
    assert data == {"product/retrievalQualityFlag", "DSG/soilMoisture", "DSG/soilMoistureUncertainty",
                    "PMI/soilMoisture", "TSR/soilMoisture", "ancillary/landCover", "ancillary/surfaceQualityFlag"}
    assert sub["version"] == sm.SUBSET_VERSION
    # a different sm_source needs other layers: the cached subset is re-extracted, not reused
    con2 = connector(lake, sm_source="product")
    with lake.local_path(con2.extract(ref, aoi)[0]) as p:
        assert "product/soilMoisture" in sm.read_subset(p)["arrays"]


def test_unpopulated_layer_fails_and_sm_source_option(lake, tmp_path):
    aoi = tiny_aoi()
    src = tmp_path / "sme2_zero.h5"
    make_sme2_h5(src, aoi.grid, zero_product=True)        # like the D00404 sample: top layer all zeros
    ref = synthetic_ref(str(src))
    con = connector(lake, sm_source="product")
    raw = con.extract(ref, aoi)
    with pytest.raises(sm.UnpopulatedLayerError, match="sm_source"):
        con.transform(ref, raw, aoi)
    con = connector(lake, sm_source="PMI")
    items = con.transform(ref, con.extract(ref, aoi), aoi)
    smv, _ = read_cog(lake, items[0].assets[0].path)
    np.testing.assert_allclose(smv, 0.17, atol=1e-6)
    assert items[0].properties["nisar:sm_source"].endswith("algorithmCandidates/PMI/soilMoisture")
    with pytest.raises(ValueError, match="sm_source"):
        connector(lake, sm_source="XYZ").extract(ref, aoi)


def test_quality_mask_bit0_masks_not_recommended(lake, tmp_path):
    aoi = tiny_aoi()
    src = tmp_path / "sme2.h5"
    make_sme2_h5(src, aoi.grid, pad_cells=0)               # flag row 1 (bit 0 set) lies over the AOI
    ref = synthetic_ref(str(src))
    raw = connector(lake).extract(ref, aoi)
    plain = connector(lake).transform(ref, raw, aoi)[0]
    strict = connector(lake, quality_mask_bits=[0]).transform(ref, raw, aoi)[0]
    assert plain.properties["nisar:recommended_fraction"] < 1
    assert strict.properties["fuellake:valid_fraction"] < plain.properties["fuellake:valid_fraction"]
    assert strict.properties["nisar:quality_mask_bits"] == [0]


def test_land_cover_and_dense_vegetation_properties(lake, tmp_path):
    aoi = tiny_aoi()
    src = tmp_path / "sme2.h5"
    make_sme2_h5(src, aoi.grid, pad_cells=0)
    ref = synthetic_ref(str(src))
    con = connector(lake)
    p = con.transform(ref, con.extract(ref, aoi), aoi)[0].properties
    assert set(p["nisar:recommended_fraction_by_land_cover"]) == {"tree_cover", "shrubland"}
    assert 0 < p["nisar:dense_vegetation_fraction"] < 1
    assert p["nisar:recommended_fraction_by_algorithm"]["DSG"] == p["nisar:recommended_fraction"]


def test_sme2_on_the_ease2_grid_is_copied_cell_by_cell(lake, tmp_path):
    """On an AOI with ``reference_grid: ease2-200m`` (like ca) every SME2 cell lands on one AOI cell, unchanged."""
    aoi = AOI(id="tiny", name="tiny on the SME2 grid", bbox=(-118.80, 34.05, -118.78, 34.07),
              reference_grid="ease2-200m", scale="region", nisar={"track": 13, "frame": 71, "direction": "descending"})
    src = tmp_path / "sme2.h5"
    make_sme2_h5(src, aoi.grid)
    with h5py.File(src, "r+") as f:                       # a per-cell pattern instead of a constant
        g = f["/science/LSAR/SME2/grids"]
        cols, rows = g["EASEGridColumnIndex"][:], g["EASEGridRowIndex"][:]
        pattern = (0.1 + (cols[None, :] % 7) * 0.01 + (rows[:, None] % 5) * 0.02).astype("float32")
        pattern[0, :] = -9999
        g["algorithmCandidates/DSG/soilMoisture"][...] = pattern
        g["algorithmCandidates/DSG/soilMoisture"].attrs["max_value"] = np.float32(pattern.max())
    con = connector(lake, sm_source="DSG")
    ref = synthetic_ref(str(src))
    item = con.transform(ref, con.extract(ref, aoi), aoi)[0]
    smv, grid = read_cog(lake, con.cog_path(aoi, item.item_id, "soil_moisture"))
    gc0, gr0 = sm.ease2_index(grid.x_coords()[0], grid.y_coords()[0])
    expected = (0.1 + ((gc0 + np.arange(grid.width))[None, :] % 7) * 0.01
                + ((gr0 + np.arange(grid.height))[:, None] % 5) * 0.02).astype("float32")
    np.testing.assert_array_equal(smv, expected)            # bit-identical: no resampling
    assert "copied cell by cell" in item.properties["nisar:grid"]


def test_misaligned_granule_on_the_reference_grid_fails(lake, tmp_path):
    aoi = AOI(id="tiny", name="tiny on the SME2 grid", bbox=(-118.80, 34.05, -118.78, 34.07),
              reference_grid="ease2-200m", scale="region")
    src = tmp_path / "sme2.h5"
    make_sme2_h5(src, aoi.grid)
    con = connector(lake)
    ref = synthetic_ref(str(src))
    raw = con.extract(ref, aoi)
    with h5py.File(lake.root + "/" + raw[0], "r+") as h:       # shift the subset by half a cell
        h.attrs["x0"] = float(h.attrs["x0"]) + sm.EASE2_200M_CELL / 2
        del h["EASEGridColumnIndex"]
    with pytest.raises(ValueError, match="not aligned"):
        con.transform(ref, raw, aoi)


def test_georeferencing_mismatch_raises():
    sub = {"dx": sm.EASE2_200M_CELL, "dy": -sm.EASE2_200M_CELL, "x0": SAMPLE_X0 + 100.0, "y0": SAMPLE_Y0,
           "arrays": {"EASEGridColumnIndex": np.array([SAMPLE_COL0]), "EASEGridRowIndex": np.array([SAMPLE_ROW0])}}
    with pytest.raises(ValueError, match="georeferencing mismatch"):
        sm.check_georeferencing(sub)
    sub["x0"] = SAMPLE_X0
    sm.check_georeferencing(sub)


# ---------------------------------------------------------------------------- chunk selection and empty units
def test_chunk_blocks_skip_blocks_outside_the_region(tmp_path):
    aoi = tiny_aoi()
    src = tmp_path / "sme2.h5"
    make_sme2_h5(src, aoi.grid, pad_cells=12, chunks=(8, 8))
    with h5py.File(src) as f:
        xc = f["/science/LSAR/SME2/grids/xCoordinates"][()]
        cut = float(xc[xc.size // 2])
        full = sm.read_sme2_subset(f, aoi.grid, pad_cells=12, keys=["DSG/soilMoisture"])
        half = sm.read_sme2_subset(f, aoi.grid, pad_cells=12, keys=["DSG/soilMoisture"],
                                   keep=lambda x0, y0, x1, y1: x1 <= cut + 1)
        none = sm.read_sme2_subset(f, aoi.grid, keep=lambda *b: False)
    assert none is None
    assert half["blocks_skipped"] > 0 and half["blocks_read"] < full["blocks_read"]
    a, b = full["arrays"]["DSG/soilMoisture"], half["arrays"]["DSG/soilMoisture"]
    assert a.shape == b.shape and (b == -9999).sum() > (a == -9999).sum()
    kept = b != -9999
    np.testing.assert_array_equal(a[kept], b[kept])


@pytest.mark.parametrize("shuffle", [False, True])
def test_concurrent_chunk_reader_matches_h5py(tmp_path, shuffle):
    """Chunks fetched as byte ranges and decoded here (deflate, shuffle, partial edge chunks, unwritten chunks)
    equal h5py's own reads."""
    aoi = tiny_aoi()
    src = tmp_path / "sme2.h5"
    make_sme2_h5(src, aoi.grid, pad_cells=10, chunks=(8, 8), shuffle=shuffle)
    with h5py.File(src, "r+") as f:                                 # a per-cell pattern and an unwritten chunk
        g = f["/science/LSAR/SME2/grids"]
        a = g["algorithmCandidates/PMI/soilMoisture"]
        a[...] = np.random.default_rng(1).uniform(0.02, 0.6, a.shape).astype("float32")
        g.create_dataset("algorithmCandidates/TSR/sparse", shape=a.shape, dtype="float32", chunks=(8, 8),
                         compression="gzip", fillvalue=-9999)
        g["algorithmCandidates/TSR/sparse"][0:8, 0:8] = 0.3
    keys = [*sm.subset_keys("DSG"), "TSR/sparse"]
    fetch = sm.range_fetcher(str(src), Settings.from_env())
    with h5py.File(src) as f:
        via_h5py = sm.read_sme2_subset(f, aoi.grid, pad_cells=6, keys=keys)
        via_ranges = sm.read_sme2_subset(f, aoi.grid, pad_cells=6, keys=keys, fetch=fetch)
    assert fetch.stats["requests"] > 0 and via_ranges["window"] == via_h5py["window"]
    assert set(via_ranges["arrays"]) == set(via_h5py["arrays"])
    for k, v in via_h5py["arrays"].items():
        np.testing.assert_array_equal(via_ranges["arrays"][k], v, err_msg=k)
        assert via_ranges["arrays"][k].dtype == v.dtype


def test_unit_outside_the_region_is_cached_empty(lake, tmp_path):
    aoi = tiny_aoi()
    src = tmp_path / "sme2.h5"
    make_sme2_h5(src, aoi.grid)
    con = connector(lake)
    ref = synthetic_ref(str(src))
    con._chunk_filter = lambda aoi: (lambda *b: False)         # no chunk block touches the "region"
    assert not con.cached_empty(ref, aoi)
    assert con.extract(ref, aoi) == []
    assert con.cached_empty(ref, aoi)                            # the next run skips it without network


# ---------------------------------------------------------------------------- discovery + pipeline
def fixture_items() -> list[dict]:
    return json.loads((FIX / "cmr_sme2_provisional_smm.json").read_text())["items"]


def test_discover_sme2(lake, monkeypatch):
    seen = []
    monkeypatch.setattr(ed, "search_granules", lambda s, cid, **k: seen.append(cid) or copy.deepcopy(fixture_items()))
    refs = connector(lake).discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 11, 14)))
    assert seen == ["C2854344945-ASF"]
    assert [r.meta["stack_id"] for r in refs] == ["013_D_071", "013_D_071"]
    assert all(r.meta["maturity"] == "PROVISIONAL" and r.urls[0].endswith(".h5") for r in refs)


def _renamed(item: dict, old: str, new: str) -> dict:
    return json.loads(json.dumps(item).replace(old, new))


def _shifted(item: dict, dlon: float) -> dict:
    it = copy.deepcopy(item)
    geom = it["umm"]["SpatialExtent"]["HorizontalSpatialDomain"]["Geometry"]
    for gp in geom.get("GPolygons", []):
        for p in gp["Boundary"]["Points"]:
            p["Longitude"] += dlon
    return it


def test_discover_region_aoi_keeps_granules_touching_the_region(lake, monkeypatch):
    items = fixture_items()
    asc = _renamed(items[1], "_013_D_071_", "_013_A_071_")                         # other orbit direction
    nevada = _shifted(_renamed(items[2], "20261013T0240", "20261013T0250"), 6.0)  # footprint east of California
    recs = [*copy.deepcopy(items), asc, nevada]
    monkeypatch.setattr(ed, "search_granules", lambda *a, **k: copy.deepcopy(recs))
    ca = load_aoi("ca")
    refs = connector(lake).discover(ca, Period(date(2026, 6, 1), date(2026, 11, 14)))
    keys = [r.key for r in refs]
    assert len(refs) == 3 and all(r.meta["orbit_state"] == "descending" for r in refs)      # all 3 CA stacks
    assert {r.meta["stack_id"] for r in refs} == {"013_D_071", "114_D_071"}
    assert not any("_A_071_" in k or "T0250" in k for k in keys)
    assert all(0 < r.meta["region_overlap"] <= 1 for r in refs)
    both = connector(lake, region_orbit_state=None).discover(ca, Period(date(2026, 6, 1), date(2026, 11, 14)))
    assert len(both) == 4


def test_missing_credentials_per_unit(lake, monkeypatch):
    monkeypatch.setattr(ed, "search_granules", lambda *a, **k: copy.deepcopy(fixture_items()))
    conn, aoi = connector(lake), load_aoi("smm")
    assert conn.missing_credentials(aoi) == []                         # CMR discovery is public
    ref = conn.discover(aoi, Period(date(2026, 6, 1), date(2026, 11, 14)))[0]
    assert conn.missing_credentials(aoi, ref) == ["EARTHDATA_TOKEN"]
    sample = SourceRef(ref.dataset_id, "sample", ref.datetime, (SAMPLE_URL,), ref.meta)
    assert conn.missing_credentials(aoi, sample) == []                 # public sample-data area
    lake.write_bytes(conn.raw_rel(ref, aoi), b"cached subset")
    assert conn.missing_credentials(aoi, ref) == []                    # AOI subset already in raw/
    monkeypatch.setenv("EARTHDATA_TOKEN", "tok-unit-test")
    assert conn.missing_credentials(aoi, SourceRef(ref.dataset_id, "x", ref.datetime, ref.urls, ref.meta)) == []


def test_pipeline_end_to_end_is_idempotent(test_config, tmp_path, monkeypatch):
    shutil.copy(REPO / "config" / "datasets" / "nisar-sme2.yaml", test_config / "datasets" / "nisar-sme2.yaml")
    aoi = load_aoi("tiny")
    src = tmp_path / "sme2.h5"
    make_sme2_h5(src, aoi.grid)
    monkeypatch.setattr(ed, "search_granules", lambda *a, **k: copy.deepcopy(fixture_items()))
    real_open = ed.open_h5
    opened = []

    @contextlib.contextmanager
    def fake_open(url, settings, **kw):
        opened.append(url)
        with real_open(str(src), settings) as f:
            yield f

    monkeypatch.setattr(ed, "open_h5", fake_open)
    real_fetcher = sm.range_fetcher
    monkeypatch.setattr(sm, "range_fetcher", lambda url, settings, **kw: real_fetcher(str(src), settings, **kw))
    lake = Lake.from_env()
    period = Period(date(2026, 6, 1), date(2026, 11, 14))
    rep = pipeline.run(["nisar-sme2"], "tiny", period, lake=lake)
    assert rep.ok and rep.datasets[0].count("created") == 2, rep.datasets[0].units
    assert all(r.ok for r in validate_collection(lake, load_dataset("nisar-sme2")))
    n_open = len(opened)
    rep2 = pipeline.run(["nisar-sme2"], "tiny", period, lake=lake)
    assert rep2.datasets[0].skipped == 2 and not rep2.datasets[0].units and len(opened) == n_open


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_discover_smm(lake):
    refs = connector(lake).discover(load_aoi("smm"), Period(date(2026, 6, 1), date(2026, 11, 14)))
    assert len(refs) >= 8 and all(r.meta["stack_id"] == "013_D_071" for r in refs)
    assert {r.meta["crid"] for r in refs} == {"P05023"}


@pytest.mark.network
def test_live_discover_ca_one_cycle(lake):
    refs = connector(lake).discover(load_aoi("ca"), Period(date(2026, 9, 7), date(2026, 9, 18)))
    assert 25 <= len(refs) <= 45 and {r.meta["orbit_state"] for r in refs} == {"descending"}
    assert len({connector(lake).item_id(load_aoi("ca"), r, ed.item_discriminator(r)) for r in refs}) == len(refs)


@pytest.mark.network
def test_public_sample_georeferencing_and_transform(lake):
    """Real NISAR SME2 file (public pre-launch sample): georeferencing checked against the file's own
    EASE-Grid indices and latitude/longitude vectors; transform run with sm_source=DSG."""
    aoi = AOI(id="sme2sample", name="inside the SME2 sample footprint", bbox=(-90.5, 35.70, -90.3, 35.85),
              crs="EPSG:32615", resolution=30)
    con = connector(lake, sm_source="DSG")
    con._region_cache = None                                        # the sample lies outside California
    ur = SAMPLE_URL.rsplit("/", 1)[-1][:-3]
    ref = SourceRef("nisar-sme2", ur, datetime(2019, 8, 29, 18, 7, 59, tzinfo=UTC), urls=(SAMPLE_URL,),
                    meta=dict(granule_ur=ur, maturity="BETA", crid="D00404", track=8, frame=70,
                              orbit_state="descending", mode="4000", pole="QPNA",
                              polarizations=["HH", "HV", "VV", "VH"]))
    raw = con.extract(ref, aoi)
    with lake.local_path(raw[0]) as p:
        sub = sm.read_subset(p)
    sm.check_georeferencing(sub)                                    # vs EASEGridColumnIndex / RowIndex
    with ed.open_h5(SAMPLE_URL, Settings.from_env()) as f:          # vs the file's latitude/longitude vectors
        g = f["/science/LSAR/SME2/grids"]
        r0, r1, c0, c1 = sub["window"]
        lat = g["latitude"][r0:r1]
        lon = g["longitude"][c0:c1]
    t = ed.transform_from_centres(sub["x0"], sub["y0"], sub["dx"], sub["dy"])
    x_mid, y_mid = t @ (0.5 + (c1 - c0) // 2, 0.5 + (r1 - r0) // 2)
    lo, la = Transformer.from_crs("EPSG:6933", "EPSG:4326", always_xy=True).transform(x_mid, y_mid)
    assert lo == pytest.approx(float(lon[(c1 - c0) // 2]), abs=2e-4)
    assert la == pytest.approx(float(lat[(r1 - r0) // 2]), abs=2e-4)
    it = con.transform(ref, raw, aoi)[0]
    assert it.properties["nisar:sm_source"].endswith("DSG/soilMoisture")
    # the staged COG reproduces native cell values at native cell centres (a one-cell shift would not)
    native = sm.layer_values(sub, "DSG/soilMoisture")
    cog, grid = read_cog(lake, con.cog_path(aoi, it.item_id, "soil_moisture"))
    to_utm = Transformer.from_crs("EPSG:6933", aoi.crs, always_xy=True)
    errs, errs_shift = [], []
    inv = ~grid.transform
    for r in range(2, native.shape[0] - 2, 3):
        for c in range(2, native.shape[1] - 2, 3):
            x, y = t @ (c + 0.5, r + 0.5)
            col, row = (int(v) for v in inv @ to_utm.transform(x, y))
            if 0 <= row < grid.height and 0 <= col < grid.width and np.isfinite(cog[row, col]) \
                    and np.isfinite(native[r, c]) and np.isfinite(native[r, c + 1]):
                errs.append(abs(cog[row, col] - native[r, c]))
                errs_shift.append(abs(cog[row, col] - native[r, c + 1]))
    assert len(errs) > 50
    assert float(np.mean(errs)) < 0.5 * float(np.mean(errs_shift))
    item = pipeline.build_stac_item(lake, con.dataset, aoi, ref, it, "test")
    assert all(r.ok for r in validate_item(lake, item, con.dataset, aoi, region=None))   # outside California


def _real_ref(con, aoi, day: date) -> SourceRef:
    refs = con.discover(aoi, Period(day, day))
    assert refs, f"no SME2 granule for {aoi.id} on {day}"
    return refs[0]


@pytest.mark.earthdata
@pytest.mark.skipif(not os.environ.get("EARTHDATA_TOKEN"), reason="needs EARTHDATA_TOKEN")
def test_real_provisional_sme2_flag_layout_and_transform(lake):
    """Real P05023 granule (2026-10-13, stack 013/D/071) over the tiny AOI: the flag layout of the module
    docstring holds, all three algorithms are populated, and the item validates."""
    aoi = tiny_aoi()
    con = connector(lake, sm_source="product")                     # reads the top-level layer too
    ref = _real_ref(con, aoi, date(2026, 10, 13))
    raw = con.extract(ref, aoi)
    with lake.local_path(raw[0]) as p:
        sub = sm.read_subset(p)
    assert sub["identification"]["productSpecificationVersion"] == "1.0.2"
    dec = sm.decode_quality_flag(sub["arrays"]["product/retrievalQualityFlag"])
    vals = {a: sm.layer_values(sub, f"{a}/soilMoisture") for a in sm.ALGORITHMS}
    ok = dec["valid"]
    for a in sm.ALGORITHMS:
        assert np.isfinite(vals[a][ok]).mean() > 0.9, a
    stack = np.stack([np.where(dec[f"included_{a}"], vals[a], np.nan) for a in sm.ALGORITHMS])
    np.testing.assert_allclose(sm.layer_values(sub, "product/soilMoisture")[ok], np.nanmean(stack[:, ok], axis=0),
                               atol=1e-5)
    item = pipeline.build_stac_item(lake, con.dataset, aoi, ref, con.transform(ref, raw, aoi)[0], "test")
    assert all(r.ok for r in validate_item(lake, item, con.dataset, aoi))


@pytest.mark.earthdata
@pytest.mark.skipif(not os.environ.get("EARTHDATA_TOKEN"), reason="needs EARTHDATA_TOKEN")
def test_real_first_date_after_the_outage(lake):
    """2026-08-14 is the first track-13 date after the 2026-07-27..08-10 gap (its T-12 granule is missing):
    the unit must still stage the configured source and the spread."""
    aoi = tiny_aoi()
    con = connector(lake)
    ref = _real_ref(con, aoi, date(2026, 8, 14))
    it = con.transform(ref, con.extract(ref, aoi), aoi)[0]
    assert {"soil_moisture", "sm_algorithm_spread", "soil_moisture_quality_flag"} <= {a.key for a in it.assets}
