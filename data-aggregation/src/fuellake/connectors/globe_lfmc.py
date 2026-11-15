# fuel-lake — connector for the Globe-LFMC 2.0 field database (lfmc_samples table)
# Licensed under Apache-2.0.
"""Globe-LFMC 2.0 (Yebra et al. 2024, figshare doi:10.6084/m9.figshare.25413790.v1, CC0) ->
``lfmc_samples`` (source ``globe-lfmc``).

Download, cache and raw subset
------------------------------
One static, global file, ``Globe-LFMC-2.0 final.xlsx`` (72,494,093 bytes, md5 ``57a2c99f...``, verified
against the figshare API record on 2026-11-14). It covers far more than the monitored region, so it never
enters the lake: it is downloaded once to the local cache ``$FUELLAKE_CACHE_DIR/globe-lfmc/`` (default
``~/.cache/fuel-lake``) and reused while its md5 matches ``md5`` (or, if that option is null, the
``computed_md5`` of the figshare API record). A copy left in the lake by an older version
(``raw/globe-lfmc/2024/03/Globe-LFMC-2.0_final.xlsx``) is moved to the cache and deleted from the lake.

The sheet ``LFMC data`` is parsed once (about 25 s with openpyxl) and only its rows inside the scope are
written to ``raw/globe-lfmc/2024/03/Globe-LFMC-2.0_final__<md5[:8]>__<scope>.csv`` (all years, original
column values as text). Later units and runs read that subset.

Spatial scope: the lake region (not the AOI)
--------------------------------------------
Training needs far more sites than a demo AOI holds, so rows are selected by the **lake region** (the
California polygon of ``config/lake.yaml``; points within its ``tolerance_m``), whatever the AOI. That is why
the dataset declares ``scales: [region]`` and runs on ``ca``. The items are cataloged under the run's AOI
(``fuellake:aoi``). The region fingerprint is part of the source key and of the subset name, so a new region
reprocesses the years. Only a lake without a region falls back to the option ``region_bbox`` (WGS84
``[west, south, east, north]``, default ``[-125, 31, -102, 49]`` = western US).

Units of work
-------------
One unit per calendar year that overlaps the run period and the file's years (``first_year`` 1977 ..
``last_year`` 2023). A unit always covers the whole year. Source key
``globe-lfmc:<figshare file id>:<yyyy>:region-<hash>``; item id ``globe-lfmc__<aoi>__<yyyy>0101``; part
``staged/tables/lfmc_samples/globe-lfmc/part-<aoi>__<yyyy>.parquet``. Running a second AOI writes a second,
identical set of parts: deduplicate on ``sample_id`` when reading several AOIs.

Mapping and collapse
--------------------
- ``site_id`` = ``site_name`` = ``Site name``. The file has no site ID column; in v2.0 every site name
  maps to exactly one coordinate pair (checked over all 293,796 rows).
- ``date`` = ``Sampling date``; ``species`` = ``Species collected`` in lower case (scientific names, so
  they do not match the FEMS common names); component = ``Old or new leaves`` (``New`` / ``Old``; ``NA`` and
  ``Mixed`` count as whole-species rows).
- ``fuel_category`` from ``Species functional type``: Tree / Small tree -> tree; Shrub / Large shrub /
  Subshrub / Shrub/Small tree -> shrub; Grass / Graminoid / Sedge -> grass; Forb / Geophyte -> herb; other
  non-empty -> other; blank -> unknown.
- ``outlier_flag`` = ``Isolated data point`` (the strings ``TRUE`` / ``FALSE``) of any collapsed raw row.
  Flagged rows are **never dropped**; ``outlier_partial`` marks keys where only some rows were flagged.
  ``source_outlier_note`` marks rows whose ``Extra information/Quality Flag`` mentions an outlier.
- Collapse rule and the other QC codes: :mod:`._lfmc_common` (replicates are averaged, the
  ``value_out_of_range`` check also looks at each raw value). ``status`` is null (the source has none).
- Rows without a numeric, non-negative LFMC value are dropped and counted (``globe:n_rows_invalid_value``).
"""

from __future__ import annotations

import hashlib
import logging
import math
from datetime import date, datetime
from typing import Any

import pandas as pd

from fuellake.connectors._lfmc_common import (
    DEFAULT_PLAUSIBLE_RANGE,
    RawSample,
    bbox_geometry,
    collapse_samples,
    points_bbox,
    qc_summary,
    short_hash,
    utc_end_of_day,
    utc_midnight,
)
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.registry import AOI, lake_region
from fuellake.tables import write_geoparquet

log = logging.getLogger("fuellake.connectors.globe_lfmc")

