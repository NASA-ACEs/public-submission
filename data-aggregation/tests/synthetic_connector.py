# fuel-lake — synthetic offline connector used by the pipeline tests (not part of the package)
# Licensed under Apache-2.0.
"""``SyntheticConnector`` produces form A rasters, form B Zarr months or ``lfmc_samples`` table parts
without any network.

Options (dataset YAML ``options``):
  n_units:    number of daily units discovered from ``period.start`` (default 1)
  fail_keys:  unit keys whose transform raises (to test error isolation)
  bad_crs:    write the COG on a grid with the wrong CRS (contract violation)
  skip_mask:  leave out-of-range pixels unmasked (contract violation)
  error_text: extra text added to the failure message (to test redaction)
  cached_empty_keys: unit keys the connector's "cache" marks as staging nothing (``cached_empty``)

Credentials: when the dataset's ``access.auth`` is ``earthdata`` or ``api_key``, ``extract`` fails unless every
``access.env`` variable is set (like the real connectors); ``missing_credentials`` is the base default.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import numpy as np
import pandas as pd

from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.grid import Grid
from fuellake.rasters import mask_out_of_range, write_cog, write_zarr
from fuellake.registry import AOI
from fuellake.tables import lfmc_sample_id, write_geoparquet


class SyntheticConnector(BaseConnector):
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        n = int(self.dataset.options.get("n_units", 1))
        refs = []
        for i in range(n):
            day = period.start + timedelta(days=i)
            if day > period.end:
                break
            dt = datetime.combine(day, time(18, 30), tzinfo=UTC)
            refs.append(self.ref(f"{self.dataset.id}-{day:%Y%m%d}", dt,
                                 urls=[f"synthetic://{self.dataset.id}/{day:%Y%m%d}"], index=i))
        return refs

    def cached_empty(self, ref: SourceRef, aoi: AOI) -> bool:
        return ref.key in self.dataset.options.get("cached_empty_keys", [])

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        if self.dataset.access.auth in ("earthdata", "api_key"):
            for name in self.dataset.access.env:
                if not self.settings.secret(name):
                    raise RuntimeError(f"{name} is not set")
        rel = self.raw_path(ref, f"{ref.key}.json")
        self.lake.write_json(rel, {"key": ref.key, "index": ref.meta.get("index")})
        return [rel]

    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        opts = self.dataset.options
        if ref.key in opts.get("fail_keys", []):
            raise RuntimeError(f"synthetic failure for {ref.key} {opts.get('error_text', '')}".strip())
        assert raw and self.lake.exists(raw[0])
        if self.dataset.kind == "table":
            return [self._table_item(ref, aoi)]
        if self.dataset.kind == "raster-b":
            return [self._zarr_item(ref, aoi)]
        return [self._raster_item(ref, aoi)]

    # ------------------------------------------------------------------ form B
    def _zarr_item(self, ref: SourceRef, aoi: AOI) -> StagedItem:
        import rioxarray  # noqa: F401  (registers the .rio accessor)
        import xarray as xr

        cell = 0.1
        w, s, e, n = aoi.bbox
        xs = np.arange(np.floor(w / cell) * cell - cell, e + 2 * cell, cell)
        ys = np.arange(np.ceil(n / cell) * cell + cell, s - 2 * cell, -cell)
        start = datetime.combine(ref.datetime.date().replace(day=1), time.min, tzinfo=UTC)
        times = pd.date_range(start.replace(tzinfo=None), periods=3, freq="D")
        data = {v.name: (("time", "y", "x"), np.full((len(times), len(ys), len(xs)), 290.0, "float32"))
                for v in self.dataset.variables}
        ds = xr.Dataset(data, coords={"time": times, "y": ys, "x": xs}).rio.write_crs("EPSG:4326")
        iid = self.item_id(aoi, ref)
        path = self.zarr_path(aoi, iid)
        write_zarr(self.lake, path, ds)
        return StagedItem(item_id=iid, datetime=start, properties={}, assets=[self.zarr_asset(path)],
                          bbox=(float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())),
                          start_datetime=start, end_datetime=start + timedelta(days=3))

    # ------------------------------------------------------------------ raster
    def _raster_item(self, ref: SourceRef, aoi: AOI) -> StagedItem:
        opts = self.dataset.options
        grid = aoi.grid
        var = self.dataset.variables[0]
        lo, hi = var.valid_range
        yy, xx = np.mgrid[0 : grid.height, 0 : grid.width]
        arr = (lo + (hi - lo) * (xx + yy) / (grid.width + grid.height)).astype("float32")
        arr[0, 0] = np.nan            # some nodata
        arr[1, 1:4] = hi + 1.0        # out of range
        if not opts.get("skip_mask"):
            arr, frac = mask_out_of_range(arr, var.valid_range)
        else:
            frac = 0.0
        if opts.get("bad_crs"):
            grid = Grid("EPSG:32610", grid.transform, grid.width, grid.height, grid.resolution)
        iid = self.item_id(aoi, ref)
        path = self.cog_path(aoi, iid, var.name)
        write_cog(self.lake, path, arr, grid, np.nan, var.dtype, description=var.name, unit=var.unit)
        return StagedItem(item_id=iid, datetime=ref.datetime,
                          properties={"fuellake:qc_masked_fraction": frac},
                          assets=[self.cog_asset(var.name, path)])

    # ------------------------------------------------------------------ table
    def _table_item(self, ref: SourceRef, aoi: AOI) -> StagedItem:
        w, s, e, n = aoi.bbox
        day = ref.datetime.date()
        rows = []
        for i, species in enumerate(["chamise", "black sage", "ceanothus"]):
            site = f"S{i + 1}"
            rows.append({
                "sample_id": lfmc_sample_id("fems", site, day, species),
                "source": "fems", "site_id": site, "site_name": f"Site {i + 1}",
                "lon": w + (e - w) * (i + 1) / 4, "lat": s + (n - s) * (i + 1) / 4,
                "date": day, "species": species, "fuel_category": "shrub", "lfmc_pct": 80.0 + 10 * i,
                "n_raw_rows": 1, "outlier_flag": False, "qc_flags": "", "status": "Submitted",
                "ingested_at": pd.Timestamp.now(tz="UTC"),
            })
        df = pd.DataFrame(rows)
        df["n_raw_rows"] = df["n_raw_rows"].astype("int32")
        path = self.table_path(ref.key)
        write_geoparquet(self.lake, path, df)
        start = datetime.combine(day, time.min, tzinfo=UTC)
        return StagedItem(item_id=self.item_id(aoi, ref), datetime=ref.datetime, properties={},
                          assets=[self.table_asset(path)],
                          bbox=(float(df.lon.min()), float(df.lat.min()), float(df.lon.max()), float(df.lat.max())),
                          start_datetime=start, end_datetime=start + timedelta(days=1) - timedelta(microseconds=1))
