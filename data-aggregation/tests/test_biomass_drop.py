# fuel-lake — tests for the biomass drop-zone connector (sidecar schema, synthetic drops, reprojection)
# Licensed under Apache-2.0.
from __future__ import annotations

import copy
import json
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml
from affine import Affine

from conftest import TINY_AOI
from fuellake import catalog, pipeline
from fuellake.connectors.base import Period
from fuellake.connectors.biomass_drop import BiomassSidecar, parse_sidecar
from fuellake.contracts import validate_collection
from fuellake.rasters import read_cog
from fuellake.registry import load_aoi, load_dataset

REPO = Path(__file__).resolve().parents[1]
SCHEMA = REPO / "config" / "schemas" / "biomass_drop.schema.json"
EXAMPLE = Path(__file__).parent / "fixtures" / "open_rasters" / "biomass_sidecar_example.json"
DS = "biomass"
PERIOD = Period(date(2026, 6, 1), date(2026, 11, 14))


@pytest.fixture
def bio_config(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    shutil.copy(REPO / "config" / "datasets" / f"{DS}.yaml", root / "datasets" / f"{DS}.yaml")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


def _example() -> dict:
    return json.loads(EXAMPLE.read_text())


# ---------------------------------------------------------------------------- sidecar validation
def _mutate(**changes):
    d = _example()
    for k, v in changes.items():
        if v is _DROP:
            d.pop(k)
        else:
            d[k] = v
    return d


_DROP = object()
INVALID = {
    "missing_date": _mutate(date=_DROP),
    "missing_producer": _mutate(producer=_DROP),
    "bad_unit": _mutate(unit="kg/ha"),
    "fresh_mass": _mutate(mass_basis="fresh"),
    "live_with_dead_class": _mutate(fuel_size_classes=["1h", "live_woody"]),
    "dead_with_live_class": _mutate(component="dead", fuel_size_classes=["10h", "live_herbaceous"]),
    "all_combined": _mutate(component="both", fuel_size_classes=["all", "1h"]),
    "empty_classes": _mutate(fuel_size_classes=[]),
    "repeated_class": _mutate(fuel_size_classes=["live_woody", "live_woody"]),
    "unknown_field": _mutate(colour="green"),
    "path_in_file": _mutate(file="../elsewhere/x.tif"),
    "not_tiff": _mutate(file="x.nc"),
    "bad_crs": _mutate(crs="32611"),
    "bad_date_format": _mutate(date="15/08/2026"),
    "wrong_version": _mutate(schema_version="2.0"),
    "wrong_variable": _mutate(variable="fuel_load"),
    "bad_uncertainty_kind": _mutate(uncertainty={"file": "sd.tif", "kind": "variance"}),
    "band_zero": _mutate(band=0),
    "string_band": _mutate(band="1"),
}
VALID = {
    "example": _example(),
    "minimal_dead_mgha": {k: v for k, v in _mutate(component="dead", unit="Mg/ha",
                                                   fuel_size_classes=["1h", "10h", "100h", "litter"]).items()
                          if k not in ("band", "nodata", "uncertainty", "inputs", "license", "notes")},
    "both_all": _mutate(component="both", fuel_size_classes=["all"]),
    "null_nodata": _mutate(nodata=None),
}
RUNTIME_ONLY = {   # rules the JSON Schema cannot express; enforced by the pydantic model only
    "calendar_invalid_date": _mutate(date="2026-02-30"),
    "uncertainty_same_file": _mutate(uncertainty={"file": "smm_live_20260815.tif", "kind": "std"}),
}


@pytest.mark.parametrize("name", sorted(VALID))
def test_valid_sidecars(name):
    sc = parse_sidecar(json.dumps(VALID[name]))
    assert isinstance(sc, BiomassSidecar)


@pytest.mark.parametrize("name", sorted(INVALID) + sorted(RUNTIME_ONLY))
def test_invalid_sidecars(name):
    with pytest.raises(ValueError, match="sidecar"):
        parse_sidecar(json.dumps({**INVALID, **RUNTIME_ONLY}[name]))


def test_not_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_sidecar(b"{not json")


def test_json_schema_agrees_with_runtime_model():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    v = jsonschema.Draft202012Validator(schema)
    for name, doc in VALID.items():
        assert not list(v.iter_errors(doc)), name
    for name, doc in INVALID.items():
        assert list(v.iter_errors(doc)), f"schema accepts invalid sidecar {name}"
    # both sides know the same top-level fields and required set
    assert set(schema["properties"]) == set(BiomassSidecar.model_fields)
    required = {n for n, f in BiomassSidecar.model_fields.items() if f.is_required()}
    assert set(schema["required"]) == required


# ---------------------------------------------------------------------------- synthetic drops
def _tif(path: Path, arr: np.ndarray, crs: str, transform: Affine, nodata=None):
    with rasterio.open(path, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
                       dtype=arr.dtype, crs=crs, transform=transform, nodata=nodata) as dst:
        dst.write(arr, 1)


def _sidecar(**kw) -> dict:
    d = {"schema_version": "1.0", "aoi": "tiny", "variable": "biomass", "unit": "kg/m2", "mass_basis": "dry",
         "date": "2026-08-15", "component": "live", "fuel_size_classes": ["live_herbaceous", "live_woody"],
         "crs": "EPSG:32611", "producer": {"name": "synthetic", "version": "0.1.0"}}
    d.update(kw)
    return d


def _drop_utm_10m(inbox: Path, name="live_utm", value=25.0, unit="Mg/ha", **kw) -> Path:
    """10 m EPSG:32611 drop covering the tiny AOI, constant ``value`` with a negative patch and nodata."""
    g = load_aoi("tiny").grid
    minx, miny, maxx, maxy = g.bounds()
    t = Affine(10, 0, minx - 200, 0, -10, maxy + 200)
    w, h = int((maxx - minx + 400) / 10), int((maxy - miny + 400) / 10)
    arr = np.full((h, w), value, "float32")
    arr[h // 2: h // 2 + 30, w // 2: w // 2 + 30] = -50.0          # implausible -> masked by valid_range
    arr[:15, :] = -9999.0                                           # nodata rows (outside the AOI pad)
    _tif(inbox / f"{name}.tif", arr, "EPSG:32611", t, nodata=-9999.0)
    _tif(inbox / f"{name}_sd.tif", np.full((h, w), value / 10, "float32"), "EPSG:32611", t, nodata=-9999.0)
    sc = _sidecar(file=f"{name}.tif", unit=unit, uncertainty={"file": f"{name}_sd.tif", "kind": "std"}, **kw)
    (inbox / f"{name}.json").write_text(json.dumps(sc))
    return inbox / f"{name}.json"


def _drop_wgs84_coarse(inbox: Path, name="dead_geo") -> Path:
    """~250 m EPSG:4326 drop with a west-east gradient (upsampled with bilinear)."""
    res = 0.0025
    t = Affine(res, 0, -118.83, 0, -res, 34.10)
    arr = np.tile(np.linspace(1.0, 3.0, 28, dtype="float32"), (24, 1))
    _tif(inbox / f"{name}.tif", arr, "EPSG:4326", t)
    sc = _sidecar(file=f"{name}.tif", component="dead", fuel_size_classes=["1h", "10h", "100h"], crs="EPSG:4326",
                  producer={"name": "synthetic", "version": "0.2.0"})
    (inbox / f"{name}.json").write_text(json.dumps(sc))
    return inbox / f"{name}.json"


@pytest.fixture
def inbox(lake, tmp_path) -> Path:
    lake.init()
    return tmp_path / "lake" / "inbox" / "biomass"


def test_pipeline_synthetic_drops(lake, bio_config, inbox):
    _drop_utm_10m(inbox)
    _drop_wgs84_coarse(inbox)
    # a broken drop next to good ones: live component with a dead size class
    (inbox / "broken.json").write_text(json.dumps(_sidecar(file="live_utm.tif", fuel_size_classes=["1h"])))
    # CRS lie: the sidecar says UTM, the file is geographic
    sub = inbox / "batch2"
    sub.mkdir()
    shutil.copy(inbox / "dead_geo.tif", sub / "liar.tif")
    (sub / "liar.json").write_text(json.dumps(_sidecar(file="liar.tif", component="both",
                                                       fuel_size_classes=["all"])))
    # other AOI and outside the period: not discovered
    (inbox / "other_aoi.json").write_text(json.dumps(_sidecar(file="live_utm.tif", aoi="smm")))
    (inbox / "old.json").write_text(json.dumps(_sidecar(file="live_utm.tif", date="2020-01-01",
                                                        producer={"name": "s", "version": "9"})))
    before = sorted(p.name for p in inbox.rglob("*"))

    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    d = rep.datasets[0]
    assert d.discovered == 4
    assert (d.count("created"), d.count("failed")) == (2, 2), [(u.key, u.error) for u in d.units]
    errors = {u.key.split(":")[1]: u.error for u in d.units if u.status == "failed"}
    assert "component 'live' allows only" in errors["inbox/biomass/broken.json"]
    assert "file CRS EPSG:4326 != sidecar crs EPSG:32611" in errors["inbox/biomass/batch2/liar.json"]
    assert sorted(p.name for p in inbox.rglob("*")) == before          # inbox untouched
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))

    items = {i["id"]: i for i in catalog.iter_items(lake, DS)}
    live = items["biomass__tiny__20260815__live-0.1.0"]
    p = live["properties"]
    assert p["fuellake:source_unit"] == "Mg/ha" and p["fuellake:unit_factor_to_kg_m2"] == 0.1
    assert p["fuellake:resampling"] == "average" and p["fuellake:uncertainty_kind"] == "std"
    assert p["fuellake:fuel_size_classes"] == ["live_herbaceous", "live_woody"]
    assert 0 < p["fuellake:qc_masked_fraction"] < 0.2
    arr, grid = read_cog(lake, catalog.asset_rel(live, "biomass"))
    unc, _ = read_cog(lake, catalog.asset_rel(live, "biomass_uncertainty"))
    assert grid.shape == load_aoi("tiny").grid.shape
    assert np.nanmax(arr) == pytest.approx(2.5) and np.nanmin(arr) == pytest.approx(2.5)   # 25 Mg/ha
    assert np.isnan(arr).any() and np.nanmax(unc) == pytest.approx(0.25)
    assert raw_copy_exists(lake, "live_utm")

    dead = items["biomass__tiny__20260815__dead-0.2.0"]
    assert dead["properties"]["fuellake:resampling"] == "bilinear"
    darr, _ = read_cog(lake, catalog.asset_rel(dead, "biomass"))
    assert np.isfinite(darr).all() and 1.0 <= darr.min() < darr.max() <= 3.0
    assert np.all(np.diff(darr[darr.shape[0] // 2]) > 0)          # gradient preserved west -> east

    # rerun: good drops skipped, bad drops fail again (they stay visible until fixed)
    rep2 = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    d2 = rep2.datasets[0]
    assert (d2.skipped, d2.count("created"), d2.count("failed")) == (2, 0, 2)

    # a changed drop (new bytes) is re-ingested and replaces its item
    _drop_utm_10m(inbox, value=30.0)
    rep3 = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    assert rep3.datasets[0].count("updated") == 1
    arr, _ = read_cog(lake, catalog.asset_rel(next(i for i in catalog.iter_items(lake, DS)
                                                   if i["id"].endswith("live-0.1.0")), "biomass"))
    assert np.nanmax(arr) == pytest.approx(3.0)


def raw_copy_exists(lake, stem: str) -> bool:
    return any(f"/{stem}__" in p for p in lake.find("raw/biomass"))


def test_duplicate_drops_fail_without_overwriting(lake, bio_config, inbox):
    _drop_utm_10m(inbox, name="a")
    _drop_utm_10m(inbox, name="b", value=40.0)          # same date, component and producer version
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    d = rep.datasets[0]
    assert d.count("failed") == 2 and d.count("created") == 0
    assert all("duplicate drops" in u.error for u in d.units)


def test_drop_outside_aoi_fails(lake, bio_config, inbox):
    t = Affine(10, 0, 500000, 0, -10, 3700000)          # far east of the tiny AOI
    _tif(inbox / "far.tif", np.ones((20, 20), "float32"), "EPSG:32611", t)
    (inbox / "far.json").write_text(json.dumps(_sidecar(file="far.tif")))
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    u = rep.datasets[0].units[0]
    assert u.status == "failed" and "no valid pixels over AOI tiny" in u.error


def test_missing_data_file_fails(lake, bio_config, inbox):
    (inbox / "lonely.json").write_text(json.dumps(_sidecar(file="nothere.tif")))
    rep = pipeline.run([DS], "tiny", PERIOD, lake=lake)
    u = rep.datasets[0].units[0]
    assert u.status == "failed" and "drop files missing" in u.error


def test_example_fixture_is_documented_in_config():
    cfg = yaml.safe_load((REPO / "config" / "datasets" / f"{DS}.yaml").read_text())
    assert cfg["options"]["sidecar_schema"] == "config/schemas/biomass_drop.schema.json"
    assert [v["name"] for v in cfg["variables"]] == ["biomass", "biomass_uncertainty"]
    assert copy.deepcopy(_example())["unit"] == "kg/m2"
