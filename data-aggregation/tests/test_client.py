# fuel-lake — LakeClient tests: the same assertions in direct-local, direct-S3 (moto) and HTTP mode
# Licensed under Apache-2.0.
from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from fixtures.services.demo_lake import (
    SME2,
    SME2_DAYS,
    add_sme2_item,
    build_demo_lake,
    expected_sme2_stats,
    serve,
    tiny_grid,
)
from fuellake import catalog, events, rasters, tables
from fuellake.client import LakeClient, filter_table
from fuellake.contracts import MixedMaturityError
from fuellake.services.api import create_app
from fuellake.storage import Lake

PIPELINE_COLLECTIONS = ["synthetic-raster", "synthetic-table", "synthetic-weather"]


@pytest.fixture(params=["local", "s3", "http"])
def demo(request, test_config, monkeypatch):
    """``(client, lake)`` on a freshly built demo lake, for each client mode."""
    monkeypatch.delenv("FUELLAKE_API_URL", raising=False)
    lake: Lake = request.getfixturevalue("s3_lake" if request.param == "s3" else "lake")
    build_demo_lake(lake)
    if request.param == "http":
        with serve(create_app(lake)) as url, LakeClient(api_url=url) as client:
            assert client.mode == "http"
            yield client, lake
        return
    client = LakeClient.from_env() if request.param == "s3" else LakeClient(lake)   # s3: LAKE_ROOT from env
    assert client.mode == "direct" and client.lake.root == lake.root
    yield client, lake


def _ids(items):
    return [i["id"] for i in items]


def test_collections_and_datasets(demo):
    client, _ = demo
    assert [c["id"] for c in client.collections()] == PIPELINE_COLLECTIONS + [SME2]
    datasets = {d["id"]: d for d in client.datasets()}
    assert {"synthetic-raster", "synthetic-table", "synthetic-weather", "synthetic-stub"} <= set(datasets)
    assert datasets["synthetic-raster"]["variables"][0]["nodata"] == "nan"
    assert datasets["synthetic-table"]["license"]["id"] == "CC0-1.0"


def test_search_filters_and_date_semantics(demo):
    client, lake = demo
    sme2 = client.search(collections=[SME2])
    assert _ids(sme2) == [f"{SME2}__tiny__{d:%Y%m%d}" for d in SME2_DAYS]
    assert client.search(collections=SME2) == sme2                              # a bare string works too
    assert len(client.search(aoi="tiny")) == 5 and client.search(aoi="nope") == []
    assert _ids(client.search(collections=[SME2], start=date(2026, 6, 10))) == [sme2[1]["id"]]
    # a bare end date includes the whole day (item at 13:50 UTC) in every mode
    assert _ids(client.search(collections=[SME2], end=date(2026, 6, 5))) == [sme2[0]["id"]]
    assert _ids(client.search(collections=[SME2], end="2026-06-05")) == [sme2[0]["id"]]
    assert client.search(collections=[SME2], end=datetime(2026, 6, 5, 12, 0, tzinfo=UTC)) == []
    assert len(client.search(collections=[SME2], start="2026-06-05T13:50:00Z", end="2026-06-17T13:50:00Z")) == 2
    assert len(client.search(collections=[SME2], query={"fuellake:maturity": "PROVISIONAL"})) == 2
    assert client.search(collections=[SME2], query={"fuellake:maturity": {"in": ["VALIDATED"]}}) == []
    assert client.search(bbox=(10.0, 10.0, 11.0, 11.0)) == []
    assert len(client.search(bbox=(-118.79, 34.06, -118.785, 34.065))) == 5
    assert _ids(client.search(collections=[SME2], limit=1)) == [sme2[0]["id"]]
    assert client.search(collections=[]) == []
    # HTTP pages through results; same order as direct mode
    client._page_size = 2
    direct = catalog.search(lake, aoi="tiny")
    assert _ids(client.search(aoi="tiny")) == _ids(direct)
    assert _ids(client.search(aoi="tiny", limit=3)) == _ids(direct)[:3]


def test_items_have_usable_hrefs_per_mode(demo):
    client, _ = demo
    item = client.get_item(SME2, f"{SME2}__tiny__20260605")
    assert item["id"] == f"{SME2}__tiny__20260605" and item["properties"]["fuellake:maturity"] == "PROVISIONAL"
    href = item["assets"]["soil_moisture"]["href"]
    if client.mode == "http":
        assert href == f"{client.api_url}/assets/{SME2}/{item['id']}/soil_moisture"
    else:
        assert href == "../../../staged/nisar-sme2/tiny/nisar-sme2__tiny__20260605/soil_moisture.tif"
    assert client.get_item(SME2, "nisar-sme2__tiny__19990101") is None
    assert client.get_item("no-such-collection", "x") is None


