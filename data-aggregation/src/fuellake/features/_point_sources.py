# fuel-lake — point readers for remote static sources (LANDFIRE ImageServer, Copernicus DEM COGs) with a cache
# Licensed under Apache-2.0.
"""Read static layers at WGS84 points straight from their public remote sources, for points that lie outside
every AOI the layer is staged on (:mod:`fuellake.features.points`).

Readers
-------
:class:`ArcGisImageSampler`
    One ArcGIS ImageServer (LANDFIRE FBFM40, canopy, LF2020 topography). ``getSamples`` with a multipoint
    geometry in EPSG:4326, ``RSP_NearestNeighbor``, sent as a form POST in batches of ``batch`` points (default 500;
    ``max_workers`` batches in parallel). Verified live on 2026-11-14 against ``lfps.usgs.gov``: the server answers
    at most 1000 locations per request (a 2000-point request returned ``locationId`` 0..999 only), so batches stay
    well below that; locations whose pixel is NoData (open ocean) are **left out** of the response (checked with
    ``identify``, which reports ``NoData`` for them), so a missing ``locationId`` is nodata. Values are mapped by
    ``locationId``, never by position; a non-numeric value (``NoData``) and the service ``noDataValue`` become NaN.
    The centre-pixel values equal ``exportImage`` clips of the same service (checked on six points).
:class:`CopDemPointReader`
    Copernicus DEM GLO-30 public COGs on AWS (``copernicus-dem-30m``), tile chosen from the point's integer
    degree (``Copernicus_DSM_COG_10_N34_00_W119_00_DEM``). For each point a 3 x 3 window of the native 1 arc-second
    grid is read: ``elevation`` is the pixel containing the point, ``slope`` / ``aspect`` come from
    :func:`fuellake.connectors.cop_dem.slope_aspect` (Horn 1981) with the cell size in metres at the point's
    latitude. Aspect is therefore relative to true north (the staged ``cop-dem`` items use grid north of the AOI
    CRS, about 1 degree apart over ``smm``). A missing tile (HTTP 404: open ocean) is nodata. A point on a tile
    edge has no slope (its 3 x 3 window is incomplete).

Cache (:class:`PointCache`)
    SQLite file outside the lake (``$FUELLAKE_CACHE_DIR/point-samples/cache.sqlite``), key ``(source, key)``
    with ``key = "<round(lon * 1e6)>,<round(lat * 1e6)>"`` (about 0.1 m) plus, for date-dependent sources, the
    date. Genuine nodata is cached (as JSON ``null``); a failed request never is, so a rerun retries it.

Failures: a batch that still fails after ``retries`` attempts marks its points as failed (``failed`` mask), so
the caller can record ``error`` instead of nodata. Requests send ``User-Agent: fuel-lake/0.1`` and nothing else
identifying.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from fuellake.config import USER_AGENT, Settings, redact

log = logging.getLogger("fuellake.features.point_sources")

GETSAMPLES_MAX = 1000          # verified server limit per request (see the module docstring)
DEFAULT_BATCH = 500
COPDEM_BUCKET = "https://copernicus-dem-30m.s3.amazonaws.com"


# ---------------------------------------------------------------------------- cache
def point_key(lon: float, lat: float, extra: str | None = None) -> str:
    key = f"{int(round(float(lon) * 1e6))},{int(round(float(lat) * 1e6))}"
    return f"{key},{extra}" if extra else key


class PointCache:
    """``(source, key) -> JSON value`` in a SQLite file (thread-safe; one connection per call)."""

    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = Settings.from_env().cache_path("point-samples", "cache.sqlite")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS samples (source TEXT NOT NULL, key TEXT NOT NULL, "
                        "value TEXT, PRIMARY KEY (source, key))")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=60)

    def get_many(self, source: str, keys: Sequence[str]) -> dict[str, Any]:
        """Cached values of ``keys`` (absent keys are not in the result; cached nodata is ``None``)."""
        out: dict[str, Any] = {}
        if not keys:
            return out
        with self._lock, self._connect() as con:
            for i in range(0, len(keys), 500):
                chunk = list(keys[i : i + 500])
                q = f"SELECT key, value FROM samples WHERE source = ? AND key IN ({','.join('?' * len(chunk))})"
                for k, v in con.execute(q, [source, *chunk]):
                    out[k] = json.loads(v) if v is not None else None
        return out

    def put_many(self, source: str, values: dict[str, Any]) -> None:
        if not values:
            return
        rows = [(source, k, json.dumps(v)) for k, v in values.items()]
        with self._lock, self._connect() as con:
            con.executemany("INSERT OR REPLACE INTO samples (source, key, value) VALUES (?, ?, ?)", rows)


def _clean(v: Any) -> Any:
    """JSON-safe value: NaN -> None."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    return v