SOURCE = "globe-lfmc"
DEFAULTS: dict[str, Any] = {
    "download_url": "https://ndownloader.figshare.com/files/45049786",
    "api_record": "https://api.figshare.com/v2/articles/25413790",
    "doi_url": "https://doi.org/10.6084/m9.figshare.25413790.v1",
    "file_id": 45049786,
    "file_name": "Globe-LFMC-2.0 final.xlsx",
    "md5": "57a2c99f82db84810cdf6acc13aef0e4",
    "published": "2024-03-25",
    "sheet": "LFMC data",
    "region_bbox": [-125.0, 31.0, -102.0, 49.0],      # only used when the lake has no region
    "first_year": 1977,
    "last_year": 2023,
    "plausible_range": list(DEFAULT_PLAUSIBLE_RANGE),
}
# Column header prefixes (lower case) of the "LFMC data" sheet -> internal names.
COLUMNS = {
    "site": "site name", "country": "country", "lat": "latitude", "lon": "longitude", "date": "sampling date",
    "value": "lfmc value", "species": "species collected", "ftype": "species functional type",
    "leaves": "old or new leaves", "extra": "extra information", "isolated": "isolated data point",
}
FUNCTIONAL_TYPE_MAP = {
    "tree": "tree", "small tree": "tree",
    "shrub": "shrub", "large shrub": "shrub", "subshrub": "shrub", "shrub/small tree": "shrub",
    "grass": "grass", "graminoid": "grass", "sedge": "grass",
    "forb": "herb", "geophyte": "herb",
}


# ---------------------------------------------------------------------------- pure helpers
def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return " ".join(str(value).split())


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_sampling_date(value: Any) -> date | None:
    """Sampling date from a datetime cell, an integer ``YYYYMMDD`` or an ISO string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and _num(value) is not None:
        value = str(int(value))
    s = _text(value)
    try:
        if len(s) == 8 and s.isdigit():
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def functional_type_category(value: Any) -> str:
    t = _text(value).lower()
    if not t:
        return "unknown"
    return FUNCTIONAL_TYPE_MAP.get(t, "other")


def is_isolated(value: Any) -> bool:
    """``Isolated data point`` holds the strings ``TRUE`` / ``FALSE`` (``bool('FALSE')`` would be True)."""
    if isinstance(value, bool):
        return value
    return _text(value).upper() in ("TRUE", "1", "YES")


def leaves_component(value: Any) -> str:
    t = _text(value).lower()
    return t if t in ("new", "old") else ""


def in_bbox(lon: float, lat: float, bbox: tuple[float, float, float, float]) -> bool:
    w, s, e, n = bbox
    return w <= lon <= e and s <= lat <= n


def read_lfmc_sheet(path: str, sheet: str, region_bbox: tuple[float, float, float, float]) -> pd.DataFrame:
    """Rows of the ``LFMC data`` sheet inside ``region_bbox`` (all years), internal column names (a cheap
    prefilter while streaming the workbook; the connector then applies the region polygon)."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        names = {n.lower(): n for n in wb.sheetnames}
        if sheet.lower() not in names:
            raise KeyError(f"sheet {sheet!r} not found in {wb.sheetnames}")
        rows = wb[names[sheet.lower()]].iter_rows(values_only=True)
        header = [_text(h).lower() for h in next(rows)]
        idx = {}
        for key, prefix in COLUMNS.items():
            hits = [i for i, h in enumerate(header) if h.startswith(prefix)]
            if not hits:
                raise KeyError(f"column starting with {prefix!r} not found in sheet {sheet!r}")
            idx[key] = hits[0]
        width = max(idx.values()) + 1
        keep = []
        for row in rows:
            if not row or len(row) < width:
                row = tuple(row or ()) + (None,) * (width - len(row or ()))
            lon, lat = _num(row[idx["lon"]]), _num(row[idx["lat"]])
            if lon is None or lat is None or not in_bbox(lon, lat, region_bbox):
                continue
            rec = {k: row[i] for k, i in idx.items()}
            rec["lon"], rec["lat"] = lon, lat
            keep.append(rec)
    finally:
        wb.close()
    return pd.DataFrame(keep, columns=list(COLUMNS))


