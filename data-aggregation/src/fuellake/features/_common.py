# fuel-lake — feature-layer internals: input discovery, variable roles, mosaics, sampling and gold items
# Licensed under Apache-2.0.
"""Shared by :mod:`fuellake.features.cube` and :mod:`fuellake.features.matchups`, so both builders apply the
same rules to the same cataloged inputs.

Rules
-----
Inputs
    Found only through :func:`fuellake.catalog.search` with ``aoi=<aoi id>``. Every registered ``raster-a`` /
    ``raster-b`` dataset (except the gold collections and the ``interface`` collections, i.e. the published
    outputs of the team's systems, which are used only when named in ``datasets``) is a candidate. A dataset
    without items is recorded in ``missing`` and never stops a build.
Roles
    *static*: a form A dataset whose ``cadence`` starts with ``static`` or whose id is in
    ``static_datasets`` (option of the ``features-cube`` config; default ``cop-dem``, ``landfire-fbfm40``,
    ``biomass``). It is searched without a time filter, and the items of its latest date on or before
    ``end`` are used (or its earliest date if all are later). Static layers are treated as time-invariant.
    *dynamic*: every other form A dataset, searched over the period.
    *weather*: form B datasets (native-grid Zarr), searched over the period.
Scene
    One dataset on one **UTC calendar date** of the item ``datetime``. Several items on one date (frames,
    tiles) are mosaicked per pixel with :func:`aggregate`. Caveat: a NISAR ascending pass at about
    01-02 UTC is dated the day after the local evening (at most one day of offset).
Maturity
    The items of each dataset must share one ``fuellake:maturity``:
    :func:`fuellake.contracts.check_single_maturity` raises ``MixedMaturityError`` unless
    ``allow_mixed_maturity``. ``maturity="VALIDATED"`` (etc.) keeps only items of that maturity (items
    without the property, i.e. non-NISAR data, are kept).
Series
    A dataset can be split into separate series by an item property (``split_by`` option, default
    ``{biomass: fuellake:biomass_component}``), so live and dead biomass are never mosaicked together.
Names
    A variable is named after its asset key (form A) or Zarr variable (form B), with the suffix ``_<value>``
    for a split series (``biomass_live``). If two datasets provide the same name, both get the prefix
    ``<dataset id, '-' -> '_'>__``.
Aggregation (mosaics, window means, coarsening)
    ``mean``: arithmetic mean of the valid values. ``db`` (unit ``dB``): mean in linear power, back to dB.
    ``circular`` (``aspect``, wind direction ``th``): circular mean in degrees [0, 360); weather directions
    are also interpolated through sin/cos. ``categorical`` (integer dtype in the
    dataset config): first valid value in (datetime, id) order for mosaics, the centre pixel for window
    samples, ``mode`` for coarsening. When all valid values are equal, the result is exactly that value.
    Categorical data is converted to float with NaN for its nodata value.
Weather (form B)
    A day's field is the mean of the store's time steps on that UTC day (NaN if there is none). The cube
    regrids it to the cube grid with bilinear resampling. The matchups sample it with bilinear interpolation
    at the site in the store's native CRS. When several items cover one day, their results are averaged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from fuellake import catalog, events
from fuellake.catalog import asset_rel, to_utc
from fuellake.contracts import CUBE_DATASET, check_single_maturity
from fuellake.grid import Grid
from fuellake.rasters import COG_MEDIA_TYPE, ZARR_MEDIA_TYPE, open_zarr, read_cog, reproject_array
from fuellake.registry import AOI, GOLD_DATASETS, DatasetConfig, list_datasets, load_dataset
from fuellake.storage import Lake

MATCHUPS_DATASET = "matchups"
GOLD_IDS = GOLD_DATASETS                      # cube, matchups, point-features; see tests/test_registry.py
DEFAULT_STATIC_DATASETS = ("cop-dem", "landfire-fbfm40", "biomass")
DEFAULT_SPLIT_BY = {"biomass": "fuellake:biomass_component"}
CIRCULAR_VARIABLES = ("aspect", "th", "wind_direction")   # degrees; "th" = gridMET wind direction
EPOCH = date(1970, 1, 1)

_GOLD_LICENSE = {
    "id": "various",
    "url": None,
    "redistributable": False,
    "attribution": "Derived from the input datasets listed in the item property fuellake:source_ids; "
                   "each input keeps its own licence (fuellake:input_licenses).",
    "notes": "Derived product. Redistributable only if every input is: see the item property "
             "fuellake:redistributable.",
}

# Built-in configs of the gold collections, used when the config directory has no YAML file with the same
# id. The repository ships identical copies as config/datasets/features-cube.yaml and matchups.yaml
# (tests check that they match).
GOLD_CONFIGS: dict[str, dict[str, Any]] = {
    CUBE_DATASET: {
        "id": CUBE_DATASET,
        "title": "fuel-lake gold datacube (AOI grid; time, y, x)",
        "kind": "raster-b",
        "connector": None,
        "status": "unverified",
        "scales": ["local", "region"],
        "provider": "fuel-lake feature layer (fuellake.features.cube), derived from cataloged staged items",
        "homepage": None,
        "access": {"method": "lake: features/<aoi>/cube.zarr (Zarr v3)", "auth": "none", "env": []},
        "license": _GOLD_LICENSE,
        "cadence": "on build (fuellake build-features)",
        "native_resolution": "AOI grid (30 m for smm) or a coarser requested resolution",
        "variables": [],
        "caveats": [
            "Variables depend on what is staged: see the item properties fuellake:variables and "
            "fuellake:missing_datasets.",
            "Dynamic variables are (time, y, x) with time = union of their UTC acquisition dates; NaN where a "
            "variable has no scene that day. Static variables are (y, x).",
            "Weather (form B) is regridded bilinearly to the cube grid and sampled on the cube dates (daily "
            "mean of the store's time steps on that UTC day, NaN if missing).",
            "NISAR series never mix maturities unless built with allow_mixed_maturity=True.",
            "Tested on synthetic lakes only (offline tests).",
        ],
        "options": {
            "static_datasets": list(DEFAULT_STATIC_DATASETS),
            "split_by": dict(DEFAULT_SPLIT_BY),
            "time_chunk": 8,
            "space_chunk": 256,
            "weather_cadence_days": 1,
        },
    },
    MATCHUPS_DATASET: {
        "id": MATCHUPS_DATASET,
        "title": "fuel-lake gold label x feature matchup table (LFMC samples x raster features)",
        "kind": "table",
        "table": "matchups",
        "connector": None,
        "status": "unverified",
        "scales": ["local", "region"],
        "provider": "fuel-lake feature layer (fuellake.features.matchups), derived from cataloged items",
        "homepage": None,
        "access": {"method": "lake: features/<aoi>/matchups.parquet (GeoParquet)", "auth": "none", "env": []},
        "license": _GOLD_LICENSE,
        "cadence": "on build (fuellake build-features)",
        "native_resolution": "point (LFMC sampling site), features sampled on the AOI grid",
        "variables": [],
        "caveats": [
            "One row per lfmc_samples row inside the AOI bbox (no buffer) and period.",
            "Rows are not independent (sites, species, dates): split by site_id / spatial blocks / time.",
            "Build parameters (window_days, lags_days, causal, sample_window_px, nearest_valid) are item "
            "properties.",
            "Tested on synthetic lakes only (offline tests).",
        ],
        "options": {},
    },
}


# ---------------------------------------------------------------------------- configs
def gold_dataset(dataset_id: str) -> DatasetConfig:
    """Config of a gold collection: ``config/datasets/<id>.yaml`` if present, else the built-in default."""
    try:
        return load_dataset(dataset_id)
    except KeyError:
        return DatasetConfig(**GOLD_CONFIGS[dataset_id])


def static_dataset_ids(cfg: DatasetConfig | None = None) -> tuple[str, ...]:
    cfg = cfg or gold_dataset(CUBE_DATASET)
    return tuple(cfg.options.get("static_datasets", DEFAULT_STATIC_DATASETS))


def split_rules(cfg: DatasetConfig | None = None) -> dict[str, str]:
    cfg = cfg or gold_dataset(CUBE_DATASET)
    return dict(cfg.options.get("split_by", DEFAULT_SPLIT_BY))


def is_static(dataset: DatasetConfig, static_ids: tuple[str, ...] | list[str]) -> bool:
    if dataset.kind != "raster-a":
        return False
    return dataset.id in static_ids or dataset.is_static


def as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return to_utc(value).date()
    if isinstance(value, date):
        return value
    return to_utc(value).date()


def day_number(d: date) -> int:
    """Days since 1970-01-01."""
    return (d - EPOCH).days


def item_day(item: dict) -> date:
    """UTC calendar date of an item's ``datetime``."""
    return to_utc(item["properties"]["datetime"]).date()


