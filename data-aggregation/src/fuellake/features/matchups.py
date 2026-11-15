# fuel-lake — label x feature matchup table builder (features/<aoi>/matchups.parquet + STAC item)
# Licensed under Apache-2.0.
"""Build ``features/<aoi>/matchups.parquet``: one row per ``lfmc_samples`` row inside the AOI and period,
joined with features sampled at the site.

Features are sampled **from the cataloged staged items** (the same inputs and rules as the cube, see
:mod:`fuellake.features._common`), not from the cube. So lags can reach before ``start``, and window means
use the native AOI grid whatever the cube resolution.

Rules
-----
Labels
    Rows of every cataloged ``lfmc_samples`` table part (items of the datasets with ``table:
    lfmc_samples``), with ``start <= date <= end`` and lon/lat inside the AOI WGS84 bbox, bounds inclusive.
    **No buffer**: the AOI grid is snapped outward, so every in-bbox site has a pixel. Duplicate
    ``sample_id`` rows (overlapping parts) are dropped, keeping the latest ``ingested_at``.
Sampling (``sample_window_px``, default 3)
    Window of ``k`` x ``k`` AOI-grid pixels centred on the pixel that contains the site (3 x 3 = 90 m at
    30 m), combined as the mean of the valid pixels (dB in linear power, aspect circular). Categorical
    layers (``fuel_model``) always use the centre pixel. ``1`` gives point sampling.
Dynamic variables (form A scenes)
    Column ``<var>`` takes the value from the scene nearest to the sample date within ``±window_days``.
    Column ``<var>_dt_days`` is the signed offset ``scene date - sample date`` (negative = before), null if
    there is no match. A tie goes to the earlier scene. With ``nearest_valid`` (default) only scenes with a
    valid value at the site count; otherwise the nearest scene counts even if its value is NaN.
Lags (``lags_days``, for ``lag_variables``)
    ``lag_variables`` defaults to :func:`config_lag_variables`: ``soil_moisture`` (NISAR SME2) and ``swvl1``
    (ERA5-Land), plus every variable flagged ``lag: true`` in a dataset config (e.g. SMAP L4 ``sm_surface``,
    ``sm_rootzone``, ``sm_profile``). Names match the variable (asset / Zarr variable) of any dataset.
    ``fuellake build-features --lag-variables a,b`` replaces the list (``none`` = no lags).
    Columns ``<var>_lag<L>d`` and ``<var>_lag<L>d_dt_days`` hold the nearest scene to
    ``target = sample date - L`` within ``±window_days``. The offset is ``scene date - target``, so the
    realised lag is ``L - dt``. Weather lag variables take the exact day ``sample date - L`` and have no
    ``_dt_days`` column.
Causal (default True)
    Every dynamic feature uses only scenes dated on or before the sample date (lags too). Weather uses the
    sample date itself (daily value). Static layers are treated as time-invariant and are exempt: one item
    date per static series (the latest on or before ``end``, else the earliest) serves every row, recorded in
    the item property ``fuellake:static_items``. For dated drops such as biomass this can be later than a
    sample date.
Weather (form B)
    Column ``<var>`` holds the daily value on the sample date: the mean of the store's time steps on that
    UTC day, interpolated bilinearly at the site in the native CRS. NaN if the day is not staged.
Static variables
    Column ``<var>``: the value at the site. Categorical codes (static or dynamic) are nullable ``Int32``.

Columns
-------
The label columns of ``lfmc_samples`` (``sample_id``, ``source``, ``site_id``, ``site_name``, ``lon``,
``lat``, ``geometry``, ``date``, ``species``, ``fuel_category``, ``lfmc_pct``, ``n_raw_rows``,
``outlier_flag``, ``qc_flags``, ``status``, ``ingested_at``). Then ``grid_x`` / ``grid_y`` (AOI CRS metres)
and ``grid_row`` / ``grid_col`` (AOI grid pixel), for grouped and spatial-block splits. Then the feature
columns above, with variable names as in the cube. A variable with no staged data has no column; the
dataset is listed in the item property ``fuellake:missing_datasets``.

The build is idempotent: it overwrites the file and replaces the AOI's single item in ``matchups``, then
emits ``item.created`` / ``item.updated`` and ``table.updated``.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from fuellake import catalog
from fuellake.features._common import (
    EPOCH,
    MATCHUPS_DATASET,
    as_date,
    day_number,
    discover_inputs,
    gold_dataset,
    new_run_id,
    read_scene,
    register_gold_item,
    sample_pixels,
    split_rules,
    static_dataset_ids,
)
from fuellake.registry import list_datasets, load_aoi
from fuellake.storage import Lake
from fuellake.tables import LFMC_COLUMNS, PARQUET_MEDIA_TYPE, read_parquet, write_geoparquet

LABEL_TABLE = "lfmc_samples"
LABEL_COLUMNS = [c for c in LFMC_COLUMNS if c != "geometry"]
GRID_COLUMNS = ["grid_x", "grid_y", "grid_row", "grid_col"]
_LABEL_DTYPES = {"lon": "float64", "lat": "float64", "lfmc_pct": "float64", "n_raw_rows": "int32",
                 "outlier_flag": "bool"}
EPOCH_ORDINAL = EPOCH.toordinal()
DEFAULT_LAG_VARIABLES = ("soil_moisture", "swvl1")      # NISAR SME2, ERA5-Land: lagged even without the flag


def config_lag_variables() -> tuple[str, ...]:
    """The default lag variables: :data:`DEFAULT_LAG_VARIABLES`, then the variables flagged ``lag: true`` in the
    registered dataset configs (config order, no repeats)."""
    names = list(DEFAULT_LAG_VARIABLES)
    for ds in list_datasets():
        for v in ds.variables:
            if v.lag and v.name not in names:
                names.append(v.name)
    return tuple(names)


def matchups_path(aoi_id: str) -> str:
    return f"features/{aoi_id}/matchups.parquet"


def pick_nearest(values: np.ndarray, scene_days: np.ndarray, target_days: np.ndarray, sample_days: np.ndarray,
                 window_days: int, causal: bool, nearest_valid: bool) -> tuple[np.ndarray, np.ndarray]:
    """Per row, the scene nearest to ``target_days`` within ``±window_days``.

    ``values``: ``(n_rows, n_scenes)``; ``scene_days`` / ``target_days`` / ``sample_days``: day numbers.
    ``causal`` keeps only scenes with ``scene day <= sample day``; ``nearest_valid`` keeps only finite values.
    A tie (same distance before and after) goes to the earlier scene. Returns ``(value, offset)`` as float
    arrays with ``offset = scene day - target day``; both are NaN where nothing matched.
    """
    n = values.shape[0]
    if values.shape[1] == 0 or n == 0:
        return np.full(n, np.nan), np.full(n, np.nan)
    off = scene_days[None, :] - target_days[:, None]
    ok = np.abs(off) <= window_days
    if causal:
        ok &= scene_days[None, :] <= sample_days[:, None]
    if nearest_valid:
        ok &= np.isfinite(values)
    key = np.where(ok, np.abs(off) * 2 + (off > 0), np.iinfo(np.int64).max)
    j = key.argmin(axis=1)
    rows = np.arange(n)
    has = ok[rows, j]
    return np.where(has, values[rows, j], np.nan), np.where(has, off[rows, j].astype("float64"), np.nan)


def weather_at(series, name: str, sample_days: np.ndarray, lon: np.ndarray, lat: np.ndarray,
               lag: int = 0) -> np.ndarray:
    """Daily value of form B variable ``name`` of a :class:`~fuellake.features._common.WeatherSeries` on the
    exact day ``sample day - lag`` at each site (day numbers since 1970-01-01; NaN where that day is not staged).
    Shared by the matchups and the point features (:mod:`fuellake.features.points`)."""
    sample_days = np.asarray(sample_days, dtype="int64")
    lon = np.asarray(lon, dtype="float64")
    lat = np.asarray(lat, dtype="float64")
    vals = np.full(len(sample_days), np.nan)
    targets = sample_days - int(lag)
    for t_day in np.unique(targets):
        idx = targets == t_day
        vals[idx] = series.points(name, date.fromordinal(EPOCH_ORDINAL + int(t_day)), lon[idx], lat[idx])
    return vals


def _int_column(a: np.ndarray) -> pd.array:
    return pd.array([None if math.isnan(v) else int(v) for v in a], dtype="Int32")


def _empty_labels() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype=_LABEL_DTYPES.get(c, "object")) for c in LABEL_COLUMNS})
    df["ingested_at"] = pd.Series(dtype="datetime64[us, UTC]")
    return df


def read_labels(lake: Lake, aoi, start: date, end: date) -> tuple[pd.DataFrame, list[str], dict]:
    """Label rows inside the AOI bbox and period from the cataloged ``lfmc_samples`` parts.
    Returns ``(rows, table item ids, {dataset: licence})``."""
    table_ds = [d for d in list_datasets() if d.kind == "table" and d.table == LABEL_TABLE]
    frames, item_ids, licenses = [], [], {}
    for ds in table_ds:
        for item in catalog.iter_items(lake, ds.id):
            if "table" not in item.get("assets", {}):
                continue
            part = read_parquet(lake, catalog.asset_rel(item, "table"))
            if len(part):
                frames.append(pd.DataFrame(part.drop(columns=["geometry"], errors="ignore")))
            item_ids.append(item["id"])
            licenses[ds.id] = {"id": ds.license.id, "redistributable": ds.license.redistributable}
    if not frames:
        return _empty_labels(), item_ids, licenses
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    w, s, e, n = aoi.bbox
    keep = (df["lon"].between(w, e) & df["lat"].between(s, n) & (df["date"] >= start) & (df["date"] <= end))
    df = df[keep.fillna(False)]
    if "ingested_at" in df.columns:
        df = df.sort_values("ingested_at", kind="stable")
    df = df.drop_duplicates(subset=["sample_id"], keep="last")
    df = df.sort_values(["date", "source", "site_id", "species"], kind="stable").reset_index(drop=True)
    cols = [c for c in LABEL_COLUMNS if c in df.columns] + [c for c in df.columns if c not in LABEL_COLUMNS]
    return df[cols], item_ids, licenses


def build_matchups(aoi_id: str, start: date, end: date, *, lake: Lake | None = None, window_days: int = 6,
                   lags_days: tuple[int, ...] = (0, 15, 30, 45, 60, 90), causal: bool = True,
                   sample_window_px: int = 3, nearest_valid: bool = True,
                   lag_variables: tuple[str, ...] | None = None, datasets: list[str] | None = None,
                   allow_mixed_maturity: bool = False, maturity: str | None = None) -> str:
    """Build the label x feature table; return ``features/<aoi>/matchups.parquet`` (rules: module docstring).

    ``lag_variables=None`` takes the lag variables from the dataset configs (:func:`config_lag_variables`)."""
    from pyproj import Transformer

    lag_variables = config_lag_variables() if lag_variables is None else tuple(lag_variables)

    lake = lake or Lake.from_env()
    start, end = as_date(start), as_date(end)
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    window_days = int(window_days)
    if window_days < 0:
        raise ValueError("window_days must be >= 0")
    lags = sorted({int(lag) for lag in lags_days})
    if any(lag < 0 for lag in lags):
        raise ValueError("lags_days must be >= 0 (days before the sample date)")
    size = int(sample_window_px)
    if size < 1 or size % 2 == 0:
        raise ValueError("sample_window_px must be an odd integer >= 1")

    aoi = load_aoi(aoi_id)
    cfg = gold_dataset(MATCHUPS_DATASET)
    grid = aoi.grid
    labels, label_items, label_licenses = read_labels(lake, aoi, start, end)
    reach = (max(lags) if lags else 0) + window_days
    search_start = start - timedelta(days=reach)
    search_end = end if causal else end + timedelta(days=window_days)
    inputs = discover_inputs(lake, aoi, start, end, search_start=search_start, search_end=search_end,
                             datasets=datasets, static_datasets=static_dataset_ids(), split_by=split_rules(),
                             allow_mixed_maturity=allow_mixed_maturity, maturity=maturity)
    inputs.licenses.update(label_licenses)

    n = len(labels)
    lon = labels["lon"].to_numpy(dtype="float64")
    lat = labels["lat"].to_numpy(dtype="float64")
    gx, gy = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True).transform(lon, lat)
    gx, gy = np.asarray(gx, dtype="float64"), np.asarray(gy, dtype="float64")
    t = grid.transform
    col = np.floor((gx - t.c) / t.a).astype("int64") if n else np.zeros(0, "int64")
    row = np.floor((gy - t.f) / t.e).astype("int64") if n else np.zeros(0, "int64")
    sample_days = np.array([day_number(d) for d in labels["date"]], dtype="int64")

    out = labels.copy()
    out["grid_x"], out["grid_y"] = gx, gy
    out["grid_row"], out["grid_col"] = row.astype("int32"), col.astype("int32")
    feature_cols: dict[str, dict] = {}
    lag_set = set(lag_variables)
    lagged: list[str] = []

    def add(name: str, values, meta: dict) -> None:
        out[name] = values
        feature_cols[name] = meta

    for v in inputs.by_role("dynamic"):
        scenes = inputs.scenes[v.series]
        scene_days = np.array([day_number(s.day) for s in scenes], dtype="int64")
        values = np.full((n, len(scenes)), np.nan)
        if n:
            for k, scene in enumerate(scenes):
                values[:, k] = sample_pixels(read_scene(lake, scene, v, grid), row, col, size, v.agg)
        val, dt = pick_nearest(values, scene_days, sample_days, sample_days, window_days, causal, nearest_valid)
        meta = {**v.describe(), "lag_days": None}
        add(v.name, _int_column(val) if v.categorical else val, {**meta, "column": "value"})
        add(f"{v.name}_dt_days", _int_column(dt), {**meta, "column": "dt_days"})
        if v.source in lag_set or v.name in lag_set:
            lagged.append(v.name)
            for lag in lags:
                val, dt = pick_nearest(values, scene_days, sample_days - lag, sample_days, window_days, causal,
                                       nearest_valid)
                meta = {**v.describe(), "lag_days": lag}
                add(f"{v.name}_lag{lag}d", _int_column(val) if v.categorical else val, {**meta, "column": "value"})
                add(f"{v.name}_lag{lag}d_dt_days", _int_column(dt), {**meta, "column": "dt_days"})

    for v in inputs.by_role("weather"):
        series = inputs.weather[v.dataset]
        cache: dict[int, np.ndarray] = {}

        def on_day_minus(lag: int, series=series, v=v, cache=cache) -> np.ndarray:
            if lag not in cache:
                cache[lag] = weather_at(series, v.source, sample_days, lon, lat, lag)
            return cache[lag].copy()

        add(v.name, on_day_minus(0), {**v.describe(), "lag_days": None, "column": "value"})
        if v.source in lag_set or v.name in lag_set:
            lagged.append(v.name)
            for lag in lags:
                add(f"{v.name}_lag{lag}d", on_day_minus(lag), {**v.describe(), "lag_days": lag, "column": "value"})

    for v in inputs.by_role("static"):
        vals = np.full(n, np.nan)
        if n:
            vals = sample_pixels(read_scene(lake, inputs.scenes[v.series][0], v, grid), row, col, size, v.agg)
        add(v.name, _int_column(vals) if v.categorical else vals, {**v.describe(), "lag_days": None,
                                                                    "column": "value"})

    missing_lag = sorted(lag_set - {v.source for v in inputs.variables} - {v.name for v in inputs.variables})
    if missing_lag:
        inputs.notes.append(f"lag variables without staged data: {missing_lag}")

    rel = matchups_path(aoi.id)
    write_geoparquet(lake, rel, out)
    props = {
        "fuellake:n_rows": int(n),
        "fuellake:label_table": LABEL_TABLE,
        "fuellake:label_items": sorted(label_items),
        "fuellake:label_rows_by_source": {str(k): int(c) for k, c in labels["source"].value_counts().items()},
        "fuellake:window_days": window_days,
        "fuellake:lags_days": lags,
        "fuellake:causal": bool(causal),
        "fuellake:sample_window_px": size,
        "fuellake:nearest_valid": bool(nearest_valid),
        "fuellake:lag_variables": list(lag_variables),
        "fuellake:lagged_variables": lagged,
        "fuellake:search_period": [search_start.isoformat(), search_end.isoformat()],
        "fuellake:buffer_m": 0,
        "fuellake:static_items": {v.name: {"date": inputs.scenes[v.series][0].day.isoformat(),
                                           "items": [i["id"] for i in inputs.scenes[v.series][0].items]}
                                  for v in inputs.by_role("static")},
        "fuellake:variables": {v.name: v.describe() for v in inputs.variables},
        "fuellake:columns": feature_cols,
        "fuellake:id_columns": ["sample_id", "source", "site_id", "date", "species", "lon", "lat", *GRID_COLUMNS],
    }
    asset = {"key": "table", "path": rel, "type": PARQUET_MEDIA_TYPE, "roles": ["data"],
             "title": f"matchups {aoi.id}"}
    register_gold_item(lake, cfg, aoi, start, end, asset, props, inputs, new_run_id(),
                       extra_source_ids=label_items)
    return rel