def sheet_to_raw(df: pd.DataFrame, year: int) -> tuple[list[RawSample], dict[str, int]]:
    """Map sheet rows of one calendar year to :class:`RawSample` records, with drop counts."""
    counts = {"n_raw_rows_in_scope": 0, "n_rows_invalid_value": 0, "n_rows_invalid_date": 0,
              "n_isolated_raw_rows": 0}
    out: list[RawSample] = []
    for rec in df.to_dict("records"):
        day = parse_sampling_date(rec["date"])
        if day is None:
            counts["n_rows_invalid_date"] += 1
            continue
        if day.year != year:
            continue
        counts["n_raw_rows_in_scope"] += 1
        value = _num(rec["value"])
        site = _text(rec["site"])
        if value is None or value < 0 or not site:
            counts["n_rows_invalid_value"] += 1
            continue
        outlier = is_isolated(rec["isolated"])
        counts["n_isolated_raw_rows"] += int(outlier)
        flags = frozenset({"source_outlier_note"}) if "outlier" in _text(rec["extra"]).lower() else frozenset()
        out.append(RawSample(
            site_id=site, site_name=site, lon=float(rec["lon"]), lat=float(rec["lat"]), date=day,
            species=_text(rec["species"]).lower() or "unknown", value=value,
            component=leaves_component(rec["leaves"]), fuel_category=functional_type_category(rec["ftype"]),
            status=None, outlier=outlier, flags=flags))
    return out, counts


def file_md5(fobj, chunk: int = 1 << 20) -> str:
    h = hashlib.md5(usedforsecurity=False)
    for block in iter(lambda: fobj.read(chunk), b""):
        h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------- connector
def write_subset_csv(df: pd.DataFrame) -> str:
    """Sheet rows (internal column names) as CSV text; every value as text, blanks for missing cells."""
    out = df.copy()
    for col in out.columns:
        out[col] = [_cell_text(v) for v in out[col]]
    return out.to_csv(index=False, lineterminator="\n")


def _cell_text(v: Any) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def read_subset_csv(text: str) -> pd.DataFrame:
    """Inverse of :func:`write_subset_csv`: all columns as strings ("" for blanks)."""
    import io

    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"Globe-LFMC subset lacks columns {sorted(missing)}")
    return df


