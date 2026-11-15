# fuel-lake — statewide point-feature table (one row per fire ignition / LFMC sample, features at native grids)
# Licensed under Apache-2.0.
"""Build ``features/<aoi>/point_features.parquet`` (gold collection ``point-features``): the training table of the
fire-spread model, P(fire >= 400 ha | ignition), and of the LFMC models, for the whole of California.

Staging 30 m rasters for the state is not feasible (about 471 M cells per layer), so every feature is sampled
**at the point from its native grid**: form B stores on their own 4-9 km grids, NISAR SME2 on the 200 m ``ca``
grid, static layers from the AOI they are staged on or, for points outside every such AOI, straight from the
public source (:mod:`fuellake.features._point_sources`), Sentinel-2 from small COG windows
(:mod:`fuellake.features._s2points`). Lag, causal and nearest-scene logic is the matchups' own
(:func:`fuellake.features.matchups.pick_nearest`, :func:`~fuellake.features.matchups.weather_at`,
:func:`~fuellake.features.matchups.config_lag_variables`).

Rules
-----
AOI
    ``region_or_aoi`` is an AOI id (``ca``) or the lake region id (``california``, mapped to the region-scale AOI).
Rows
    One row per row of each requested point table (``tables``; specs in ``options.point_tables``): the cataloged
    parts of every dataset with that ``table``, lon/lat inside the AOI bbox and date in ``[start, end]`` (bounds
    inclusive), deduplicated on the table's id column keeping the latest ``ingested_at`` (``ignition_id`` for
    ``fire_ignitions``). A requested table without a registered dataset or without rows is recorded in the item
    property ``fuellake:missing_tables`` and does not stop the build. Features are computed once per distinct
    point-date (lon, lat, date): LFMC species of one site-day share them.
Common columns
    ``point_type`` (``fire_ignition`` | ``lfmc_sample``), ``point_id`` (``ignition_id`` | ``sample_id``),
    ``date`` (ignition discovery date, local; LFMC sampling date), ``lon``, ``lat``, ``block_50km``, then each
    table's identifying and label columns (``options.point_tables.<table>.keep``) and ``large_fire`` (final
    size >= ``large_fire_ha``, default 400 ha; null when the size is unknown or for LFMC rows).
block_50km
    Equal-area blocks of 250 x 250 cells of the NISAR SME2 EASE-Grid 2.0 200 m grid (EPSG:6933, cell
    200.17900467 m, so 50.04475 km), counted from the NSIDC upper-left corner (-17367530.44516138,
    7314540.83011508): ``row = floor((UL_Y - y) / B)``, ``col = floor((x - UL_X) / B)``, id ``"r<row>c<col>"``
    with three digits each (e.g. ``r101c568``). Every ``ca`` grid cell and every 1 km EASE-Grid 2.0 cell (5 x 5
    SME2 cells, the GEDI L4B grid) lies in exactly one block; 9 km SMAP cells (45 SME2 cells) do not nest.
    EPSG:6933 is equal-area but not conformal: every block covers 2504.5 km2, but on the ground it measures
    48.7 km east-west x 51.5 km north-south at 32.5 N, 46.2 x 54.4 km at 37 N and 43.0 x 58.5 km at 42 N
    (geodesic lengths of the block edges). Deterministic and independent of the data.
Causal (default True)
    Nothing dated after the point's date is used: form B on the exact days ``date - L``; dynamic scenes and
    Sentinel-2 scenes on or before the date (same day allowed, recorded in ``*_dt_days``); versioned static
    layers only from a release whose conditions end before the date (see "Statics"). Terrain is time-invariant
    and exempt. ``causal=False`` allows later scenes within the windows and the item-date rule for releases.
Form B (native grids)
    Every registered ``raster-b`` dataset (not gold, not interface, not the climatology) whose items intersect
    the points' bbox, on any AOI. Column ``<var>`` is the daily value on the point date (bilinear in the native
    CRS, the matchups' rule), ``<var>_lag<L>d`` the value on ``date - L`` for the lag variables
    (:func:`~fuellake.features.matchups.config_lag_variables`: ``soil_moisture``, ``swvl1`` and every variable
    flagged ``lag: true``, e.g. SMAP ``sm_*`` and the team feature ``smap_sm``). A variable with ``alias_of``
    (``smap_sm`` in ``smap-l3e``) is read from its own Zarr variable, or from the aliased one when a store
    lacks the copy. Points are processed in calendar-month chunks; each monthly store is loaded once and kept
    while a later chunk can still need it (lags).
Dynamic form A (on the build AOI)
    Scene datasets that support the AOI's scale (on ``ca``: NISAR SME2). Column ``<var>`` = nearest scene on or
    before the date within ``window_days`` (default 12 = one NISAR repeat cycle), sampled at the 200 m cell
    containing the point; ``<var>_dt_days`` = scene date - point date; lags like the matchups.
SME2 anomaly (``options.sme2``)
    When a climatology item exists (``smap-climatology``: 366 steps of the leap reference year 1904, variables
    ``<prefix>_mean`` / ``<prefix>_std``, default prefix ``l4_sm_surface``): ``sme2_anomaly = SME2 - mean`` and
    ``sme2_zscore = sme2_anomaly / std``, where mean and std are the climatology on the **month and day of the
    SME2 scene** (29 Feb is its own step), interpolated bilinearly at the point on the 9 km grid. No bias is
    removed: SME2 PROVISIONAL is reported to read about +0.11 m3/m3 too wet against SMAP, and that offset is
    carried in both columns (a constant shift, plus scale differences: 200 m SME2 against a 9 km climatology).
Statics (``options.statics``; one group per entry)
    ``fuel`` (``landfire-fbfm40``: ``fuel_model`` and the Scott and Burgan (2005) Table 7 loads derived from the
    code with :func:`fuellake.connectors.landfire_fuel_load.map_codes`), ``canopy`` (``landfire-canopy``),
    ``terrain`` (``landfire-topo`` LF 2020 bare earth, else ``cop-dem``; the first product with a finite
    elevation gives all three values) and ``gedi`` (``gedi-l4b``). Releases = the cataloged items of the product
    (any AOI) merged by date with the releases known from config (``landfire-fbfm40`` ``options.releases``, the
    ``remote_releases`` of the group, the topo and DEM defaults). Release choice per point date, from that list
    only (availability never chooses the release):

    - causal, versioned: the latest release whose ``valid_from`` is on or before the date. ``valid_from`` is the
      day after the release's disturbance window (``disturbances.through``, else 1 January after
      ``disturbances.last_year``; LANDFIRE: LF 2016 -> 2017-01-01, LF 2022 -> 2022-10-01, LF 2025 ->
      2025-10-01), else the day after the item's ``end_datetime`` (GEDI L4B: 2023-03-17). A release contains the
      disturbances of its own year, so with the plain item date a 2022 fire would see its own scar in LF 2022.
      No valid release -> NaN, missing reason ``no_release`` (2016 ignitions get no fuels: LF 2016 is the oldest
      keyless release).
    - not causal, versioned: the latest release dated on or before the date, else the earliest.
    - time-invariant (terrain): the latest release on or before the date, else the earliest.

    Value: the staged COG pixel containing the point when the point lies inside the grid of an item of that
    release (``<group>_source`` = ``lake:<aoi>``), else the remote source of that release (``remote``), else
    missing (``outside``). Columns ``<group>_release`` and ``<group>_source`` record the choice per row.
Sentinel-2 (``sentinel2``)
    Rules in :mod:`fuellake.features._s2points` (window ``s2_window_days`` = 8, causal). ``s2_max_points`` caps
    the point-dates processed: every point-date of an ignition with final size >= ``large_fire_ha`` first, then a
    stratified random sample (seed ``seed``) of the rest, strata (point_type, year, size class), proportional
    allocation with largest remainders. Others get ``s2_status = not_sampled``. Columns ``s2_ndvi``,
    ``s2_ndmi``, ``s2_dt_days``, ``s2_scene``, ``s2_status``.
Missing inputs
    Every value that is not available is NaN / null, never 0. ``missing_inputs`` lists, per row, each input group
    without an on-date value as ``<group>:<reason>`` joined with ``;`` (reasons: ``no_items`` nothing cataloged,
    ``no_data`` no value at the point/date, ``no_scene`` no scene in the window, ``no_release``, ``outside``,
    ``error`` a remote read failed, ``cloudy``, ``not_sampled``, ``no_sme2``, ``no_climatology``);
    ``n_missing_inputs`` counts them. Groups: each form B dataset, each dynamic dataset, ``sme2_anomaly``, each
    static group, ``sentinel2``.

Columns are listed in the item property ``fuellake:columns`` (role, dataset, lag, unit); fill rates per column in
``fuellake:fill_rates``; timings and remote/Sentinel-2 statistics in ``fuellake:timings_s``,
``fuellake:remote_stats`` and ``fuellake:s2_stats``. The build is idempotent (overwrites the file, replaces the
AOI's single item).
"""

