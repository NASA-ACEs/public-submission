# fuel-lake — ERA5-Land daily aggregates (form B) from the Copernicus Climate Data Store (CDS API, needs a key)
# Licensed under Apache-2.0.
"""ERA5-Land hourly fields from the CDS, aggregated to UTC days, stored as form B Zarr (one item per month).

Verified live on 2026-11-14 (new CDS, ``cdsapi`` 0.7.7 on ``ecmwf-datastores-client`` 0.6.0):

- Dataset ``reanalysis-era5-land`` (hourly). ``derived-era5-land-daily-statistics`` exists but has neither
  ``total_precipitation`` nor a wind speed (only daily statistics of u and v separately), so the hourly product
  is requested and aggregated here.
- Request keys: ``variable`` (list of long names), ``date`` = ``"YYYY-MM-DD/YYYY-MM-DD"`` (a range across a
  month boundary is accepted; the process description lists ``year``/``month``/``day``, which cannot cross a
  month), ``time`` = 24 ``HH:00`` strings, ``area`` = ``[N, W, S, E]``, ``data_format=netcdf``,
  ``download_format=unarchived``. The response is one netCDF4/HDF5 file (cfgrib conversion).
- ``area`` edges off the 0.1 degree lattice are snapped inward to it (no interpolation), so the connector snaps
  the request box outward to whole 0.1 degree multiples.
- File layout: dims ``valid_time, latitude, longitude``; latitude north to south, longitude -180..180; extra
  coordinates ``expver`` (per ``valid_time``) and scalar ``number`` (dropped); variables ``t2m``, ``d2m``,
  ``tp``, ``u10``, ``v10``, ``swvl1``..``swvl4`` (float32, NaN over the ocean).
- Jobs go through the states ``accepted`` -> ``running`` -> ``successful`` (or ``failed`` / ``rejected`` /
  ``dismissed`` / ``deleted``). The connector submits one job per variable group (``request_groups``) for a
  month, polls them with backoff and logs every state change; a failed job fails the unit with the CDS error.

Daily aggregation (UTC days, :func:`daily_aggregate`, rule per output variable from the config ``daily`` key):
``mean`` / ``max`` of the 24 hourly values 00..23 UTC for state variables (``t2m``, ``d2m``, ``u10``, ``v10``,
``swvl1``..``swvl4``); wind speed ``ws10`` = ``sqrt(u10^2 + v10^2)`` per hour, then its daily mean and max
(``ws10_max``). ``tp`` is accumulated from 00 UTC within each day, so the total of day D is the value at D+1
00 UTC (hourly accumulations are never summed); each request therefore runs to 00 UTC of the day after the
month. Only when that hour is not yet published (the open month) does the last day use D 23 UTC (23 h),
flagged by the item property ``era5_land:tp_last_day_23h``. ``tp`` is stored in mm (m x 1000).

Raw files ``raw/era5-land-daily/<yyyy>/<mm>/<aoi>_era5-land_<yyyymm>_<group>.nc`` (the region subset as
received) are reused when present and readable, so a unit that failed after download is not queued again.

Credentials come only from the environment (``CDSAPI_URL``, ``CDSAPI_KEY``), never from ``~/.cdsapirc``; the
official client ``cdsapi`` (a fuel-lake dependency) is imported lazily.

No keyless mirror (checked live 2026-11-14): ECMWF ARCO ERA5-Land Zarr and DestinE Earth Data Hub answer HTTP
401; Google ARCO-ERA5 and Earthmover Icechunk hold ERA5 0.25 degree only; Open-Meteo ``era5_land`` is a point API.
"""

from __future__ import annotations

import logging
import math
import time as _time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from fuellake.config import redact
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.connectors.gridmet import (
    buffered_bbox,
    cell_bounds,
    clip_lonlat,
    finalize_lonlat_dataset,
    month_end,
    month_starts,
    utc_midnight,
)
from fuellake.rasters import mask_out_of_range, write_zarr
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.era5_land")

