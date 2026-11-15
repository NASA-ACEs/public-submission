# fuel-lake — tests for publishing System A / B outputs (LakeClient.publish_raster / publish_table)
# Licensed under Apache-2.0.
"""Round trips publish -> search -> open in direct-local and direct-S3 (moto) mode, discovery through the HTTP
API, the read-only error in HTTP mode, and the refusals (wrong grid, out-of-range value, missing variable or
property, non-interface dataset)."""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from fixtures.services.demo_lake import serve
from fuellake import catalog, events, pipeline
from fuellake.catalog import resolve_href
from fuellake.client import LakeClient, ReadOnlyClientError
from fuellake.connectors.base import Period
from fuellake.contracts import validate_collection
from fuellake.publish import PublishError
from fuellake.registry import lake_region, load_aoi, load_dataset
from fuellake.services.api import create_app
from fuellake.storage import Lake

REPO = Path(__file__).resolve().parents[1]
PERIOD = Period(date(2026, 6, 1), date(2026, 6, 30))
RISK_POINTS = {
    "id": "risk-points", "title": "Test interface table", "kind": "table", "table": "risk_points",
    "connector": None, "status": "interface", "scales": ["local", "region"],
    "license": {"id": "unknown", "redistributable": False},
    "item_properties": [{"name": "risk_points:model_version", "type": "string", "required": True}],
}


def _copy_interfaces(config: Path) -> None:
    for ds_id in ("fuel-state", "spread-risk"):
        shutil.copy(REPO / "config" / "datasets" / f"{ds_id}.yaml", config / "datasets" / f"{ds_id}.yaml")
    (config / "datasets" / "risk-points.yaml").write_text(yaml.safe_dump(RISK_POINTS))


@pytest.fixture(params=["local", "s3"])
def pub(request, test_config, monkeypatch):
    """``(client, lake, source item)``: the real interface configs, a synthetic source item, each direct mode."""
    monkeypatch.delenv("FUELLAKE_API_URL", raising=False)
    _copy_interfaces(test_config)
    lake: Lake = request.getfixturevalue("s3_lake" if request.param == "s3" else "lake")
    assert pipeline.run(["synthetic-raster"], "tiny", PERIOD, lake=lake).ok
    client = LakeClient.from_env() if request.param == "s3" else LakeClient(lake)
    (src,) = client.search(collections=["synthetic-raster"], aoi="tiny")
    yield client, lake, src


def fuel_arrays(shape, lfmc: float = 90.0, **extra) -> dict:
    out = {"fuel_load": np.full(shape, 2.5, "float32"), "lfmc": np.full(shape, lfmc, "float32"),
           "spread_potential": np.full(shape, 0.4, "float32")}
    out["fuel_load"][0, 0] = np.nan                      # nodata is fine
    out.update(extra)
    return out


def risk_arrays(shape) -> dict:
    cls = np.full(shape, 3, "uint8")
    cls[0, :] = 0                                        # nodata row
    return {"risk_index": np.full(shape, 0.55, "float32"), "risk_class": cls}


RISK_PROPS = {"valid_from": "2026-06-02T00:00:00Z", "valid_to": "2026-06-04T23:59:59Z",
              "model_version": "spreadrisk-0.1", "weather_source": "gridmet"}


