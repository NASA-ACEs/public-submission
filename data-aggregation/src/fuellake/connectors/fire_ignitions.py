# fuel-lake — California fire ignition points with final size (FPA-FOD 7th edition, NIFC WFIGS after its last year)
# Licensed under Apache-2.0.
"""Every recorded wildfire ignition in California with its final size, table ``fire_ignitions``.

Purpose: negatives and positives for P(fire grows to >= 400 ha | ignition). Perimeters exist only for fires that
grew; this table holds every reported ignition point, small or large.

Sources (public ArcGIS REST layers, no login; verified live 2026-11-14):

- ``fpa-fod``: USDA Forest Service "Spatial wildfire occurrence data for the United States, 1992-2024
  [FPA_FOD_20260615]", 7th edition (Short 2026, doi:10.2737/RDS-2013-0009.7), EDW MapServer layer 0
  (:data:`FPA_LAYER`, ``maxRecordCount`` 2000). Used for every year up to the layer's ``max(fire_year)`` (2024).
  Query ``state = 'CA' AND fire_year = <year>`` plus the AOI bbox on the ``latitude``/``longitude`` attributes.
  ``discovery_date`` is the **local** calendar date stored as 00:00 UTC (Camp Fire: 1541635200000 = 2018-11-08);
  ``discovery_time`` is local ``HHMM`` (HIGHLAND 2024-12-30 ``0245`` = WFIGS 10:45 UTC). The ``latitude``/
  ``longitude`` attributes are NAD83 (layer wkid 4269); they are stored as is (the server's NAD83 -> WGS84 shift
  is about 1.5 m in California).
- ``wfigs``: NIFC "Wildland Fire Incident Locations" (item b4402f7887ca4ea9a6189443f220ef28, :data:`WFIGS_LAYER`,
  ``maxRecordCount`` 2000), operational IRWIN records since 2014, refreshed every 5 minutes, not QA'd after a
  fire is out. Used only for years after the last FPA-FOD year. Query ``POOState = 'US-CA' AND
  IncidentTypeCategory = 'WF'`` and ``FireDiscoveryDateTime`` (UTC, ``dateFieldsTimeReference`` UTC) within the
  **local** (America/Los_Angeles) calendar year, so a fire discovered on 31 December at 16:45 PST stays in the
  earlier year, as in FPA-FOD.

WFIGS fields: size = ``IncidentSize`` (null for 36-46 % of California records of 2025-2026; where those have a
``DiscoveryAcres`` it is <= 0.1, kept in ``discovery_size_ac``; null stays null, never 0), else ``FinalAcres``.
Location = the layer's point geometry (the incident point of origin the ``POO*`` attributes such as
``POOState`` and ``POOProtectingUnit`` refer to); ``InitialLatitude``/``InitialLongitude`` ("initial reported point of
origin") are kept in ``initial_lat``/``initial_lon`` and used only when the geometry is missing (option
``wfigs_location: initial`` reverses the order). Reason (2025-2026 California rows): Initial* is null for 4 %,
more than 2 km from the geometry for 6 % (median 1 m) and outside California for 12 rows (2025-CACNF-000012
OVERLOOK: initial point in Washington state, geometry in San Diego County); every geometry lies in the region.
That the POO* attributes follow the geometry is observed, not stated in the item description.
``FireCauseGeneral`` is almost always null in California; ``cause_class`` comes from ``FireCause``.
``IncidentSize`` is the latest reported size, not necessarily final while a fire burns. ``FireOutDateTime``
(``fire_out_date``) is reported for only about 9 % of 2025 rows and ``ContainmentDateTime`` for about 58 %, so
neither marks final sizes reliably.

Selection: rows whose point lies inside the AOI bbox widened by ``buffer_m`` (default 1 km) **and** inside the
lake region (within its ``tolerance_m`` of the California polygon). Rows without a location or outside the
region are dropped before ``raw/`` is written (counted in item properties).

Paging: object-id batching. ``returnIdsOnly`` lists the matching ids, which are sorted and cut into batches of
``min(page_size, maxRecordCount)`` (2000); each batch is one query ``<where> AND <oid> >= lo AND <oid> <= hi``
(short GET URLs). A batch must return exactly its ids and no ``exceededTransferLimit``, else the unit fails.

Cross-source duplicates (:func:`match_duplicates`): FPA-FOD has no IRWIN id. The two services overlap in time
for 2014-2024, but each year is taken from one source only, so in this table they overlap only near the cut-over
(the WFIGS unit of the first year after FPA-FOD). Such a WFIGS unit also fetches the FPA-FOD rows discovered
within ``dedupe_max_days`` of its year (kept in its raw file) and drops every WFIGS row that lies within
``dedupe_max_m`` (2000 m, EPSG:3310) of an FPA-FOD row whose ``discovery_date`` differs by at most
``dedupe_max_days`` (2) days; the FPA-FOD row (QA'd final size) is kept. Counts and the removed id pairs are in
the item properties. Each item also counts rows with a same-source neighbour under the same rule
(``fire_ignitions:n_same_source_near``), a measure of how often the rule would pair distinct fires (or, in WFIGS,
repeated records of one fire).

Units: whole calendar years touched by the period. Keys: ``fpafod-<aoi>-<year>-v<FPA_FOD date>-n<CA rows>``
(the layer has no ``editingInfo``; the edition token comes from the service description) and
``wfigs-<aoi>-<year>-v<YYYYMMDD of the latest ModifiedOnDateTime_dt>-n<CA rows>`` (+ ``-x<FPA_FOD date>`` when
the unit is deduplicated against FPA-FOD). A rerun is a no-op until the source rows of that year change; for the
open WFIGS year that means a new California record or an edit on a later UTC day. Item and part ids depend only
on source, AOI and year. A year without rows writes a zero-row part.

Columns: :data:`IGNITION_SCHEMA` (+ point ``geometry``, EPSG:4326).
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow as pa

from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.connectors.fire_perimeters import (
    ACRE_TO_HA,
    buffer_bbox_m,
    clean_guid,
    clean_text,
    epoch_ms_to_date,
    epoch_ms_to_datetime,
    sha1,
    typed_frame,
)
from fuellake.registry import AOI, lake_region
from fuellake.tables import write_geoparquet

log =logging.getLogger("fuellake.connectors.fire_ignitions")

FPA_LAYER = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_FireOccurrenceCurrentEdition_01/MapServer/0"
WFIGS_LAYER = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
               "WFIGS_Incident_Locations/FeatureServer/0")
SOURCES = ("fpa-fod", "wfigs")
SOURCE_NAMES = {"fpa-fod": "fpa-fod", "wfigs": "nifc-wfigs"}
KEY_PREFIX = {"fpa-fod": "fpafod", "wfigs": "wfigs"}
LICENSES = {
    "fpa-fod": ("USDA Forest Service FPA FOD 7th edition: can be used without additional permissions or fees; "
                "cite Short, Karen C. 2026, doi:10.2737/RDS-2013-0009.7"),
    "wfigs": "NIFC WFIGS Incident Locations: no licence stated; public US federal layer with disclaimer",
}
PACIFIC = ZoneInfo("America/Los_Angeles")
METRIC_CRS = "EPSG:3310"
ACRES_400_HA = 400.0 / ACRE_TO_HA           # 988.42 acres
FPA_FIRST_YEAR = 1992
WFIGS_FIRST_YEAR = 2014                     # IRWIN start (the layer description: "back to 2014")
# NWCG fire size classes, upper bounds in acres: A <= 0.25 < B < 10 <= C < 100 <= D < 300 <= E < 1000 <= F < 5000 <= G.
# Checked against FPA-FOD's own fire_size_class on all California rows 2016-2024.
SIZE_CLASS_BOUNDS = (("B", 10.0), ("C", 100.0), ("D", 300.0), ("E", 1000.0), ("F", 5000.0))

FPA_FIELDS = ("objectid", "fod_id", "fpa_id", "fire_name", "complex_name", "fire_year", "discovery_date",
              "discovery_time", "cont_date", "fire_size", "fire_size_class", "nwcg_cause_classification",
              "nwcg_general_cause", "latitude", "longitude", "mtbs_id", "ics_209_plus_incident_join_id",
              "nwcg_reporting_unit_id", "state")
WFIGS_FIELDS = ("OBJECTID", "IrwinID", "UniqueFireIdentifier", "IncidentName", "CpxName", "FireDiscoveryDateTime",
                "ContainmentDateTime", "FireOutDateTime", "IncidentSize", "FinalAcres", "DiscoveryAcres",
                "InitialLatitude", "InitialLongitude", "FireCause", "FireCauseGeneral", "POOState",
                "POOProtectingUnit", "IncidentTypeCategory", "ModifiedOnDateTime_dt")

STRING = pd.ArrowDtype(pa.string())
DATE32 = pd.ArrowDtype(pa.date32())
TS_UTC = "datetime64[us, UTC]"
IGNITION_SCHEMA: dict[str, Any] = {
    "ignition_id": STRING,          # sha1("source|source_id")
    "source": STRING,               # fpa-fod | nifc-wfigs
    "source_id": STRING,            # FPA-FOD fod_id / WFIGS IrwinID (lower case, no braces)
    "fod_id": "Int64",              # FPA-FOD FOD_ID
    "fpa_id": STRING,               # FPA-FOD FPA_ID (e.g. INF-2024-CABTU-013761)
    "irwin_id": STRING,             # WFIGS IrwinID (FPA-FOD has none)
    "unique_fire_id": STRING,       # WFIGS UniqueFireIdentifier (YYYY-UNIT-NUMBER)
    "mtbs_id": STRING,              # FPA-FOD MTBS_ID (fires mapped by MTBS)
    "ics209_incident_id": STRING,   # FPA-FOD ICS_209_PLUS_INCIDENT_JOIN_ID
    "fire_name": STRING,
    "complex_name": STRING,         # FPA-FOD COMPLEX_NAME / WFIGS CpxName
    "fire_year": "Int32",           # FPA-FOD FIRE_YEAR / year of the local discovery date
    "discovery_date": DATE32,       # local (Pacific) calendar date
    "discovery_datetime": TS_UTC,   # FPA-FOD date + local DISCOVERY_TIME; WFIGS FireDiscoveryDateTime
    "containment_date": DATE32,     # local date: FPA-FOD CONT_DATE / WFIGS ContainmentDateTime
    "fire_out_date": DATE32,        # local date: WFIGS FireOutDateTime (FPA-FOD: null); null = not reported out
    "fire_size_ac": "float64",      # final size: FPA-FOD FIRE_SIZE / WFIGS IncidentSize, else FinalAcres
    "fire_size_ha": "float64",
    "size_class": STRING,           # NWCG A-G (FPA-FOD FIRE_SIZE_CLASS; WFIGS derived from acres)
    "size_field": STRING,           # source field of fire_size_ac: FIRE_SIZE | IncidentSize | FinalAcres
    "discovery_size_ac": "float64",  # WFIGS DiscoveryAcres (FPA-FOD: null)
    "cause_class": STRING,          # human | natural | undetermined (NWCG_CAUSE_CLASSIFICATION / FireCause)
    "cause_general": STRING,        # FPA-FOD NWCG_GENERAL_CAUSE / WFIGS FireCauseGeneral (source text)
    "lat": "float64",
    "lon": "float64",
    "location_source": STRING,      # latitude/longitude (FPA-FOD) | geometry | initial (WFIGS)
    "initial_lat": "float64",       # WFIGS InitialLatitude
    "initial_lon": "float64",       # WFIGS InitialLongitude
    "unit_id": STRING,              # FPA-FOD NWCG_REPORTING_UNIT_ID / WFIGS POOProtectingUnit
    "license": STRING,
    "ingested_at": TS_UTC,
}


# ---------------------------------------------------------------------------- value helpers
def num(v: Any) -> float | None:
    """Finite float or None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def acres_to_ha(acres: float | None) -> float | None:
    return None if acres is None else acres * ACRE_TO_HA


