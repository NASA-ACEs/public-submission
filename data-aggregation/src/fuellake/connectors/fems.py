# fuel-lake — connector for USDA FEMS live fuel moisture field samples (lfmc_samples table)
# Licensed under Apache-2.0.
"""USDA FEMS Field Sample module -> ``lfmc_samples`` (source ``fems``). No login needed.

Endpoints (tested 2026-11-14 without credentials; see ``config/datasets/fems-lfmc.yaml``)
---------------------------------------------------------------------------------------
- Site list: GraphQL POST ``getSites(returnAll: true)`` (1,661 sites with coordinates).
- Site metadata CSV ``siteMetadataDownload/download?siteIds=...``: the coordinates and state stored in
  the table come from here (GraphQL coordinates are the fallback).
- Samples CSV ``sample/download?...`` (the web app's "Copy Data Link"). ``filterByCategory``,
  ``filterBySubCategory`` and ``filterByMethod`` must be ``All`` and ``sortBy`` must be set, otherwise the
  server answers HTTP 200 with a header-only CSV. Do **not** use GraphQL ``getFuelSamples`` (zero rows
  without login, "Java heap space" with ``returnAll``).

Site selection
--------------
All sites (active or not) whose distance to the AOI bbox is at most ``site_buffer_km`` (default 25 km;
0 = inside the bbox) **and** that lie inside the lake region (the California polygon, within its
``tolerance_m``). The distance uses an equirectangular approximation, which is fine at this scale.
The Santa Monica Mountains bbox holds only 6 sites (5 active), so the default buffer adds the
surrounding LA / Ventura County sites; rows outside the AOI bbox cannot be matched against AOI-grid
rasters, so consumers filter by ``lon``/``lat`` when they need that. On the region AOI ``ca`` the bbox is the
state's envelope, so the selection is every California site (nothing in Nevada, Oregon, Arizona or Mexico).
A site whose longitude is positive (0 < lon < 170) with latitude > 24 is a sign error in FEMS (e.g. site
4937 "Fisher Springs", California, +118.70): it is negated **before** the buffer test and its rows get
``lon_sign_corrected``. The pipeline drops any row whose site-metadata coordinates fall outside the region.

Units of work and refresh of the open month
-------------------------------------------
One unit per calendar month that overlaps the run period (and is not in the future). A unit always
covers the **whole** month, whatever ``--start`` / ``--end`` say, so a clipped run cannot leave a
partial month that later runs would skip. Item id ``fems-lfmc__<aoi>__<yyyymm01>``, table part
``staged/tables/lfmc_samples/fems-lfmc/part-<aoi>__<yyyy-mm>.parquet``; both are stable per month.

The source key is ``fems:<yyyy-mm>:sites-<hash>`` where ``<hash>`` covers the selected site IDs (so
changing ``site_buffer_km`` or a new FEMS site reprocesses the months). FEMS samples are often entered
late (sequential sample IDs vs sampling dates in June-November 2026: median about 2 days, 90th
percentile 27 days, 95th 42 days), so a month counts as **open** until ``settle_days`` (default 45) days
after its last day (UTC wall clock, not the run period). While open, the key gets ``:asof-<yyyymmdd>``
(UTC today): a rerun on the same day is a no-op, the first run on a later day re-downloads that month and
replaces the same item and part. Once the month is settled the key loses the suffix, the month is
fetched one last time and is skipped from then on. Samples entered after that need ``--force``.

Rows are assigned to months by the **UTC calendar date** of the FEMS ``Date-Time`` string (date-only
entries arrive as local or UTC midnight, e.g. ``T05:00Z`` / ``T00:00Z``, whose UTC date is the
sampling date). Requests ask for the month padded by ``query_pad_days`` on both sides and the rows are
filtered client-side, so no sample can land in two parts and none is lost at a boundary.

Transform
---------
- Category ``Dead`` (dead fuel moisture: 1-h ... 1000-h, duff) is not live fuel and is excluded
  (``exclude_categories``); the count is kept in ``fems:n_rows_excluded_category``.
- Rows without a numeric, non-negative value or without site coordinates are dropped and counted.
- Species: the fuel type without its growth-phase suffix, in common-name order, lower case
  (``"Sage, Black"`` -> ``"black sage"``, ``"Chamise, New Growth"`` -> ``"chamise"``).
- Component: growth phase from the ``", New Growth"`` / ``", Old Growth"`` suffix and the Sub-Category
  (``New``, ``Old``, ``Lower``, ``Middle``, ``Upper``, ``Disturbance``), lower case; blank = whole species.
- Collapse to ``(source, site_id, date, species)`` with the rule in :mod:`._lfmc_common`. In short:
  the plain species row wins (FEMS derives it as the mean of the growth-phase rows), otherwise the mean of
  the component means; ``n_raw_rows`` keeps every collapsed raw row.
- ``fuel_category``: Shrub -> shrub, Tree -> tree, Grass -> grass, Forb -> herb, other non-empty ->
  other, blank -> unknown. ``status`` is the FEMS Sample Status (``Submitted``; requested via
  ``status_filter``). ``outlier_flag`` is always False (FEMS has no outlier screening).
- Row-level QC codes: ``method_missing`` (Method blank, the common case), ``method_non_gravimetric``
  (e.g. ``Moisture Probe``), ``lon_sign_corrected``; plus the collapse codes (``value_out_of_range`` with
  ``plausible_range`` default [30, 400] %, ``components_averaged``, ``single_component``,
  ``replicates_averaged``, ``component_mean_mismatch``).

A month without samples still registers an item with a zero-row part, so it is not re-queried.
"""

