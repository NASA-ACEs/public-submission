# fuel-lake — tests for the static STAC catalog (incremental writes, relative hrefs, search)
# Licensed under Apache-2.0.
from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime

import pystac
import pytest

from fuellake import catalog
from fuellake.registry import DatasetConfig

DS = DatasetConfig(
    id="nisar-sme2", title="NISAR SME2", kind="raster-a", status="search-only", provider="ASF",
    homepage="https://example.org", license={"id": "other", "url": "https://example.org/terms",
                                             "redistributable": False},
    maturity="PROVISIONAL",
    variables=[{"name": "soil_moisture", "unit": "m3/m3", "dtype": "float32", "nodata": "NaN",
                "valid_range": [0, 0.7]}],
)


def _item(day: int, aoi="smm", maturity="PROVISIONAL", key=None, bbox=(-118.95, 33.98, -118.5, 34.2)):
    iid = f"nisar-sme2__{aoi}__202606{day:02d}"
    return catalog.make_item(
        "nisar-sme2", iid, datetime(2026, 6, day, 13, 0, tzinfo=UTC),
        {"fuellake:aoi": aoi, "fuellake:maturity": maturity, "fuellake:source_key": key or f"G{day}"},
        {"soil_moisture": {"path": f"staged/nisar-sme2/{aoi}/{iid}/soil_moisture.tif",
                           "type": "image/tiff", "roles": ["data"]}},
        bbox,
    )


def test_collection_and_items_roundtrip(lake):
    catalog.upsert_collection(lake, DS)
    rel, created = catalog.add_item(lake, _item(3))
    assert created and rel == "catalog/nisar-sme2/nisar-sme2__smm__20260603/nisar-sme2__smm__20260603.json"
    item = catalog.get_item(lake, "nisar-sme2", "nisar-sme2__smm__20260603")
    staged = "staged/nisar-sme2/smm/nisar-sme2__smm__20260603/soil_moisture.tif"
    assert item["assets"]["soil_moisture"]["href"] == "../../../" + staged
    assert catalog.asset_rel(item, "soil_moisture") == staged
    coll = catalog.get_collection(lake, "nisar-sme2")
    assert coll["license"] == "other" and coll["fuellake:license"]["id"] == "other"
    assert coll["extent"]["spatial"]["bbox"][0] == [-118.95, 33.98, -118.5, 34.2]
    assert coll["extent"]["temporal"]["interval"][0] == ["2026-06-03T13:00:00Z", "2026-06-03T13:00:00Z"]
    assert [c["id"] for c in catalog.list_collections(lake)] == ["nisar-sme2"]


def test_incremental_and_idempotent_add(lake):
    catalog.upsert_collection(lake, DS)
    rel1, _ = catalog.add_item(lake, _item(3))
    before = lake.read_bytes(rel1)
    catalog.add_item(lake, _item(15))
    assert lake.read_bytes(rel1) == before  # other items are not rewritten
    _, created = catalog.add_item(lake, _item(15))
    assert created is False
    coll = catalog.get_collection(lake, "nisar-sme2")
    assert sum(1 for link in coll["links"] if link["rel"] == "item") == 2
    assert coll["extent"]["temporal"]["interval"][0] == ["2026-06-03T13:00:00Z", "2026-06-15T13:00:00Z"]
    catalog.upsert_collection(lake, DS)  # refresh keeps item links
    assert catalog.item_ids(lake, "nisar-sme2") == ["nisar-sme2__smm__20260603", "nisar-sme2__smm__20260615"]
    assert catalog.source_keys(lake, "nisar-sme2") == {"G3": ["nisar-sme2__smm__20260603"],
                                                       "G15": ["nisar-sme2__smm__20260615"]}
    assert [i["id"] for i in catalog.find_by_source_key(lake, "nisar-sme2", "G15")] == ["nisar-sme2__smm__20260615"]
    assert catalog.remove_item(lake, "nisar-sme2", "nisar-sme2__smm__20260615")
    assert catalog.item_ids(lake, "nisar-sme2") == ["nisar-sme2__smm__20260603"]


def test_relative_hrefs_and_pystac_reads_copied_catalog(lake, tmp_path):
    catalog.upsert_collection(lake, DS)
    for d in (3, 15, 27):
        catalog.add_item(lake, _item(d))
    for rel in lake.find("catalog"):
        text = lake.read_text(rel)
        assert lake.root not in text and "file://" not in text
        for link in json.loads(text).get("links", []):
            if link["rel"] in ("root", "parent", "child", "item", "collection"):
                assert link["href"].startswith(("./", "../"))
    copy = tmp_path / "elsewhere"
    shutil.copytree(lake.url(""), copy)
    cat = pystac.Catalog.from_file(str(copy / "catalog" / "catalog.json"))
    items = list(cat.get_items(recursive=True))
    assert len(items) == 3
    href = items[0].assets["soil_moisture"].get_absolute_href()
    assert href.startswith(str(copy)) and "/staged/nisar-sme2/smm/" in href


