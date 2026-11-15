# fuel-lake — Sentinel-2 L2A NDVI and NDMI on the AOI grid (Earth Search STAC, windowed COG reads)
# Licensed under Apache-2.0.
"""Sentinel-2 L2A vegetation indices from Element 84 Earth Search (public COGs on AWS).

Source (verified 2026-11-14)
----------------------------
- STAC API ``https://earth-search.aws.element84.com/v1``, collection ``sentinel-2-c1-l2a`` (Collection 1,
  the current reprocessed L2A; the legacy ``sentinel-2-l2a`` collection is still online).
- Asset keys: ``red`` (B04, 10 m), ``nir`` (B08, 10 m), ``nir08`` (B8A, 20 m), ``swir16`` (B11, 20 m),
  ``scl`` (scene classification, 20 m); uint16 DN, nodata 0.
- Reflectance = DN x scale + offset, read **per item** from ``raster:bands`` (C1: scale 0.0001,
  offset -0.1, i.e. the product's ``BOA_ADD_OFFSET = -1000`` with quantification 10000 used since
  processing baseline 04.00). Guard: legacy ``sentinel-2-l2a`` items with
  ``earthsearch:boa_offset_applied: true`` store already-harmonised pixels (DN = C1 DN - 1000, checked on
  scene 2026-11-11 T11SLT) although their item ``raster:bands`` still say offset -0.1, so the offset is
  0 for them. Without ``raster:bands`` the offset is -0.1 for baseline >= 04.00, else 0.
- SCL classes (from the product's ``Scene_Classification_List``): 0 no data, 1 saturated/defective,
  2 cast shadow, 3 cloud shadow, 4 vegetation, 5 not vegetated, 6 water, 7 unclassified, 8 cloud medium
  probability, 9 cloud high probability, 10 thin cirrus, 11 snow/ice. Masked by default:
  ``[0, 1, 2, 3, 6, 8, 9, 10, 11]`` (option ``scl_mask_classes``). Water (6) is masked because its
  offset-corrected reflectances are near zero or negative, which gives NaN or spurious +-1 indices (seen
  over Santa Monica Bay); unclassified (7) is kept.

Unit of work: one UTC acquisition date for the AOI (all scenes of that date: neighbouring orbits,
datastrip splits, tiles). Key ``<collection>:<yyyymmdd>:<sha1 of the sorted scene ids>``, so a scene
that appears later for the same date re-processes the date (the item is then updated). ``discover``
returns dates **newest first**, so ``--limit N`` takes the N most recent dates.

Per date (``transform``)
------------------------
1. Read only the SCL window covering the AOI (20 m), mark clear pixels, and compute the AOI masked
   fraction on the 30 m grid (a cell is clear if any clear 20 m pixel falls in it; swath no-data counts
   as masked). Above ``max_masked_fraction`` (default 0.8) the date is skipped: nothing is staged, the
   decision is cached in ``raw/.../<date>__<hash>.decision.json`` so later runs skip it without reading
   pixels (delete the file or change the threshold/classes to re-evaluate). Such cached dates are reported
   as ``empty`` by the pipeline before any work (``cached_empty``) and do not use up ``--limit``.
2. Per scene: windowed native reads of the bands, DN 0 and masked SCL -> NaN (jointly for both bands of
   an index), reflectance from scale/offset, ``average`` to the AOI grid (contract: downsampling), then
   ``NDVI = (B08 - B04) / (B08 + B04)`` and ``NDMI = (B8A - B11) / (B8A + B11)``; NaN where the sum is
   <= 0.
3. Composite the scenes of the date first-valid (scene with the most clear cells first); values outside
   [-1, 1] are masked and counted in ``fuellake:qc_masked_fraction``.

Item properties: ``fuellake:aoi_masked_fraction``, ``fuellake:valid_fraction_by_variable`` (share of AOI
cells with a value in each COG), ``fuellake:qc_masked_fraction``,
``fuellake:scl_mask_classes``, ``fuellake:max_masked_fraction``, ``fuellake:s2_scenes`` (priority order),
``fuellake:s2_processing_baselines``, ``fuellake:boa_offset`` (per scene), ``constellation``,
``platform``, ``instruments``.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

from fuellake.catalog import iso
from fuellake.connectors._stac_source import (
    WindowRead,
    gdal_env,
    grid_bounds_in,
    read_window,
    search_items,
    window_for_bounds,
)
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.grid import Grid
from fuellake.rasters import mask_out_of_range, reproject_array, write_cog
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.sentinel2")

DEFAULT_API = "https://earth-search.aws.element84.com/v1"
DEFAULT_COLLECTION = "sentinel-2-c1-l2a"
ASSETS = {"red": "B04", "nir": "B08", "nir08": "B8A", "swir16": "B11", "scl": "SCL"}
SCL_CLASSES = {
    0: "no_data", 1: "saturated_defective", 2: "cast_shadow", 3: "cloud_shadow", 4: "vegetation",
    5: "not_vegetated", 6: "water", 7: "unclassified", 8: "cloud_medium_probability",
    9: "cloud_high_probability", 10: "thin_cirrus", 11: "snow_ice",
}
DEFAULT_MASK_CLASSES = (0, 1, 2, 3, 6, 8, 9, 10, 11)
DEFAULT_MAX_MASKED_FRACTION = 0.8
_KEEP_PROPS = ("datetime", "platform", "constellation", "s2:processing_baseline", "earthsearch:boa_offset_applied",
               "eo:cloud_cover", "s2:product_uri", "proj:epsg", "mgrs:utm_zone", "mgrs:latitude_band",
               "mgrs:grid_square")


# ---------------------------------------------------------------------------- pure helpers
def band_scale_offset(item: dict, asset_key: str) -> tuple[float, float]:
    """``(scale, offset)`` turning DN into surface reflectance for one asset of a STAC item dict."""
    props = item.get("properties", {})
    bands = (item.get("assets", {}).get(asset_key, {}).get("raster:bands") or [{}])
    scale = bands[0].get("scale")
    offset = bands[0].get("offset")
    scale = 1e-4 if scale is None else float(scale)
    if props.get("earthsearch:boa_offset_applied") is True:
        return scale, 0.0          # legacy collection: pixels already harmonised (see module docstring)
    if offset is None:
        try:
            baseline = float(props.get("s2:processing_baseline"))
        except (TypeError, ValueError):
            baseline = None
        offset = -0.1 if baseline is not None and baseline >= 4.0 else 0.0
    return scale, float(offset)


def to_reflectance(dn: np.ndarray, scale: float, offset: float) -> np.ndarray:
    """float32 reflectance; DN 0 (no data) -> NaN."""
    dn = np.asarray(dn)
    out = dn.astype("float32") * np.float32(scale) + np.float32(offset)
    out[dn == 0] = np.nan
    return out


def scl_clear(scl: np.ndarray, mask_classes=DEFAULT_MASK_CLASSES) -> np.ndarray:
    """True where the SCL class is not masked (class 0, no data, is always masked)."""
    scl = np.asarray(scl)
    return ~np.isin(scl, np.asarray(list(mask_classes) + [0], dtype=scl.dtype))


def normalized_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``(a - b) / (a + b)`` as float32; NaN where either input is NaN or ``a + b <= 0``."""
    a = np.asarray(a, dtype="float32")
    b = np.asarray(b, dtype="float32")
    s = a + b
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(s > 0, (a - b) / s, np.nan)
    return out.astype("float32")