from __future__ import annotations

import io
import logging
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from fuellake import catalog
from fuellake.catalog import asset_rel, to_utc
from fuellake.features._common import (
    GOLD_IDS,
    FeatureVar,
    Inputs,
    WeatherSeries,
    _form_a_var,
    as_date,
    day_number,
    discover_inputs,
    is_static,
    item_day,
    new_run_id,
    read_scene,
    register_gold_item,
    sample_pixels,
    split_rules,
    static_dataset_ids,
    to_float,
)
from fuellake.features.matchups import EPOCH_ORDINAL, config_lag_variables, pick_nearest, weather_at
from fuellake.grid import EASE2_200M_CELL, EASE2_UL_X, EASE2_UL_Y
from fuellake.registry import AOI, DatasetConfig, lake_region, list_aois, list_datasets, load_aoi, load_dataset
from fuellake.storage import Lake
from fuellake.tables import PARQUET_MEDIA_TYPE, read_parquet, to_geodataframe

log = logging.getLogger("fuellake.features.points")

POINTS_DATASET = "point-features"
POINT_TABLE = "point_features"
BLOCK_CELLS = 250
BLOCK_SIZE_M = BLOCK_CELLS * EASE2_200M_CELL          # 50,044.75 m
COMMON_COLUMNS = ["point_type", "point_id", "date", "lon", "lat", "block_50km"]

LFMC_KEEP = ["sample_id", "source", "site_id", "site_name", "species", "fuel_category", "lfmc_pct", "n_raw_rows",
             "outlier_flag", "qc_flags", "status", "ingested_at"]
IGNITION_KEEP = ["ignition_id", "source", "source_id", "fire_name", "fire_year", "discovery_date",
                 "discovery_datetime", "fire_size_ac", "fire_size_ha", "size_class", "cause_class", "cause_general",
                 "mtbs_id", "irwin_id", "unique_fire_id", "fpa_id", "ingested_at"]

DEFAULT_OPTIONS: dict[str, Any] = {
    "large_fire_ha": 400.0,
    "window_days": 12,
    "point_tables": {
        "lfmc_samples": {"point_type": "lfmc_sample", "id": "sample_id", "date": "date", "keep": LFMC_KEEP},
        "fire_ignitions": {"point_type": "fire_ignition", "id": "ignition_id", "date": "discovery_date",
                           "keep": IGNITION_KEEP},
    },
    "sme2": {"dataset": "nisar-sme2", "variable": "soil_moisture", "climatology_dataset": "smap-climatology",
             "climatology_prefix": "l4_sm_surface"},
    "statics": [
        {"group": "fuel", "products": ["landfire-fbfm40"], "variables": {"fuel_model": "fuel_model"},
         "derive": "scott-burgan", "time_invariant": False},
        {"group": "canopy", "products": ["landfire-canopy"],
         "variables": {"canopy_cover": "canopy_cover", "canopy_height": "canopy_height",
                       "canopy_base_height": "canopy_base_height", "canopy_bulk_density": "canopy_bulk_density"},
         "time_invariant": False,
         "remote_releases": {"landfire-canopy": [
             {"version": "LF 2016", "year": 2016, "valid_from": "2017-01-01"},
             {"version": "LF 2022", "year": 2022, "valid_from": "2022-10-01"},
             {"version": "LF 2025", "year": 2025, "valid_from": "2025-10-01"}]}},
        {"group": "terrain", "products": ["landfire-topo", "cop-dem"],
         "variables": {"elevation": "elevation", "slope": "slope", "aspect": "aspect"}, "time_invariant": True},
        {"group": "gedi", "products": ["gedi-l4b"],
         "variables": {"agbd": "agbd", "agbd_se": "agbd_se", "gedi_quality_flag": "quality_flag"},
         "time_invariant": False},
    ],
    "sentinel2": {"config_dataset": "sentinel2-indices", "collections": ["sentinel-2-c1-l2a", "sentinel-2-l2a"],
                  "window_days": 8, "footprint_m": 30, "max_workers": 16, "search_bin_days": 8},
    "remote": {"batch": 500, "max_workers": 6},
}

_LICENSE = {
    "id": "various", "url": None, "redistributable": False,
    "attribution": "Derived from the input datasets listed in the item property fuellake:source_ids and the remote "
                   "sources in fuellake:remote_sources; each input keeps its own licence (fuellake:input_licenses).",
    "notes": "Derived product. Redistributable only if every input is: see the item property "
             "fuellake:redistributable.",
}

# Built-in config used when the config directory has no point-features.yaml (the repository ships an identical
# copy as config/datasets/point-features.yaml; tests/test_features_points.py checks that they match).
BUILTIN_CONFIG: dict[str, Any] = {
    "id": POINTS_DATASET,
    "title": "fuel-lake gold point-feature table (fire ignitions and LFMC samples x native-grid features)",
    "kind": "table",
    "table": POINT_TABLE,
    "connector": None,
    "status": "unverified",
    "scales": ["local", "region"],
    "provider": "fuel-lake feature layer (fuellake.features.points), derived from cataloged items and public "
                "remote sources",
    "homepage": None,
    "access": {"method": "lake: features/<aoi>/point_features.parquet (GeoParquet)", "auth": "none", "env": []},
    "license": _LICENSE,
    "cadence": "on build (fuellake build-points)",
    "native_resolution": "point (fire ignition or LFMC site), features sampled on each input's native grid",
    "variables": [],
    "caveats": [
        "One row per fire_ignitions / lfmc_samples row inside the AOI bbox and period; rules in the "
        "fuellake.features.points docstring.",
        "Rows are not independent (sites, species, dates, neighbouring fires): split by block_50km for "
        "spatial cross-validation.",
        "Causal by default: nothing dated after the point's date; versioned statics use the release whose "
        "disturbance window ends before the date, so 2016 ignitions have no LANDFIRE fuels.",
        "SME2 anomalies carry the reported SME2 PROVISIONAL wet bias (about +0.11 m3/m3); no bias is removed.",
        "Missing values are null, never 0; missing_inputs lists the input groups without a value per row.",
    ],
    "options": DEFAULT_OPTIONS,
}


# ---------------------------------------------------------------------------- config and small helpers
def points_path(aoi_id: str) -> str:
    return f"features/{aoi_id}/point_features.parquet"


def point_config() -> DatasetConfig:
    """``config/datasets/point-features.yaml`` if present, else :data:`BUILTIN_CONFIG`."""
    try:
        return load_dataset(POINTS_DATASET)
    except KeyError:
        return DatasetConfig(**BUILTIN_CONFIG)


def _options(cfg: DatasetConfig) -> dict:
    out = {**DEFAULT_OPTIONS, **(cfg.options or {})}
    return out


def resolve_aoi(region_or_aoi: str) -> AOI:
    """An AOI id, or the lake region id mapped to the (single) region-scale AOI."""
    try:
        return load_aoi(region_or_aoi)
    except KeyError:
        region = lake_region()
        if region is not None and region.id == region_or_aoi:
            cands = [a for a in list_aois() if a.scale == "region"]
            if len(cands) == 1:
                return cands[0]
            raise KeyError(f"region {region_or_aoi!r} has {len(cands)} region-scale AOIs; pass an AOI id") from None
        raise