# ---------------------------------------------------------------------------- connector
class GlobeLfmcConnector(BaseConnector):
    """Yearly units of Globe-LFMC 2.0 rows inside the lake region (see module docstring)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._verified: set[str] = set()
        self._rows_cache: dict[str, pd.DataFrame] = {}
        self._region = lake_region()

    def opt(self, name: str) -> Any:
        """Dataset option ``name`` or its default (an explicit ``md5: null`` means "ask figshare")."""
        return self.dataset.options[name] if name in self.dataset.options else DEFAULTS[name]

    @property
    def region_bbox(self) -> tuple[float, float, float, float]:
        """Envelope of the scope: the lake region's bbox, else the ``region_bbox`` option."""
        if self._region is not None:
            return self._region.bbox
        bbox = tuple(float(v) for v in self.opt("region_bbox"))
        if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise ValueError(f"{self.dataset.id}: region_bbox must be [west, south, east, north], got {bbox}")
        return bbox

    @property
    def scope(self) -> dict:
        """What selects the rows (part of the source key and of the subset file name)."""
        if self._region is not None:
            return {"region": self._region.id, "fingerprint": self._region.fingerprint}
        return {"region_bbox": list(self.region_bbox)}

    def scope_tag(self) -> str:
        name = self._region.id if self._region is not None else "bbox"
        return f"{name}-{short_hash(self.scope)}"

    def raw_rel(self) -> str:
        """Where older versions kept the whole workbook inside the lake (migrated to the cache, see docstring)."""
        pub = date.fromisoformat(str(self.opt("published")))
        name = str(self.opt("file_name")).replace(" ", "_")
        return f"raw/{self.dataset.id}/{pub:%Y}/{pub:%m}/{name}"

    def subset_rel(self) -> str:
        """``raw/`` path of the region subset CSV (all years)."""
        pub = date.fromisoformat(str(self.opt("published")))
        stem = str(self.opt("file_name")).replace(" ", "_").rsplit(".", 1)[0]
        md5 = str(self.opt("md5") or "live")[:8]
        return f"raw/{self.dataset.id}/{pub:%Y}/{pub:%m}/{stem}__{md5}__{self.scope_tag()}.csv"

    def cache_file(self):
        """Local cache path of the workbook (outside the lake)."""
        return self.settings.cache_path(self.dataset.id, str(self.opt("file_name")).replace(" ", "_"))

    # ------------------------------------------------------------------ connector interface
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        first = max(period.start.year, int(self.opt("first_year")))
        last = min(period.end.year, int(self.opt("last_year")))
        region_hash = short_hash(self.scope)
        refs = []
        for year in range(first, last + 1):
            key = f"{SOURCE}:{self.opt('file_id')}:{year}:region-{region_hash}"
            refs.append(self.ref(key, utc_midnight(date(year, 1, 1)), urls=[self.opt("download_url")],
                                 year=year, region_bbox=list(self.region_bbox), scope=self.scope))
        return refs

    def expected_md5(self) -> str:
        md5 = self.opt("md5")
        if md5:
            return str(md5).lower()
        resp = self.http_session().get(self.opt("api_record"), timeout=self.settings.http_timeout)
        resp.raise_for_status()
        for f in resp.json().get("files", []):
            if int(f.get("id", -1)) == int(self.opt("file_id")):
                return str(f.get("computed_md5") or f.get("supplied_md5")).lower()
        raise RuntimeError(f"file {self.opt('file_id')} not listed in {self.opt('api_record')}")

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        """The region subset CSV in ``raw/`` (written once from the cached workbook; see module docstring)."""
        rel = self.subset_rel()
        if rel in self._verified:
            return [rel]
        legacy = self.raw_rel()
        if not self.lake.exists(rel):
            path = self.cached_workbook()
            sheet = read_lfmc_sheet(str(path), str(self.opt("sheet")), self.region_bbox)
            if self._region is not None and len(sheet):
                sheet = sheet[self._region.contains_points(sheet["lon"].to_numpy(), sheet["lat"].to_numpy())]
            self.lake.write_text(rel, write_subset_csv(sheet.reset_index(drop=True)))
            log.info("globe-lfmc: %d rows inside %s written to %s", len(sheet), self.scope_tag(), rel)
        if self.lake.exists(legacy):           # the global workbook must not stay in the lake
            self.lake.rm(legacy)
        self._verified.add(rel)
        return [rel]

    def cached_workbook(self):
        """Path of the md5-verified workbook in the local cache: reused, moved there from the lake (older
        versions kept it in ``raw/``), or downloaded."""
        expected = self.expected_md5()
        path = self.cache_file()
        if path.is_file():
            with open(path, "rb") as f:
                if file_md5(f) == expected:
                    return path
            log.warning("cached %s has a different md5; fetching it again", path)
        legacy = self.raw_rel()
        if self.lake.exists(legacy):
            with self.lake.open(legacy, "rb") as f:
                ok = file_md5(f) == expected
            if ok:
                tmp = path.with_suffix(path.suffix + ".part")
                with self.lake.open(legacy, "rb") as src, open(tmp, "wb") as dst:
                    for block in iter(lambda: src.read(1 << 20), b""):
                        dst.write(block)
                tmp.replace(path)
                self.lake.rm(legacy)
                log.info("moved %s out of the lake to the cache %s", legacy, path)
                return path
        self._download(path, expected)
        return path

    def _download(self, path, expected_md5: str) -> None:
        """Stream the workbook to ``path`` (via ``<path>.part``), refusing an md5 mismatch."""
        session = self.http_session()
        h = hashlib.md5(usedforsecurity=False)
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            with session.get(self.opt("download_url"), stream=True, timeout=self.settings.http_timeout) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as out:
                    for block in resp.iter_content(chunk_size=1 << 20):
                        out.write(block)
                        h.update(block)
            if h.hexdigest() != expected_md5:
                raise RuntimeError(f"md5 mismatch for {self.opt('download_url')}: got {h.hexdigest()}, "
                                   f"expected {expected_md5}")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    def load_region_rows(self, rel: str) -> pd.DataFrame:
        if rel not in self._rows_cache:
            self._rows_cache[rel] = read_subset_csv(self.lake.read_text(rel))
        return self._rows_cache[rel]

    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        year = int(ref.meta["year"])
        sheet = self.load_region_rows(raw[0])
        rows, counts = sheet_to_raw(sheet, year)
        df = collapse_samples(rows, SOURCE, tuple(self.opt("plausible_range")))
        part = self.table_path(f"{aoi.id}__{year}")
        write_geoparquet(self.lake, part, df)

        bbox = points_bbox(df) or self.region_bbox
        props = {
            "globe:year": year,
            "globe:region": self._region.id if self._region is not None else None,
            "globe:region_bbox": list(self.region_bbox),
            "globe:file_md5": self.expected_md5(),
            "globe:subset": raw[0],
            **{f"globe:{k}": v for k, v in counts.items()},
            "globe:n_raw_rows_collapsed": int(df["n_raw_rows"].sum()) if len(df) else 0,
            "globe:n_sites": int(df["site_id"].nunique()),
            "globe:n_site_days": int(df[["site_id", "date"]].drop_duplicates().shape[0]) if len(df) else 0,
            "globe:qc_flag_counts": qc_summary(df),
            "fuellake:source_ids": [self.opt("doi_url"), self.opt("download_url")],
        }
        start, end = date(year, 1, 1), date(year, 12, 31)
        return [StagedItem(item_id=self.item_id(aoi, ref), datetime=ref.datetime, properties=props,
                           assets=[self.table_asset(part)], bbox=bbox, geometry=bbox_geometry(bbox),
                           start_datetime=utc_midnight(start), end_datetime=utc_end_of_day(end))]