# ---------------------------------------------------------------------------- HTTP
def _session(user_agent: str = USER_AGENT):
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": user_agent})
    return s


def _with_retries(fn: Callable[[], Any], retries: int, what: str) -> Any:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:  # network / server errors: retry, then give up
            last = exc
            if attempt < retries:
                time.sleep(min(2.0 * (attempt + 1), 10.0))
    raise RuntimeError(f"{what}: {redact(f'{type(last).__name__}: {last}')}") from last


# ---------------------------------------------------------------------------- ArcGIS ImageServer
def parse_samples(payload: dict, n: int, nodata: float | None) -> np.ndarray:
    """Values (float64, NaN for nodata) of a ``getSamples`` response for ``n`` requested locations, mapped by
    ``locationId``; locations left out of the response are nodata."""
    if "samples" not in payload:
        raise ValueError(f"getSamples: unexpected response {str(payload)[:200]}")
    out = np.full(n, np.nan)
    for s in payload["samples"]:
        i = int(s.get("locationId", -1))
        if not 0 <= i < n:
            continue
        raw = str(s.get("value", "")).strip().split(" ")[0]
        try:
            v = float(raw)
        except ValueError:
            continue                      # "NoData" and other non-numeric values
        if not math.isfinite(v) or (nodata is not None and v == nodata):
            continue
        out[i] = v
    return out