def block_ids(lon: Iterable[float], lat: Iterable[float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(id strings, rows, cols)`` of the 50 km EASE-Grid 2.0 blocks (module docstring); None / -1 for invalid
    coordinates."""
    from pyproj import Transformer

    lon = np.asarray(lon, dtype="float64")
    lat = np.asarray(lat, dtype="float64")
    ok = np.isfinite(lon) & np.isfinite(lat)
    x = np.full(lon.shape, np.nan)
    y = np.full(lon.shape, np.nan)
    if ok.any():
        tx, ty = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True).transform(lon[ok], lat[ok])
        x[ok], y[ok] = tx, ty
    rows = np.where(ok, np.floor((EASE2_UL_Y - y) / BLOCK_SIZE_M), -1).astype("int64")
    cols = np.where(ok, np.floor((x - EASE2_UL_X) / BLOCK_SIZE_M), -1).astype("int64")
    ids = np.array([f"r{r:03d}c{c:03d}" if k else None for r, c, k in zip(rows, cols, ok, strict=True)],
                   dtype=object)
    return ids, rows, cols


def _as_day(value) -> date | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        return to_utc(value).date()
    except Exception:
        return None


def _int_col(a: np.ndarray) -> pd.array:
    return pd.array([None if (v is None or (isinstance(v, float) and math.isnan(v))) else int(v) for v in a],
                    dtype="Int32")


# ---------------------------------------------------------------------------- rows
@dataclass
class PointSet:
    rows: pd.DataFrame                 # output rows (common + kept columns)
    pd_lon: np.ndarray                 # distinct point-dates
    pd_lat: np.ndarray
    pd_date: list[date]
    pd_day: np.ndarray                 # day numbers since 1970-01-01
    row_pd: np.ndarray                 # row -> point-date index
    loc_lon: np.ndarray                # distinct locations
    loc_lat: np.ndarray
    pd_loc: np.ndarray                 # point-date -> location index
    item_ids: list[str] = field(default_factory=list)
    licenses: dict = field(default_factory=dict)
    by_table: dict = field(default_factory=dict)
    missing_tables: list[str] = field(default_factory=list)

    @property
    def bbox(self) -> tuple[float, float, float, float] | None:
        if not len(self.loc_lon):
            return None
        return (float(self.loc_lon.min()), float(self.loc_lat.min()), float(self.loc_lon.max()),
                float(self.loc_lat.max()))


def read_points(lake: Lake, aoi: AOI, start: date, end: date, tables: Iterable[str], options: dict,
                large_fire_ha: float, notes: list[str]) -> PointSet:
    """Rows of the requested point tables inside the AOI bbox and period (module docstring, "Rows")."""
    specs = options["point_tables"]
    frames, item_ids, licenses, by_table, missing = [], [], {}, {}, []
    w, s, e, n = aoi.bbox
    for table in tables:
        if table not in specs:
            raise ValueError(f"no point table spec for {table!r} (options.point_tables: {sorted(specs)})")
        spec = specs[table]
        dss = [d for d in list_datasets() if d.kind == "table" and d.table == table]
        if not dss:
            notes.append(f"table {table}: no registered dataset")
            missing.append(table)
            by_table[table] = 0
            continue
        parts = []
        for ds in dss:
            for item in catalog.iter_items(lake, ds.id):
                if "table" not in item.get("assets", {}):
                    continue
                part = read_parquet(lake, catalog.asset_rel(item, "table"))
                item_ids.append(item["id"])
                licenses[ds.id] = {"id": ds.license.id, "redistributable": ds.license.redistributable}
                if len(part):
                    parts.append(pd.DataFrame(part.drop(columns=["geometry"], errors="ignore")))
        if not parts:
            notes.append(f"table {table}: no cataloged rows")
            missing.append(table)
            by_table[table] = 0
            continue
        df = pd.concat(parts, ignore_index=True)
        df["_date"] = [_as_day(v) for v in df[spec["date"]]]
        keep = (df["lon"].between(w, e) & df["lat"].between(s, n) & df["_date"].notna())
        keep &= df["_date"].map(lambda d: d is not None and start <= d <= end)
        df = df[keep.fillna(False)]
        if "ingested_at" in df.columns:
            df = df.sort_values("ingested_at", kind="stable")
        df = df.drop_duplicates(subset=[spec["id"]], keep="last")
        out = pd.DataFrame({
            "point_type": spec["point_type"],
            "point_id": df[spec["id"]].astype(str).to_numpy(),
            "date": df["_date"].to_numpy(),
            "lon": df["lon"].astype("float64").to_numpy(),
            "lat": df["lat"].astype("float64").to_numpy(),
        })
        for c in spec.get("keep", []):
            if c in df.columns and c not in out.columns:
                out[c] = df[c].to_numpy()
        if "fire_size_ha" in df.columns:
            size = pd.to_numeric(df["fire_size_ha"], errors="coerce").to_numpy(dtype="float64")
            out["large_fire"] = pd.array([None if math.isnan(v) else bool(v >= large_fire_ha) for v in size],
                                         dtype="boolean")
        frames.append(out)
        by_table[table] = len(out)
    if frames:
        rows = pd.concat(frames, ignore_index=True)
    else:
        rows = pd.DataFrame({c: pd.Series(dtype="object") for c in ["point_type", "point_id", "date"]})
        rows["lon"] = pd.Series(dtype="float64")
        rows["lat"] = pd.Series(dtype="float64")
    if "large_fire" not in rows.columns:
        rows["large_fire"] = pd.array([None] * len(rows), dtype="boolean")
    else:
        rows["large_fire"] = rows["large_fire"].astype("boolean")
    rows = rows.sort_values(["date", "point_type", "point_id"], kind="stable").reset_index(drop=True)
    ids, _, _ = block_ids(rows["lon"].to_numpy(), rows["lat"].to_numpy())
    rows.insert(5, "block_50km", ids)

    keys = list(zip(rows["lon"].to_numpy(), rows["lat"].to_numpy(), rows["date"].to_numpy(), strict=True))
    pd_index: dict[tuple, int] = {}
    row_pd = np.empty(len(keys), dtype="int64")
    for i, k in enumerate(keys):
        row_pd[i] = pd_index.setdefault(k, len(pd_index))
    pd_keys = list(pd_index)
    loc_index: dict[tuple, int] = {}
    pd_loc = np.empty(len(pd_keys), dtype="int64")
    for j, (lo, la, _) in enumerate(pd_keys):
        pd_loc[j] = loc_index.setdefault((lo, la), len(loc_index))
    locs = list(loc_index)
    return PointSet(
        rows=rows,
        pd_lon=np.array([k[0] for k in pd_keys], dtype="float64"),
        pd_lat=np.array([k[1] for k in pd_keys], dtype="float64"),
        pd_date=[k[2] for k in pd_keys],
        pd_day=np.array([day_number(k[2]) for k in pd_keys], dtype="int64"),
        row_pd=row_pd,
        loc_lon=np.array([k[0] for k in locs], dtype="float64"),
        loc_lat=np.array([k[1] for k in locs], dtype="float64"),
        pd_loc=pd_loc,
        item_ids=sorted(set(item_ids)), licenses=licenses, by_table=by_table, missing_tables=missing,
    )


# ---------------------------------------------------------------------------- feature collection
class Features:
    """Feature columns per point-date, their metadata and the per-point-date missing records."""

    def __init__(self, n: int):
        self.n = n
        self.cols: dict[str, Any] = {}
        self.meta: dict[str, dict] = {}
        self.missing: list[list[str]] = [[] for _ in range(n)]
        self._stems: dict[str, str] = {}

    def stem(self, name: str, dataset: str) -> str:
        """Column stem of a variable: its name, or ``<dataset>__<name>`` if another dataset already uses it."""
        owner = self._stems.setdefault(name, dataset)
        return name if owner == dataset else f"{dataset.replace('-', '_')}__{name}"

    def add(self, name: str, values, meta: dict) -> None:
        self.cols[name] = values
        self.meta[name] = meta

    def record(self, group: str, mask: np.ndarray, reason: str | np.ndarray) -> None:
        reasons = np.broadcast_to(np.asarray(reason, dtype=object), (self.n,))
        for i in np.flatnonzero(mask):
            self.missing[i].append(f"{group}:{reasons[i]}")


def _finite(a) -> np.ndarray:
    return np.isfinite(np.asarray(a, dtype="float64"))


# ---------------------------------------------------------------------------- form B
def _weather_datasets(datasets: list[str] | None, exclude: set[str]) -> list[DatasetConfig]:
    cands = [d for d in list_datasets() if d.kind == "raster-b" and d.id not in GOLD_IDS and d.id not in exclude]
    if datasets is None:
        return [d for d in cands if not d.is_interface]
    return [d for d in cands if d.id in set(datasets)]


def _item_days(item: dict) -> tuple[int, int]:
    i0, i1 = catalog.item_interval(item)
    return day_number(i0.date()), day_number(i1.date())


def weather_features(lake: Lake, ps: PointSet, feats: Features, inputs: Inputs, lags: list[int],
                     lag_set: set[str], datasets: list[str] | None, exclude: set[str], timings: dict) -> None:
    """Form B columns (module docstring, "Form B")."""
    t0 = time.perf_counter()
    n = len(ps.pd_day)
    reach = max(lags) if lags else 0
    bbox = ps.bbox
    for ds in _weather_datasets(datasets, exclude):
        names = [v.name for v in ds.variables]
        alias = {v.name: str((v.model_extra or {}).get("alias_of") or "") for v in ds.variables}
        items = [] if bbox is None else catalog.search(
            lake, [ds.id], bbox=bbox, start=date.fromordinal(EPOCH_ORDINAL + int(ps.pd_day.min()) - reach),
            end=date.fromordinal(EPOCH_ORDINAL + int(ps.pd_day.max())))
        if not items:
            inputs.missing.append(ds.id)
            feats.record(ds.id, np.ones(n, dtype=bool), "no_items")
            continue
        cols: dict[str, np.ndarray] = {}
        lagged = [nm for nm in names if nm in lag_set]
        for nm in names:
            cols[nm] = np.full(n, np.nan)
            for lag in (lags if nm in lagged else []):
                cols[f"{nm}_lag{lag}d"] = np.full(n, np.nan)
        spans = {it["id"]: _item_days(it) for it in items}
        cache: dict[str, WeatherSeries] = {}
        used: dict[str, dict] = {}
        month_of = np.array([d.year * 12 + d.month - 1 for d in ps.pd_date], dtype="int64")
        for ym in np.unique(month_of):
            idx = np.flatnonzero(month_of == ym)
            lo, hi = int(ps.pd_day[idx].min()) - reach, int(ps.pd_day[idx].max())
            need = [it for it in items if spans[it["id"]][1] >= lo and spans[it["id"]][0] <= hi]
            for k in [k for k in cache if spans[k][1] < lo]:
                del cache[k]                                     # later chunks start later: never needed again
            for it in need:
                if it["id"] not in cache:
                    ws = WeatherSeries(lake, ds, [it])
                    for st in ws.stores:                  # alias per store: a store without the copy reads
                        for nm, target in alias.items():  # the aliased variable (no data copied)
                            if target and nm not in st.data and target in st.data:
                                st.data[nm] = st.data[target]
                    cache[it["id"]] = ws
                    inputs.notes.extend(ws.notes)
                    used[it["id"]] = it
            if not need:
                continue
            series = WeatherSeries.merged(ds, [cache[it["id"]] for it in need])
            present = set(series.variable_names())
            for nm in names:
                if nm not in present:
                    continue
                cols[nm][idx] = weather_at(series, nm, ps.pd_day[idx], ps.pd_lon[idx], ps.pd_lat[idx], 0)
                for lag in (lags if nm in lagged else []):
                    cols[f"{nm}_lag{lag}d"][idx] = weather_at(series, nm, ps.pd_day[idx], ps.pd_lon[idx],
                                                              ps.pd_lat[idx], lag)
        inputs.items[ds.id] = sorted(used.values(), key=lambda it: it["id"])
        inputs.licenses[ds.id] = {"id": ds.license.id, "redistributable": ds.license.redistributable}
        any_value = np.zeros(n, dtype=bool)
        for nm in names:
            stem = feats.stem(nm, ds.id)
            spec = ds.variable(nm)
            base = {"role": "weather", "dataset": ds.id, "source": nm, "alias_of": alias[nm] or None,
                    "unit": spec.unit if spec else None}
            feats.add(stem, cols[nm], {**base, "lag_days": None, "column": "value"})
            any_value |= _finite(cols[nm])
            for lag in (lags if nm in lagged else []):
                feats.add(f"{stem}_lag{lag}d", cols[f"{nm}_lag{lag}d"], {**base, "lag_days": lag, "column": "value"})
        feats.record(ds.id, ~any_value, "no_data")
    timings["weather"] = round(time.perf_counter() - t0, 2)


# ---------------------------------------------------------------------------- dynamic form A + SME2 anomaly
def dynamic_features(lake: Lake, aoi: AOI, ps: PointSet, feats: Features, inputs: Inputs, lags: list[int],
                     lag_set: set[str], window_days: int, causal: bool, datasets: list[str] | None,
                     exclude: set[str], allow_mixed_maturity: bool, maturity: str | None,
                     timings: dict) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Dynamic scene columns; returns ``{variable source: (value, dt_days)}`` for the SME2 anomaly."""
    from pyproj import Transformer

    t0 = time.perf_counter()
    n = len(ps.pd_day)
    static_ids = static_dataset_ids()
    ids = [d.id for d in list_datasets()
           if d.kind == "raster-a" and d.id not in GOLD_IDS and not d.is_interface and d.supports(aoi)
           and not is_static(d, static_ids) and d.id not in exclude]
    if datasets is not None:
        ids = [i for i in ids if i in set(datasets)]
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    if not ids or n == 0:
        timings["dynamic"] = round(time.perf_counter() - t0, 2)
        return out
    reach = (max(lags) if lags else 0) + window_days
    first = date.fromordinal(EPOCH_ORDINAL + int(ps.pd_day.min()))
    last = date.fromordinal(EPOCH_ORDINAL + int(ps.pd_day.max()))
    dyn = discover_inputs(lake, aoi, first, last, search_start=first - timedelta(days=reach),
                          search_end=last if causal else last + timedelta(days=window_days), datasets=ids,
                          static_datasets=static_ids, split_by=split_rules(),
                          allow_mixed_maturity=allow_mixed_maturity, maturity=maturity)
    inputs.notes.extend(dyn.notes)
    inputs.maturity.update(dyn.maturity)
    inputs.licenses.update(dyn.licenses)
    inputs.items.update(dyn.items)
    for ds_id in dyn.missing:
        inputs.missing.append(ds_id)
        feats.record(ds_id, np.ones(n, dtype=bool), "no_items")
    grid = aoi.grid
    gx, gy = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True).transform(ps.pd_lon, ps.pd_lat)
    t = grid.transform
    col = np.floor((np.asarray(gx) - t.c) / t.a).astype("int64")
    row = np.floor((np.asarray(gy) - t.f) / t.e).astype("int64")
    found: dict[str, np.ndarray] = {}
    for v in dyn.by_role("dynamic"):
        scenes = dyn.scenes[v.series]
        scene_days = np.array([day_number(s.day) for s in scenes], dtype="int64")
        values = np.full((n, len(scenes)), np.nan)
        for k, scene in enumerate(scenes):
            values[:, k] = sample_pixels(read_scene(lake, scene, v, grid), row, col, 1, v.agg)
        stem = feats.stem(v.name, v.dataset)
        base = {**v.describe(), "window_days": window_days}
        val, dt = pick_nearest(values, scene_days, ps.pd_day, ps.pd_day, window_days, causal, True)
        feats.add(stem, _int_col(val) if v.categorical else val, {**base, "lag_days": None, "column": "value"})
        feats.add(f"{stem}_dt_days", _int_col(dt), {**base, "lag_days": None, "column": "dt_days"})
        out[f"{v.dataset}:{v.source}"] = (val, dt)
        found[v.dataset] = found.get(v.dataset, np.zeros(n, dtype=bool)) | _finite(val)
        if v.source in lag_set or v.name in lag_set:
            for lag in lags:
                lv, ld = pick_nearest(values, scene_days, ps.pd_day - lag, ps.pd_day, window_days, causal, True)
                feats.add(f"{stem}_lag{lag}d", _int_col(lv) if v.categorical else lv,
                          {**base, "lag_days": lag, "column": "value"})
                feats.add(f"{stem}_lag{lag}d_dt_days", _int_col(ld), {**base, "lag_days": lag, "column": "dt_days"})
    for ds_id, ok in found.items():
        feats.record(ds_id, ~ok, "no_scene")
    timings["dynamic"] = round(time.perf_counter() - t0, 2)
    return out


def sme2_anomaly(lake: Lake, aoi: AOI, ps: PointSet, feats: Features, inputs: Inputs, opts: dict,
                 sme2: tuple[np.ndarray, np.ndarray] | None, timings: dict) -> None:
    """``sme2_anomaly`` / ``sme2_zscore`` (module docstring, "SME2 anomaly")."""
    t0 = time.perf_counter()
    n = len(ps.pd_day)
    meta = {"role": "anomaly", "dataset": opts["dataset"], "climatology": opts["climatology_dataset"],
            "climatology_prefix": opts["climatology_prefix"], "lag_days": None, "unit": "m3/m3"}
    anomaly = np.full(n, np.nan)
    zscore = np.full(n, np.nan)
    reason = np.full(n, "no_sme2", dtype=object)
    if sme2 is not None:
        value, dt = sme2
        has = _finite(value)
        reason[has] = "no_climatology"
        try:
            cds = load_dataset(opts["climatology_dataset"])
        except KeyError:
            cds = None
        items = [] if cds is None or ps.bbox is None else catalog.search(lake, [cds.id], bbox=ps.bbox,
                                                                        static_any_time=True)
        own = [it for it in items if it["properties"].get("fuellake:aoi") == aoi.id]
        items = own or items
        if items and has.any():
            item = items[-1]
            series = WeatherSeries(lake, cds, [item])
            year = int(item["properties"].get("smap_clim:reference_year")
                       or cds.options.get("reference_year", 1904))
            mean_name, std_name = f"{opts['climatology_prefix']}_mean", f"{opts['climatology_prefix']}_std"
            idx = np.flatnonzero(has)
            scene_days = [date.fromordinal(EPOCH_ORDINAL + int(ps.pd_day[i] + dt[i])) for i in idx]
            steps = np.array([date(year, d.month, d.day).toordinal() for d in scene_days])
            mean = np.full(len(idx), np.nan)
            std = np.full(len(idx), np.nan)
            for s in np.unique(steps):
                sel = steps == s
                ref = date.fromordinal(int(s))
                mean[sel] = series.points(mean_name, ref, ps.pd_lon[idx[sel]], ps.pd_lat[idx[sel]])
                std[sel] = series.points(std_name, ref, ps.pd_lon[idx[sel]], ps.pd_lat[idx[sel]])
            anomaly[idx] = value[idx] - mean
            with np.errstate(invalid="ignore", divide="ignore"):
                zscore[idx] = np.where(std > 0, anomaly[idx] / std, np.nan)
            reason[idx] = np.where(_finite(anomaly[idx]), reason[idx], "no_data")
            inputs.items[cds.id] = [item]
            inputs.licenses[cds.id] = {"id": cds.license.id, "redistributable": cds.license.redistributable}
            meta["climatology_item"] = item["id"]
        elif cds is not None and not items:
            inputs.missing.append(cds.id)
    feats.add("sme2_anomaly", anomaly, {**meta, "column": "value",
                                        "method": "SME2 - climatology mean on the SME2 scene's month and day"})
    feats.add("sme2_zscore", zscore, {**meta, "unit": "1", "column": "value",
                                      "method": "sme2_anomaly / climatology std"})
    feats.record("sme2_anomaly", ~_finite(anomaly), reason)
    timings["sme2_anomaly"] = round(time.perf_counter() - t0, 2)


# ---------------------------------------------------------------------------- statics
@dataclass
class Release:
    product: str
    label: str
    day: date                          # nominal release date (item datetime convention)
    valid_from: date                   # causal: usable for points on or after this date
    items: list[dict] = field(default_factory=list)
    remote: dict = field(default_factory=dict)       # product-specific remote spec (services per variable)


def _valid_from_disturbances(dist: dict | None, year: int | None) -> date | None:
    if isinstance(dist, dict):
        if dist.get("through"):
            return date.fromisoformat(str(dist["through"])[:10]) + timedelta(days=1)
        if dist.get("last_year"):
            return date(int(dist["last_year"]) + 1, 1, 1)
    return date(int(year) + 1, 1, 1) if year else None


def _item_valid_from(item: dict) -> date:
    p = item["properties"]
    vf = _valid_from_disturbances(p.get("fuellake:landfire_disturbances"), None)
    if vf:
        return vf
    end = p.get("end_datetime")
    return (to_utc(end).date() + timedelta(days=1)) if end else item_day(item)


def config_releases(product: DatasetConfig, group: dict) -> list[Release]:
    """Releases known from config for a static product (module docstring, "Statics")."""
    o = product.options
    out: list[Release] = []
    if product.id == "landfire-fbfm40":
        rels = o.get("releases") or ([{"version": o.get("product_version", "LF 2025"),
                                       "year": int(o.get("product_year", 2025)),
                                       "image_service": o.get("image_service")}] if o.get("image_service") else [])
        for r in rels:
            year = int(r["year"])
            out.append(Release(product.id, str(r["version"]), date(year, 1, 1),
                               _valid_from_disturbances(r.get("disturbances"), year) or date(year + 1, 1, 1),
                               remote={"fuel_model": {"service": r["image_service"], "kind": "code"}}))
    elif product.id == "landfire-canopy":
        from fuellake.connectors.landfire_canopy import DEFAULT_LAYERS
        from fuellake.connectors.landfire_canopy import default_urls as canopy_urls

        layers = o.get("layers") or DEFAULT_LAYERS
        rels = (group.get("remote_releases") or {}).get(product.id) or [
            {"version": o.get("product_version", "LF 2025"), "year": int(o.get("product_year", 2025))}]
        for r in rels:
            year = int(r["year"])
            remote = {}
            for var, spec in layers.items():
                nf = spec.get("non_forested", 0.0)
                remote[var] = {"service": canopy_urls(spec["code"], year)["service"], "kind": "scaled",
                               "scale": float(spec["scale"]),
                               "non_forested": float("nan") if str(nf).lower() == "nan" else float(nf)}
            vf = date.fromisoformat(str(r["valid_from"])) if r.get("valid_from") else date(year + 1, 1, 1)
            out.append(Release(product.id, str(r["version"]), date(year, 1, 1), vf, remote=remote))
    elif product.id == "landfire-topo":
        from fuellake.connectors.landfire_topo import DEFAULT_LAYERS as TOPO_LAYERS
        from fuellake.connectors.landfire_topo import default_urls as topo_urls

        year = int(o.get("product_year", 2020))
        layers = o.get("layers") or TOPO_LAYERS
        remote = {var: {"service": topo_urls(spec["code"], year)["service"],
                        "kind": "aspect" if spec.get("kind") == "aspect" else "topo"} for var, spec in layers.items()}
        out.append(Release(product.id, str(o.get("product_version", f"LF {year}")), date(year, 1, 1),
                           date(year, 1, 1), remote=remote))
    elif product.id == "cop-dem":
        out.append(Release(product.id, "COP-DEM GLO-30 2021", date(2021, 4, 22), date(2021, 4, 22),
                           remote={"*": {"kind": "cop-dem"}}))
    return out


def product_releases(lake: Lake, product: DatasetConfig, group: dict, bbox) -> list[Release]:
    """Config releases merged by date with the product's cataloged items (any AOI), oldest first."""
    rels = {r.day: r for r in config_releases(product, group)}
    items = [] if bbox is None else catalog.search(lake, [product.id], bbox=bbox, static_any_time=True)
    for it in items:
        d = item_day(it)
        if d not in rels:
            label = str(it["properties"].get("fuellake:landfire_version") or d.isoformat())
            rels[d] = Release(product.id, label, d, _item_valid_from(it))
        rels[d].items.append(it)
    return [rels[d] for d in sorted(rels)]


def choose_release(releases: list[Release], days: list[date], causal: bool, time_invariant: bool) -> np.ndarray:
    """Index into ``releases`` per date (-1 = none), rules of the module docstring."""
    out = np.full(len(days), -1, dtype="int64")
    if not releases:
        return out
    for i, d in enumerate(days):
        if time_invariant or not causal:
            before = [k for k, r in enumerate(releases) if r.day <= d]
            out[i] = before[-1] if before else 0
        else:
            valid = [k for k, r in enumerate(releases) if r.valid_from <= d]
            out[i] = valid[-1] if valid else -1
    return out


RemoteFactory = Callable[[str, Release, list[str], dict], Callable[[np.ndarray, np.ndarray], tuple]]


class RemoteReaders:
    """Builds and caches the remote point readers of the static products."""

    def __init__(self, cache, opts: dict, factory: RemoteFactory | None = None):
        self.cache = cache
        self.batch = int(opts.get("batch", 500))
        self.max_workers = int(opts.get("max_workers", 6))
        self.factory = factory
        self.readers: dict[str, Any] = {}
        self.sources: dict[str, str] = {}

    def _arcgis(self, service: str):
        from fuellake.features._point_sources import ArcGisImageSampler

        if service not in self.readers:
            self.readers[service] = ArcGisImageSampler(service, cache=self.cache, batch=self.batch,
                                                       max_workers=self.max_workers)
            self.sources[service] = service
        return self.readers[service]

    def read(self, release: Release, variables: dict[str, str], product: DatasetConfig,
             lon: np.ndarray, lat: np.ndarray) -> tuple[dict[str, np.ndarray], np.ndarray]:
        """Values ``{asset: array}`` of ``release`` at the points, plus a failed mask."""
        assets = list(dict.fromkeys(variables.values()))
        if self.factory is not None:
            return self.factory(product.id, release, assets, {})(lon, lat)
        n = len(lon)
        failed = np.zeros(n, dtype=bool)
        out: dict[str, np.ndarray] = {}
        if "*" in release.remote and release.remote["*"]["kind"] == "cop-dem":
            from fuellake.features._point_sources import CopDemPointReader

            if "cop-dem" not in self.readers:
                self.readers["cop-dem"] = CopDemPointReader(cache=self.cache)
                self.sources["cop-dem"] = "https://copernicus-dem-30m.s3.amazonaws.com (GLO-30 COGs)"
            vals, failed = self.readers["cop-dem"].sample(lon, lat)
            return {a: vals[a] for a in assets if a in vals}, failed
        for a in assets:
            spec = release.remote.get(a)
            if spec is None:
                continue
            vals, f = self._arcgis(spec["service"]).sample(lon, lat)
            failed |= f
            out[a] = decode_remote(vals, spec, product.variable(a))
        if release.product == "landfire-topo" and {"elevation", "slope"} <= set(out):
            fill = (out["elevation"] == 0) & (out["slope"] == 0) & ~_finite(out.get("aspect", np.full(n, np.nan)))
            for a in out:                                 # LANDFIRE coastal buffer fill (0 m, 0 deg, flat)
                out[a] = np.where(fill, np.nan, out[a])
        return out, failed

    def stats(self) -> dict:
        return {self.sources.get(k, k): dict(r.stats) for k, r in self.readers.items()}


def decode_remote(raw: np.ndarray, spec: dict, var) -> np.ndarray:
    """Physical values from remote raw values: ``code`` as is, ``scaled`` (LANDFIRE canopy: code x scale, code 0 =
    non-forested value), ``aspect`` (-1 flat = NaN); then the variable's ``valid_range``."""
    from fuellake.rasters import mask_out_of_range

    v = np.asarray(raw, dtype="float64").copy()
    kind = spec.get("kind")
    if kind == "scaled":
        zero = v == 0
        v = v * float(spec["scale"])
        v[zero] = spec["non_forested"]
    elif kind == "aspect":
        v[v < 0] = np.nan
    if var is not None and var.valid_range is not None and kind != "code":
        v = mask_out_of_range(v.astype("float32"), var.valid_range)[0].astype("float64")
    return v


def _lake_values(lake: Lake, items: list[dict], product: DatasetConfig, assets: list[str], lon: np.ndarray,
                 lat: np.ndarray) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]:
    """Staged values of a release's items at the points: ``({asset: values}, covered mask, aoi per point)``."""
    from pyproj import Transformer

    from fuellake.rasters import read_cog

    n = len(lon)
    vals = {a: np.full(n, np.nan) for a in assets}
    covered = np.zeros(n, dtype=bool)
    where = np.full(n, None, dtype=object)
    for it in sorted(items, key=lambda i: (i["properties"].get("fuellake:aoi") or "", i["id"])):
        aoi_id = it["properties"].get("fuellake:aoi")
        try:
            grid = load_aoi(aoi_id, check_region=False).grid
        except KeyError:
            continue
        x, y = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True).transform(lon, lat)
        t = grid.transform
        col = np.floor((np.asarray(x) - t.c) / t.a).astype("int64")
        row = np.floor((np.asarray(y) - t.f) / t.e).astype("int64")
        inside = (row >= 0) & (row < grid.height) & (col >= 0) & (col < grid.width) & ~covered
        if not inside.any():
            continue
        for a in assets:
            if a not in it.get("assets", {}):
                continue
            arr, _ = read_cog(lake, asset_rel(it, a))
            fv = _form_a_var(product, a, [it], "static")
            arr = to_float(arr, fv)
            vals[a][inside] = arr[row[inside], col[inside]]
        covered |= inside
        where[inside] = f"lake:{aoi_id}"
    return vals, covered, where


