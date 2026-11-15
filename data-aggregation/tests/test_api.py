# fuel-lake — HTTP API tests: endpoints, href rewriting, Range requests, GDAL/rasterio and pystac-client
# Licensed under Apache-2.0.
from __future__ import annotations

import io
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient

from fixtures.services.demo_lake import (
    SME2,
    add_cog_item,
    add_sme2_item,
    build_demo_lake,
    serve,
    serve_cli,
)
from fuellake import catalog
from fuellake.client import parquet_to_frame
from fuellake.grid import Grid
from fuellake.services.api import create_app, parse_range
from fuellake.storage import Lake

REPO = Path(__file__).resolve().parents[1]
IID = f"{SME2}__tiny__20260605"
ASSET = f"/assets/{SME2}/{IID}/soil_moisture"


@pytest.fixture
def api(test_config, lake):
    build_demo_lake(lake)
    return TestClient(create_app(lake)), lake


def _asset_bytes(lake: Lake, cid: str, iid: str, key: str) -> bytes:
    return lake.read_bytes(catalog.asset_rel(catalog.get_item(lake, cid, iid), key))


def test_health_landing_datasets(api):
    client, _ = api
    assert client.get("/health").json() == {"status": "ok", "version": "0.1.0", "catalog": True}
    landing = client.get("/").json()
    assert "Not a complete STAC API" in landing["description"]
    assert "https://api.stacspec.org/v1.0.0/item-search" in landing["conformsTo"]
    assert {(lk["rel"], lk.get("method")) for lk in landing["links"]} >= {("data", None), ("search", "POST")}
    datasets = client.get("/datasets")
    assert datasets.status_code == 200
    by_id = {d["id"]: d for d in datasets.json()}
    assert by_id["synthetic-raster"]["variables"][0]["nodata"] == "nan"     # NaN must not break the JSON
    assert "Not a complete STAC API" in client.get("/openapi.json").json()["info"]["description"]


def test_module_app_uses_lake_root_per_request(test_config, lake):
    from fuellake.services.api import app

    client = TestClient(app)
    assert client.get("/health").json()["catalog"] is False
    build_demo_lake(lake)                                   # LAKE_ROOT (autouse fixture) points at this lake
    assert client.get("/health").json()["catalog"] is True
    assert client.get(f"/collections/{SME2}/items/{IID}").status_code == 200


def test_collections(api):
    client, _ = api
    body = client.get("/collections").json()
    assert [c["id"] for c in body["collections"]] == ["synthetic-raster", "synthetic-table", "synthetic-weather", SME2]
    coll = client.get(f"/collections/{SME2}").json()
    rels = {lk["rel"]: lk["href"] for lk in coll["links"]}
    assert rels["self"] == f"http://testserver/collections/{SME2}"
    assert rels["items"] == f"http://testserver/collections/{SME2}/items"
    assert "item" not in rels and coll["fuellake:maturity"] == "PROVISIONAL"
    assert client.get("/collections/nope").status_code == 404
    assert client.get("/collections/bad$id").status_code == 404


def test_items_endpoint_filters_and_paging(api):
    client, _ = api
    page1 = client.get(f"/collections/{SME2}/items", params={"limit": 1})
    assert page1.headers["content-type"].startswith("application/geo+json")
    fc = page1.json()
    assert fc["type"] == "FeatureCollection" and fc["numberMatched"] == 2 and fc["numberReturned"] == 1
    nxt = next(lk for lk in fc["links"] if lk["rel"] == "next")
    page2 = client.get(nxt["href"]).json()
    assert [f["id"] for f in fc["features"] + page2["features"]] == [IID, f"{SME2}__tiny__20260617"]
    assert not any(lk["rel"] == "next" for lk in page2["links"])
    def ids(**params):
        return [f["id"] for f in client.get(f"/collections/{SME2}/items", params=params).json()["features"]]
    assert ids(datetime="2026-06-10/..") == [f"{SME2}__tiny__20260617"]
    assert ids(datetime="../2026-06-05") == [IID]                          # bare end date = whole day
    assert ids(datetime="2026-06-05T13:50:00+00:00") == [IID]                # '+' survives params encoding
    assert ids(bbox="10,10,11,11") == [] and len(ids(bbox="-119,33,0,-118,35,100")) == 2
    assert client.get(f"/collections/{SME2}/items", params={"datetime": "June"}).status_code == 400
    assert client.get(f"/collections/{SME2}/items", params={"bbox": "1,2,3"}).status_code == 400
    assert client.get(f"/collections/{SME2}/items", params={"limit": 0}).status_code == 422
    assert client.get("/collections/nope/items").status_code == 404