from __future__ import annotations

import io
import logging
import math
import re
import time as _time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from fuellake.connectors._lfmc_common import (
    DEFAULT_PLAUSIBLE_RANGE,
    RawSample,
    bbox_geometry,
    collapse_samples,
    month_end,
    months_between,
    points_bbox,
    qc_summary,
    short_hash,
    utc_end_of_day,
    utc_midnight,
)
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.registry import AOI, lake_region
from fuellake.tables import write_geoparquet

log = logging.getLogger("fuellake.connectors.fems")

SOURCE = "fems"
DEFAULTS: dict[str, Any] = {
    "sites_graphql": "https://fems.fs2c.usda.gov/fuelmodel/apis/graphql",
    "samples_csv": "https://fems.fs2c.usda.gov/fuelmodel/sample/download",
    "site_metadata_csv": "https://fems.fs2c.usda.gov/fuelmodel/siteMetadataDownload/download",
    "site_buffer_km": 25.0,
    "settle_days": 45,
    "query_pad_days": 2,
    "max_sites_per_request": 140,
    "status_filter": "Submitted",
    "exclude_categories": ["Dead"],
    "plausible_range": list(DEFAULT_PLAUSIBLE_RANGE),
    "request_pause_s": 0.5,
    "retries": 3,
}
SITES_QUERY = ("query($returnAll:Boolean!){getSites(returnAll:$returnAll){sites{siteId siteName active "
               "longitude latitude elevation areaName areaId groupName siteStatus}}}")

# Samples CSV header (Date-Time is matched by prefix: its header carries an example timestamp).
SAMPLE_COLUMNS = {
    "Sample Id": "sample_id", "Site Name": "site_name", "SiteId": "site_id", "Fuel Type": "fuel_type",
    "Category": "category", "Sub-Category": "sub_category", "Method": "method",
    "Sample Avg Value": "value", "Sample Status": "status",
}
CATEGORY_MAP = {"shrub": "shrub", "tree": "tree", "grass": "grass", "forb": "herb", "herb": "herb",
                "herbaceous": "herb"}
PHASE_SUFFIX = re.compile(r",\s*(new|old)\s+growth\s*$", re.IGNORECASE)


