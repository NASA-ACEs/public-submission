# fuel-lake — end-to-end pipeline tests with the offline synthetic connector
# Licensed under Apache-2.0.
from __future__ import annotations

from datetime import date

import pytest

from fuellake import catalog, events, pipeline
from fuellake.connectors.base import Period
from fuellake.contracts import validate_collection
from fuellake.registry import load_dataset
from fuellake.tables import read_table

PERIOD = Period(date(2026, 6, 1), date(2026, 6, 30))


@pytest.fixture
def env(test_config, lake):
    return lake


def test_end_to_end_then_noop(env):
    lake = env
    rep = pipeline.run(["synthetic-raster", "synthetic-table"], "tiny", PERIOD)
    assert rep.ok, rep.to_dict()
    assert rep.counts() == {"discovered": 2, "skipped": 0, "created": 2, "updated": 0, "empty": 0,
                            "invalid": 0, "failed": 0, "missing_credentials": 0, "items": 2}
    raster = catalog.get_item(lake, "synthetic-raster", "synthetic-raster__tiny__20260601")
    props = raster["properties"]
    assert props["fuellake:source_key"] == "synthetic-raster-20260601"
    assert props["fuellake:source_ids"] == ["synthetic://synthetic-raster/20260601"]
    assert props["fuellake:run_id"] == rep.run_id and props["fuellake:aoi"] == "tiny"
    assert props["fuellake:license"] == "CC0-1.0" and props["fuellake:redistributable"] is True
    assert props["fuellake:qc_masked_fraction"] > 0
    asset = raster["assets"]["soil_moisture"]
    assert asset["proj:epsg"] == 32611 and asset["raster:bands"][0]["nodata"] == "nan"
    assert catalog.PROJ_EXT in raster["stac_extensions"]
    table_item = catalog.get_item(lake, "synthetic-table", "synthetic-table__tiny__20260601")
    assert table_item["properties"]["fuellake:n_rows"] == 3
    assert table_item["assets"]["table"]["type"] == "application/vnd.apache.parquet"
    assert len(read_table(lake, "lfmc_samples")) == 3
    for ds_id in ("synthetic-raster", "synthetic-table"):
        results = validate_collection(lake, load_dataset(ds_id))
        assert all(r.ok for r in results), [r.to_dict() for r in results if not r.ok]
    manifest = lake.read_json(rep.manifest_path)
    assert manifest["ok"] and manifest["run_id"] == rep.run_id
    types = [e["type"] for e in events.read_events(lake)]
    assert types == ["item.created", "item.created", "table.updated", "run.completed"]

    # second run: everything already cataloged -> no-op
    rep2 = pipeline.run(["synthetic-raster", "synthetic-table"], "tiny", PERIOD)
    assert rep2.ok and rep2.counts()["skipped"] == 2 and rep2.counts()["items"] == 0
    assert all(not d.units for d in rep2.datasets)
    assert [e["type"] for e in events.read_events(lake)][4:] == ["run.completed"]

    # force: reprocess, items are updated in place
    rep3 = pipeline.run(["synthetic-raster"], "tiny", PERIOD, force=True)
    assert rep3.datasets[0].units[0].status == "updated"
    assert catalog.item_ids(lake, "synthetic-raster") == ["synthetic-raster__tiny__20260601"]


def test_form_b_zarr_end_to_end(env):
    lake = env
    rep = pipeline.run(["synthetic-weather"], "tiny", PERIOD)
    assert rep.ok, rep.to_dict()
    item = catalog.get_item(lake, "synthetic-weather", "synthetic-weather__tiny__20260601")
    assert item["assets"]["data"]["type"] == "application/vnd+zarr"
    assert item["properties"]["start_datetime"] == "2026-06-01T00:00:00Z"
    results = validate_collection(lake, load_dataset("synthetic-weather"))
    assert all(r.ok and not r.warnings for r in results), [r.to_dict() for r in results]
    assert pipeline.run(["synthetic-weather"], "tiny", PERIOD).counts()["skipped"] == 1


def test_source_keys_are_scoped_per_aoi(test_config, lake):
    import yaml

    from conftest import TINY_AOI

    other = dict(TINY_AOI, id="tiny2", bbox=[-118.70, 34.05, -118.68, 34.07])
    (test_config / "aois" / "tiny2.yaml").write_text(yaml.safe_dump(other))
    pipeline.run(["synthetic-raster"], "tiny", PERIOD)
    rep = pipeline.run(["synthetic-raster"], "tiny2", PERIOD)
    assert rep.datasets[0].skipped == 0 and rep.datasets[0].units[0].status == "created"
    assert len(catalog.item_ids(lake, "synthetic-raster")) == 2


