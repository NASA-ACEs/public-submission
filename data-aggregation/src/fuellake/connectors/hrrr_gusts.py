# fuel-lake — NOAA HRRR daily maximum surface wind gust and 10 m wind (form B) from the AWS open-data bucket
# Licensed under Apache-2.0.
"""Daily sampled maxima of HRRR surface wind gust and 10 m wind speed for an AOI, read message by message.

Source (verified live 2026-11-14, keyless): NOAA Open Data Dissemination bucket ``noaa-hrrr-bdp-pds``
(``https://noaa-hrrr-bdp-pds.s3.amazonaws.com``), files ``hrrr.YYYYMMDD/conus/hrrr.tHHz.wrfsfcfFF.grib2`` with a
text index ``....grib2.idx`` (``n:offset:d=YYYYMMDDHH:VAR:level:forecast:``; a message ends where the next one
starts). Messages used:

- ``GUST:surface:anl`` from the analyses (``wrfsfcf00``): instantaneous surface wind gust (m/s), about 1.17 MB.
- ``WIND:10 m above ground:0-1 hour max fcst`` from the 1-hour forecasts (``wrfsfcf01``): maximum 10 m wind speed
  over the hour ending at the valid time (ecCodes: ``max_10si``, "Time-maximum 10 metre wind speed"), about
  1.18 MB. Its valid time is cycle + 1 h, so the sample "WIND at T" comes from the cycle T - 1 h.

Only those messages are downloaded (HTTP ``Range`` from the idx offsets). A message is always the full CONUS grid
(1799 x 1059 cells of 3 km, Lambert conformal on a sphere of radius 6371229 m), so the download per message does
not depend on the AOI. Messages are decoded with GDAL's GRIB driver (rasterio), which gives values identical to
ecCodes/cfgrib (checked on 2026-06-01 00Z: max difference 0.0) and the grid georeferencing.

Sampling and day definition
---------------------------
The day is the **gridMET day**: day D runs from 07 UTC on D to 07 UTC on D+1 (midnight to midnight MST), so the
values pair with gridMET's daily means. ``options.messages`` lists, per staged variable, the valid hours (UTC)
sampled each day; a valid hour h < 7 belongs to day D at h on D+1. ``gust_max`` is the maximum of the sampled
analysis gusts, ``wind10_max`` the maximum of the sampled hourly maxima of 10 m wind. Both are **lower bounds** of
the true daily maxima (the sampling is sparse to stay within the download budget; see the dataset caveats for
the calibration against hourly data). The number of samples that entered each day is stored as the time
coordinates ``n_gust_samples`` and ``n_wind10_samples`` (a missing source file lowers it; 0 gives NaN).

Units of work and caching
-------------------------
One unit per calendar month touched by the period (whole months, as gridMET). A month is complete when its last
day is complete (all sampled files exist); its key is ``hrrr-gusts-YYYYMM|<sampling>``. The month still filling
has the key ``hrrr-gusts-YYYYMM-through-YYYYMMDD|<sampling>`` with the **last complete day**, so reruns on the
same day are no-ops and a new key appears once per day. ``<sampling>`` (:func:`sampling_tag`, e.g.
``gust_max@00,12+wind10_max@00,12``) makes a changed sampling reprocess the month.

``extract`` caches, per AOI and day, the AOI crops of all sampled messages as one multi-band GeoTIFF
``raw/hrrr-gusts/YYYY/MM/hrrr-gusts__<aoi>__YYYYMMDD.tif`` (band description
``<variable>|<valid time>``, tags with the bytes downloaded and the source URLs). Cached days are never fetched
again, so an interrupted month resumes where it stopped; the CONUS messages themselves are not kept. Before
fetching a day the planned bytes are summed from the idx offsets; a unit stops with an error once it would exceed
``options.max_unit_mb``.

Output: ``staged/hrrr-gusts/<aoi>/hrrr-gusts__<aoi>__YYYYMM01/data.zarr``, dims ``(time, y, x)``, native HRRR
grid (x/y = cell-centre coordinates in metres of the HRRR Lambert projection, ``spatial_ref`` WKT), clipped to the
AOI bbox plus ``buffer_cells`` cells. Wind direction is not staged (u/v messages are about 2.3 MB each).
"""

