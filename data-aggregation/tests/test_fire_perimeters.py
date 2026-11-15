# fuel-lake — tests for the fire perimeters connector (FRAP / WFIGS fixtures, polygon GeoParquet, live ArcGIS)
# Licensed under Apache-2.0.
from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from shapely.geometry import MultiPolygon, Polygon

from fuellake import catalog, pipeline
from fuellake.config import Settings
from fuellake.connectors.base import Period
from fuellake.connectors.fire_perimeters import (
    PERIMETER_SCHEMA,
    FirePerimetersConnector,
    buffer_bbox_m,
    features_to_frame,
    polygonal,
)
from fuellake.contracts import validate_collection, validate_table
from fuellake.registry import load_aoi, load_dataset
from fuellake.storage import Lake
from fuellake.tables import read_table, write_geoparquet

FIX = Path(__file__).parent / "fixtures" / "weather_fire"
FRAP_FC = json.loads((FIX / "frap_2018_sample.geojson").read_text())
WFIGS_FC = json.loads((FIX / "wfigs_2026_sample.geojson").read_text())
MS_2026_05_21 = 1779384987029   # FRAP dataLastEditDate seen live (2026-05-21)
MS_2026_11_14 = 1794665129837


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.payload, self.status_code = payload, status

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeArcGIS:
    """FRAP (max year 2025, features for 2018 only) and WFIGS (one 2026 feature) layer endpoints."""

    def __init__(self, frap_ms: int = MS_2026_05_21):
        self.frap_ms = frap_ms
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params))
        frap = "California_Historic_Fire_Perimeters" in url
        fc = FRAP_FC if frap else WFIGS_FC
        if not url.endswith("/query"):
            return FakeResponse({"editingInfo": {"dataLastEditDate": self.frap_ms if frap else MS_2026_11_14}})
        if "outStatistics" in params:
            return FakeResponse({"features": [{"attributes": {"maxy": 2025}}]})
        if params.get("returnIdsOnly") == "true":
            where = params["where"]
            hit = ("YEAR_ = 2018" in where) if frap else ("2026-01-01" in where)
            return FakeResponse({"objectIds": [f["id"] for f in fc["features"]] if hit else []})
        ids = {int(i) for i in params["objectIds"].split(",")}
        return FakeResponse({"type": "FeatureCollection", "features": [f for f in fc["features"] if f["id"] in ids]})


def test_frap_rows_are_normalised():
    gdf = features_to_frame(FRAP_FC["features"], "frap")
    assert list(gdf.columns) == list(PERIMETER_SCHEMA) + ["geometry"]
    w = gdf[gdf.fire_name == "WOOLSEY"].iloc[0]
    assert w.event_id == "b13d8c7f-2b0b-4ba5-960c-cc4767139b06" == w.irwin_id   # braces stripped, lower case
    assert w.start_date == date(2018, 11, 8) and w.containment_date == date(2018, 11, 20)
    assert w.fire_year == 2018 and w.source == "cal-fire-frap" and w.cause == "11"
    assert w.area_acres == pytest.approx(89551.38) and w.area_ha == pytest.approx(89551.38 * 0.40468564224)
    assert -119.0 < w.centroid_lon < -118.6 and 34.0 < w.centroid_lat < 34.25
    b = gdf[gdf.fire_name == "SYNTHETIC BOWTIE"].iloc[0]
    assert b.event_id == "frap:2018:SMP:synthetic bowtie"     # no IRWIN id, no incident number
    assert b.start_date is None or str(b.start_date) in ("<NA>", "NaT")
    assert gdf.geometry.is_valid.all()                         # bow-tie and simplified Woolsey repaired
    assert set(gdf.geometry.geom_type) <= {"Polygon", "MultiPolygon"}
    assert gdf.perimeter_id.is_unique


def test_wfigs_rows_are_normalised():
    gdf = features_to_frame(WFIGS_FC["features"], "wfigs")
    r = gdf.iloc[0]
    assert r.source == "nifc-wfigs" and r.fire_name == "Bryson" and r.fire_year == 2026
    assert r.event_id == "11111111-2222-3333-4444-555555555555"
    assert r.feature_category == "Wildfire Daily Fire Perimeter"
    assert r.start_date == date(2026, 10, 15)
    assert str(r.area_acres) == "nan"                          # source null kept as missing, not 0