# ---------------------------------------------------------------------------- pure helpers
def parse_fuel_type(fuel_type: str | None, sub_category: str | None = None) -> tuple[str, str]:
    """``(species, component)`` from a FEMS fuel type and Sub-Category (see module docstring)."""
    name = " ".join(str(fuel_type or "").split())
    parts: list[str] = []
    m = PHASE_SUFFIX.search(name)
    if m:
        parts.append(m.group(1).lower())
        name = name[: m.start()]
    sub = " ".join(str(sub_category or "").split()).lower()
    if sub:
        parts.append(sub)
    words = [p.strip() for p in name.split(",") if p.strip()]
    species = " ".join(reversed(words)).lower() if words else "unknown"
    return species, "+".join(dict.fromkeys(parts))


def fuel_category(category: str | None) -> str:
    c = " ".join(str(category or "").split()).lower()
    if not c:
        return "unknown"
    return CATEGORY_MAP.get(c, "other")


def fix_longitude(lon: float, lat: float) -> tuple[float, bool]:
    """Negate a positive longitude that is a sign error for a US site (0 < lon < 170, lat > 24)."""
    if 0 < lon < 170 and lat > 24:
        return -lon, True
    return lon, False


def distance_to_bbox_km(lon: float, lat: float, bbox: tuple[float, float, float, float]) -> float:
    """Approximate distance (km) from a point to a lon/lat bbox; 0 inside."""
    w, s, e, n = bbox
    dx = max(w - lon, 0.0, lon - e) * 111.32 * math.cos(math.radians(lat))
    dy = max(s - lat, 0.0, lat - n) * 110.57
    return math.hypot(dx, dy)


def _num(value: Any) -> float | None:
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_site_list(payload: dict) -> list[dict]:
    """Sites from the GraphQL ``getSites`` response, coordinates sign-checked."""
    if payload.get("errors"):
        raise RuntimeError(f"FEMS getSites returned errors: {payload['errors']}")
    sites = (((payload.get("data") or {}).get("getSites") or {}).get("sites")) or []
    out = []
    for s in sites:
        lon, lat = _num(s.get("longitude")), _num(s.get("latitude"))
        if lon is None or lat is None or s.get("siteId") is None:
            continue
        lon, fixed = fix_longitude(lon, lat)
        out.append({"site_id": str(s["siteId"]), "site_name": s.get("siteName"), "lon": lon, "lat": lat,
                    "lon_sign_corrected": fixed, "status": s.get("siteStatus")})
    if not out:
        raise RuntimeError("FEMS getSites returned no sites with coordinates")
    return out


def select_sites(sites: list[dict], bbox: tuple[float, float, float, float], buffer_km: float,
                 region=None) -> list[dict]:
    """Sites within ``buffer_km`` of ``bbox`` (and inside ``region``, a :class:`fuellake.region.Region`, if
    given), sorted by numeric site id."""
    picked = []
    for s in sites:
        d = distance_to_bbox_km(s["lon"], s["lat"], bbox)
        if d <= buffer_km:
            picked.append({**s, "distance_km": round(d, 2)})
    if region is not None and picked:
        inside = region.contains_points([s["lon"] for s in picked], [s["lat"] for s in picked])
        picked = [s for s, ok in zip(picked, inside, strict=True) if ok]
    return sorted(picked, key=lambda s: (int(s["site_id"]) if s["site_id"].isdigit() else 0, s["site_id"]))


def parse_site_metadata(text: str) -> dict[str, dict]:
    """``{site_id: {site_name, lon, lat, state, lon_sign_corrected}}`` from the site-metadata CSV."""
    df = pd.read_csv(io.StringIO(text.lstrip("\ufeff")), dtype=str, keep_default_na=False)
    need = {"Site ID", "Latitude", "Longitude"}
    if not need <= set(df.columns):
        raise ValueError(f"unexpected FEMS site metadata header: {list(df.columns)}")
    out = {}
    for row in df.to_dict("records"):
        lon, lat = _num(row.get("Longitude")), _num(row.get("Latitude"))
        if lon is None or lat is None:
            continue
        lon, fixed = fix_longitude(lon, lat)
        out[str(row["Site ID"]).strip()] = {"site_name": row.get("Site Name") or None, "lon": lon, "lat": lat,
                                            "state": row.get("State") or None, "lon_sign_corrected": fixed}
    return out


