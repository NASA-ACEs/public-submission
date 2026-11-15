# fuel-lake — tests for the AOI/dataset registry and repository conventions
# Licensed under Apache-2.0.
from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import pytest

from fuellake.config import Settings, redact
from fuellake.connectors.base import BaseConnector, Period
from fuellake.registry import DatasetConfig, list_datasets, load_aoi, load_connector_class, load_dataset

# The datasets of the first build. New configs may be added at any time (the checks below are derived from the
# config directory or are superset checks), so a new dataset never breaks these tests.
INVENTORY = {"fems-lfmc", "globe-lfmc", "nisar-sme2", "nisar-gcov", "opera-rtc-s1", "sentinel2-indices",
             "cop-dem", "landfire-fbfm40", "biomass", "era5-land-daily", "gridmet", "firms", "fire-perimeters"}
GOLD = {"features-cube", "matchups", "point-features"}   # written by `fuellake build-features` / `build-points`
INTERFACES = {"fuel-state", "spread-risk"}  # published by the team's systems A and B (LakeClient.publish_*)
REPO = Path(__file__).resolve().parents[1]


def test_real_config_matches_inventory():
    datasets = list_datasets()
    files = sorted(p.stem for p in (REPO / "config" / "datasets").glob("*.yaml"))
    assert [d.id for d in datasets] == files                 # every file loads, ids match file names
    assert INVENTORY | GOLD | INTERFACES <= set(files)
    assert all(d.connector is None for d in datasets if d.id in GOLD | INTERFACES)
    from fuellake.features._common import CUBE_DATASET, MATCHUPS_DATASET
    from fuellake.features.points import POINTS_DATASET
    from fuellake.registry import GOLD_DATASETS

    assert set(GOLD_DATASETS) == GOLD == {CUBE_DATASET, MATCHUPS_DATASET, POINTS_DATASET}
    for d in datasets:
        # connector authors fill these in later: allowed values only, not the phase-1 placeholders
        assert d.status in ("tested", "search-only", "stub", "unverified", "interface")
        assert d.connector is None or d.connector.startswith("fuellake.connectors.")
        assert (d.status == "interface") == (d.id in INTERFACES) or d.id not in INVENTORY | GOLD | INTERFACES
        if d.kind == "table":
            assert d.table
            if d.id in INVENTORY | GOLD:
                assert d.table in {"lfmc_samples", "fire_detections", "fire_perimeters", "matchups",
                                   "point_features"}
    assert {"era5-land-daily", "gridmet", "features-cube"} <= {d.id for d in datasets if d.kind == "raster-b"}
    sme2 = load_dataset("nisar-sme2")
    sm = sme2.variable("soil_moisture")
    assert math.isnan(sm.nodata) and sm.valid_range == (0.0, 0.7) and sme2.maturity == "PROVISIONAL"
    gcov = load_dataset("nisar-gcov")
    assert {"gamma0_hh_db", "gamma0_hv_db"} <= {v.name for v in gcov.variables}
    assert load_dataset("landfire-fbfm40").variable("fuel_model").categorical


def test_aoi():
    aoi = load_aoi("smm")
    assert aoi.bbox == (-118.95, 33.98, -118.50, 34.20)
    assert aoi.nisar.track == 13 and aoi.nisar.frame == 71 and aoi.nisar.direction == "descending"
    with pytest.raises(KeyError):
        load_aoi("nowhere")


def test_connector_loading(test_config):
    cls = load_connector_class("synthetic_connector:SyntheticConnector")
    assert issubclass(cls, BaseConnector)
    with pytest.raises(ValueError):
        load_connector_class("no_colon")
    with pytest.raises(TypeError):
        load_connector_class("fuellake.storage:Lake")
    with pytest.raises(ValueError):
        DatasetConfig(id="x", title="x", kind="table", license={"id": "x"})  # table without 'table'


def test_period_and_item_id(test_config, lake):
    with pytest.raises(ValueError):
        Period(date(2026, 6, 2), date(2026, 6, 1))
    cls = load_connector_class("synthetic_connector:SyntheticConnector")
    conn = cls(load_dataset("synthetic-raster"), lake, Settings.from_env())
    aoi = load_aoi("tiny")
    ref = conn.discover(aoi, Period(date(2026, 6, 1), date(2026, 6, 1)))[0]
    assert conn.item_id(aoi, ref) == "synthetic-raster__tiny__20260601"
    assert conn.item_id(aoi, ref, "t013d071") == "synthetic-raster__tiny__20260601__t013d071"
    assert conn.raw_path(ref, "f.h5") == "raw/synthetic-raster/2026/06/f.h5"


def test_settings_and_redaction(monkeypatch):
    monkeypatch.setenv("FIRMS_MAP_KEY", "abcd1234efgh")
    s = Settings.from_env()
    assert "abcd1234efgh" not in repr(s)
    assert Settings.secret("FIRMS_MAP_KEY") == "abcd1234efgh"
    assert redact("https://firms/api?key=abcd1234efgh") == "https://firms/api?key=***"
    with pytest.raises(KeyError):
        Settings.secret("HOME")
    monkeypatch.setenv("LAKE_ROOT", "s3://bucket/lake")
    monkeypatch.setenv("FUELLAKE_S3_ENDPOINT_URL", "http://localhost:9000")
    assert Settings.from_env().storage_options() == {"client_kwargs": {"endpoint_url": "http://localhost:9000"}}


def test_default_lake_root_is_a_per_test_sentinel(monkeypatch, tmp_path):
    # conftest redirects the fallback root, so no test can touch ../fuel-lake-data even with LAKE_ROOT unset
    monkeypatch.delenv("LAKE_ROOT")
    root = Settings.from_env().lake_root
    assert root.startswith(str(tmp_path)) and "fuel-lake-data" not in root


def test_every_python_file_has_the_header():
    files = sorted((REPO / "src").rglob("*.py")) + sorted((REPO / "tests").rglob("*.py"))
    assert files
    for f in files:
        lines = f.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("# fuel-lake — "), f
        assert lines[1] == "# Licensed under Apache-2.0.", f
