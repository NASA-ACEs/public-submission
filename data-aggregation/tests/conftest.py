# fuel-lake — shared pytest fixtures (temporary lakes and configs, moto S3 server)
# Licensed under Apache-2.0.
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import yaml

import fuellake.config
from fuellake.config import SECRET_ENV_VARS
from fuellake.storage import Lake

TINY_AOI = {
    "id": "tiny",
    "name": "Tiny test AOI inside the Santa Monica Mountains",
    "bbox": [-118.80, 34.05, -118.78, 34.07],
    "crs": "EPSG:32611",
    "resolution": 30,
    "nisar": {"track": 13, "frame": 71, "direction": "descending"},
}

_LICENSE = {"id": "CC0-1.0", "url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "redistributable": True, "attribution": "synthetic", "notes": "test data"}
_SM = {"name": "soil_moisture", "unit": "m3/m3", "dtype": "float32", "nodata": "NaN",
       "valid_range": [0.0, 0.7], "resampling": "bilinear"}


def _raster_ds(ds_id: str, **options) -> dict:
    return {"id": ds_id, "title": f"Synthetic raster {ds_id}", "kind": "raster-a",
            "connector": "synthetic_connector:SyntheticConnector", "status": "tested",
            "provider": "tests", "access": {"method": "none", "auth": "none"}, "license": _LICENSE,
            "variables": [_SM], "options": options}


SYNTHETIC_DATASETS = {
    "synthetic-raster": _raster_ds("synthetic-raster"),
    "synthetic-table": {"id": "synthetic-table", "title": "Synthetic LFMC table", "kind": "table",
                        "table": "lfmc_samples", "connector": "synthetic_connector:SyntheticConnector",
                        "status": "tested", "provider": "tests", "access": {"method": "none", "auth": "none"},
                        "license": _LICENSE},
    "synthetic-flaky": _raster_ds("synthetic-flaky", n_units=3, fail_keys=["synthetic-flaky-20260602"]),
    "synthetic-badcrs": _raster_ds("synthetic-badcrs", bad_crs=True),
    "synthetic-unmasked": _raster_ds("synthetic-unmasked", skip_mask=True),
    "synthetic-weather": {"id": "synthetic-weather", "title": "Synthetic form B weather", "kind": "raster-b",
                          "connector": "synthetic_connector:SyntheticConnector", "status": "tested",
                          "license": _LICENSE,
                          "variables": [{"name": "t2m", "unit": "K", "dtype": "float32", "nodata": "NaN",
                                         "valid_range": [180, 340]}]},
    "synthetic-stub": {"id": "synthetic-stub", "title": "Registered without connector", "kind": "raster-a",
                       "connector": None, "status": "stub", "license": _LICENSE, "variables": [_SM]},
}


@pytest.fixture(autouse=True)
def _isolated_env(request, tmp_path, monkeypatch):
    """Every test gets its own LAKE_ROOT and no S3 endpoint from the developer's shell. Secrets are removed
    too, except for tests marked ``network`` or ``earthdata`` (live tests need EARTHDATA_TOKEN etc.).

    The default lake root (used when LAKE_ROOT is unset) points at a per-test sentinel directory, and the test
    fails if anything is written there. This replaces a check that ``../fuel-lake-data`` did not appear
    during a test: that check was flaky, because other processes (a real ``fuellake run``, another checkout's
    test run) create and fill that directory while the suite runs, and the failure then landed on whichever
    test happened to be running (e.g. ``test_ease2_centre_matches_real_sample_values``)."""
    sentinel = tmp_path / "default-lake-root-must-not-be-used"
    monkeypatch.setattr(fuellake.config, "default_lake_root", lambda: str(sentinel))
    monkeypatch.setenv("LAKE_ROOT", str(tmp_path / "lake"))
    monkeypatch.setenv("FUELLAKE_CACHE_DIR", str(tmp_path / "download-cache"))   # never ~/.cache/fuel-lake
    names = ["FUELLAKE_CONFIG_DIR", "FUELLAKE_S3_ENDPOINT_URL", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3",
             "FUELLAKE_S3_ANON"]
    live = request.node.get_closest_marker("network") or request.node.get_closest_marker("earthdata")
    if not live:
        names += list(SECRET_ENV_VARS)
    for name in names:
        monkeypatch.delenv(name, raising=False)
    yield
    assert not sentinel.exists(), "a test fell back to the default lake root (LAKE_ROOT unset) and wrote to it"


@pytest.fixture
def lake(tmp_path) -> Lake:
    return Lake(tmp_path / "lake")


@pytest.fixture
def test_config(tmp_path, monkeypatch) -> Path:
    """Temporary config dir with the tiny AOI and the synthetic datasets; FUELLAKE_CONFIG_DIR points at it."""
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    for ds_id, ds in SYNTHETIC_DATASETS.items():
        (root / "datasets" / f"{ds_id}.yaml").write_text(yaml.safe_dump(ds))
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


# A synthetic region whose edge at lon -118.79 cuts through the tiny AOI (east half inside), while its envelope
# (an L-shape with a strip along 33.95-34.00) still covers the whole AOI, so the AOI loads.
TEST_REGION_COORDS = [[-118.90, 33.95], [-118.70, 33.95], [-118.70, 34.15], [-118.79, 34.15], [-118.79, 34.00],
                      [-118.90, 34.00], [-118.90, 33.95]]
TEST_REGION_CUT_LON = -118.79


def write_test_region(config_dir: Path) -> None:
    """Write ``lake.yaml`` naming the synthetic region ``testland`` (TEST_REGION_COORDS) into ``config_dir``."""
    import json

    (config_dir / "lake.yaml").write_text("region: testland\n")
    regions = config_dir / "regions"
    regions.mkdir(exist_ok=True)
    (regions / "testland.yaml").write_text(yaml.safe_dump({
        "id": "testland", "name": "Synthetic test region", "geometry": "testland.geojson",
        "metric_crs": "EPSG:32611", "tolerance_m": 10,
        "license": {"id": "CC0-1.0", "redistributable": True}}))
    (regions / "testland.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [TEST_REGION_COORDS]}}]}))


@pytest.fixture
def region_config(test_config) -> Path:
    """``test_config`` plus ``lake.yaml`` naming the synthetic region ``testland`` (see TEST_REGION_COORDS)."""
    write_test_region(test_config)
    return test_config


@pytest.fixture(scope="session")
def moto_endpoint():
    """A local moto S3 server (no network, no AWS account)."""
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def s3_lake(moto_endpoint, monkeypatch) -> Lake:
    """A Lake on ``s3://<fresh bucket>/lake`` configured purely through environment variables."""
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    bucket = f"lake-{uuid.uuid4().hex[:10]}"
    boto3.client("s3", endpoint_url=moto_endpoint, region_name="us-east-1").create_bucket(Bucket=bucket)
    monkeypatch.setenv("FUELLAKE_S3_ENDPOINT_URL", moto_endpoint)
    monkeypatch.setenv("LAKE_ROOT", f"s3://{bucket}/lake")
    return Lake.from_env()