# ---------------------------------------------------------------------------- variables and inputs
@dataclass(frozen=True)
class FeatureVar:
    name: str                 # output name (cube variable / matchup column stem)
    dataset: str
    source: str               # asset key (form A) or Zarr variable (form B)
    role: str                 # dynamic | static | weather
    series: str = ""          # key into Inputs.scenes / Inputs.items: dataset id, or "<dataset>:<split value>"
    unit: str | None = None
    categorical: bool = False
    nodata: Any = None
    source_dtype: str = "float32"
    description: str | None = None

    @property
    def agg(self) -> str:
        if self.categorical:
            return "categorical"
        if self.source.lower() in CIRCULAR_VARIABLES:
            return "circular"
        if (self.unit or "").strip().lower() == "db":
            return "db"
        return "mean"

    def describe(self) -> dict:
        return {"role": self.role, "dataset": self.dataset, "series": self.series, "source": self.source,
                "unit": self.unit, "categorical": self.categorical, "aggregation": self.agg}


@dataclass
class Scene:
    dataset: str
    day: date
    items: list[dict]         # sorted by (datetime, id)


@dataclass
class Inputs:
    aoi: AOI
    variables: list[FeatureVar] = field(default_factory=list)
    scenes: dict[str, list[Scene]] = field(default_factory=dict)       # dynamic and static datasets
    weather: dict[str, WeatherSeries] = field(default_factory=dict)
    items: dict[str, list[dict]] = field(default_factory=dict)          # items used, per dataset
    missing: list[str] = field(default_factory=list)
    maturity: dict[str, list[str]] = field(default_factory=dict)
    licenses: dict[str, dict] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def by_role(self, role: str) -> list[FeatureVar]:
        return [v for v in self.variables if v.role == role]

    def scene_on(self, dataset: str, day: date) -> Scene | None:
        for s in self.scenes.get(dataset, []):
            if s.day == day:
                return s
        return None

    @property
    def item_ids(self) -> list[str]:
        return sorted({i["id"] for its in self.items.values() for i in its})

    @property
    def redistributable(self) -> bool:
        return all(lic["redistributable"] for lic in self.licenses.values())

    @property
    def single_maturity(self) -> str | None:
        found = {m for ms in self.maturity.values() for m in ms}
        return found.pop() if len(found) == 1 else None