CDS_DATASET = "reanalysis-era5-land"
CATALOGUE_URL = "https://cds.climate.copernicus.eu/api/catalogue/v1/collections/reanalysis-era5-land"
RESOLUTION_DEG = 0.1
ACCUMULATED = {"tp"}
DERIVED = {"ws10": ("u10", "v10")}          # hourly wind speed from the components
# CDS netCDF short name -> CDS request variable name
CDS_VARIABLES = {
    "t2m": "2m_temperature",
    "d2m": "2m_dewpoint_temperature",
    "tp": "total_precipitation",
    "u10": "10m_u_component_of_wind",
    "v10": "10m_v_component_of_wind",
    "swvl1": "volumetric_soil_water_layer_1",
    "swvl2": "volumetric_soil_water_layer_2",
    "swvl3": "volumetric_soil_water_layer_3",
    "swvl4": "volumetric_soil_water_layer_4",
}
DEFAULT_GROUPS = {"surface": ["t2m", "d2m", "tp", "u10", "v10"], "soil": ["swvl1", "swvl2", "swvl3", "swvl4"]}
SCALE = {"tp": 1000.0}                      # m -> mm
PENDING_STATES = ("accepted", "queued", "running")
FAILED_STATES = ("failed", "rejected", "dismissed", "deleted")
RULE_TEXT = {"mean": "mean of the 24 hourly values 00-23 UTC", "max": "maximum of the 24 hourly values 00-23 UTC",
             "min": "minimum of the 24 hourly values 00-23 UTC",
             "accum": "value at D+1 00 UTC (accumulation from 00 UTC), m converted to mm"}


class Era5CredentialsError(RuntimeError):
    """CDS credentials or the ``cdsapi`` client are missing."""


class CdsJobError(RuntimeError):
    """A CDS job ended in a failed state or did not finish in time."""


def cds_credentials(settings) -> tuple[str, str]:
    url, key = settings.secret("CDSAPI_URL"), settings.secret("CDSAPI_KEY")
    missing = [n for n, v in (("CDSAPI_URL", url), ("CDSAPI_KEY", key)) if not v]
    if missing:
        raise Era5CredentialsError(
            f"era5-land-daily needs CDS credentials: {', '.join(missing)} not set. Register at "
            "https://cds.climate.copernicus.eu, accept the ERA5-Land licence (CC-BY-4.0), then export "
            "CDSAPI_URL=https://cds.climate.copernicus.eu/api and CDSAPI_KEY=<personal access token>.")
    return url, key


def import_cdsapi():
    try:
        import cdsapi
    except ImportError as exc:
        raise Era5CredentialsError("era5-land-daily needs the 'cdsapi' package (a fuel-lake dependency: "
                                   "run `uv sync`)") from exc
    return cdsapi


def snapped_area(bbox, res: float) -> list[float]:
    """CDS ``area`` [N, W, S, E] of ``bbox`` (W, S, E, N) snapped outward to multiples of ``res``."""
    w, s, e, n = bbox
    k = 1e-6
    return [round(math.ceil(n / res - k) * res, 4), round(math.floor(w / res + k) * res, 4),
            round(math.floor(s / res + k) * res, 4), round(math.ceil(e / res - k) * res, 4)]


def daily_rule(v) -> tuple[str, str]:
    """``(hourly source variable, rule)`` of a config variable (keys ``source`` and ``daily``)."""
    extra = v.model_extra or {}
    source = str(extra.get("source") or v.name)
    rule = str(extra.get("daily") or ("accum" if source in ACCUMULATED else "mean"))
    if rule not in RULE_TEXT:
        raise ValueError(f"era5-land variable {v.name}: unknown daily rule {rule!r}")
    return source, rule