# ---------------------------------------------------------------------------- round trips
def test_publish_search_open_round_trip(pub):
    client, lake, src = pub
    shape = load_aoi("tiny").grid.shape
    arrays = fuel_arrays(shape, lfmc_p05=np.full(shape, 70.0))
    fs = client.publish_raster("fuel-state", "tiny", date(2026, 6, 1), arrays,
                               properties={"model_version": "fuelstate-gbm-0.3"}, source_items=[src])
    assert fs["id"] == "fuel-state__tiny__20260601" and fs["collection"] == "fuel-state"
    p = fs["properties"]
    assert p["fuel_state:model_version"] == "fuelstate-gbm-0.3"
    assert p["fuellake:source_items"] == [src["id"]] == p["fuellake:source_ids"]
    assert p["fuellake:status"] == "interface" and p["fuellake:run_id"].startswith("publish-")
    assert set(fs["assets"]) == {"fuel_load", "lfmc", "spread_potential", "lfmc_p05"}
    (link,) = [lk for lk in fs["links"] if lk["rel"] == "derived_from"]
    assert resolve_href(catalog.item_rel("fuel-state", fs["id"]), link["href"]) == \
        catalog.item_rel(src["collection"], src["id"])

    (found,) = client.search(collections=["fuel-state"], aoi="tiny", start="2026-06-01", end="2026-06-01")
    assert found == fs
    lfmc = client.open_asset(found, "lfmc")
    assert lfmc.shape == shape and float(lfmc.mean()) == pytest.approx(90.0) and lfmc.rio.crs.to_epsg() == 32611
    assert np.isnan(client.open_asset(found, "fuel_load").values[0, 0])

    # System B reads System A's item and publishes a risk map that points back to it
    sr = client.publish_raster("spread-risk", "tiny", datetime(2026, 6, 1, 18, 0, tzinfo=UTC), risk_arrays(shape),
                               properties=RISK_PROPS, source_items=[found])
    assert sr["id"] == "spread-risk__tiny__20260601__180000"
    q = sr["properties"]
    assert q["spread_risk:fuel_state_item"] == fs["id"] and q["fuellake:source_items"] == [fs["id"]]
    assert (q["start_datetime"], q["end_datetime"]) == ("2026-06-02T00:00:00Z", "2026-06-04T23:59:59Z")
    assert q["spread_risk:weather_source"] == "gridmet"
    assert sr["assets"]["risk_class"]["raster:bands"][0] == {"data_type": "uint8", "nodata": 0}
    assert [i["id"] for i in client.search(collections=["spread-risk"], start="2026-06-03", end="2026-06-03")] == \
        [sr["id"]]                                               # found by its validity window
    assert client.search(collections=["spread-risk"], start="2026-06-10", end="2026-06-11") == []
    cls = client.open_asset(sr, "risk_class")
    assert cls.dtype == np.uint8 and int(cls.values[0, 0]) == 0 and int(cls.values[1, 1]) == 3

    for ds_id in ("fuel-state", "spread-risk"):
        results = validate_collection(lake, load_dataset(ds_id))
        assert all(r.ok for r in results), [r.to_dict() for r in results if not r.ok]
    evs = [e for e in client.events() if e.get("origin") == "publish"]
    assert [(e["type"], e["collection"]) for e in evs] == [("item.created", "fuel-state"),
                                                           ("item.created", "spread-risk")]

    # the same dataset, AOI and datetime again: replaced in place, without the optional layer
    again = client.publish_raster("fuel-state", "tiny", "2026-06-01", fuel_arrays(shape, lfmc=95.0),
                                  properties={"model_version": "fuelstate-gbm-0.4"}, source_items=[src["id"]])
    assert set(again["assets"]) == {"fuel_load", "lfmc", "spread_potential"}
    assert catalog.item_ids(lake, "fuel-state") == [fs["id"]]
    assert not lake.exists(f"staged/fuel-state/tiny/{fs['id']}/lfmc_p05.tif")
    assert client.events(types=["item.updated"])[-1]["item_id"] == fs["id"]
    assert float(client.open_asset(client.get_item("fuel-state", fs["id"]), "lfmc").mean()) == pytest.approx(95.0)
    assert not [p for p in lake.find("staged") if "/.publish-" in p]       # no temporary files left


def test_publish_accepts_dataarrays_on_the_aoi_grid(pub):
    client, lake, src = pub
    sm = client.open_asset(src, "soil_moisture")                # a DataArray on the tiny grid
    arrays = {"fuel_load": sm * 0 + 1.0, "lfmc": (sm * 100).clip(0, 400), "spread_potential": sm.clip(0, 1)}
    item = client.publish_raster("fuel-state", "tiny", "2026-06-02", arrays, properties={"model_version": "a"},
                                 source_items=[src])
    assert item["id"] == "fuel-state__tiny__20260602"


