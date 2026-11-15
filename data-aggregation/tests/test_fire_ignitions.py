# fuel-lake — tests for the fire ignitions connector (FPA-FOD / WFIGS fixtures, paging, dedupe rule, live ArcGIS)
# Licensed under Apache-2.0.
from __future__ import annotations

import io
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fuellake import catalog, pipeline
from fuellake.config import Settings
from fuellake.connectors.base import Period
from fuellake.connectors.fire_ignitions import (
    ACRES_400_HA,
    FPA_FIELDS,
    IGNITION_SCHEMA,
    BatchMismatch,
    FireIgnitionsConnector,
    local_time_to_utc,
    local_year_bounds_utc,
    match_duplicates,
    records_to_rows,
    rows_to_frame,
    size_class,
    wfigs_location,
    wfigs_row,
)
from fuellake.contracts import validate_collection, validate_table
from fuellake.registry import lake_region, load_aoi, load_dataset
from fuellake.storage import Lake
from fuellake.tables import read_table, write_geoparquet

FIX = Path(__file__).parent / "fixtures" / "fire_ignitions"
FPA_RECS = json.loads((FIX / "fpa_fod_sample.json").read_text())["records"]
WFIGS_RECS = json.loads((FIX / "wfigs_sample.json").read_text())["records"]
MS = 1000

# Synthetic records inside smm for the cross-source rule: an FPA-FOD fire on the last FPA day, a WFIGS report of
# the same fire 0.5 km away the next local day (a duplicate) and an unrelated WFIGS fire 2.5 km away.
SYN_FPA = {"attributes": {"objectid": 999000001, "fod_id": 999000001, "fpa_id": "SYN-2024-0001",
                          "fire_name": "SYNTHETIC DEC31", "fire_year": 2024, "discovery_date": 1735603200000,
                          "discovery_time": "2300", "fire_size": 0.5, "fire_size_class": "B",
                          "nwcg_cause_classification": "Human", "nwcg_general_cause": "Smoking",
                          "latitude": 34.06, "longitude": -118.80, "state": "CA"}, "geometry": None}


def _syn_wfigs(oid: int, name: str, lon: float, lat: float, utc: datetime) -> dict:
    return {"attributes": {"OBJECTID": oid, "IrwinID": f"{{00000000-0000-0000-0000-{oid:012d}}}",
                           "UniqueFireIdentifier": f"2025-SYN-{oid}", "IncidentName": name,
                           "FireDiscoveryDateTime": int(utc.timestamp() * MS), "IncidentSize": 0.1,
                           "FinalAcres": None, "DiscoveryAcres": 0.1, "InitialLatitude": lat,
                           "InitialLongitude": lon, "FireCause": "Human", "FireCauseGeneral": None,
                           "POOState": "US-CA", "POOProtectingUnit": "CALAC", "IncidentTypeCategory": "WF",
                           "ModifiedOnDateTime_dt": int(datetime(2025, 1, 2, tzinfo=UTC).timestamp() * MS)},
            "geometry": {"x": lon, "y": lat}}


SYN_DUP = _syn_wfigs(999000002, "SYNTHETIC DUP", -118.80, 34.0645, datetime(2025, 1, 1, 20, tzinfo=UTC))
SYN_FAR = _syn_wfigs(999000003, "SYNTHETIC FAR", -118.80, 34.0825, datetime(2025, 1, 1, 21, tzinfo=UTC))


# ---------------------------------------------------------------------------- fake ArcGIS REST
class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.payload, self.status_code = payload, status

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


_CMP = re.compile(r"(\w+)\s*(>=|<=|<|>|=)\s*(TIMESTAMP '[^']+'|'[^']*'|-?\d+(?:\.\d+)?)")