def daily_aggregate(hourly, rules: dict[str, tuple[str, str]] | None = None, days: list[date] | None = None):
    """Hourly ``(time, y, x)`` Dataset -> daily UTC Dataset (module docstring).

    ``rules`` maps output name -> ``(hourly variable, mean|max|min|accum)``; default: every hourly variable,
    ``accum`` for ``tp``, else ``mean``. ``ws10`` is derived from ``u10``/``v10`` when a rule needs it. Returns
    ``(daily, tp_last_day_23h)``; ``days`` limits the output to these dates (the requested month).
    """
    import pandas as pd
    import xarray as xr

    if rules is None:
        rules = {n: (n, "accum" if n in ACCUMULATED else "mean") for n in hourly.data_vars}
    hourly = hourly.copy()
    for name, (u, v) in DERIVED.items():
        if name not in hourly and any(src == name for src, _ in rules.values()):
            hourly[name] = np.hypot(hourly[u], hourly[v]).astype("float32")
    times = pd.DatetimeIndex(hourly["time"].values)
    closed = {t.date() - timedelta(days=1) for t in times if t.hour == 0}   # days whose D+1 00 UTC is present
    out = {}
    has_accum = False
    for name, (src, rule) in rules.items():
        da = hourly[src]
        if rule == "accum":
            # value at hour h is the accumulation since 00 UTC of its day; 00 UTC of D+1 closes day D
            has_accum = True
            shifted = da.assign_coords(time=times - pd.Timedelta(hours=1))
            daily = shifted.groupby(shifted["time"].dt.floor("D")).last(skipna=False) * SCALE.get(src, 1.0)
        else:
            daily = getattr(da.groupby(da["time"].dt.floor("D")), rule)(skipna=False)
        out[name] = daily.rename({daily.dims[0]: "time"})
    ds = xr.Dataset(out)
    if days is not None:
        keep = np.isin(ds["time"].values.astype("datetime64[D]"), np.array(days, dtype="datetime64[D]"))
        ds = ds.isel(time=np.flatnonzero(keep))
    out_days = [pd.Timestamp(t).date() for t in ds["time"].values]
    last_is_23h = bool(has_accum and out_days and out_days[-1] not in closed)
    return ds, last_is_23h


def read_cds_netcdf(lake, rel: str, names: list[str]):
    """Open a CDS ERA5-Land NetCDF (``valid_time``/``time``, ``latitude``, ``longitude``) as hourly
    float32 ``(time, y, x)`` with x/y = cell-centre lon/lat (longitudes wrapped to -180..180)."""
    import xarray as xr

    with lake.local_path(rel, "r") as path, xr.open_dataset(path, engine="netcdf4") as src:
        tname = "valid_time" if "valid_time" in src.coords or "valid_time" in src.dims else "time"
        data = {}
        for name in names:
            if name not in src:
                raise KeyError(f"{rel}: variable {name!r} missing (has {list(src.data_vars)})")
            da = src[name]
            extra = [d for d in da.dims if d not in (tname, "latitude", "longitude")]
            for d in extra:   # e.g. a length-1 expver/number dimension
                da = da.isel({d: 0}, drop=True)
            data[name] = (("time", "y", "x"),
                          np.asarray(da.transpose(tname, "latitude", "longitude").values, dtype="float32"))
        lon = np.asarray(src["longitude"].values, dtype="float64")
        coords = {"time": np.asarray(src[tname].values).astype("datetime64[ns]"),
                  "y": np.asarray(src["latitude"].values, dtype="float64"),
                  "x": np.where(lon > 180, lon - 360, lon)}
    return xr.Dataset(data, coords=coords).sortby("x").sortby("y", ascending=False).sortby("time")


def raw_expver(lake, rel: str) -> list[str]:
    """Distinct ``expver`` values of a CDS file (``0001`` final ERA5-Land, ``0005`` preliminary ERA5-Land-T)."""
    import xarray as xr

    with lake.local_path(rel, "r") as path, xr.open_dataset(path, engine="netcdf4") as src:
        return sorted({str(v) for v in np.asarray(src["expver"].values).ravel()}) if "expver" in src else []


def merge_groups(parts: list):
    """Merge the hourly group datasets of one month (they must share the grid and the hours)."""
    import xarray as xr

    return xr.merge(parts, join="exact", combine_attrs="drop")