def _band_spec(item: dict, key: str) -> dict:
    bands = item["assets"][key].get("raster:bands") or [{}]
    return bands[0]


def _form_a_keys(items: list[dict]) -> list[str]:
    keys: list[str] = []
    for item in items:
        for key, a in item.get("assets", {}).items():
            if a.get("type") == COG_MEDIA_TYPE and "data" in (a.get("roles") or []) and key not in keys:
                keys.append(key)
    return keys


def _form_a_var(ds: DatasetConfig, key: str, items: list[dict], role: str) -> FeatureVar:
    spec = ds.variable(key)
    if spec is not None:
        return FeatureVar(name=key, dataset=ds.id, source=key, role=role, unit=spec.unit,
                          categorical=spec.categorical, nodata=spec.nodata, source_dtype=spec.dtype,
                          description=spec.description)
    band = next((_band_spec(i, key) for i in items if key in i["assets"]), {})
    dtype = str(band.get("data_type", "float32"))
    nodata = band.get("nodata")
    nodata = float("nan") if nodata == "nan" else nodata
    return FeatureVar(name=key, dataset=ds.id, source=key, role=role, unit=band.get("unit"),
                      categorical=not np.issubdtype(np.dtype(dtype), np.floating), nodata=nodata,
                      source_dtype=dtype)


def _pick_static(items: list[dict], end: date) -> list[dict]:
    days = sorted({item_day(i) for i in items})
    before = [d for d in days if d <= end]
    chosen = before[-1] if before else days[0]
    return [i for i in items if item_day(i) == chosen]


