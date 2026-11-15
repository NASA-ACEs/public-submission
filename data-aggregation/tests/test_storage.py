# fuel-lake — tests for Lake storage on local paths and on an S3 root (moto server)
# Licensed under Apache-2.0.
from __future__ import annotations

import os
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from fuellake import catalog, events, pipeline, rasters, tables
from fuellake.connectors.base import Period
from fuellake.grid import grid_from_bbox
from fuellake.storage import Lake


def _exercise_basic_io(lake: Lake) -> None:
    lake.write_bytes("a/b/c.bin", b"\x00\x01")
    assert lake.exists("a/b/c.bin")
    assert lake.read_bytes("a/b/c.bin") == b"\x00\x01"
    lake.write_text("a/x.txt", "hello")
    lake.write_json("a/d.json", {"v": 1, "when": datetime(2026, 6, 1, tzinfo=UTC), "day": date(2026, 6, 1)})
    assert lake.read_json("a/d.json") == {"v": 1, "when": "2026-06-01T00:00:00Z", "day": "2026-06-01"}
    assert lake.ls("a") == ["a/b", "a/d.json", "a/x.txt"]
    assert lake.ls("does/not/exist") == []
    assert lake.glob("a/*.txt") == ["a/x.txt"]
    assert lake.find("a") == ["a/b/c.bin", "a/d.json", "a/x.txt"]
    with lake.open("a/x.txt", "rb") as f:
        assert f.read() == b"hello"
    lake.append_text("a/log.jsonl", "1\n")
    lake.append_text("a/log.jsonl", "2\n")
    assert lake.read_text("a/log.jsonl") == "1\n2\n"
    with pytest.raises(ValueError):
        lake.write_json("a/nan.json", {"v": float("nan")})
    with pytest.raises(ValueError):
        lake.url("../outside")
    # local_path: file write + read
    with lake.local_path("lp/file.txt", "w") as p:
        with open(p, "w") as f:
            f.write("local")
    assert lake.read_text("lp/file.txt") == "local"
    with lake.local_path("lp/file.txt", "r") as p:
        assert open(p).read() == "local"
    # local_path: directory write + read
    with lake.local_path("lp/store.zarr", "w") as p:
        os.makedirs(os.path.join(p, "sub"))
        open(os.path.join(p, "sub", "chunk"), "wb").write(b"zz")
        open(os.path.join(p, "zarr.json"), "w").write("{}")
    assert lake.find("lp/store.zarr") == ["lp/store.zarr/sub/chunk", "lp/store.zarr/zarr.json"]
    with lake.local_path("lp/store.zarr", "r") as p:
        assert open(os.path.join(p, "sub", "chunk"), "rb").read() == b"zz"
    lake.rm("a/x.txt")
    assert not lake.exists("a/x.txt")


def test_local_lake_basic_io(lake):
    assert lake.is_local
    _exercise_basic_io(lake)
    assert lake.rel(lake.url("a/b/c.bin")) == "a/b/c.bin"


def test_from_env_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LAKE_ROOT", str(tmp_path / "not-yet"))
    lake = Lake.from_env()
    assert lake.root == str((tmp_path / "not-yet").resolve())
    assert not (tmp_path / "not-yet").exists()
    lake.init()
    assert (tmp_path / "not-yet" / "inbox" / "biomass").is_dir()


def test_local_path_write_failure_keeps_old_remote(s3_lake):
    s3_lake.write_text("keep.txt", "old")
    with pytest.raises(RuntimeError), s3_lake.local_path("keep.txt", "w") as p:
        open(p, "w").write("new")
        raise RuntimeError("boom")
    assert s3_lake.read_text("keep.txt") == "old"


def test_s3_lake_basic_io(s3_lake):
    assert not s3_lake.is_local
    assert s3_lake.root.startswith("s3://")
    _exercise_basic_io(s3_lake)


def test_s3_lake_rasters_tables_catalog(s3_lake):
    grid = grid_from_bbox((-118.80, 34.05, -118.78, 34.07), "EPSG:32611", 30)
    arr = np.linspace(0, 0.5, grid.width * grid.height, dtype="float32").reshape(grid.shape)
    rasters.write_cog(s3_lake, "staged/x/tiny/i/soil_moisture.tif", arr, grid, np.nan, "float32")
    back, g2 = rasters.read_cog(s3_lake, "staged/x/tiny/i/soil_moisture.tif")
    np.testing.assert_allclose(back, arr)
    assert g2 == grid
    assert rasters.cog_info(s3_lake, "staged/x/tiny/i/soil_moisture.tif")["layout"] == "COG"

    df = pd.DataFrame({"lon": [-118.79], "lat": [34.06], "v": [1.0]})
    tables.write_geoparquet(s3_lake, "staged/tables/t/ds/part-1.parquet", df)
    assert len(tables.read_table(s3_lake, "t")) == 1

    ds = xr.Dataset({"t2m": (("time", "y", "x"), np.ones((2, 3, 4), "float32"))},
                    coords={"time": pd.date_range("2026-06-01", periods=2), "y": [3.0, 2.0, 1.0],
                            "x": [0.0, 1.0, 2.0, 3.0]})
    rasters.write_zarr(s3_lake, "staged/w/tiny/i/data.zarr", ds)
    assert float(rasters.open_zarr(s3_lake, "staged/w/tiny/i/data.zarr")["t2m"].sum()) == 24.0

    events.emit(s3_lake, "item.created", dataset="x")
    events.emit(s3_lake, "item.updated", dataset="x")
    assert [e["type"] for e in events.read_events(s3_lake)] == ["item.created", "item.updated"]


def test_s3_pipeline_end_to_end(s3_lake, test_config):
    period = Period(date(2026, 6, 1), date(2026, 6, 30))
    rep = pipeline.run(["synthetic-raster", "synthetic-table"], "tiny", period)
    assert rep.ok, rep.to_dict()
    assert rep.counts()["items"] == 2
    assert len(catalog.search(s3_lake, aoi="tiny")) == 2
    rep2 = pipeline.run(["synthetic-raster", "synthetic-table"], "tiny", period)
    assert rep2.counts()["items"] == 0 and rep2.counts()["skipped"] == 2