def _value(tok: str):
    if tok.startswith("TIMESTAMP"):
        return datetime.strptime(tok[11:-1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp() * MS
    if tok.startswith("'"):
        return tok[1:-1]
    return float(tok)


def _matches(rec: dict, where: str, params: dict) -> bool:
    a = rec["attributes"]
    for field, op, tok in _CMP.findall(where):
        if field.isdigit():
            continue                               # 1=1
        v, x = a.get(field), _value(tok)
        if v is None:
            return False
        ok = {">=": v >= x, "<=": v <= x, "<": v < x, ">": v > x, "=": v == x}[op]
        if not ok:
            return False
    if "geometry" in params:
        w, s, e, n = (float(t) for t in params["geometry"].split(","))
        g = rec.get("geometry") or {}
        if not (w <= g.get("x", 999) <= e and s <= g.get("y", 999) <= n):
            return False
    return True


class FakeArcGIS:
    """The FPA-FOD MapServer (service, layer, query) and the WFIGS FeatureServer over fixture records."""

    def __init__(self, fpa=None, wfigs=None, max_record_count: int = 2000, short_page: bool = False):
        self.fpa = list(FPA_RECS if fpa is None else fpa)
        self.wfigs = list(WFIGS_RECS if wfigs is None else wfigs)
        self.max_record_count, self.short_page = max_record_count, short_page
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        p = dict(params or {})
        self.calls.append((url, p))
        if url.endswith("/MapServer"):
            return FakeResponse({"serviceDescription": "current edition ... Seventh Edition (FPA_FOD_20260615) ..."})
        fpa = "EDW_FireOccurrence" in url
        if not url.endswith("/query"):
            return FakeResponse({"name": "Fire_FPA_FOD_7th_Fires" if fpa else "Incidents",
                                 "maxRecordCount": self.max_record_count})
        oid = "objectid" if fpa else "OBJECTID"
        sel = [r for r in (self.fpa if fpa else self.wfigs) if _matches(r, p["where"], p)]
        if "outStatistics" in p:
            return FakeResponse({"features": self._stats(sel, json.loads(p["outStatistics"]),
                                                         p.get("groupByFieldsForStatistics"))})
        if p.get("returnIdsOnly") == "true":
            return FakeResponse({"objectIdFieldName": oid, "objectIds": [r["attributes"][oid] for r in sel]})
        if self.short_page:
            sel = sel[:-1]
        out = sel[:self.max_record_count]
        return FakeResponse({"features": [{"attributes": r["attributes"], "geometry": r["geometry"]} for r in out],
                             "exceededTransferLimit": len(sel) > self.max_record_count})

    @staticmethod
    def _stats(sel: list[dict], stats: list[dict], group_by: str | None) -> list[dict]:
        groups: dict = {}
        for r in sel:
            groups.setdefault(r["attributes"].get(group_by) if group_by else None, []).append(r)
        out = []
        for key, recs in sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0])) or [(None, [])]:
            attrs = {group_by: key} if group_by else {}
            for st in stats:
                vals = [r["attributes"].get(st["onStatisticField"]) for r in recs]
                vals = [v for v in vals if v is not None]
                attrs[st["outStatisticFieldName"]] = (len(vals) if st["statisticType"] == "count"
                                                      else max(vals) if vals else None)
            out.append({"attributes": attrs})
        return out


def _connector(lake, fake=None) -> FireIgnitionsConnector:
    conn = FireIgnitionsConnector(load_dataset("fire-ignitions"), lake, Settings.from_env())
    if fake is not None:
        conn.http_session = lambda bearer_env=None: fake
    return conn


# ---------------------------------------------------------------------------- conversions
def test_size_class_matches_nwcg_bounds_and_hectares():
    assert [size_class(a) for a in (0.01, 0.25, 0.26, 9.99, 10, 99.5, 100, 299.9, 300, 999.9, 1000, 4999, 5000)] == \
        ["A", "A", "B", "B", "C", "C", "D", "D", "E", "E", "F", "F", "G"]
    assert size_class(None) is None
    assert ACRES_400_HA == pytest.approx(988.4215, abs=1e-3)
    rows = records_to_rows("fpa-fod", FPA_RECS)
    camp = next(r for r in rows if r["fire_name"] == "CAMP")
    assert camp["fire_size_ac"] == 153336.0 and camp["fire_size_ha"] == pytest.approx(153336.0 * 0.40468564224)
    assert all(size_class(r["fire_size_ac"]) == r["size_class"] for r in rows)   # FPA's own class reproduced