from __future__ import annotations

import calendar
import logging
import time as _time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.warp import transform_bounds
from rasterio.windows import Window

from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.rasters import mask_out_of_range, write_zarr
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.hrrr_gusts")

DEFAULT_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
DAY_START_HOUR_UTC = 7            # gridMET day: midnight MST = 07 UTC
GRIB_MAGIC = b"GRIB"


# ---------------------------------------------------------------------------- idx parsing
@dataclass(frozen=True)
class IdxEntry:
    """One line of a NOAA ``.idx`` file: message ``number`` starts at byte ``offset`` and ends at ``end``
    (inclusive; ``None`` for the last message, i.e. to the end of the file)."""

    number: int
    offset: int
    end: int | None
    ref_time: str
    var: str
    level: str
    forecast: str

    @property
    def length(self) -> int | None:
        return None if self.end is None else self.end - self.offset + 1

    def range_header(self) -> str:
        return f"bytes={self.offset}-" if self.end is None else f"bytes={self.offset}-{self.end}"


def parse_idx(text: str) -> list[IdxEntry]:
    """Parse ``n:offset:d=YYYYMMDDHH:VAR:level:forecast:`` lines; message ends come from the next offset."""
    rows = []
    for line in text.strip().splitlines():
        p = line.split(":")
        if len(p) < 6:
            raise ValueError(f"bad idx line {line!r}")
        rows.append((int(p[0]), int(p[1]), p[2].removeprefix("d="), p[3], p[4], p[5]))
    out = []
    for i, (n, off, d, var, lev, fc) in enumerate(rows):
        nxt = rows[i + 1][1] if i + 1 < len(rows) else None
        if nxt is not None and nxt <= off:
            raise ValueError(f"idx offsets not increasing at message {n}")
        out.append(IdxEntry(n, off, None if nxt is None else nxt - 1, d, var, lev, fc))
    return out


def find_message(entries: Iterable[IdxEntry], var: str, level: str, forecast: str | None = None) -> IdxEntry:
    """The single entry with ``var`` and ``level`` (and ``forecast`` if given); raises if none or several."""
    hits = [e for e in entries if e.var == var and e.level == level and (forecast is None or e.forecast == forecast)]
    if len(hits) != 1:
        raise KeyError(f"{len(hits)} idx entries for {var}:{level}:{forecast}")
    return hits[0]


# ---------------------------------------------------------------------------- sampling
def day_window(day: date) -> tuple[datetime, datetime]:
    """gridMET day ``day``: [07 UTC on day, 07 UTC on day + 1)."""
    start = datetime(day.year, day.month, day.day, DAY_START_HOUR_UTC, tzinfo=UTC)
    return start, start + timedelta(days=1)


def sample_times(day: date, hours_utc: Iterable[int]) -> list[datetime]:
    """Valid times of the sampled hours inside the gridMET day (hours < 07 fall on the next calendar date)."""
    start, _ = day_window(day)
    out = []
    for h in sorted(set(int(h) % 24 for h in hours_utc)):
        t = datetime(day.year, day.month, day.day, h, tzinfo=UTC)
        out.append(t if h >= DAY_START_HOUR_UTC else t + timedelta(days=1))
    return sorted(out)


def sampling_tag(messages: dict) -> str:
    """``gust_max@00,12+wind10_max@00,12``: the sampled valid hours per variable (part of the unit key)."""
    return "+".join(f"{n}@{','.join(f'{int(h) % 24:02d}' for h in sorted(m['hours_utc']))}"
                    for n, m in sorted(messages.items()))


def file_url(bucket: str, domain: str, cycle: datetime, fhour: int) -> str:
    return f"{bucket}/hrrr.{cycle:%Y%m%d}/{domain}/hrrr.t{cycle:%H}z.wrfsfcf{fhour:02d}.grib2"


def month_days(first: date) -> list[date]:
    n = calendar.monthrange(first.year, first.month)[1]
    return [first + timedelta(days=i) for i in range(n)]