def size_class(acres: float | None) -> str | None:
    """NWCG size class of a final size in acres (None if unknown)."""
    if acres is None:
        return None
    if acres <= 0.25:
        return "A"
    for cls, upper in SIZE_CLASS_BOUNDS:
        if acres < upper:
            return cls
    return "G"


def cause_class(v: Any) -> str | None:
    s = clean_text(v)
    if not s:
        return None
    low = s.lower()
    if low.startswith("human"):
        return "human"
    if low.startswith("natural"):
        return "natural"
    if low.startswith(("missing", "undetermined", "unknown")):
        return "undetermined"
    return low


def local_date(dt: datetime | None) -> date | None:
    """Pacific calendar date of a UTC datetime."""
    return dt.astimezone(PACIFIC).date() if dt else None


def local_time_to_utc(d: date | None, hhmm: Any) -> datetime | None:
    """UTC datetime of a local (Pacific) date and ``HHMM`` time; None if either is missing or invalid."""
    s = clean_text(hhmm)
    if d is None or not s or not s.isdigit() or len(s) > 4:
        return None
    s = s.zfill(4)
    h, m = int(s[:2]), int(s[2:])
    if h > 23 or m > 59:
        return None
    return datetime(d.year, d.month, d.day, h, m, tzinfo=PACIFIC).astimezone(UTC)