def test_publish_table_round_trip(pub):
    client, lake, src = pub
    df = pd.DataFrame({"lon": [-118.795, -118.785], "lat": [34.055, 34.065], "risk": [0.2, 0.8],
                       "date": [date(2026, 6, 1), date(2026, 6, 2)]})
    item = client.publish_table("risk-points", "tiny", df, "run-1", properties={"model_version": "pts-0.1"},
                                source_items=[src])
    assert item["id"] == "risk-points__tiny__20260601__run-1" and item["properties"]["fuellake:n_rows"] == 2
    assert item["properties"]["end_datetime"].startswith("2026-06-02T23:59:59")
    out = client.read_table("risk_points")
    assert sorted(out["risk"]) == [0.2, 0.8] and out.crs.to_epsg() == 4326
    # same key, other dates: still one item (replaced), one part
    df2 = df.assign(date=[date(2026, 7, 1), date(2026, 7, 1)], risk=[0.3, 0.4])
    item2 = client.publish_table("risk-points", "tiny", df2, "run-1", properties={"model_version": "pts-0.2"})
    assert catalog.item_ids(lake, "risk-points") == [item2["id"]] == ["risk-points__tiny__20260701__run-1"]
    assert sorted(client.read_table("risk_points")["risk"]) == [0.3, 0.4]
    types = [e["type"] for e in client.events() if e.get("collection") == "risk-points"]
    assert types == ["item.created", "table.updated", "item.created", "table.updated"]
    assert all(r.ok for r in validate_collection(lake, load_dataset("risk-points")))


# ---------------------------------------------------------------------------- refusals
@pytest.fixture
def local_pub(test_config, lake, monkeypatch):
    monkeypatch.delenv("FUELLAKE_API_URL", raising=False)
    _copy_interfaces(test_config)
    assert pipeline.run(["synthetic-raster"], "tiny", PERIOD, lake=lake).ok
    client = LakeClient(lake)
    (src,) = client.search(collections=["synthetic-raster"])
    return client, lake, src


def test_refusals_leave_the_lake_untouched(local_pub):
    client, lake, src = local_pub
    grid = load_aoi("tiny").grid
    h, w = grid.shape
    ok_props = {"model_version": "a"}

    def refuse(match, dataset="fuel-state", arrays=None, props=ok_props, sources=(src,), when="2026-06-01"):
        with pytest.raises(PublishError, match=match):
            client.publish_raster(dataset, "tiny", when, fuel_arrays((h, w)) if arrays is None else arrays,
                                  properties=props, source_items=list(sources))

    refuse("does not match the AOI tiny grid", arrays=fuel_arrays((h + 1, w)))          # wrong grid (shape)
    sm = client.open_asset(src, "soil_moisture")
    shifted = sm.assign_coords(x=sm.x + 30.0)
    refuse("grid mismatch", arrays={**fuel_arrays((h, w)), "lfmc": shifted * 0 + 80})    # wrong grid (transform)
    refuse(r"lfmc: 1 values outside valid_range \[0, 400\]",
           arrays={**fuel_arrays((h, w)), "lfmc": np.where(np.arange(h * w).reshape(h, w) == 5, 500.0, 90.0)})
    refuse("infinite", arrays={**fuel_arrays((h, w)), "spread_potential": np.full((h, w), np.inf)})
    no_sp = fuel_arrays((h, w))
    del no_sp["spread_potential"]
    refuse(r"missing required variables \['spread_potential'\]", arrays=no_sp)
    refuse("unknown variables", arrays={**fuel_arrays((h, w)), "ndvi": np.zeros((h, w))})
    refuse("missing required property fuel_state:model_version", props={})
    refuse("missing required property fuellake:source_items", sources=())
    refuse("is set by fuel-lake", props={"model_version": "a", "fuellake:aoi": "smm"})
    refuse("not in the catalog", sources=("synthetic-raster__tiny__19990101",))
    refuse("not an interface collection", dataset="synthetic-raster",
           arrays={"soil_moisture": np.zeros((h, w))})

    bad_cls = risk_arrays((h, w))
    bad_cls["risk_class"][2, 2] = 7
    fs = client.publish_raster("fuel-state", "tiny", "2026-06-01", fuel_arrays((h, w)), properties=ok_props,
                               source_items=[src])
    refuse(r"risk_class: 1 values outside valid_range \[1, 5\]", dataset="spread-risk", arrays=bad_cls,
           props=RISK_PROPS, sources=(fs,))
    refuse("missing required property spread_risk:fuel_state_item", dataset="spread-risk",
           arrays=risk_arrays((h, w)), props=RISK_PROPS, sources=(src,))
    refuse("ends before it starts", dataset="spread-risk", arrays=risk_arrays((h, w)),
           props={**RISK_PROPS, "valid_to": "2026-06-01T00:00:00Z"}, sources=(fs,))
    refuse("is not a valid datetime", dataset="spread-risk", arrays=risk_arrays((h, w)),
           props={**RISK_PROPS, "valid_from": "tomorrow"}, sources=(fs,))

    # a refused republish of fs keeps the published version intact
    refuse("outside valid_range", arrays={**fuel_arrays((h, w)), "lfmc": np.full((h, w), -1.0)})
    assert catalog.item_ids(lake, "fuel-state") == [fs["id"]] and catalog.item_ids(lake, "spread-risk") == []
    assert float(client.open_asset(client.get_item("fuel-state", fs["id"]), "lfmc").mean()) == pytest.approx(90.0)
    assert not lake.exists("staged/spread-risk")
    assert not [p for p in lake.find("staged") if "/.publish-" in p]
    with pytest.raises(PublishError, match="cannot date the table"):
        client.publish_table("risk-points", "tiny", pd.DataFrame({"lon": [-118.79], "lat": [34.06]}), "k",
                             properties={"model_version": "x"})