def test_epoch_and_local_dates():
    camp = next(r for r in records_to_rows("fpa-fod", FPA_RECS) if r["fire_name"] == "CAMP")
    # FPA-FOD DISCOVERY_DATE is the local date at 00:00 UTC; DISCOVERY_TIME is local (PST in November)
    assert camp["discovery_date"] == date(2018, 11, 8) and camp["fire_year"] == 2018
    assert camp["discovery_datetime"] == datetime(2018, 11, 8, 14, 33, tzinfo=UTC)
    assert camp["containment_date"] == date(2018, 11, 25) and camp["fire_out_date"] is None
    assert local_time_to_utc(date(2024, 7, 24), "1452") == datetime(2024, 7, 24, 21, 52, tzinfo=UTC)   # PDT
    assert local_time_to_utc(date(2024, 7, 24), "2460") is None and local_time_to_utc(date(2024, 7, 24), None) is None
    # WFIGS FireDiscoveryDateTime is UTC; 2025-01-01 00:57 UTC is 31 December 2024 in California
    creek = wfigs_row(next(r["attributes"] for r in WFIGS_RECS if r["attributes"]["IncidentName"] == "CREEK"),
                      None)
    assert creek["discovery_datetime"] == datetime(2025, 1, 1, 0, 57, 13, tzinfo=UTC)
    assert creek["discovery_date"] == date(2024, 12, 31) and creek["fire_year"] == 2024
    lo, hi = local_year_bounds_utc(2025)
    assert (lo, hi) == (datetime(2025, 1, 1, 8, tzinfo=UTC), datetime(2026, 1, 1, 8, tzinfo=UTC))


def test_wfigs_location_size_and_cause():
    recs = {r["attributes"]["UniqueFireIdentifier"]: r for r in WFIGS_RECS}
    over = recs["2025-CACNF-000012"]            # initial point in Washington, geometry in San Diego County
    lon, lat, src = wfigs_location(over["attributes"], over["geometry"])
    assert (src, round(lat, 3)) == ("geometry", 32.857)
    assert wfigs_location(over["attributes"], over["geometry"], prefer="initial")[1] == pytest.approx(47.460448)
    calfd = recs["2025-CALFD-000042"]           # InitialLatitude null
    assert wfigs_location(calfd["attributes"], None) == (None, None, None)
    assert wfigs_location(calfd["attributes"], calfd["geometry"], prefer="initial")[2] == "geometry"
    row = wfigs_row(over["attributes"], None)
    assert row["location_source"] == "initial" and row["lat"] == pytest.approx(47.460448)
    gifford = wfigs_row(recs["2025-CALPF-002181"]["attributes"], recs["2025-CALPF-002181"]["geometry"])
    assert gifford["irwin_id"] == "08a6358f-8301-4093-9988-e265c8f6f0a9" == gifford["source_id"]
    assert (gifford["fire_size_ac"], gifford["size_class"], gifford["size_field"]) == (131612, "G", "IncidentSize")
    assert gifford["discovery_size_ac"] == 0.25 and gifford["cause_class"] == "undetermined"
    # 2025-10-14 23:30 UTC is 16:30 PDT; out 2026-03-05 19:25 UTC
    assert (gifford["containment_date"], gifford["fire_out_date"]) == (date(2025, 10, 14), date(2026, 3, 5))
    madera = wfigs_row(recs["2025-CAVNC-0048219"]["attributes"], recs["2025-CAVNC-0048219"]["geometry"])
    assert madera["fire_size_ac"] is None and madera["size_class"] is None and madera["size_field"] is None
    assert madera["cause_class"] is None and madera["fire_out_date"] is None     # not reported out
    final = wfigs_row({**recs["2025-CAVNC-0048219"]["attributes"], "FinalAcres": 3.0}, None)
    assert (final["fire_size_ac"], final["size_field"], final["size_class"]) == (3.0, "FinalAcres", "B")


