# fuel-lake — gridMET daily weather, water balance and NFDRS fuel indices (form B) via NKN THREDDS NCSS
# Licensed under Apache-2.0.
"""gridMET (Abatzoglou 2013) daily surface weather and NFDRS fuel indices, about 4 km (1/24 degree).

Access (verified live 2026-11-14, no key): the Northwest Knowledge Network THREDDS server publishes one
aggregated dataset per variable, ``agg_met_<short>_1979_CurrentYear_CONUS.nc``. The NetCDF Subset Service
(NCSS) returns a bbox/time subset as a small NetCDF file::

    <thredds>/ncss/agg_met_vs_1979_CurrentYear_CONUS.nc?var=daily_mean_wind_speed&north=..&south=..
        &west=..&east=..&time_start=..&time_end=..&accept=netcdf

``<thredds>/ncss/<dataset>/dataset.xml`` gives the time span (``<TimeSpan><end>``) used to find the last
available day. NCSS serves NetCDF-3 only (``accept=netcdf4`` answers HTTP 500) and does not gzip responses, so
the response size is cells x days x 2 bytes (int16-packed variables) or 4 bytes (``fm100``, ``fm1000``).

Units of work: one per calendar month touched by the period (whole months, never cut at ``--start`` /
``--end``). A complete month has key ``gridmet-YYYYMM``. The month that is still filling up has key
``gridmet-YYYYMM-through-YYYYMMDD``, so a later run with newer data reprocesses it and replaces the item (the
item id and Zarr path depend only on AOI and month). Months after the last available day are skipped.

Requests: one NCSS request per variable and request box, at most ``max_workers`` (default 4) at a time, with
``retries`` retries and exponential backoff on HTTP 408/429/5xx and connection errors. A local AOI uses one box
(the AOI bbox plus ``request_buffer_cells``). A region-scale AOI (``ca``) with a lake region uses
``region_request_bands`` latitude bands: each band's box is the bounding box of the region (buffered by
``region_margin_cells`` cells) inside that band, padded by one cell. California fills only about 43 % of its
bbox, so 4 bands request about 64 % of the rectangle's cells. The bands are put back on one regular lattice
(cells placed by integer index, :func:`mosaic_bands`); cells no band requested are NaN. Those cells do not touch
the region, so the region clip would set them to NaN anyway: the staged store is the same as with one box. The
transform checks that every cell touching the region was requested (:func:`check_band_coverage`).

Output (form B): ``staged/gridmet/<aoi>/gridmet__<aoi>__YYYYMM01/data.zarr`` with dims ``(time, y, x)``,
``x``/``y`` = cell-centre longitude/latitude in EPSG:4326, clipped to the AOI bbox plus ``buffer_cells``
native cells. One float32 variable per config variable (asset ``data``), NaN = no data (ocean cells).
Item properties also record ``gridmet:request_boxes``, ``gridmet:download_bytes`` (NetCDF bytes received),
``gridmet:raw_stored_bytes`` and ``gridmet:qc_masked_cells`` (per variable: values outside ``valid_range``).

Raw files: ``raw/gridmet/<yyyy>/<mm>/<aoi>_<var>_<yyyymm>[_b<band>].nc.gz``, the received NetCDF bytes
gzip-compressed (``raw_gzip``, default on; the int16 subsets compress about 3.6x). They are written only after
the axis check; ``extract`` always downloads again (no raw cache), so a failed month is simply fetched anew.

Source naming: config variable ``name`` is the gridMET short name; ``source_name`` is the NetCDF variable
inside the aggregated dataset (both declared in ``config/datasets/gridmet.yaml``).
"""

from __future__ import annotations

import calendar
import gzip
import logging
import re
import threading
import time as _time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode

import numpy as np

from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.rasters import mask_out_of_range, write_zarr
from fuellake.registry import AOI, lake_region

log = logging.getLogger("fuellake.connectors.gridmet")