def _series_value(value: Any) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(value)).strip("_").lower() or "none"


def discover_inputs(lake: Lake, aoi: AOI, start: date, end: date, *, search_start: date | None = None,
                    search_end: date | None = None, datasets: list[str] | tuple[str, ...] | None = None,
                    static_datasets: tuple[str, ...] | list[str] | None = None,
                    split_by: dict[str, str] | None = None,
                    allow_mixed_maturity: bool = False, maturity: str | None = None) -> Inputs:
    """Find the cube / matchup inputs of ``aoi`` through the catalog (see the module docstring).

    ``start``/``end`` is the nominal period (static selection uses ``end``); dynamic and weather items are
    searched over ``[search_start, search_end]`` (default: the period). ``split_by`` maps a form A dataset
    id to an item property whose values are separate series (default ``{"biomass":
    "fuellake:biomass_component"}``: live and dead biomass are never mosaicked together). The variables of
    a split series are named ``<asset>_<value>``.
    """
    s0 = search_start or start
    s1 = search_end or end
    static_ids = tuple(static_datasets) if static_datasets is not None else static_dataset_ids()
    split = dict(split_by) if split_by is not None else split_rules()
    candidates = [d for d in list_datasets() if d.kind in ("raster-a", "raster-b") and d.id not in GOLD_IDS]
    if datasets is None:            # model outputs never feed the cube unless asked for (no feedback loop)
        candidates = [d for d in candidates if not d.is_interface]
    else:
        known = {d.id: d for d in candidates}
        unknown = [i for i in datasets if i not in known]
        if unknown:
            raise KeyError(f"not registered raster datasets: {unknown}")
        candidates = [known[i] for i in dict.fromkeys(datasets)]

    inputs = Inputs(aoi=aoi)
    pending: list[tuple[DatasetConfig, str, list[dict]]] = []
    for ds in candidates:
        static = is_static(ds, static_ids)
        if static:
            items = catalog.search(lake, [ds.id], aoi=aoi.id)
        else:
            items = catalog.search(lake, [ds.id], aoi=aoi.id, start=s0, end=s1)
        if maturity is not None:
            items = [i for i in items if i["properties"].get("fuellake:maturity") in (None, maturity)]
        if not items:
            inputs.missing.append(ds.id)
            continue
        found = check_single_maturity(items, allow_mixed=allow_mixed_maturity)
        if found:
            inputs.maturity[ds.id] = sorted(found)
        role = "static" if static else ("weather" if ds.kind == "raster-b" else "dynamic")
        pending.append((ds, role, items))

    raw_vars: list[FeatureVar] = []
    for ds, role, all_items in pending:
        prop = split.get(ds.id) if role != "weather" else None
        groups: dict[str | None, list[dict]] = {}
        for item in all_items:
            key = _series_value(item["properties"].get(prop)) if prop else None
            groups.setdefault(key, []).append(item)
        if role == "weather":
            items = all_items
            series = WeatherSeries(lake, ds, items)
            names = series.variable_names()
            inputs.notes.extend(series.notes)
            if not names:
                inputs.missing.append(ds.id)
                inputs.notes.append(f"{ds.id}: no declared variable found in its Zarr stores")
                continue
            inputs.weather[ds.id] = series
            inputs.items[ds.id] = items
            for name in names:
                spec = ds.variable(name)
                raw_vars.append(FeatureVar(name=name, dataset=ds.id, source=name, role=role, series=ds.id,
                                           unit=spec.unit if spec else None,
                                           description=spec.description if spec else None))
            inputs.licenses[ds.id] = {"id": ds.license.id, "redistributable": ds.license.redistributable}
            continue
        for value, items in sorted(groups.items(), key=lambda kv: kv[0] or ""):
            series = ds.id if value is None else f"{ds.id}:{value}"
            if role == "static":
                items = _pick_static(items, end)
            keys = _form_a_keys(items)
            declared = [v.name for v in ds.variables]
            keys = [k for k in declared if k in keys] + sorted(k for k in keys if k not in declared)
            if not keys:
                inputs.notes.append(f"{series}: items have no form A data assets")
                continue
            by_day: dict[date, list[dict]] = {}
            for item in items:
                by_day.setdefault(item_day(item), []).append(item)
            inputs.scenes[series] = [
                Scene(series, d, sorted(its, key=lambda i: (to_utc(i["properties"]["datetime"]), i["id"])))
                for d, its in sorted(by_day.items())
            ]
            inputs.items[series] = items
            for k in keys:
                v = _form_a_var(ds, k, items, role)
                raw_vars.append(replace(v, series=series, name=k if value is None else f"{k}_{value}"))
        if any(v.dataset == ds.id for v in raw_vars):
            inputs.licenses[ds.id] = {"id": ds.license.id, "redistributable": ds.license.redistributable}
        else:
            inputs.missing.append(ds.id)

    counts: dict[str, int] = {}
    for v in raw_vars:
        counts[v.name] = counts.get(v.name, 0) + 1
    inputs.variables = [v if counts[v.name] == 1 else replace(v, name=f"{v.dataset.replace('-', '_')}__{v.name}")
                        for v in raw_vars]
    return inputs


