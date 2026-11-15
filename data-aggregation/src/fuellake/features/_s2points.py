# fuel-lake — Sentinel-2 L2A NDVI / NDMI at points (STAC search, small COG window reads, SCL mask)
# Licensed under Apache-2.0.
"""Per-point Sentinel-2 indices for :mod:`fuellake.features.points`; no scene is staged.

Rules
-----
Search
    Element 84 Earth Search STAC (``stac_api`` of the ``sentinel2-indices`` config). Point-dates are binned by
    date (``search_bin_days``, default 8); one search per bin and collection with ``intersects`` = the bin's
    distinct locations as a MultiPoint (at most ``search_max_points`` per request) and ``datetime`` =
    ``[first date - window_days, last date]`` (``+ window_days`` when not causal), ``search_workers`` requests in
    parallel. Collections, in priority order:
    ``sentinel-2-c1-l2a`` then the legacy ``sentinel-2-l2a``. Checked live on 2026-11-14: Collection 1 has no
    items over the Santa Monica Mountains in July of 2016, 2017 and 2022, the legacy collection has 2017-2024.
    A legacy scene is used only for a (MGRS tile, UTC date) without a Collection 1 scene; among legacy
    duplicates (``_0_`` / ``_1_`` reprocessings) the highest processing baseline wins.
Candidates
    Scenes whose footprint contains the point and whose UTC date lies within ``window_days`` of the point date,
    only on or before it when ``causal`` (a same-day scene is allowed; ``s2_dt_days`` = 0 then, and the
    overpass, about 18:30 UTC over California, may be after an ignition: filter on it if needed). Ordered by
    ``|scene date - point date|``, ties to the earlier scene.
Acceptance and values
    Candidates are tried nearest first. A scene is accepted when the SCL class of the 20 m pixel containing the
    point is clear (``scl_mask_classes`` of the ``sentinel2-indices`` config are masked; class 0 always). The
    index footprint is a ``footprint_m`` square (default 30 m) centred on the point: the native pixels whose
    centres fall inside it (10 m bands: 3 x 3; 20 m bands: 1-2 per axis; always at least the pixel containing the
    point) that are clear (SCL looked up at each pixel centre) and have DN > 0. Reflectances are averaged over
    those pixels, then ``NDVI = (B08 - B04) / (B08 + B04)`` and ``NDMI = (B8A - B11) / (B8A + B11)``, with the
    scale and offset of each item (:func:`fuellake.connectors.sentinel2.band_scale_offset`, which also handles
    the legacy offset quirk). A scene whose footprint has no valid pixel counts as cloudy.
Statuses (``s2_status``)
    ``ok``, ``cloudy`` (every candidate masked at the point), ``no_scene`` (no candidate), ``error`` (a read
    failed and no later candidate succeeded). ``not_sampled`` is set by the caller for points outside the cap.
Work grouping
    Rounds: in round ``k`` every unresolved point tries its ``k``-th candidate. Within a round the points are
    grouped by scene (tile and date), each scene's COGs are opened once and read only in small windows around its
    points (GDAL fetches only the COG blocks they touch and caches them for the other points of the scene);
    scenes run in a thread pool (``max_workers``).
Cache
    Results ``ok`` / ``cloudy`` / ``no_scene`` are cached per point-date in :class:`PointCache` (``no_scene`` only
    when the window ended more than 30 days ago, so late scenes are picked up); errors are not cached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from fuellake.config import USER_AGENT, redact

log = logging.getLogger("fuellake.features.s2points")

DEFAULT_API = "https://earth-search.aws.element84.com/v1"
DEFAULT_COLLECTIONS = ("sentinel-2-c1-l2a", "sentinel-2-l2a")
DEFAULT_MASK_CLASSES = (0, 1, 2, 3, 6, 8, 9, 10, 11)
BANDS = ("red", "nir", "nir08", "swir16", "scl")
STATUSES = ("ok", "cloudy", "no_scene", "error", "not_sampled")


@dataclass
class Scene:
    id: str
    collection: str
    day: date
    epsg: int | None
    tile: str
    baseline: float
    geometry: Any                       # shapely geometry (EPSG:4326)
    item: dict                          # slim item: properties + assets with href and raster:bands


@dataclass
class S2Stats:
    point_dates: int = 0
    cache_hits: int = 0
    stac_requests: int = 0
    scenes_found: int = 0
    scene_tasks: int = 0
    point_scene_reads: int = 0          # (point, scene) pairs read (SCL at least)
    band_reads: int = 0                 # (point, scene) pairs whose bands were read
    rounds: int = 0
    errors: int = 0
    search_seconds: float = 0.0
    read_seconds: float = 0.0
    status_counts: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        total = self.search_seconds + self.read_seconds
        d["seconds"] = round(total, 2)
        d["search_seconds"] = round(self.search_seconds, 2)
        d["read_seconds"] = round(self.read_seconds, 2)
        done = self.point_dates - self.cache_hits
        d["point_dates_per_minute"] = round(60.0 * done / total, 1) if total > 0 and done > 0 else None
        d["seconds_per_point_scene"] = round(self.read_seconds / self.point_scene_reads, 3) \
            if self.point_scene_reads else None
        return d


def _utc_day(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).date()


def _tile_of(item: dict) -> str:
    p = item.get("properties", {})
    if p.get("mgrs:utm_zone") is not None:
        return f"{int(p['mgrs:utm_zone']):02d}{p.get('mgrs:latitude_band', '')}{p.get('mgrs:grid_square', '')}"
    if p.get("s2:mgrs_tile"):
        return str(p["s2:mgrs_tile"])
    return item["id"]


def _baseline(item: dict) -> float:
    try:
        return float(item.get("properties", {}).get("s2:processing_baseline"))
    except (TypeError, ValueError):
        return 0.0


def slim(item: dict, collection: str) -> Scene | None:
    """A :class:`Scene` from a STAC item dict, or None when a needed asset is missing."""
    from shapely.geometry import shape

    assets = item.get("assets", {})
    if not all(k in assets for k in BANDS) or not item.get("geometry"):
        return None
    p = item.get("properties", {})
    keep = ("datetime", "s2:processing_baseline", "earthsearch:boa_offset_applied", "proj:epsg", "platform")
    slim_item = {"id": item["id"], "properties": {k: p[k] for k in keep if k in p},
                 "assets": {k: {"href": assets[k]["href"], "raster:bands": assets[k].get("raster:bands")}
                            for k in BANDS}}
    epsg = p.get("proj:epsg")
    if epsg is None and p.get("mgrs:utm_zone") is not None:
        epsg = 32600 + int(p["mgrs:utm_zone"]) if str(p.get("mgrs:latitude_band", "N")) >= "N" \
            else 32700 + int(p["mgrs:utm_zone"])
    return Scene(id=item["id"], collection=collection, day=_utc_day(p["datetime"]), epsg=int(epsg) if epsg else None,
                 tile=_tile_of(item), baseline=_baseline(item), geometry=shape(item["geometry"]), item=slim_item)


def merge_collections(scenes_by_collection: dict[str, list[Scene]], priority: Sequence[str]) -> list[Scene]:
    """Scenes of the first collection, plus later collections only for (tile, date) without an earlier one;
    within a later collection one scene per (tile, date): the highest processing baseline (then id)."""
    out: list[Scene] = []
    seen: set[tuple[str, date]] = set()
    for coll in priority:
        scenes = scenes_by_collection.get(coll, [])
        if coll == priority[0]:
            kept = {s.id: s for s in scenes}.values()
        else:
            best: dict[tuple[str, date], Scene] = {}
            for s in scenes:
                k = (s.tile, s.day)
                if k in seen:
                    continue
                if k not in best or (s.baseline, s.id) > (best[k].baseline, best[k].id):
                    best[k] = s
            kept = best.values()
        for s in kept:
            out.append(s)
        seen |= {(s.tile, s.day) for s in kept}
    return sorted(out, key=lambda s: (s.day, s.id))


# ---------------------------------------------------------------------------- pixel maths
def footprint_window(x: float, y: float, transform, width: int, height: int, half_m: float):
    """``(row0, row1, col0, col1)`` (inclusive) of the pixels whose centres lie within ``half_m`` of (x, y) along
    both axes (north-up grid), always including the pixel containing the point; None if that pixel is off the
    raster."""
    a, c, e, f = transform.a, transform.c, transform.e, transform.f
    col = math.floor((x - c) / a)
    row = math.floor((y - f) / e)
    if not (0 <= col < width and 0 <= row < height):
        return None
    c0 = math.ceil((x - half_m - c) / a - 0.5)
    c1 = math.floor((x + half_m - c) / a - 0.5)
    r0 = math.ceil((y + half_m - f) / e - 0.5)            # e < 0: north edge first
    r1 = math.floor((y - half_m - f) / e - 0.5)
    c0, c1 = min(c0, col), max(c1, col)
    r0, r1 = min(r0, row), max(r1, row)
    return max(r0, 0), min(r1, height - 1), max(c0, 0), min(c1, width - 1)


def mean_index(a_refl: np.ndarray, b_refl: np.ndarray, clear: np.ndarray) -> tuple[float, int]:
    """Normalised difference of the mean reflectances over clear pixels with valid a and b; (NaN, 0) if none."""
    ok = clear & np.isfinite(a_refl) & np.isfinite(b_refl)
    n = int(ok.sum())
    if n == 0:
        return float("nan"), 0
    a, b = float(a_refl[ok].mean()), float(b_refl[ok].mean())
    s = a + b
    return ((a - b) / s if s > 0 else float("nan")), n


# ---------------------------------------------------------------------------- sampler
class S2PointSampler:
    """Sentinel-2 NDVI / NDMI at point-dates (rules: module docstring)."""

    def __init__(self, *, api: str = DEFAULT_API, collections: Sequence[str] = DEFAULT_COLLECTIONS,
                 mask_classes: Iterable[int] = DEFAULT_MASK_CLASSES, window_days: int = 8, causal: bool = True,
                 footprint_m: float = 30.0, max_workers: int = 8, search_bin_days: int = 8,
                 search_max_points: int = 400, search_workers: int = 8, cache=None,
                 user_agent: str = USER_AGENT,
                 search_fn: Callable[[str, dict], list[dict]] | None = None,
                 open_fn: Callable[[str], Any] | None = None, today: date | None = None):
        self.api = api.rstrip("/")
        self.collections = tuple(collections)
        self.mask_classes = sorted({int(c) for c in mask_classes} | {0})
        self.window_days = int(window_days)
        self.causal = bool(causal)
        self.half_m = float(footprint_m) / 2.0
        self.max_workers = max(1, int(max_workers))
        self.search_bin_days = max(1, int(search_bin_days))
        self.search_max_points = max(1, int(search_max_points))
        self.cache = cache
        self.user_agent = user_agent
        self._search_fn = search_fn
        self._open_fn = open_fn
        self._local = threading.local()
        self.search_workers = max(1, int(search_workers))
        self.today = today or datetime.now(UTC).date()
        self.stats = S2Stats()
        tag = json.dumps([self.collections, self.mask_classes, self.window_days, self.causal, footprint_m])
        self.source_id = f"s2-point:{hashlib.sha1(tag.encode()).hexdigest()[:12]}"

    # ------------------------------------------------------------------ search
    def _search(self, collection: str, body: dict) -> list[dict]:
        if self._search_fn is not None:
            return self._search_fn(collection, body)
        client = getattr(self._local, "client", None)
        if client is None:                          # one pystac-client per thread (its HTTP session is not shared)
            from pystac_client import Client

            client = self._local.client = Client.open(self.api, headers={"User-Agent": self.user_agent})
        res = client.search(collections=[collection], intersects=body["intersects"], datetime=body["datetime"],
                            limit=100)
        return list(res.items_as_dicts())

    def find_scenes(self, lon: np.ndarray, lat: np.ndarray, days: list[date]) -> list[Scene]:
        """Scenes for all point-dates (search rules: module docstring); searches run in parallel
        (``search_workers``)."""
        t0 = time.perf_counter()
        order = sorted(range(len(days)), key=lambda i: days[i])
        bins: list[list[int]] = []
        for i in order:
            if bins and (days[i] - days[bins[-1][0]]).days < self.search_bin_days:
                bins[-1].append(i)
            else:
                bins.append([i])
        tasks: list[tuple[str, dict]] = []
        for b in bins:
            first, last = days[b[0]], days[b[-1]]
            t_start = first - timedelta(days=self.window_days)
            t_end = last if self.causal else last + timedelta(days=self.window_days)
            dt = f"{t_start:%Y-%m-%d}T00:00:00Z/{t_end:%Y-%m-%d}T23:59:59Z"
            locs = sorted({(round(float(lon[i]), 6), round(float(lat[i]), 6)) for i in b})
            for k in range(0, len(locs), self.search_max_points):
                chunk = locs[k : k + self.search_max_points]
                geom = ({"type": "Point", "coordinates": list(chunk[0])} if len(chunk) == 1
                        else {"type": "MultiPoint", "coordinates": [list(p) for p in chunk]})
                tasks += [(coll, {"intersects": geom, "datetime": dt}) for coll in self.collections]

        def run(task: tuple[str, dict]):
            coll, body = task
            try:
                return coll, self._search(coll, body), None
            except Exception as exc:  # one failed search must not stop the build
                return coll, [], exc

        with ThreadPoolExecutor(max_workers=self.search_workers) as pool:
            results = list(pool.map(run, tasks))
        found: dict[str, dict[str, Scene]] = {c: {} for c in self.collections}
        for (coll, items, exc), (_, body) in zip(results, tasks, strict=True):
            self.stats.stac_requests += 1
            if exc is not None:
                self.stats.errors += 1
                log.warning("sentinel-2 search %s %s failed: %s", coll, body["datetime"],
                            redact(f"{type(exc).__name__}: {exc}"))
                continue
            for it in items:
                sc = slim(it, coll)
                if sc is not None:
                    found[coll][sc.id] = sc
        scenes = merge_collections({c: list(v.values()) for c, v in found.items()}, self.collections)
        self.stats.scenes_found = len(scenes)
        self.stats.search_seconds += time.perf_counter() - t0
        return scenes

    def candidates(self, lon: float, lat: float, day: date, scenes: list[Scene]) -> list[Scene]:
        from shapely import Point

        p = Point(lon, lat)
        out = []
        for s in scenes:
            dt = (s.day - day).days
            if abs(dt) > self.window_days or (self.causal and dt > 0):
                continue
            if s.geometry.covers(p):
                out.append(s)
        return sorted(out, key=lambda s: (abs((s.day - day).days), (s.day - day).days > 0, -s.baseline, s.id))

    # ------------------------------------------------------------------ pixels
    def _open(self, href: str):
        if self._open_fn is not None:
            return self._open_fn(href)
        import rasterio

        return rasterio.open(href)

    def _read_band(self, scene: Scene, key: str, pts: list[tuple[int, float, float]]) -> dict[int, tuple]:
        """Reflectance windows of one band for the points ``(i, x, y)`` (scene CRS): ``{i: (refl, window,
        transform)}``; points whose footprint is off the raster are left out."""
        from rasterio.windows import Window

        from fuellake.connectors.sentinel2 import band_scale_offset, to_reflectance

        out: dict[int, tuple] = {}
        scale_offset = band_scale_offset(scene.item, key)
        with self._open(scene.item["assets"][key]["href"]) as src:
            for i, x, y in pts:
                w = footprint_window(x, y, src.transform, src.width, src.height, self.half_m)
                if w is None:
                    continue
                r0, r1, c0, c1 = w
                dn = src.read(1, window=Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1))
                out[i] = (to_reflectance(dn, *scale_offset), w, src.transform)
        return out

    def _read_scene(self, scene: Scene, pts: list[tuple[int, float, float]]) -> tuple[dict[int, dict], int, int]:
        """Results for the points of one scene: ``({i: {"status": ..., ...}}, point reads, band reads)``.

        Phase 1 reads the SCL window of every point; phase 2 reads the four bands for the clear points, one
        thread per band (each band file is opened once per scene)."""
        from pyproj import Transformer
        from rasterio.windows import Window

        from fuellake.connectors.sentinel2 import scl_clear

        out: dict[int, dict] = {}
        clear_pts: list[tuple[int, float, float]] = []
        try:
            with self._open(scene.item["assets"]["scl"]["href"]) as scl_src:
                tr = Transformer.from_crs("EPSG:4326", scl_src.crs, always_xy=True)
                st = scl_src.transform
                masks: dict[int, tuple[np.ndarray, int, int]] = {}
                for i, lon, lat in pts:
                    x, y = tr.transform(lon, lat)
                    win = footprint_window(x, y, st, scl_src.width, scl_src.height, self.half_m + 20.0)
                    if win is None:
                        out[i] = {"status": "outside"}
                        continue
                    r0, r1, c0, c1 = win
                    clear = scl_clear(scl_src.read(1, window=Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1)),
                                      self.mask_classes)
                    if not clear[math.floor((y - st.f) / st.e) - r0, math.floor((x - st.c) / st.a) - c0]:
                        out[i] = {"status": "cloudy"}
                        continue
                    masks[i] = (clear, r0, c0)
                    clear_pts.append((i, x, y))
            if clear_pts:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futs = {k: pool.submit(self._read_band, scene, k, clear_pts) for k in BANDS if k != "scl"}
                    bands = {k: f.result() for k, f in futs.items()}

            def clear_at(i: int, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
                clear, r0, c0 = masks[i]
                cc = np.floor((xs - st.c) / st.a).astype(int) - c0
                rr = np.floor((ys - st.f) / st.e).astype(int) - r0
                ok = (rr >= 0) & (rr < clear.shape[0]) & (cc >= 0) & (cc < clear.shape[1])
                res = np.zeros(xs.shape, dtype=bool)
                res[ok] = clear[rr[ok], cc[ok]]
                return res

            for i, _x, _y in clear_pts:
                vals: dict[str, float] = {}
                npx: dict[str, int] = {}
                for name, (ka, kb) in (("ndvi", ("nir", "red")), ("ndmi", ("nir08", "swir16"))):
                    if i not in bands[ka] or i not in bands[kb]:
                        vals[name], npx[name] = float("nan"), 0
                        continue
                    (ra, w, t), (rb, _, _) = bands[ka][i], bands[kb][i]
                    r0, r1, c0, c1 = w
                    gx, gy = np.meshgrid(t.c + t.a * (np.arange(c0, c1 + 1) + 0.5),
                                         t.f + t.e * (np.arange(r0, r1 + 1) + 0.5))
                    vals[name], npx[name] = mean_index(ra, rb, clear_at(i, gx, gy))
                if not (math.isfinite(vals["ndvi"]) or math.isfinite(vals["ndmi"])):
                    out[i] = {"status": "cloudy"}
                    continue
                out[i] = {"status": "ok", "ndvi": vals["ndvi"], "ndmi": vals["ndmi"], "scene": scene.id,
                          "day": scene.day.isoformat(), "n_px_ndvi": npx["ndvi"], "n_px_ndmi": npx["ndmi"]}
        except Exception as exc:  # any read failure: the scene's unresolved points are marked as errors
            log.warning("sentinel-2 %s: %s", scene.id, redact(f"{type(exc).__name__}: {exc}"))
            for i, _, _ in pts:
                if out.get(i, {}).get("status") != "ok":
                    out[i] = {"status": "error", "error": redact(str(exc))[:200]}
        return out, len(pts), len(clear_pts)

    # ------------------------------------------------------------------ main
    def sample(self, lon: Sequence[float], lat: Sequence[float], days: Sequence[date]) -> dict[str, np.ndarray]:
        """Per point-date: ``ndvi``, ``ndmi`` (float64), ``dt_days`` (float, NaN if none), ``scene`` and
        ``status`` (object arrays)."""
        from fuellake.connectors._stac_source import gdal_env
        from fuellake.features._point_sources import point_key

        lon = np.asarray(lon, dtype="float64")
        lat = np.asarray(lat, dtype="float64")
        days = list(days)
        n = len(days)
        out = {"ndvi": np.full(n, np.nan), "ndmi": np.full(n, np.nan), "dt_days": np.full(n, np.nan),
               "scene": np.full(n, None, dtype=object), "status": np.full(n, None, dtype=object)}
        self.stats.point_dates += n
        keys = [point_key(lon[i], lat[i], days[i].isoformat()) for i in range(n)]
        cached = self.cache.get_many(self.source_id, keys) if self.cache else {}
        todo = []
        for i in range(n):
            if keys[i] in cached and cached[keys[i]] is not None:
                self._fill(out, i, cached[keys[i]], days[i])
                self.stats.cache_hits += 1
            else:
                todo.append(i)
        if todo:
            with gdal_env(self.user_agent, CPL_VSIL_CURL_CACHE_SIZE=str(256 * 1024 * 1024)):
                self._run(out, todo, lon, lat, days, keys)
        counts: dict[str, int] = {}
        for s in out["status"]:
            counts[str(s)] = counts.get(str(s), 0) + 1
        self.stats.status_counts = counts
        return out

    def _fill(self, out: dict, i: int, res: dict, day: date) -> None:
        out["status"][i] = res["status"]
        if res["status"] == "ok":
            out["ndvi"][i] = np.nan if res.get("ndvi") is None else res["ndvi"]
            out["ndmi"][i] = np.nan if res.get("ndmi") is None else res["ndmi"]
            out["scene"][i] = res["scene"]
            out["dt_days"][i] = (date.fromisoformat(res["day"]) - day).days

    def _run(self, out: dict, todo: list[int], lon, lat, days, keys) -> None:
        scenes = self.find_scenes(lon[todo], lat[todo], [days[i] for i in todo])
        cands = {i: self.candidates(float(lon[i]), float(lat[i]), days[i], scenes) for i in todo}
        status = {i: ("no_scene" if not cands[i] else "cloudy") for i in todo}
        result: dict[int, dict] = {}
        pending = [i for i in todo if cands[i]]
        k = 0
        t0 = time.perf_counter()
        errored: set[int] = set()
        pool = ThreadPoolExecutor(max_workers=self.max_workers)
        while pending:
            self.stats.rounds += 1
            groups: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
            by_id: dict[str, Scene] = {}
            for i in pending:
                s = cands[i][k]
                groups[s.id].append((i, float(lon[i]), float(lat[i])))
                by_id[s.id] = s
            self.stats.scene_tasks += len(groups)
            futures = {sid: pool.submit(self._read_scene, by_id[sid], pts) for sid, pts in groups.items()}
            for _sid, fut in futures.items():
                res_map, n_reads, n_bands = fut.result()
                self.stats.point_scene_reads += n_reads
                self.stats.band_reads += n_bands
                if any(r["status"] == "error" for r in res_map.values()):
                    self.stats.errors += 1
                for i, res in res_map.items():
                    if res["status"] == "ok":
                        result[i] = res
                    elif res["status"] == "error":
                        errored.add(i)
            k += 1
            pending = [i for i in pending if i not in result and len(cands[i]) > k]
        pool.shutdown()
        self.stats.read_seconds += time.perf_counter() - t0
        fresh: dict[str, Any] = {}
        for i in todo:
            if i in result:
                res = result[i]
            elif i in errored:
                res = {"status": "error"}
            else:
                res = {"status": status[i]}
            self._fill(out, i, res, days[i])
            window_end = days[i] + timedelta(days=0 if self.causal else self.window_days)
            cacheable = res["status"] in ("ok", "cloudy") or (
                res["status"] == "no_scene" and (self.today - window_end).days > 30)
            if cacheable and i not in errored:
                fresh[keys[i]] = res
        if self.cache and fresh:
            self.cache.put_many(self.source_id, fresh)
