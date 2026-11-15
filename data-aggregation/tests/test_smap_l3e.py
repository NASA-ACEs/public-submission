# fuel-lake — tests for the SMAP enhanced L3 (SPL3SMP_E) connector and the smap_sm feature alias
# Licensed under Apache-2.0.
"""Fixtures in ``tests/fixtures/smap`` are real (fetched 2026-11-14): CMR ``granules.umm_json`` responses for
SPL3SMP_E 2026-06-01..02 and for 2026-10-12 (a day listed with counters 001 and 002), and the two OPeNDAP DAP4
subsets for AOI ``smm`` (AM/PM soil moisture and retrieval_qual_flag, AM EASE row/column index). On 2026-06-01
the AM swath covers the west of the smm window and every retrieval there is flagged *not recommended*
(coast, urban, mountains); on 2026-06-02 no swath crosses smm (all cells fill)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import unquote

import numpy as np
import pytest

from fuellake import catalog, pipeline
from fuellake.config import Settings
from fuellake.connectors.base import Period
from fuellake.connectors.smap import (
    L3E_CONCEPT_ID,
    SmapL3eConnector,
    SmapL4Connector,
    aoi_window,
    check_l3e_subset,
    dap4_constraint_2d,
    granule_digest,
    layer_values,
    parse_l3e_item,
    parse_l3e_name,
    quality_masked,
    read_l3e_subset,
    select_l3e_granules,
)
from fuellake.contracts import validate_collection
from fuellake.rasters import open_zarr
from fuellake.registry import list_datasets, load_aoi, load_dataset
from fuellake.storage import Lake

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
FIX = Path(__file__).parent / "fixtures" / "smap"
L3E_CMR_JUNE = FIX / "cmr_spl3smp_e_20260601_02.json"
L3E_CMR_DUP = FIX / "cmr_spl3smp_e_20261012_duplicates.json"
L4_CMR_JUNE = FIX / "cmr_spl4smgp_20260601_02.json"
AM = "Soil_Moisture_Retrieval_Data_AM"
PM = "Soil_Moisture_Retrieval_Data_PM"
PATHS = [f"{AM}/soil_moisture", f"{AM}/retrieval_qual_flag", f"{PM}/soil_moisture_pm", f"{PM}/retrieval_qual_flag_pm",
         f"{AM}/EASE_row_index", f"{AM}/EASE_column_index"]
SMM_WINDOW = (354, 359, 652, 660)
SUBSETS = {"20260601": FIX / "SMAP_L3_SM_P_E_20260601_R19240_004__smm.nc4",
           "20260602": FIX / "SMAP_L3_SM_P_E_20260602_R19240_004__smm.nc4",
           "20260601T133000": FIX / "SMAP_L4_SM_gph_20260601T133000_Vv8013_001__smm.nc4",
           "20260602T133000": FIX / "SMAP_L4_SM_gph_20260602T133000_Vv8013_001__smm.nc4"}
SMAP_DATASETS = ("smap-l4", "smap-l3e", "smap-climatology")


def cmr_items(path: Path) -> list[dict]:
    return json.loads(path.read_text())["items"]


class FakeResponse:
    def __init__(self, status: int = 200, content: bytes = b"", payload: dict | None = None,
                 headers: dict | None = None):
        self.status_code, self.content, self._payload = status, content, payload
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> dict:
        return self._payload if self._payload is not None else json.loads(self.content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeCmr:
    """Serves fixed CMR records for every granule query (search and newest-first)."""

    def __init__(self, items: list[dict]):
        self.items = items
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append(dict(params or {}))
        items = list(self.items)
        if (params or {}).get("sort_key") == "-start_date":
            items = items[::-1]
        return FakeResponse(200, payload={"items": items}, headers={"CMR-Hits": str(len(items))})


class FakeOpendap:
    """Serves the fixture subset whose day (L3E) or day and slot (L4) appears in the URL; 404 otherwise."""

    def __init__(self, body: bytes | None = None):
        self.body, self.urls = body, []

    def get(self, url, timeout=None, **kw):
        self.urls.append(unquote(url))
        if self.body is not None:
            return FakeResponse(200, self.body)
        for key, path in SUBSETS.items():
            if ("T" in key and f"_{key}_" in url) or ("T" not in key and f"_{key}_R" in url):
                return FakeResponse(200, path.read_bytes())
        return FakeResponse(404, b"not found")


def write_smap_config(tmp_path: Path, monkeypatch, climatology_options: dict | None = None) -> Path:
    """Config dir with the real smm AOI and the real SMAP dataset configs (no lake region)."""
    import yaml

    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "aois" / "smm.yaml").write_text((REPO_CONFIG / "aois" / "smm.yaml").read_text())
    for ds_id in SMAP_DATASETS:
        text = (REPO_CONFIG / "datasets" / f"{ds_id}.yaml").read_text()
        if ds_id == "smap-climatology" and climatology_options:
            data = yaml.safe_load(text)
            data["options"].update(climatology_options)
            text = yaml.safe_dump(data, sort_keys=False)
        (root / "datasets" / f"{ds_id}.yaml").write_text(text)
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


def fake_sources(monkeypatch) -> FakeOpendap:
    """Route both SMAP connectors to the fixtures (CMR and OPeNDAP); returns the shared OPeNDAP fake."""
    monkeypatch.setenv("EARTHDATA_TOKEN", "dummy-offline-test-token")
    fake = FakeOpendap()
    monkeypatch.setattr(SmapL4Connector, "cmr_session", lambda self: FakeCmr(cmr_items(L4_CMR_JUNE)))
    monkeypatch.setattr(SmapL3eConnector, "cmr_session", lambda self: FakeCmr(cmr_items(L3E_CMR_JUNE)))
    monkeypatch.setattr(SmapL4Connector, "opendap_session", lambda self: fake)
    monkeypatch.setattr(SmapL3eConnector, "opendap_session", lambda self: fake)
    return fake


@pytest.fixture
def smap_config(tmp_path, monkeypatch) -> Path:
    return write_smap_config(tmp_path, monkeypatch)


def make_connector(lake: Lake, cmr: FakeCmr | None = None, opendap: FakeOpendap | None = None) -> SmapL3eConnector:
    conn = SmapL3eConnector(load_dataset("smap-l3e"), lake, Settings.from_env())
    conn.cmr_session = lambda: cmr or FakeCmr(cmr_items(L3E_CMR_JUNE))
    conn.opendap_session = lambda: opendap or FakeOpendap()
    return conn


# ---------------------------------------------------------------------------- config
def test_config_declares_verified_product_and_quality_policy():
    ds = load_dataset("smap-l3e")
    assert ds.connector == "fuellake.connectors.smap:SmapL3eConnector" and ds.kind == "raster-b"
    assert ds.options["concept_id"] == L3E_CONCEPT_ID == "C2938664763-NSIDC_CPRD" and ds.options["version"] == "006"
    names = [v.name for v in ds.variables]
    assert names == ["l3e_sm_am", "l3e_sm_am_all", "l3e_sm_pm"]
    extra = {v.name: v.model_extra for v in ds.variables}
    assert extra["l3e_sm_am"]["source_name"] == f"{AM}/soil_moisture"
    assert extra["l3e_sm_am"]["quality_flag"] == f"{AM}/retrieval_qual_flag" and extra["l3e_sm_am"]["reject_bits"] == 1
    assert "quality_flag" not in extra["l3e_sm_am_all"]
    assert extra["l3e_sm_pm"]["source_name"] == f"{PM}/soil_moisture_pm"
    assert not [v.name for v in ds.variables if v.lag]
    # the team feature smap_sm is provisionally a copy of L4 sm_surface (see smap-l4.yaml)
    sm = load_dataset("smap-l4").variable("smap_sm")
    assert sm.model_extra["alias_of"] == "sm_surface" and sm.lag and "source_name" not in sm.model_extra
    assert all(v.unit == "m3/m3" and tuple(v.valid_range) == (0.02, 0.5) for v in ds.variables)
    assert ds.access.auth == "earthdata" and set(ds.scales) == {"local", "region"}
    assert ds.license.redistributable is True and "10.5067/M20OXIZHY3RJ" in ds.license.attribution


def test_feature_names_are_unique_across_datasets():
    """The features layer prefixes clashing names with the dataset id, which would rename existing lag columns;
    the SMAP names must not clash and smap_sm must be declared exactly once."""
    owners: dict[str, list[str]] = {}
    for d in list_datasets():
        if d.kind in ("raster-a", "raster-b"):
            for v in d.variables:
                owners.setdefault(v.name, []).append(d.id)
    for name in [v.name for v in load_dataset("smap-l3e").variables] + ["sm_surface", "sm_rootzone", "sm_profile"]:
        assert len(owners[name]) == 1, (name, owners[name])
    assert owners["smap_sm"] == ["smap-l4"]


def test_request_paths_are_lean_and_deduplicated(smap_config, lake):
    conn = make_connector(lake)
    assert conn.request_paths() == PATHS
    assert conn.sources() == [f"{AM}/soil_moisture", f"{AM}/soil_moisture", f"{PM}/soil_moisture_pm"]
    assert conn.quality("l3e_sm_am") == (f"{AM}/retrieval_qual_flag", 1)
    assert conn.quality("l3e_sm_am_all") == (None, 0)


# ---------------------------------------------------------------------------- names and selection
def test_parse_l3e_names_and_items():
    a = parse_l3e_name("SMAP_L3_SM_P_E_20261012_R19240_002.h5_1MMe51bJ")
    assert a["stem"] == "SMAP_L3_SM_P_E_20261012_R19240_002" and a["crid"] == "R19240" and a["counter"] == 2
    assert a["dt"] == datetime(2026, 10, 12, tzinfo=UTC) and a["suffix"] == "_1MMe51bJ"
    b = parse_l3e_name("https://x/SMAP/SPL3SMP_E/006/2019/07/01/SMAP_L3_SM_P_E_20190701_R19240_001.h5")
    assert b["suffix"] is None and b["date"] == "20190701"
    assert parse_l3e_name("SMAP_L3_SM_P_E_20190701_R19240_001.qa") is None
    assert parse_l3e_name("SMAP_L4_SM_gph_20190701T133000_Vv8010_001.h5") is None
    g = parse_l3e_item(cmr_items(L3E_CMR_JUNE)[0])
    assert g.stem == "SMAP_L3_SM_P_E_20260601_R19240_004" and g.version == "R19240" and g.counter == 4
    assert g.opendap == ("https://opendap.earthdata.nasa.gov/collections/C2938664763-NSIDC_CPRD/granules/"
                         "SMAP_L3_SM_P_E_20260601_R19240_004.h5_1MMe51bJ")
    assert g.https.endswith("/SPL3SMP_E/006/2026/06/01/SMAP_L3_SM_P_E_20260601_R19240_004.h5")
    assert g.day == date(2026, 6, 1) and g.time == "2026-06-01T00:00:00+00:00"


def test_select_l3e_keeps_one_granule_per_day_highest_counter():
    grans = select_l3e_granules(cmr_items(L3E_CMR_DUP))
    assert [g.stem for g in grans] == ["SMAP_L3_SM_P_E_20261012_R19240_002"]
    assert select_l3e_granules(cmr_items(L3E_CMR_DUP)[::-1])[0].counter == 2
    june = select_l3e_granules(cmr_items(L3E_CMR_JUNE), first=date(2026, 6, 2))
    assert [g.day for g in june] == [date(2026, 6, 2)]


def test_dap4_constraint_has_no_coordinate_variables():
    ce = dap4_constraint_2d(PATHS[:2], SMM_WINDOW)
    assert ce == f"/{AM}/soil_moisture[354:358][652:659];/{AM}/retrieval_qual_flag[354:358][652:659]"


# ---------------------------------------------------------------------------- subsets
def test_read_real_subset_checks_ease_indices_and_quality():
    sub = read_l3e_subset(SUBSETS["20260601"], PATHS)
    assert sub["arrays"][f"{AM}/soil_moisture"].shape == (5, 8)
    assert sub["attrs"][f"{AM}/soil_moisture"]["_FillValue"] == -9999.0
    assert sub["attrs"][f"{AM}/soil_moisture"]["valid_max"] == pytest.approx(0.5)
    assert check_l3e_subset(sub, SMM_WINDOW, PATHS) == 40          # every cell carries its global index
    with pytest.raises(ValueError, match="georeferencing mismatch"):
        check_l3e_subset(sub, (354, 359, 653, 661), PATHS)         # one cell east: 9 km off
    with pytest.raises(ValueError, match="shape"):
        check_l3e_subset(sub, (354, 360, 652, 660), PATHS)
    every = layer_values(sub, f"{AM}/soil_moisture")
    assert int(np.isfinite(every).sum()) == 13 and 0.02 <= np.nanmin(every) and np.nanmax(every) < 0.2
    good, rejected = quality_masked(sub, f"{AM}/soil_moisture", f"{AM}/retrieval_qual_flag", 1)
    assert rejected == 13 and not np.isfinite(good).any()          # smm: coast, urban, mountains
    none = read_l3e_subset(SUBSETS["20260602"], PATHS)
    assert check_l3e_subset(none, SMM_WINDOW, PATHS) == 0          # no swath over smm that day
    assert not np.isfinite(layer_values(none, f"{AM}/soil_moisture")).any()
    with pytest.raises(KeyError):
        read_l3e_subset(SUBSETS["20260601"], [f"{AM}/soil_moisture_scah_missing"])


def test_corrupted_index_is_refused():
    sub = read_l3e_subset(SUBSETS["20260601"], PATHS)
    sub["arrays"][f"{AM}/EASE_column_index"] = sub["arrays"][f"{AM}/EASE_column_index"].copy()
    sub["arrays"][f"{AM}/EASE_column_index"][0, 0] = 1
    with pytest.raises(ValueError, match=r"cell \(0, 0\)"):
        check_l3e_subset(sub, SMM_WINDOW, PATHS)


# ---------------------------------------------------------------------------- discovery and pipeline
def test_discover_groups_the_month(smap_config, lake):
    conn = make_connector(lake)
    refs = conn.discover(load_aoi("smm"), Period(date(2026, 5, 20), date(2026, 7, 10)))
    assert len(refs) == 1
    ref = refs[0]
    grans = select_l3e_granules(cmr_items(L3E_CMR_JUNE))
    assert ref.key == f"smap-l3e-202606-through-20260602-{granule_digest(grans)}"
    assert ref.meta["n_granules"] == 2 and ref.meta["expected_granules"] == 2 and ref.meta["versions"] == ["R19240"]
    assert ref.meta["window"] == list(SMM_WINDOW) and ref.meta["collection"] == L3E_CONCEPT_ID
    assert all(u.startswith("https://opendap.earthdata.nasa.gov/collections/C2938664763") for u in ref.urls)


def test_missing_token_is_reported(smap_config, lake):
    conn = make_connector(lake)
    aoi = load_aoi("smm")
    ref = conn.discover(aoi, Period(date(2026, 6, 1), date(2026, 6, 2)))[0]
    assert conn.missing_credentials(aoi, ref) == ["EARTHDATA_TOKEN"]


def test_pipeline_offline_run_and_alias(smap_config, lake, monkeypatch):
    fake = fake_sources(monkeypatch)
    period = Period(date(2026, 6, 1), date(2026, 6, 2))
    rep = pipeline.run(["smap-l3e"], "smm", period, lake=lake)
    assert rep.ok, rep.to_dict()
    assert rep.counts()["created"] == 1 and len(fake.urls) == 2
    for u in fake.urls:
        assert "/x[" not in u and "/time" not in u
        assert f"/{AM}/EASE_row_index[354:358][652:659]" in u and f"/{PM}/soil_moisture_pm[354:358][652:659]" in u
    items = list(catalog.iter_items(lake, "smap-l3e"))
    assert [i["id"] for i in items] == ["smap-l3e__smm__20260601"]
    p = items[0]["properties"]
    assert p["smap:product"] == "SPL3SMP_E" and p["smap:crids"] == ["R19240"] and p["smap:missing_days"] == []
    assert p["fuellake:source_ids"][0] == "SMAP_L3_SM_P_E_20260601_R19240_004.h5_1MMe51bJ"
    assert p["smap:shape"] == [2, 5, 8] and p["smap:complete_month"] is False
    assert p["smap:quality_rejected_fraction"]["l3e_sm_am"] == 1.0
    assert p["smap:quality_rejected_fraction"]["l3e_sm_am_all"] == 0.0
    assert p["smap:valid_fractions"]["l3e_sm_am_all"] == pytest.approx(13 / 80)
    assert p["smap:valid_fractions"]["l3e_sm_am"] == 0.0
    ds = open_zarr(lake, catalog.asset_rel(items[0], "data"))
    assert set(ds.data_vars) == {"l3e_sm_am", "l3e_sm_am_all", "l3e_sm_pm"}
    assert [str(t)[:19] for t in ds["time"].values] == ["2026-06-01T00:00:00", "2026-06-02T00:00:00"]
    assert ds.rio.crs.to_epsg() == 6933
    assert ds["l3e_sm_am"].attrs["smap_quality_flag"] == f"/{AM}/retrieval_qual_flag"
    assert int(np.isfinite(ds["l3e_sm_am_all"].values).sum()) == 13
    assert all(r.ok for r in validate_collection(lake, load_dataset("smap-l3e")))
    assert lake.exists("raw/smap-l3e/2026/06/SMAP_L3_SM_P_E_20260601_R19240_004__smm.nc4")

    rep2 = pipeline.run(["smap-l3e"], "smm", period, lake=lake)
    assert rep2.ok and rep2.counts()["skipped"] == 1 and rep2.counts()["created"] == 0
    rep3 = pipeline.run(["smap-l3e"], "smm", period, lake=lake, force=True)
    assert rep3.ok and len(fake.urls) == 2                       # forced rerun reuses the raw subsets


def test_alias_copies_any_target(smap_config, lake, monkeypatch):
    """An alias of l3e_sm_am_all is a NaN-for-NaN copy of it (what a remapped smap_sm would hold)."""
    from fuellake.registry import Variable

    fake_sources(monkeypatch)
    conn = SmapL3eConnector(load_dataset("smap-l3e"), lake, Settings.from_env())
    conn.dataset.variables.append(Variable(name="smap_sm", alias_of="l3e_sm_am_all", unit="m3/m3",
                                           valid_range=(0.02, 0.5), lag=True))
    aoi = load_aoi("smm")
    ref = conn.discover(aoi, Period(date(2026, 6, 1), date(2026, 6, 2)))[0]
    (item,) = conn.transform(ref, conn.extract(ref, aoi), aoi)
    ds = open_zarr(lake, item.assets[0].path)
    np.testing.assert_array_equal(ds["smap_sm"].values, ds["l3e_sm_am_all"].values)
    assert int(np.isfinite(ds["smap_sm"].values).sum()) == 13
    for v in conn.dataset.variables:
        if v.name == "smap_sm":
            v.model_extra["alias_of"] = "no_such_variable"
    with pytest.raises(ValueError, match="alias_of"):
        conn.source_variables()


def test_features_layer_exposes_smap_sm(smap_config, lake, monkeypatch):
    """build-features names form B features after Zarr variables: smap_sm is a weather feature with lags,
    next to the L4 variables, and no SMAP name gets a dataset prefix."""
    from fuellake.features._common import discover_inputs
    from fuellake.features.matchups import config_lag_variables

    fake_sources(monkeypatch)
    period = Period(date(2026, 6, 1), date(2026, 6, 2))
    assert pipeline.run(["smap-l4", "smap-l3e"], "smm", period, lake=lake).ok
    inputs = discover_inputs(lake, load_aoi("smm"), period.start, period.end)
    by_name = {v.name: v for v in inputs.variables}
    assert by_name["smap_sm"].dataset == "smap-l4" and by_name["smap_sm"].role == "weather"
    assert {"sm_surface", "sm_rootzone", "sm_profile", "l3e_sm_am", "l3e_sm_am_all", "l3e_sm_pm"} <= set(by_name)
    assert not [n for n in by_name if "__" in n]
    assert {"smap_sm", "sm_surface", "sm_rootzone", "sm_profile"} <= set(config_lag_variables())
    assert "l3e_sm_am" not in config_lag_variables()
    l4 = inputs.weather["smap-l4"]
    np.testing.assert_array_equal(l4.points("smap_sm", date(2026, 6, 2), np.array([-118.8]), np.array([34.1])),
                                  l4.points("sm_surface", date(2026, 6, 2), np.array([-118.8]), np.array([34.1])))
    lon, lat = np.array([-118.93]), np.array([34.15])            # the cell with an AM retrieval on 2026-06-01
    every = inputs.weather["smap-l3e"].points("l3e_sm_am_all", date(2026, 6, 1), lon, lat)
    assert np.isfinite(every).all() and 0.02 < every[0] < 0.2
    assert np.isnan(inputs.weather["smap-l3e"].points("l3e_sm_am_all", date(2026, 6, 2), lon, lat)).all()


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_cmr_l3e_discovery(tmp_path):
    conn = SmapL3eConnector(load_dataset("smap-l3e"), Lake(tmp_path / "live"), Settings.from_env())
    latest = conn.latest_day()
    assert latest is not None and (date.today() - latest).days < 10, latest
    refs = conn.discover(load_aoi("smm"), Period(date(2019, 6, 1), date(2019, 6, 30)))
    # 2019-06-20..2019-07-22: SMAP safe mode, no granule
    assert len(refs) == 1 and refs[0].meta["n_granules"] == 19 and refs[0].meta["versions"] == ["R19240"]


@pytest.mark.earthdata
def test_live_opendap_l3e_subset_for_ca(tmp_path):
    if not Settings.has_secret("EARTHDATA_TOKEN"):
        pytest.skip("EARTHDATA_TOKEN not set")
    conn = SmapL3eConnector(load_dataset("smap-l3e"), Lake(tmp_path / "live"), Settings.from_env())
    aoi = load_aoi("ca")
    ref = conn.discover(aoi, Period(date(2024, 7, 1), date(2024, 7, 31)))[0]
    g = ref.meta["granules"][0]
    window = aoi_window(aoi)
    body = conn.fetch_subset(g, window, conn.request_paths())
    assert body[:4] == b"\x89HDF" and len(body) < 300_000
    import io

    sub = read_l3e_subset(io.BytesIO(body), conn.request_paths())
    assert check_l3e_subset(sub, window, conn.request_paths()) > 1000
    sm = layer_values(sub, f"{AM}/soil_moisture")
    finite = sm[np.isfinite(sm)]
    # a few not-recommended retrievals exceed the product's valid_max 0.5 (0.61 on 2024-07-01): the config masks them
    assert finite.size > 1000 and finite.max() < 0.7 and (finite > 0.5).mean() < 0.01