# ---------------------------------------------------------------------------- array helpers
def to_float(arr: np.ndarray, var: FeatureVar) -> np.ndarray:
    """float32 copy with NaN for nodata (categorical integer codes become exact floats)."""
    a = np.asarray(arr)
    if np.issubdtype(a.dtype, np.floating):
        out = a.astype("float32", copy=True)
        if var.nodata is not None and not (isinstance(var.nodata, float) and math.isnan(var.nodata)):
            out[out == var.nodata] = np.nan
        return out
    out = a.astype("float32")
    if var.nodata is not None and not (isinstance(var.nodata, float) and math.isnan(var.nodata)):
        out[a == var.nodata] = np.nan
    return out


def aggregate(values: np.ndarray, agg: str, axis: int) -> np.ndarray:
    """Combine valid (finite) values along ``axis`` with the rule ``agg`` (see the module docstring).
    ``categorical`` returns the first valid value. NaN where nothing is valid."""
    v = np.asarray(values, dtype="float64")
    valid = np.isfinite(v)
    n = valid.sum(axis=axis)
    if agg == "categorical":
        first = np.expand_dims(np.argmax(valid, axis=axis), axis)
        out = np.take_along_axis(v, first, axis=axis).squeeze(axis)
        return np.where(n > 0, out, np.nan)
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        if agg == "db":
            lin = np.where(valid, np.power(10.0, v / 10.0), 0.0)
            out = 10.0 * np.log10(lin.sum(axis=axis) / n)
        elif agg == "circular":
            rad = np.deg2rad(np.where(valid, v, 0.0))
            s = np.where(valid, np.sin(rad), 0.0).sum(axis=axis)
            c = np.where(valid, np.cos(rad), 0.0).sum(axis=axis)
            out = np.mod(np.rad2deg(np.arctan2(s, c)), 360.0)
        else:
            out = np.where(valid, v, 0.0).sum(axis=axis) / n
    lo = np.where(valid, v, np.inf).min(axis=axis)
    hi = np.where(valid, v, -np.inf).max(axis=axis)
    out = np.where(lo == hi, lo, out)        # exact when all valid values agree
    return np.where(n > 0, out, np.nan)


def read_scene(lake: Lake, scene: Scene, var: FeatureVar, grid: Grid) -> np.ndarray:
    """Mosaic of ``var`` over the scene's items on the AOI grid: float32, NaN nodata."""
    arrays = []
    for item in scene.items:
        if var.source not in item.get("assets", {}):
            continue
        arr, g = read_cog(lake, asset_rel(item, var.source))
        if not grid.same_as(g.crs, g.transform, g.width, g.height):
            raise ValueError(f"{item['id']}/{var.source}: COG grid differs from the AOI grid (form A contract)")
        arrays.append(to_float(arr, var))
    if not arrays:
        return np.full(grid.shape, np.nan, dtype="float32")
    if len(arrays) == 1:
        return arrays[0]
    return aggregate(np.stack(arrays), var.agg, axis=0).astype("float32")


def warp_array(arr: np.ndarray, src_crs, src_transform, dst: Grid, resampling: str) -> np.ndarray:
    """float32 :func:`fuellake.rasters.reproject_array`. No lock: it is thread-safe, and the cube calls it
    from dask threads."""
    return reproject_array(np.asarray(arr, dtype="float32"), src_crs, src_transform, dst, resampling)