def composite_first_valid(layers: list[np.ndarray]) -> np.ndarray:
    """Per pixel, the first finite value along ``layers``."""
    out = np.full(layers[0].shape, np.nan, dtype="float32")
    for layer in layers:
        fill = np.isnan(out) & np.isfinite(layer)
        out[fill] = layer[fill]
    return out


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def group_by_date(items: list[dict]) -> dict[date, list[dict]]:
    """Scenes grouped by UTC acquisition date, each group sorted by (datetime, id)."""
    groups: dict[date, list[dict]] = defaultdict(list)
    for it in items:
        groups[_dt(it["properties"]["datetime"]).date()].append(it)
    return {d: sorted(g, key=lambda it: (it["properties"]["datetime"], it["id"])) for d, g in sorted(groups.items())}


def unit_key(collection: str, day: date, scene_ids: list[str]) -> str:
    digest = hashlib.sha1(",".join(sorted(scene_ids)).encode()).hexdigest()[:12]
    return f"{collection}:{day:%Y%m%d}:{digest}"


def slim_item(item: dict) -> dict:
    """The parts of a STAC item the transform needs (kept in ``SourceRef.meta`` and in ``raw/``)."""
    props = item.get("properties", {})
    return {
        "id": item["id"],
        "properties": {k: props[k] for k in _KEEP_PROPS if k in props},
        "assets": {k: {"href": item["assets"][k]["href"], "raster:bands": item["assets"][k].get("raster:bands")}
                   for k in ASSETS if k in item.get("assets", {})},
    }


def _window_bounds(w: WindowRead) -> tuple[float, float, float, float]:
    t = w.transform
    h, wd = w.array.shape
    return (t.c, t.f + t.e * h, t.c + t.a * wd, t.f)


