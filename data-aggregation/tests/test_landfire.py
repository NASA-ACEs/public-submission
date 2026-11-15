# fuel-lake — tests for the LANDFIRE FBFM40 connector (releases, code tables, metadata, remote zip members, pipeline)
# Licensed under Apache-2.0.
from __future__ import annotations

import io
import shutil
import zipfile
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml

from conftest import TINY_AOI
from fuellake import catalog, pipeline
from fuellake.connectors.base import Period
from fuellake.connectors.landfire import (
    LandfireFbfm40Connector,
    native_clip_request,
    parse_code_table,
    parse_metadata,
    read_zip_member,
    release_list,
    release_tag,
)
from fuellake.contracts import validate_collection
from fuellake.rasters import read_cog
from fuellake.registry import load_aoi, load_dataset
from landfire_fakes import LF_XML, FakeLandfire, service_json, zip_bytes

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
FIXTURES = Path(__file__).parent / "fixtures" / "open_rasters"
DS = "landfire-fbfm40"
SERVICE = service_json("Landfire_LF2025/LF2025_FBFM40_CONUS")
METADATA_XML = """<metadata><idinfo><citation><citeinfo><origin>LANDFIRE</origin><pubdate>20260430</pubdate>
<title>LANDFIRE 2025 Scott and Burgan Fire Behavior Fuel Model (FBFM40) CONUS</title><edition>LF 2025</edition>
<onlink>https://doi.org/10.5066/P1MAAYLC</onlink></citeinfo></citation></idinfo>
<useconst>These data are in the public domain and do not have any use constraints.</useconst></metadata>"""
PERIOD = Period(date(2026, 6, 1), date(2026, 6, 2))


