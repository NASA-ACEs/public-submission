# fuel-lake — shared helpers for the LFMC label connectors (collapse rule, QC flags, typed table frames)
# Licensed under Apache-2.0.
"""Helpers shared by :mod:`fuellake.connectors.fems` and :mod:`fuellake.connectors.globe_lfmc`.

Both connectors first turn source rows into :class:`RawSample` records, then call
:func:`collapse_samples`, which produces one ``lfmc_samples`` row per contract key
``(source, site_id, date, species)``.

Collapse rule (deterministic, shared by both sources)
-----------------------------------------------------
Every raw row has a *component*: ``""`` for a whole-species row, otherwise the part it describes
(FEMS growth phase ``new`` / ``old`` from the fuel-type suffix or the Sub-Category, crown position
``lower`` / ``middle`` / ``upper``, ``disturbance``; Globe-LFMC ``Old or new leaves`` = ``new`` / ``old``).
For each key:

1. If whole-species rows exist, the value is the mean of those rows. Component rows of the same key are
   then ignored for the value. FEMS publishes the plain species row as the mean of its growth-phase rows
   (139 of 140 tested site-days), so averaging the components again would double-count.
2. Otherwise the value is the mean over components of the per-component mean (equal weight per
   component, e.g. ``(new + old) / 2``), which reproduces the FEMS convention for site-days without a
   plain row.
3. ``n_raw_rows`` counts **all** raw rows of the key, including ignored component rows.
4. ``outlier_flag`` is True if any raw row carries the source outlier flag.
5. ``fuel_category`` is the most frequent category of the raw rows (ties: alphabetical);
   ``status`` the sorted distinct source statuses joined with ``;`` (null if none).

QC flag codes (``qc_flags``, sorted, ``;``-joined, empty string when clean)
---------------------------------------------------------------------------
``value_out_of_range``      a raw value or the collapsed value is outside the plausible range
                            (dataset option ``plausible_range``, default 30-400 %); checked on raw
                            rows too, so e.g. a 599999 sentinel cannot hide inside a mean
``components_averaged``     no whole-species row; the value is the mean of component means
``single_component``        no whole-species row and only one component (e.g. new growth only)
``replicates_averaged``     more than one raw row of the same component went into the value
``component_mean_mismatch`` a whole-species value differs by more than 1 percentage point from the
                            mean of two or more component rows of the same key (so it is not their
                            average; FEMS California June-November 2026: 1 of 140 such site-days)
``outlier_partial``         only some of the raw rows carry the source outlier flag
plus row-level codes set by the connectors (e.g. ``method_missing``, ``lon_sign_corrected``), which
are the union over all raw rows of the key.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from itertools import groupby

import pandas as pd
import pyarrow as pa

from fuellake.tables import LFMC_COLUMNS, lfmc_sample_id

DEFAULT_PLAUSIBLE_RANGE = (30.0, 400.0)
MISMATCH_TOLERANCE_PCT = 1.0

STRING = pd.ArrowDtype(pa.string())
DATE32 = pd.ArrowDtype(pa.date32())


@dataclass(frozen=True)
class RawSample:
    """One source row, already mapped to contract vocabulary."""

    site_id: str
    site_name: str | None
    lon: float
    lat: float
    date: date
    species: str                     # normalised lower case
    value: float                     # LFMC % of dry weight
    component: str = ""              # "" = whole-species row
    fuel_category: str = "unknown"
    status: str | None = None
    outlier: bool = False
    flags: frozenset[str] = field(default_factory=frozenset)

    @property
    def key(self) -> tuple[str, date, str]:
        return (self.site_id, self.date, self.species)


def _mean(values: Iterable[float]) -> float:
    vals = list(values)
    return math.fsum(vals) / len(vals)


def _mode(values: Iterable[str]) -> str:
    counts = Counter(values)
    best = max(counts.values())
    return sorted(v for v, n in counts.items() if n == best)[0]


def collapse_samples(samples: Iterable[RawSample], source: str,
                     plausible_range: tuple[float, float] = DEFAULT_PLAUSIBLE_RANGE,
                     ingested_at: datetime | None = None) -> pd.DataFrame:
    """Collapse raw rows to one row per ``(source, site_id, date, species)`` (see module docstring)."""
    lo, hi = (float(v) for v in plausible_range)
    ordered = sorted(samples, key=lambda s: (s.site_id, s.date, s.species, s.component, s.value))
    records = []
    for (site_id, day, species), grp in groupby(ordered, key=lambda s: s.key):
        rows = list(grp)
        plain = [r for r in rows if not r.component]
        components = sorted({r.component for r in rows if r.component})
        by_comp = {c: [r for r in rows if r.component == c] for c in components}
        used = {"": plain} if plain else by_comp
        value = _mean(_mean(r.value for r in rs) for rs in used.values())

        flags: set[str] = set()
        for r in rows:
            flags |= r.flags
        if not plain:
            flags.add("components_averaged" if len(components) > 1 else "single_component")
        if any(len(rs) > 1 for rs in used.values()):
            flags.add("replicates_averaged")
        if plain and len(components) >= 2:
            comp_mean = _mean(_mean(r.value for r in rs) for rs in by_comp.values())
            if abs(value - comp_mean) > MISMATCH_TOLERANCE_PCT:
                flags.add("component_mean_mismatch")
        if not lo <= value <= hi or any(not lo <= r.value <= hi for r in rows):
            flags.add("value_out_of_range")
        n_outlier = sum(1 for r in rows if r.outlier)
        if 0 < n_outlier < len(rows):
            flags.add("outlier_partial")

        statuses = sorted({r.status for r in rows if r.status})
        first = rows[0]
        records.append({
            "source": source, "site_id": site_id, "site_name": first.site_name,
            "lon": float(first.lon), "lat": float(first.lat), "date": day, "species": species,
            "fuel_category": _mode(r.fuel_category for r in rows), "lfmc_pct": float(value),
            "n_raw_rows": len(rows), "outlier_flag": n_outlier > 0, "qc_flags": ";".join(sorted(flags)),
            "status": ";".join(statuses) if statuses else None,
        })
    return lfmc_frame(records, ingested_at=ingested_at)


def lfmc_frame(records: list[dict], ingested_at: datetime | None = None) -> pd.DataFrame:
    """``lfmc_samples`` DataFrame with exact column types (also for zero rows) and computed ``sample_id``."""
    ts = pd.Timestamp(ingested_at or datetime.now(UTC))
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    n = len(records)

    def col(name: str) -> list:
        return [r.get(name) for r in records]

    df = pd.DataFrame({
        "sample_id": pd.Series([lfmc_sample_id(r["source"], r["site_id"], r["date"], r["species"])
                                for r in records], dtype=STRING),
        "source": pd.Series(col("source"), dtype=STRING),
        "site_id": pd.Series(col("site_id"), dtype=STRING),
        "site_name": pd.Series(col("site_name"), dtype=STRING),
        "lon": pd.Series(col("lon"), dtype="float64"),
        "lat": pd.Series(col("lat"), dtype="float64"),
        "date": pd.Series(col("date"), dtype=DATE32),
        "species": pd.Series(col("species"), dtype=STRING),
        "fuel_category": pd.Series(col("fuel_category"), dtype=STRING),
        "lfmc_pct": pd.Series(col("lfmc_pct"), dtype="float64"),
        "n_raw_rows": pd.Series(col("n_raw_rows"), dtype="int32"),
        "outlier_flag": pd.Series(col("outlier_flag"), dtype="bool"),
        "qc_flags": pd.Series([r.get("qc_flags") or "" for r in records], dtype=STRING),
        "status": pd.Series(col("status"), dtype=STRING),
        "ingested_at": pd.Series([ts] * n, dtype="datetime64[us, UTC]"),
    })
    return df[[c for c in LFMC_COLUMNS if c != "geometry"]]


# ---------------------------------------------------------------------------- units and items
def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def months_between(start: date, end: date) -> list[date]:
    """First day of every calendar month that overlaps ``[start, end]``."""
    out, cur = [], month_start(start)
    while cur <= end:
        out.append(cur)
        cur = month_end(cur) + timedelta(days=1)
    return out


def utc_midnight(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def utc_end_of_day(d: date) -> datetime:
    return datetime.combine(d, time.max, tzinfo=UTC)


def short_hash(obj) -> str:
    """8 hex chars of sha1 over a canonical JSON dump (for source keys that must follow config)."""
    return hashlib.sha1(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:8]


def points_bbox(df: pd.DataFrame) -> tuple[float, float, float, float] | None:
    if df.empty:
        return None
    return (float(df["lon"].min()), float(df["lat"].min()), float(df["lon"].max()), float(df["lat"].max()))


def bbox_geometry(bbox: tuple[float, float, float, float]) -> dict | None:
    """A Point geometry for a degenerate bbox (single site), else None (the catalog uses the bbox polygon)."""
    w, s, e, n = bbox
    if w == e and s == n:
        return {"type": "Point", "coordinates": [w, s]}
    return None


def qc_summary(df: pd.DataFrame) -> dict[str, int]:
    """Count of rows per QC flag code (for item properties)."""
    counts: Counter[str] = Counter()
    for flags in df["qc_flags"].fillna(""):
        counts.update(f for f in str(flags).split(";") if f)
    return dict(sorted(counts.items()))
