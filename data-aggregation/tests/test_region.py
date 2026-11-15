# fuel-lake — tests for the monitored region: shipped California boundary, AOI checks, clipping, validation, scales
# Licensed under Apache-2.0.
from __future__ import annotations

import io
import json
import math
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
import shapely
import yaml
from shapely.geometry import box
from typer.testing import CliRunner

from conftest import TEST_REGION_CUT_LON, TINY_AOI, write_test_region
from fuellake import catalog, pipeline
from fuellake.cli import app
from fuellake.connectors import fems
from fuellake.connectors._lfmc_common import RawSample, collapse_samples
from fuellake.connectors.base import Period
from fuellake.contracts import validate_collection
from fuellake.rasters import open_zarr, read_cog
from fuellake.region import OutsideRegionError, clip_table
from fuellake.registry import lake_region, load_aoi, load_dataset, load_lake_config
from fuellake.tables import write_geoparquet

REPO = Path(__file__).resolve().parents[1]
PERIOD = Period(date(2026, 6, 1), date(2026, 6, 30))
runner = CliRunner()


# ---------------------------------------------------------------------------- shipped California region
def test_shipped_config_declares_california():
    assert load_lake_config().region == "california"
    r = lake_region()
    assert r.id == "california" and r.name == "State of California" and r.tolerance_m == 1000
    assert r.geometry.geom_type == "MultiPolygon" and r.geometry.is_valid and len(r.geometry.geoms) == 15
    assert shapely.get_num_coordinates(r.geometry) == 5134
    assert r.bbox == pytest.approx((-124.409591, 32.534435, -114.131211, 42.009485), abs=1e-6)
    src = r.meta["source"]
    assert src["url"] == "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_500k.zip"
    assert src["vintage"] == 2025 and "STATEFP 06" in src["feature"]
    assert r.meta["license"]["redistributable"] is True and "Census" in r.meta["license"]["attribution"]
    area = shapely.area(r.in_crs("EPSG:3310")) / 1e6
    assert area == pytest.approx(409_819, rel=0.005)          # ALAND + coastal parts of the 1:500k outline
    gj = json.loads((REPO / "config" / "regions" / "california.geojson").read_text())
    assert gj["type"] == "FeatureCollection" and gj["features"][0]["properties"]["GEOID"] == "06"


def test_points_rows_and_bboxes_against_california():
    r = lake_region()
    places = {"san_francisco": (-122.4194, 37.7749), "sacramento": (-121.4944, 38.5816),
              "yosemite": (-119.5383, 37.8651), "reno": (-119.8138, 39.5296), "las_vegas": (-115.1398, 36.1699),
              "phoenix": (-112.0740, 33.4484), "tijuana": (-117.0382, 32.5149), "portland": (-122.6765, 45.5231)}
    inside = r.contains_points([p[0] for p in places.values()], [p[1] for p in places.values()])
    assert dict(zip(places, inside.tolist(), strict=True)) == {
        "san_francisco": True, "sacramento": True, "yosemite": True, "reno": False, "las_vegas": False,
        "phoenix": False, "tijuana": False, "portland": False}
    assert not r.contains_points([np.nan], [37.0])[0]
    # the westmost vertex of the outline (Cape Mendocino): 500 m offshore counts (tolerance), 5 km does not
    coords = shapely.get_coordinates(r.geometry)
    lon, lat = coords[np.argmin(coords[:, 0])]
    dlon = 1 / (111_320 * math.cos(math.radians(lat)))
    assert r.contains_points([lon - 500 * dlon], [lat])[0]
    assert not r.contains_points([lon - 500 * dlon], [lat], tolerance_m=0)[0]
    assert not r.contains_points([lon - 5000 * dlon], [lat])[0]
    # polygons: kept when they intersect (a fire crossing the state line), dropped when wholly outside
    tahoe_cross = box(-120.10, 39.10, -119.90, 39.20)          # straddles the CA-NV line at -120.0
    nevada = box(-119.85, 39.45, -119.75, 39.55)
    assert r.rows_inside([tahoe_cross, nevada, None]).tolist() == [True, False, False]
    # bboxes
    assert r.bbox_problem(load_aoi("smm").bbox) is None and r.bbox_problem(load_aoi("ca").bbox) is None
    assert "does not intersect" in r.bbox_problem((-119.9, 39.4, -119.7, 39.6))
    assert "extends beyond" in r.bbox_problem((-126.0, 37.0, -122.0, 38.0))
    assert r.clamp_bbox((-126.0, 30.0, -110.0, 45.0)) == r.bbox


