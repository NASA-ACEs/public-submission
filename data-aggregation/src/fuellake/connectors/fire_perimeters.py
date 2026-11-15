# fuel-lake — historical fire perimeters (CAL FIRE FRAP, NIFC WFIGS for the current year) as GeoParquet polygons
# Licensed under Apache-2.0.
"""Fire perimeter polygons around an AOI, one table part per source x AOI x calendar year.

Sources (both public ArcGIS Online feature layers, no login; verified live 2026-11-14):

- ``frap``: CAL FIRE FRAP "California Fire Perimeters (all)" (item c3c10388e3b24cec8a954ba10458039d), years
  1878 to the latest annual release (2025 on 2026-11-14). Year field ``YEAR_``.
- ``wfigs``: NIFC "WFIGS Interagency Fire Perimeters" (item 5e72b1699bf74eefb3f3aff6f4ba5511), operational
  perimeters, used only for years after the last FRAP year (the current season). Year = year of
  ``attr_FireDiscoveryDateTime``. It mixes daily and final perimeters and 0-acre records
  (column ``feature_category`` keeps that distinction).

Selection: perimeters of that year whose geometry **intersects** the AOI bbox widened by ``buffer_m``
(default 5 km) **and** the lake region (the California polygon buffered by its ``tolerance_m``). Polygons are
kept whole (not clipped), so ``area_acres`` refers to the full fire; a fire crossing the state line is kept.
On the region AOI ``ca`` the bbox is the state's envelope, so WFIGS fires in Nevada, Oregon, Arizona or
Mexico are dropped before the raw GeoJSON is written (``fuellake.n_outside_region`` in that file).
Option ``max_allowable_offset_deg`` asks ArcGIS to generalise the geometries (``maxAllowableOffset``,
degrees): a number for every AOI or a mapping from AOI scale to a number. The shipped config sets
``{region: 0.0005}`` (about 50 m, well below the 200 m ``ca`` cells), which shrinks a statewide year from roughly
100 MB to under 10 MB; local AOIs get full-precision polygons. A set offset is part of the source key
(``-g<offset>``).

Units: whole calendar years touched by the period. ``SourceRef.key`` = ``<source>-<aoi>-<year>-v<YYYYMMDD>``
where the date is the layer's ``editingInfo.dataLastEditDate``: a rerun is a no-op until the source layer
changes, then the year is fetched again and the item/part (whose ids depend only on source, AOI and year) is
replaced. A year without perimeters still writes a zero-row part so that it is not retried forever.

Table ``fire_perimeters`` columns: see :data:`PERIMETER_SCHEMA`. Geometry is WKB (Multi)Polygon in EPSG:4326.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import pyarrow as pa

from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.registry import AOI, lake_region
from fuellake.tables import write_geoparquet

log = logging.getLogger("fuellake.connectors.fire_perimeters")

FRAP_LAYER = ("https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/arcgis/rest/services/"
              "California_Historic_Fire_Perimeters/FeatureServer/0")
WFIGS_LAYER = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
               "WFIGS_Interagency_Perimeters/FeatureServer/0")
SOURCE_NAMES = {"frap": "cal-fire-frap", "wfigs": "nifc-wfigs"}
LICENSES = {
    "frap": "CAL FIRE FRAP: no licence stated; public layer with disclaimer (contact calfire.egis@fire.ca.gov)",
    "wfigs": "NIFC WFIGS: no licence stated; public US federal layer with disclaimer",
}
ACRE_TO_HA = 0.40468564224
M_PER_DEG_LAT = 111_320.0

# column -> pandas dtype used for every part (also for zero-row parts, so all parts share one schema)
STRING = pd.ArrowDtype(pa.string())
DATE32 = pd.ArrowDtype(pa.date32())
TS_UTC = "datetime64[us, UTC]"
PERIMETER_SCHEMA: dict[str, Any] = {
    "perimeter_id": STRING,       # sha1("source|source_feature_id")
    "source": STRING,             # cal-fire-frap | nifc-wfigs
    "event_id": STRING,           # IRWIN id if known, else a source-native incident key
    "irwin_id": STRING,
    "fire_name": STRING,
    "fire_year": "Int32",
    "start_date": DATE32,         # FRAP ALARM_DATE / WFIGS attr_FireDiscoveryDateTime (UTC date)
    "containment_date": DATE32,   # FRAP CONT_DATE / WFIGS attr_ContainmentDateTime
    "fire_out_date": DATE32,      # WFIGS attr_FireOutDateTime (FRAP: null)
    "area_acres": "float64",      # source GIS acres of the whole perimeter
    "area_ha": "float64",
    "agency": STRING,
    "unit_id": STRING,
    "cause": STRING,              # FRAP numeric cause code as text / WFIGS attr_FireCause
    "feature_category": STRING,   # FRAP: "final perimeter"; WFIGS: poly_FeatureCategory
    "perimeter_datetime": TS_UTC,  # WFIGS poly_PolygonDateTime (FRAP: null)
    "source_feature_id": STRING,  # FRAP GlobalID / WFIGS poly_SourceGlobalID or GlobalID
    "centroid_lon": "float64",
    "centroid_lat": "float64",
    "license": STRING,
    "ingested_at": TS_UTC,
}


# ---------------------------------------------------------------------------- shared table helpers (also FIRMS)
def typed_frame(rows: list[dict], schema: Mapping[str, Any], geometry: list | None = None):
    """Build a GeoDataFrame with exactly the columns and dtypes of ``schema`` (+ ``geometry`` in EPSG:4326).

    Explicit dtypes keep all-null and zero-row parts on the same Arrow types as full ones.
    """
    import geopandas as gpd

    data = {c: pd.array([r.get(c) for r in rows], dtype=dt) for c, dt in schema.items()}
    df = pd.DataFrame(data)
    geom = gpd.GeoSeries(geometry if geometry is not None else [], crs="EPSG:4326")
    if len(geom) != len(df):
        raise ValueError(f"{len(geom)} geometries for {len(df)} rows")
    return gpd.GeoDataFrame(df, geometry=geom.values, crs="EPSG:4326")


def sha1(*parts: Any) -> str:
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


def epoch_ms_to_datetime(v: Any) -> datetime | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return datetime.fromtimestamp(float(v) / 1000.0, tz=UTC)


def epoch_ms_to_date(v: Any) -> date | None:
    dt = epoch_ms_to_datetime(v)
    return dt.date() if dt else None


def clean_text(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def clean_guid(v: Any) -> str | None:
    s = clean_text(v)
    return s.strip("{}").lower() if s else None


def polygonal(geom):
    """Valid (Multi)Polygon from any shapely geometry: ``make_valid`` only if invalid, keep polygonal parts."""
    from shapely import make_valid
    from shapely.geometry import GeometryCollection, MultiPolygon, Polygon

    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = make_valid(geom)
    if isinstance(geom, Polygon | MultiPolygon):
        return geom
    if isinstance(geom, GeometryCollection):
        polys: list[Polygon] = []
        for g in geom.geoms:
            if isinstance(g, Polygon):
                polys.append(g)
            elif isinstance(g, MultiPolygon):
                polys.extend(g.geoms)
        return MultiPolygon(polys) if polys else None
    return None


def buffer_bbox_m(bbox: Iterable[float], buffer_m: float) -> tuple[float, float, float, float]:
    """Widen a WGS84 bbox by ``buffer_m`` metres (longitude scaled at the bbox mid-latitude)."""
    w, s, e, n = bbox
    dlat = buffer_m / M_PER_DEG_LAT
    dlon = buffer_m / (M_PER_DEG_LAT * max(math.cos(math.radians((s + n) / 2)), 1e-6))
    return (w - dlon, s - dlat, e + dlon, n + dlat)


# ---------------------------------------------------------------------------- row normalisation
def frap_row(props: Mapping[str, Any]) -> dict:
    irwin = clean_guid(props.get("IRWINID"))
    unit, inc, year = clean_text(props.get("UNIT_ID")), clean_text(props.get("INC_NUM")), props.get("YEAR_")
    name = clean_text(props.get("FIRE_NAME"))
    event = irwin or f"frap:{year}:{unit or 'na'}:{inc or (name or 'unnamed').lower()}"
    acres = props.get("GIS_ACRES")
    cause = props.get("CAUSE")
    fid = clean_text(props.get("GlobalID")) or f"objectid:{props.get('OBJECTID')}"
    return {
        "perimeter_id": sha1("cal-fire-frap", fid), "source": "cal-fire-frap", "event_id": event,
        "irwin_id": irwin, "fire_name": name, "fire_year": int(year) if year is not None else None,
        "start_date": epoch_ms_to_date(props.get("ALARM_DATE")),
        "containment_date": epoch_ms_to_date(props.get("CONT_DATE")), "fire_out_date": None,
        "area_acres": float(acres) if acres is not None else None,
        "area_ha": float(acres) * ACRE_TO_HA if acres is not None else None,
        "agency": clean_text(props.get("AGENCY")), "unit_id": unit,
        "cause": None if cause is None else str(int(cause)) if isinstance(cause, int | float) else str(cause),
        "feature_category": "final perimeter", "perimeter_datetime": None, "source_feature_id": fid,
        "license": LICENSES["frap"],
    }


def wfigs_row(props: Mapping[str, Any]) -> dict:
    irwin = clean_guid(props.get("attr_IrwinID") or props.get("poly_IRWINID"))
    start = epoch_ms_to_datetime(props.get("attr_FireDiscoveryDateTime"))
    uid = clean_text(props.get("attr_UniqueFireIdentifier"))
    acres = props.get("poly_GISAcres")
    fid = (clean_text(props.get("poly_SourceGlobalID")) or clean_text(props.get("GlobalID"))
           or f"objectid:{props.get('OBJECTID')}")
    return {
        "perimeter_id": sha1("nifc-wfigs", fid), "source": "nifc-wfigs",
        "event_id": irwin or (f"wfigs:{uid}" if uid else f"wfigs:{fid}"),
        "irwin_id": irwin,
        "fire_name": clean_text(props.get("poly_IncidentName") or props.get("attr_IncidentName")),
        "fire_year": start.year if start else None, "start_date": start.date() if start else None,
        "containment_date": epoch_ms_to_date(props.get("attr_ContainmentDateTime")),
        "fire_out_date": epoch_ms_to_date(props.get("attr_FireOutDateTime")),
        "area_acres": float(acres) if acres is not None else None,
        "area_ha": float(acres) * ACRE_TO_HA if acres is not None else None,
        "agency": clean_text(props.get("attr_POOProtectingAgency")),
        "unit_id": clean_text(props.get("attr_POOProtectingUnit")),
        "cause": clean_text(props.get("attr_FireCause")),
        "feature_category": clean_text(props.get("poly_FeatureCategory")),
        "perimeter_datetime": epoch_ms_to_datetime(props.get("poly_PolygonDateTime")),
        "source_feature_id": fid, "license": LICENSES["wfigs"],
    }


def features_to_frame(features: list[dict], source: str, ingested_at: datetime | None = None):
    """GeoJSON features of one source -> typed GeoDataFrame (rows without polygon geometry are dropped)."""
    from shapely.geometry import shape

    to_row = frap_row if source == "frap" else wfigs_row
    ingested_at = ingested_at or datetime.now(UTC)
    rows, geoms, seen = [], [], set()
    for f in features:
        geom = polygonal(shape(f["geometry"])) if f.get("geometry") else None
        if geom is None:
            log.warning("fire-perimeters %s: feature %s has no polygon geometry; dropped", source, f.get("id"))
            continue
        row = to_row(f.get("properties") or {})
        if row["perimeter_id"] in seen:
            continue
        seen.add(row["perimeter_id"])
        pt = geom.representative_point()
        row.update(centroid_lon=float(pt.x), centroid_lat=float(pt.y), ingested_at=ingested_at)
        rows.append(row)
        geoms.append(geom)
    gdf = typed_frame(rows, PERIMETER_SCHEMA, geoms)
    return gdf.sort_values(["start_date", "fire_name", "perimeter_id"], na_position="last").reset_index(drop=True)


# ---------------------------------------------------------------------------- connector
class FirePerimetersConnector(BaseConnector):
    """CAL FIRE FRAP perimeters (and NIFC WFIGS for years after the last FRAP release)."""

    @property
    def opts(self) -> dict:
        return self.dataset.options

    def layer(self, source: str) -> str:
        return str(self.opts.get(f"{source}_layer", FRAP_LAYER if source == "frap" else WFIGS_LAYER)).rstrip("/")

    def query_bbox(self, aoi: AOI) -> tuple[float, float, float, float]:
        return buffer_bbox_m(aoi.bbox, float(self.opts.get("buffer_m", 5000)))

    def _get_json(self, session, url: str, params: dict) -> dict:
        r = session.get(url, params=params, timeout=max(self.settings.http_timeout, 120))
        r.raise_for_status()
        d = r.json()
        if isinstance(d, dict) and "error" in d:
            raise RuntimeError(f"ArcGIS error from {url}: {d['error']}")
        return d

    def layer_version(self, session, source: str) -> str:
        """``YYYYMMDD`` of the layer's ``editingInfo.dataLastEditDate`` (UTC)."""
        meta = self._get_json(session, self.layer(source), {"f": "json"})
        ms = (meta.get("editingInfo") or {}).get("dataLastEditDate") or (meta.get("editingInfo") or {}).get(
            "lastEditDate")
        if not ms:
            raise RuntimeError(f"{source}: layer metadata has no editingInfo.dataLastEditDate")
        return f"{epoch_ms_to_datetime(ms):%Y%m%d}"

    def frap_max_year(self, session) -> int:
        stats = json.dumps([{"statisticType": "max", "onStatisticField": "YEAR_", "outStatisticFieldName": "maxy"}])
        d = self._get_json(session, f"{self.layer('frap')}/query",
                           {"where": "1=1", "outStatistics": stats, "f": "json"})
        return int(d["features"][0]["attributes"]["maxy"])

    def where(self, source: str, year: int) -> str:
        if source == "frap":
            return f"YEAR_ = {int(year)}"
        return (f"attr_FireDiscoveryDateTime >= TIMESTAMP '{year}-01-01 00:00:00' AND "
                f"attr_FireDiscoveryDateTime < TIMESTAMP '{year + 1}-01-01 00:00:00'")

    # ------------------------------------------------------------------ discover
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        s = self.http_session()
        sources = list(self.opts.get("sources", ["frap", "wfigs"]))
        max_frap = self.frap_max_year(s) if "frap" in sources else -1
        versions: dict[str, str] = {}
        refs = []
        for year in range(period.start.year, period.end.year + 1):
            if "frap" in sources and year <= max_frap:
                source = "frap"
            elif "wfigs" in sources and year > max_frap:
                source = "wfigs"
            else:
                log.info("fire-perimeters %s: no configured source covers %s", aoi.id, year)
                continue
            if source not in versions:
                versions[source] = self.layer_version(s, source)
            offset = self.max_allowable_offset(aoi)
            key = f"{source}-{aoi.id}-{year}-v{versions[source]}" + (f"-g{offset:g}" if offset else "")
            refs.append(self.ref(key, datetime(year, 1, 1, tzinfo=UTC), [self.layer(source)], source=source,
                                 year=year, layer_version=versions[source], frap_max_year=max_frap,
                                 max_allowable_offset_deg=offset))
        return refs

    # ------------------------------------------------------------------ extract
    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        source, year = ref.meta["source"], int(ref.meta["year"])
        s = self.http_session()
        w, so, e, n = self.query_bbox(aoi)
        spatial = {"geometry": f"{w:.6f},{so:.6f},{e:.6f},{n:.6f}", "geometryType": "esriGeometryEnvelope",
                   "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
        ids = self._get_json(s, f"{self.layer(source)}/query",
                             {"where": self.where(source, year), **spatial, "returnIdsOnly": "true",
                              "f": "json"}).get("objectIds") or []
        ids = sorted(int(i) for i in ids)
        chunk = int(self.opts.get("ids_per_request", 50))
        offset = ref.meta.get("max_allowable_offset_deg")
        features: list[dict] = []
        for i in range(0, len(ids), chunk):
            params = {"objectIds": ",".join(map(str, ids[i:i + chunk])), "outFields": "*",
                      "outSR": "4326", "geometryPrecision": "6", "f": "geojson"}
            if offset:
                params["maxAllowableOffset"] = str(float(offset))
            part = self._get_json(s, f"{self.layer(source)}/query", params)
            features.extend(part.get("features") or [])
        if len(features) != len(ids):
            raise RuntimeError(f"{source} {year}: asked for {len(ids)} features, got {len(features)}")
        features, n_outside = self.region_features(features)
        fc = {"type": "FeatureCollection", "features": features,
              "fuellake": {"source": source, "year": year, "layer": self.layer(source),
                           "layer_version": ref.meta.get("layer_version"), "query_bbox": [w, so, e, n],
                           "where": self.where(source, year), "n_outside_region": n_outside,
                           "max_allowable_offset_deg": offset}}
        rel = self.raw_path(ref, f"{source}_{aoi.id}_{year}.geojson")
        self.lake.write_bytes(rel, json.dumps(fc).encode())
        return [rel]

    def max_allowable_offset(self, aoi: AOI) -> float | None:
        """Generalisation for this AOI (option ``max_allowable_offset_deg``: number or ``{scale: number}``)."""
        opt = self.opts.get("max_allowable_offset_deg")
        if isinstance(opt, dict):
            opt = opt.get(aoi.scale)
        return float(opt) if opt else None

    def region_features(self, features: list[dict]) -> tuple[list[dict], int]:
        """Features whose geometry intersects the lake region (all of them if the lake has none); also returns
        the number dropped. Features without geometry are kept here and dropped by the transform."""
        region = lake_region()
        if region is None or not features:
            return features, 0
        from shapely.geometry import shape

        geoms = [shape(f["geometry"]) if f.get("geometry") else None for f in features]
        inside = region.rows_inside(geoms)
        keep = [f for f, g, ok in zip(features, geoms, inside, strict=True) if ok or g is None]
        return keep, len(features) - len(keep)

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        source, year = ref.meta["source"], int(ref.meta["year"])
        fc = json.loads(self.lake.read_bytes(raw[0]))
        gdf = features_to_frame(fc.get("features") or [], source)
        path = self.table_path(f"{source}-{aoi.id}-{year}")
        write_geoparquet(self.lake, path, gdf)
        start = datetime(year, 1, 1, tzinfo=UTC)
        end = datetime(year + 1, 1, 1, tzinfo=UTC) - timedelta(microseconds=1)
        qb = self.query_bbox(aoi)
        bbox = tuple(float(v) for v in gdf.total_bounds) if len(gdf) else qb
        props = {
            "fire_perimeters:source": SOURCE_NAMES[source], "fire_perimeters:year": year,
            "fire_perimeters:layer_version": ref.meta.get("layer_version"),
            "fire_perimeters:query_bbox": list(qb),
            "fire_perimeters:buffer_m": float(self.opts.get("buffer_m", 5000)),
            "fire_perimeters:n_outside_region": int((fc.get("fuellake") or {}).get("n_outside_region") or 0),
            "fire_perimeters:max_allowable_offset_deg": ref.meta.get("max_allowable_offset_deg"),
            "fire_perimeters:n_events": int(gdf["event_id"].nunique()) if len(gdf) else 0,
            "fire_perimeters:largest_fire": (str(gdf.loc[gdf["area_acres"].idxmax(), "fire_name"])
                                             if len(gdf) and gdf["area_acres"].notna().any() else None),
        }
        return [StagedItem(item_id=self.item_id(aoi, ref, source), datetime=start, properties=props,
                           assets=[self.table_asset(path)], bbox=bbox, start_datetime=start, end_datetime=end)]