def test_failing_unit_is_isolated(env, monkeypatch):
    lake = env
    monkeypatch.setenv("EARTHDATA_TOKEN", "super-secret-token-123")
    rep = pipeline.run(["synthetic-flaky", "synthetic-raster"], "tiny", PERIOD)
    assert not rep.ok
    flaky, other = rep.datasets
    assert flaky.status == "partial" and [u.status for u in flaky.units] == ["created", "failed", "created"]
    assert "synthetic failure" in flaky.units[1].error
    assert other.status == "ok" and other.units[0].status == "created"
    assert len(catalog.item_ids(lake, "synthetic-flaky")) == 2
    assert events.read_events(lake)[-1]["type"] == "run.failed"
    # retry: only the failed unit is attempted again
    rep2 = pipeline.run(["synthetic-flaky"], "tiny", PERIOD)
    assert rep2.datasets[0].skipped == 2 and [u.key for u in rep2.datasets[0].units] == ["synthetic-flaky-20260602"]
    assert "super-secret-token-123" not in lake.read_text(rep.manifest_path)


def test_secret_in_error_is_redacted(test_config, lake, monkeypatch):
    import yaml

    path = test_config / "datasets" / "synthetic-flaky.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["options"]["error_text"] = "url=https://x?token=super-secret-token-123"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("EARTHDATA_TOKEN", "super-secret-token-123")
    rep = pipeline.run(["synthetic-flaky"], "tiny", PERIOD)
    err = rep.datasets[0].units[1].error
    assert "***" in err and "super-secret-token-123" not in err
    for rel in lake.find(""):
        if rel.endswith((".json", ".jsonl")):
            assert "super-secret-token-123" not in lake.read_text(rel)


def test_contract_violations_are_not_registered(env):
    lake = env
    rep = pipeline.run(["synthetic-badcrs", "synthetic-unmasked"], "tiny", PERIOD)
    assert not rep.ok
    for d in rep.datasets:
        assert d.status == "failed" and d.units[0].status == "invalid"
        assert catalog.item_ids(lake, d.dataset_id) == []
    assert "grid mismatch" in rep.datasets[0].units[0].error
    assert "outside valid_range" in rep.datasets[1].units[0].error


def test_no_connector_limit_and_unknown_dataset(env):
    rep = pipeline.run(["synthetic-stub"], "tiny", PERIOD)
    assert rep.ok and rep.datasets[0].status == "no-connector"
    rep = pipeline.run(["synthetic-flaky"], "tiny", PERIOD, limit=1)
    assert [u.key for u in rep.datasets[0].units] == ["synthetic-flaky-20260601"]
    rep = pipeline.run(["does-not-exist"], "tiny", PERIOD)
    assert not rep.ok and rep.datasets[0].status == "failed"


def test_discover_only(env):
    pipeline.run(["synthetic-flaky"], "tiny", PERIOD, limit=1)
    found = pipeline.discover(["synthetic-flaky"], "tiny", PERIOD)
    assert [known for _, known in found["synthetic-flaky"]] == [True, False, False]


def _add_dataset(test_config, ds_id: str, **overrides) -> None:
    import yaml

    from conftest import SYNTHETIC_DATASETS

    cfg = {**SYNTHETIC_DATASETS["synthetic-raster"], "id": ds_id, "title": ds_id, **overrides}
    (test_config / "datasets" / f"{ds_id}.yaml").write_text(yaml.safe_dump(cfg))


EARTHDATA = {"access": {"method": "none", "auth": "earthdata", "env": ["EARTHDATA_TOKEN"]}}