def test_ca_and_smm_grids():
    from fuellake.grid import EASE2_200M_CELL

    r = lake_region()
    ca = load_aoi("ca")
    assert (ca.crs, ca.resolution, ca.scale) == ("EPSG:6933", EASE2_200M_CELL, "region")   # the NISAR SME2 grid
    g = ca.grid
    gx0, gy0, gx1, gy1 = g.bounds()
    rx0, ry0, rx1, ry1 = r.in_crs("EPSG:6933").bounds
    assert gx0 <= rx0 and gy0 <= ry0 and gx1 >= rx1 and gy1 >= ry1          # the grid covers the region
    mask = r.pixel_mask(g)
    assert mask.sum() == pytest.approx(10_227_225, rel=0.001)                # SME2 cells inside the state
    assert mask.sum() * EASE2_200M_CELL ** 2 / 1e6 == pytest.approx(409_819, rel=0.002)   # equal-area grid
    smm = load_aoi("smm")
    assert smm.scale == "local"
    frac = r.pixel_mask(smm.grid).mean()
    assert 0.6 < frac < 0.9                                                  # the Pacific part is outside


def test_fems_sites_outside_the_region_are_not_selected():
    sites = [{"site_id": "1", "lon": -120.05, "lat": 39.0}, {"site_id": "2", "lon": -119.9, "lat": 39.0},
             {"site_id": "3", "lon": -122.4, "lat": 37.77}]
    tahoe = (-120.2, 38.9, -119.8, 39.3)
    assert [s["site_id"] for s in fems.select_sites(sites, tahoe, 50)] == ["1", "2"]
    assert [s["site_id"] for s in fems.select_sites(sites, tahoe, 50, lake_region())] == ["1"]   # 2 is in Nevada


# ---------------------------------------------------------------------------- AOI checks
@pytest.fixture
def real_config_copy(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config-copy"
    shutil.copytree(REPO / "config", root)
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


def test_aoi_outside_the_region_is_an_error(real_config_copy, lake):
    reno = dict(TINY_AOI, id="reno", name="Reno, Nevada", bbox=[-119.90, 39.45, -119.70, 39.60])
    (real_config_copy / "aois" / "reno.yaml").write_text(yaml.safe_dump(reno))
    tahoe = dict(TINY_AOI, id="tahoe", name="Lake Tahoe (both states)", bbox=[-120.25, 38.85, -119.85, 39.30])
    (real_config_copy / "aois" / "tahoe.yaml").write_text(yaml.safe_dump(tahoe))
    with pytest.raises(OutsideRegionError, match="lies outside the lake region 'california'"):
        load_aoi("reno")
    assert load_aoi("reno", check_region=False).id == "reno"
    assert load_aoi("tahoe").id == "tahoe"                    # straddles the line: Nevada pixels are nodata
    with pytest.raises(OutsideRegionError):
        pipeline.run(["fems-lfmc"], "reno", PERIOD, lake=lake)
    res = runner.invoke(app, ["run", "fems-lfmc", "--aoi", "reno", "--start", "2026-06-01", "--end", "2026-06-02"])
    assert res.exit_code == 1 and "Traceback" not in res.output and "lies outside" in res.output
    assert not lake.exists("catalog/fems-lfmc")


def test_config_without_lake_yaml_has_no_region(test_config):
    assert load_lake_config().region is None and lake_region() is None
    assert load_aoi("tiny").id == "tiny"


# ---------------------------------------------------------------------------- central clipping
@pytest.mark.parametrize("mode", ["local", "s3"])
def test_pipeline_clips_every_output_to_the_region(region_config, mode, request):
    lake = request.getfixturevalue("s3_lake" if mode == "s3" else "lake")
    rep = pipeline.run(["synthetic-raster", "synthetic-table", "synthetic-weather"], "tiny", PERIOD, lake=lake)
    assert rep.ok, rep.to_dict()
    assert rep.region == "testland"
    region = lake_region()
    aoi = load_aoi("tiny")

    raster = catalog.get_item(lake, "synthetic-raster", "synthetic-raster__tiny__20260601")
    arr, grid = read_cog(lake, catalog.asset_rel(raster, "soil_moisture"))
    inside = region.pixel_mask(grid)
    assert 0 < inside.mean() < 1                               # the region edge cuts through the AOI
    assert np.isnan(arr[~inside]).all() and np.isfinite(arr[inside]).any()
    xs = grid.x_coords()
    from pyproj import Transformer

    lon_cut = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True).transform(TEST_REGION_CUT_LON, 34.06)[0]
    assert np.isnan(arr[:, xs < lon_cut - 60]).all()           # west of the cut (2 pixels of margin)
    props = raster["properties"]
    assert props["fuellake:region"] == "testland" and props["fuellake:region_clip"]["masked_pixels"] > 0
    assert region.bbox_problem(raster["bbox"]) is None

    table = catalog.get_item(lake, "synthetic-table", "synthetic-table__tiny__20260601")
    rel = catalog.asset_rel(table, "table")
    df = pq.read_table(io.BytesIO(lake.read_bytes(rel))).to_pandas()
    assert sorted(df["site_id"]) == ["S2", "S3"]               # S1 lies west of the cut
    assert table["properties"]["fuellake:n_rows"] == 2
    assert table["properties"]["fuellake:region_clip"] == {"masked_pixels": 0, "dropped_rows": 1}
    w, s, e, n = table["bbox"]
    assert w == pytest.approx(df["lon"].min()) and e == pytest.approx(df["lon"].max())

    weather = catalog.get_item(lake, "synthetic-weather", "synthetic-weather__tiny__20260601")
    ds = open_zarr(lake, catalog.asset_rel(weather, "data"))
    t2m = ds["t2m"].values
    assert np.isnan(t2m[:, :, ds["x"].values > -118.64]).all()      # cells east of the region (x = -118.6)
    assert np.isfinite(t2m).any() and ds.rio.crs.to_epsg() == 4326
    assert weather["properties"]["fuellake:region_clip"]["masked_pixels"] > 0

    for ds_id in ("synthetic-raster", "synthetic-table", "synthetic-weather"):
        results = validate_collection(lake, load_dataset(ds_id))
        assert all(r.ok for r in results), [r.to_dict() for r in results if not r.ok]
    assert aoi.id == "tiny"