class ArcGisImageSampler:
    """Values of one ArcGIS ImageServer at WGS84 points (see the module docstring)."""

    def __init__(self, service: str, *, nodata: float | None = -9999, cache: PointCache | None = None,
                 batch: int = DEFAULT_BATCH, retries: int = 2, timeout: float = 120.0,
                 user_agent: str = USER_AGENT, session=None, max_workers: int = 1):
        if not 1 <= batch <= GETSAMPLES_MAX:
            raise ValueError(f"batch must be 1..{GETSAMPLES_MAX}")
        self.service = service.rstrip("/")
        self.nodata = nodata
        self.cache = cache
        self.batch = int(batch)
        self.retries = int(retries)
        self.timeout = float(timeout)
        self.max_workers = max(1, int(max_workers))
        self._session = session or _session(user_agent)
        self.source_id = f"arcgis:{self.service}"
        self.stats = {"requests": 0, "points_requested": 0, "cache_hits": 0, "failed_points": 0, "seconds": 0.0}

    def _request(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        geom = {"points": [[round(float(a), 7), round(float(b), 7)] for a, b in zip(lon, lat, strict=True)],
                "spatialReference": {"wkid": 4326}}
        data = {"geometry": json.dumps(geom, separators=(",", ":")), "geometryType": "esriGeometryMultipoint",
                "returnFirstValueOnly": "true", "interpolation": "RSP_NearestNeighbor", "f": "json"}

        def call():
            r = self._session.post(f"{self.service}/getSamples", data=data, timeout=self.timeout)
            r.raise_for_status()
            payload = r.json()
            if "error" in payload:
                raise RuntimeError(f"service error {payload['error']}")
            return parse_samples(payload, len(lon), self.nodata)

        return _with_retries(call, self.retries, f"getSamples {self.service}")

    def sample(self, lon: Iterable[float], lat: Iterable[float]) -> tuple[np.ndarray, np.ndarray]:
        """``(values float64 with NaN for nodata, failed bool mask)`` at the points."""
        t0 = time.perf_counter()
        lon = np.asarray(lon, dtype="float64")
        lat = np.asarray(lat, dtype="float64")
        n = len(lon)
        values = np.full(n, np.nan)
        failed = np.zeros(n, dtype=bool)
        ok = np.isfinite(lon) & np.isfinite(lat)
        keys = [point_key(a, b) if k else "" for a, b, k in zip(lon, lat, ok, strict=True)]
        cached = self.cache.get_many(self.source_id, [k for k in keys if k]) if self.cache else {}
        todo = [i for i in range(n) if ok[i] and keys[i] not in cached]
        for i in range(n):
            if ok[i] and keys[i] in cached:
                v = cached[keys[i]]
                values[i] = np.nan if v is None else float(v)
        self.stats["cache_hits"] += n - len(todo) - int((~ok).sum())
        batches = [np.asarray(todo[b : b + self.batch]) for b in range(0, len(todo), self.batch)]

        def run(idx: np.ndarray):
            try:
                return idx, self._request(lon[idx], lat[idx]), None
            except RuntimeError as exc:
                return idx, None, exc

        if self.max_workers > 1 and len(batches) > 1:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                results = list(pool.map(run, batches))
        else:
            results = [run(b) for b in batches]
        for idx, vals, exc in results:
            self.stats["requests"] += 1
            self.stats["points_requested"] += len(idx)
            if exc is not None:
                log.warning("%s", exc)
                failed[idx] = True
                self.stats["failed_points"] += len(idx)
                continue
            values[idx] = vals
            if self.cache:
                self.cache.put_many(self.source_id, {keys[i]: _clean(float(v)) for i, v in zip(idx, vals,
                                                                                               strict=True)})
        self.stats["seconds"] += time.perf_counter() - t0
        return values, failed


# ---------------------------------------------------------------------------- Copernicus DEM
def copdem_tile(lon: float, lat: float) -> str:
    """Copernicus DEM GLO-30 tile name of the 1 x 1 degree cell containing the point."""
    la, lo = math.floor(lat), math.floor(lon)
    ns = f"N{la:02d}" if la >= 0 else f"S{-la:02d}"
    ew = f"E{lo:03d}" if lo >= 0 else f"W{-lo:03d}"
    return f"Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM"


def copdem_href(tile: str) -> str:
    return f"{COPDEM_BUCKET}/{tile}/{tile}.tif"


def metres_per_degree(lat: float) -> tuple[float, float]:
    """(metres per degree of longitude, of latitude) on the WGS84 ellipsoid at ``lat`` (series expansion)."""
    p = math.radians(lat)
    m_lat = 111132.954 - 559.822 * math.cos(2 * p) + 1.175 * math.cos(4 * p)
    m_lon = 111412.84 * math.cos(p) - 93.5 * math.cos(3 * p) + 0.118 * math.cos(5 * p)
    return m_lon, m_lat


def terrain_from_window(z: np.ndarray, lat: float, res_deg_x: float, res_deg_y: float) -> tuple[float, float, float]:
    """(elevation, slope, aspect) of the centre of a 3 x 3 north-up window on a geographic grid."""
    from fuellake.connectors.cop_dem import slope_aspect

    z = np.asarray(z, dtype="float64")
    if z.shape != (3, 3):
        centre = float(z[z.shape[0] // 2, z.shape[1] // 2]) if z.size else float("nan")
        return centre, float("nan"), float("nan")
    m_lon, m_lat = metres_per_degree(lat)
    slope, aspect = slope_aspect(z, abs(res_deg_x) * m_lon, abs(res_deg_y) * m_lat)
    return float(z[1, 1]), float(slope[1, 1]), float(aspect[1, 1])


class CopDemPointReader:
    """Elevation, slope and aspect at WGS84 points from the public Copernicus DEM GLO-30 COGs."""

    source_id = "cop-dem-glo-30:point-3x3"
    variables = ("elevation", "slope", "aspect")

    def __init__(self, *, cache: PointCache | None = None, href_for: Callable[[str], str] = copdem_href,
                 user_agent: str = USER_AGENT, retries: int = 2):
        self.cache = cache
        self.href_for = href_for
        self.user_agent = user_agent
        self.retries = retries
        self.stats = {"tiles": 0, "points_read": 0, "cache_hits": 0, "failed_points": 0, "seconds": 0.0}

    def _read_tile(self, tile: str, pts: list[tuple[int, float, float]]) -> dict[int, tuple | None]:
        import rasterio
        from rasterio.errors import RasterioIOError
        from rasterio.windows import Window

        from fuellake.connectors._stac_source import gdal_env

        href = self.href_for(tile)
        out: dict[int, tuple | None] = {}

        def opener():
            try:
                return rasterio.open(href)
            except RasterioIOError as exc:
                if _is_missing(exc):
                    return None                                  # no tile: open ocean (not retried)
                raise

        with gdal_env(self.user_agent):
            src = _with_retries(opener, self.retries, f"open {tile}")
            if src is None:
                return {i: None for i, _, _ in pts}
            with src:
                inv = ~src.transform
                nod = src.nodata
                for i, lon, lat in pts:
                    c, r = inv * (lon, lat)
                    col, row = int(math.floor(c)), int(math.floor(r))
                    win = Window(col - 1, row - 1, 3, 3)
                    try:
                        z = src.read(1, window=win, boundless=True, fill_value=np.nan).astype("float64")
                    except RasterioIOError as exc:
                        raise RuntimeError(f"read {tile}: {exc}") from exc
                    if nod is not None and not (isinstance(nod, float) and math.isnan(nod)):
                        z[z == nod] = np.nan
                    elev, slope, aspect = terrain_from_window(z, lat, src.transform.a, src.transform.e)
                    out[i] = None if not math.isfinite(elev) else (elev, slope, aspect)
        return out

    def sample(self, lon: Iterable[float], lat: Iterable[float]) -> tuple[dict[str, np.ndarray], np.ndarray]:
        t0 = time.perf_counter()
        lon = np.asarray(lon, dtype="float64")
        lat = np.asarray(lat, dtype="float64")
        n = len(lon)
        out = {v: np.full(n, np.nan) for v in self.variables}
        failed = np.zeros(n, dtype=bool)
        keys = [point_key(a, b) for a, b in zip(lon, lat, strict=True)]
        cached = self.cache.get_many(self.source_id, keys) if self.cache else {}
        by_tile: dict[str, list[tuple[int, float, float]]] = {}
        for i in range(n):
            if not (math.isfinite(lon[i]) and math.isfinite(lat[i])):
                continue
            if keys[i] in cached:
                self.stats["cache_hits"] += 1
                v = cached[keys[i]]
                if v is not None:
                    for name, x in zip(self.variables, v, strict=True):
                        out[name][i] = np.nan if x is None else float(x)
                continue
            by_tile.setdefault(copdem_tile(lon[i], lat[i]), []).append((i, float(lon[i]), float(lat[i])))
        for tile, pts in sorted(by_tile.items()):
            self.stats["tiles"] += 1
            try:
                res = self._read_tile(tile, pts)
            except RuntimeError as exc:
                log.warning("%s", exc)
                for i, _, _ in pts:
                    failed[i] = True
                self.stats["failed_points"] += len(pts)
                continue
            self.stats["points_read"] += len(pts)
            for i, v in res.items():
                if v is not None:
                    for name, x in zip(self.variables, v, strict=True):
                        out[name][i] = x
            if self.cache:
                self.cache.put_many(self.source_id, {keys[i]: (_clean([float(x) for x in v]) if v else None)
                                                     for i, v in res.items()})
        self.stats["seconds"] += time.perf_counter() - t0
        return out, failed


def _is_missing(exc: Exception) -> bool:
    text = str(exc)
    return any(s in text for s in ("404", "does not exist", "No such file", "not recognized as a supported"))
