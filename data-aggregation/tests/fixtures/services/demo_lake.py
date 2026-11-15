# fuel-lake — synthetic demo lake and a threaded uvicorn server for the client, API and consumer tests
# Licensed under Apache-2.0.
"""Helpers shared by ``tests/test_client.py``, ``tests/test_api.py``, ``tests/test_example_consumer.py`` and the
live smoke test (not a test module).

:func:`build_demo_lake` needs ``FUELLAKE_CONFIG_DIR`` pointing at :func:`write_config` output (the ``tiny`` AOI
and the synthetic datasets of ``tests/conftest.py``). It runs the real pipeline for a form A raster, a table
and a form B Zarr dataset, then adds hand-built NISAR SME2 items (the synthetic connector cannot produce the
NISAR properties) and a small gold cube.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr
import yaml

from conftest import SYNTHETIC_DATASETS, TINY_AOI  # tests/ is on sys.path (pyproject pythonpath)
from fuellake import catalog, events, pipeline, rasters
from fuellake.connectors.base import Period
from fuellake.grid import Grid, grid_from_bbox
from fuellake.registry import DatasetConfig, License, Variable
from fuellake.storage import Lake

DEMO_PERIOD = Period(date(2026, 6, 1), date(2026, 6, 30))
PIPELINE_DATASETS = ["synthetic-raster", "synthetic-table", "synthetic-weather"]
SME2 = "nisar-sme2"
SME2_VALUE = 0.25
SME2_DAYS = (date(2026, 6, 5), date(2026, 6, 17))
SME2_TIME = dtime(13, 50)


def tiny_grid() -> Grid:
    return grid_from_bbox(TINY_AOI["bbox"], TINY_AOI["crs"], TINY_AOI["resolution"])


def write_config(root: Path) -> Path:
    """Config dir with the ``tiny`` AOI and the synthetic datasets (same as the ``test_config`` fixture)."""
    (root / "aois").mkdir(parents=True, exist_ok=True)
    (root / "datasets").mkdir(exist_ok=True)
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    for ds_id, ds in SYNTHETIC_DATASETS.items():
        (root / "datasets" / f"{ds_id}.yaml").write_text(yaml.safe_dump(ds))
    return root


def sme2_dataset(collection: str = SME2, maturity: str = "PROVISIONAL") -> DatasetConfig:
    return DatasetConfig(
        id=collection, title=f"Synthetic {collection}", kind="raster-a", status="tested", provider="tests",
        license=License(id="other", redistributable=False), maturity=maturity,
        variables=[Variable(name="soil_moisture", unit="m3/m3", dtype="float32", nodata="NaN",
                            valid_range=(0.0, 0.7))],
    )


def add_cog_item(lake: Lake, collection: str, item_id: str, when: datetime, arr: np.ndarray, grid: Grid,
                 key: str = "soil_moisture", properties: dict | None = None, run_id: str = "demo-run-1") -> dict:
    """Write a one-band COG, catalog it as a form A item and emit ``item.created`` / ``item.updated``."""
    if catalog.get_collection(lake, collection) is None:
        catalog.upsert_collection(lake, sme2_dataset(collection), bbox=grid.bounds_wgs84())
    aoi = item_id.split("__")[1]
    rel = f"staged/{collection}/{aoi}/{item_id}/{key}.tif"
    rasters.write_cog(lake, rel, arr, grid, np.nan, "float32", description=key, unit="m3/m3")
    props = {
        "fuellake:aoi": aoi, "fuellake:dataset": collection, "fuellake:license": "other",
        "fuellake:redistributable": False, "fuellake:status": "tested", "fuellake:source_ids": [f"synthetic://{item_id}"],
        "fuellake:run_id": run_id, "fuellake:pipeline_version": "test", "fuellake:source_key": item_id,
        "fuellake:qc_masked_fraction": 0.0, **(properties or {}),
    }
    asset = {"path": rel, "type": rasters.COG_MEDIA_TYPE, "roles": ["data"], "title": key, **grid.stac_proj(),
             "raster:bands": [{"data_type": "float32", "nodata": "nan", "unit": "m3/m3"}]}
    item = catalog.make_item(collection, item_id, when, props, {key: asset}, grid.bounds_wgs84(),
                             stac_extensions=[catalog.PROJ_EXT, catalog.RASTER_EXT, catalog.SAT_EXT])
    href, created = catalog.add_item(lake, item)
    events.emit(lake, "item.created" if created else "item.updated", dataset=collection, aoi=aoi,
                collection=collection, item_id=item_id, href=href, run_id=run_id)
    return item


def add_sme2_item(lake: Lake, day: date, *, maturity: str = "PROVISIONAL", value: float = SME2_VALUE,
                  collection: str = SME2, run_id: str = "demo-run-1", nan_rows: int = 1) -> dict:
    """A NISAR-SME2-like item on the tiny grid: constant ``value``, the first ``nan_rows`` rows NaN."""
    grid = tiny_grid()
    arr = np.full(grid.shape, value, "float32")
    arr[:nan_rows] = np.nan
    props = {"fuellake:maturity": maturity, "sat:relative_orbit": 13, "fuellake:frame": 71,
             "sat:orbit_state": "descending", "fuellake:processing_id": "P05023", "sar:polarizations": ["HH", "HV"],
             "sar:frequency_band": "L"}
    return add_cog_item(lake, collection, f"{collection}__tiny__{day:%Y%m%d}",
                        datetime.combine(day, SME2_TIME, tzinfo=UTC), arr, grid, properties=props, run_id=run_id)


def expected_sme2_stats(nan_rows: int = 1, value: float = SME2_VALUE) -> tuple[float, float]:
    """(AOI mean, valid-pixel fraction) of :func:`add_sme2_item` output."""
    height, _ = tiny_grid().shape
    return value, (height - nan_rows) / height


def add_cube(lake: Lake, aoi: str = "tiny") -> xr.Dataset:
    grid = tiny_grid()
    h, w = grid.shape
    ds = xr.Dataset(
        {"soil_moisture": (("time", "y", "x"), np.full((2, h, w), 0.2, "float32")),
         "lfmc": (("time", "y", "x"), np.full((2, h, w), 95.0, "float32"))},
        coords={"time": pd.date_range("2026-06-05", periods=2, freq="12D"), "y": grid.y_coords(),
                "x": grid.x_coords()},
    ).rio.write_crs(grid.crs)
    rasters.write_zarr(lake, f"features/{aoi}/cube.zarr", ds)
    return ds


def build_demo_lake(lake: Lake) -> None:
    """Pipeline items (raster, table, weather) + two PROVISIONAL SME2 items + a cube."""
    report = pipeline.run(PIPELINE_DATASETS, "tiny", DEMO_PERIOD, lake=lake)
    assert report.ok, report.to_dict()
    for day in SME2_DAYS:
        add_sme2_item(lake, day)
    add_cube(lake)


# ---------------------------------------------------------------------------- HTTP server
def recording(app, log: list[dict]):
    """Wrap an ASGI app; append ``{method, path, range, status, bytes}`` per HTTP request to ``log``."""
    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        entry = {"method": scope["method"], "path": scope["path"],
                 "range": dict(scope["headers"]).get(b"range", b"").decode() or None, "status": None, "bytes": 0}
        log.append(entry)

        async def send_and_record(message):
            if message["type"] == "http.response.start":
                entry["status"] = message["status"]
            elif message["type"] == "http.response.body":
                entry["bytes"] += len(message.get("body", b""))
            await send(message)

        await app(scope, receive, send_and_record)

    return wrapped


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def serve_cli(lake_root: str | Path, config_dir: str | Path, log_path: Path) -> Iterator[str]:
    """Run ``fuellake serve`` in a subprocess (the real CLI, lake from ``LAKE_ROOT``); yield its base URL.

    Needed when the HTTP client is GDAL (rasterio ``/vsicurl/``): GDAL holds the GIL while it waits for the
    response, so an in-process server thread would deadlock. uvicorn's access log goes to ``log_path``.
    """
    import os
    import subprocess
    import sys

    import httpx

    port = free_port()
    env = {k: v for k, v in os.environ.items() if k != "FUELLAKE_API_URL"}
    env.update(LAKE_ROOT=str(lake_root), FUELLAKE_CONFIG_DIR=str(config_dir))
    with open(log_path, "wb") as log:
        proc = subprocess.Popen([sys.executable, "-m", "fuellake.cli", "serve", "--host", "127.0.0.1",
                                 "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT)
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise RuntimeError(f"fuellake serve did not start: {log_path.read_text()[-2000:]}")
                time.sleep(0.1)
            yield url
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


@contextlib.contextmanager
def serve(app) -> Iterator[str]:
    """Run an ASGI app with uvicorn in a background thread on a free port; yield its base URL.

    Fine for Python HTTP clients (httpx, requests); use :func:`serve_cli` for GDAL clients.
    """
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(15)
        sock.close()