def test_search_filters(lake):
    catalog.upsert_collection(lake, DS)
    catalog.add_item(lake, _item(3, maturity="BETA"))
    catalog.add_item(lake, _item(15))
    catalog.add_item(lake, _item(27, aoi="other", bbox=(-120.0, 35.0, -119.5, 35.5)))
    ids = lambda items: [i["id"][-8:] for i in items]  # noqa: E731
    assert ids(catalog.search(lake)) == ["20260603", "20260615", "20260627"]
    assert ids(catalog.search(lake, ["nisar-sme2"], aoi="smm")) == ["20260603", "20260615"]
    assert ids(catalog.search(lake, bbox=(-119.8, 35.1, -119.7, 35.2))) == ["20260627"]
    assert ids(catalog.search(lake, start=date(2026, 6, 10), end=date(2026, 6, 15))) == ["20260615"]
    assert ids(catalog.search(lake, datetime_range="2026-06-16/..")) == ["20260627"]
    assert ids(catalog.search(lake, query={"fuellake:maturity": "PROVISIONAL"})) == ["20260615", "20260627"]
    assert ids(catalog.search(lake, query={"fuellake:maturity": {"in": ["BETA"]}})) == ["20260603"]
    assert ids(catalog.search(lake, limit=1)) == ["20260603"]
    assert catalog.search(lake, ["missing-collection"]) == []
    with pytest.raises(ValueError):
        catalog.search(lake, query={"fuellake:maturity": {"like": "PROV"}})


def test_add_item_requires_collection(lake):
    with pytest.raises(KeyError):
        catalog.add_item(lake, _item(3))


def test_datetime_helpers():
    assert catalog.parse_datetime_interval("2026-06-01")[1] == datetime(2026, 6, 1, 23, 59, 59, 999999, tzinfo=UTC)
    assert catalog.parse_datetime_interval("../2026-06-01T00:00:00Z")[0] is None
    assert catalog.iso("2026-06-01T12:00:00+02:00") == "2026-06-01T10:00:00Z"
    with pytest.raises(ValueError):
        catalog.relative_href("catalog/catalog.json", "s3://bucket/x")


def test_static_collections_match_any_period(lake):
    # cop-dem is dated 2021 and landfire-fbfm40 2025, but both are time-invariant layers
    dem = DatasetConfig(id="cop-dem", title="DEM", kind="raster-a", license={"id": "other"},
                        cadence="static (Copernicus DEM 2021 release)")
    biomass = DatasetConfig(id="biomass", title="biomass drops", kind="raster-a", license={"id": "other"},
                            cadence="on drop")
    globe = DatasetConfig(id="globe-lfmc", title="labels", kind="table", table="lfmc_samples",
                          license={"id": "CC0-1.0"}, cadence="static (published 2024-03-25; samples 1977-2023)")
    assert dem.is_static and not biomass.is_static and not DS.is_static and not globe.is_static
    for ds, day in ((dem, date(2021, 4, 22)), (biomass, date(2025, 8, 1))):
        coll = catalog.upsert_collection(lake, ds)
        assert coll["fuellake:static"] is ds.is_static
        iid = f"{ds.id}__smm__{day:%Y%m%d}"
        catalog.add_item(lake, catalog.make_item(ds.id, iid, day, {"fuellake:aoi": "smm"},
                                                 {"elevation": {"path": f"staged/{ds.id}/x.tif", "roles": ["data"]}},
                                                 (-118.95, 33.98, -118.5, 34.2)))
    catalog.upsert_collection(lake, DS)
    catalog.add_item(lake, _item(15))
    found = catalog.search(lake, aoi="smm", start=date(2026, 6, 1), end=date(2026, 6, 30))
    assert [i["collection"] for i in found] == ["cop-dem", "nisar-sme2"]          # dated biomass drop: no
    assert [i["collection"] for i in catalog.search(lake, datetime_range="2026-06-01/2026-06-30")] == \
        ["cop-dem", "nisar-sme2"]
    strict = catalog.search(lake, start=date(2026, 6, 1), end=date(2026, 6, 30), static_any_time=False)
    assert [i["collection"] for i in strict] == ["nisar-sme2"]