def test_item_json_has_api_hrefs_that_work(api):
    client, lake = api
    item = client.get(f"/collections/{SME2}/items/{IID}").json()
    href = item["assets"]["soil_moisture"]["href"]
    assert href == f"http://testserver{ASSET}"
    links = {lk["rel"]: lk["href"] for lk in item["links"]}
    assert links["self"] == f"http://testserver/collections/{SME2}/items/{IID}"
    assert links["collection"] == f"http://testserver/collections/{SME2}" and links["root"] == "http://testserver/"
    assert client.get(href).content == _asset_bytes(lake, SME2, IID, "soil_moisture")
    assert item["properties"] == catalog.get_item(lake, SME2, IID)["properties"]
    assert client.get(f"/collections/{SME2}/items/nisar-sme2__tiny__19990101").status_code == 404


def test_search_get_and_post(api):
    client, _ = api
    def get_ids(**params):
        r = client.get("/search", params=params)
        assert r.status_code == 200, r.text
        return [f["id"] for f in r.json()["features"]]
    def post_ids(body):
        r = client.post("/search", json=body)
        assert r.status_code == 200, r.text
        return [f["id"] for f in r.json()["features"]]
    assert len(get_ids()) == 5 and get_ids() == post_ids({})
    assert get_ids(collections=f"{SME2},synthetic-raster") == post_ids({"collections": [SME2, "synthetic-raster"]})
    q = {"fuellake:maturity": {"eq": "PROVISIONAL"}}
    assert get_ids(query=json.dumps(q)) == post_ids({"query": q}) == [IID, f"{SME2}__tiny__20260617"]
    assert get_ids(ids=IID) == post_ids({"ids": [IID]}) == [IID]
    assert get_ids(aoi="nope") == post_ids({"aoi": "nope"}) == []
    assert post_ids({"datetime": "2026-06-10/2026-06-30", "collections": [SME2]}) == [f"{SME2}__tiny__20260617"]
    fc = client.post("/search", json={"limit": 2}).json()
    nxt = next(lk for lk in fc["links"] if lk["rel"] == "next")
    assert nxt["method"] == "POST" and nxt["body"] == {"limit": 2, "offset": 2}
    assert client.post("/search", json={"intersects": {"type": "Point", "coordinates": [0, 0]}}).status_code == 422
    assert client.get("/search", params={"query": "{not json"}).status_code == 400
    assert client.post("/search", json={"query": {"fuellake:maturity": {"like": "P%"}}}).status_code == 400


def test_parse_range():
    assert parse_range(None, 100) is None and parse_range("bytes=0-1,5-6", 100) is None    # multi-range: ignored
    assert parse_range("items=0-1", 100) is None and parse_range("bytes=-", 100) is None
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99) and parse_range("bytes=-500", 100) == (0, 99)
    assert parse_range("bytes=95-500", 100) == (95, 99)
    for bad in ("bytes=100-", "bytes=5-4", "bytes=-0"):
        with pytest.raises(ValueError):
            parse_range(bad, 100)