def month_starts(period: Period) -> list[date]:
    out, cur = [], period.start.replace(day=1)
    while cur <= period.end:
        out.append(cur)
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


# ---------------------------------------------------------------------------- GRIB crop
def aoi_window(src, bbox_wgs84, buffer_cells: int) -> Window:
    """Window of the GRIB grid covering ``bbox_wgs84`` plus ``buffer_cells`` cells (clipped to the grid)."""
    w, s, e, n = transform_bounds("EPSG:4326", src.crs, *bbox_wgs84, densify_pts=21)
    res = abs(src.transform.a)
    pad = buffer_cells * res
    win = src.window(w - pad, s - pad, e + pad, n + pad)
    col0, row0 = max(0, int(np.floor(win.col_off))), max(0, int(np.floor(win.row_off)))
    col1 = min(src.width, int(np.ceil(win.col_off + win.width)))
    row1 = min(src.height, int(np.ceil(win.row_off + win.height)))
    if col1 <= col0 or row1 <= row0:
        raise ValueError("AOI is outside the HRRR grid")
    return Window(col0, row0, col1 - col0, row1 - row0)


def decode_crop(grib: bytes, bbox_wgs84, buffer_cells: int) -> tuple[np.ndarray, Any, Any]:
    """Decode one GRIB2 message with GDAL and return ``(float32 crop, crop transform, crs)``."""
    if not grib.startswith(GRIB_MAGIC):
        raise ValueError("not a GRIB message")
    with rasterio.Env(GDAL_PAM_ENABLED="NO"), MemoryFile(grib, ext=".grib2") as mem, mem.open() as src:
        if src.count != 1:
            raise ValueError(f"expected one GRIB message, got {src.count} bands")
        win = aoi_window(src, bbox_wgs84, buffer_cells)
        arr = src.read(1, window=win).astype("float32")
        nod = src.nodata
        if nod is not None and np.isfinite(nod):
            arr[arr == np.float32(nod)] = np.nan
        return arr, src.window_transform(win), src.crs