def _resample_mask(mask: np.ndarray, src: WindowRead, dst_transform, dst_shape) -> np.ndarray:
    """Nearest-neighbour copy of a boolean mask from the SCL window grid to a band window grid."""
    if mask.shape == tuple(dst_shape) and src.transform.almost_equals(dst_transform):
        return mask
    dst = np.zeros(dst_shape, dtype="uint8")
    reproject(mask.astype("uint8"), dst, src_transform=src.transform, src_crs=src.crs,
              dst_transform=dst_transform, dst_crs=src.crs, resampling=Resampling.nearest)
    return dst.astype(bool)


# ---------------------------------------------------------------------------- connector
class Sentinel2IndicesConnector(BaseConnector):
    """NDVI and NDMI per acquisition date on the AOI grid."""

    def _opts(self) -> dict:
        o = self.dataset.options
        return {
            "api": str(o.get("stac_api", DEFAULT_API)).rstrip("/"),
            "collection": o.get("collection", DEFAULT_COLLECTION),
            "mask_classes": [int(c) for c in o.get("scl_mask_classes", DEFAULT_MASK_CLASSES)],
            "max_masked_fraction": float(o.get("max_masked_fraction", DEFAULT_MAX_MASKED_FRACTION)),
        }

    def _search(self, bbox: tuple[float, float, float, float], interval: str) -> list[dict]:
        o = self._opts()
        return search_items(o["api"], o["collection"], bbox, datetime=interval, user_agent=self.settings.user_agent)

    # ------------------------------------------------------------------ discover / extract
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        o = self._opts()
        interval = f"{period.start:%Y-%m-%d}T00:00:00Z/{period.end:%Y-%m-%d}T23:59:59Z"
        items = [it for it in self._search(aoi.grid.bounds_wgs84(), interval)
                 if all(k in it.get("assets", {}) for k in ASSETS)]
        refs = []
        for day, scenes in group_by_date(items).items():
            if not period.start <= day <= period.end:
                continue
            ids = [s["id"] for s in scenes]
            urls = [f"{o['api']}/collections/{o['collection']}/items/{i}" for i in ids]
            dt = min(_dt(s["properties"]["datetime"]) for s in scenes)
            refs.append(self.ref(unit_key(o["collection"], day, ids), dt, urls=urls,
                                 scenes=[slim_item(s) for s in scenes], collection=o["collection"]))
        return sorted(refs, key=lambda r: r.datetime, reverse=True)

    def _stem(self, ref: SourceRef) -> str:
        return f"{ref.datetime:%Y%m%d}__{ref.key.rsplit(':', 1)[-1]}"

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        """Write the scene metadata used for this date (provenance; pixels are streamed in transform)."""
        rel = self.raw_path(ref, f"{self._stem(ref)}.scenes.json")
        self.lake.write_json(rel, {"key": ref.key, "collection": ref.meta.get("collection"),
                                   "scenes": list(ref.meta["scenes"])})
        return [rel]

    # ------------------------------------------------------------------ skip decisions
    def _decision_rel(self, ref: SourceRef) -> str:
        return self.raw_path(ref, f"{self._stem(ref)}.decision.json")

    def cached_empty(self, ref: SourceRef, aoi: AOI) -> bool:
        """True when a cached decision says this date is too cloudy: the pipeline then skips the unit without
        work, and it does not use up ``--limit``."""
        return self._cached_skip(ref, self._opts()) is not None

    def _cached_skip(self, ref: SourceRef, o: dict) -> dict | None:
        rel = self._decision_rel(ref)
        if not self.lake.exists(rel):
            return None
        d = self.lake.read_json(rel)
        same = (d.get("decision") == "skip" and d.get("key") == ref.key
                and d.get("max_masked_fraction") == o["max_masked_fraction"]
                and d.get("scl_mask_classes") == o["mask_classes"])
        return d if same else None

    # ------------------------------------------------------------------ pixels
    def _read_scl(self, scene: dict, grid: Grid, mask_classes: list[int]) -> dict | None:
        with rasterio.open(scene["assets"]["scl"]["href"]) as src:
            bounds = grid_bounds_in(grid, src.crs, pad_m=2 * grid.resolution)
            w = read_window(src, bounds, pad_px=2)
        if w is None:
            return None
        clear = scl_clear(w.array, mask_classes)
        clear_f = np.where(clear, 1.0, np.nan).astype("float32")
        clear30 = np.isfinite(reproject_array(clear_f, w.crs, w.transform, grid, "average"))
        return {"scene": scene, "scl": w, "clear": clear, "clear30": clear30, "n_clear": int(clear30.sum())}

    def _band(self, scene: dict, key: str, bounds) -> WindowRead:
        with rasterio.open(scene["assets"][key]["href"]) as src:
            win = window_for_bounds(src, bounds, pad_px=0)
            if win is None:
                raise ValueError(f"{scene['id']}/{key}: window outside the raster")
            return read_window(src, window=win)

    def _index(self, s: dict, keys: tuple[str, str], grid: Grid) -> np.ndarray:
        """(a - b) / (a + b) for one scene on the AOI grid, from jointly masked, averaged reflectances."""
        scene, scl = s["scene"], s["scl"]
        bounds = _window_bounds(scl)
        a_w, b_w = self._band(scene, keys[0], bounds), self._band(scene, keys[1], bounds)
        if a_w.array.shape != b_w.array.shape or not a_w.transform.almost_equals(b_w.transform):
            raise ValueError(f"{scene['id']}: {keys} windows are not aligned")
        clear = _resample_mask(s["clear"], scl, a_w.transform, a_w.array.shape)
        a = to_reflectance(a_w.array, *band_scale_offset(scene, keys[0]))
        b = to_reflectance(b_w.array, *band_scale_offset(scene, keys[1]))
        valid = clear & np.isfinite(a) & np.isfinite(b)
        a[~valid] = np.nan
        b[~valid] = np.nan
        a30 = reproject_array(a, a_w.crs, a_w.transform, grid, "average")
        b30 = reproject_array(b, b_w.crs, b_w.transform, grid, "average")
        return normalized_difference(a30, b30)

    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        o = self._opts()
        grid = aoi.grid
        cached = self._cached_skip(ref, o)
        if cached is not None:
            log.info("sentinel2: %s skipped (cached: masked fraction %.3f > %.3f)", ref.key,
                     cached.get("aoi_masked_fraction", float("nan")), o["max_masked_fraction"])
            return []
        with gdal_env(self.settings.user_agent):
            reads = [r for r in (self._read_scl(s, grid, o["mask_classes"]) for s in ref.meta["scenes"]) if r]
            clear_any = np.zeros(grid.shape, dtype=bool)
            for r in reads:
                clear_any |= r["clear30"]
            masked = float(1.0 - clear_any.mean())
            if masked > o["max_masked_fraction"]:
                self.lake.write_json(self._decision_rel(ref), {
                    "key": ref.key, "decision": "skip", "aoi_masked_fraction": round(masked, 6),
                    "max_masked_fraction": o["max_masked_fraction"], "scl_mask_classes": o["mask_classes"],
                    "evaluated_at": iso(datetime.now(UTC)),
                })
                log.info("sentinel2: %s skipped (masked fraction %.3f > %.3f)", ref.key, masked,
                         o["max_masked_fraction"])
                return []
            reads = sorted((r for r in reads if r["n_clear"]), key=lambda r: -r["n_clear"])
            ndvi_layers = [self._index(r, ("nir", "red"), grid) for r in reads]
            ndmi_layers = [self._index(r, ("nir08", "swir16"), grid) for r in reads]

        iid = self.item_id(aoi, ref)
        assets, qc, valid = [], {}, {}
        for name, layers in (("ndvi", ndvi_layers), ("ndmi", ndmi_layers)):
            var = self.variable(name)
            arr, qc[name] = mask_out_of_range(composite_first_valid(layers), var.valid_range)
            valid[name] = round(float(np.isfinite(arr).mean()), 6)
            path = self.cog_path(aoi, iid, name)
            write_cog(self.lake, path, arr, grid, np.nan, var.dtype, description=name, unit=var.unit)
            assets.append(self.cog_asset(name, path))

        used = [r["scene"] for r in reads]
        times = sorted(_dt(s["properties"]["datetime"]) for s in ref.meta["scenes"])
        props: dict[str, Any] = {
            "fuellake:qc_masked_fraction": max(qc.values()),
            "fuellake:aoi_masked_fraction": round(masked, 6),
            "fuellake:valid_fraction_by_variable": valid,
            "fuellake:max_masked_fraction": o["max_masked_fraction"],
            "fuellake:scl_mask_classes": o["mask_classes"],
            "fuellake:s2_collection": ref.meta.get("collection"),
            "fuellake:s2_scenes": [s["id"] for s in used],
            "fuellake:s2_processing_baselines": sorted({str(s["properties"].get("s2:processing_baseline"))
                                                        for s in used}),
            "fuellake:boa_offset": {s["id"]: band_scale_offset(s, "red")[1] for s in used},
            "fuellake:index_method": ("SCL-masked native reflectances averaged to the AOI grid, index per "
                                      "scene, scenes composited first-valid (most clear cells first)"),
            "constellation": "sentinel-2",
            "platform": used[0]["properties"].get("platform") if used else None,
            "instruments": ["msi"],
        }
        props = {k: v for k, v in props.items() if v is not None}
        start, end = (times[0], times[-1]) if times and times[0] != times[-1] else (None, None)
        return [StagedItem(item_id=iid, datetime=ref.datetime, properties=props, assets=assets,
                           start_datetime=start, end_datetime=end)]