def test_http_mode_finds_published_items_but_is_read_only(local_pub):
    client, lake, src = local_pub
    shape = load_aoi("tiny").grid.shape
    fs = client.publish_raster("fuel-state", "tiny", "2026-06-01", fuel_arrays(shape),
                               properties={"model_version": "a"}, source_items=[src])
    with serve(create_app(lake)) as url, LakeClient(api_url=url) as http:
        (found,) = http.search(collections=["fuel-state"], aoi="tiny")
        assert found["id"] == fs["id"]
        assert float(http.open_asset(found, "spread_potential").mean()) == pytest.approx(0.4)
        with pytest.raises(ReadOnlyClientError, match="read-only.*direct mode"):
            http.publish_raster("fuel-state", "tiny", "2026-06-02", fuel_arrays(shape),
                                properties={"model_version": "a"}, source_items=[src])
        with pytest.raises(ReadOnlyClientError):
            http.publish_table("risk-points", "tiny", pd.DataFrame({"lon": [0.0], "lat": [0.0]}), "k")


def test_published_items_are_clipped_to_the_region(local_pub, region_config):
    client, lake, src = local_pub
    shape = load_aoi("tiny").grid.shape
    fs = client.publish_raster("fuel-state", "tiny", "2026-06-01", fuel_arrays(shape),
                               properties={"model_version": "a"}, source_items=[src])
    assert fs["properties"]["fuellake:region"] == "testland"
    assert fs["properties"]["fuellake:region_clip"]["masked_pixels"] > 0
    arr = client.open_asset(fs, "lfmc").values
    inside = lake_region().pixel_mask(load_aoi("tiny").grid)
    assert np.isnan(arr[~inside]).all() and (arr[inside] == 90.0).all()
    df = pd.DataFrame({"lon": [-118.795, -118.785], "lat": [34.06, 34.06], "datetime": ["2026-06-01T00:00Z"] * 2})
    item = client.publish_table("risk-points", "tiny", df, "clip", properties={"model_version": "x"})
    assert item["properties"]["fuellake:n_rows"] == 1                        # the western row is outside
    assert all(r.ok for r in validate_collection(lake, load_dataset("fuel-state")))


def test_interface_collections_do_not_feed_the_cube(local_pub):
    from fuellake.features._common import discover_inputs

    client, lake, src = local_pub
    shape = load_aoi("tiny").grid.shape
    client.publish_raster("fuel-state", "tiny", "2026-06-01", fuel_arrays(shape), properties={"model_version": "a"},
                          source_items=[src])
    inputs = discover_inputs(lake, load_aoi("tiny"), date(2026, 6, 1), date(2026, 6, 30))
    assert {v.dataset for v in inputs.variables} == {"synthetic-raster"}
    named = discover_inputs(lake, load_aoi("tiny"), date(2026, 6, 1), date(2026, 6, 30), datasets=["fuel-state"])
    assert {v.dataset for v in named.variables} == {"fuel-state"}
    assert [e["type"] for e in events.read_events(lake) if e.get("origin") == "publish"] == ["item.created"]