def static_features(lake: Lake, ps: PointSet, feats: Features, inputs: Inputs, groups: list[dict], causal: bool,
                    remote: RemoteReaders | None, timings: dict) -> None:
    """Static columns per group (module docstring, "Statics")."""
    t0 = time.perf_counter()
    n = len(ps.pd_day)
    for g in groups:
        gname = g["group"]
        out_vars: dict[str, str] = dict(g["variables"])
        assets = list(dict.fromkeys(out_vars.values()))
        values = {a: np.full(n, np.nan) for a in assets}
        rel_col = np.full(n, None, dtype=object)
        src_col = np.full(n, None, dtype=object)
        reason = np.full(n, "no_items", dtype=object)
        pending = np.ones(n, dtype=bool)                 # point-dates still without a value (terrain: next product)
        products_used = []
        for pid in g["products"]:
            try:
                product = load_dataset(pid)
            except KeyError:
                continue
            rels = product_releases(lake, product, g, ps.bbox)
            if not rels:
                continue
            products_used.append(pid)
            choice = choose_release(rels, ps.pd_date, causal, bool(g.get("time_invariant")))
            reason[pending & (choice < 0)] = "no_release"
            for k, rel in enumerate(rels):
                sel = pending & (choice == k)
                if not sel.any():
                    continue
                locs = np.unique(ps.pd_loc[sel])
                lon, lat = ps.loc_lon[locs], ps.loc_lat[locs]
                lv, covered, where = _lake_values(lake, rel.items, product, assets, lon, lat)
                loc_vals = {a: lv[a].copy() for a in assets}
                loc_src = where.copy()
                loc_reason = np.full(len(locs), "outside", dtype=object)
                todo = ~covered
                if todo.any() and rel.remote and remote is not None:
                    rv, failed = remote.read(rel, out_vars, product, lon[todo], lat[todo])
                    t_idx = np.flatnonzero(todo)
                    for a in assets:
                        if a in rv:
                            loc_vals[a][t_idx] = np.where(failed, np.nan, rv[a])   # a failed read is no value
                    loc_src[t_idx] = "remote"
                    loc_reason[t_idx[failed]] = "error"
                    loc_src[t_idx[failed]] = None
                    inputs.licenses[product.id] = {"id": product.license.id,
                                                   "redistributable": product.license.redistributable}
                has = np.zeros(len(locs), dtype=bool)
                for a in assets:
                    has |= _finite(loc_vals[a])
                loc_reason[np.array([v is not None for v in loc_src], dtype=bool) & ~has] = "no_data"
                loc_map = {int(lc): j for j, lc in enumerate(locs)}
                pd_idx = np.flatnonzero(sel)
                j = np.array([loc_map[int(ps.pd_loc[i])] for i in pd_idx], dtype="int64")
                ok = has[j]
                for a in assets:
                    values[a][pd_idx[ok]] = loc_vals[a][j[ok]]
                rel_col[pd_idx[ok]] = rel.label
                src_col[pd_idx[ok]] = loc_src[j[ok]]
                reason[pd_idx[~ok]] = loc_reason[j[~ok]]
                pending[pd_idx[ok]] = False
                if covered.any():
                    inputs.items.setdefault(product.id, [])
                    known = {i["id"] for i in inputs.items[product.id]}
                    inputs.items[product.id] += [i for i in rel.items if i["id"] not in known]
                    inputs.licenses[product.id] = {"id": product.license.id,
                                                   "redistributable": product.license.redistributable}
            if not g.get("time_invariant") or not pending.any():
                break                                     # versioned: one product; terrain: next only if needed
        if not products_used:
            inputs.missing.extend(p for p in g["products"] if p not in inputs.missing)
        meta = {"role": "static", "group": gname, "products": list(g["products"]), "lag_days": None,
                "time_invariant": bool(g.get("time_invariant"))}
        for out_name, a in out_vars.items():
            spec = None
            for pid in g["products"]:
                try:
                    spec = load_dataset(pid).variable(a) or spec
                except KeyError:
                    pass
            categorical = bool(spec is not None and spec.categorical)
            feats.add(out_name, _int_col(values[a]) if categorical else values[a],
                      {**meta, "source": a, "unit": spec.unit if spec else None, "column": "value"})
        if g.get("derive") == "scott-burgan" and "fuel_model" in values:
            for name, arr in scott_burgan(values["fuel_model"]).items():
                feats.add(name, _int_col(arr) if name == "burnable" else arr,
                          {**meta, "source": "fuel_model", "derived": "Scott and Burgan (2005) Table 7",
                           "unit": None if name == "burnable" else ("m" if name == "fuel_bed_depth" else
                                                                    "%" if name == "moisture_of_extinction"
                                                                    else "kg/m2"), "column": "value"})
        feats.add(f"{gname}_release", rel_col, {**meta, "column": "release"})
        feats.add(f"{gname}_source", src_col, {**meta, "column": "source"})
        feats.record(gname, pending, reason)
    timings["statics"] = round(time.perf_counter() - t0, 2)