@pytest.fixture
def lf_config(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    shutil.copy(REPO_CONFIG / "datasets" / f"{DS}.yaml", root / "datasets" / f"{DS}.yaml")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


# ---------------------------------------------------------------------------- pure helpers
def test_code_table_from_official_csv():
    classes = parse_code_table((FIXTURES / "LF2025_FBFM40.csv").read_text())
    assert len(classes) == 45
    assert min(classes) == 91 and max(classes) == 204
    assert classes[91] == "NB1" and classes[98] == "NB8" and classes[102] == "GR2"
    assert classes[145] == "SH5" and classes[165] == "TU5" and classes[204] == "SB4"
    assert -9999 not in classes
    assert parse_code_table("﻿" + (FIXTURES / "LF2025_FBFM40.csv").read_text()) == classes   # BOM tolerated


def test_metadata_facts():
    m = parse_metadata(METADATA_XML)
    assert m["edition"] == "LF 2025" and m["pubdate"] == "2026-04-30"
    assert m["doi"] == "https://doi.org/10.5066/P1MAAYLC"


@pytest.mark.parametrize("name, edition, pubdate, ground, doi, use", [
    ("LF2016_FBFM40_CONUS.xml", "LF Remap", "2020-08-21", "2020 ground condition", None, "None"),
    ("LC22_F40_230.xml", "LF 2022", "2023-05-01", "2022 ground condition", "https://doi.org/10.5066/P974JF8W", "None"),
    ("LF2025_FBFM40_CONUS.xml", "LF 2025", "2026-04-30", "2025 ground condition", "https://doi.org/10.5066/P1MAAYLC",
     "These data are marked with a Creative Common CC0 1.0 Universal License. These data are in the public domain "
     "and do not have any use constraints."),
])
def test_real_fgdc_metadata_of_each_release(name, edition, pubdate, ground, doi, use):
    """The official FGDC files (LF 2016 from the full-extent zip). LF 2016 cites RMRS-GTR-153's DOI in a later
    citation: it must not be taken as the dataset DOI."""
    m = parse_metadata((LF_XML / name).read_text())
    assert (m["edition"], m["pubdate"], m["ground_condition"], m["doi"], m["use_constraints"]) == \
        (edition, pubdate, ground, doi, use)
    assert "FBFM40" in m["title"]


def test_native_clip_request_is_aligned_to_service_grid():
    p = native_clip_request(TINY_AOI["bbox"], SERVICE)
    xmin, ymin, xmax, ymax = (float(v) for v in p["bbox"].split(","))
    assert (xmin - SERVICE["extent"]["xmin"]) % 30 == 0 and (SERVICE["extent"]["ymax"] - ymax) % 30 == 0
    w, h = (int(v) for v in p["size"].split(","))
    assert (w, h) == ((xmax - xmin) / 30, (ymax - ymin) / 30)
    assert p["interpolation"] == "RSP_NearestNeighbor" and p["imageSR"] == 5070 and p["format"] == "tiff"


def test_releases_share_one_lattice():
    """LF 2016 starts one cell west of LF 2025: the snapped request is the same, so releases align cell by cell."""
    lf16 = {**SERVICE, "extent": {**SERVICE["extent"], "xmin": -2362425, "ymax": 3177435}}
    assert native_clip_request(TINY_AOI["bbox"], lf16)["bbox"] == native_clip_request(TINY_AOI["bbox"], SERVICE)["bbox"]


# ---------------------------------------------------------------------------- releases
def test_repo_config_releases():
    rels = release_list(load_dataset(DS).options)
    assert [(r["version"], r["year"], release_tag(r)) for r in rels] == [
        ("LF 2016", 2016, "lf2016"), ("LF 2022", 2022, "lf2022"), ("LF 2025", 2025, "lf2025")]
    assert all(r["image_service"].startswith("https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF20") for r in rels)
    assert rels[0]["classes_csv"]["zip"].endswith("/CONUS_LF2016/LF2016_FBFM40_CONUS.zip")
    assert [r["disturbances"]["last_year"] for r in rels] == [2016, 2022, 2025]
    assert rels[1]["disturbances"]["through"] == "2022-09-30"


def test_single_release_options_still_work():
    rels = release_list({"product_year": 2025, "product_version": "LF 2025"})
    assert len(rels) == 1 and rels[0]["image_service"].endswith("/LF2025_FBFM40_CONUS/ImageServer")
    assert release_tag(rels[0]) == "lf2025"


def test_bad_release_lists_are_refused():
    base = {"version": "LF 2022", "year": 2022, "image_service": "https://x/ImageServer", "classes_csv": "https://x.csv"}
    with pytest.raises(ValueError, match="share a year"):
        release_list({"releases": [base, {**base, "version": "LF 2022b"}]})
    with pytest.raises(ValueError, match="lacks"):
        release_list({"releases": [{**base, "image_service": None}]})
    with pytest.raises(ValueError, match="'zip' and 'member'"):
        release_list({"releases": [{**base, "classes_csv": {"zip": "https://x.zip"}}]})


# ---------------------------------------------------------------------------- remote zip members
def test_read_zip_member_reads_only_a_small_part():
    data = zip_bytes({"a/CSV_Data/x.csv": b"VALUE,FBFM40\n91,NB1\n", "a/meta.xml": b"<m/>"})
    served = []

    def fetch(start, end):
        served.append(end - start + 1)
        return data[start:end + 1]

    assert read_zip_member(fetch, len(data), "a/CSV_Data/x.csv") == b"VALUE,FBFM40\n91,NB1\n"
    assert sum(served) < len(data) / 2            # the 200 kB placeholder member is never read
    with pytest.raises(KeyError, match="no member"):
        read_zip_member(fetch, len(data), "a/missing.csv")
    with pytest.raises(ValueError, match="not fetched"):
        read_zip_member(fetch, len(data), "Tif/BIG_PLACEHOLDER.tif", max_bytes=1000)


def test_read_zip_member_handles_zip64():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", allowZip64=True) as z:
        with z.open("m/x.csv", "w", force_zip64=True) as f:
            f.write(b"VALUE\n1\n")
    data = buf.getvalue()
    assert read_zip_member(lambda a, b: data[a:b + 1], len(data), "m/x.csv") == b"VALUE\n1\n"


# ---------------------------------------------------------------------------- offline pipeline
def test_pipeline_offline_three_releases(lake, lf_config, monkeypatch):
    fake = FakeLandfire().patch(monkeypatch, LandfireFbfm40Connector)
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", [u.error for u in d.units]
    items = {it["id"]: it for it in catalog.iter_items(lake, DS)}
    assert sorted(items) == ["landfire-fbfm40__tiny__20160101__lf2016", "landfire-fbfm40__tiny__20220101__lf2022",
                             "landfire-fbfm40__tiny__20250101__lf2025"]
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))

    lf16 = items["landfire-fbfm40__tiny__20160101__lf2016"]["properties"]
    assert lf16["fuellake:source_key"] == "landfire:Landfire_LF2016/LF2016_FBFM40_CONUS"
    assert lf16["fuellake:landfire_version"] == "LF 2016" and lf16["fuellake:landfire_edition"] == "LF Remap"
    assert lf16["fuellake:landfire_doi"] is None and lf16["fuellake:landfire_use_constraints"] == "None"
    assert lf16["fuellake:landfire_ground_condition"] == "2020 ground condition"
    assert lf16["fuellake:landfire_disturbances"]["last_year"] == 2016
    assert lf16["fuellake:landfire_files"]["classes_csv"].endswith("LF2016_FBFM40_CONUS.zip!/"
                                                                  "LF2016_FBFM40_CONUS/CSV_Data/LF2016_FBFM40.csv")
    assert lf16["start_datetime"] == "2016-01-01T00:00:00Z" and lf16["end_datetime"].startswith("2016-12-31")
    lf22 = items["landfire-fbfm40__tiny__20220101__lf2022"]["properties"]
    assert lf22["fuellake:landfire_doi"] == "https://doi.org/10.5066/P974JF8W"
    assert lf22["fuellake:landfire_disturbances"]["through"] == "2022-09-30"
    lf25 = items["landfire-fbfm40__tiny__20250101__lf2025"]
    p = lf25["properties"]
    assert p["fuellake:source_key"] == "landfire:Landfire_LF2025/LF2025_FBFM40_CONUS"   # key of the 1-release build
    assert p["fuellake:landfire_version"] == "LF 2025" and p["fuellake:landfire_pubdate"] == "2026-04-30"
    assert "CC0" in p["fuellake:landfire_use_constraints"]
    assert 0 < p["fuellake:qc_masked_fraction"] < 0.05           # the unknown code 100
    assert lf25["assets"]["classes"]["roles"] == ["metadata"]
    assert lf25["assets"]["fuel_model"]["raster:bands"][0] == {"data_type": "int16", "nodata": -9999}

    seen = {}
    for iid, it in items.items():
        arr, grid = read_cog(lake, catalog.asset_rel(it, "fuel_model"))
        assert arr.dtype == np.int16 and grid.shape == load_aoi("tiny").grid.shape
        seen[iid.rsplit("__", 1)[-1]] = set(np.unique(arr).tolist())
    assert seen["lf2016"] <= {-9999, 142, 122, 102, 165, 98} and 142 in seen["lf2016"]   # nearest: no new codes
    assert seen["lf2022"] <= {-9999, 145, 121, 101, 165, 98} and 145 in seen["lf2022"]
    with lake.local_path(catalog.asset_rel(lf25, "fuel_model")) as path, rasterio.open(path) as src:
        assert src.nodata == -9999 and "145=SH5" in src.tags()["FBFM40_CODES"]
        assert src.tags()["LANDFIRE_RELEASE"] == "LF 2025"
    # zip members: only a small part of each archive was transferred, always with Range requests
    for url, n in fake.zip_bytes_served.items():
        assert n < len(fake.zips[url]) / 2, url
    assert all(h and "Range" in h for u, _, h in fake.calls if u.endswith(".zip"))

    n_calls = len(fake.calls)
    rep2 = pipeline.run([DS], "tiny", Period(date(2026, 7, 1), date(2026, 7, 2)), lake=lake)
    assert (rep2.datasets[0].skipped, len(rep2.datasets[0].units)) == (3, 0)
    assert len(fake.calls) == n_calls + 3                         # only the three service checks in discover