DEFAULT_THREDDS = "http://thredds.northwestknowledge.net:8080/thredds"
DEFAULT_DATASET = "agg_met_{short}_1979_CurrentYear_CONUS.nc"
RESOLUTION_DEG = 1.0 / 24.0
RETRY_STATUS = (408, 429)   # plus every 5xx
_NETCDF_LOCK = threading.Lock()   # netCDF-C is not thread-safe (subset_problem runs in the fetch threads)
CITATION = ("Abatzoglou, J.T., 2013, Development of gridded surface meteorological data for ecological "
            "applications and modelling, International Journal of Climatology, 33, 121-131, doi:10.1002/joc.3413")


# ---------------------------------------------------------------------------- shared helpers (also ERA5-Land)
def month_starts(period: Period) -> list[date]:
    """First day of every calendar month touched by ``period`` (inclusive)."""
    out, cur = [], period.start.replace(day=1)
    while cur <= period.end:
        out.append(cur)
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def month_end(first: date) -> date:
    return first.replace(day=calendar.monthrange(first.year, first.month)[1])


def utc_midnight(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def buffered_bbox(bbox: Iterable[float], cells: float, res: float) -> tuple[float, float, float, float]:
    w, s, e, n = bbox
    pad = cells * res
    return (w - pad, s - pad, e + pad, n + pad)


def clip_lonlat(ds, bbox: Iterable[float], res: float, buffer_cells: int = 1):
    """Keep cells whose extent intersects ``bbox`` plus ``buffer_cells`` more cells on every side.

    ``ds`` has 1-D cell-centre coordinates ``x`` (longitude) and ``y`` (latitude). A cell centred at ``c``
    spans ``c +- res/2``; it is kept when ``c`` lies within ``bbox`` widened by ``(buffer_cells + 0.5) * res``.
    """
    w, s, e, n = bbox
    pad = (buffer_cells + 0.5) * res - 1e-9 * res
    x, y = ds["x"].values, ds["y"].values
    xs = (x > w - pad) & (x < e + pad)
    ys = (y > s - pad) & (y < n + pad)
    if not xs.any() or not ys.any():
        raise ValueError(f"no grid cells within bbox {tuple(bbox)} (+{buffer_cells} cells)")
    return ds.isel(x=np.flatnonzero(xs), y=np.flatnonzero(ys))


def cell_bounds(ds, res: float) -> tuple[float, float, float, float]:
    """WGS84 bbox of the cell edges of a clipped grid."""
    x, y = ds["x"].values, ds["y"].values
    return (float(x.min() - res / 2), float(y.min() - res / 2), float(x.max() + res / 2), float(y.max() + res / 2))


# ---------------------------------------------------------------------------- region bands
Box = tuple[float, float, float, float]


def band_boxes(geometry, outer: Iterable[float], n_bands: int, res: float, margin_cells: float = 1.5,
               pad_cells: float = 1.0) -> list[Box]:
    """Request boxes covering every cell of ``outer`` that can touch ``geometry`` (EPSG:4326), in latitude bands.

    A cell touching the polygon has its centre within ``res * sqrt(2) / 2`` (in degrees) of it, so it lies inside
    the polygon buffered by ``margin_cells`` (> 0.71) cells. That buffer, cut to ``outer``, is split into
    ``n_bands`` equal latitude strips; each box is the strip's bounding box padded by ``pad_cells`` cells (so the
    NCSS edge rule does not matter) and cut to ``outer``. Boxes overlap at band edges; empty strips are dropped.
    """
    import shapely
    from shapely.geometry import box

    w, s, e, n = (float(v) for v in outer)
    area = shapely.intersection(geometry.buffer(margin_cells * res), box(w, s, e, n))
    if area.is_empty:
        raise ValueError(f"region does not reach the request box {(w, s, e, n)}")
    gw, gs, ge, gn = area.bounds
    edges = np.linspace(gs, gn, max(1, int(n_bands)) + 1)
    pad = pad_cells * res
    out: list[Box] = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        part = shapely.intersection(area, box(gw, lo, ge, hi))
        if part.is_empty:
            continue
        bw, bs, be, bn = part.bounds
        out.append((max(w, bw - pad), max(s, bs - pad), min(e, be + pad), min(n, bn + pad)))
    return out


def mosaic_bands(parts: Sequence, res: float):
    """Put the band subsets of one variable on one regular north-up lattice; returns ``(mosaic, covered)``.

    ``parts`` are ``(time, y, x)`` DataArrays from :func:`read_ncss_variable` on the same native grid. Cells are
    placed by integer lattice index (not by float equality); cells requested by no band are NaN and
    ``covered`` (``(y, x)`` bool) is False there. Bands must share the time axis, and overlapping cells must
    agree (both would fail on a server mid-update). Coordinates keep the source values where a band has them.
    """
    import xarray as xr

    if not parts:
        raise ValueError("mosaic_bands: no parts")
    if len(parts) == 1:
        p = parts[0]
        return p, np.ones((p.sizes["y"], p.sizes["x"]), dtype=bool)
    times = parts[0]["time"].values
    for p in parts[1:]:
        if not np.array_equal(p["time"].values, times):
            raise RuntimeError(f"gridmet {parts[0].name}: request bands have different time axes "
                               f"({p.sizes['time']} vs {len(times)} days)")
    x0 = min(float(p["x"].min()) for p in parts)
    y0 = max(float(p["y"].max()) for p in parts)
    nx = int(round((max(float(p["x"].max()) for p in parts) - x0) / res)) + 1
    ny = int(round((y0 - min(float(p["y"].min()) for p in parts)) / res)) + 1
    xs, ys = x0 + np.arange(nx) * res, y0 - np.arange(ny) * res
    out = np.full((len(times), ny, nx), np.nan, dtype="float32")
    covered = np.zeros((ny, nx), dtype=bool)
    for p in parts:
        ci = np.rint((p["x"].values - x0) / res).astype("int64")
        ri = np.rint((y0 - p["y"].values) / res).astype("int64")
        if (np.abs(x0 + ci * res - p["x"].values).max() > 1e-3 * res
                or np.abs(y0 - ri * res - p["y"].values).max() > 1e-3 * res):
            raise RuntimeError(f"gridmet {p.name}: request bands are not on one lattice")
        vals = np.asarray(p.values, dtype="float32")
        seen = covered[np.ix_(ri, ci)]
        if seen.any():
            old = out[:, ri[:, None], ci[None, :]][:, seen]
            if not np.array_equal(old, vals[:, seen], equal_nan=True):
                raise RuntimeError(f"gridmet {p.name}: overlapping request bands disagree")
        out[:, ri[:, None], ci[None, :]] = vals
        covered[np.ix_(ri, ci)] = True
        xs[ci], ys[ri] = p["x"].values, p["y"].values
    da = xr.DataArray(out, dims=("time", "y", "x"), coords={"time": times, "y": ys, "x": xs}, name=parts[0].name)
    return da, covered


def check_band_coverage(ds, covered: np.ndarray, region) -> None:
    """Raise if a cell of ``ds`` (1-D lon/lat ``x``/``y``, north-up) that touches ``region`` was not requested."""
    import rioxarray  # noqa: F401  (registers the .rio accessor)
    import xarray as xr

    from fuellake.region import zarr_inside_mask

    grid = xr.Dataset(coords={"x": ds["x"].values, "y": ds["y"].values})
    grid = grid.rio.set_spatial_dims(x_dim="x", y_dim="y").rio.write_crs("EPSG:4326")
    missed = zarr_inside_mask(grid, region) & ~covered
    if missed.any():
        raise RuntimeError(f"gridmet: request bands miss {int(missed.sum())} cells that touch region {region.id}")


# ---------------------------------------------------------------------------- connector
class GridmetConnector(BaseConnector):
    """gridMET daily variables for one AOI, one Zarr item per calendar month."""

    # ------------------------------------------------------------------ config
    @property
    def thredds(self) -> str:
        return str(self.dataset.options.get("thredds_base", DEFAULT_THREDDS)).rstrip("/")

    def source_dataset(self, short: str) -> str:
        return str(self.dataset.options.get("dataset_pattern", DEFAULT_DATASET)).format(short=short)

    @property
    def resolution(self) -> float:
        return float(self.dataset.options.get("resolution_deg", RESOLUTION_DEG))

    @property
    def buffer_cells(self) -> int:
        return int(self.dataset.options.get("buffer_cells", 1))

    @property
    def request_buffer_cells(self) -> int:
        return int(self.dataset.options.get("request_buffer_cells", 2))

    @property
    def max_workers(self) -> int:
        return max(1, int(self.dataset.options.get("max_workers", 4)))

    @property
    def retries(self) -> int:
        return max(0, int(self.dataset.options.get("retries", 3)))

    @property
    def retry_backoff_s(self) -> float:
        return float(self.dataset.options.get("retry_backoff_s", 2.0))

    def request_boxes(self, aoi: AOI) -> list[Box]:
        """NCSS request boxes for ``aoi``: one box (bbox + ``request_buffer_cells``), or latitude bands over the
        lake region for a region-scale AOI (module docstring; ``region_request_bands`` <= 1 turns bands off)."""
        outer = buffered_bbox(aoi.bbox, self.request_buffer_cells, self.resolution)
        n_bands = int(self.dataset.options.get("region_request_bands", 1))
        region = lake_region() if aoi.scale == "region" and n_bands > 1 else None
        if region is None:
            return [outer]
        margin = float(self.dataset.options.get("region_margin_cells", 1.5))
        return band_boxes(region.geometry, outer, n_bands, self.resolution, margin_cells=margin)

    def source_name(self, short: str) -> str:
        v = self.variable(short)
        name = getattr(v, "source_name", None) or (v.model_extra or {}).get("source_name")
        if not name:
            raise KeyError(f"gridmet variable {short!r} needs 'source_name' in config/datasets/gridmet.yaml")
        return str(name)

    def ncss_url(self, short: str, bbox: Iterable[float], first: date, last: date) -> str:
        w, s, e, n = bbox
        q = {
            "var": self.source_name(short),
            "north": f"{n:.5f}", "west": f"{w:.5f}", "east": f"{e:.5f}", "south": f"{s:.5f}",
            "disableProjSubset": "on", "horizStride": "1",
            "time_start": f"{first:%Y-%m-%d}T00:00:00Z", "time_end": f"{last:%Y-%m-%d}T00:00:00Z",
            "timeStride": "1", "accept": "netcdf",
        }
        return f"{self.thredds}/ncss/{self.source_dataset(short)}?{urlencode(q)}"

    # ------------------------------------------------------------------ discover
    def latest_day(self, session=None) -> date:
        """Last day available for **every** configured variable (NCSS ``dataset.xml`` TimeSpan end)."""
        s = session or self.http_session()
        ends = []
        for v in self.dataset.variables:
            url = f"{self.thredds}/ncss/{self.source_dataset(v.name)}/dataset.xml"
            r = s.get(url, timeout=self.settings.http_timeout)
            r.raise_for_status()
            ends.append(parse_timespan_end(r.text))
        return min(ends)

    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        if not self.dataset.variables:
            raise ValueError("gridmet: no variables configured")
        latest = self.latest_day()
        boxes = self.request_boxes(aoi)
        refs: list[SourceRef] = []
        for first in month_starts(period):
            if first > latest:
                log.info("gridmet %s: %s not available yet (data end %s)", aoi.id, f"{first:%Y-%m}", latest)
                continue
            last = min(month_end(first), latest)
            complete = last == month_end(first)
            key = f"gridmet-{first:%Y%m}" if complete else f"gridmet-{first:%Y%m}-through-{last:%Y%m%d}"
            urls = [self.ncss_url(v.name, b, first, last) for v in self.dataset.variables for b in boxes]
            refs.append(self.ref(key, utc_midnight(first), urls, month=f"{first:%Y-%m}",
                                 first_day=first.isoformat(), last_day=last.isoformat(), complete=complete,
                                 data_end=latest.isoformat(),
                                 request_boxes=[[round(c, 5) for c in b] for b in boxes]))
        return refs

    # ------------------------------------------------------------------ extract
    @property
    def raw_gzip(self) -> bool:
        return bool(self.dataset.options.get("raw_gzip", True))

    def raw_name(self, aoi: AOI, short: str, first: date, band: int | None) -> str:
        """``<aoi>_<var>_<yyyymm>.nc`` (one box) or ``<aoi>_<var>_<yyyymm>_b<k>.nc`` (band ``k``), plus ``.gz``
        when ``raw_gzip`` (the default) keeps the received bytes gzip-compressed (about 3.6x smaller)."""
        name = f"{aoi.id}_{short}_{first:%Y%m}" + ("" if band is None else f"_b{band}") + ".nc"
        return name + ".gz" if self.raw_gzip else name

    def fetch(self, session, url: str, what: str) -> bytes:
        """GET one NCSS subset; retries HTTP 408/429/5xx and connection errors with exponential backoff."""
        import requests

        last = ""
        for attempt in range(self.retries + 1):
            try:
                r = session.get(url, timeout=max(self.settings.http_timeout, 180))
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if r.status_code == 200:
                    if not (r.content[:3] == b"CDF" or r.content[:4] == b"\x89HDF"):
                        raise RuntimeError(f"gridmet NCSS {what}: response is not NetCDF "
                                           f"({r.headers.get('content-type')})")
                    return r.content
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code not in RETRY_STATUS and r.status_code < 500:
                    break
            if attempt < self.retries:
                log.warning("gridmet NCSS %s: %s; retry %d/%d", what, last, attempt + 1, self.retries)
                _time.sleep(self.retry_backoff_s * 2 ** attempt)
        raise RuntimeError(f"gridmet NCSS {what}: {last}")

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        """One NCSS request per variable and request box, ``max_workers`` at a time, but never two at once for
        the same variable: concurrent requests to one aggregated dataset returned files with garbage time and
        lat/lon axes (1 of 44 subsets in each of two tests on 2026-11-14; none in 88 with this rule).

        The threads only download. The calling thread checks every response's axes (:func:`subset_problem`;
        netCDF-C/HDF5 print error stacks from other threads) and fetches damaged ones again, up to ``retries``
        times. Only checked subsets are written to ``raw/``. Returns the raw files in variable order, then box
        order (the order :meth:`transform` expects)."""
        first, last = date.fromisoformat(ref.meta["first_day"]), date.fromisoformat(ref.meta["last_day"])
        boxes = [tuple(b) for b in ref.meta.get("request_boxes") or self.request_boxes(aoi)]
        names = [v.name for v in self.dataset.variables]
        jobs = [(short, k) for k in range(len(boxes)) for short in names]   # band-major: spread the datasets
        locks = {short: threading.Lock() for short in names}
        local = threading.local()

        def label(job) -> str:
            return job[0] if len(boxes) == 1 else f"{job[0]} band {job[1]}"

        def download(job) -> bytes:
            short, k = job
            if not hasattr(local, "session"):
                local.session = self.http_session()
            with locks[short]:
                return self.fetch(local.session, self.ncss_url(short, boxes[k], first, last), label(job))

        t0 = _time.monotonic()
        bodies: dict[tuple[str, int], bytes] = {}
        pending, damaged = list(jobs), []
        for attempt in range(self.retries + 1):
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(pending))) as pool:
                got = list(pool.map(download, pending))
            damaged = []
            for job, body in zip(pending, got, strict=True):
                problem = subset_problem(body, first, last, boxes[job[1]], self.resolution)
                if problem is None:
                    bodies[job] = body
                else:
                    damaged.append((job, problem))
            if not damaged:
                break
            for job, problem in damaged:
                log.warning("gridmet NCSS %s: damaged response (%s); fetch %d/%d", label(job), problem,
                            attempt + 2, self.retries + 1)
            pending = [job for job, _ in damaged]
            if attempt < self.retries:
                _time.sleep(self.retry_backoff_s * 2 ** attempt)
        if damaged:
            job, problem = damaged[0]
            raise RuntimeError(f"gridmet NCSS {label(job)}: damaged response after {self.retries + 1} fetches: "
                               f"{problem}")
        out = []
        for short in names:
            for k in range(len(boxes)):
                rel = self.raw_path(ref, self.raw_name(aoi, short, first, None if len(boxes) == 1 else k))
                body = bodies[(short, k)]
                self.lake.write_bytes(rel, gzip.compress(body, compresslevel=6, mtime=0) if rel.endswith(".gz")
                                      else body)
                out.append(rel)
        size, secs = sum(map(len, bodies.values())), _time.monotonic() - t0
        log.info("gridmet %s %s: %d NCSS subsets, %.1f MB in %.1f s (%.2f MB/s)", aoi.id, ref.meta["month"],
                 len(bodies), size / 1e6, secs, size / 1e6 / max(secs, 1e-6))
        return out

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        import xarray as xr

        boxes = [tuple(b) for b in ref.meta.get("request_boxes") or self.request_boxes(aoi)]
        nb, variables = len(boxes), list(self.dataset.variables)
        if len(raw) != len(variables) * nb:
            raise RuntimeError(f"gridmet: expected {len(variables) * nb} raw files ({len(variables)} variables x "
                               f"{nb} request boxes), got {len(raw)}")
        region = lake_region() if nb > 1 else None
        arrays = []
        var_attrs: dict[str, dict] = {}
        masked_by_var: dict[str, int] = {}
        masked = n_valid = 0
        for i, v in enumerate(variables):
            parts = [read_ncss_variable(self.lake, rel, self.source_name(v.name), v.name)
                     for rel in raw[i * nb:(i + 1) * nb]]
            da, covered = mosaic_bands(parts, self.resolution)
            sub = clip_lonlat(xr.Dataset({v.name: da, "covered": (("y", "x"), covered)}), aoi.bbox,
                              self.resolution, self.buffer_cells)
            da = sub[v.name]
            if region is not None and i == 0:   # every variable uses the same boxes
                check_band_coverage(sub, sub["covered"].values, region)
            vals, _ = mask_out_of_range(da.values, v.valid_range)
            nv = int(np.isfinite(da.values).sum())
            masked_by_var[v.name] = nv - int(np.isfinite(vals).sum())
            masked += masked_by_var[v.name]
            n_valid += nv
            var_attrs[v.name] = {"units": v.unit or "", "long_name": v.description or v.name,
                                 "gridmet_source_variable": self.source_name(v.name),
                                 "gridmet_source_dataset": self.source_dataset(v.name)}
            arrays.append(xr.DataArray(vals.astype("float32"), dims=("time", "y", "x"),
                                       coords={"time": da["time"].values, "y": da["y"].values,
                                               "x": da["x"].values}, name=v.name))
        # join="exact": variables on different grids or days (server mid-update) fail the unit.
        ds = xr.merge(arrays, join="exact", combine_attrs="drop")
        for name, attrs in var_attrs.items():
            ds[name].attrs = attrs
        ds = finalize_lonlat_dataset(ds, attrs={
            "title": "gridMET daily surface meteorology and fuel indices (AOI subset)",
            "source": f"{self.thredds}/ncss (Northwest Knowledge Network THREDDS)",
            "citation": CITATION,
            "license": "CC0-1.0 (gridMET: 'free of known copyright restrictions')",
            "time_convention": "gridMET day D ends at midnight MST (07 UTC on D+1); time = D at 00:00",
            "fuellake_aoi": aoi.id,
        })
        first = date.fromisoformat(ref.meta["first_day"])
        days = ds["time"].values.astype("datetime64[D]")
        if days.size == 0:
            raise RuntimeError("gridmet: empty time axis")
        last = date.fromisoformat(str(days.max()))
        iid = self.item_id(aoi, ref)
        path = self.zarr_path(aoi, iid)
        write_zarr(self.lake, path, ds)
        props: dict[str, Any] = {
            "fuellake:qc_masked_fraction": (masked / n_valid) if n_valid else 0.0,
            "gridmet:qc_masked_cells": masked_by_var,   # per variable: values outside valid_range set to NaN
            "gridmet:first_day": first.isoformat(),
            "gridmet:last_day": last.isoformat(),
            "gridmet:complete_month": bool(ref.meta.get("complete")),
            "gridmet:variables": [v.name for v in self.dataset.variables],
            "gridmet:shape": [int(ds.sizes["time"]), int(ds.sizes["y"]), int(ds.sizes["x"])],
            "gridmet:resolution_deg": self.resolution,
            "gridmet:request_boxes": [[round(c, 5) for c in b] for b in boxes],
            "gridmet:download_bytes": int(sum(raw_netcdf_size(self.lake, rel) for rel in raw)),
            "gridmet:raw_stored_bytes": int(sum(self.lake.fs.size(self.lake.url(rel)) for rel in raw)),
        }
        return [StagedItem(item_id=iid, datetime=utc_midnight(first), properties=props,
                           assets=[self.zarr_asset(path)], bbox=cell_bounds(ds, self.resolution),
                           start_datetime=utc_midnight(first),
                           end_datetime=utc_midnight(last + timedelta(days=1)) - timedelta(microseconds=1))]