def test_limit_counts_processed_units_only(env, test_config):
    # units the connector's cache marks as empty are recorded but do not use up --limit
    cached = ["synthetic-cached-20260601", "synthetic-cached-20260602"]
    _add_dataset(test_config, "synthetic-cached", options={"n_units": 4, "cached_empty_keys": cached})
    rep = pipeline.run(["synthetic-cached"], "tiny", PERIOD, limit=1)
    d = rep.datasets[0]
    assert rep.ok and [(u.key[-4:], u.status) for u in d.units] == [("0601", "empty"), ("0602", "empty"),
                                                                     ("0603", "created")]
    assert d.units[0].reason.startswith("cached")
    rep = pipeline.run(["synthetic-cached"], "tiny", PERIOD, limit=1)       # 0603 is cataloged now
    d = rep.datasets[0]
    assert d.skipped == 1 and [(u.key[-4:], u.status) for u in d.units][-1] == ("0604", "created")
    assert catalog.item_ids(env, "synthetic-cached") == ["synthetic-cached__tiny__20260603",
                                                         "synthetic-cached__tiny__20260604"]
    assert pipeline.run(["synthetic-cached"], "tiny", PERIOD, limit=0, force=True).datasets[0].units == []


def test_lenient_mode_skips_stubs_and_units_without_credentials(env, test_config):
    lake = env
    _add_dataset(test_config, "synthetic-earthdata", options={"n_units": 2}, **EARTHDATA)
    _add_dataset(test_config, "synthetic-stubbed", status="stub", options={"fail_keys": ["synthetic-stubbed-20260601"]})
    rep = pipeline.run(["synthetic-raster", "synthetic-earthdata", "synthetic-stubbed"], "tiny", PERIOD,
                       skip_stubs=True, skip_missing_credentials=True)
    assert rep.ok, rep.to_dict()
    raster, earthdata, stub = rep.datasets
    assert raster.status == "ok" and raster.count("created") == 1
    assert earthdata.status == "skipped" and earthdata.missing_credentials == ["EARTHDATA_TOKEN"]
    assert [u.status for u in earthdata.units] == ["missing_credentials", "missing_credentials"]
    assert earthdata.reason == "skipped: missing credentials (EARTHDATA_TOKEN) for 2 unit(s)"
    assert stub.status == "skipped" and stub.reason.startswith("skipped: stub") and stub.discovered == 0
    assert rep.counts()["missing_credentials"] == 2 and rep.counts()["failed"] == 0
    assert catalog.item_ids(lake, "synthetic-earthdata") == []
    assert events.read_events(lake)[-1]["type"] == "run.completed"
    assert lake.read_json(rep.manifest_path)["skip_missing_credentials"] is True
    # retried by the next run (nothing was cataloged), and named explicitly it fails loudly
    rep = pipeline.run(["synthetic-earthdata", "synthetic-stubbed"], "tiny", PERIOD)
    earthdata, stub = rep.datasets
    assert not rep.ok and earthdata.status == "failed" and "EARTHDATA_TOKEN is not set" in earthdata.units[0].error
    assert stub.status == "failed" and stub.units[0].status == "failed"   # a stub with a connector runs when named


def test_lenient_mode_runs_units_once_credentials_exist(env, test_config, monkeypatch):
    lake = env
    _add_dataset(test_config, "synthetic-earthdata", **EARTHDATA)
    monkeypatch.setenv("EARTHDATA_TOKEN", "tok-very-secret-42")
    rep = pipeline.run(["synthetic-earthdata"], "tiny", PERIOD, skip_missing_credentials=True)
    assert rep.ok and rep.datasets[0].status == "ok" and rep.datasets[0].count("created") == 1
    assert "tok-very-secret-42" not in lake.read_text(rep.manifest_path)


def test_connector_item_redistributable_is_kept(env, monkeypatch):
    # the dataset licence says redistributable; a connector may restrict one item (e.g. a mixed-source table)
    from synthetic_connector import SyntheticConnector

    lake = env
    orig = SyntheticConnector.transform

    def transform(self, ref, raw, aoi):
        items = orig(self, ref, raw, aoi)
        for it in items:
            it.properties["fuellake:redistributable"] = False
        return items

    monkeypatch.setattr(SyntheticConnector, "transform", transform)
    assert pipeline.run(["synthetic-raster"], "tiny", PERIOD).ok
    props = catalog.get_item(lake, "synthetic-raster", "synthetic-raster__tiny__20260601")["properties"]
    assert props["fuellake:redistributable"] is False and props["fuellake:license"] == "CC0-1.0"
    assert all(r.ok for r in validate_collection(lake, load_dataset("synthetic-raster")))
    # without a connector value the dataset licence value applies
    monkeypatch.setattr(SyntheticConnector, "transform", orig)
    assert pipeline.run(["synthetic-raster"], "tiny", PERIOD, force=True).ok
    props = catalog.get_item(lake, "synthetic-raster", "synthetic-raster__tiny__20260601")["properties"]
    assert props["fuellake:redistributable"] is True