def scott_burgan(codes: np.ndarray) -> dict[str, np.ndarray]:
    """Fuel loads (kg/m2), depth (m), moisture of extinction (%) and burnable (1/0) from FBFM40 codes (NaN =
    nodata) with the connector's parameter table (``config/tables/fbfm40_parameters.csv``)."""
    from fuellake.connectors.landfire_fuel_load import DEFAULT_TABLE, FLAG_NODATA, map_codes, parse_parameter_table
    from fuellake.registry import config_dir

    try:
        rel = load_dataset("landfire-fuel-load").options.get("parameter_table", DEFAULT_TABLE)
    except KeyError:
        rel = DEFAULT_TABLE
    path = config_dir() / rel
    if not path.is_file():
        from fuellake.config import default_config_dir

        path = default_config_dir() / DEFAULT_TABLE
    table = parse_parameter_table(path.read_text(encoding="utf-8"))
    c = np.asarray(codes, dtype="float64")
    ints = np.where(np.isfinite(c), c, -9999).astype("int64")
    layers, _ = map_codes(ints, table, -9999)
    out = {k: np.asarray(v, dtype="float64") for k, v in layers.items() if k != "burnable"}
    burn = np.asarray(layers["burnable"], dtype="float64")
    out["burnable"] = np.where(burn == FLAG_NODATA, np.nan, burn)
    return out


