# fuel-lake — gold datacube builder (features/<aoi>/cube.zarr + STAC item in collection features-cube)
# Licensed under Apache-2.0.
"""Build ``features/<aoi>/cube.zarr`` from cataloged items.

Layout (Zarr v3, CF-1.8 style)
------------------------------
- Coordinates: ``time`` (UTC calendar dates, ``datetime64``; see "Time axis"), ``y`` / ``x`` (pixel centres in
  the AOI CRS, metres, north-up), ``spatial_ref`` (CF grid mapping written by rioxarray, with ``GeoTransform``).
- **Dynamic** variables (form A scene products: soil moisture, backscatter, indices...): ``(time, y, x)``.
  ``time`` is the union of the UTC acquisition dates of all dynamic variables; a variable is NaN on dates
  where it has no scene. Items of one dataset on one date are mosaicked (see :mod:`._common`).
- **Weather** variables (form B, native-grid Zarr): ``(time, y, x)``, regridded to the cube grid with
  bilinear resampling and sampled **on the cube dates**: the value of a date is the mean of the store's time
  steps on that UTC day (one step for daily products), NaN if the store has no step that day. Weather days
  that are not cube dates are not in the cube (the matchups read weather directly on the sample dates).
- **Static** variables (``elevation``, ``slope``, ``aspect``, ``fuel_model``, ``biomass``...): ``(y, x)``.
- Everything is ``float32`` with NaN fill, chunked ``(time_chunk, space_chunk, space_chunk)`` / ``(space_chunk,
  space_chunk)`` (defaults 8 and 256, options of the ``features-cube`` config) and Blosc-zstd compressed.
  Categorical codes (``fuel_model``) are exact floats; attribute ``fuellake:categorical`` is true.
- Variable attributes: ``units``, ``long_name``, ``fuellake:dataset``, ``fuellake:role`` (dynamic, weather or
  static), ``fuellake:aggregation``, ``fuellake:source_items``, ``fuellake:maturity`` (when the items carry
  one), ``fuellake:items_by_date`` and ``fuellake:dates_with_data`` (time-dependent variables).
- Global attributes: ``Conventions``, ``title``, ``history``, ``fuellake:aoi``, ``fuellake:start`` / ``end``,
  ``fuellake:resolution``, ``fuellake:grid``, ``fuellake:missing_datasets``, ``fuellake:notes``, the rules
  in force (``fuellake:rules``) and the ``run_id``.

``resolution`` coarser than the AOI resolution downsamples every form A layer onto ``aoi.grid_at(resolution)``
by area-weighted averaging (``mode`` for categorical data, linear power for dB, circular mean for aspect).
Weather is regridded directly onto that grid. A finer resolution is refused.

Time axis (item property ``fuellake:time_axis``): ``scenes`` = the union of the UTC acquisition dates of the
dynamic variables (weather is sampled on those dates). When no dynamic scene falls in the period (the
statewide AOI ``ca`` has no scene datasets) but weather exists, the axis is ``weather``: every
``weather_cadence_days``-th day from ``start`` to ``end`` (default 1 = daily; option ``weather_cadence_days`` of
the ``features-cube`` config, or the argument) on which at least one weather series has data. With neither,
the cube holds only the static variables with an empty ``time`` axis.

Building with no inputs at all raises ``LookupError``. A NISAR
dataset whose items mix maturities raises ``MixedMaturityError`` unless ``allow_mixed_maturity=True``.

The build is idempotent: it overwrites the store and replaces the AOI's single item in ``features-cube``
(id ``features-cube__<aoi>__<start>__<end>``), then emits ``item.created`` or ``item.updated``.

Region: when the lake has a region, every cube pixel whose centre lies outside it is NaN (weather regridded
from all-touched coastal cells would otherwise reach a little beyond the polygon).
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import numpy as np
from rasterio.crs import CRS

from fuellake import catalog
from fuellake.features._common import (
    CUBE_DATASET,
    FeatureVar,
    Inputs,
    as_date,
    coarsen,
    discover_inputs,
    gold_dataset,
    new_run_id,
    read_scene,
    register_gold_item,
    split_rules,
    static_dataset_ids,
)
from fuellake.grid import Grid
from fuellake.rasters import ZARR_MEDIA_TYPE, write_zarr
from fuellake.registry import AOI, lake_region, load_aoi
from fuellake.storage import Lake

CUBE_RULES = {
    "time": "union of the UTC acquisition dates of the dynamic (form A scene) variables; NaN where a variable "
            "has no scene that day. Without dynamic dates: every weather_cadence_days-th day of the period with "
            "weather data (time_axis 'weather')",
    "mosaic": "items of one dataset on one date: mean of valid pixels (dB in linear power, aspect circular, "
              "categorical first valid)",
    "static": "static datasets: items of the latest date on or before end (else the earliest date); no time axis",
    "weather": "form B regridded bilinearly to the cube grid and sampled on the cube dates: mean of the store's "
               "time steps on that UTC day, NaN if none",
    "coarsening": "resolution > AOI resolution: area-weighted average (mode for categorical, linear power for "
                  "dB, circular mean for aspect)",
    "maturity": "one fuellake:maturity per dataset unless allow_mixed_maturity",
}


def cube_path(aoi_id: str) -> str:
    return f"features/{aoi_id}/cube.zarr"


def cube_grid(aoi: AOI, resolution: float | None) -> tuple[Grid, bool]:
    """``(grid, coarsened)`` for the requested resolution (None = the AOI grid)."""
    if resolution is None or abs(float(resolution) - aoi.resolution) < 1e-9:
        return aoi.grid, False
    if float(resolution) < aoi.resolution:
        raise ValueError(f"cube resolution {resolution} m is finer than the AOI grid ({aoi.resolution} m); "
                         "only coarsening is supported")
    return aoi.grid_at(float(resolution)), True


def _read_group(read_day: Callable[[date], np.ndarray | None], days: list[date],
                shape: tuple[int, int]) -> np.ndarray:
    out = np.full((len(days), *shape), np.nan, dtype="float32")
    for k, d in enumerate(days):
        arr = read_day(d)
        if arr is not None:
            out[k] = arr
    return out


def _time_stack(read_day: Callable[[date], np.ndarray | None], days: list[date], grid: Grid, tc: int, sc: int):
    """Lazy ``(time, y, x)`` array: one delayed read per group of ``tc`` dates, chunked ``(tc, sc, sc)``."""
    import dask
    import dask.array as da

    if not days:
        return np.zeros((0, *grid.shape), dtype="float32")
    blocks = []
    for i in range(0, len(days), tc):
        group = days[i : i + tc]
        fn = functools.partial(_read_group, read_day, group, grid.shape)
        blocks.append(da.from_delayed(dask.delayed(fn, pure=False)(), shape=(len(group), *grid.shape),
                                      dtype="float32"))
    return da.concatenate(blocks, axis=0).rechunk((tc, sc, sc))


def _static_array(read: Callable[[], np.ndarray], grid: Grid, sc: int):
    import dask
    import dask.array as da

    return da.from_delayed(dask.delayed(read, pure=False)(), shape=grid.shape, dtype="float32").rechunk((sc, sc))


def _var_attrs(v: FeatureVar, inputs: Inputs, days: list[date] | None) -> dict:
    attrs: dict = {
        "long_name": v.description or f"{v.source} ({v.dataset})",
        "fuellake:dataset": v.dataset,
        "fuellake:source_variable": v.source,
        "fuellake:series": v.series,
        "fuellake:role": v.role,
        "fuellake:aggregation": v.agg,
        "fuellake:categorical": v.categorical,
        "fuellake:source_dtype": v.source_dtype,
        "fuellake:source_items": sorted(i["id"] for i in inputs.items.get(v.series, [])),
    }
    if v.unit:
        attrs["units"] = v.unit
    if v.categorical and v.nodata is not None:
        attrs["fuellake:source_nodata"] = v.nodata
    if inputs.maturity.get(v.dataset):
        attrs["fuellake:maturity"] = ";".join(inputs.maturity[v.dataset])
    if days is not None and v.role == "dynamic":
        wanted = set(days)
        by_date = {s.day.isoformat(): [i["id"] for i in s.items] for s in inputs.scenes.get(v.series, [])
                   if s.day in wanted}
        attrs["fuellake:items_by_date"] = by_date
        attrs["fuellake:dates_with_data"] = sorted(by_date)
    if days is not None and v.role == "weather":
        series = inputs.weather[v.dataset]
        attrs["fuellake:dates_with_data"] = [d.isoformat() for d in days if series.has_day(d)]
        attrs["fuellake:sampling"] = CUBE_RULES["weather"]
    return attrs


def weather_days(start: date, end: date, cadence: int, has_day: Callable[[date], bool]) -> list[date]:
    """Every ``cadence``-th day from ``start`` to ``end`` (inclusive) for which ``has_day`` is true."""
    if cadence < 1:
        raise ValueError("weather_cadence_days must be >= 1")
    out, d = [], start
    while d <= end:
        if has_day(d):
            out.append(d)
        d += timedelta(days=cadence)
    return out


def build_cube(aoi_id: str, start: date, end: date, *, lake: Lake | None = None,
               resolution: float | None = None, datasets: list[str] | None = None,
               allow_mixed_maturity: bool = False, maturity: str | None = None,
               weather_cadence_days: int | None = None) -> str:
    """Build the gold datacube for ``aoi_id`` over ``[start, end]``; return ``features/<aoi>/cube.zarr``.

    ``datasets`` restricts the inputs to these dataset ids (default: every registered raster dataset).
    ``maturity`` keeps only NISAR items of that maturity; ``allow_mixed_maturity`` accepts a mixed series.
    ``weather_cadence_days`` sets the weather time axis used when there are no dynamic scene dates (module
    docstring; default: the ``features-cube`` option, else 1).
    """
    import pandas as pd
    import rioxarray  # noqa: F401  (registers the .rio accessor)
    import xarray as xr
    from zarr.codecs import BloscCodec

    lake = lake or Lake.from_env()
    start, end = as_date(start), as_date(end)
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    aoi = load_aoi(aoi_id)
    cfg = gold_dataset(CUBE_DATASET)
    tc = max(1, int(cfg.options.get("time_chunk", 8)))
    sc = max(16, int(cfg.options.get("space_chunk", 256)))
    native = aoi.grid
    grid, coarse = cube_grid(aoi, resolution)
    inputs = discover_inputs(lake, aoi, start, end, datasets=datasets, static_datasets=static_dataset_ids(cfg),
                             split_by=split_rules(cfg),
                             allow_mixed_maturity=allow_mixed_maturity, maturity=maturity)
    if not inputs.variables:
        raise LookupError(f"no cube inputs cataloged for AOI {aoi_id} in {start}..{end}; "
                          f"datasets without items: {sorted(inputs.missing)}")

    dynamic, weather, static = (inputs.by_role(r) for r in ("dynamic", "weather", "static"))
    days = sorted({s.day for v in dynamic for s in inputs.scenes[v.series]})
    time_axis = "scenes" if days else "none"
    if weather and not days:
        cadence = int(weather_cadence_days or cfg.options.get("weather_cadence_days", 1))
        series = {v.dataset: inputs.weather[v.dataset] for v in weather}
        days = weather_days(start, end, cadence,
                            lambda d: any(s.has_day(d) for s in series.values()))
        if days:
            time_axis = "weather"
            inputs.notes.append(f"no dynamic scene dates in the period: weather on its own time axis (every "
                                f"{cadence} day(s) with weather data, {len(days)} dates)")
        else:
            inputs.notes.append("weather skipped: no dynamic scene dates and no weather data in the period")
            weather = []

    region = lake_region()
    inside = region.pixel_mask(grid) if region is not None else None

    def clip(arr: np.ndarray | None) -> np.ndarray | None:
        if arr is None or inside is None or inside.all():
            return arr
        return np.where(inside, arr, np.float32(np.nan)).astype("float32", copy=False)

    def read_dynamic(v: FeatureVar, d: date) -> np.ndarray | None:
        scene = inputs.scene_on(v.series, d)
        if scene is None:
            return None
        arr = read_scene(lake, scene, v, native)
        return clip(coarsen(arr, v, native, grid) if coarse else arr)

    def read_weather(v: FeatureVar, d: date) -> np.ndarray | None:
        return clip(inputs.weather[v.dataset].grid_slice(v.source, d, grid))

    def read_static(v: FeatureVar) -> np.ndarray:
        arr = read_scene(lake, inputs.scenes[v.series][0], v, native)
        return clip(coarsen(arr, v, native, grid) if coarse else arr)

    compressors = (BloscCodec(cname="zstd", clevel=5, shuffle="shuffle"),)
    cy, cx = min(sc, grid.height), min(sc, grid.width)
    data_vars = {}
    encoding = {}
    for v in dynamic + weather:
        reader = functools.partial(read_dynamic if v.role == "dynamic" else read_weather, v)
        data_vars[v.name] = xr.Variable(("time", "y", "x"), _time_stack(reader, days, grid, tc, sc),
                                        attrs=_var_attrs(v, inputs, days))
        encoding[v.name] = {"chunks": (max(1, min(tc, len(days))), cy, cx), "compressors": compressors}
    for v in static:
        data_vars[v.name] = xr.Variable(("y", "x"), _static_array(functools.partial(read_static, v), grid, sc),
                                        attrs=_var_attrs(v, inputs, None))
        encoding[v.name] = {"chunks": (cy, cx), "compressors": compressors}

    run_id = new_run_id()
    unit = "m" if CRS.from_user_input(grid.crs).is_projected else "degrees"
    coords = {
        "time": ("time", pd.DatetimeIndex([pd.Timestamp(d) for d in days], dtype="datetime64[ns]"),
                 {"standard_name": "time", "long_name": "acquisition date (UTC calendar day)"}),
        "y": ("y", grid.y_coords(), {"standard_name": "projection_y_coordinate", "long_name": "pixel centre y",
                                     "units": unit, "axis": "Y"}),
        "x": ("x", grid.x_coords(), {"standard_name": "projection_x_coordinate", "long_name": "pixel centre x",
                                     "units": unit, "axis": "X"}),
    }
    ds = xr.Dataset(data_vars, coords=coords)
    ds = ds.rio.write_crs(grid.crs).rio.write_transform(grid.transform)
    built_at = catalog.iso(datetime.now(UTC))
    ds.attrs = {
        "Conventions": "CF-1.8",
        "title": f"fuel-lake gold datacube for AOI {aoi.id} ({aoi.name})",
        "institution": "fuel-lake (NASA Space Apps 2026)",
        "source": "fuel-lake staged items (see fuellake:source_items per variable)",
        "history": f"{built_at} fuellake.features.cube.build_cube run {run_id}",
        "fuellake:aoi": aoi.id,
        "fuellake:start": start.isoformat(),
        "fuellake:end": end.isoformat(),
        "fuellake:resolution": grid.resolution,
        "fuellake:grid": grid.to_dict(),
        "fuellake:missing_datasets": sorted(inputs.missing),
        "fuellake:maturity_by_dataset": inputs.maturity,
        "fuellake:allow_mixed_maturity": allow_mixed_maturity,
        "fuellake:notes": inputs.notes,
        "fuellake:time_axis": time_axis,
        "fuellake:rules": CUBE_RULES,
        "fuellake:run_id": run_id,
    }
    for name, enc in encoding.items():
        ds[name].encoding.update(enc)

    import dask

    rel = cube_path(aoi.id)
    with dask.config.set(scheduler="threads", num_workers=4):
        write_zarr(lake, rel, ds)

    variables = {v.name: {**v.describe(), "dims": list(ds[v.name].dims)} for v in dynamic + weather + static}
    props = {
        "fuellake:dims": {"time": len(days), "y": grid.height, "x": grid.width},
        "fuellake:time_axis": time_axis,
        "fuellake:times": [d.isoformat() for d in days],
        "fuellake:variables": variables,
        "fuellake:resolution": grid.resolution,
        "fuellake:allow_mixed_maturity": allow_mixed_maturity,
        "fuellake:rules": CUBE_RULES,
    }
    asset = {"key": "data", "path": rel, "type": ZARR_MEDIA_TYPE, "roles": ["data"],
             "title": f"gold datacube {aoi.id}", **grid.stac_proj()}
    register_gold_item(lake, cfg, aoi, start, end, asset, props, inputs, run_id,
                       stac_extensions=(catalog.PROJ_EXT,))
    return rel