def test_open_asset_form_a(demo):
    client, lake = demo
    grid = tiny_grid()
    item = client.search(collections=[SME2])[0]
    da = client.open_asset(item, "soil_moisture")
    assert da.dims == ("y", "x") and da.shape == grid.shape and da.dtype == np.float32
    assert da.rio.crs.to_epsg() == 32611 and np.isnan(da.rio.nodata)
    assert tuple(da.rio.transform())[:6] == tuple(grid.transform)[:6]
    assert da.name == "soil_moisture" and da.attrs["fuellake:item_id"] == item["id"]
    mean, valid_fraction = expected_sme2_stats()
    assert float(da.mean()) == pytest.approx(mean) and float(np.isfinite(da).mean()) == pytest.approx(valid_fraction)
    # pipeline-produced COG: identical to the core reader
    raster_item = client.search(collections=["synthetic-raster"])[0]
    arr, _ = rasters.read_cog(lake, catalog.asset_rel(catalog.get_item(lake, "synthetic-raster", raster_item["id"]),
                                                      "soil_moisture"))
    np.testing.assert_array_equal(client.open_asset(raster_item, "soil_moisture").values, arr)
    with pytest.raises(KeyError):
        client.open_asset(item, "no_such_asset")


def test_open_asset_form_b_and_table_asset(demo):
    client, _ = demo
    weather = client.search(collections=["synthetic-weather"])[0]
    t2m = client.open_asset(weather, "t2m")
    assert t2m.dims == ("time", "y", "x") and t2m.sizes["time"] == 3 and float(t2m.mean()) == 290.0
    assert t2m.rio.crs.to_epsg() == 4326
    assert client.open_asset(weather, "data").name == "t2m"           # single-variable store: asset key works
    with pytest.raises(KeyError):
        client.open_asset(weather, "wind")
    table_item = client.search(collections=["synthetic-table"])[0]
    with pytest.raises(TypeError, match="read_table"):
        client.open_asset(table_item, "table")


def test_read_table_filters(demo):
    client, lake = demo
    df = client.read_table("lfmc_samples")
    direct = tables.read_table(lake, "lfmc_samples")
    assert type(df).__name__ == "GeoDataFrame" and df.crs.to_epsg() == 4326 and len(df) == 3
    pd.testing.assert_frame_equal(pd.DataFrame(df.drop(columns="geometry")),
                                  pd.DataFrame(direct.drop(columns="geometry")))
    assert list(df.geometry.to_wkb()) == list(direct.geometry.to_wkb())
    assert len(client.read_table("lfmc_samples", sources=["fems"])) == 3
    assert len(client.read_table("lfmc_samples", sources=["globe-lfmc"])) == 0
    assert len(client.read_table("lfmc_samples", sources=[])) == 0
    assert len(client.read_table("lfmc_samples", start=date(2026, 6, 1), end="2026-06-01")) == 3
    assert len(client.read_table("lfmc_samples", start=date(2026, 6, 2))) == 0
    assert len(client.read_table("lfmc_samples", end=datetime(2026, 5, 31, tzinfo=UTC))) == 0
    empty = client.read_table("no_such_table", sources=["fems"], start="2026-01-01")
    assert isinstance(empty, pd.DataFrame) and empty.empty


def test_static_items_are_found_for_any_period(demo):
    # a static layer (cadence "static ...", e.g. cop-dem dated 2021) matches 2026 searches in every mode
    from fuellake.registry import DatasetConfig

    client, lake = demo
    dem = DatasetConfig(id="cop-dem", title="DEM", kind="raster-a", license={"id": "other"},
                        cadence="static (Copernicus DEM 2021 release)")
    catalog.upsert_collection(lake, dem)
    item = catalog.make_item("cop-dem", "cop-dem__tiny__20210422", date(2021, 4, 22), {"fuellake:aoi": "tiny"},
                             {"elevation": {"path": "staged/cop-dem/tiny/x/elevation.tif", "roles": ["data"]}},
                             tiny_grid().bounds_wgs84())
    catalog.add_item(lake, item)
    june = client.search(aoi="tiny", start="2026-06-01", end="2026-06-30")
    assert "cop-dem__tiny__20210422" in _ids(june)
    assert _ids(client.search(collections=["cop-dem"], start=date(2030, 1, 1))) == ["cop-dem__tiny__20210422"]