# ---------------------------------------------------------------------------- Sentinel-2
def select_s2_sample(ps: PointSet, max_points: int | None, seed: int, large_fire_ha: float) -> np.ndarray:
    """Boolean mask over point-dates (module docstring, "Sentinel-2")."""
    n = len(ps.pd_day)
    if max_points is None or max_points >= n:
        return np.ones(n, dtype=bool)
    rows = ps.rows
    first = pd.DataFrame({"pd": ps.row_pd, "type": rows["point_type"].to_numpy(),
                          "size": pd.to_numeric(rows.get("fire_size_ha", pd.Series([np.nan] * len(rows))),
                                                errors="coerce").to_numpy(),
                          "cls": rows.get("size_class", pd.Series([None] * len(rows))).to_numpy()})
    first = first.drop_duplicates("pd").set_index("pd").sort_index()
    large = (first["type"] == "fire_ignition") & (first["size"] >= large_fire_ha)
    chosen = np.zeros(n, dtype=bool)
    chosen[first.index[large.to_numpy()]] = True
    budget = max(0, int(max_points) - int(chosen.sum()))
    rest = first[~large.to_numpy()]
    if budget == 0 or rest.empty:
        return chosen
    years = np.array([ps.pd_date[i].year for i in rest.index])
    strata = [f"{t}|{y}|{c if isinstance(c, str) else '-'}" for t, y, c in zip(rest["type"], years, rest["cls"],
                                                                             strict=True)]
    sdf = pd.DataFrame({"pd": rest.index, "stratum": strata})
    sizes = sdf.groupby("stratum").size()
    quota = sizes / sizes.sum() * min(budget, len(sdf))
    alloc = np.floor(quota).astype(int)
    left = min(budget, len(sdf)) - int(alloc.sum())
    for s in (quota - alloc).sort_values(ascending=False, kind="stable").index[:left]:
        alloc[s] += 1
    rng = np.random.default_rng(seed)
    for s, k in alloc.items():
        members = sdf.loc[sdf["stratum"] == s, "pd"].to_numpy()
        if k > 0:
            chosen[rng.choice(members, size=min(k, len(members)), replace=False)] = True
    return chosen