def test_polygonal_drops_non_polygons():
    from shapely.geometry import GeometryCollection, LineString, Point

    assert polygonal(LineString([(0, 0), (1, 1)])) is None
    gc = GeometryCollection([Point(0, 0), Polygon([(0, 0), (1, 0), (1, 1)])])
    assert isinstance(polygonal(gc), MultiPolygon)
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
    fixed = polygonal(bowtie)
    assert fixed.is_valid and fixed.area == pytest.approx(0.5)


def test_buffer_bbox_m():
    w, s, e, n = buffer_bbox_m((-118.95, 33.98, -118.50, 34.20), 5000)
    assert s == pytest.approx(33.98 - 5000 / 111320)
    assert w < -118.95 - 5000 / 111320      # longitude degrees are shorter at 34 N


def test_polygon_geoparquet_round_trip(lake):
    gdf = features_to_frame(FRAP_FC["features"], "frap")
    rel = "staged/tables/fire_perimeters/fire-perimeters/part-test.parquet"
    write_geoparquet(lake, rel, gdf)
    res = validate_table(lake, rel, "fire_perimeters")
    assert res.ok, res.errors
    meta = json.loads(pq.read_schema(io.BytesIO(lake.read_bytes(rel))).metadata[b"geo"])
    col = meta["columns"]["geometry"]
    assert col["encoding"] == "WKB" and set(col["geometry_types"]) <= {"Polygon", "MultiPolygon"}
    back = read_table(lake, "fire_perimeters", "fire-perimeters")
    assert back.crs.to_epsg() == 4326 and len(back) == len(gdf)
    for a, b in zip(gdf.sort_values("perimeter_id").geometry, back.sort_values("perimeter_id").geometry,
                    strict=True):
        assert a.equals(b)
    assert str(back.start_date.dtype).startswith("date32")


def test_zero_row_part_keeps_schema(lake):
    empty = features_to_frame([], "frap")
    full = features_to_frame(FRAP_FC["features"], "frap")
    write_geoparquet(lake, "staged/tables/fire_perimeters/fire-perimeters/part-empty.parquet", empty)
    write_geoparquet(lake, "staged/tables/fire_perimeters/fire-perimeters/part-full.parquet", full)
    res = validate_table(lake, "staged/tables/fire_perimeters/fire-perimeters/part-empty.parquet", "fire_perimeters")
    assert res.ok and res.details["n_rows"] == 0
    s_empty = pq.read_schema(io.BytesIO(lake.read_bytes(
        "staged/tables/fire_perimeters/fire-perimeters/part-empty.parquet")))
    s_full = pq.read_schema(io.BytesIO(lake.read_bytes(
        "staged/tables/fire_perimeters/fire-perimeters/part-full.parquet")))
    assert [(f.name, str(f.type)) for f in s_empty] == [(f.name, str(f.type)) for f in s_full]


def test_discover_picks_frap_then_wfigs(lake):
    conn = FirePerimetersConnector(load_dataset("fire-perimeters"), lake, Settings.from_env())
    fake = FakeArcGIS()
    conn.http_session = lambda bearer_env=None: fake
    refs = conn.discover(load_aoi("smm"), Period(date(2024, 6, 1), date(2026, 11, 14)))
    assert [r.key for r in refs] == ["frap-smm-2024-v20260521", "frap-smm-2025-v20260521",
                                     "wfigs-smm-2026-v20261114"]


def test_pipeline_offline_run_is_idempotent(lake, monkeypatch):
    fake = FakeArcGIS()
    monkeypatch.setattr(FirePerimetersConnector, "http_session", lambda self, bearer_env=None: fake)
    period = Period(date(2017, 1, 1), date(2018, 12, 31))
    rep = pipeline.run(["fire-perimeters"], "smm", period, lake=lake)
    assert rep.ok, rep.to_dict()
    items = {i["id"]: i for i in catalog.iter_items(lake, "fire-perimeters")}
    assert set(items) == {"fire-perimeters__smm__20170101__frap", "fire-perimeters__smm__20180101__frap"}
    assert items["fire-perimeters__smm__20170101__frap"]["properties"]["fuellake:n_rows"] == 0   # empty year kept
    assert items["fire-perimeters__smm__20180101__frap"]["properties"]["fuellake:n_rows"] == 2
    assert items["fire-perimeters__smm__20180101__frap"]["properties"]["fire_perimeters:largest_fire"] == "WOOLSEY"
    assert all(r.ok for r in validate_collection(lake, load_dataset("fire-perimeters")))
    n_calls = len(fake.calls)
    rep2 = pipeline.run(["fire-perimeters"], "smm", period, lake=lake)
    assert rep2.ok and rep2.counts()["skipped"] == 2 and rep2.counts()["created"] == 0
    assert not any(p.get("returnIdsOnly") for _, p in fake.calls[n_calls:])   # no feature download on rerun


