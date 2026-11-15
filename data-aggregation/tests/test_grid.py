# fuel-lake — tests for AOI target-grid construction and snapping
# Licensed under Apache-2.0.
from __future__ import annotations

from affine import Affine
from rasterio.warp import transform_bounds

from fuellake.grid import Grid, grid_from_bbox
from fuellake.registry import load_aoi

SMM_BBOX = (-118.95, 33.98, -118.50, 34.20)


def test_smm_grid_is_snapped_outward():
    aoi = load_aoi("smm")
    g = aoi.grid
    assert g.crs == "EPSG:32611" and g.epsg == 32611
    assert g.resolution == 30
    minx, miny, maxx, maxy = g.bounds()
    for v in (minx, miny, maxx, maxy):
        assert v % 30 == 0
    pminx, pminy, pmaxx, pmaxy = transform_bounds("EPSG:4326", "EPSG:32611", *SMM_BBOX, densify_pts=21)
    assert minx <= pminx and miny <= pminy and maxx >= pmaxx and maxy >= pmaxy
    assert pminx - minx < 30 and pmaxy <= maxy < pmaxy + 30
    assert g.shape == (837, 1398)
    assert g.transform == Affine(30, 0, 319860, 0, -30, 3786060)
    w, s, e, n = g.bounds_wgs84()
    assert w <= SMM_BBOX[0] and s <= SMM_BBOX[1] and e >= SMM_BBOX[2] and n >= SMM_BBOX[3]


def test_grid_helpers_roundtrip():
    g = grid_from_bbox((-118.80, 34.05, -118.78, 34.07), "EPSG:32611", 30)
    assert Grid.from_dict(g.to_dict()) == g
    assert g.same_as("EPSG:32611", list(g.transform), g.width, g.height)
    assert not g.same_as("EPSG:32610", list(g.transform), g.width, g.height)
    assert not g.same_as(g.crs, list(g.transform), g.width + 1, g.height)
    assert len(g.x_coords()) == g.width and g.y_coords()[0] > g.y_coords()[-1]
    proj = g.stac_proj()
    assert proj["proj:epsg"] == 32611 and proj["proj:shape"] == [g.height, g.width]


def test_grid_at_coarser_resolution():
    coarse = load_aoi("smm").grid_at(100)
    assert coarse.resolution == 100
    assert all(v % 100 == 0 for v in coarse.bounds())


# ---------------------------------------------------------------------------- snap origin and reference grids
def test_origin_snapping_puts_every_edge_on_the_origin_lattice():
    g = grid_from_bbox((-118.80, 34.05, -118.78, 34.07), "EPSG:32611", 10, origin=(7.0, 3.0))
    minx, miny, maxx, maxy = g.bounds()
    edges = ((minx, 7), (maxx, 7), (miny, 3), (maxy, 3))
    assert all(abs(((v - o) / 10) - round((v - o) / 10)) < 1e-9 for v, o in edges)
    plain = grid_from_bbox((-118.80, 34.05, -118.78, 34.07), "EPSG:32611", 10)
    assert plain == grid_from_bbox((-118.80, 34.05, -118.78, 34.07), "EPSG:32611", 10, origin=(0.0, 0.0))


def test_ca_cell_edges_are_sme2_cell_edges():
    from fuellake.connectors import nisar_sme2 as sm
    from fuellake.grid import EASE2_200M_CELL, EASE2_UL_X, EASE2_UL_Y, REFERENCE_GRIDS

    assert sm.EASE2_200M_CELL is EASE2_200M_CELL and sm.EASE2_UL_X is EASE2_UL_X   # one definition, reused
    aoi = load_aoi("ca")
    assert aoi.reference_grid == "ease2-200m" and REFERENCE_GRIDS["ease2-200m"].crs == aoi.crs == "EPSG:6933"
    assert aoi.resolution == EASE2_200M_CELL and aoi.origin == (EASE2_UL_X, EASE2_UL_Y)
    g = aoi.grid
    col0 = (g.transform.c - EASE2_UL_X) / EASE2_200M_CELL
    row0 = (EASE2_UL_Y - g.transform.f) / EASE2_200M_CELL
    assert abs(col0 - round(col0)) < 1e-9 and abs(row0 - round(row0)) < 1e-9
    col0, row0 = round(col0), round(row0)
    # pixel centres are exactly the SME2 cell centres (global column/row indices of the SME2 product)
    for k in (0, g.width // 2, g.width - 1):
        assert abs(g.x_coords()[k] - float(sm.ease2_centre(col0 + k, 0)[0])) < 1e-6
    for j in (0, g.height // 2, g.height - 1):
        assert abs(g.y_coords()[j] - float(sm.ease2_centre(0, row0 + j)[1])) < 1e-6
    cols, rows = sm.ease2_index(g.x_coords()[[0, -1]], g.y_coords()[[0, -1]])
    assert cols.tolist() == [col0, col0 + g.width - 1] and rows.tolist() == [row0, row0 + g.height - 1]
    assert g.shape == (4827, 4956)                                     # 23.9 M cells, 10.2 M inside California
    w, s, e, n = g.bounds_wgs84()
    assert w <= -124.41 and s <= 32.53 and e >= -114.13 and n >= 42.01
    # a coarser cube on the same AOI stays on the EASE-Grid 2.0 lattice (5 cells = the 1 km grid)
    coarse = aoi.grid_at(5 * EASE2_200M_CELL)
    c = (coarse.transform.c - EASE2_UL_X) / (5 * EASE2_200M_CELL)
    assert abs(c - round(c)) < 1e-9


def test_reference_grid_rules():
    import pytest

    from fuellake.registry import AOI

    base = {"id": "x", "name": "x", "bbox": (-118.80, 34.05, -118.78, 34.07)}
    assert AOI(**base, reference_grid="ease2-200m", crs="EPSG:6933").crs == "EPSG:6933"
    with pytest.raises(ValueError, match="contradicts reference_grid"):
        AOI(**base, reference_grid="ease2-200m", crs="EPSG:3310")
    with pytest.raises(ValueError, match="contradicts reference_grid"):
        AOI(**base, reference_grid="ease2-200m", resolution=200.179)          # rounded: leave it out
    with pytest.raises(ValueError, match="unknown reference_grid"):
        AOI(**base, reference_grid="ease2-9km")
