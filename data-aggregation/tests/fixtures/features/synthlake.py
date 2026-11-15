# fuel-lake — synthetic lake builder for the feature-layer tests (offline, core helpers only)
# Licensed under Apache-2.0.
"""Write form A COGs, form B Zarr months and ``lfmc_samples`` parts straight into a temporary lake and
register their STAC items with the core helpers (``pipeline.build_stac_item`` + ``catalog.add_item``).

Datasets (test config dir, AOI ``tiny`` = ``conftest.TINY_AOI``):
  nisar-sm   raster-a dynamic, soil_moisture + soil_moisture_uncertainty, maturity PROVISIONAL (is_nisar)
  nisar-bsc  raster-a dynamic, gamma0_hh_db (unit dB)
  s2-test    raster-a dynamic, ndvi, ndmi
  dem-test   raster-a static (cadence: static), elevation, slope, aspect
  fuel-test  raster-a static (cadence: static), fuel_model int16, nodata -9999 (categorical)
  biomass    raster-a static (in static_datasets), split into series by fuellake:biomass_component
  wx-test    raster-b, t2m, swvl1, th (wind direction) (EPSG:4326, 0.01 degree cells)
  lfmc-test  table lfmc_samples
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from pyproj import Transformer

from fuellake import catalog
from fuellake.connectors.base import SourceRef, StagedAsset, StagedItem
from fuellake.grid import Grid
from fuellake.pipeline import build_stac_item
from fuellake.rasters import COG_MEDIA_TYPE, ZARR_MEDIA_TYPE, write_cog, write_zarr
from fuellake.registry import load_aoi, load_dataset
from fuellake.storage import Lake
from fuellake.tables import PARQUET_MEDIA_TYPE, lfmc_sample_id, table_part_path, write_geoparquet

FIXTURES = Path(__file__).resolve().parent
GOLD_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "datasets"   # features-cube, matchups
TINY_AOI = {
    "id": "tiny",
    "name": "Tiny test AOI inside the Santa Monica Mountains",
    "bbox": [-118.80, 34.05, -118.78, 34.07],
    "crs": "EPSG:32611",
    "resolution": 30,
    "nisar": {"track": 13, "frame": 71, "direction": "descending"},
}
LICENSE = {"id": "CC0-1.0", "url": "https://creativecommons.org/publicdomain/zero/1.0/", "redistributable": True,
           "attribution": "synthetic", "notes": "test data"}


def _var(name, unit, dtype="float32", nodata="NaN", valid_range=None, resampling="bilinear"):
    return {"name": name, "unit": unit, "dtype": dtype, "nodata": nodata, "valid_range": valid_range,
            "resampling": resampling}


def _ds(ds_id, kind, variables=(), **extra):
    d = {"id": ds_id, "title": f"Synthetic {ds_id}", "kind": kind, "connector": None, "status": "tested",
         "provider": "tests", "access": {"method": "none", "auth": "none"}, "license": LICENSE,
         "variables": list(variables)}
    d.update(extra)
    return d


DATASETS = {
    "nisar-sm": _ds("nisar-sm", "raster-a", [_var("soil_moisture", "m3/m3", valid_range=[0.0, 0.7]),
                                             _var("soil_moisture_uncertainty", "m3/m3")],
                    maturity="PROVISIONAL", cadence="12 days"),
    "nisar-bsc": _ds("nisar-bsc", "raster-a", [_var("gamma0_hh_db", "dB", resampling="average")],
                     maturity="PROVISIONAL"),
    "s2-test": _ds("s2-test", "raster-a", [_var("ndvi", "1"), _var("ndmi", "1")], cadence="5 days"),
    "dem-test": _ds("dem-test", "raster-a", [_var("elevation", "m"), _var("slope", "degree"),
                                             _var("aspect", "degree", resampling="nearest")], cadence="static"),
    "fuel-test": _ds("fuel-test", "raster-a", [_var("fuel_model", None, dtype="int16", nodata=-9999,
                                                    resampling="nearest")], cadence="static (versioned)"),
    "biomass": _ds("biomass", "raster-a", [_var("biomass", "kg/m2")], cadence="on drop"),
    "wx-test": _ds("wx-test", "raster-b", [_var("t2m", "K"), _var("swvl1", "m3/m3"), _var("th", "degree")],
                   cadence="daily"),
    "lfmc-test": _ds("lfmc-test", "table", table="lfmc_samples"),
}


def write_config(root: Path, *, with_gold_yaml: bool = False) -> Path:
    (root / "aois").mkdir(parents=True, exist_ok=True)
    (root / "datasets").mkdir(exist_ok=True)
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    for ds_id, d in DATASETS.items():
        (root / "datasets" / f"{ds_id}.yaml").write_text(yaml.safe_dump(d))
    if with_gold_yaml:
        for name in ("features-cube.yaml", "matchups.yaml"):
            shutil.copy(GOLD_CONFIG_DIR / name, root / "datasets" / name)
    return root


def at(d: date, hh: int = 18, mm: int = 30) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=UTC)


class SynthLake:
    """Builder bound to a lake and the ``tiny`` AOI (call after the config dir is in place)."""

    def __init__(self, lake: Lake, aoi_id: str = "tiny"):
        self.lake = lake
        self.aoi = load_aoi(aoi_id)
        self.grid: Grid = self.aoi.grid
        self._to_ll = Transformer.from_crs(self.grid.crs, "EPSG:4326", always_xy=True)

    # ------------------------------------------------------------------ geometry
    def pixel_lonlat(self, row: int, col: int) -> tuple[float, float]:
        """WGS84 lon/lat of the centre of AOI-grid pixel (row, col)."""
        t = self.grid.transform
        lon, lat = self._to_ll.transform(t.c + t.a * (col + 0.5), t.f + t.e * (row + 0.5))
        return float(lon), float(lat)

    def field(self, fn: Callable[[np.ndarray, np.ndarray], np.ndarray], dtype="float32") -> np.ndarray:
        rr, cc = np.mgrid[0 : self.grid.height, 0 : self.grid.width]
        return np.asarray(fn(rr, cc)).astype(dtype)

    def const(self, value, dtype="float32") -> np.ndarray:
        return np.full(self.grid.shape, value, dtype=dtype)

    # ------------------------------------------------------------------ registration
    def _register(self, ds_id: str, iid: str, when: datetime, assets: list[StagedAsset], properties: dict,
                  **staged_kw) -> dict:
        ds = load_dataset(ds_id)
        staged = StagedItem(item_id=iid, datetime=when, properties=dict(properties), assets=assets, **staged_kw)
        ref = SourceRef(dataset_id=ds_id, key=iid, datetime=when, urls=(f"synthetic://{iid}",))
        item = build_stac_item(self.lake, ds, self.aoi, ref, staged, run_id="synthetic-run")
        catalog.upsert_collection(self.lake, ds, bbox=self.aoi.bbox)
        catalog.add_item(self.lake, item)
        return item

    def raster(self, ds_id: str, when: datetime | date, arrays: dict[str, np.ndarray], *,
               maturity: str | None = None, discriminator: str | None = None,
               properties: dict | None = None) -> dict:
        """One form A item with one COG per variable in ``arrays``."""
        ds = load_dataset(ds_id)
        when = at(when) if not isinstance(when, datetime) else when
        iid = f"{ds_id}__{self.aoi.id}__{when:%Y%m%d}" + (f"__{discriminator}" if discriminator else "")
        assets = []
        for name, arr in arrays.items():
            var = ds.variable(name)
            path = f"staged/{ds_id}/{self.aoi.id}/{iid}/{name}.tif"
            write_cog(self.lake, path, arr, self.grid, var.nodata, var.dtype, description=name, unit=var.unit)
            assets.append(StagedAsset(key=name, path=path, media_type=COG_MEDIA_TYPE, roles=["data"],
                                      raster_band=var.stac_band()))
        props: dict = {"fuellake:qc_masked_fraction": 0.0, **(properties or {})}
        if maturity:
            props["fuellake:maturity"] = maturity
        return self._register(ds_id, iid, when, assets, props)

    def weather(self, ds_id: str, first_day: date, n_days: int,
                fields: dict[str, Callable[[int, np.ndarray, np.ndarray], np.ndarray]], *, cell: float = 0.01,
                skip_days: tuple[int, ...] = (), steps_per_day: int = 1, south_up: bool = False) -> dict:
        """One form B item: EPSG:4326 grid covering the AOI bbox plus one cell; ``fields[var](k, lon, lat)``
        gives the value on day ``first_day + k`` (``skip_days`` are left out of the store)."""
        import rioxarray  # noqa: F401
        import xarray as xr

        w, s, e, n = self.aoi.bbox
        xs = np.round(np.arange(w - 1.5 * cell, e + 1.5 * cell + 1e-9, cell), 8)
        ys = np.round(np.arange(n + 1.5 * cell, s - 1.5 * cell - 1e-9, -cell), 8)
        if south_up:
            ys = ys[::-1]
        lon, lat = np.meshgrid(xs, ys)
        ks = [k for k in range(n_days) if k not in skip_days]
        times, data = [], {v: [] for v in fields}
        for k in ks:
            for step in range(steps_per_day):
                times.append(pd.Timestamp(first_day) + pd.Timedelta(days=k, hours=24 * step / steps_per_day))
                for v, fn in fields.items():
                    data[v].append(np.asarray(fn(k, lon, lat), dtype="float32"))
        dvars = {v: (("time", "y", "x"), np.stack(a)) for v, a in data.items()}
        xds = xr.Dataset(dvars, coords={"time": pd.DatetimeIndex(times), "y": ys, "x": xs}).rio.write_crs("EPSG:4326")
        iid = f"{ds_id}__{self.aoi.id}__{first_day:%Y%m%d}"
        path = f"staged/{ds_id}/{self.aoi.id}/{iid}/data.zarr"
        write_zarr(self.lake, path, xds)
        t0 = datetime.combine(first_day, time.min, tzinfo=UTC)
        return self._register(ds_id, iid, t0, [StagedAsset("data", path, ZARR_MEDIA_TYPE, ["data"])], {},
                              bbox=(float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())),
                              start_datetime=t0, end_datetime=t0 + timedelta(days=n_days) - timedelta(microseconds=1))

    def labels(self, rows: list[dict], key: str = "part1", ds_id: str = "lfmc-test") -> dict:
        """One ``lfmc_samples`` part; each row needs ``site_id``, ``lon``, ``lat``, ``date`` (rest defaulted)."""
        full = []
        for r in rows:
            d = {"source": "fems", "site_name": f"Site {r['site_id']}", "species": "chamise",
                 "fuel_category": "shrub", "lfmc_pct": 80.0, "n_raw_rows": 1, "outlier_flag": False,
                 "qc_flags": "", "status": "Submitted", "ingested_at": pd.Timestamp("2026-11-14", tz="UTC"), **r}
            d["sample_id"] = lfmc_sample_id(d["source"], d["site_id"], d["date"], d["species"])
            full.append(d)
        df = pd.DataFrame(full)
        df["n_raw_rows"] = df["n_raw_rows"].astype("int32")
        path = table_part_path("lfmc_samples", ds_id, key)
        write_geoparquet(self.lake, path, df)
        day = min(df["date"])
        t0 = datetime.combine(day, time.min, tzinfo=UTC)
        return self._register(ds_id, f"{ds_id}__{self.aoi.id}__{day:%Y%m%d}__{key}", t0,
                              [StagedAsset("table", path, PARQUET_MEDIA_TYPE, ["data"])], {},
                              bbox=(float(df.lon.min()), float(df.lat.min()), float(df.lon.max()),
                                    float(df.lat.max())))
