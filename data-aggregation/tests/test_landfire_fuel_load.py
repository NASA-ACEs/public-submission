# fuel-lake — tests for landfire-fuel-load (RMRS-GTR-153 table transcription, unit conversion, code mapping, pipeline)
# Licensed under Apache-2.0.
from __future__ import annotations

import csv
import re
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import yaml

from conftest import TINY_AOI
from fuellake import catalog, pipeline
from fuellake.connectors.base import Period
from fuellake.connectors.landfire import LandfireFbfm40Connector, parse_code_table
from fuellake.connectors.landfire_fuel_load import (
    KG_M2_PER_TON_ACRE,
    LOAD_CLASSES,
    map_codes,
    parse_parameter_table,
)
from fuellake.contracts import validate_collection
from fuellake.rasters import read_cog
from fuellake.registry import load_dataset
from landfire_fakes import FakeLandfire

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
TABLE_CSV = REPO_CONFIG / "tables" / "fbfm40_parameters.csv"
FIX = Path(__file__).parent / "fixtures" / "fuel_load"
LF_FIX = Path(__file__).parent / "fixtures" / "open_rasters"
DS, SRC = "landfire-fuel-load", "landfire-fbfm40"


@pytest.fixture(scope="module")
def table():
    return parse_parameter_table(TABLE_CSV.read_text())


# ---------------------------------------------------------------------------- table transcription
ROW_RE = re.compile(r"^(GR|GS|SH|TU|TL|SB)(\d)\s+" + r"\s+".join([r"([\d.]+)"] * 5) + r"\s+(dynamic|static|N/A)\s+"
                    + r"\s+".join([r"(\d+)"] * 3) + r"\s+([\d.]+)\s+(\d+)\s+(\d+)\s*$")


def _table7_from_pdf_text() -> dict[str, tuple]:
    """Rows of Table 7 parsed from ``pdftotext -layout`` of RMRS-GTR-153 PDF page 25 (printed page 18)."""
    text = (FIX / "rmrs_gtr153_pdfpage25_table7.txt").read_text()
    assert text.startswith("Table 7—Fuel model parameters.")
    assert "RMRS-GTR-153. 2005" in text
    rows = {}
    for line in text.splitlines():
        m = ROW_RE.match(line.strip())
        if m:
            rows[m.group(1) + m.group(2)] = m.groups()[2:]
    return rows


def test_table_csv_equals_publication_table7(table):
    pdf_rows = _table7_from_pdf_text()
    assert len(pdf_rows) == 40
    by_model = {fm.model: fm for fm in table.values()}
    for model, g in pdf_rows.items():
        fm = by_model[model]
        loads = [float(x) for x in g[0:5]]
        assert [fm.loads_tac[c] for c in LOAD_CLASSES] == loads, model
        assert (fm.fuel_model_type or "") == g[5], model
        assert [fm.sav_per_ft["1h"], fm.sav_per_ft["live_herb"], fm.sav_per_ft["live_woody"]] == \
            [float(g[6]), float(g[7]), float(g[8])], model
        assert fm.depth_ft == float(g[9]) and fm.mx_dead_pct == float(g[10]) and fm.heat_btu_per_lb == float(g[11])
    # the only burnable rows are exactly the 40 rows of Table 7
    assert {fm.model for fm in table.values() if fm.burnable} == set(pdf_rows)


def _model_pages() -> dict[str, dict]:
    with open(FIX / "rmrs_gtr153_model_pages.csv") as f:
        return {r["model"]: r for r in csv.DictReader(f)}


def test_table_agrees_with_fuel_model_pages(table):
    """Each fuel model page prints the code number, the fine fuel load (1-h + live herb + live woody), the packing
    ratio and the extinction moisture: an independent check of the Table 7 transcription."""
    pages = _model_pages()
    assert len(pages) == 45
    for fm in table.values():
        p = pages[fm.model]
        assert int(p["code"]) == fm.code, fm.model
        assert int(p["printed_page"]) == int(p["pdf_page"]) - 7
        if not fm.burnable:
            continue
        assert fm.fine_load_tac == pytest.approx(float(p["fine_fuel_load_tac"]), abs=0.006), fm.model
        assert fm.mx_dead_pct == float(p["mx_pct"]), fm.model
        beta = fm.packing_ratio()
        assert beta == pytest.approx(float(p["packing_ratio"]), rel=3e-3, abs=1.1e-5), fm.model


def test_short_ton_is_the_only_unit_that_reproduces_packing_ratios(table):
    pages = _model_pages()
    gr1 = next(fm for fm in table.values() if fm.model == "GR1")
    assert round(gr1.packing_ratio(), 5) == float(pages["GR1"]["packing_ratio"]) == 0.00143
    tonne_lb = 1000 / 0.45359237
    off = [fm.packing_ratio(tonne_lb) / float(pages[fm.model]["packing_ratio"]) - 1 for fm in table.values()
           if fm.burnable]
    assert min(off) > 0.09            # a metric tonne would be ~10 % too high for every model