def test_read_matchups_table(demo):
    # the gold table lives at features/<aoi>/matchups.parquet, outside staged/tables: found via the catalog
    from fuellake.features.matchups import build_matchups

    client, lake = demo
    assert client.read_table("matchups").empty
    build_matchups("tiny", date(2026, 6, 1), date(2026, 6, 30), lake=lake)
    df = client.read_table("matchups")
    direct = tables.read_table(lake, "matchups")
    assert type(df).__name__ == "GeoDataFrame" and len(df) == len(direct) == 3
    assert {"grid_row", "grid_col", "soil_moisture", "t2m"} <= set(df.columns)
    assert sorted(df["sample_id"]) == sorted(tables.read_table(lake, "lfmc_samples")["sample_id"])
    assert len(client.read_table("matchups", sources=["fems"], start="2026-06-01", end="2026-06-01")) == 3
    assert len(client.read_table("lfmc_samples", dedupe=False)) == 3


def test_open_cube_and_events(demo):
    client, lake = demo
    cube = client.open_cube("tiny")
    assert set(cube.data_vars) == {"soil_moisture", "lfmc"} and cube["lfmc"].dims == ("time", "y", "x")
    assert float(cube["lfmc"].isel(time=0).mean()) == 95.0 and cube.rio.crs.to_epsg() == 32611
    with pytest.raises(FileNotFoundError):
        client.open_cube("nowhere")
    evs = client.events()
    assert evs == events.read_events(lake)
    assert [e["type"] for e in evs] == ["item.created", "item.created", "table.updated", "item.created",
                                        "run.completed", "item.created", "item.created"]
    assert client.events(since=evs[3]["time"]) == evs[3:]                      # inclusive
    assert client.events(since=datetime.fromisoformat(evs[3]["time"])) == evs[3:]
    assert client.events(since=date(2100, 1, 1)) == []
    created = client.events(types=["item.created"])
    assert len(created) == 5 and {e["collection"] for e in created} == set(PIPELINE_COLLECTIONS) | {SME2}


def test_mixed_maturity_is_refused(demo):
    client, lake = demo
    add_sme2_item(lake, date(2026, 6, 29), maturity="VALIDATED")
    with pytest.raises(MixedMaturityError, match="nisar-sme2"):
        client.search(collections=[SME2])
    with pytest.raises(MixedMaturityError):
        client.search(aoi="tiny")
    assert len(client.search(collections=[SME2], query={"fuellake:maturity": "PROVISIONAL"})) == 2
    assert len(client.search(collections=[SME2], start=date(2026, 6, 20))) == 1   # one maturity in range
    both = client.search(collections=[SME2], allow_mixed_maturity=True)
    assert [i["properties"]["fuellake:maturity"] for i in both] == ["PROVISIONAL", "PROVISIONAL", "VALIDATED"]
    # different collections are different series: no refusal
    add_sme2_item(lake, date(2026, 6, 5), maturity="VALIDATED", collection="nisar-other")
    two_series = client.search(collections=[SME2, "nisar-other"], end=date(2026, 6, 20))
    assert sorted((i["collection"], i["properties"]["fuellake:maturity"]) for i in two_series) == [
        ("nisar-other", "VALIDATED"), (SME2, "PROVISIONAL"), (SME2, "PROVISIONAL")]
    assert len(client.search(collections=["nisar-other", SME2], query={"fuellake:maturity": "VALIDATED"})) == 2


def test_from_env_and_constructor_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("LAKE_ROOT", str(tmp_path / "lake"))
    monkeypatch.delenv("FUELLAKE_API_URL", raising=False)
    direct = LakeClient.from_env()
    assert direct.mode == "direct" and direct.lake.root == str((tmp_path / "lake").resolve())
    assert LakeClient().mode == "direct"
    assert LakeClient(str(tmp_path / "other")).lake.root == str((tmp_path / "other").resolve())
    monkeypatch.setenv("FUELLAKE_API_URL", "http://api.example:8000/")
    for client in (LakeClient.from_env(), LakeClient()):
        assert client.mode == "http" and client.api_url == "http://api.example:8000" and client.lake is None
        client.close()
    assert LakeClient.from_env(api_url="http://other:1").api_url == "http://other:1"
    with pytest.raises(ValueError):
        LakeClient(Lake(tmp_path), api_url="http://x")
    assert not (tmp_path / "lake").exists(), "constructing a client must not create the lake"


def test_filter_table_handles_timestamps_and_missing_columns():
    stamps = pd.to_datetime(["2026-06-01T23:00", "2026-06-02T00:00"], utc=True)
    df = pd.DataFrame({"source": ["a", "b"], "date": stamps})
    assert len(filter_table(df, start="2026-06-02")) == 1
    assert len(filter_table(df, sources=["a"], end=date(2026, 6, 1))) == 1
    with pytest.raises(KeyError):
        filter_table(pd.DataFrame({"x": [1]}), sources=["a"])
    assert filter_table(pd.DataFrame(), sources=["a"], start="2026-01-01").empty