def read_samples_csv(text: str) -> pd.DataFrame:
    """FEMS samples CSV -> DataFrame with normalised column names (all strings, blanks = "")."""
    df = pd.read_csv(io.StringIO(text.lstrip("\ufeff")), dtype=str, keep_default_na=False)
    rename = {}
    for col in df.columns:
        if col in SAMPLE_COLUMNS:
            rename[col] = SAMPLE_COLUMNS[col]
        elif col.startswith("Date-Time"):
            rename[col] = "date_time"
    df = df.rename(columns=rename)
    missing = (set(SAMPLE_COLUMNS.values()) | {"date_time"}) - set(df.columns)
    if missing:
        raise ValueError(f"FEMS samples CSV lacks columns {sorted(missing)} (header: {list(df.columns)})")
    return df.drop_duplicates(subset=["sample_id"]).reset_index(drop=True)


def samples_to_raw(df: pd.DataFrame, month: date, coords: dict[str, dict], exclude_categories: list[str]
                   ) -> tuple[list[RawSample], dict[str, int]]:
    """Map FEMS rows of one calendar month (UTC date) to :class:`RawSample` records, with drop counts."""
    excluded = {c.strip().lower() for c in exclude_categories}
    counts = {"n_rows_in_month": 0, "n_rows_excluded_category": 0, "n_rows_invalid_value": 0,
              "n_rows_no_coordinates": 0}
    first, last = month.replace(day=1), month_end(month)
    out: list[RawSample] = []
    for row in df.to_dict("records"):
        try:
            day = date.fromisoformat(str(row["date_time"]).strip()[:10])
        except ValueError:
            counts["n_rows_invalid_value"] += 1
            continue
        if not first <= day <= last:
            continue
        counts["n_rows_in_month"] += 1
        if str(row["category"]).strip().lower() in excluded:
            counts["n_rows_excluded_category"] += 1
            continue
        value = _num(row["value"])
        if value is None or value < 0:
            counts["n_rows_invalid_value"] += 1
            continue
        site_id = str(row["site_id"]).strip()
        site = coords.get(site_id)
        if site is None:
            counts["n_rows_no_coordinates"] += 1
            continue
        species, component = parse_fuel_type(row["fuel_type"], row["sub_category"])
        method = str(row["method"]).strip()
        flags = set()
        if not method:
            flags.add("method_missing")
        elif method.lower() != "gravimetric":
            flags.add("method_non_gravimetric")
        if site.get("lon_sign_corrected"):
            flags.add("lon_sign_corrected")
        out.append(RawSample(
            site_id=site_id, site_name=(str(row["site_name"]).strip() or site.get("site_name")),
            lon=site["lon"], lat=site["lat"], date=day, species=species, value=value, component=component,
            fuel_category=fuel_category(row["category"]), status=str(row["status"]).strip() or None,
            outlier=False, flags=frozenset(flags)))
    return out, counts