# ---------------------------------------------------------------------------- dedupe rule
def _pt(lon, lat, d):
    return {"lon": lon, "lat": lat, "discovery_date": d}


def test_match_duplicates_distance_and_days():
    ref = [_pt(-118.80, 34.06, date(2024, 12, 31))]
    dlat_1900 = 1900 / 111_000        # about 1.9 km north
    cand = [_pt(-118.80, 34.06 + dlat_1900, date(2025, 1, 2)),     # 1.9 km, 2 days: duplicate
            _pt(-118.80, 34.06 + 2100 / 111_000, date(2025, 1, 1)),  # 2.1 km: distinct
            _pt(-118.80, 34.06, date(2025, 1, 3)),                  # same place, 3 days: distinct
            _pt(-118.80, 34.06, None)]                              # no date: never a duplicate
    assert match_duplicates(cand, ref) == [0, None, None, None]
    assert match_duplicates(cand, ref, max_m=2500) == [0, 0, None, None]
    assert match_duplicates(cand, ref, max_days=3) == [0, None, 0, None]
    ref2 = ref + [_pt(-118.80, 34.06 + 1800 / 111_000, date(2025, 1, 1))]
    assert match_duplicates(cand[:1], ref2) == [1]                 # nearest reference row wins
    assert match_duplicates([], ref) == [] and match_duplicates(cand, []) == [None] * 4
    # same list twice: rows pair with their nearest other row (0-1: 200 m, 1 day; 2-0: 1.9 km, 1 day), never self
    assert match_duplicates(cand, cand, exclude_self=True) == [1, 0, 0, None]
    assert match_duplicates(cand, cand) == [0, 1, 2, None]


# ---------------------------------------------------------------------------- paging
def test_object_id_batches_follow_page_size(lake):
    fake = FakeArcGIS()
    conn = _connector(lake, fake)
    recs = conn.fetch_records(fake, "fpa-fod", "state = 'CA' AND fire_year = 2024", ["fod_id"], 3)
    ids = sorted(r["attributes"]["objectid"] for r in FPA_RECS if r["attributes"]["fire_year"] == 2024)
    assert [r["attributes"]["objectid"] for r in recs] == ids and len(ids) == 8
    pages = [p["where"] for _, p in fake.calls if "outFields" in p]
    assert len(pages) == 3                                          # 8 ids in batches of 3
    assert f"objectid >= {ids[0]} AND objectid <= {ids[2]}" in pages[0]
    assert f"objectid >= {ids[6]} AND objectid <= {ids[7]}" in pages[2]


def test_page_size_capped_by_max_record_count_and_mismatch_fails(lake):
    fake = FakeArcGIS(max_record_count=2)
    conn = _connector(lake, fake)
    assert conn.page_size(fake, "wfigs") == 2 and conn.page_size(FakeArcGIS(), "wfigs") == 2000
    short = FakeArcGIS(short_page=True)
    with pytest.raises(BatchMismatch):
        conn.fetch_records(short, "fpa-fod", "state = 'CA' AND fire_year = 2024", ["fod_id"], 3)
    assert sum(1 for _, p in short.calls if p.get("returnIdsOnly")) == 2   # ids listed again once, then fail
    with pytest.raises(BatchMismatch):                                      # exceededTransferLimit
        conn.fetch_records(FakeArcGIS(max_record_count=2), "fpa-fod", "state = 'CA' AND fire_year = 2024",
                           ["fod_id"], 3)