def test_single_release_key_matches_multi_release_key(lake, lf_config, monkeypatch):
    """A lake staged with the former single-release config (LF 2025) is not re-staged by the release list."""
    cfg = yaml.safe_load((lf_config / "datasets" / f"{DS}.yaml").read_text())
    lf25 = cfg["options"]["releases"][-1]
    cfg["options"] = {"image_service": lf25["image_service"], "classes_csv": lf25["classes_csv"],
                      "metadata_xml": lf25["metadata_xml"], "product_version": "LF 2025", "product_year": 2025}
    (lf_config / "datasets" / f"{DS}.yaml").write_text(yaml.safe_dump(cfg))
    FakeLandfire().patch(monkeypatch, LandfireFbfm40Connector)
    assert pipeline.run([DS], "tiny", PERIOD, lake=lake).datasets[0].count("created") == 1
    shutil.copy(REPO_CONFIG / "datasets" / f"{DS}.yaml", lf_config / "datasets" / f"{DS}.yaml")
    d = pipeline.run([DS], "tiny", PERIOD, lake=lake).datasets[0]
    assert (d.skipped, d.count("created")) == (1, 2)
    assert len(list(catalog.iter_items(lake, DS))) == 3


def test_server_ignoring_range_fails_the_unit(lake, lf_config, monkeypatch):
    fake = FakeLandfire(honour_range=False).patch(monkeypatch, LandfireFbfm40Connector)
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    units = {u.key: u for u in rep.datasets[0].units}
    assert units["landfire:Landfire_LF2016/LF2016_FBFM40_CONUS"].status == "failed"
    assert "206 Partial Content" in units["landfire:Landfire_LF2016/LF2016_FBFM40_CONUS"].error
    assert units["landfire:Landfire_LF2025/LF2025_FBFM40_CONUS"].status == "created"     # other releases unaffected
    assert fake.zip_bytes_served == {}