def sentinel2_features(ps: PointSet, feats: Features, inputs: Inputs, opts: dict, causal: bool, window_days: int,
                       max_points: int | None, seed: int, large_fire_ha: float, cache, sampler,
                       timings: dict) -> dict:
    t0 = time.perf_counter()
    n = len(ps.pd_day)
    try:
        s2cfg = load_dataset(opts.get("config_dataset", "sentinel2-indices"))
    except KeyError:
        s2cfg = None
    o = s2cfg.options if s2cfg else {}
    if sampler is None:
        from fuellake.features._s2points import S2PointSampler

        sampler = S2PointSampler(api=o.get("stac_api", "https://earth-search.aws.element84.com/v1"),
                                 collections=opts.get("collections"), window_days=window_days, causal=causal,
                                 mask_classes=o.get("scl_mask_classes", (0, 1, 2, 3, 6, 8, 9, 10, 11)),
                                 footprint_m=float(opts.get("footprint_m", 30)),
                                 max_workers=int(opts.get("max_workers", 16)),
                                 search_bin_days=int(opts.get("search_bin_days", 8)), cache=cache)
    chosen = select_s2_sample(ps, max_points, seed, large_fire_ha)
    ndvi, ndmi, dt = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    scene = np.full(n, None, dtype=object)
    status = np.full(n, "not_sampled", dtype=object)
    idx = np.flatnonzero(chosen)
    if len(idx):
        res = sampler.sample(ps.pd_lon[idx], ps.pd_lat[idx], [ps.pd_date[i] for i in idx])
        ndvi[idx], ndmi[idx], dt[idx] = res["ndvi"], res["ndmi"], res["dt_days"]
        scene[idx], status[idx] = res["scene"], res["status"]
    meta = {"role": "sentinel2", "dataset": "sentinel-2-l2a (Earth Search)", "window_days": window_days,
            "lag_days": None}
    feats.add("s2_ndvi", ndvi, {**meta, "unit": "1", "column": "value"})
    feats.add("s2_ndmi", ndmi, {**meta, "unit": "1", "column": "value"})
    feats.add("s2_dt_days", _int_col(dt), {**meta, "column": "dt_days"})
    feats.add("s2_scene", scene, {**meta, "column": "scene"})
    feats.add("s2_status", status, {**meta, "column": "status"})
    feats.record("sentinel2", status != "ok", status)
    if s2cfg is not None and len(idx):
        inputs.licenses[s2cfg.id] = {"id": s2cfg.license.id, "redistributable": s2cfg.license.redistributable}
    stats = sampler.stats.to_dict() if hasattr(sampler.stats, "to_dict") else dict(sampler.stats)
    stats["selected_point_dates"] = int(len(idx))
    stats["max_workers"] = int(opts.get("max_workers", 16))
    timings["sentinel2"] = round(time.perf_counter() - t0, 2)
    return stats