def local_year_bounds_utc(year: int) -> tuple[datetime, datetime]:
    """UTC instants of local (Pacific) midnight on 1 January of ``year`` and of ``year + 1``."""
    return (datetime(year, 1, 1, tzinfo=PACIFIC).astimezone(UTC),
            datetime(year + 1, 1, 1, tzinfo=PACIFIC).astimezone(UTC))


def arcgis_ts(dt: datetime) -> str:
    return f"TIMESTAMP '{dt.astimezone(UTC):%Y-%m-%d %H:%M:%S}'"


def wfigs_location(attrs: Mapping[str, Any], geom: Mapping[str, Any] | None,
                   prefer: str = "geometry") -> tuple[float | None, float | None, str | None]:
    """``(lon, lat, location_source)`` of a WFIGS record: the layer point geometry, else Initial* (or reversed)."""
    g = (num((geom or {}).get("x")), num((geom or {}).get("y")), "geometry")
    i = (num(attrs.get("InitialLongitude")), num(attrs.get("InitialLatitude")), "initial")
    for lon, lat, src in ((g, i) if prefer == "geometry" else (i, g)):
        if lon is not None and lat is not None and (lon, lat) != (0.0, 0.0):
            return lon, lat, src
    return None, None, None


def record_location(source: str, rec: Mapping[str, Any], prefer: str = "geometry") -> tuple[float | None,
                                                                                              float | None]:
    a = rec.get("attributes") or {}
    if source == "fpa-fod":
        return num(a.get("longitude")), num(a.get("latitude"))
    lon, lat, _ = wfigs_location(a, rec.get("geometry"), prefer)
    return lon, lat