def test_export_error_body_is_reported(lake, lf_config, monkeypatch):
    fake = FakeLandfire()

    def get(self, url, **kw):
        if url.endswith("/exportImage"):
            from landfire_fakes import Resp
            return Resp(b'{"error": {"code": 400, "message": "Invalid bbox"}}', "application/json")
        return fake.get(self, url, **kw)
    monkeypatch.setattr(LandfireFbfm40Connector, "_get", get)
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    u = rep.datasets[0].units[0]
    assert u.status == "failed" and "Invalid bbox" in u.error


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_landfire_tiny_aoi(lake, lf_config):
    rep = pipeline.run([DS], "tiny", Period(date(2026, 11, 1), date(2026, 11, 14)), lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", d.error or [u.error for u in d.units]
    items = {it["properties"]["fuellake:landfire_version"]: it for it in catalog.iter_items(lake, DS)}
    assert set(items) == {"LF 2016", "LF 2022", "LF 2025"}
    table = parse_code_table((FIXTURES / "LF2025_FBFM40.csv").read_text())
    arrays = {}
    for version, item in items.items():
        assert item["properties"]["fuellake:classes"] == {str(k): v for k, v in sorted(table.items())}
        arr, _ = read_cog(lake, catalog.asset_rel(item, "fuel_model"))
        codes = set(np.unique(arr[arr != -9999]).tolist())
        assert codes and codes <= set(table), version
        arrays[version] = arr
    assert any(141 <= c <= 149 for c in np.unique(arrays["LF 2025"]))   # shrub models in the Santa Monica Mountains
    assert (arrays["LF 2016"] != arrays["LF 2022"]).any()                # the releases differ
    assert items["LF 2016"]["properties"]["fuellake:landfire_edition"] == "LF Remap"
    assert items["LF 2022"]["properties"]["fuellake:landfire_doi"] == "https://doi.org/10.5066/P974JF8W"
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))