def test_codes_and_labels_match_landfire_code_table(table):
    lf = parse_code_table((LF_FIX / "LF2025_FBFM40.csv").read_text())
    assert set(lf) == set(table)
    assert all(lf[c] == table[c].model for c in table)
    assert {c for c, fm in table.items() if not fm.burnable} == {91, 92, 93, 98, 99}


def test_csv_header_cites_source_and_pages():
    head = "".join(line for line in TABLE_CSV.read_text().splitlines(keepends=True) if line.startswith("#"))
    assert "RMRS-GTR-153" in head and "doi:10.2737/RMRS-GTR-153" in head
    assert "printed page 18 (PDF page 25)" in head and "research.fs.usda.gov/download/treesearch/9521.pdf" in head


# ---------------------------------------------------------------------------- units and mapping
def test_unit_conversion_constants(table):
    assert KG_M2_PER_TON_ACRE == pytest.approx(907.18474 / 4046.8564224, rel=1e-12)
    assert KG_M2_PER_TON_ACRE == pytest.approx(0.2241702, abs=1e-7)
    sh5 = table[145].si()
    assert sh5["total_load"] == pytest.approx((3.60 + 2.10 + 0.00 + 0.00 + 2.90) * 0.22417023, rel=1e-6)
    assert sh5["dead_1h_load"] == pytest.approx(0.807013, abs=1e-6)
    assert sh5["live_woody_load"] == pytest.approx(0.650094, abs=1e-6)
    assert sh5["fuel_bed_depth"] == pytest.approx(6.0 * 0.3048)
    assert sh5["moisture_of_extinction"] == 15.0
    nb = table[91].si()
    assert nb["total_load"] == 0.0 and nb["fuel_bed_depth"] == 0.0 and np.isnan(nb["moisture_of_extinction"])


def test_map_codes_nodata_unknown_and_nonburnable(table):
    codes = np.array([[145, 91, -9999], [100, 147, 98]], dtype="int16")
    out, unknown = map_codes(codes, table, nodata=-9999)
    assert unknown == pytest.approx(1 / 5)                       # code 100 is not a fuel model
    assert out["burnable"].tolist() == [[1, 0, 255], [255, 1, 0]]
    tl = out["total_load"]
    assert tl[0, 0] == pytest.approx(8.6 * KG_M2_PER_TON_ACRE, rel=1e-6)
    assert tl[0, 1] == 0.0 and tl[1, 2] == 0.0                   # non-burnable: zero load
    assert np.isnan(tl[0, 2]) and np.isnan(tl[1, 0])             # nodata and unknown code
    parts = sum(out[k] for k in ("dead_1h_load", "dead_10h_load", "dead_100h_load", "live_herb_load",
                                 "live_woody_load"))
    np.testing.assert_allclose(parts, tl, rtol=1e-6)
    assert np.isnan(out["moisture_of_extinction"][0, 1]) and out["moisture_of_extinction"][1, 1] == 15.0
    assert out["total_load"].dtype == np.float32 and out["burnable"].dtype == np.uint8


def test_parser_rejects_bad_rows():
    good = TABLE_CSV.read_text()
    with pytest.raises(ValueError, match="duplicate"):
        parse_parameter_table(good + good.splitlines()[-1] + "\n")
    bad = good.replace("91,NB1,Urban/Developed,false,0,", "91,NB1,Urban/Developed,false,1,")
    with pytest.raises(ValueError, match="zero load"):
        parse_parameter_table(bad)


# ---------------------------------------------------------------------------- offline pipeline
@pytest.fixture
def fl_config(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "tables").mkdir()
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    for ds in (DS, SRC):
        shutil.copy(REPO_CONFIG / "datasets" / f"{ds}.yaml", root / "datasets" / f"{ds}.yaml")
    shutil.copy(TABLE_CSV, root / "tables" / TABLE_CSV.name)
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


PERIOD = Period(date(2026, 6, 1), date(2026, 6, 2))


def test_missing_source_item_fails_with_hint(lake, fl_config):
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    d = rep.datasets[0]
    assert d.status == "failed" and "fuellake run landfire-fbfm40 --aoi tiny" in d.error and "first" in d.error