def coarsen(arr: np.ndarray, var: FeatureVar, src: Grid, dst: Grid) -> np.ndarray:
    """Area-weighted downsampling of an AOI-grid array onto ``dst`` (rasterio ``average``; ``mode`` for
    categorical data; dB averaged in linear power; aspect as a circular mean)."""
    def warp(a: np.ndarray, how: str) -> np.ndarray:
        return warp_array(a, src.crs, src.transform, dst, how)

    agg = var.agg
    if agg == "categorical":
        return warp(arr, "mode")
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        if agg == "db":
            return (10.0 * np.log10(warp(np.power(10.0, arr / 10.0), "average"))).astype("float32")
        if agg == "circular":
            rad = np.deg2rad(arr)
            s, c = warp(np.sin(rad), "average"), warp(np.cos(rad), "average")
            return np.mod(np.rad2deg(np.arctan2(s, c)), 360.0).astype("float32")
    return warp(arr, "average")


def sample_pixels(arr: np.ndarray, rows: np.ndarray, cols: np.ndarray, size: int, agg: str) -> np.ndarray:
    """Value per site: centre pixel (``size`` 1 or categorical) or the ``size`` x ``size`` window combined
    with ``agg`` (pixels outside the grid are ignored). NaN when the centre pixel is off the grid."""
    h, w = arr.shape
    n = len(rows)
    out = np.full(n, np.nan)
    centre_ok = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
    if size == 1 or agg == "categorical":
        out[centre_ok] = arr[rows[centre_ok], cols[centre_ok]]
        return out
    half = size // 2
    off = np.arange(-half, half + 1)
    dr, dc = np.meshgrid(off, off, indexing="ij")
    rr = rows[:, None] + dr.ravel()[None, :]
    cc = cols[:, None] + dc.ravel()[None, :]
    inside = (rr >= 0) & (rr < h) & (cc >= 0) & (cc < w)
    vals = np.full(rr.shape, np.nan)
    vals[inside] = arr[rr[inside], cc[inside]]
    out = aggregate(vals, agg, axis=1)
    out[~centre_ok] = np.nan
    return out