def test_validate_flags_outputs_outside_the_region(test_config, lake):
    rep = pipeline.run(["synthetic-raster", "synthetic-table", "synthetic-weather"], "tiny", PERIOD, lake=lake)
    assert rep.ok                                              # no region yet: nothing clipped
    write_test_region(test_config)
    errors = []
    for ds_id in ("synthetic-raster", "synthetic-table", "synthetic-weather"):
        errors += [e for r in validate_collection(lake, load_dataset(ds_id)) for e in r.errors]
    assert any("valid pixels lie outside the testland region" in e for e in errors)
    assert any("1 rows lie outside the testland region" in e for e in errors)
    assert any("finite values lie outside the testland region" in e for e in errors)
    res = runner.invoke(app, ["validate", "synthetic-raster"])
    assert res.exit_code == 1 and "outside the testland region" in res.output


def test_table_clip_keeps_arrow_types(region_config, lake):
    rows = [RawSample(site_id=f"S{i}", site_name=None, lon=lon, lat=34.06, date=date(2026, 6, 1), species="chamise",
                      value=90.0) for i, lon in enumerate((-118.795, -118.785))]
    df = collapse_samples(rows, "globe-lfmc")                  # status is all null, like Globe-LFMC
    rel = "staged/tables/lfmc_samples/x/part-a.parquet"
    write_geoparquet(lake, rel, df)
    before = pq.read_schema(io.BytesIO(lake.read_bytes(rel)))
    dropped, bounds = clip_table(lake, rel, lake_region())
    after = pq.read_table(io.BytesIO(lake.read_bytes(rel)))
    assert dropped == 1 and after.num_rows == 1 and bounds[0] == pytest.approx(-118.785)
    assert after.schema.types == before.types and after.schema.field("status").type == before.field("status").type
    geo = json.loads(after.schema.metadata[b"geo"])
    assert geo["primary_column"] == "geometry"


# ---------------------------------------------------------------------------- AOI scales
@pytest.fixture
def wide_aoi(test_config) -> str:
    (test_config / "aois" / "wide.yaml").write_text(yaml.safe_dump(dict(TINY_AOI, id="wide", scale="region")))
    return "wide"


def test_local_only_datasets_are_refused_on_region_aois(wide_aoi, lake):
    assert load_dataset("synthetic-raster").scales == ["local"]          # the default
    rep = pipeline.run(["synthetic-raster"], wide_aoi, PERIOD, lake=lake)
    d = rep.datasets[0]
    assert not rep.ok and d.status == "failed" and d.error.startswith("refused: synthetic-raster supports")
    rep = pipeline.run(["synthetic-raster"], wide_aoi, PERIOD, lake=lake, skip_stubs=True,
                       skip_missing_credentials=True)
    assert rep.ok and rep.datasets[0].status == "skipped" and "--force-scale" in rep.datasets[0].reason
    rep = pipeline.run(["synthetic-raster"], wide_aoi, PERIOD, lake=lake, force_scale=True)
    assert rep.ok and rep.datasets[0].count("created") == 1
    period = ["--aoi", wide_aoi, "--start", "2026-06-01", "--end", "2026-06-02"]
    res = runner.invoke(app, ["run", "synthetic-table", *period])
    assert res.exit_code == 1 and "refused" in res.output
    res = runner.invoke(app, ["run", "synthetic-table", "--force-scale", *period])
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["discover", "all", *period])
    assert "synthetic-raster: skipped: synthetic-raster supports AOI scales ['local']" in res.output


def test_shipped_scales():
    local_only = {"nisar-gcov", "opera-rtc-s1", "sentinel2-indices", "cop-dem", "landfire-fbfm40"}
    region = {"fems-lfmc", "globe-lfmc", "gridmet", "era5-land-daily", "firms", "fire-perimeters", "nisar-sme2"}
    for ds_id in local_only:
        assert load_dataset(ds_id).scales == ["local"], ds_id
    for ds_id in region:
        assert "region" in load_dataset(ds_id).scales, ds_id
    assert load_dataset("globe-lfmc").scales == ["region"]
    for ds_id in ("firms", "fire-perimeters", "fems-lfmc", "gridmet"):
        assert "local" in load_dataset(ds_id).scales