# ---------------------------------------------------------------------------- row normalisation
def fpa_row(a: Mapping[str, Any]) -> dict:
    fod = a.get("fod_id")
    acres = num(a.get("fire_size"))
    d = epoch_ms_to_date(a.get("discovery_date"))     # local date stored as 00:00 UTC: take the UTC date
    year = a.get("fire_year")
    return {
        "ignition_id": sha1("fpa-fod", fod), "source": "fpa-fod", "source_id": None if fod is None else str(fod),
        "fod_id": None if fod is None else int(fod), "fpa_id": clean_text(a.get("fpa_id")), "irwin_id": None,
        "unique_fire_id": None, "mtbs_id": clean_text(a.get("mtbs_id")),
        "ics209_incident_id": clean_text(a.get("ics_209_plus_incident_join_id")),
        "fire_name": clean_text(a.get("fire_name")), "complex_name": clean_text(a.get("complex_name")),
        "fire_year": int(year) if year is not None else (d.year if d else None),
        "discovery_date": d, "discovery_datetime": local_time_to_utc(d, a.get("discovery_time")),
        "containment_date": epoch_ms_to_date(a.get("cont_date")), "fire_out_date": None,
        "fire_size_ac": acres, "fire_size_ha": acres_to_ha(acres),
        "size_class": clean_text(a.get("fire_size_class")) or size_class(acres),
        "size_field": "FIRE_SIZE" if acres is not None else None, "discovery_size_ac": None,
        "cause_class": cause_class(a.get("nwcg_cause_classification")),
        "cause_general": clean_text(a.get("nwcg_general_cause")),
        "lat": num(a.get("latitude")), "lon": num(a.get("longitude")), "location_source": "latitude/longitude",
        "initial_lat": None, "initial_lon": None, "unit_id": clean_text(a.get("nwcg_reporting_unit_id")),
        "license": LICENSES["fpa-fod"],
    }


def wfigs_row(a: Mapping[str, Any], geom: Mapping[str, Any] | None, prefer: str = "geometry") -> dict:
    irwin = clean_guid(a.get("IrwinID"))
    sid = irwin or f"objectid:{a.get('OBJECTID')}"
    disc = epoch_ms_to_datetime(a.get("FireDiscoveryDateTime"))
    d = local_date(disc)
    inc, final = num(a.get("IncidentSize")), num(a.get("FinalAcres"))
    acres, field = (inc, "IncidentSize") if inc is not None else (final, "FinalAcres") if final is not None \
        else (None, None)
    lon, lat, loc = wfigs_location(a, geom, prefer)
    return {
        "ignition_id": sha1("nifc-wfigs", sid), "source": "nifc-wfigs", "source_id": sid, "fod_id": None,
        "fpa_id": None, "irwin_id": irwin, "unique_fire_id": clean_text(a.get("UniqueFireIdentifier")),
        "mtbs_id": None, "ics209_incident_id": None, "fire_name": clean_text(a.get("IncidentName")),
        "complex_name": clean_text(a.get("CpxName")), "fire_year": d.year if d else None,
        "discovery_date": d, "discovery_datetime": disc,
        "containment_date": local_date(epoch_ms_to_datetime(a.get("ContainmentDateTime"))),
        "fire_out_date": local_date(epoch_ms_to_datetime(a.get("FireOutDateTime"))),
        "fire_size_ac": acres, "fire_size_ha": acres_to_ha(acres), "size_class": size_class(acres),
        "size_field": field, "discovery_size_ac": num(a.get("DiscoveryAcres")),
        "cause_class": cause_class(a.get("FireCause")), "cause_general": clean_text(a.get("FireCauseGeneral")),
        "lat": lat, "lon": lon, "location_source": loc,
        "initial_lat": num(a.get("InitialLatitude")), "initial_lon": num(a.get("InitialLongitude")),
        "unit_id": clean_text(a.get("POOProtectingUnit")), "license": LICENSES["wfigs"],
    }


def records_to_rows(source: str, records: Iterable[Mapping[str, Any]], prefer: str = "geometry") -> list[dict]:
    """Normalised rows of raw ArcGIS records (``{"attributes", "geometry"}``); rows without a location dropped."""
    out = []
    for rec in records:
        a = rec.get("attributes") or {}
        row = fpa_row(a) if source == "fpa-fod" else wfigs_row(a, rec.get("geometry"), prefer)
        if row["lon"] is None or row["lat"] is None:
            continue
        out.append(row)
    return out


def rows_to_frame(rows: list[dict], ingested_at: datetime | None = None):
    """Typed GeoDataFrame (columns of :data:`IGNITION_SCHEMA` + point geometry), unique on ``ignition_id``."""
    from shapely.geometry import Point

    ingested_at = ingested_at or datetime.now(UTC)
    seen: set[str] = set()
    keep = []
    for r in rows:
        if r["ignition_id"] in seen:
            continue
        seen.add(r["ignition_id"])
        keep.append({**r, "ingested_at": ingested_at})
    gdf = typed_frame(keep, IGNITION_SCHEMA, [Point(r["lon"], r["lat"]) for r in keep])
    return gdf.sort_values(["discovery_date", "discovery_datetime", "ignition_id"],
                           na_position="last").reset_index(drop=True)