# ---------------------------------------------------------------------------- schema
def test_schema_types_and_zero_row_part(lake):
    full = rows_to_frame(records_to_rows("fpa-fod", FPA_RECS) + records_to_rows("wfigs", WFIGS_RECS))
    assert list(full.columns) == list(IGNITION_SCHEMA) + ["geometry"]
    assert full.ignition_id.is_unique and set(full.source) == {"fpa-fod", "nifc-wfigs"}
    empty = rows_to_frame([])
    base = "staged/tables/fire_ignitions/fire-ignitions"
    write_geoparquet(lake, f"{base}/part-full.parquet", full)
    write_geoparquet(lake, f"{base}/part-empty.parquet", empty)
    s_full = pq.read_schema(io.BytesIO(lake.read_bytes(f"{base}/part-full.parquet")))
    s_empty = pq.read_schema(io.BytesIO(lake.read_bytes(f"{base}/part-empty.parquet")))
    types = {f.name: f.type for f in s_full}
    assert {f.name: str(f.type) for f in s_empty} == {k: str(v) for k, v in types.items()}   # by name
    assert types["discovery_date"] == pa.date32() and types["fod_id"] == pa.int64()
    assert types["fire_year"] == pa.int32() and types["fire_size_ha"] == pa.float64()
    assert types["discovery_datetime"] == pa.timestamp("us", tz="UTC") == types["ingested_at"]
    assert pa.types.is_string(types["ignition_id"]) and pa.types.is_binary(types["geometry"])
    for part in ("full", "empty"):
        assert validate_table(lake, f"{base}/part-{part}.parquet", "fire_ignitions").ok
    back = read_table(lake, "fire_ignitions", "fire-ignitions")
    assert len(back) == len(full) and back.crs.to_epsg() == 4326
    camp = back[back.fire_name == "CAMP"].iloc[0]
    assert (camp.geometry.x, camp.geometry.y) == (camp.lon, camp.lat) == (-121.4347, 39.8139)
    assert camp.mtbs_id == "CA3982012144020181108" and camp.fod_id == 400307281 and pd.isna(camp.irwin_id)


# ---------------------------------------------------------------------------- region and discovery
def test_region_filter_drops_points_outside_california(lake):
    conn = _connector(lake)
    east = [r for r in FPA_RECS if r["attributes"]["fire_name"] in ("EASTSIDE", "CAMP")]
    kept, n_no_loc, n_out = conn.region_records("fpa-fod", east)
    assert [r["attributes"]["fire_name"] for r in kept] == ["CAMP"] and (n_no_loc, n_out) == (0, 1)
    over = [r for r in WFIGS_RECS if r["attributes"]["IncidentName"] == "OVERLOOK"]
    assert len(conn.region_records("wfigs", over)[0]) == 1          # geometry in San Diego County
    conn.dataset.options["wfigs_location"] = "initial"              # load_dataset returns a fresh config
    assert conn.region_records("wfigs", over)[2] == 1               # its initial point is in Washington
    assert lake_region() is not None


def test_discover_cuts_over_after_last_fpa_year(lake):
    fake = FakeArcGIS(fpa=FPA_RECS + [SYN_FPA], wfigs=WFIGS_RECS + [SYN_DUP, SYN_FAR])
    conn = _connector(lake, fake)
    refs = conn.discover(load_aoi("smm"), Period(date(2024, 6, 1), date(2026, 3, 1)))
    assert [r.key for r in refs] == ["fpafod-smm-2024-v20260615-n9", "wfigs-smm-2025-v20260513-n7-x20260615",
                                     "wfigs-smm-2026-vnone-n0"]
    assert refs[1].meta["dedupe_against_fpa"] and not refs[2].meta["dedupe_against_fpa"]
    assert all(r.meta["page_size"] == 2000 for r in refs)
    assert "CREEK" not in {r["attributes"]["IncidentName"] for r in WFIGS_RECS
                           if _matches(r, conn.wfigs_where(2025), {})}      # 31 Dec local stays in 2024