# ---------------------------------------------------------------------------- connector
class FemsConnector(BaseConnector):
    """Monthly units of FEMS field samples for the sites near an AOI (see module docstring)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session = None
        self._sites: list[dict] | None = None
        self._metadata_cache: dict[tuple[str, ...], list[str]] = {}

    # ------------------------------------------------------------------ options and clock
    def opt(self, name: str) -> Any:
        return self.dataset.options.get(name, DEFAULTS[name])

    def today(self) -> date:
        """UTC date that decides which months are still open (tests override this)."""
        return datetime.now(UTC).date()

    # ------------------------------------------------------------------ HTTP
    @property
    def session(self):
        if self._session is None:
            self._session = self.http_session()
        return self._session

    def _request(self, method: str, url: str, **kwargs):
        import requests

        retries = max(1, int(self.opt("retries")))
        for attempt in range(retries):
            try:
                resp = self.session.request(method, url, timeout=self.settings.http_timeout, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code} from {url}", response=resp)
                resp.raise_for_status()
                return resp
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status is None or status == 429 or status >= 500
                if not retryable or attempt + 1 == retries:
                    raise
                log.warning("FEMS request failed (%s), retry %d/%d", exc, attempt + 1, retries - 1)
                _time.sleep(2.0 * (attempt + 1))
        raise AssertionError("unreachable")

    def _pause(self) -> None:
        pause = float(self.opt("request_pause_s"))
        if pause > 0:
            _time.sleep(pause)

    def fetch_sites(self) -> list[dict]:
        """All FEMS sites (GraphQL ``getSites``), cached per connector instance."""
        if self._sites is None:
            resp = self._request("POST", self.opt("sites_graphql"),
                                 json={"query": SITES_QUERY, "variables": {"returnAll": True}})
            self._sites = parse_site_list(resp.json())
        return self._sites

    def fetch_site_metadata(self, site_ids: list[str]) -> list[str]:
        """Site-metadata CSV responses for ``site_ids`` (one per chunk), cached per instance."""
        key = tuple(site_ids)
        if key not in self._metadata_cache:
            texts = []
            for chunk in _chunks(site_ids, int(self.opt("max_sites_per_request"))):
                resp = self._request("GET", self.opt("site_metadata_csv"), params={"siteIds": ",".join(chunk)})
                _check_csv(resp, "Site ID")
                texts.append(resp.text)
                self._pause()
            self._metadata_cache[key] = texts
        return self._metadata_cache[key]

    def samples_params(self, site_ids: list[str], start: date, end: date) -> dict[str, str]:
        """Query of the tested CSV link; the three ``filterBy...`` values must stay ``All``."""
        return {
            "returnAll": "false", "responseFormat": "csv", "siteId": ",".join(site_ids), "sampleId": "",
            "startDate": f"{start:%Y-%m-%d}T00:00:00.000Z", "endDate": f"{end:%Y-%m-%d}T23:59:59.999Z",
            "filterByFuelId": "", "filterByStatus": str(self.opt("status_filter")),
            "filterByCategory": "All", "filterBySubCategory": "All", "filterByMethod": "All",
            "sortBy": "sample", "sortOrder": "desc",
        }

    def samples_urls(self, site_ids: list[str], month: date) -> list[str]:
        import requests

        pad = timedelta(days=int(self.opt("query_pad_days")))
        start, end = month.replace(day=1) - pad, month_end(month) + pad
        return [requests.Request("GET", self.opt("samples_csv"), params=self.samples_params(chunk, start, end))
                .prepare().url for chunk in _chunks(site_ids, int(self.opt("max_sites_per_request")))]

    def fetch_samples(self, urls: list[str]) -> list[str]:
        """Samples CSV responses, one per URL (site-ID chunk), exactly as fetched."""
        texts = []
        for url in urls:
            resp = self._request("GET", url)
            _check_csv(resp, "Sample Id")
            texts.append(resp.text)
            self._pause()
        return texts

    # ------------------------------------------------------------------ connector interface
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        today = self.today()
        end = min(period.end, today)
        if end < period.start:
            return []
        buffer_km = float(self.opt("site_buffer_km"))
        sites = select_sites(self.fetch_sites(), tuple(aoi.bbox), buffer_km, lake_region())
        site_ids = [s["site_id"] for s in sites]
        sites_hash = short_hash({"site_ids": site_ids})
        settle = timedelta(days=int(self.opt("settle_days")))
        refs = []
        for month in months_between(period.start, end):
            closed = today > month_end(month) + settle
            key = f"{SOURCE}:{month:%Y-%m}:sites-{sites_hash}" + ("" if closed else f":asof-{today:%Y%m%d}")
            urls = self.samples_urls(site_ids, month) if site_ids else []
            refs.append(self.ref(key, utc_midnight(month), urls=urls, month=month.isoformat(), closed=closed,
                                 as_of=today.isoformat(), site_buffer_km=buffer_km, site_ids=site_ids,
                                 sites=sites))
        return refs

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        """Write ``raw/fems-lfmc/<yyyy>/<mm>/<aoi>__sites-NNN.csv`` and ``<aoi>__samples-NNN.csv``
        (one file per request, as fetched; an open month overwrites its previous download)."""
        site_ids = list(ref.meta.get("site_ids") or [])
        if not site_ids:
            return []
        written = []
        for what, texts in (("sites", self.fetch_site_metadata(site_ids)),
                            ("samples", self.fetch_samples(list(ref.urls)))):
            for i, text in enumerate(texts):
                rel = self.raw_path(ref, f"{aoi.id}__{what}-{i:03d}.csv")
                self.lake.write_text(rel, text)
                written.append(rel)
        return written

    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        month = date.fromisoformat(ref.meta["month"])
        coords = {s["site_id"]: s for s in ref.meta.get("sites") or []}
        frames = []
        for rel in raw:
            name = rel.rsplit("/", 1)[-1]
            if "__sites-" in name:
                coords.update(parse_site_metadata(self.lake.read_text(rel)))   # metadata wins over GraphQL
            elif "__samples-" in name:
                frames.append(read_samples_csv(self.lake.read_text(rel)))
        samples = (pd.concat(frames, ignore_index=True).drop_duplicates(subset=["sample_id"])
                   if frames else pd.DataFrame())
        rows, counts = [], {"n_rows_in_month": 0, "n_rows_excluded_category": 0, "n_rows_invalid_value": 0,
                            "n_rows_no_coordinates": 0}
        if not samples.empty:
            rows, counts = samples_to_raw(samples, month, coords, list(self.opt("exclude_categories")))
        df = collapse_samples(rows, SOURCE, tuple(self.opt("plausible_range")))
        part = self.table_path(f"{aoi.id}__{month:%Y-%m}")
        write_geoparquet(self.lake, part, df)

        sites = ref.meta.get("sites") or []
        bbox = points_bbox(df) or (points_bbox(pd.DataFrame(sites)) if sites else None) or tuple(aoi.bbox)
        states = sorted({str(c.get("state")) for sid, c in coords.items()
                         if c.get("state") and sid in set(ref.meta.get("site_ids") or [])})
        props = {
            "fems:month": f"{month:%Y-%m}",
            "fems:site_states": states,            # State column of the FEMS site metadata (a cross-check)
            "fems:month_closed": bool(ref.meta.get("closed")),
            "fems:as_of": ref.meta.get("as_of"),
            "fems:site_buffer_km": ref.meta.get("site_buffer_km"),
            "fems:n_sites_queried": len(ref.meta.get("site_ids") or []),
            "fems:n_sites_with_samples": int(df["site_id"].nunique()),
            "fems:n_raw_rows_collapsed": int(df["n_raw_rows"].sum()) if len(df) else 0,
            **{f"fems:{k}": v for k, v in counts.items()},
            "fems:qc_flag_counts": qc_summary(df),
        }
        if not ref.urls:
            props["fuellake:source_ids"] = [self.opt("samples_csv")]
        return [StagedItem(item_id=self.item_id(aoi, ref), datetime=ref.datetime, properties=props,
                           assets=[self.table_asset(part)], bbox=bbox, geometry=bbox_geometry(bbox),
                           start_datetime=utc_midnight(month), end_datetime=utc_end_of_day(month_end(month)))]


# ---------------------------------------------------------------------------- small utilities
def _chunks(items: list[str], size: int) -> list[list[str]]:
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]


def _check_csv(resp, first_column: str) -> None:
    """Refuse responses that are not the expected CSV (e.g. an HTML error page with HTTP 200)."""
    ctype = resp.headers.get("Content-Type", "")
    if "csv" not in ctype and "text/plain" not in ctype:
        raise RuntimeError(f"FEMS download returned {ctype!r}, expected text/csv")
    head = resp.text.lstrip("﻿").split("\n", 1)[0]
    if first_column not in head:
        raise RuntimeError(f"unexpected FEMS CSV header (wanted {first_column!r}): {head[:120]!r}")