# ---------------------------------------------------------------------------- output
def _write(lake: Lake, rel: str, df: pd.DataFrame) -> None:
    """GeoParquet (WKB point geometry from lon/lat, EPSG:4326, zstd) keeping the column order."""
    gdf = to_geodataframe(df)
    buf = io.BytesIO()
    gdf.to_parquet(buf, index=False, compression="zstd", schema_version="1.1.0", coerce_timestamps="us",
                   allow_truncated_timestamps=True)
    lake.write_bytes(rel, buf.getvalue())


def fill_rates(df: pd.DataFrame, columns: list[str]) -> dict[str, float]:
    n = len(df)
    return {c: (round(float(df[c].notna().sum()) / n, 4) if n else 0.0) for c in columns}


def build_point_features(region_or_aoi: str, start, end, *, tables=("fire_ignitions", "lfmc_samples"),
                         lags_days=(0, 15, 30, 45, 60, 90), causal: bool = True, lake: Lake | None = None,
                         window_days: int | None = None, lag_variables: tuple[str, ...] | None = None,
                         datasets: list[str] | None = None, maturity: str | None = None,
                         allow_mixed_maturity: bool = False, statics: bool = True, remote: bool = True,
                         sentinel2: bool = True, s2_window_days: int | None = None,
                         s2_max_points: int | None = None, seed: int = 0, large_fire_ha: float | None = None,
                         cache=None, s2_sampler=None, remote_factory: RemoteFactory | None = None) -> str:
    """Build the point-feature table; return ``features/<aoi>/point_features.parquet`` (rules: module docstring).

    ``datasets`` restricts the form B and dynamic inputs; ``statics=False`` / ``remote=False`` /
    ``sentinel2=False`` switch those groups off (their columns are then absent / unread); ``cache`` is a
    :class:`~fuellake.features._point_sources.PointCache` (default: in ``$FUELLAKE_CACHE_DIR``);
    ``s2_sampler`` and ``remote_factory`` replace the network readers (tests)."""
    t_start = time.perf_counter()
    lake = lake or Lake.from_env()
    start, end = as_date(start), as_date(end)
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    cfg = point_config()
    opts = _options(cfg)
    window_days = int(opts.get("window_days", 12) if window_days is None else window_days)
    if window_days < 0:
        raise ValueError("window_days must be >= 0")
    s2_opts = {**DEFAULT_OPTIONS["sentinel2"], **(opts.get("sentinel2") or {})}
    s2_window = int(s2_opts.get("window_days", 8) if s2_window_days is None else s2_window_days)
    if s2_window < 0:
        raise ValueError("s2_window_days must be >= 0")
    if s2_max_points is not None and s2_max_points < 0:
        raise ValueError("s2_max_points must be >= 0")
    lags = sorted({int(x) for x in lags_days})
    if any(x < 0 for x in lags):
        raise ValueError("lags_days must be >= 0 (days before the point date)")
    large_fire_ha = float(opts.get("large_fire_ha", 400.0) if large_fire_ha is None else large_fire_ha)
    lag_variables = config_lag_variables() if lag_variables is None else tuple(lag_variables)
    tables = tuple(dict.fromkeys(tables))
    if not tables:
        raise ValueError("tables must name at least one point table")
    aoi = resolve_aoi(region_or_aoi)

    notes: list[str] = []
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    ps = read_points(lake, aoi, start, end, tables, opts, large_fire_ha, notes)
    timings["rows"] = round(time.perf_counter() - t0, 2)
    n_pd = len(ps.pd_day)
    feats = Features(n_pd)
    inputs = Inputs(aoi=aoi)
    inputs.licenses.update(ps.licenses)
    inputs.notes = notes
    sme2_opts = {**DEFAULT_OPTIONS["sme2"], **(opts.get("sme2") or {})}
    groups = list(opts.get("statics") or [])
    static_products = {p for g in groups for p in g["products"]}
    if cache is None and n_pd and (remote or sentinel2):
        from fuellake.features._point_sources import PointCache

        cache = PointCache()
    lag_set = set(lag_variables)

    if n_pd:
        weather_features(lake, ps, feats, inputs, lags, lag_set, datasets, {sme2_opts["climatology_dataset"]},
                         timings)
        dyn = dynamic_features(lake, aoi, ps, feats, inputs, lags, lag_set, window_days, causal, datasets,
                               static_products, allow_mixed_maturity, maturity, timings)
        sme2_anomaly(lake, aoi, ps, feats, inputs, sme2_opts,
                     dyn.get(f"{sme2_opts['dataset']}:{sme2_opts['variable']}"), timings)
    readers = RemoteReaders(cache, opts.get("remote") or {}, remote_factory) if remote else None
    if statics and n_pd:
        static_features(lake, ps, feats, inputs, groups, causal, readers, timings)
    s2_stats: dict = {}
    if sentinel2 and n_pd:
        s2_stats = sentinel2_features(ps, feats, inputs, s2_opts, causal, s2_window, s2_max_points, seed,
                                      large_fire_ha, cache, s2_sampler, timings)

    extra: dict[str, Any] = {}
    for name, vals in feats.cols.items():
        arr = vals if isinstance(vals, pd.api.extensions.ExtensionArray) else np.asarray(vals)
        extra[name] = arr[ps.row_pd] if len(ps.rows) else arr[:0]
    extra["missing_inputs"] = pd.array([";".join(feats.missing[j]) for j in ps.row_pd], dtype="string")
    extra["n_missing_inputs"] = pd.array([len(feats.missing[j]) for j in ps.row_pd], dtype="int32")
    out = pd.concat([ps.rows, pd.DataFrame(extra, index=ps.rows.index)], axis=1)

    rel = points_path(aoi.id)
    _write(lake, rel, out)
    feature_cols = list(feats.cols)
    by_type = {t: fill_rates(out[out["point_type"] == t], feature_cols) for t in sorted(out["point_type"].unique())}
    timings["total"] = round(time.perf_counter() - t_start, 2)
    remote_stats = readers.stats() if readers is not None else {}
    props = {
        "fuellake:n_rows": int(len(out)),
        "fuellake:n_point_dates": int(n_pd),
        "fuellake:n_locations": int(len(ps.loc_lon)),
        "fuellake:point_tables": list(tables),
        "fuellake:rows_by_table": ps.by_table,
        "fuellake:missing_tables": ps.missing_tables,
        "fuellake:point_items": ps.item_ids,
        "fuellake:causal": bool(causal),
        "fuellake:lags_days": lags,
        "fuellake:window_days": window_days,
        "fuellake:lag_variables": list(lag_variables),
        "fuellake:large_fire_ha": large_fire_ha,
        "fuellake:block": {"crs": "EPSG:6933", "cells": BLOCK_CELLS, "cell_m": EASE2_200M_CELL,
                           "size_m": BLOCK_SIZE_M, "origin": [EASE2_UL_X, EASE2_UL_Y],
                           "n_blocks": int(out["block_50km"].nunique()) if len(out) else 0},
        "fuellake:sentinel2": {"enabled": bool(sentinel2), "window_days": s2_window, "max_points": s2_max_points,
                               "seed": seed, "collections": list(s2_opts.get("collections") or [])},
        "fuellake:s2_stats": s2_stats,
        "fuellake:statics": {"enabled": bool(statics), "remote": bool(remote), "groups": groups},
        "fuellake:remote_sources": sorted(remote_stats),
        "fuellake:remote_stats": remote_stats,
        "fuellake:sme2": sme2_opts,
        "fuellake:columns": feats.meta,
        "fuellake:fill_rates": fill_rates(out, feature_cols),
        "fuellake:fill_rates_by_point_type": by_type,
        "fuellake:timings_s": timings,
        "fuellake:id_columns": COMMON_COLUMNS,
    }
    asset = {"key": "table", "path": rel, "type": PARQUET_MEDIA_TYPE, "roles": ["data"],
             "title": f"point features {aoi.id}"}
    inputs.missing = sorted(set(inputs.missing))
    register_gold_item(lake, cfg, aoi, start, end, asset, props, inputs, new_run_id(), extra_source_ids=ps.item_ids)
    return rel


__all__ = ["build_point_features", "block_ids", "points_path", "select_s2_sample", "choose_release",
           "read_points", "resolve_aoi", "point_config", "BUILTIN_CONFIG", "FeatureVar"]
