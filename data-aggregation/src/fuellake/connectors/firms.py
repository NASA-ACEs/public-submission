# fuel-lake — NASA FIRMS active fire detections (yearly country archive without key; area API with MAP_KEY)
# Licensed under Apache-2.0.
"""NASA LANCE FIRMS MODIS / VIIRS active fire detections around an AOI, table ``fire_detections``.

Two access paths (checked live 2026-11-14):

1. **Yearly country archive, no key** (``kind=archive``): one CSV per product, year and country, e.g.
   ``https://firms.modaps.eosdis.nasa.gov/data/country/modis/2018/modis_2018_United_States.csv``. Found for
   ``modis`` 2000-2025 and ``viirs-snpp`` 2012-2025 (2026: HTTP 404; NOAA-20 names tried: 404). MODIS files are
   about 10 MB per year, VIIRS S-NPP about 40 MB. These nationwide files never enter the lake: each is
   downloaded once to the local cache ``$FUELLAKE_CACHE_DIR/firms/<product>/`` (default ``~/.cache/fuel-lake``;
   the file name carries the ``Last-Modified`` date, older versions are deleted), so runs for several AOIs
   download it once. Only the rows inside the AOI bbox (widened by ``buffer_m``) **and** the lake region are
   kept in ``raw/``. Status ``tested`` refers to this path.
2. **Area API, needs FIRMS_MAP_KEY** (``kind=api``): ``/api/area/csv/<MAP_KEY>/<SOURCE>/<w,s,e,n>/<1..5>/<date>``
   for years without any archive file (the current year). One unit per API source and calendar month.
   Without the key the unit fails with a clear message. **This path has not been run** (no key available).
   The key is never written to URLs stored in the catalog, raw files or logs.

Units: whole calendar years (archive) or months (API) touched by the period. Archive keys carry the file's
``Last-Modified`` date, so a re-published archive file is fetched again; API keys of the open month carry
``through-YYYYMMDD``. Item and part ids depend only on product, AOI and year/month.

Columns: see :data:`DETECTION_SCHEMA`. Points in EPSG:4326 (``lon``/``lat`` = pixel centre).

Region: on the region AOI ``ca`` the query bbox is the state's envelope, so the region polygon (within its
``tolerance_m``) removes the Nevada, Oregon, Arizona, Mexico and offshore detections before anything is
written; area-API responses are filtered the same way.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import pandas as pd
import pyarrow as pa

from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.connectors.fire_perimeters import buffer_bbox_m, sha1, typed_frame
from fuellake.connectors.gridmet import month_end, month_starts
from fuellake.registry import AOI, lake_region
from fuellake.tables import write_geoparquet

log = logging.getLogger("fuellake.connectors.firms")

COUNTRY_BASE = "https://firms.modaps.eosdis.nasa.gov/data/country"
API_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
KEY_ENV = "FIRMS_MAP_KEY"
STRING = pd.ArrowDtype(pa.string())
DETECTION_SCHEMA: dict[str, Any] = {
    "detection_id": STRING,       # sha1("product|lat|lon|acq_date|acq_time|satellite")
    "source": STRING,             # "firms"
    "product": STRING,            # archive: modis | viirs-snpp; API: e.g. VIIRS_NOAA20_NRT
    "access": STRING,             # archive-country | api-area
    "satellite": STRING,
    "instrument": STRING,
    "lon": "float64",
    "lat": "float64",
    "acq_datetime": "datetime64[us, UTC]",
    "acq_date": pd.ArrowDtype(pa.date32()),
    "acq_time": STRING,           # HHMM UTC as in the source
    "confidence": STRING,         # source value (MODIS 0-100, VIIRS l/n/h or low/nominal/high)
    "confidence_class": STRING,   # low | nominal | high (MODIS: <30, 30-79, >=80)
    "brightness_k": "float64",    # MODIS brightness (21/22 um) / VIIRS bright_ti4
    "brightness_2_k": "float64",  # MODIS bright_t31 / VIIRS bright_ti5
    "frp_mw": "float64",
    "scan": "float64",
    "track": "float64",
    "daynight": STRING,
    "detection_type": "Int32",    # archive 'type': 0 vegetation fire, 1 volcano, 2 other static, 3 offshore
    "version": STRING,
    "ingested_at": "datetime64[us, UTC]",
}
_VIIRS_CONF = {"l": "low", "n": "nominal", "h": "high", "low": "low", "nominal": "nominal", "high": "high"}


# ---------------------------------------------------------------------------- parsing
def stream_filter_csv(lines, bbox: tuple[float, float, float, float], region=None) -> tuple[str, int, int]:
    """Filter FIRMS CSV lines (iterable of ``str``) to rows inside ``bbox`` and, if given, inside ``region`` (a
    :class:`fuellake.region.Region`; points within its tolerance). Returns ``(csv_text, n_in, n_total)``."""
    import numpy as np

    w, s, e, n = bbox
    out = io.StringIO()
    reader = csv.reader(lines)
    writer = csv.writer(out, lineterminator="\n")
    header = next(reader, None)
    if not header:
        return "", 0, 0
    header = [h.strip().lower() for h in header]
    if "latitude" not in header or "longitude" not in header:
        raise ValueError(f"not a FIRMS CSV header: {header[:6]}")
    ilat, ilon = header.index("latitude"), header.index("longitude")
    writer.writerow(header)
    n_total = 0
    rows, lons, lats = [], [], []
    for row in reader:
        if not row:
            continue
        n_total += 1
        try:
            lat, lon = float(row[ilat]), float(row[ilon])
        except (ValueError, IndexError):
            continue
        if w <= lon <= e and s <= lat <= n:
            rows.append(row)
            lons.append(lon)
            lats.append(lat)
    keep = region.contains_points(lons, lats) if (region is not None and rows) else np.ones(len(rows), bool)
    for row, ok in zip(rows, keep, strict=True):
        if ok:
            writer.writerow(row)
    return out.getvalue(), int(keep.sum()), n_total


def confidence_class(value: Any, instrument: str) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
        return None
    v = str(value).strip().lower()
    if v in _VIIRS_CONF:
        return _VIIRS_CONF[v]
    try:
        pct = float(v)
    except ValueError:
        return None
    return "low" if pct < 30 else "nominal" if pct < 80 else "high"


def detections_frame(csv_text: str, product: str, access: str, bbox: tuple[float, float, float, float] | None = None,
                     ingested_at: datetime | None = None):
    """Parse a FIRMS CSV (archive or API layout, MODIS or VIIRS) into a typed GeoDataFrame."""
    from shapely.geometry import Point

    ingested_at = ingested_at or datetime.now(UTC)
    if not csv_text.strip():
        return typed_frame([], DETECTION_SCHEMA, [])
    raw = pd.read_csv(io.StringIO(csv_text), dtype=str, keep_default_na=False)
    raw.columns = [c.strip().lower() for c in raw.columns]

    def col(*names: str) -> pd.Series:
        for name in names:
            if name in raw.columns:
                return raw[name].replace("", None)
        return pd.Series([None] * len(raw), index=raw.index, dtype=object)

    def num(*names: str) -> pd.Series:
        return pd.to_numeric(col(*names), errors="coerce")

    lat, lon = num("latitude"), num("longitude")
    acq_date = pd.to_datetime(col("acq_date"), format="%Y-%m-%d", errors="coerce")
    acq_time = col("acq_time").fillna("").astype(str).str.strip().str.zfill(4)
    acq_dt = pd.to_datetime(acq_date.dt.strftime("%Y-%m-%d") + " " + acq_time, format="%Y-%m-%d %H%M",
                            errors="coerce", utc=True)
    instrument = col("instrument").fillna("MODIS" if product.lower().startswith("modis") else "VIIRS")
    conf, sats, daynight, version = col("confidence"), col("satellite"), col("daynight"), col("version")
    dtype_col = pd.to_numeric(col("type"), errors="coerce")
    b1, b2 = num("brightness", "bright_ti4"), num("bright_t31", "bright_ti5")
    frp, scan, track = num("frp"), num("scan"), num("track")

    def opt(v: Any) -> float | None:
        return None if pd.isna(v) else float(v)

    rows, geoms = [], []
    for i in range(len(raw)):
        la, lo = lat.iat[i], lon.iat[i]
        if pd.isna(la) or pd.isna(lo) or pd.isna(acq_dt.iat[i]):
            continue
        if bbox is not None and not (bbox[0] <= lo <= bbox[2] and bbox[1] <= la <= bbox[3]):
            continue
        sat, t, d = sats.iat[i], acq_time.iat[i], acq_date.iat[i].date()
        rows.append({
            "detection_id": sha1(product, f"{la:.5f}", f"{lo:.5f}", d.isoformat(), t, sat),
            "source": "firms", "product": product, "access": access, "satellite": sat,
            "instrument": instrument.iat[i], "lon": float(lo), "lat": float(la),
            "acq_datetime": acq_dt.iat[i].to_pydatetime(), "acq_date": d, "acq_time": t,
            "confidence": conf.iat[i], "confidence_class": confidence_class(conf.iat[i], instrument.iat[i]),
            "brightness_k": opt(b1.iat[i]), "brightness_2_k": opt(b2.iat[i]), "frp_mw": opt(frp.iat[i]),
            "scan": opt(scan.iat[i]), "track": opt(track.iat[i]), "daynight": daynight.iat[i],
            "detection_type": None if pd.isna(dtype_col.iat[i]) else int(dtype_col.iat[i]),
            "version": version.iat[i], "ingested_at": ingested_at,
        })
        geoms.append(Point(float(lo), float(la)))
    gdf = typed_frame(rows, DETECTION_SCHEMA, geoms)
    gdf = gdf.drop_duplicates(subset=["detection_id"]).sort_values(["acq_datetime", "lat", "lon"])
    return gdf.reset_index(drop=True)


# ---------------------------------------------------------------------------- connector
class FirmsConnector(BaseConnector):
    """FIRMS detections: keyless yearly country archive; area API (FIRMS_MAP_KEY) for later dates."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._region = lake_region()

    @property
    def opts(self) -> dict:
        return self.dataset.options

    def aoi_bbox(self, aoi: AOI) -> tuple[float, float, float, float]:
        return buffer_bbox_m(aoi.bbox, float(self.opts.get("buffer_m", 1000)))

    def country_url(self, product: str, year: int) -> str:
        base = str(self.opts.get("country_base", COUNTRY_BASE)).rstrip("/")
        country = self.opts.get("country", "United_States")
        return f"{base}/{product}/{year}/{product}_{year}_{country}.csv"

    def api_url(self, source: str, bbox, n_days: int, day: date, key: str = "[MAP_KEY]") -> str:
        base = str(self.opts.get("api_base", API_BASE)).rstrip("/")
        w, s, e, n = bbox
        return f"{base}/{key}/{source}/{w:.5f},{s:.5f},{e:.5f},{n:.5f}/{n_days}/{day:%Y-%m-%d}"

    # ------------------------------------------------------------------ discover
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        s = self.http_session()
        products = list(self.opts.get("archive_products", ["modis", "viirs-snpp"]))
        api_sources = list(self.opts.get("api_sources", ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT",
                                                          "VIIRS_NOAA21_NRT", "MODIS_NRT"]))
        today = datetime.now(UTC).date()
        refs: list[SourceRef] = []
        for year in range(period.start.year, period.end.year + 1):
            found = False
            for product in products:
                url = self.country_url(product, year)
                r = s.head(url, timeout=self.settings.http_timeout, allow_redirects=True)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                found = True
                lm = r.headers.get("Last-Modified")
                ver = f"{parsedate_to_datetime(lm):%Y%m%d}" if lm else "unknown"
                refs.append(self.ref(f"country-{product}-{aoi.id}-{year}-v{ver}", datetime(year, 1, 1, tzinfo=UTC),
                                     [url], kind="archive", product=product, year=year, version=ver,
                                     size_bytes=int(r.headers.get("Content-Length") or 0), last_modified=lm))
            if found:
                continue
            # No archive file for any product: this year needs the area API (one unit per source x month).
            y_start, y_end = max(period.start, date(year, 1, 1)), min(period.end, date(year, 12, 31), today)
            if y_end < y_start:       # the part of the period in this year lies in the future
                continue
            for first in month_starts(Period(y_start, y_end)):
                last = min(month_end(first), today)
                complete = last == month_end(first) and last < today
                for src in api_sources:
                    key = f"api-{src}-{aoi.id}-{first:%Y%m}" + ("" if complete else f"-through-{last:%Y%m%d}")
                    refs.append(self.ref(key, datetime(first.year, first.month, 1, tzinfo=UTC),
                                         [self.api_url(src, self.aoi_bbox(aoi), 5, first)], kind="api",
                                         product=src, first_day=first.isoformat(), last_day=last.isoformat()))
        return refs

    def missing_credentials(self, aoi: AOI, ref: SourceRef | None = None) -> list[str]:
        """Discovery and the yearly archive are keyless; only area-API units need ``FIRMS_MAP_KEY``."""
        if ref is None or ref.meta.get("kind") != "api" or self.settings.has_secret(KEY_ENV):
            return []
        return [KEY_ENV]

    # ------------------------------------------------------------------ extract
    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        if ref.meta["kind"] == "archive":
            return self._extract_archive(ref, aoi)
        return self._extract_api(ref, aoi)

    def archive_cache_path(self, ref: SourceRef):
        """Local cache file (outside the lake) of the nationwide archive CSV of ``ref``."""
        name = ref.urls[0].rsplit("/", 1)[-1].rsplit(".", 1)[0]
        ver = ref.meta.get("version") or ref.key.rsplit("-v", 1)[-1]
        return self.settings.cache_path("firms", str(ref.meta["product"]), f"{name}__v{ver}.csv")

    def cached_archive(self, ref: SourceRef):
        """Download the archive CSV of ``ref`` to the cache unless that version is there; delete older versions."""
        path = self.archive_cache_path(ref)
        if path.is_file() and path.stat().st_size > 0:
            return path
        url = ref.urls[0]
        tmp = path.with_suffix(".part")
        try:
            with self.http_session().get(url, stream=True, timeout=max(self.settings.http_timeout, 300)) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for ln in r.iter_lines():
                        if ln:
                            f.write(ln + b"\n")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        stem = path.name.split("__v", 1)[0]
        for old in path.parent.glob(f"{stem}__v*.csv"):
            if old != path:
                old.unlink(missing_ok=True)
        return path

    def _extract_archive(self, ref: SourceRef, aoi: AOI) -> list[str]:
        url = ref.urls[0]
        path = self.cached_archive(ref)
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            text, n_in, n_total = stream_filter_csv(f, self.aoi_bbox(aoi), self._region)
        if n_total == 0:
            raise RuntimeError(f"FIRMS archive {url}: empty file")
        log.info("firms %s: %d of %d detections inside the AOI and region", ref.key, n_in, n_total)
        rel = self.raw_path(ref, f"country-{ref.meta['product']}-{aoi.id}-{ref.meta['year']}.csv")
        self.lake.write_bytes(rel, text.encode())
        return [rel]

    def _extract_api(self, ref: SourceRef, aoi: AOI) -> list[str]:
        key = self.settings.secret(KEY_ENV)
        if not key:
            raise RuntimeError(f"{KEY_ENV} not set: FIRMS area API units ({ref.meta['product']}, "
                               f"{ref.meta['first_day'][:7]}) need a free MAP_KEY from "
                               "https://firms.modaps.eosdis.nasa.gov/api/map_key/ (keyless archive covers only "
                               "complete past years)")
        s = self.http_session()
        bbox = self.aoi_bbox(aoi)
        day, last = date.fromisoformat(ref.meta["first_day"]), date.fromisoformat(ref.meta["last_day"])
        header, body = None, []
        while day <= last:
            n_days = min(int(self.opts.get("api_days_per_request", 5)), (last - day).days + 1)
            r = s.get(self.api_url(ref.meta["product"], bbox, n_days, day, key),
                      timeout=max(self.settings.http_timeout, 120))
            text = r.text
            if r.status_code != 200 or not text.lower().startswith("latitude"):
                from fuellake.config import redact

                raise RuntimeError(redact(f"FIRMS API {ref.meta['product']} {day}: HTTP {r.status_code}: "
                                          f"{text[:200].strip()}"))
            lines = text.splitlines()
            header = header or lines[0]
            body.extend(ln for ln in lines[1:] if ln.strip())
            day += timedelta(days=n_days)
        rel = self.raw_path(ref, f"api-{ref.meta['product']}-{aoi.id}-{ref.datetime:%Y%m}.csv")
        text, _, _ = stream_filter_csv([header or ""] + body, bbox, self._region) if header else ("", 0, 0)
        self.lake.write_bytes(rel, (text or "\n").encode())
        return [rel]

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        kind, product = ref.meta["kind"], str(ref.meta["product"])
        bbox = self.aoi_bbox(aoi)
        text = self.lake.read_bytes(raw[0]).decode("utf-8")
        gdf = detections_frame(text, product, "archive-country" if kind == "archive" else "api-area", bbox)
        if kind == "archive":
            year = int(ref.meta["year"])
            start = datetime(year, 1, 1, tzinfo=UTC)
            end = datetime(year + 1, 1, 1, tzinfo=UTC)
            part = f"country-{product}-{aoi.id}-{year}"
            disc = product
        else:
            first = date.fromisoformat(ref.meta["first_day"])
            last = date.fromisoformat(ref.meta["last_day"])
            start = datetime(first.year, first.month, 1, tzinfo=UTC)
            end = datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
            part = f"api-{product}-{aoi.id}-{first:%Y%m}"
            disc = product.lower()
        path = self.table_path(part)
        write_geoparquet(self.lake, path, gdf)
        props: dict[str, Any] = {
            "firms:access": "archive-country" if kind == "archive" else "api-area",
            "firms:product": product,
            "firms:query_bbox": list(bbox),
            "firms:n_high_confidence": int((gdf["confidence_class"] == "high").sum()) if len(gdf) else 0,
            "firms:first_detection": gdf["acq_datetime"].min().isoformat() if len(gdf) else None,
            "firms:last_detection": gdf["acq_datetime"].max().isoformat() if len(gdf) else None,
        }
        if kind == "archive":
            props["firms:archive_last_modified"] = ref.meta.get("last_modified")
        return [StagedItem(item_id=self.item_id(aoi, ref, disc), datetime=start, properties=props,
                           assets=[self.table_asset(path)], bbox=bbox, start_datetime=start,
                           end_datetime=end - timedelta(microseconds=1))]