def test_source_update_replaces_part_in_place(lake, monkeypatch):
    fake = FakeArcGIS()
    monkeypatch.setattr(FirePerimetersConnector, "http_session", lambda self, bearer_env=None: fake)
    period = Period(date(2018, 1, 1), date(2018, 12, 31))
    assert pipeline.run(["fire-perimeters"], "smm", period, lake=lake).ok
    fake.frap_ms = MS_2026_11_14          # FRAP layer edited: new source key, same item id and part path
    rep = pipeline.run(["fire-perimeters"], "smm", period, lake=lake)
    assert rep.ok and rep.counts()["updated"] == 1
    assert len(read_table(lake, "fire_perimeters", "fire-perimeters")) == 2      # no duplicate parts
    (item,) = catalog.iter_items(lake, "fire-perimeters")
    assert item["properties"]["fuellake:source_key"] == "frap-smm-2018-v20261114"


def test_region_filter_and_statewide_generalisation(lake, monkeypatch):
    """Features outside California are dropped before raw/; ca asks ArcGIS for ~50 m generalised geometry."""
    conn = FirePerimetersConnector(load_dataset("fire-perimeters"), lake, Settings.from_env())
    reno = {"type": "Feature", "id": 1, "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [[[-119.85, 39.45], [-119.75, 39.45], [-119.75, 39.55],
                                                             [-119.85, 39.45]]]}}
    tahoe = {"type": "Feature", "id": 2, "properties": {},
             "geometry": {"type": "Polygon", "coordinates": [[[-120.1, 39.1], [-119.9, 39.1], [-119.9, 39.2],
                                                              [-120.1, 39.1]]]}}
    kept, n_out = conn.region_features([reno, tahoe])
    assert n_out == 1 and [f["id"] for f in kept] == [2]          # a fire crossing the state line stays whole
    fake = FakeArcGIS()
    conn.http_session = lambda bearer_env=None: fake
    (ref,) = conn.discover(load_aoi("ca"), Period(date(2018, 1, 1), date(2018, 12, 31)))
    assert ref.key == "frap-ca-2018-v20260521-g0.0005" and ref.meta["max_allowable_offset_deg"] == 0.0005
    conn.extract(ref, load_aoi("ca"))
    assert {p.get("maxAllowableOffset") for _, p in fake.calls if "objectIds" in p} == {"0.0005"}
    (local,) = conn.discover(load_aoi("smm"), Period(date(2018, 1, 1), date(2018, 12, 31)))
    assert local.key == "frap-smm-2018-v20260521" and local.meta["max_allowable_offset_deg"] is None


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_frap_layer_and_woolsey(tmp_path):
    conn = FirePerimetersConnector(load_dataset("fire-perimeters"), Lake(tmp_path / "live"), Settings.from_env())
    s = conn.http_session()
    assert conn.frap_max_year(s) >= 2025
    assert len(conn.layer_version(s, "frap")) == 8
    d = conn._get_json(s, f"{conn.layer('frap')}/query",
                       {"where": "YEAR_ = 2018 AND FIRE_NAME = 'WOOLSEY'", "outFields": "GIS_ACRES,ALARM_DATE",
                        "returnGeometry": "false", "f": "json"})
    assert d["features"] and d["features"][0]["attributes"]["GIS_ACRES"] > 80000
    ids = conn._get_json(s, f"{conn.layer('wfigs')}/query",
                         {"where": conn.where("wfigs", 2026), "returnIdsOnly": "true", "f": "json"})
    assert ids.get("objectIds")          # the current season has perimeters somewhere in the US