# ---------------------------------------------------------------------------- duplicates across sources
def _metric_xy(lon: Sequence[float], lat: Sequence[float]) -> np.ndarray:
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True)
    x, y = tr.transform(np.asarray(lon, dtype="float64"), np.asarray(lat, dtype="float64"))
    return np.column_stack([np.atleast_1d(x), np.atleast_1d(y)])


def _day_numbers(dates: Sequence[date | None]) -> np.ndarray:
    return np.array([d.toordinal() if d else np.nan for d in dates], dtype="float64")


def match_duplicates(cand: Sequence[Mapping[str, Any]], ref: Sequence[Mapping[str, Any]], *,
                     max_m: float = 2000.0, max_days: int = 2, exclude_self: bool = False) -> list[int | None]:
    """For each candidate row, the index of the nearest reference row within ``max_m`` metres (EPSG:3310) whose
    ``discovery_date`` differs by at most ``max_days`` days, else None. Rows need ``lon``, ``lat`` and
    ``discovery_date``. With ``exclude_self`` (same list twice) a row never matches itself."""
    out: list[int | None] = [None] * len(cand)
    if not cand or not ref:
        return out
    from scipy.spatial import cKDTree

    cxy = _metric_xy([r["lon"] for r in cand], [r["lat"] for r in cand])
    rxy = _metric_xy([r["lon"] for r in ref], [r["lat"] for r in ref])
    cday = _day_numbers([r["discovery_date"] for r in cand])
    rday = _day_numbers([r["discovery_date"] for r in ref])
    tree = cKDTree(rxy)
    for i, near in enumerate(tree.query_ball_point(cxy, r=max_m)):
        best, best_d = None, math.inf
        for j in near:
            if (exclude_self and j == i) or np.isnan(cday[i]) or np.isnan(rday[j]):
                continue
            if abs(cday[i] - rday[j]) > max_days:
                continue
            dist = float(np.hypot(*(cxy[i] - rxy[j])))
            if dist < best_d:
                best, best_d = j, dist
        out[i] = best
    return out


# ---------------------------------------------------------------------------- connector
class BatchMismatch(RuntimeError):
    """An object-id batch returned other records than the ids listed for it."""