def _check_ranges(client: TestClient, lake: Lake, path: str, data: bytes) -> None:
    size = len(data)
    full = client.get(path)
    assert full.status_code == 200 and full.content == data
    assert full.headers["accept-ranges"] == "bytes" and int(full.headers["content-length"]) == size
    r = client.get(path, headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and r.content == data[:100]
    assert r.headers["content-range"] == f"bytes 0-99/{size}" and r.headers["content-length"] == "100"
    r = client.get(path, headers={"Range": "bytes=100-"})
    assert r.status_code == 206 and r.content == data[100:]
    r = client.get(path, headers={"Range": "bytes=-50"})
    assert r.status_code == 206 and r.content == data[-50:]
    r = client.get(path, headers={"Range": f"bytes={size - 10}-{size + 1000}"})
    assert r.status_code == 206 and r.content == data[-10:]
    r = client.get(path, headers={"Range": f"bytes={size}-"})
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{size}"
    r = client.get(path, headers={"Range": "bytes=0-1,10-11"})
    assert r.status_code == 200 and r.content == data
    head = client.head(path)
    assert head.status_code == 200 and head.content == b"" and int(head.headers["content-length"]) == size
    head = client.head(path, headers={"Range": "bytes=10-19"})
    assert head.status_code == 206 and head.headers["content-range"] == f"bytes 10-19/{size}"


def test_asset_range_requests(api):
    client, lake = api
    data = _asset_bytes(lake, SME2, IID, "soil_moisture")
    assert len(data) > 200
    _check_ranges(client, lake, ASSET, data)
    r = client.get(ASSET)
    assert r.headers["x-fuellake-license"] == "other" and r.headers["x-fuellake-redistributable"] == "false"
    assert r.headers["content-type"].startswith("image/tiff")
    for missing in (f"{ASSET}.ovr", f"/assets/{SME2}/{IID}/nope", f"/assets/{SME2}/nisar-sme2__tiny__19990101/x",
                    f"{ASSET}/sub/file"):
        assert client.get(missing).status_code == 404, missing
        assert client.head(missing).status_code == 404, missing


def test_api_on_s3_lake_ranges_and_fresh_listings(s3_lake):
    client = TestClient(create_app(s3_lake))
    add_sme2_item(s3_lake, date(2026, 6, 5))
    data = _asset_bytes(s3_lake, SME2, IID, "soil_moisture")
    _check_ranges(client, s3_lake, ASSET, data)
    assert client.get(f"/collections/{SME2}/items").json()["numberMatched"] == 1
    # another writer (own S3 client, like the ETL container) rewrites the file and adds an item
    writer = Lake(s3_lake.root, storage_options={**s3_lake.storage_options, "skip_instance_cache": True})
    assert writer.fs is not s3_lake.fs
    add_sme2_item(writer, date(2026, 6, 5), value=0.5, nan_rows=5, run_id="demo-run-2")
    add_sme2_item(writer, date(2026, 6, 17))
    new = _asset_bytes(s3_lake, SME2, IID, "soil_moisture")
    assert new != data
    r = client.get(ASSET)
    assert r.content == new and int(r.headers["content-length"]) == len(new)
    assert client.get(f"/collections/{SME2}/items").json()["numberMatched"] == 2


def test_zarr_asset_and_cube_listing(api):
    client, lake = api
    iid = "synthetic-weather__tiny__20260601"
    base = f"/assets/synthetic-weather/{iid}/data"
    listing = client.get(base).json()
    assert listing["media_type"] == "application/vnd+zarr" and "zarr.json" in listing["files"]
    rel = catalog.asset_rel(catalog.get_item(lake, "synthetic-weather", iid), "data")
    assert client.get(f"{base}/zarr.json").content == lake.read_bytes(f"{rel}/zarr.json")
    assert client.get(f"{base}/t2m/zarr.json").status_code == 200
    assert client.get(f"{base}/..%2F..%2F..%2F..%2F..%2Fcatalog%2Fcatalog.json").status_code == 404
    assert client.get(f"{base}/t2m").status_code == 404                     # a directory, not a file
    cube = client.get("/cubes/tiny").json()
    assert {"zarr.json", "lfmc/zarr.json"} <= set(cube["files"])
    assert client.get("/cubes/tiny/zarr.json").status_code == 200
    assert client.get("/cubes/nowhere").status_code == 404


def test_tables_endpoint(api):
    client, _ = api
    r = client.get("/tables/lfmc_samples")
    assert r.status_code == 200 and r.headers["content-type"] == "application/vnd.apache.parquet"
    df = parquet_to_frame(r.content)
    assert type(df).__name__ == "GeoDataFrame" and len(df) == 3 and df.crs.to_epsg() == 4326
    assert len(parquet_to_frame(client.get("/tables/lfmc_samples?sources=fems,globe-lfmc").content)) == 3
    assert len(parquet_to_frame(client.get("/tables/lfmc_samples?sources=globe-lfmc").content)) == 0
    assert len(parquet_to_frame(client.get("/tables/lfmc_samples?start=2026-06-02").content)) == 0
    assert len(parquet_to_frame(client.get("/tables/lfmc_samples?end=2026-06-01").content)) == 3
    assert client.get("/tables/lfmc_samples?start=June").status_code == 400
    assert parquet_to_frame(client.get("/tables/unknown_table").content).empty
    assert len(parquet_to_frame(client.get("/tables/lfmc_samples?dedupe=false").content)) == 3


def test_tables_endpoint_serves_the_gold_matchups(api):
    from fuellake.features.matchups import build_matchups

    client, lake = api
    build_matchups("tiny", date(2026, 6, 1), date(2026, 6, 30), lake=lake)
    r = client.get("/tables/matchups")
    assert r.status_code == 200
    df = parquet_to_frame(r.content)
    assert len(df) == 3 and "grid_row" in df.columns and df.crs.to_epsg() == 4326


def test_events_endpoint(api):
    client, _ = api
    evs = client.get("/events").json()
    assert len(evs) == 7 and evs[-1]["type"] == "item.created"
    assert client.get("/events", params={"since": evs[2]["time"]}).json() == evs[2:]      # inclusive
    assert [e["type"] for e in client.get("/events", params={"type": "run.completed"}).json()] == ["run.completed"]
    both = client.get("/events", params=[("type", "run.completed"), ("type", "table.updated")]).json()
    assert {e["type"] for e in both} == {"run.completed", "table.updated"}
    assert client.get("/events", params={"since": "yesterday"}).status_code == 400


def test_rasterio_reads_cog_over_http_with_range_requests(test_config, lake, tmp_path):
    """GDAL (/vsicurl/) reads a window of a ~4 MB COG through the API using only ranged GETs."""
    import rasterio
    import rioxarray
    from affine import Affine
    from rasterio.windows import Window

    grid = Grid("EPSG:32611", Affine(30.0, 0.0, 345000.0, 0.0, -30.0, 3770000.0), 1024, 1024, 30.0)
    arr = np.random.default_rng(0).random(grid.shape, dtype="float32") * 0.5
    iid = f"{SME2}__big__20260605"
    add_cog_item(lake, SME2, iid, datetime(2026, 6, 5, tzinfo=UTC), arr, grid)
    size = len(_asset_bytes(lake, SME2, iid, "soil_moisture"))
    log_path = tmp_path / "access.log"
    with serve_cli(lake.root, test_config, log_path) as url:
        href = f"{url}/assets/{SME2}/{iid}/soil_moisture"
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"), rasterio.open(href) as src:
            assert src.crs.to_epsg() == 32611 and src.shape == (1024, 1024) and src.overviews(1)
            window = src.read(1, window=Window(512, 512, 64, 64))
        np.testing.assert_array_equal(window, arr[512:576, 512:576])
        da = rioxarray.open_rasterio(href).squeeze("band", drop=True)
        np.testing.assert_array_equal(da.isel(y=slice(0, 8), x=slice(0, 8)).values, arr[:8, :8])
    log = log_path.read_text()
    asset_gets = re.findall(rf'"GET /assets/{SME2}/{iid}/soil_moisture HTTP/1.1" (\d+)', log)
    assert asset_gets and set(asset_gets) == {"206"}, log
    assert size > 2_000_000


def test_pystac_client_can_search(api):
    pystac_client = pytest.importorskip("pystac_client")
    _, lake = api
    with serve(create_app(lake)) as url:
        cat = pystac_client.Client.open(url)
        assert {c.id for c in cat.get_collections()} >= {SME2, "synthetic-raster"}
        search = cat.search(collections=[SME2], datetime="2026-06-01/2026-06-30", limit=1)
        items = list(search.items())
        assert [i.id for i in items] == [IID, f"{SME2}__tiny__20260617"]          # followed the next link
        assert items[0].assets["soil_moisture"].href == f"{url}{ASSET}"
        assert search.matched() == 2


def test_docker_compose_references():
    """Docker is not available here: check the compose file statically (YAML, references, no secrets)."""
    compose = yaml.safe_load((REPO / "docker" / "compose.yaml").read_text())
    services = compose["services"]
    assert {"minio", "bucket-init", "api", "etl"} <= set(services)
    for name, svc in services.items():
        deps = svc.get("depends_on") or {}
        assert set(deps) <= set(services), (name, deps)
        for vol in svc.get("volumes") or []:
            source = vol.split(":")[0] if isinstance(vol, str) else vol["source"]
            if not source.startswith((".", "/")):
                assert source in (compose.get("volumes") or {}), (name, source)
        build = svc.get("build")
        if build:
            ctx = (REPO / "docker" / build["context"]).resolve()
            assert ctx == REPO and (ctx / build["dockerfile"]).is_file()
    assert services["bucket-init"]["depends_on"]["minio"]["condition"] == "service_healthy"
    for name in ("api", "etl"):
        env = services[name]["environment"]
        assert env["LAKE_ROOT"] == "s3://fuel-lake" and env["FUELLAKE_S3_ENDPOINT_URL"] == "http://minio:9000"
        assert env["FUELLAKE_CONFIG_DIR"] == "/app/config"
        assert services[name]["depends_on"]["bucket-init"]["condition"] == "service_completed_successfully"
    assert "0.0.0.0" in " ".join(services["api"]["command"])
    text = (REPO / "docker" / "compose.yaml").read_text()
    for key in ("MINIO_ROOT_PASSWORD", "AWS_SECRET_ACCESS_KEY"):
        for value in re.findall(rf"^\s*{key}:\s*(\S+)", text, flags=re.M):
            assert value.startswith(("${", '"${')), f"{key} must come from the environment, got {value}"
    dockerfile = (REPO / "docker" / "Dockerfile").read_text()
    assert dockerfile.startswith("# fuel-lake — ") and "FROM python:3.12-slim" in dockerfile
    for src in re.findall(r"^COPY (?!--from)(.+) \S+$", dockerfile, flags=re.M):
        for path in src.split():
            assert (REPO / path).exists(), f"Dockerfile COPY source missing: {path}"
    assert not re.search(r"^COPY \.\s", dockerfile, flags=re.M), "copy named paths only (.env would be shipped)"


def test_local_api_sees_a_second_writer(test_config, tmp_path):
    """The API pinned to a local root sees files written later through another Lake object (the ETL)."""
    import rasterio

    client = TestClient(create_app(Lake(tmp_path / "shared")))
    assert client.get("/events").json() == []
    add_sme2_item(Lake(tmp_path / "shared"), date(2026, 6, 5))
    assert [e["item_id"] for e in client.get("/events").json()] == [IID]
    with rasterio.open(io.BytesIO(client.get(ASSET).content)) as src:
        assert src.count == 1 and src.crs.to_epsg() == 32611