# ---------------------------------------------------------------------------- connector
class HrrrGustsConnector(BaseConnector):
    """HRRR daily sampled maxima of gust and 10 m wind for one AOI, one Zarr item per calendar month."""

    def _opts(self) -> dict:
        o = self.dataset.options
        msgs = {}
        for name, m in (o.get("messages") or {}).items():
            msgs[name] = {"var": m["var"], "level": m["level"], "forecast": m.get("forecast"),
                          "fhour": int(m.get("fhour", 0)), "hours_utc": [int(h) for h in m["hours_utc"]]}
        if not msgs:
            raise ValueError("hrrr-gusts: options.messages is empty")
        return {"bucket": str(o.get("bucket_url", DEFAULT_BUCKET)).rstrip("/"), "domain": o.get("domain", "conus"),
                "messages": msgs, "buffer_cells": int(o.get("buffer_cells", 1)),
                "max_unit_mb": float(o.get("max_unit_mb", 250)), "retries": int(o.get("retries", 2))}

    # ------------------------------------------------------------------ http
    def _session(self):
        if not hasattr(self, "_s"):
            self._s = self.http_session()
        return self._s

    def _get(self, url: str, range_header: str | None = None):
        headers = {"Range": range_header} if range_header else None
        last: Exception | None = None
        for attempt in range(self._opts()["retries"] + 1):
            try:
                r = self._session().get(url, headers=headers, timeout=self.settings.http_timeout)
                if r.status_code in (403, 404):
                    return None
                r.raise_for_status()
                return r.content
            except Exception as exc:   # network hiccup: retry
                last = exc
                _time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"HRRR request failed for {url}: {type(last).__name__}: {last}")

    def _exists(self, url: str) -> bool:
        r = self._session().head(url, timeout=self.settings.http_timeout)
        return r.status_code == 200

    # ------------------------------------------------------------------ plan
    def day_samples(self, day: date) -> list[dict]:
        """Every message to read for ``day``: variable, valid time, cycle, file url."""
        o = self._opts()
        out = []
        for name, m in o["messages"].items():
            for t in sample_times(day, m["hours_utc"]):
                cycle = t - timedelta(hours=m["fhour"])
                out.append({"name": name, "valid": t, "cycle": cycle,
                            "url": file_url(o["bucket"], o["domain"], cycle, m["fhour"]), **m})
        return sorted(out, key=lambda s: (s["name"], s["valid"]))

    def day_complete_remote(self, day: date) -> bool:
        """True if the idx of the latest file needed for ``day`` exists in the bucket."""
        last = max(self.day_samples(day), key=lambda s: s["cycle"])
        return self._exists(last["url"] + ".idx")

    def last_complete_day(self, upto: date, lookback_days: int = 4) -> date | None:
        for k in range(lookback_days + 1):
            d = upto - timedelta(days=k)
            if self.day_complete_remote(d):
                return d
        return None

    # ------------------------------------------------------------------ discover
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        last = self.last_complete_day(datetime.now(UTC).date())   # whole months: never cut at --end
        if last is None:
            raise RuntimeError("hrrr-gusts: no complete day found in the bucket in the last days")
        refs = []
        for first in month_starts(period):
            if first > last:
                log.info("hrrr-gusts %s: %s not available yet (last complete day %s)", aoi.id, f"{first:%Y-%m}", last)
                continue
            mlast = min(month_days(first)[-1], last)
            complete = mlast == month_days(first)[-1]
            key = f"hrrr-gusts-{first:%Y%m}" if complete else f"hrrr-gusts-{first:%Y%m}-through-{mlast:%Y%m%d}"
            o = self._opts()
            key = f"{key}|{sampling_tag(o['messages'])}"     # a changed sampling reprocesses the month
            refs.append(self.ref(key, datetime(first.year, first.month, 1, tzinfo=UTC), urls=[o["bucket"]],
                                 first_day=first.isoformat(), last_day=mlast.isoformat(), complete=complete,
                                 last_complete_day=last.isoformat(),
                                 sampling={n: m["hours_utc"] for n, m in o["messages"].items()}))
        return refs

    # ------------------------------------------------------------------ extract
    def crop_path(self, ref: SourceRef, aoi: AOI, day: date) -> str:
        return self.raw_path(ref, f"hrrr-gusts__{aoi.id}__{day:%Y%m%d}.tif")

    def _cached(self, rel: str, samples: list[dict]) -> bool:
        if not self.lake.exists(rel):
            return False
        try:
            with self.lake.local_path(rel, "r") as path, rasterio.open(path) as src:
                want = {f"{s['name']}|{s['valid']:%Y-%m-%dT%H}Z" for s in samples}
                return set(src.tags().get("SAMPLES_ATTEMPTED", "").split(";")) >= want
        except Exception:
            return False

    def fetch_day(self, ref: SourceRef, aoi: AOI, day: date, budget: dict) -> str:
        """Download, decode and crop every sampled message of ``day``; write the day's crop GeoTIFF."""
        o = self._opts()
        samples = self.day_samples(day)
        rel = self.crop_path(ref, aoi, day)
        if self._cached(rel, samples):
            return rel
        planned, plans = 0, []
        for s in samples:
            idx = self._get(s["url"] + ".idx")
            if idx is None:
                log.warning("hrrr-gusts: missing %s.idx", s["url"])
                plans.append((s, None))
                continue
            e = find_message(parse_idx(idx.decode("ascii", "replace")), s["var"], s["level"], s["forecast"])
            planned += e.length or 2_000_000
            plans.append((s, e))
        if (budget["bytes"] + planned) / 1e6 > o["max_unit_mb"]:
            raise RuntimeError(f"hrrr-gusts: download budget of {o['max_unit_mb']} MB per unit would be exceeded "
                               f"on {day} ({budget['bytes'] / 1e6:.0f} MB so far + {planned / 1e6:.1f} MB planned)")
        bands, descs, crs, transform, got = [], [], None, None, 0
        for s, e in plans:
            if e is None:
                continue
            data = self._get(s["url"], e.range_header())
            if data is None:
                continue
            got += len(data)
            arr, tr, c = decode_crop(data, aoi.bbox, o["buffer_cells"])
            if crs is None:
                crs, transform = c, tr
            elif c != crs or tr != transform or arr.shape != bands[0].shape:
                raise ValueError(f"HRRR grid changed within {day} ({s['url']})")
            bands.append(arr)
            descs.append(f"{s['name']}|{s['valid']:%Y-%m-%dT%H}Z")
        budget["bytes"] += got
        if not bands:
            raise RuntimeError(f"hrrr-gusts: no sampled message found for {day}")
        tags = {"SAMPLES_ATTEMPTED": ";".join(f"{s['name']}|{s['valid']:%Y-%m-%dT%H}Z" for s in samples),
                "BYTES_DOWNLOADED": str(got), "SOURCE": ";".join(sorted({s["url"] for s, _ in plans})),
                "DAY_WINDOW": "/".join(t.isoformat() for t in day_window(day))}
        profile = dict(driver="GTiff", width=bands[0].shape[1], height=bands[0].shape[0], count=len(bands),
                       dtype="float32", crs=crs, transform=transform, nodata=np.nan, compress="deflate")
        with self.lake.local_path(rel, "w") as path, rasterio.open(path, "w", **profile) as dst:
            for i, (a, d) in enumerate(zip(bands, descs, strict=True), start=1):
                dst.write(a, i)
                dst.set_band_description(i, d)
            dst.update_tags(**tags)
        return rel

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        first, last = date.fromisoformat(ref.meta["first_day"]), date.fromisoformat(ref.meta["last_day"])
        budget = {"bytes": 0}
        out = []
        d = first
        while d <= last:
            out.append(self.fetch_day(ref, aoi, d, budget))
            d += timedelta(days=1)
        log.info("hrrr-gusts %s %s: downloaded %.1f MB", aoi.id, ref.key, budget["bytes"] / 1e6)
        return out

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        import rioxarray  # noqa: F401
        import xarray as xr

        o = self._opts()
        names = list(o["messages"])
        days, data, counts = [], {n: [] for n in names}, {n: [] for n in names}
        crs = transform = shape = None
        downloaded = 0
        for rel in sorted(raw):
            day = datetime.strptime(rel.rsplit("__", 1)[-1].removesuffix(".tif"), "%Y%m%d").date()
            with self.lake.local_path(rel, "r") as path, rasterio.open(path) as src:
                stack = src.read().astype("float32")
                descs = list(src.descriptions)
                downloaded += int(src.tags().get("BYTES_DOWNLOADED", 0))
                if crs is None:
                    crs, transform, shape = src.crs, src.transform, (src.height, src.width)
                elif src.crs != crs or src.transform != transform:
                    raise ValueError(f"HRRR crop grid differs on {day}")
            days.append(day)
            for n in names:
                idx = [i for i, d in enumerate(descs) if d and d.split("|", 1)[0] == n]
                counts[n].append(len(idx))
                if idx:
                    with np.errstate(all="ignore"):
                        data[n].append(np.nanmax(stack[idx], axis=0) if len(idx) > 1 else stack[idx[0]])
                else:
                    data[n].append(np.full(shape, np.nan, dtype="float32"))
        if not days:
            raise RuntimeError("hrrr-gusts: no day crops")
        res_x, res_y = transform.a, transform.e
        xs = transform.c + res_x * (np.arange(shape[1]) + 0.5)
        ys = transform.f + res_y * (np.arange(shape[0]) + 0.5)
        times = np.array([np.datetime64(d.isoformat()) for d in days], dtype="datetime64[ns]")
        masked = n_valid = 0
        arrays = {}
        for n in names:
            var = self.variable(n)
            cube = np.stack(data[n]).astype("float32")
            vals, frac = mask_out_of_range(cube, var.valid_range)
            nv = int(np.isfinite(cube).sum())
            masked += int(round(frac * nv))
            n_valid += nv
            m = o["messages"][n]
            arrays[n] = xr.DataArray(vals, dims=("time", "y", "x"), name=n, attrs={
                "units": var.unit or "", "long_name": var.description or n,
                "hrrr_message": f"{m['var']}:{m['level']}:{m['forecast'] or ''}",
                "hrrr_file": f"wrfsfcf{m['fhour']:02d}",
                "sampled_valid_hours_utc": ",".join(str(h) for h in m["hours_utc"]),
                "cell_methods": "time: maximum (sampled, lower bound of the true daily maximum)"})
        ds = xr.Dataset(arrays, coords={"time": times, "y": ys, "x": xs})
        for n in names:
            ds = ds.assign_coords({f"n_{n.removesuffix('_max')}_samples": ("time", np.array(counts[n], dtype="int16"))})
        ds["x"].attrs.update({"units": "m", "standard_name": "projection_x_coordinate",
                              "long_name": "cell centre x (HRRR Lambert conformal)"})
        ds["y"].attrs.update({"units": "m", "standard_name": "projection_y_coordinate",
                              "long_name": "cell centre y (HRRR Lambert conformal)"})
        ds = ds.rio.set_spatial_dims(x_dim="x", y_dim="y").rio.write_crs(crs.to_wkt())
        for n in ds.data_vars:
            ds[n].encoding = {}
        ds.attrs.update({
            "title": "HRRR daily sampled maximum surface wind gust and 10 m wind (AOI subset)",
            "source": f"{o['bucket']} (NOAA Open Data Dissemination, HRRR {o['domain']})",
            "day_definition": "gridMET day: D runs from 07 UTC on D to 07 UTC on D+1; time = D at 00:00",
            "license": "NOAA open data (public domain, no restrictions); cite NOAA HRRR",
            "fuellake_aoi": aoi.id,
        })
        first = date.fromisoformat(ref.meta["first_day"])
        iid = self.item_id(aoi, ref)
        path = self.zarr_path(aoi, iid)
        write_zarr(self.lake, path, ds)
        from pyproj import Transformer

        tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        cx = [xs.min() - abs(res_x) / 2, xs.max() + abs(res_x) / 2]
        cy = [ys.min() - abs(res_y) / 2, ys.max() + abs(res_y) / 2]
        lon, lat = tr.transform([cx[0], cx[0], cx[1], cx[1]], [cy[0], cy[1], cy[0], cy[1]])
        stats = {n: {"max": float(np.nanmax(ds[n].values)) if np.isfinite(ds[n].values).any() else None,
                     "mean": float(np.nanmean(ds[n].values)) if np.isfinite(ds[n].values).any() else None}
                 for n in names}
        last = max(days)
        props: dict[str, Any] = {
            "fuellake:qc_masked_fraction": (masked / n_valid) if n_valid else 0.0,
            "hrrr:first_day": first.isoformat(),
            "hrrr:last_day": last.isoformat(),
            "hrrr:complete_month": bool(ref.meta.get("complete")),
            "hrrr:variables": names,
            "hrrr:sampling_hours_utc": {n: o["messages"][n]["hours_utc"] for n in names},
            "hrrr:samples_per_day": {n: {"min": int(min(counts[n])), "max": int(max(counts[n]))} for n in names},
            "hrrr:day_definition": "07 UTC D to 07 UTC D+1 (gridMET day)",
            "hrrr:shape": [int(ds.sizes["time"]), int(ds.sizes["y"]), int(ds.sizes["x"])],
            "hrrr:resolution_m": abs(float(res_x)),
            "hrrr:value_stats": stats,
            "fuellake:bytes_downloaded": downloaded,
            "fuellake:stats_scope": "AOI crop before the pipeline's region clip (see fuellake:region_clip)",
        }
        start = datetime(first.year, first.month, first.day, tzinfo=UTC)
        end = datetime(last.year, last.month, last.day, 23, 59, 59, tzinfo=UTC)
        return [StagedItem(item_id=iid, datetime=start, properties=props, assets=[self.zarr_asset(path)],
                           bbox=(min(lon), min(lat), max(lon), max(lat)), start_datetime=start, end_datetime=end)]
