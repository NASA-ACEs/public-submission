# fuel-lake — tests for the LANDFIRE EVT connector (code -> class rule, class shares, blocks, offline pipeline)
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
from fuellake.connectors.landfire_evt import (
    CLASSES,
    LandfireEvtConnector,
    blocks,
    class_lut,
    class_shares,
    class_table,
    classify,
    parse_attribute_table,
)
from fuellake.contracts import validate_collection
from fuellake.grid import Grid
from fuellake.rasters import read_cog
from fuellake.registry import load_aoi, load_dataset
from landfire_fakes import EVT_CSV, FakeLandfire

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
DS = "landfire-evt"
PERIOD = Period(date(2026, 6, 1), date(2026, 6, 2))
LF25 = (EVT_CSV / "LF2025_EVT.csv").read_text(encoding="utf-8-sig")
LF16 = (EVT_CSV / "LF16_EVT_200.csv").read_text(encoding="utf-8-sig")
COARSE_AOI = {**TINY_AOI, "id": "coarse", "name": "Coarse test AOI", "bbox": [-118.80, 34.05, -118.70, 34.12],
              "crs": "EPSG:6933", "resolution": 200}


@pytest.fixture
def evt_config(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    for aoi in (TINY_AOI, COARSE_AOI):
        (root / "aois" / f"{aoi['id']}.yaml").write_text(yaml.safe_dump(aoi))
    shutil.copy(REPO_CONFIG / "datasets" / f"{DS}.yaml", root / "datasets" / f"{DS}.yaml")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


# ---------------------------------------------------------------------------- code -> class
@pytest.mark.parametrize("code, expected", [
    (7110, "chaparral"), (7097, "chaparral"), (7092, "coastal_scrub"), (7064, "sagebrush"), (7065, "desert_scrub"),
    (4401, "other_shrub"), (7113, "oak_woodland"), (4463, "other_hardwood"), (7024, "conifer_oak"),
    (7020, "dry_conifer"), (7014, "mesic_conifer"), (7016, "juniper_pinyon"), (9301, "grass"), (7129, "grass"),
    (4402, "riparian"), (7292, "sparse"), (7754, "agriculture"), (7299, "developed"), (7296, "developed"),
])
def test_class_of_real_attribute_rows(code, expected):
    """Rows of the official LF2025_EVT.csv; every one of the 16 classes is reached."""
    assert class_table([LF25])[code]["class"] == expected


def test_every_class_is_reached_and_the_order_of_rules_matters():
    assert {r["class"] for r in class_table([LF25]).values()} == set(CLASSES)
    assert classify("Chaparral", "Shrubland", "Shrub") == "chaparral"
    assert classify("Chaparral", "Developed-Roads", "Shrub") == "developed"          # developed before lifeform
    assert classify("Western Riparian Woodland and Shrubland", "Riparian", "Tree") == "riparian"
    assert classify("Introduced Annual Grassland", "Exotic Herbaceous", "Herb") == "grass"
    assert classify("Pinyon-Juniper Woodland", "Conifer", "Tree") == "juniper_pinyon"
    assert classify("Ponderosa Pine Forest, Woodland and Savanna", "Conifer", "Tree") == "dry_conifer"
    assert classify("Douglas-fir Forest and Woodland", "Conifer", "Tree") == "mesic_conifer"
    assert classify(" Creosotebush Desert Scrub ", " Shrubland ", "Shrub") == "desert_scrub"   # blanks ignored


def test_first_table_that_lists_a_code_decides():
    assert -9999 not in parse_attribute_table(LF25)
    only16 = set(parse_attribute_table(LF16)) - set(parse_attribute_table(LF25))
    assert only16 == {7940, 7945}
    both = class_table([LF25, LF16])
    assert set(both) == set(parse_attribute_table(LF25)) | only16
    assert both[7945]["class"] == "developed"                                         # completed from LF 2016
    # code 7300: LF 2025 says physiognomy "Developed-Open Space", LF 2016 "Developed"; the first table is kept
    assert both[7300]["physiognomy"] == "Developed-Open Space"
    assert class_table([LF16, LF25])[7300]["physiognomy"] == "Developed"
    lut = class_lut(both)
    assert lut[7110] == CLASSES.index("chaparral") and lut[7001] == -1 and lut.dtype == np.int8
    with pytest.raises(ValueError):
        parse_attribute_table("VALUE,EVT_NAME,EVT_LF,EVT_PHYS,EVT_GP_N\n-9999,Fill-NoData,NA,NA,NA\n")


# ---------------------------------------------------------------------------- shares
def _source(codes):
    return np.asarray(codes, dtype="int16"), "EPSG:5070", Affine(30, 0, -2000010.0, 0, -30, 1500000.0)


def test_shares_of_a_coarse_cell_count_valid_pixels_only():
    lut = class_lut(class_table([LF25, LF16]))
    codes = np.full((8, 8), 7110, dtype="int16")            # 2 x 2 cells of 120 m, 16 pixels each
    codes[:4, :4][:1] = 9301                                # NW cell: 4 of 16 grass
    codes[:4, 4:] = -9999                                   # NE cell: no data
    codes[4:, :4][:2] = -9999                               # SW cell: 8 nodata, 8 chaparral
    codes[4:, 4:][:, :1] = 7001                             # SE cell: 4 pixels of a code no table lists
    codes[4:, 4:][:, 1:2] = 7299                            #          4 developed, 8 chaparral
    arr, crs, tr = _source(codes)
    grid = Grid(crs=crs, transform=Affine(120, 0, tr.c, 0, -120, tr.f), width=2, height=2, resolution=120.0)
    shares, facts = class_shares(arr, crs, tr, grid, lut, len(CLASSES))
    chap, grass, dev = (shares[CLASSES.index(c)] for c in ("chaparral", "grass", "developed"))
    assert shares.shape == (16, 2, 2) and shares.dtype == np.float32 and facts["resampling"] == "average"
    np.testing.assert_allclose(chap, [[0.75, np.nan], [1.0, 2 / 3]], rtol=1e-6)
    np.testing.assert_allclose(grass, [[0.25, np.nan], [0.0, 0.0]], rtol=1e-6)
    np.testing.assert_allclose(dev, [[0.0, np.nan], [0.0, 1 / 3]], rtol=1e-6)
    np.testing.assert_allclose(np.nansum(shares, axis=0), [[1, 0], [1, 1]], rtol=1e-6)
    assert facts["unlisted"].tolist() == [7001] and (facts["unlisted_pixels"], facts["data_pixels"]) == (4, 40)

    stepped, _ = class_shares(arr, crs, tr, grid, lut, len(CLASSES), share_steps=200)
    assert stepped[CLASSES.index("chaparral")][1, 1] == np.float32(0.665)            # 2/3 in steps of 1/200
    assert np.allclose(np.round(stepped[np.isfinite(stepped)] * 200), stepped[np.isfinite(stepped)] * 200, atol=1e-4)


def test_a_grid_as_fine_as_the_source_gets_0_1_layers():
    lut = class_lut(class_table([LF25, LF16]))
    arr, crs, tr = _source([[7110, 9301, -9999], [7299, 7001, 7110]])
    grid = Grid(crs=crs, transform=tr, width=3, height=2, resolution=30.0)
    shares, facts = class_shares(arr, crs, tr, grid, lut, len(CLASSES), share_steps=200)
    assert facts["resampling"] == "nearest" and facts["unlisted"].tolist() == [7001]
    np.testing.assert_array_equal(shares[CLASSES.index("chaparral")], [[1, 0, np.nan], [0, np.nan, 1]])
    np.testing.assert_array_equal(shares[CLASSES.index("grass")], [[0, 1, np.nan], [0, np.nan, 0]])
    assert set(np.unique(shares[np.isfinite(shares)]).tolist()) == {0.0, 1.0}


def test_blocks():
    fine = Grid(crs="EPSG:32611", transform=Affine(30, 0, 0, 0, -30, 0), width=900, height=700, resolution=30.0)
    assert blocks(fine, 512) == [(0, 0, fine)]
    coarse = Grid(crs="EPSG:6933", transform=Affine(200, 0, 1000, 0, -200, 9000), width=1100, height=600,
                  resolution=200.0)
    got = blocks(coarse, 512)
    assert [(r, c, g.height, g.width) for r, c, g in got] == [
        (0, 0, 512, 512), (0, 512, 512, 512), (0, 1024, 512, 76), (512, 0, 88, 512), (512, 512, 88, 512),
        (512, 1024, 88, 76)]
    assert got[4][2].transform == Affine(200, 0, 1000 + 512 * 200, 0, -200, 9000 - 512 * 200)


# ---------------------------------------------------------------------------- pipeline, offline
def test_repo_config():
    ds = load_dataset(DS)
    assert [v.name for v in ds.variables] == [f"evt_share_{c}" for c in CLASSES]
    assert set(ds.scales) == {"local", "region"} and ds.options["share_steps"] == 200
    assert [r["valid_from"] for r in ds.options["releases"]] == ["2017-01-01", "2022-10-01", "2025-10-01"]
    assert [Path(u).name for u in ds.options["attribute_tables"]] == ["LF2025_EVT.csv", "LF16_EVT_200.csv"]


def test_pipeline_offline_on_a_30m_aoi(lake, evt_config, monkeypatch):
    fake = FakeLandfire().patch(monkeypatch, LandfireEvtConnector)
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", [u.error for u in d.units]
    items = {it["id"]: it for it in catalog.iter_items(lake, DS)}
    assert sorted(items) == ["landfire-evt__tiny__20160101__lf2016", "landfire-evt__tiny__20220101__lf2022",
                             "landfire-evt__tiny__20250101__lf2025"]
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))
    assert fake.calls_to("/exportImage") == 3                    # one clip per release

    it = items["landfire-evt__tiny__20250101__lf2025"]
    p = it["properties"]
    assert p["fuellake:source_key"] == "landfire:Landfire_LF2025/LF2025_EVT_CONUS"
    assert (p["fuellake:landfire_version"], p["fuellake:valid_from"]) == ("LF 2025", "2025-10-01")
    assert p["fuellake:resampling"] == "nearest" and p["fuellake:share_steps"] is None
    assert p["fuellake:classes"] == list(CLASSES) and p["fuellake:unmapped_codes"] == [7001]
    assert 0 < p["fuellake:qc_masked_fraction"] < 0.2
    assert [Path(u).name for u in p["fuellake:landfire_files"]["attribute_tables"]] == ["LF2025_EVT.csv",
                                                                                        "LF16_EVT_200.csv"]
    assert items["landfire-evt__tiny__20160101__lf2016"]["properties"]["fuellake:valid_from"] == "2017-01-01"
    assert it["assets"]["classes"]["roles"] == ["metadata"]
    assert "7945,Western Warm Temperate Developed Ruderal Deciduous Forested Wetland" in lake.read_text(
        catalog.asset_rel(it, "classes"))
    grid = load_aoi("tiny").grid
    layers = np.stack([read_cog(lake, catalog.asset_rel(it, f"evt_share_{c}"))[0] for c in CLASSES])
    assert layers.shape == (16, *grid.shape) and layers.dtype == np.float32
    valid = np.isfinite(layers[0])
    assert 0.5 < valid.mean() < 1 and (np.isfinite(layers) == valid).all()
    assert set(np.unique(layers[:, valid]).tolist()) == {0.0, 1.0} and (layers[:, valid].sum(axis=0) == 1).all()
    for c in ("chaparral", "coastal_scrub", "grass", "developed", "oak_woodland", "sparse"):
        assert layers[CLASSES.index(c)][valid].any(), c
    assert not layers[CLASSES.index("sagebrush")][valid].any()

    rep2 = pipeline.run([DS], "tiny", Period(date(2026, 7, 1), date(2026, 7, 2)), lake=lake)
    assert (rep2.datasets[0].skipped, len(rep2.datasets[0].units)) == (3, 0)