def test_pipeline_offline_run_dedupes_and_is_idempotent(lake, monkeypatch):
    fake = FakeArcGIS(fpa=FPA_RECS + [SYN_FPA], wfigs=WFIGS_RECS + [SYN_DUP, SYN_FAR], max_record_count=2)
    monkeypatch.setattr(FireIgnitionsConnector, "http_session", lambda self, bearer_env=None: fake)
    period = Period(date(2023, 1, 1), date(2025, 12, 31))
    rep = pipeline.run(["fire-ignitions"], "smm", period, lake=lake)
    assert rep.ok, rep.to_dict()
    items = {i["id"]: i for i in catalog.iter_items(lake, "fire-ignitions")}
    assert set(items) == {"fire-ignitions__smm__20230101__fpa-fod", "fire-ignitions__smm__20240101__fpa-fod",
                          "fire-ignitions__smm__20250101__wfigs"}
    p23 = items["fire-ignitions__smm__20230101__fpa-fod"]["properties"]
    assert p23["fuellake:n_rows"] == 0 and p23["fuellake:source_key"] == "fpafod-smm-2023-v20260615-n0"
    p24 = items["fire-ignitions__smm__20240101__fpa-fod"]["properties"]
    p25 = items["fire-ignitions__smm__20250101__wfigs"]["properties"]
    assert p24["fuellake:n_rows"] == 4 and p24["fire_ignitions:largest_fire"]["fire_name"] == "FRANKLIN"
    assert p24["fire_ignitions:page_size"] == 2 and p24["fire_ignitions:dedupe"] == {"applied": False}
    d = p25["fire_ignitions:dedupe"]
    assert d["applied"] and d["n_removed"] == 1 and d["reference_rows"] == 1
    assert d["removed"][0]["fire_name"] == "SYNTHETIC DUP" and d["removed"][0]["fod_id"] == 999000001
    assert p25["fuellake:n_rows"] == 2 and p25["fire_ignitions:n_size_missing"] == 0
    assert p25["fire_ignitions:n_not_out"] == 2 and p24["fire_ignitions:n_not_out"] is None
    assert p25["fuellake:redistributable"] is False
    df = read_table(lake, "fire_ignitions", "fire-ignitions")
    assert set(df.fire_name) == {"FRANKLIN", "BROAD", "VIEW", "SYNTHETIC DEC31", "CALFD-000042", "SYNTHETIC FAR"}
    assert all(r.ok for r in validate_collection(lake, load_dataset("fire-ignitions")))
    n_calls = len(fake.calls)
    rep2 = pipeline.run(["fire-ignitions"], "smm", period, lake=lake)
    assert rep2.ok and rep2.counts()["skipped"] == 3 and rep2.counts()["created"] == 0
    assert not any(p.get("returnIdsOnly") for _, p in fake.calls[n_calls:])   # nothing downloaded on rerun


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_fpa_fod_layer_counts_and_camp_fire(tmp_path):
    conn = _connector(Lake(tmp_path / "live"))
    s = conn.http_session()
    info = conn.fpa_info(s)
    assert info["max_year"] >= 2024 and len(info["version"]) == 8
    assert conn.page_size(s, "fpa-fod") <= 2000
    if info["version"] == "20260615":        # 7th edition as checked on 2026-11-14
        assert sum(n for y, n in info["counts"].items() if y >= 2016) == 74573
        assert info["max_year"] == 2024 and info["max_date"] == "2024-12-31"
    recs = conn.fetch_records(s, "fpa-fod", "state = 'CA' AND fire_year = 2018 AND fire_name = 'CAMP' AND "
                                            "fire_size > 100000", FPA_FIELDS, 2000)
    (camp,) = records_to_rows("fpa-fod", recs)
    assert camp["discovery_date"] == date(2018, 11, 8) and camp["fire_size_ac"] == pytest.approx(153336, rel=0.01)


@pytest.mark.network
def test_live_wfigs_layer_fields_and_counts(tmp_path):
    conn = _connector(Lake(tmp_path / "live"))
    s = conn.http_session()
    meta = conn._get_json(s, conn.layer("wfigs"), {"f": "json"})
    names = {f["name"] for f in meta["fields"]}
    assert {"IrwinID", "FireDiscoveryDateTime", "IncidentSize", "InitialLatitude", "InitialLongitude",
            "FireCauseGeneral", "POOState", "ModifiedOnDateTime_dt"} <= names
    assert meta["dateFieldsTimeReference"]["timeZone"] == "UTC" and conn.page_size(s, "wfigs") <= 2000
    assert conn.wfigs_info(s, 2025)["n"] > 10000