# ---------------------------------------------------------------------------- parsing helpers
_END_RE = re.compile(r"<TimeSpan>.*?<end>\s*([^<\s]+)\s*</end>", re.S | re.I)


def parse_timespan_end(xml_text: str) -> date:
    """Date of ``<TimeSpan><end>`` in an NCSS ``dataset.xml`` document."""
    m = _END_RE.search(xml_text)
    if not m:
        raise ValueError("NCSS dataset.xml has no <TimeSpan><end>")
    return datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).date()


def subset_problem(body: bytes, first: date, last: date, box: Iterable[float], res: float) -> str | None:
    """Why an NCSS NetCDF subset does not match its request, or None if it does: the time axis must be the days
    ``first..last``, ``lat``/``lon`` finite and regular with spacing ``res`` inside ``box`` widened by two cells.
    Catches the damaged responses described in :meth:`GridmetConnector.extract`. Thread-safe: netCDF-C is not, so
    every call holds :data:`_NETCDF_LOCK` (concurrent in-memory opens failed with "NetCDF: Not a valid ID")."""
    import netCDF4

    try:
        with _NETCDF_LOCK, netCDF4.Dataset("ncss-subset.nc", mode="r", memory=body) as d:
            tname = next((t for t in ("day", "time") if t in d.variables), None)
            if tname is None or "lat" not in d.variables or "lon" not in d.variables:
                return f"missing day/lat/lon variables (has {list(d.variables)})"
            tvar = d.variables[tname]
            t = np.ma.filled(np.ma.asarray(tvar[:], dtype="float64"), np.nan).ravel()
            units = str(getattr(tvar, "units", ""))
            lat = np.ma.filled(np.ma.asarray(d.variables["lat"][:], dtype="float64"), np.nan).ravel()
            lon = np.ma.filled(np.ma.asarray(d.variables["lon"][:], dtype="float64"), np.nan).ravel()
    except Exception as exc:
        return f"unreadable NetCDF ({type(exc).__name__}: {exc})"
    n = (last - first).days + 1
    if t.size != n or not np.isfinite(t).all():
        return f"time axis has {t.size} values ({int(np.isfinite(t).sum())} finite), expected {n} days"
    try:
        got = [date(d.year, d.month, d.day) for d in netCDF4.num2date(t, units)]
    except Exception as exc:
        return f"undecodable time axis ({type(exc).__name__}: {exc})"
    if got != [first + timedelta(days=i) for i in range(n)]:
        return f"time axis {got[0]}..{got[-1]} does not match {first}..{last}"
    w, s, e, nn = (float(v) for v in box)
    for name, c, lo, hi in (("lon", lon, w, e), ("lat", lat, s, nn)):
        if c.size == 0 or not np.isfinite(c).all():
            return f"{name} axis empty or not finite"
        if c.size > 1 and not np.allclose(np.abs(np.diff(c)), res, rtol=0, atol=1e-3 * res):
            return f"{name} axis is not regular at {res:.6f} degrees"
        if c.min() < lo - 2 * res or c.max() > hi + 2 * res:
            return f"{name} axis {c.min():.4f}..{c.max():.4f} outside the requested {lo:.4f}..{hi:.4f}"
    return None