class Era5LandDailyConnector(BaseConnector):
    """ERA5-Land via the CDS API (``CDSAPI_URL`` / ``CDSAPI_KEY``), one Zarr item per AOI and month."""

    @property
    def resolution(self) -> float:
        return float(self.dataset.options.get("resolution_deg", RESOLUTION_DEG))

    @property
    def groups(self) -> dict[str, list[str]]:
        return {str(k): [str(n) for n in v] for k, v in
                (self.dataset.options.get("request_groups") or DEFAULT_GROUPS).items()}

    def rules(self) -> dict[str, tuple[str, str]]:
        return {v.name: daily_rule(v) for v in self.dataset.variables}

    def hourly_names(self) -> list[str]:
        """Hourly CDS short names the configured variables need (``ws10`` needs ``u10`` and ``v10``)."""
        names: list[str] = []
        for src, _ in self.rules().values():
            for n in DERIVED.get(src, (src,)):
                if n not in names:
                    names.append(n)
        return names

    def active_groups(self) -> dict[str, list[str]]:
        need = self.hourly_names()
        out = {g: [n for n in names if n in need] for g, names in self.groups.items()}
        out = {g: names for g, names in out.items() if names}
        grouped = {n for names in out.values() for n in names}
        if set(need) - grouped:
            raise ValueError(f"era5-land: variables {sorted(set(need) - grouped)} are in no request group")
        return out

    def data_end(self) -> date:
        """Last available day from the public CDS catalogue (``extent.temporal``), else today - 6 days."""
        try:
            r = self.http_session().get(self.dataset.options.get("catalogue_url", CATALOGUE_URL),
                                        timeout=self.settings.http_timeout)
            r.raise_for_status()
            end = r.json()["extent"]["temporal"]["interval"][0][1]
            return datetime.fromisoformat(end.replace("Z", "+00:00")).date()
        except Exception as exc:  # catalogue is advisory
            log.warning("era5-land: catalogue end date unavailable (%s); assuming today - 6 days", redact(exc))
            return datetime.now(UTC).date() - timedelta(days=6)

    def request(self, aoi: AOI, first: date, last: date, names: list[str] | None = None,
                through_next_00: bool = True) -> dict[str, Any]:
        """One CDS request: ``first`` 00 UTC .. ``last`` 23 UTC, plus ``last + 1`` (its 00 UTC closes the
        accumulation of ``last``) when ``through_next_00``."""
        box = buffered_bbox(aoi.bbox, int(self.dataset.options.get("request_buffer_cells", 2)), self.resolution)
        end = last + timedelta(days=1) if through_next_00 else last
        return {
            "variable": [CDS_VARIABLES[n] for n in (names or self.hourly_names())],
            "date": f"{first:%Y-%m-%d}/{end:%Y-%m-%d}",
            "time": [f"{h:02d}:00" for h in range(24)],
            "area": snapped_area(box, self.resolution),
            "data_format": "netcdf", "download_format": "unarchived",
        }

    def missing_credentials(self, aoi: AOI, ref: SourceRef | None = None) -> list[str]:
        """``CDSAPI_URL`` / ``CDSAPI_KEY`` are needed for discover and for every unit."""
        return [n for n in ("CDSAPI_URL", "CDSAPI_KEY") if not self.settings.has_secret(n)]

    # ------------------------------------------------------------------ discover
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        cds_credentials(self.settings)   # one clear error for the whole dataset when credentials are missing
        import_cdsapi()
        end = self.data_end()
        refs = []
        for first in month_starts(period):
            if first > end:
                log.info("era5-land %s: %s not available yet (data end %s)", aoi.id, f"{first:%Y-%m}", end)
                continue
            last = min(month_end(first), end)
            complete = last == month_end(first)
            key = f"era5-land-{first:%Y%m}" + ("" if complete else f"-through-{last:%Y%m%d}")
            refs.append(self.ref(key, utc_midnight(first), [f"cds:{CDS_DATASET}/{first:%Y-%m}"],
                                 first_day=first.isoformat(), last_day=last.isoformat(), complete=complete,
                                 through_next_00=last < end, data_end=end.isoformat()))
        return refs

    # ------------------------------------------------------------------ extract
    def raw_name(self, aoi: AOI, first: date, group: str) -> str:
        return f"{aoi.id}_era5-land_{first:%Y%m}_{group}.nc"

    def _usable_raw(self, rel: str, names: list[str], first: date, end: datetime) -> bool:
        """A cached raw file is reused only if it is readable, has ``names`` and runs from ``first`` 00 UTC to
        ``end`` (an open month cached before newer days were published is requested again)."""
        if not self.lake.exists(rel):
            return False
        try:
            t = read_cds_netcdf(self.lake, rel, names)["time"].values
        except Exception as exc:
            log.warning("era5-land: cached %s unreadable (%s); requesting again", rel, redact(exc))
            return False
        want = np.array([utc_midnight(first).replace(tzinfo=None), end.replace(tzinfo=None)], dtype="datetime64[ns]")
        if t.size == 0 or t.min() != want[0] or t.max() != want[1]:
            log.info("era5-land: cached %s covers %s..%s, need %s..%s; requesting again", rel,
                     t.min() if t.size else None, t.max() if t.size else None, want[0], want[1])
            return False
        return True

    def wait(self, jobs: dict[str, Any], label: str) -> None:
        """Poll CDS jobs until all are ``successful``; log state changes; raise on a failed state or timeout."""
        poll = float(self.dataset.options.get("poll_initial_s", 5.0))
        poll_max = float(self.dataset.options.get("poll_max_s", 60.0))
        max_wait = float(self.dataset.options.get("max_wait_s", 4 * 3600))
        t0 = _time.monotonic()
        states: dict[str, str] = {}
        pending = dict(jobs)
        while pending:
            for group, remote in list(pending.items()):
                try:
                    state = str(remote.status)
                except Exception as exc:  # transient HTTP error while polling: keep waiting
                    log.warning("era5-land %s %s: status check failed (%s)", label, group, redact(exc))
                    continue
                if states.get(group) != state:
                    log.info("era5-land %s %s: job %s %s after %.0f s", label, group,
                             getattr(remote, "request_id", "?"), state, _time.monotonic() - t0)
                    states[group] = state
                if state == "successful":
                    pending.pop(group)
                elif state in FAILED_STATES:
                    detail = ""
                    try:
                        remote.get_results()
                    except Exception as exc:
                        detail = f": {redact(exc)}"
                    raise CdsJobError(f"era5-land {label} {group}: CDS job {state}{detail}")
                elif state not in PENDING_STATES:
                    log.warning("era5-land %s %s: unknown CDS state %r, still waiting", label, group, state)
            if pending:
                if _time.monotonic() - t0 > max_wait:
                    raise CdsJobError(f"era5-land {label}: CDS jobs {sorted(pending)} not finished after "
                                      f"{max_wait:.0f} s (states {states})")
                _time.sleep(poll)
                poll = min(poll * 1.5, poll_max)

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        """One CDS job per variable group for the month, submitted together, polled, downloaded to ``raw/``."""
        first, last = date.fromisoformat(ref.meta["first_day"]), date.fromisoformat(ref.meta["last_day"])
        through = bool(ref.meta.get("through_next_00", True))
        groups = self.active_groups()
        rels = {g: self.raw_path(ref, self.raw_name(aoi, first, g)) for g in groups}
        # the ``time`` list applies to every date, so a request through D+1 returns D+1 00..23 UTC
        end = utc_midnight(last + timedelta(days=1 if through else 0)) + timedelta(hours=23)
        todo = {g: names for g, names in groups.items() if not self._usable_raw(rels[g], names, first, end)}
        label = f"{aoi.id} {first:%Y-%m}"
        if todo:
            url, key = cds_credentials(self.settings)
            cdsapi = import_cdsapi()
            client = cdsapi.Client(url=url, key=key, quiet=True, progress=False, wait_until_complete=False)
            jobs = {}
            for g, names in todo.items():
                req = self.request(aoi, first, last, names, through_next_00=through)
                log.info("era5-land %s %s: submitting %d variables, %s, area %s", label, g, len(names),
                         req["date"], req["area"])
                jobs[g] = client.retrieve(CDS_DATASET, req)
            self.wait(jobs, label)
            for g, remote in jobs.items():
                with self.lake.local_path(rels[g], "w") as path:
                    remote.download(str(path))
                log.info("era5-land %s %s: downloaded", label, g)
        return [rels[g] for g in groups]

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        first, last = date.fromisoformat(ref.meta["first_day"]), date.fromisoformat(ref.meta["last_day"])
        groups = self.active_groups()
        if len(raw) != len(groups):
            raise RuntimeError(f"era5-land: expected {len(groups)} raw files (groups {list(groups)}), got {len(raw)}")
        hourly = merge_groups([read_cds_netcdf(self.lake, rel, names)
                               for rel, names in zip(raw, groups.values(), strict=True)])
        hourly = clip_lonlat(hourly, aoi.bbox, self.resolution, int(self.dataset.options.get("buffer_cells", 1)))
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        rules = self.rules()
        ds, tp_23h = daily_aggregate(hourly, rules, days)
        if ds.sizes.get("time", 0) != len(days):
            raise RuntimeError(f"era5-land: {ds.sizes.get('time', 0)} days in the response, expected {len(days)}")
        masked = n_valid = 0
        masked_by_var: dict[str, int] = {}
        for v in self.dataset.variables:
            raw_vals = ds[v.name].values
            vals, _ = mask_out_of_range(raw_vals, v.valid_range)
            nv = int(np.isfinite(raw_vals).sum())
            masked_by_var[v.name] = nv - int(np.isfinite(vals).sum())
            masked += masked_by_var[v.name]
            n_valid += nv
            src, rule = rules[v.name]
            ds[v.name] = (("time", "y", "x"), vals.astype("float32"))
            ds[v.name].attrs = {"units": v.unit or "", "long_name": v.description or v.name,
                                "cds_variable": ", ".join(CDS_VARIABLES[n] for n in DERIVED.get(src, (src,))),
                                "daily_rule": RULE_TEXT[rule]}
        ds = finalize_lonlat_dataset(ds, attrs={
            "title": "ERA5-Land daily aggregates (AOI subset)", "source": f"Copernicus CDS {CDS_DATASET}",
            "license": "CC-BY-4.0", "attribution": "Contains modified Copernicus Climate Change Service information",
            "time_convention": "UTC days (00-24 UTC); time = D at 00:00",
            "fuellake_aoi": aoi.id,
        })
        iid = self.item_id(aoi, ref)
        path = self.zarr_path(aoi, iid)
        write_zarr(self.lake, path, ds)
        lastd = date.fromisoformat(str(ds["time"].values.astype("datetime64[D]").max()))
        has_tp = any(rule == "accum" for _, rule in rules.values())
        props = {"fuellake:qc_masked_fraction": (masked / n_valid) if n_valid else 0.0,
                 "era5_land:qc_masked_cells": masked_by_var,
                 "era5_land:first_day": first.isoformat(), "era5_land:last_day": lastd.isoformat(),
                 "era5_land:complete_month": bool(ref.meta.get("complete")),
                 "era5_land:tp_last_day_23h": bool(tp_23h and has_tp),
                 "era5_land:request_groups": {g: list(n) for g, n in groups.items()},
                 "era5_land:expver": sorted({e for rel in raw for e in raw_expver(self.lake, rel)}),
                 "era5_land:shape": [int(ds.sizes["time"]), int(ds.sizes["y"]), int(ds.sizes["x"])]}
        return [StagedItem(item_id=iid, datetime=utc_midnight(first), properties=props,
                           assets=[self.zarr_asset(path)], bbox=cell_bounds(ds, self.resolution),
                           start_datetime=utc_midnight(first),
                           end_datetime=utc_midnight(lastd + timedelta(days=1)) - timedelta(microseconds=1))]