def bilinear(arr: np.ndarray, transform, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Bilinear interpolation of a north-up grid at points (same CRS). Invalid neighbours are left out
    and the weights renormalised; NaN outside the grid extent."""
    x = np.asarray(x, dtype="float64")
    y = np.asarray(y, dtype="float64")
    ny, nx = arr.shape
    u = (x - transform.c) / transform.a - 0.5
    v = (y - transform.f) / transform.e - 0.5
    inside = (u >= -0.5) & (u <= nx - 0.5) & (v >= -0.5) & (v <= ny - 0.5)
    c0 = np.floor(u).astype("int64")
    r0 = np.floor(v).astype("int64")
    du, dv = u - c0, v - r0
    total = np.zeros(x.shape)
    weight = np.zeros(x.shape)
    for dr, dc, wgt in ((0, 0, (1 - du) * (1 - dv)), (0, 1, du * (1 - dv)), (1, 0, (1 - du) * dv), (1, 1, du * dv)):
        r, c = r0 + dr, c0 + dc
        ok = inside & (r >= 0) & (r < ny) & (c >= 0) & (c < nx)
        val = np.full(x.shape, np.nan)
        val[ok] = arr[r[ok], c[ok]]
        ok &= np.isfinite(val) & (wgt > 0)
        total[ok] += wgt[ok] * val[ok]
        weight[ok] += wgt[ok]
    with np.errstate(invalid="ignore", divide="ignore"):
        out = total / weight
    return np.where(inside & (weight > 0), out, np.nan)


# ---------------------------------------------------------------------------- weather (form B)
@dataclass
class _Store:
    item_id: str
    data: dict[str, np.ndarray]       # variable -> (time, y, x), north-up
    crs: str
    transform: Any
    days: dict[date, list[int]]       # UTC day -> time indices


class WeatherSeries:
    """Daily access to one form B dataset (monthly Zarr items on the dataset's native grid)."""

    def __init__(self, lake: Lake, dataset: DatasetConfig, items: list[dict]):
        import pandas as pd
        import rioxarray  # noqa: F401  (registers the .rio accessor)
        from affine import Affine

        from fuellake.grid import crs_to_str

        self.dataset = dataset
        self.notes: list[str] = []
        self.stores: list[_Store] = []
        declared = [v.name for v in dataset.variables]
        for item in items:
            key = next((k for k, a in item["assets"].items() if a.get("type") == ZARR_MEDIA_TYPE), None)
            if key is None:
                self.notes.append(f"{item['id']}: no Zarr asset, skipped")
                continue
            ds = open_zarr(lake, asset_rel(item, key))
            names = [n for n in (declared or list(ds.data_vars)) if n in ds.data_vars
                     and ds[n].dims == ("time", "y", "x")]
            crs = ds.rio.crs
            if crs is None:
                xs, ys = ds["x"].values, ds["y"].values
                if xs.min() < -180 or xs.max() > 360 or ys.min() < -90 or ys.max() > 90:
                    raise ValueError(f"{item['id']}: Zarr store has no CRS and its coordinates are not lon/lat")
                crs = "EPSG:4326"
                self.notes.append(f"{item['id']}: no CRS in the store, assumed EPSG:4326")
            ds = ds[names]
            if ds.sizes["y"] > 1 and ds["y"].values[0] < ds["y"].values[-1]:
                ds = ds.isel(y=slice(None, None, -1))
            if ds.sizes["x"] > 1 and ds["x"].values[0] > ds["x"].values[-1]:
                ds = ds.isel(x=slice(None, None, -1))
            xs, ys = ds["x"].values.astype("float64"), ds["y"].values.astype("float64")
            if len(xs) < 2 or len(ys) < 2:
                raise ValueError(f"{item['id']}: need at least 2x2 cells to infer the grid")
            rx, ry = (xs[-1] - xs[0]) / (len(xs) - 1), (ys[0] - ys[-1]) / (len(ys) - 1)
            transform = Affine(rx, 0.0, xs[0] - rx / 2, 0.0, -ry, ys[0] + ry / 2)
            times = pd.DatetimeIndex(ds["time"].values)
            if times.tz is not None:
                times = times.tz_convert("UTC").tz_localize(None)
            days: dict[date, list[int]] = {}
            for i, t in enumerate(times):
                days.setdefault(t.date(), []).append(i)
            data = {n: np.asarray(ds[n].values, dtype="float32") for n in names}
            self.stores.append(_Store(item["id"], data, crs_to_str(crs), transform, days))

    @classmethod
    def merged(cls, dataset: DatasetConfig, parts: list[WeatherSeries]) -> WeatherSeries:
        """One series over the stores of ``parts`` (series of the same dataset, e.g. one cached series per item,
        so a long build loads each monthly store once)."""
        out = cls.__new__(cls)
        out.dataset = dataset
        out.notes = [n for p in parts for n in p.notes]
        out.stores = [st for p in parts for st in p.stores]
        return out

    def variable_names(self) -> list[str]:
        names: list[str] = []
        for st in self.stores:
            names += [n for n in st.data if n not in names]
        return names

    def has_day(self, day: date) -> bool:
        return any(day in st.days for st in self.stores)

    def days(self) -> list[date]:
        return sorted({d for st in self.stores for d in st.days})

    @staticmethod
    def _agg(name: str) -> str:
        return "circular" if name.lower() in CIRCULAR_VARIABLES else "mean"

    def _daily(self, name: str, day: date):
        agg = self._agg(name)
        for st in self.stores:
            if name in st.data and day in st.days:
                steps = st.data[name][st.days[day]]
                yield st, (steps[0] if len(steps) == 1 else aggregate(steps, agg, axis=0)).astype("float32")

    def _interp(self, name: str, a: np.ndarray, fn) -> np.ndarray:
        """Apply a linear interpolator ``fn``; circular variables (directions) through sin/cos."""
        if self._agg(name) != "circular":
            return fn(a)
        rad = np.deg2rad(a)
        with np.errstate(invalid="ignore"):
            return np.mod(np.rad2deg(np.arctan2(fn(np.sin(rad)), fn(np.cos(rad)))), 360.0)

    def grid_slice(self, name: str, day: date, grid: Grid) -> np.ndarray | None:
        """Daily field regridded (bilinear) onto ``grid``; None if no store has that day."""
        arrs = [self._interp(name, a, lambda b, st=st: warp_array(b, st.crs, st.transform, grid, "bilinear"))
                for st, a in self._daily(name, day)]
        if not arrs:
            return None
        return (arrs[0] if len(arrs) == 1 else aggregate(np.stack(arrs), self._agg(name), axis=0)).astype("float32")

    def points(self, name: str, day: date, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        """Daily value at WGS84 points (bilinear in the native CRS); NaN if no store has that day."""
        from pyproj import Transformer

        vals = []
        for st, a in self._daily(name, day):
            px, py = Transformer.from_crs("EPSG:4326", st.crs, always_xy=True).transform(lon, lat)
            px, py = np.asarray(px), np.asarray(py)
            vals.append(self._interp(name, a, lambda b, st=st, px=px, py=py: bilinear(b, st.transform, px, py)))
        if not vals:
            return np.full(len(lon), np.nan)
        return vals[0] if len(vals) == 1 else aggregate(np.stack(vals), self._agg(name), axis=0)


# ---------------------------------------------------------------------------- gold STAC items
def new_run_id() -> str:
    from fuellake.pipeline import new_run_id as _new

    return _new()


def register_gold_item(lake: Lake, cfg: DatasetConfig, aoi: AOI, start: date, end: date, asset: dict,
                       properties: dict, inputs: Inputs, run_id: str, *, extra_source_ids: tuple | list = (),
                       stac_extensions: tuple[str, ...] = ()) -> tuple[dict, str, bool]:
    """Create or replace the single gold item of ``cfg`` for ``aoi`` and emit ``item.created`` /
    ``item.updated``. Other items of the same collection and AOI point at the same (overwritten) output,
    so they are removed. Returns ``(item, item lake path, created)``."""
    from fuellake.pipeline import PIPELINE_VERSION

    catalog.upsert_collection(lake, cfg, bbox=aoi.bbox)
    item_id = f"{cfg.id}__{aoi.id}__{start:%Y%m%d}__{end:%Y%m%d}"
    replaced = []
    for old in list(catalog.iter_items(lake, cfg.id)):
        if old["properties"].get("fuellake:aoi") == aoi.id and old["id"] != item_id:
            catalog.remove_item(lake, cfg.id, old["id"])
            replaced.append(old["id"])
    source_ids = sorted(set(inputs.item_ids) | set(extra_source_ids)) or [f"{cfg.id}:no-inputs"]
    licence_ids = sorted({lic["id"] for lic in inputs.licenses.values()})
    props = {
        "fuellake:aoi": aoi.id,
        "fuellake:dataset": cfg.id,
        "fuellake:license": licence_ids[0] if len(licence_ids) == 1 else cfg.license.id,
        "fuellake:redistributable": bool(inputs.licenses) and inputs.redistributable,
        "fuellake:input_licenses": inputs.licenses,
        "fuellake:status": cfg.status,
        "fuellake:source_ids": source_ids,
        "fuellake:run_id": run_id,
        "fuellake:pipeline_version": PIPELINE_VERSION,
        "fuellake:built_at": catalog.iso(datetime.now(UTC)),
        "fuellake:missing_datasets": sorted(inputs.missing),
        "fuellake:maturity_by_dataset": inputs.maturity,
        "fuellake:notes": inputs.notes,
        "fuellake:replaced_items": replaced,
        **properties,
    }
    if inputs.single_maturity:
        props["fuellake:maturity"] = inputs.single_maturity
    t0 = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    t1 = datetime.combine(end, datetime.min.time(), tzinfo=UTC) + timedelta(days=1) - timedelta(microseconds=1)
    item = catalog.make_item(cfg.id, item_id, t0, props, {asset.pop("key"): asset}, aoi.bbox,
                             start=t0, end=t1, stac_extensions=stac_extensions)
    rel, created = catalog.add_item(lake, item)
    ev = dict(dataset=cfg.id, aoi=aoi.id, collection=cfg.id, item_id=item_id, href=rel, run_id=run_id)
    events.emit(lake, "item.created" if created else "item.updated", **ev)
    if cfg.kind == "table":
        events.emit(lake, "table.updated", table=cfg.table, n_rows=props.get("fuellake:n_rows"), **ev)
    return item, rel, created