def raw_netcdf_size(lake, rel: str) -> int:
    """Size in bytes of the NetCDF file as received (``.gz``: the gzip ISIZE trailer, files are < 4 GiB)."""
    if not rel.endswith(".gz"):
        return int(lake.fs.size(lake.url(rel)))
    with lake.open(rel, "rb") as f:
        f.seek(-4, 2)
        return int.from_bytes(f.read(4), "little")


@contextmanager
def open_raw_netcdf(lake, rel: str):
    """xarray Dataset (CF-decoded, netCDF4 engine) of a raw NCSS file: ``.nc`` through a local path, ``.nc.gz``
    decompressed in memory (no temporary file)."""
    import xarray as xr

    if rel.endswith(".gz"):
        import netCDF4

        nc = netCDF4.Dataset(rel.rsplit("/", 1)[-1][:-3], mode="r", memory=gzip.decompress(lake.read_bytes(rel)))
        with xr.open_dataset(xr.backends.NetCDF4DataStore(nc), mask_and_scale=True, decode_times=True) as src:
            yield src
        return
    with lake.local_path(rel, "r") as path, \
            xr.open_dataset(path, engine="netcdf4", mask_and_scale=True, decode_times=True) as src:
        yield src


def read_ncss_variable(lake, rel: str, source_name: str, name: str):
    """Read one variable of an NCSS NetCDF subset (``.nc`` or ``.nc.gz``) as float32 ``(time, y, x)`` with NaN
    for missing values.

    CF decoding applies ``scale_factor`` / ``add_offset`` / ``_FillValue`` / ``missing_value``; the result is
    rebuilt as a plain array so that no source encoding (int16 packing) leaks into the Zarr store.

    Uses the netCDF4 engine: the scipy engine breaks on the gridMET variable attribute named ``dimensions`` (it
    shadows scipy's own ``dimensions`` property).
    """
    import warnings

    import xarray as xr

    with warnings.catch_warnings():
        # fm100/fm1000 are float32 with scale_factor 0.1 and a stray _Unsigned flag; xarray applies the
        # scale (values checked: 5-21 % in smm) and warns about _Unsigned. Silence only that warning.
        warnings.filterwarnings("ignore", message=".*_Unsigned attribute but is not of integer type.*")
        with open_raw_netcdf(lake, rel) as src:
            if source_name not in src:
                raise KeyError(f"{rel}: variable {source_name!r} not found (has {list(src.data_vars)})")
            da = src[source_name]
            tdim = next((d for d in da.dims if d in ("day", "time")), None)
            if tdim is None or "lat" not in da.dims or "lon" not in da.dims:
                raise ValueError(f"{rel}: unexpected dims {da.dims}")
            da = da.transpose(tdim, "lat", "lon")
            vals = np.asarray(da.values, dtype="float64")
            times = np.asarray(src[tdim].values).astype("datetime64[ns]")
            lat = np.asarray(src["lat"].values, dtype="float64")
            lon = np.asarray(src["lon"].values, dtype="float64")
    out = xr.DataArray(vals.astype("float32"), dims=("time", "y", "x"),
                       coords={"time": times, "y": lat, "x": lon}, name=name)
    return out.sortby("x").sortby("y", ascending=False).sortby("time")


def finalize_lonlat_dataset(ds, attrs: dict | None = None):
    """Attach EPSG:4326 (``spatial_ref`` coordinate), coordinate attributes and dataset attributes."""
    import rioxarray  # noqa: F401  (registers the .rio accessor)

    ds = ds.transpose("time", "y", "x")
    ds["x"].attrs.update({"units": "degrees_east", "standard_name": "longitude", "long_name": "cell centre longitude"})
    ds["y"].attrs.update({"units": "degrees_north", "standard_name": "latitude", "long_name": "cell centre latitude"})
    ds = ds.rio.set_spatial_dims(x_dim="x", y_dim="y").rio.write_crs("EPSG:4326")
    for name in ds.data_vars:
        ds[name].encoding = {}
    if attrs:
        ds.attrs.update({k: str(v) for k, v in attrs.items()})
    return ds