def test_pipeline_offline_fuel_load_one_item_per_release(lake, fl_config, monkeypatch):
    FakeLandfire().patch(monkeypatch, LandfireFbfm40Connector)
    assert pipeline.run([SRC], "tiny", PERIOD, lake=lake).datasets[0].status == "ok"
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", d.error or [u.error for u in d.units]
    items = {it["id"]: it for it in catalog.iter_items(lake, DS)}
    sources = {it["id"]: it for it in catalog.iter_items(lake, SRC)}
    assert sorted(items) == ["landfire-fuel-load__tiny__20160101__lf2016", "landfire-fuel-load__tiny__20220101__lf2022",
                             "landfire-fuel-load__tiny__20250101__lf2025"]
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))
    table = parse_parameter_table(TABLE_CSV.read_text())
    for iid, item in items.items():
        p = item["properties"]
        src_id = iid.replace(DS, SRC)
        src = sources[src_id]
        sp = src["properties"]
        assert p["fuellake:derived_from"]["item_id"] == src_id
        assert p["fuellake:derived_from"]["source_key"] == sp["fuellake:source_key"]
        assert p["fuellake:derived_from"]["landfire_version"] == sp["fuellake:landfire_version"]
        assert p["fuellake:source_ids"][0] == src_id
        assert p["fuellake:landfire_version"] == sp["fuellake:landfire_version"]
        assert p["fuellake:landfire_release_year"] == sp["fuellake:landfire_release_year"]
        assert p["fuellake:landfire_disturbances"] == sp["fuellake:landfire_disturbances"]
        assert (p["datetime"], p["start_datetime"], p["end_datetime"]) == \
            (sp["datetime"], sp["start_datetime"], sp["end_datetime"])
        assert set(item["assets"]) == {v.name for v in load_dataset(DS).variables} | {"parameters"}

        codes, _ = read_cog(lake, catalog.asset_rel(src, "fuel_model"))
        tl, _ = read_cog(lake, catalog.asset_rel(item, "total_load"))
        flag, _ = read_cog(lake, catalog.asset_rel(item, "burnable"))
        present = [int(c) for c in np.unique(codes) if c != -9999]
        assert len(present) >= 4, iid
        for code in present:
            sel = codes == code
            np.testing.assert_allclose(tl[sel], table[code].total_load_tac * KG_M2_PER_TON_ACRE, rtol=1e-6)
            assert (flag[sel] == (1 if table[code].burnable else 0)).all()
        assert np.isnan(tl[codes == -9999]).all() and (flag[codes == -9999] == 255).all()
    # the releases differ, and so do their derived loads
    a = read_cog(lake, catalog.asset_rel(items["landfire-fuel-load__tiny__20160101__lf2016"], "total_load"))[0]
    b = read_cog(lake, catalog.asset_rel(items["landfire-fuel-load__tiny__20220101__lf2022"], "total_load"))[0]
    assert not np.allclose(np.nan_to_num(a), np.nan_to_num(b))

    rep2 = pipeline.run([DS], "tiny", Period(date(2026, 7, 1), date(2026, 7, 2)), lake=lake)
    assert (rep2.datasets[0].skipped, len(rep2.datasets[0].units)) == (3, 0)     # idempotent


def test_new_release_adds_only_its_derived_item(lake, fl_config, monkeypatch):
    """Staging one more FBFM40 release later derives exactly one new fuel-load item; existing ones are skipped."""
    FakeLandfire().patch(monkeypatch, LandfireFbfm40Connector)
    cfg_path = fl_config / "datasets" / f"{SRC}.yaml"
    full = yaml.safe_load(cfg_path.read_text())
    two = {**full, "options": {**full["options"], "releases": full["options"]["releases"][1:]}}
    cfg_path.write_text(yaml.safe_dump(two))
    pipeline.run([SRC, DS], "tiny", PERIOD, lake=lake)
    assert len(list(catalog.iter_items(lake, DS))) == 2
    cfg_path.write_text(yaml.safe_dump(full))
    rep = pipeline.run([SRC, DS], "tiny", PERIOD, lake=lake)
    fl = next(d for d in rep.datasets if d.dataset_id == DS)
    assert (fl.skipped, fl.count("created")) == (2, 1)
    assert {it["properties"]["fuellake:landfire_version"] for it in catalog.iter_items(lake, DS)} == \
        {"LF 2016", "LF 2022", "LF 2025"}


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_fuel_load_tiny_aoi(lake, fl_config):
    rep = pipeline.run([SRC, DS], "tiny", Period(date(2026, 11, 1), date(2026, 11, 14)), lake=lake)
    assert all(d.status == "ok" for d in rep.datasets), [(d.error, [u.error for u in d.units]) for d in rep.datasets]
    table = parse_parameter_table(TABLE_CSV.read_text())
    sources = {it["id"]: it for it in catalog.iter_items(lake, SRC)}
    items = list(catalog.iter_items(lake, DS))
    assert len(items) == len(sources) == 3
    for item in items:
        src = sources[item["properties"]["fuellake:derived_from"]["item_id"]]
        codes, _ = read_cog(lake, catalog.asset_rel(src, "fuel_model"))
        tl, _ = read_cog(lake, catalog.asset_rel(item, "total_load"))
        for c in np.unique(codes[codes != -9999]):
            np.testing.assert_allclose(tl[codes == c], table[int(c)].total_load_tac * KG_M2_PER_TON_ACRE, rtol=1e-6)
    lf25 = next(i for i in items if i["properties"]["fuellake:landfire_version"] == "LF 2025")
    codes, _ = read_cog(lake, catalog.asset_rel(sources[lf25["properties"]["fuellake:derived_from"]["item_id"]],
                                                "fuel_model"))
    assert any(141 <= c <= 149 for c in np.unique(codes)), "expected shrub fuel models in the Santa Monica Mountains"
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))