def _one_release(evt_config, **options):
    path = evt_config / "datasets" / f"{DS}.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["options"] = {**cfg["options"], "releases": cfg["options"]["releases"][-1:], **options}
    path.write_text(yaml.safe_dump(cfg))


def test_pipeline_offline_blocks_of_a_coarse_aoi_give_the_same_shares_as_one_clip(lake, evt_config, tmp_path,
                                                                                    monkeypatch):
    fake = FakeLandfire().patch(monkeypatch, LandfireEvtConnector)
    grid = load_aoi("coarse").grid
    assert grid.resolution == 200 and min(grid.shape) > 32

    def run(block_cells, root):
        _one_release(evt_config, block_cells=block_cells)
        rep = pipeline.run([DS], "coarse", PERIOD, lake=root)
        assert rep.datasets[0].status == "ok", [u.error for u in rep.datasets[0].units]
        (it,) = catalog.iter_items(root, DS)
        return it, np.stack([read_cog(root, catalog.asset_rel(it, f"evt_share_{c}"))[0] for c in CLASSES])

    it, tiled = run(16, lake)
    n_blocks = len(blocks(grid, 16))
    assert n_blocks > 4 and fake.calls_to("/exportImage") == n_blocks
    p = it["properties"]
    assert p["fuellake:resampling"] == "average" and p["fuellake:share_steps"] == 200
    valid = np.isfinite(tiled[0])
    assert valid.mean() > 0.9 and (np.isfinite(tiled) == valid).all()
    assert np.abs(tiled[:, valid].sum(axis=0) - 1).max() < 0.03                    # 16 shares, each rounded to 1/200
    assert np.abs(np.round(tiled[:, valid] * 200) - tiled[:, valid] * 200).max() < 1e-3
    assert 0 < tiled[CLASSES.index("chaparral")][valid].mean() < 0.5

    # One clip instead of blocks: the same shares, up to GDAL's approximation of the transform along the rows
    # of the piece it warps (a share moves by a few rounding steps in cells on class edges; the fake is all edges).
    from fuellake.storage import Lake
    _, whole = run(10_000, Lake(tmp_path / "lake-one-clip"))
    assert (np.isfinite(whole[0]) != valid).mean() < 0.01
    both = valid & np.isfinite(whole[0])
    diff = np.abs(tiled[:, both] - whole[:, both])
    assert diff.max() <= 0.0201 and (diff > 0).mean() < 0.05, (diff.max(), (diff > 0).mean())

    # a rerun with force finds the raw clips in the lake and requests none again
    before = fake.calls_to("/exportImage")
    _one_release(evt_config, block_cells=16)
    rep = pipeline.run([DS], "coarse", PERIOD, lake=lake, force=True)
    assert rep.datasets[0].status == "ok" and fake.calls_to("/exportImage") == before


def test_bad_options_are_refused(lake, evt_config):
    from fuellake.config import Settings

    settings = Settings.from_env()
    path = evt_config / "datasets" / f"{DS}.yaml"
    cfg = yaml.safe_load(path.read_text())

    def connector(**options):
        path.write_text(yaml.safe_dump({**cfg, "options": {**cfg["options"], **options}}))
        return LandfireEvtConnector(load_dataset(DS), lake, settings)

    with pytest.raises(ValueError, match="attribute_tables"):
        connector(attribute_tables=[]).releases()
    with pytest.raises(ValueError, match="distinct"):
        connector(attribute_tables=["https://a/x.csv", "https://b/x.csv"]).tables()
    with pytest.raises(ValueError, match="releases"):
        connector(releases=[]).releases()