class FireIgnitionsConnector(BaseConnector):
    """FPA-FOD ignitions up to its last year, NIFC WFIGS incident locations after it (California)."""

    @property
    def opts(self) -> dict:
        return self.dataset.options

    def layer(self, source: str) -> str:
        default = FPA_LAYER if source == "fpa-fod" else WFIGS_LAYER
        return str(self.opts.get(f"{source.replace('-', '_')}_layer", default)).rstrip("/")

    @property
    def max_m(self) -> float:
        return float(self.opts.get("dedupe_max_m", 2000))

    @property
    def max_days(self) -> int:
        return int(self.opts.get("dedupe_max_days", 2))

    @property
    def wfigs_prefer(self) -> str:
        return str(self.opts.get("wfigs_location", "geometry"))

    def query_bbox(self, aoi: AOI, extra_m: float = 0.0) -> tuple[float, float, float, float]:
        return buffer_bbox_m(aoi.bbox, float(self.opts.get("buffer_m", 1000)) + extra_m)

    # ------------------------------------------------------------------ HTTP
    def _get_json(self, session, url: str, params: dict) -> dict:
        """GET ``url`` as JSON; retries network errors and HTTP 5xx (``options.http_retries``), not 4xx."""
        import requests

        tries = max(1, int(self.opts.get("http_retries", 3)))
        for attempt in range(1, tries + 1):
            try:
                r = session.get(url, params=params, timeout=max(self.settings.http_timeout, 120))
            except requests.RequestException as exc:
                if attempt == tries:
                    raise
                log.warning("fire-ignitions: %s (attempt %d/%d): %s", url, attempt, tries, exc)
                time.sleep(2.0 * attempt)
                continue
            if r.status_code >= 500 and attempt < tries:
                log.warning("fire-ignitions: %s HTTP %s (attempt %d/%d)", url, r.status_code, attempt, tries)
                time.sleep(2.0 * attempt)
                continue
            r.raise_for_status()
            d = r.json()
            if isinstance(d, dict) and "error" in d:
                raise RuntimeError(f"ArcGIS error from {url}: {d['error']}")
            return d
        raise RuntimeError(f"{url}: no response after {tries} attempts")

    def _stats(self, session, source: str, where: str, stats: list[dict], group_by: str | None = None) -> list[dict]:
        params = {"where": where, "outStatistics": json.dumps(stats), "f": "json"}
        if group_by:
            params.update(groupByFieldsForStatistics=group_by, orderByFields=group_by)
        d = self._get_json(session, f"{self.layer(source)}/query", params)
        return [f.get("attributes") or {} for f in d.get("features") or []]

    def page_size(self, session, source: str) -> int:
        """``min(options.page_size, layer maxRecordCount)``."""
        meta = self._get_json(session, self.layer(source), {"f": "json"})
        limit = int(meta.get("maxRecordCount") or 1000)
        return max(1, min(int(self.opts.get("page_size", 2000)), limit))

    def fetch_records(self, session, source: str, where: str, out_fields: Sequence[str], page_size: int, *,
                      bbox: tuple[float, float, float, float] | None = None,
                      geometry: bool = False) -> list[dict]:
        """All records matching ``where`` (and the envelope ``bbox``) via object-id batching, sorted by id.
        A batch that does not return exactly its ids (the live WFIGS layer changed meanwhile) restarts the
        download once, then fails."""
        try:
            return self._fetch_records(session, source, where, out_fields, page_size, bbox, geometry)
        except BatchMismatch as exc:
            log.warning("fire-ignitions: %s; listing the ids again", exc)
            return self._fetch_records(session, source, where, out_fields, page_size, bbox, geometry)

    def _fetch_records(self, session, source, where, out_fields, page_size, bbox, geometry) -> list[dict]:
        url = f"{self.layer(source)}/query"
        spatial = {}
        if bbox is not None:
            w, s, e, n = bbox
            spatial = {"geometry": f"{w:.6f},{s:.6f},{e:.6f},{n:.6f}", "geometryType": "esriGeometryEnvelope",
                       "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
        d = self._get_json(session, url, {"where": where, **spatial, "returnIdsOnly": "true", "f": "json"})
        oid = d.get("objectIdFieldName") or "OBJECTID"
        ids = sorted(int(i) for i in d.get("objectIds") or [])
        records: list[dict] = []
        for i in range(0, len(ids), page_size):
            batch = ids[i:i + page_size]
            params = {"where": f"({where}) AND {oid} >= {batch[0]} AND {oid} <= {batch[-1]}", **spatial,
                      "outFields": ",".join(dict.fromkeys([oid, *out_fields])),
                      "returnGeometry": "true" if geometry else "false", "f": "json"}
            if geometry:
                params.update(outSR="4326", geometryPrecision="6")
            page = self._get_json(session, url, params)
            feats = sorted(page.get("features") or [], key=lambda f: int((f.get("attributes") or {}).get(oid)))
            got = [int(f["attributes"][oid]) for f in feats]
            if page.get("exceededTransferLimit") or got != batch:
                raise BatchMismatch(f"{source}: object-id batch {batch[0]}..{batch[-1]} returned {len(got)} of "
                                   f"{len(batch)} records (exceededTransferLimit={page.get('exceededTransferLimit')}"
                                   "); the layer changed during the download or the page is too large")
            records.extend({"attributes": f.get("attributes") or {}, "geometry": f.get("geometry")} for f in feats)
        return records

    # ------------------------------------------------------------------ source facts
    def fpa_info(self, session) -> dict:
        """FPA-FOD edition token, coverage and California rows per year (one service + two statistics queries)."""
        svc = self._get_json(session, self.layer("fpa-fod").rsplit("/", 1)[0], {"f": "json"})
        text = " ".join(str(svc.get(k) or "") for k in ("serviceDescription", "description"))
        m = re.search(r"FPA_FOD_(\d{8})", text)
        if not m:
            meta = self._get_json(session, self.layer("fpa-fod"), {"f": "json"})
            m = re.search(r"(\d+)(?:st|nd|rd|th)", str(meta.get("name") or ""))
            if not m:
                raise RuntimeError("fpa-fod: no edition token (FPA_FOD_YYYYMMDD or 'Nth') in the service metadata")
        version = m.group(1)
        (span,) = self._stats(session, "fpa-fod", "1=1", [
            {"statisticType": "max", "onStatisticField": "fire_year", "outStatisticFieldName": "maxy"},
            {"statisticType": "max", "onStatisticField": "discovery_date", "outStatisticFieldName": "maxd"}])
        state = str(self.opts.get("fpa_state", "CA"))
        counts = {int(a["fire_year"]): int(a["n"]) for a in self._stats(
            session, "fpa-fod", f"state = '{state}'",
            [{"statisticType": "count", "onStatisticField": "fod_id", "outStatisticFieldName": "n"}], "fire_year")}
        return {"version": version, "max_year": int(span["maxy"]),
                "max_date": epoch_ms_to_date(span["maxd"]).isoformat(), "counts": counts}

    def fpa_where(self, year: int) -> str:
        return f"state = '{self.opts.get('fpa_state', 'CA')}' AND fire_year = {int(year)}"

    def wfigs_base_where(self) -> str:
        return (f"POOState = '{self.opts.get('wfigs_state', 'US-CA')}' AND "
                f"IncidentTypeCategory = '{self.opts.get('wfigs_category', 'WF')}'")

    def wfigs_where(self, year: int) -> str:
        lo, hi = local_year_bounds_utc(year)
        return (f"{self.wfigs_base_where()} AND FireDiscoveryDateTime >= {arcgis_ts(lo)} AND "
                f"FireDiscoveryDateTime < {arcgis_ts(hi)}")

    def wfigs_info(self, session, year: int) -> dict:
        (a,) = self._stats(session, "wfigs", self.wfigs_where(year), [
            {"statisticType": "count", "onStatisticField": "OBJECTID", "outStatisticFieldName": "n"},
            {"statisticType": "max", "onStatisticField": "ModifiedOnDateTime_dt", "outStatisticFieldName": "mod"}])
        mod = epoch_ms_to_datetime(a.get("mod"))
        return {"n": int(a.get("n") or 0), "modified": f"{mod:%Y%m%d}" if mod else "none"}

    @staticmethod
    def bbox_where(bbox: tuple[float, float, float, float]) -> str:
        w, s, e, n = bbox
        return (f"latitude >= {s:.6f} AND latitude <= {n:.6f} AND longitude >= {w:.6f} AND "
                f"longitude <= {e:.6f}")

    # ------------------------------------------------------------------ discover
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        s = self.http_session()
        sources = [x for x in self.opts.get("sources", list(SOURCES)) if x in SOURCES]
        fpa = self.fpa_info(s) if "fpa-fod" in sources else None
        fpa_last = fpa["max_year"] if fpa else WFIGS_FIRST_YEAR - 1
        this_year = datetime.now(UTC).year
        page = {src: self.page_size(s, src) for src in sources}
        refs = []
        for year in range(period.start.year, period.end.year + 1):
            if fpa and FPA_FIRST_YEAR <= year <= fpa_last:
                n = fpa["counts"].get(year, 0)
                key = f"fpafod-{aoi.id}-{year}-v{fpa['version']}-n{n}"
                refs.append(self.ref(key, datetime(year, 1, 1, tzinfo=UTC), [self.layer("fpa-fod")],
                                     source="fpa-fod", year=year, version=fpa["version"], n_state=n,
                                     page_size=page["fpa-fod"], fpa_last_year=fpa_last))
            elif "wfigs" in sources and year > fpa_last and WFIGS_FIRST_YEAR <= year <= this_year:
                info = self.wfigs_info(s, year)
                key = f"wfigs-{aoi.id}-{year}-v{info['modified']}-n{info['n']}"
                dedupe = fpa is not None and date(year, 1, 1) - timedelta(days=self.max_days) <= \
                    date.fromisoformat(fpa["max_date"])
                if dedupe:
                    key += f"-x{fpa['version']}"
                refs.append(self.ref(key, datetime(year, 1, 1, tzinfo=UTC), [self.layer("wfigs")], source="wfigs",
                                     year=year, version=info["modified"], n_state=info["n"],
                                     page_size=page["wfigs"], fpa_last_year=fpa_last,
                                     dedupe_against_fpa=dedupe, fpa_version=fpa["version"] if fpa else None,
                                     fpa_page_size=page.get("fpa-fod")))
            else:
                log.info("fire-ignitions %s: no configured source covers %s", aoi.id, year)
        return refs

    # ------------------------------------------------------------------ extract
    def region_records(self, source: str, records: list[dict]) -> tuple[list[dict], int, int]:
        """Records with a location inside the lake region; also the numbers without location / outside."""
        locs = [record_location(source, r, self.wfigs_prefer) for r in records]
        located = [(r, lon, lat) for r, (lon, lat) in zip(records, locs, strict=True)
                   if lon is not None and lat is not None]
        n_no_loc = len(records) - len(located)
        region = lake_region()
        if region is None or not located:
            return [r for r, _, _ in located], n_no_loc, 0
        inside = region.contains_points([x for _, x, _ in located], [y for _, _, y in located])
        keep = [r for (r, _, _), ok in zip(located, inside, strict=True) if ok]
        return keep, n_no_loc, len(located) - len(keep)

    def bbox_records(self, source: str, records: list[dict], bbox) -> list[dict]:
        w, s, e, n = bbox
        out = []
        for r in records:
            lon, lat = record_location(source, r, self.wfigs_prefer)
            if lon is None or lat is None or (w <= lon <= e and s <= lat <= n):
                out.append(r)       # records without a location are counted (and dropped) by region_records
        return out

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        source, year = ref.meta["source"], int(ref.meta["year"])
        s = self.http_session()
        bbox = self.query_bbox(aoi)
        if source == "fpa-fod":
            where = f"{self.fpa_where(year)} AND {self.bbox_where(bbox)}"
            records = self.fetch_records(s, source, where, FPA_FIELDS, int(ref.meta["page_size"]))
        else:
            where = self.wfigs_where(year)
            records = self.fetch_records(s, source, where, WFIGS_FIELDS, int(ref.meta["page_size"]), bbox=bbox,
                                         geometry=True)
            records = self.bbox_records(source, records, bbox)
        n_fetched = len(records)
        records, n_no_loc, n_out = self.region_records(source, records)
        reference, ref_where = [], None
        if source == "wfigs" and ref.meta.get("dedupe_against_fpa"):
            lo = date(year, 1, 1) - timedelta(days=self.max_days)
            hi = date(year + 1, 1, 1) + timedelta(days=self.max_days)
            ref_where = (f"state = '{self.opts.get('fpa_state', 'CA')}' AND "
                         f"discovery_date >= TIMESTAMP '{lo:%Y-%m-%d} 00:00:00' AND "
                         f"discovery_date < TIMESTAMP '{hi:%Y-%m-%d} 00:00:00' AND "
                         f"{self.bbox_where(self.query_bbox(aoi, extra_m=self.max_m))}")
            reference = self.fetch_records(s, "fpa-fod", ref_where, FPA_FIELDS,
                                           int(ref.meta.get("fpa_page_size") or ref.meta["page_size"]))
        log.info("fire-ignitions %s: %d records kept (%d fetched, %d without location, %d outside the region)",
                 ref.key, len(records), n_fetched, n_no_loc, n_out)
        doc = {"source": source, "year": year, "layer": self.layer(source), "where": where,
               "query_bbox": list(bbox), "records": records, "reference": reference,
               "fuellake": {"version": ref.meta.get("version"), "n_fetched": n_fetched,
                            "n_no_location": n_no_loc, "n_outside_region": n_out,
                            "page_size": ref.meta.get("page_size"), "reference_where": ref_where,
                            "reference_layer": self.layer("fpa-fod") if reference or ref_where else None,
                            "wfigs_location": self.wfigs_prefer if source == "wfigs" else None}}
        rel = self.raw_path(ref, f"{KEY_PREFIX[source]}_{aoi.id}_{year}.json")
        self.lake.write_bytes(rel, json.dumps(doc).encode())
        return [rel]

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        source, year = ref.meta["source"], int(ref.meta["year"])
        doc = json.loads(self.lake.read_bytes(raw[0]))
        meta = doc.get("fuellake") or {}
        rows = records_to_rows(source, doc.get("records") or [], self.wfigs_prefer)
        dedupe: dict[str, Any] = {"applied": False}
        if source == "wfigs" and ref.meta.get("dedupe_against_fpa"):
            ref_rows = records_to_rows("fpa-fod", doc.get("reference") or [])
            hits = match_duplicates(rows, ref_rows, max_m=self.max_m, max_days=self.max_days)
            removed = [{"irwin_id": rows[i]["irwin_id"], "fire_name": rows[i]["fire_name"],
                        "discovery_date": str(rows[i]["discovery_date"]), "fod_id": ref_rows[j]["fod_id"],
                        "fpa_fire_name": ref_rows[j]["fire_name"],
                        "fpa_discovery_date": str(ref_rows[j]["discovery_date"])}
                       for i, j in enumerate(hits) if j is not None]
            rows = [r for r, j in zip(rows, hits, strict=True) if j is None]
            dedupe = {"applied": True, "against": "fpa-fod", "max_m": self.max_m, "max_days": self.max_days,
                      "reference_rows": len(ref_rows), "n_removed": len(removed), "removed": removed[:100]}
        same = match_duplicates(rows, rows, max_m=self.max_m, max_days=self.max_days, exclude_self=True)
        gdf = rows_to_frame(rows)
        path = self.table_path(f"{KEY_PREFIX[source]}-{aoi.id}-{year}")
        write_geoparquet(self.lake, path, gdf)
        start = datetime(year, 1, 1, tzinfo=UTC)
        end = datetime(year + 1, 1, 1, tzinfo=UTC) - timedelta(microseconds=1)
        qb = tuple(doc.get("query_bbox") or self.query_bbox(aoi))
        bbox = tuple(float(v) for v in gdf.total_bounds) if len(gdf) else qb
        sizes = gdf["fire_size_ha"]
        largest = None
        if len(gdf) and sizes.notna().any():
            top = gdf.loc[sizes.idxmax()]
            largest = {"fire_name": None if pd.isna(top["fire_name"]) else str(top["fire_name"]),
                       "fire_size_ac": float(top["fire_size_ac"]), "discovery_date": str(top["discovery_date"]),
                       "source_id": str(top["source_id"])}
        props = {
            "fire_ignitions:source": SOURCE_NAMES[source], "fire_ignitions:year": year,
            "fire_ignitions:layer": doc.get("layer"), "fire_ignitions:where": doc.get("where"),
            "fire_ignitions:version": meta.get("version"), "fire_ignitions:query_bbox": list(qb),
            "fire_ignitions:page_size": meta.get("page_size"),
            "fire_ignitions:n_fetched": int(meta.get("n_fetched") or 0),
            "fire_ignitions:n_no_location": int(meta.get("n_no_location") or 0),
            "fire_ignitions:n_outside_region": int(meta.get("n_outside_region") or 0),
            "fire_ignitions:n_size_missing": int(sizes.isna().sum()),
            "fire_ignitions:n_not_out": int(gdf["fire_out_date"].isna().sum()) if source == "wfigs" else None,
            "fire_ignitions:n_ge_400ha": int((sizes >= 400.0).sum()),
            "fire_ignitions:n_same_source_near": sum(1 for j in same if j is not None),
            "fire_ignitions:dedupe": dedupe, "fire_ignitions:largest_fire": largest,
        }
        if source == "wfigs":
            props["fire_ignitions:wfigs_location"] = meta.get("wfigs_location") or self.wfigs_prefer
            props["fire_ignitions:n_location_initial"] = int((gdf["location_source"] == "initial").sum())
        disc = SOURCE_NAMES[source].removeprefix("nifc-")
        return [StagedItem(item_id=self.item_id(aoi, ref, disc), datetime=start, properties=props,
                           assets=[self.table_asset(path)], bbox=bbox, start_datetime=start, end_datetime=end)]
