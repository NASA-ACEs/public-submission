# fuel-lake — connector: NISAR L3 SME2 soil moisture (200 m EASE-Grid 2.0) on the AOI grid
# Licensed under Apache-2.0.
"""NISAR L3 SME2 -> ``soil_moisture`` (one algorithm per series), ``soil_moisture_uncertainty``,
``soil_moisture_quality_flag``, the three algorithm candidates ``soil_moisture_dsg`` / ``_pmi`` / ``_tsr`` and
``sm_algorithm_spread`` on the AOI grid.

Collection: ``NISAR_L3_SME2_PROVISIONAL_V1`` = ``C2854344945-ASF`` (CRID P05023; no VALIDATED SME2 collection
yet). Discovery is shared with GCOV (:func:`fuellake.connectors._earthdata.discover_nisar`); on a region-scale
AOI (``ca``) :meth:`NisarSme2Connector.discover` keeps every granule whose CMR footprint touches the region
polygon, of one orbit direction (``options.region_orbit_state``). Files are 65-126 MB; extract reads the HDF5
metadata through :func:`~fuellake.connectors._earthdata.open_h5` with small blocks (``options.http_block_size``),
then fetches only the stored 512 x 512 chunks of the needed datasets that touch the AOI window and the region,
as concurrent HTTPS range reads (``options.http_concurrency``; one request takes about 1.6 s, so sequential
block-cache reads cost ~4 min per statewide granule), decodes them (deflate / shuffle) and stores the subset
under ``raw/nisar-sme2/<yyyy>/<mm>/<granule>__<aoi>.h5``.

Verified on real PROVISIONAL files (P05023, ``identification/productVersion`` 1.0.11,
``productSpecificationVersion`` 1.0.2; 2026-09-01 track 106 A frame 19 over the San Gabriel Mountains and
2026-09-07 / 10-01 track 13 D frame 71)
---------------------------------------------------------------------------------------------------------
Layout (``/science/LSAR/SME2/grids``): ``soilMoisture``, ``soilMoistureUncertainty``, ``retrievalQualityFlag``,
``surfaceQualityFlag``; ``algorithmCandidates/{DSG,PMI,TSR}/{soilMoisture,soilMoistureUncertainty,
retrievalQualityFlag,...}``; ``ancillaryData/{landCover,...}``; ``x/yCoordinates`` (centres),
``x/yCoordinateSpacing`` (200.17900467 m), ``projection`` (EPSG 6933), ``EASEGridColumnIndex`` /
``EASEGridRowIndex``. Float layers ``_FillValue`` -9999, chunks 512 x 512, gzip.

Top-level ``retrievalQualityFlag`` (int16, fill -9999). The public specification JPL D-107677 Rev C (version
1.0.0, Nov 2024) still lists the old 8-bit layout (0 recommended, 1 attempted, 2 success, 3 thresholds
0.02-0.60, 4-7 sigma anomalies). The real files (spec version 1.0.2) describe and contain a different one:

- bit 0 (:data:`FLAG_NOT_RECOMMENDED`): 1 = retrieval **not** recommended, 0 = recommended;
- bit 1 / 2 / 3 (:data:`FLAG_INCLUDED`): PMI / TSR / DSG was used in the top-level average.

Checked cell by cell on the San Gabriel window (38,275 cells): bit 0 is 0 exactly where at least one algorithm
has its own flag bit 0 clear; bit k is set exactly where that algorithm entered the average; and the top-level
``soilMoisture`` equals the mean of the included candidates (the recommended ones, or all valid ones where none
is recommended). So ``recommended for algorithm k = (bit 0 == 0) and bit k``; bit 3 means "DSG used", not
"outside 0.02-0.60". Values are range-checked separately (``nisar:algorithm_stats``).

Per-algorithm flags (``algorithmCandidates/*/retrievalQualityFlag``, "0 means clear"): only bit 0 (not
recommended) is verified. Their other bits do not follow Rev C (TSR sets bits up to 9; PMI sets bit 7 "sigma
VH anomaly" on HH/HV data), so they are not read.

``surfaceQualityFlag`` sits at ``grids/`` (Rev C: ``ancillaryData/``); bit 7 = dense vegetation (Rev C).
``ancillaryData/landCover`` uses the Rev C classes (1 tree cover, 2 shrubland, 3 grassland, 4 cropland,
5 built-up, 6 bare/sparse, 7 snow and ice, 8 permanent water, 9 herbaceous wetland, 10 mangroves, 11 moss and
lichen). Both are kept in the raw subset for the diagnostic item properties only.

``soil_moisture`` is one source for the whole series (``options.sm_source``: ``product`` or DSG / PMI / TSR,
see the dataset config for the choice). The top-level ``product`` average changes its member algorithms from
cell to cell and date to date, so it is not used as the series. A source layer that holds only zeros (as in the
pre-launch sample D00404) still fails the unit (:class:`UnpopulatedLayerError`).

``soil_moisture_uncertainty`` is the product's own uncertainty layer of the same source. ``sm_algorithm_spread``
is max - min of the DSG, PMI and TSR soil moisture per native 200 m cell where at least two are valid, computed
before resampling. No cell is masked by the flag unless ``options.quality_mask_bits`` lists top-level bits
(e.g. ``[0]`` = drop cells where no algorithm is recommended).

Grids: on a local AOI (30 m UTM) continuous layers are bilinear-upsampled and the flag is nearest. On an AOI with
``reference_grid: ease2-200m`` (``ca``) every SME2 cell is copied onto one AOI cell
(:func:`fuellake.rasters.reproject_array` detects the alignment); a granule that is not aligned fails the unit
instead of being warped.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import h5py
import numpy as np
from rasterio.warp import transform_bounds

from fuellake.connectors import _earthdata as ed
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.grid import (  # noqa: F401  (re-exported: smap.py and tests import them from here)
    EASE2_200M_CELL,
    EASE2_200M_COLS,
    EASE2_200M_ROWS,
    EASE2_EPSG,
    EASE2_UL_X,
    EASE2_UL_Y,
    Grid,
)
from fuellake.rasters import aligned_offset, mask_out_of_range, read_cog, reproject_array, write_cog
from fuellake.registry import AOI, lake_region

log = logging.getLogger("fuellake.connectors.nisar_sme2")

GRIDS = "/science/LSAR/SME2/grids"
IDENT = "/science/LSAR/identification"
ALGORITHMS = ("DSG", "PMI", "TSR")
LAYERS = ("soilMoisture", "soilMoistureUncertainty", "retrievalQualityFlag")
FILL = -9999
SUBSET_VERSION = 2
DEFAULT_BLOCK_SIZE = 64 * 1024    # HTTP block for the HDF5 metadata reads (4 MiB blocks: ~17 MB per open)

# EASE-Grid 2.0 Global (EPSG:6933): EASE2_* constants (NSIDC corner, SME2 cell count) are defined in core
# (fuellake.grid), because the region AOI `ca` uses this grid; they are re-exported here for the connectors.

# Top-level retrievalQualityFlag, as written in the P05023 files (productSpecificationVersion 1.0.2).
FLAG_NOT_RECOMMENDED = 1 << 0
FLAG_INCLUDED = {"PMI": 1 << 1, "TSR": 1 << 2, "DSG": 1 << 3}
SURFACE_DENSE_VEGETATION = 1 << 7      # surfaceQualityFlag bit 7 (Rev C)
LAND_COVER = {1: "tree_cover", 2: "shrubland", 3: "grassland", 4: "cropland", 5: "built_up", 6: "bare_sparse",
              7: "snow_ice", 8: "permanent_water", 9: "herbaceous_wetland", 10: "mangroves", 11: "moss_lichen"}
RETRIEVAL_BOUNDS = (0.02, 0.60)        # the algorithms' soil moisture thresholds (DSG/TSR values are clamped)
SPREAD_DEFINITION = ("max - min of algorithmCandidates/{DSG,PMI,TSR}/soilMoisture per native 200 m cell where at "
                     "least 2 of the 3 are valid (NaN otherwise), computed before resampling to the AOI grid")

# subset key -> path below GRIDS (first existing path wins)
ANCILLARY = {"ancillary/landCover": ("ancillaryData/landCover",),
             "ancillary/surfaceQualityFlag": ("surfaceQualityFlag", "ancillaryData/surfaceQualityFlag")}
VARIABLE_SOURCES = {          # variable -> (layer, source); source None = options.sm_source
    "soil_moisture": ("soilMoisture", None),
    "soil_moisture_uncertainty": ("soilMoistureUncertainty", None),
    "soil_moisture_quality_flag": ("retrievalQualityFlag", "product"),
    "soil_moisture_dsg": ("soilMoisture", "DSG"),
    "soil_moisture_pmi": ("soilMoisture", "PMI"),
    "soil_moisture_tsr": ("soilMoisture", "TSR"),
    "sm_algorithm_spread": (None, "spread"),
}


class UnpopulatedLayerError(ValueError):
    """The selected SME2 source layer holds only zeros / zero statistics (placeholder, not data)."""


def ease2_centre(col: np.ndarray | int, row: np.ndarray | int, cell: float = EASE2_200M_CELL) -> tuple:
    """Map coordinates (EPSG:6933, metres) of the centre of global EASE-Grid 2.0 cell ``(col, row)``."""
    col = np.asarray(col, dtype="float64")
    row = np.asarray(row, dtype="float64")
    return EASE2_UL_X + (col + 0.5) * cell, EASE2_UL_Y - (row + 0.5) * cell


def ease2_index(x: np.ndarray | float, y: np.ndarray | float, cell: float = EASE2_200M_CELL) -> tuple:
    """Global EASE-Grid 2.0 ``(col, row)`` of the cell containing map point ``(x, y)``."""
    x = np.asarray(x, dtype="float64")
    y = np.asarray(y, dtype="float64")
    return np.floor((x - EASE2_UL_X) / cell).astype("int64"), np.floor((EASE2_UL_Y - y) / cell).astype("int64")


def source_key(source: str) -> str:
    """Subset key prefix of ``options.sm_source`` (``product`` or an algorithm name)."""
    s = str(source).upper()
    if s == "PRODUCT":
        return "product"
    if s not in ALGORITHMS:
        raise ValueError(f"sm_source must be 'product' or one of {ALGORITHMS}, got {source!r}")
    return s


def source_group(source: str) -> str:
    key = source_key(source)
    return GRIDS if key == "product" else f"{GRIDS}/algorithmCandidates/{key}"


def subset_keys(source: str) -> list[str]:
    """The datasets an extract reads: the top-level flag, the source's soil moisture and uncertainty, the three
    algorithm candidates and the two small ancillary layers."""
    src = source_key(source)
    keys = ["product/retrievalQualityFlag", f"{src}/soilMoisture", f"{src}/soilMoistureUncertainty"]
    keys += [f"{a}/soilMoisture" for a in ALGORITHMS]
    keys += list(ANCILLARY)
    return list(dict.fromkeys(keys))


def _h5_path(key: str) -> tuple[str, ...]:
    if key in ANCILLARY:
        return ANCILLARY[key]
    group, layer = key.split("/", 1)
    return (layer,) if group == "product" else (f"algorithmCandidates/{group}/{layer}",)


# ---------------------------------------------------------------------------- flags and statistics
def decode_quality_flag(flag: np.ndarray) -> dict[str, np.ndarray]:
    """Boolean layers of the top-level ``retrievalQualityFlag`` (bit layout in the module docstring):
    ``valid`` (not fill), ``recommended`` (bit 0 clear), ``included_<ALG>`` (bit 1/2/3) and
    ``recommended_<ALG>`` = recommended and included."""
    f = np.asarray(flag).astype("int64")
    valid = (f != FILL) & (f >= 0)
    out = {"valid": valid, "recommended": valid & ((f & FLAG_NOT_RECOMMENDED) == 0)}
    for alg, bit in FLAG_INCLUDED.items():
        inc = valid & ((f & bit) != 0)
        out[f"included_{alg}"] = inc
        out[f"recommended_{alg}"] = inc & out["recommended"]
    return out


def algorithm_spread(values: dict[str, np.ndarray], min_algorithms: int = 2) -> tuple[np.ndarray, np.ndarray]:
    """``(spread, count)``: max - min across the algorithm arrays per cell where at least ``min_algorithms``
    are finite (NaN elsewhere), and the number of finite algorithms per cell."""
    stack = np.stack([np.asarray(v, dtype="float32") for v in values.values()])
    finite = np.isfinite(stack)
    count = finite.sum(axis=0)
    hi = np.where(finite, stack, -np.inf).max(axis=0)
    lo = np.where(finite, stack, np.inf).min(axis=0)
    spread = np.where(count >= min_algorithms, hi - lo, np.nan).astype("float32")
    return spread, count


def value_stats(values: np.ndarray, bounds: tuple[float, float] = RETRIEVAL_BOUNDS, tol: float = 1e-6) -> dict | None:
    """Median, p10/p90 and the range check of the valid values: shares below / above the retrieval bounds and
    pinned at a bound (clamped retrievals). Compared in float64 with ``tol`` so float32 0.02 / 0.60 count as
    'at the bound', not outside it."""
    v = np.asarray(values, dtype="float64")
    v = v[np.isfinite(v)]
    if not v.size:
        return None
    lo, hi = bounds
    return {"n": int(v.size), "median": round(float(np.median(v)), 4),
            "p10": round(float(np.percentile(v, 10)), 4), "p90": round(float(np.percentile(v, 90)), 4),
            "below_min": round(float(np.mean(v < lo - tol)), 5), "above_max": round(float(np.mean(v > hi + tol)), 5),
            "at_bounds": round(float(np.mean((np.abs(v - lo) <= tol) | (np.abs(v - hi) <= tol))), 5)}


def _fraction(mask: np.ndarray, of: np.ndarray) -> float | None:
    n = int(of.sum())
    return round(float((mask & of).sum() / n), 5) if n else None


# ---------------------------------------------------------------------------- HDF5 subset
def chunk_blocks(rs: slice, cs: slice, chunks: tuple[int, int] | None, xc: np.ndarray, yc: np.ndarray,
                 keep=None) -> tuple[list[tuple[int, int, int, int]], int]:
    """Split the window into blocks aligned to the dataset chunks; keep those whose cell-edge box (map
    coordinates) passes ``keep(minx, miny, maxx, maxy)``. Returns ``(blocks, n_skipped)``, blocks as
    ``(r0, r1, c0, c1)`` file indices."""
    ch_r, ch_c = chunks or (rs.stop - rs.start, cs.stop - cs.start)
    hx = abs(float(xc[1] - xc[0])) / 2 if xc.size > 1 else 0.0
    hy = abs(float(yc[1] - yc[0])) / 2 if yc.size > 1 else 0.0
    blocks, skipped = [], 0
    for r in range((rs.start // ch_r) * ch_r, rs.stop, ch_r):
        r0, r1 = max(r, rs.start), min(r + ch_r, rs.stop)
        for c in range((cs.start // ch_c) * ch_c, cs.stop, ch_c):
            c0, c1 = max(c, cs.start), min(c + ch_c, cs.stop)
            box = (min(xc[c0], xc[c1 - 1]) - hx, min(yc[r0], yc[r1 - 1]) - hy,
                   max(xc[c0], xc[c1 - 1]) + hx, max(yc[r0], yc[r1 - 1]) + hy)
            if keep is None or keep(*box):
                blocks.append((r0, r1, c0, c1))
            else:
                skipped += 1
    return blocks, skipped


def _empty_window(ds, rs: slice, cs: slice) -> np.ndarray:
    fill = np.asarray(ds.attrs.get("_FillValue", FILL)).ravel()[0]
    return np.full((rs.stop - rs.start, cs.stop - cs.start), fill, dtype=ds.dtype)


def _read_blocks(ds, rs: slice, cs: slice, blocks) -> np.ndarray:
    out = _empty_window(ds, rs, cs)
    for r0, r1, c0, c1 in blocks:
        out[r0 - rs.start:r1 - rs.start, c0 - cs.start:c1 - cs.start] = ds[r0:r1, c0:c1]
    return out


H5Z_DEFLATE, H5Z_SHUFFLE = 1, 2


def _filter_codes(ds) -> list[int] | None:
    """HDF5 filter pipeline of ``ds`` (write order) if it only uses deflate / shuffle, else None."""
    dcpl = ds.id.get_create_plist()
    codes = [int(dcpl.get_filter(i)[0]) for i in range(dcpl.get_nfilters())]
    return codes if set(codes) <= {H5Z_DEFLATE, H5Z_SHUFFLE} else None


def decode_chunk(buf: bytes, codes: list[int], dtype, shape: tuple[int, ...]) -> np.ndarray:
    """Undo the HDF5 filter pipeline ``codes`` (deflate, shuffle) of one stored chunk."""
    import zlib

    data = buf
    dtype = np.dtype(dtype)
    for code in reversed(codes):
        if code == H5Z_DEFLATE:
            data = zlib.decompress(data)
        elif code == H5Z_SHUFFLE and dtype.itemsize > 1:
            data = np.frombuffer(data, np.uint8).reshape(dtype.itemsize, -1).T.tobytes()
    return np.frombuffer(data, dtype=dtype).reshape(shape)


def _chunk_plan(ds, blocks) -> list[tuple] | None:
    """``[(block, chunk_origin, byte_offset, size)]`` of the allocated chunks behind ``blocks`` (blocks are
    chunk-aligned, see :func:`chunk_blocks`); None when the layout needs h5py (other filters, skipped filters)."""
    if ds.chunks is None or _filter_codes(ds) is None:
        return None
    ch_r, ch_c = ds.chunks
    plan = []
    for r0, r1, c0, c1 in blocks:
        origin = ((r0 // ch_r) * ch_r, (c0 // ch_c) * ch_c)
        info = ds.id.get_chunk_info_by_coord(origin)
        if info.byte_offset is None or not info.size:
            continue                                     # never written: fill value
        if info.filter_mask:
            return None
        plan.append(((r0, r1, c0, c1), origin, int(info.byte_offset), int(info.size)))
    return plan


def _read_layers(datasets: dict[str, Any], rs: slice, cs: slice, blocks, fetch=None) -> dict[str, np.ndarray]:
    """Window arrays of several datasets. With ``fetch`` (``[(offset, size)] -> [bytes]``) the stored chunks of
    all layers are fetched in one concurrent batch and decoded here; layouts :func:`_chunk_plan` cannot handle
    (and every layer without ``fetch``) are read through h5py."""
    out: dict[str, np.ndarray] = {}
    plans = {k: _chunk_plan(ds, blocks) for k, ds in datasets.items()} if fetch is not None else {}
    jobs = [(k, item) for k, plan in plans.items() if plan for item in plan]
    payload = fetch([(off, size) for _, (_, _, off, size) in jobs]) if jobs else []
    for key, ds in datasets.items():
        if plans.get(key) is None:
            out[key] = _read_blocks(ds, rs, cs, blocks)
            continue
        out[key] = _empty_window(ds, rs, cs)
    codes = {k: _filter_codes(ds) for k, ds in datasets.items() if plans.get(k)}
    for (key, (block, origin, _, _)), buf in zip(jobs, payload, strict=True):
        ds = datasets[key]
        chunk = decode_chunk(buf, codes[key], ds.dtype, ds.chunks)
        r0, r1, c0, c1 = block
        out[key][r0 - rs.start:r1 - rs.start, c0 - cs.start:c1 - cs.start] = \
            chunk[r0 - origin[0]:r1 - origin[0], c0 - origin[1]:c1 - origin[1]]
    return out


def range_fetcher(url: str, settings, concurrency: int = 16, retries: int = 2):
    """``fetch([(offset, size)]) -> [bytes]`` for ``url``: concurrent HTTPS range GETs (``concurrency`` at a
    time) with the same headers as :func:`fuellake.connectors._earthdata.open_h5` (``User-Agent``, and the
    bearer token only for the Earthdata data hosts, via :func:`~fuellake.connectors._earthdata.require_token`;
    aiohttp drops it on the cross-origin redirect to the signed URL), or plain reads of a local file.
    ``fetch.stats`` counts requests and bytes."""
    import time
    from urllib.parse import urlsplit

    from fuellake.config import redact

    stats = {"requests": 0, "bytes": 0}
    if not ed.is_remote(url):
        path = url[len("file://"):] if url.startswith("file://") else url

        def fetch_local(ranges):
            out = []
            with open(path, "rb") as fh:
                for off, size in ranges:
                    fh.seek(off)
                    out.append(fh.read(size))
            stats["requests"] += len(ranges)
            stats["bytes"] += sum(len(b) for b in out)
            return out

        fetch_local.stats = stats       # type: ignore[attr-defined]
        return fetch_local
    import fsspec

    headers = dict(settings.http_headers())
    token = ed.require_token(settings, url)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    fs = fsspec.filesystem("http", headers=headers, skip_instance_cache=True)

    def fetch(ranges):
        starts = [int(o) for o, _ in ranges]
        ends = [int(o) + int(n) for o, n in ranges]
        for attempt in range(retries + 1):
            try:
                out = fs.cat_ranges([url] * len(ranges), starts, ends, batch_size=concurrency, on_error="raise")
                break
            except Exception as exc:     # aiohttp errors may echo request details: redact, path only
                if attempt == retries:
                    raise ed.EarthdataAuthError(redact(
                        f"range reads failed for {urlsplit(url).path[-80:]}: {type(exc).__name__}: {exc}")) from None
                time.sleep(2.0 * (attempt + 1))
        bad = [i for i, (b, (_, n)) in enumerate(zip(out, ranges, strict=True)) if len(b) != int(n)]
        if bad:
            raise ed.EarthdataAuthError(f"{urlsplit(url).path[-80:]}: {len(bad)} range reads returned the wrong size")
        stats["requests"] += len(ranges)
        stats["bytes"] += sum(len(b) for b in out)
        return out

    fetch.stats = stats                  # type: ignore[attr-defined]
    return fetch


def read_sme2_subset(f: h5py.File, grid, pad_cells: int = 2, keys: list[str] | None = None,
                     keep=None, fetch=None) -> dict[str, Any] | None:
    """Read the AOI window of ``keys`` (default :func:`subset_keys` of ``product``); only chunk blocks passing
    ``keep`` (see :func:`chunk_blocks`) are read, the rest stays fill. ``fetch`` (see :func:`range_fetcher`)
    fetches the stored chunks concurrently instead of through h5py. ``None`` if nothing intersects."""
    g = f[GRIDS]
    epsg = int(g["projection"].attrs.get("epsg_code", g["projection"][()]))
    if epsg != EASE2_EPSG:
        raise ValueError(f"SME2 grid EPSG {epsg} != {EASE2_EPSG} (EASE-Grid 2.0 Global)")
    xc = g["xCoordinates"][()]
    yc = g["yCoordinates"][()]
    dx = float(g["xCoordinateSpacing"][()])
    dy = float(g["yCoordinateSpacing"][()])
    bounds = transform_bounds(grid.crs, f"EPSG:{epsg}", *grid.bounds(), densify_pts=21)
    win = ed.window_from_centres(xc, yc, bounds, pad=pad_cells * abs(dx))
    if win is None:
        return None
    rs, cs = win
    keys = keys or subset_keys("product")
    for alg in ALGORITHMS:
        p = f"algorithmCandidates/{alg}/xCoordinates"
        if p in g:
            gx = g[p][()]
            if gx.shape != xc.shape or not np.allclose(gx, xc, atol=1e-3):
                raise ValueError(f"SME2 {alg} grid differs from the product grid; not supported")
    first = next((g[p] for k in keys for p in _h5_path(k) if p in g), None)
    blocks, skipped = chunk_blocks(rs, cs, first.chunks if first is not None else None, xc, yc, keep)
    if not blocks:
        return None
    out: dict[str, Any] = {"epsg": epsg, "x0": float(xc[cs.start]), "y0": float(yc[rs.start]), "dx": dx, "dy": dy,
                           "window": [rs.start, rs.stop, cs.start, cs.stop], "arrays": {}, "attrs": {},
                           "blocks_read": len(blocks), "blocks_skipped": skipped}
    datasets = {}
    for key in keys:
        path = next((p for p in _h5_path(key) if p in g), None)
        if path is None:
            continue
        ds = datasets[key] = g[path]
        out["attrs"][key] = {k: float(np.asarray(ds.attrs[k]).ravel()[0]) for k in
                             ("_FillValue", "min_value", "max_value", "mean_value") if k in ds.attrs}
    out["arrays"].update(_read_layers(datasets, rs, cs, blocks, fetch))
    for name in ("EASEGridColumnIndex", "EASEGridRowIndex"):
        if name in g:
            out["arrays"][name] = g[name][cs if name == "EASEGridColumnIndex" else rs]
    ident = {}
    if IDENT in f:
        for k in ("trackNumber", "frameNumber", "orbitPassDirection", "compositeReleaseId", "granuleId",
                  "zeroDopplerStartTime", "productVersion", "productSpecificationVersion", "missionId"):
            if k in f[IDENT]:
                ident[k] = ed.h5_str(f[IDENT][k][()])
    out["identification"] = ident
    return out


def write_subset(path: str, subset: dict[str, Any], source_url: str) -> None:
    with h5py.File(path, "w") as h:
        for name, arr in subset["arrays"].items():
            h.create_dataset(name, data=arr, compression="gzip", compression_opts=4)
        for k in ("epsg", "x0", "y0", "dx", "dy"):
            h.attrs[k] = subset[k]
        h.attrs["window"] = subset["window"]
        h.attrs["layer_attrs"] = json.dumps(subset["attrs"])
        h.attrs["identification"] = json.dumps(subset["identification"])
        h.attrs["blocks"] = [int(subset.get("blocks_read", 0)), int(subset.get("blocks_skipped", 0))]
        h.attrs["source_url"] = source_url
        h.attrs["fuellake_subset_version"] = SUBSET_VERSION
        h.attrs["complete"] = 1


def read_subset(path: str) -> dict[str, Any]:
    with h5py.File(path, "r") as h:
        if int(h.attrs.get("complete", 0)) != 1:
            raise ValueError(f"{path}: incomplete raw subset")
        sub = {k: h.attrs[k] for k in ("epsg", "x0", "y0", "dx", "dy")}
        sub["epsg"] = int(sub["epsg"])
        sub["version"] = int(h.attrs.get("fuellake_subset_version", 0))
        sub["window"] = [int(v) for v in h.attrs["window"]]
        sub["blocks"] = [int(v) for v in h.attrs.get("blocks", [0, 0])]
        sub["attrs"] = json.loads(ed.h5_str(h.attrs["layer_attrs"]))
        sub["identification"] = json.loads(ed.h5_str(h.attrs["identification"]))
        arrays: dict[str, np.ndarray] = {}
        h.visititems(lambda n, o: arrays.__setitem__(n, o[()]) if isinstance(o, h5py.Dataset) else None)
        sub["arrays"] = arrays
    return sub


def check_georeferencing(sub: dict[str, Any], tol_m: float = 1.0) -> None:
    """Cross-check the subset's centre coordinates against the global EASE-Grid 2.0 indices."""
    cols = sub["arrays"].get("EASEGridColumnIndex")
    rows = sub["arrays"].get("EASEGridRowIndex")
    if abs(abs(sub["dx"]) - EASE2_200M_CELL) > 1e-3 or abs(abs(sub["dy"]) - EASE2_200M_CELL) > 1e-3:
        log.info("SME2 spacing %.6f / %.6f differs from the 200 m EASE-Grid 2.0 cell %.6f (e.g. 400 m product?)",
                 sub["dx"], sub["dy"], EASE2_200M_CELL)
    if cols is None or rows is None or len(cols) == 0 or len(rows) == 0:
        return
    cell = abs(sub["dx"])
    x_exp, y_exp = ease2_centre(int(cols[0]), int(rows[0]), cell)
    if abs(float(x_exp) - sub["x0"]) > tol_m or abs(float(y_exp) - sub["y0"]) > tol_m:
        raise ValueError(
            f"EASE-Grid 2.0 georeferencing mismatch: file centre ({sub['x0']:.2f}, {sub['y0']:.2f}) vs "
            f"index ({int(cols[0])}, {int(rows[0])}) -> ({float(x_exp):.2f}, {float(y_exp):.2f})")


def layer_values(sub: dict[str, Any], key: str) -> np.ndarray | None:
    """float32 layer with fill and non-finite values as NaN (``None`` if absent)."""
    arr = sub["arrays"].get(key)
    if arr is None:
        return None
    fill = sub["attrs"].get(key, {}).get("_FillValue", FILL)
    out = ed.fill_to_nan(arr, fill)
    out[~np.isfinite(out)] = np.nan
    return out


def is_unpopulated(sub: dict[str, Any], key: str, values: np.ndarray) -> bool:
    """True when the layer is a placeholder: every valid value is exactly 0, or the file-level statistics
    attributes are all zero."""
    a = sub["attrs"].get(key, {})
    stats = [a.get(k) for k in ("min_value", "max_value", "mean_value")]
    if all(s is not None for s in stats) and all(s == 0.0 for s in stats):
        return True
    finite = values[np.isfinite(values)]
    return bool(finite.size) and bool(np.all(finite == 0.0))


# ---------------------------------------------------------------------------- connector
class NisarSme2Connector(BaseConnector):
    """NISAR L3 SME2 soil moisture (PROVISIONAL), EASE-Grid 2.0 200 m -> AOI grid."""

    product = "SME2"

    # ------------------------------------------------------------------ options
    @property
    def sm_source(self) -> str:
        return str(self.dataset.options.get("sm_source", "product"))

    def _region(self):
        if not hasattr(self, "_region_cache"):
            self._region_cache = lake_region()
        return self._region_cache

    # ------------------------------------------------------------------ discovery
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        if aoi.scale != "region":
            return ed.discover_nisar(self, aoi, period, self.product)
        return self._discover_region(aoi, period)

    def _discover_region(self, aoi: AOI, period: Period) -> list[SourceRef]:
        """Region-scale AOI: every granule of every stack whose footprint touches the region polygon (one frame
        covers a few percent of the region bbox, so the local ``min_aoi_coverage`` rule would drop them all),
        optionally one orbit direction (``options.region_orbit_state``)."""
        opts = self.dataset.options
        ds = self.dataset.model_copy(deep=True)
        ds.options.update(stack="all", min_aoi_coverage=0.0)
        view = SimpleNamespace(dataset=ds, settings=self.settings, ref=self.ref)
        refs = ed.discover_nisar(view, aoi, period, self.product)       # type: ignore[arg-type]
        want = opts.get("region_orbit_state")
        if want:
            refs = [r for r in refs if str(r.meta.get("orbit_state")) == str(want).lower()]
        region = self._region()
        if region is None or not refs:
            return refs
        maturity = str(opts.get("maturity") or self.dataset.maturity or "PROVISIONAL").upper()
        concept = opts["collections"][maturity]["concept_id"]
        items = ed.search_granules(ed.cmr_session(self.settings), concept, bbox=aoi.bbox, start=period.start,
                                   end=period.end)
        prints = {g.ur: ed.footprint(g) for g in ed.parse_granules(items)}
        min_frac = float(opts.get("region_min_overlap", 0.0))
        out = []
        for r in refs:
            fp = prints.get(r.meta.get("granule_ur", r.key))
            if fp is None or fp.is_empty or not fp.intersects(region.geometry):
                continue
            frac = float(fp.intersection(region.geometry).area / fp.area) if fp.area else 0.0
            if frac <= min_frac:
                continue
            out.append(SourceRef(r.dataset_id, r.key, r.datetime, r.urls, {**r.meta, "region_overlap": round(frac, 4)}))
        log.info("%s discover on %s: %d of %d granules touch region %s", self.dataset.id, aoi.id, len(out),
                 len(refs), region.id)
        return out

    # ------------------------------------------------------------------ cache
    def raw_rel(self, ref: SourceRef, aoi: AOI) -> str:
        return self.raw_path(ref, f"{ref.meta.get('granule_ur', ref.key)}__{aoi.id}.h5")

    def empty_rel(self, ref: SourceRef, aoi: AOI) -> str:
        return self.raw_path(ref, f"{ref.meta.get('granule_ur', ref.key)}__{aoi.id}.empty.json")

    def _mark_empty(self, ref: SourceRef, aoi: AOI, reason: str) -> None:
        log.warning("%s: %s (cached in %s)", ref.key, reason, self.empty_rel(ref, aoi))
        self.lake.write_bytes(self.empty_rel(ref, aoi), json.dumps(
            {"granule": ref.key, "aoi": aoi.id, "reason": reason,
             "written": datetime.now(UTC).isoformat(timespec="seconds")}).encode())

    def cached_empty(self, ref: SourceRef, aoi: AOI) -> bool:
        """True when an earlier run found that this granule has no SME2 cell inside the AOI and the region (no
        network). Delete ``raw/.../<granule>__<aoi>.empty.json`` to try again."""
        return self.lake.exists(self.empty_rel(ref, aoi))

    def missing_credentials(self, aoi: AOI, ref: SourceRef | None = None) -> list[str]:
        """CMR discovery is public; a unit needs ``EARTHDATA_TOKEN`` unless its AOI subset is already in
        ``raw/`` or the file is in the public sample-data area."""
        if ref is None or self.lake.exists(self.raw_rel(ref, aoi)):
            return []
        return ed.missing_token(self.settings, ref.urls[:1])

    def _chunk_filter(self, aoi: AOI):
        """``keep(minx, miny, maxx, maxy)`` in EPSG:6933: the block touches the region (buffered by its
        tolerance). None without a lake region."""
        region = self._region()
        if region is None:
            return None
        import shapely
        from shapely.geometry import box

        geom = region.in_crs(f"EPSG:{EASE2_EPSG}", region.tolerance_m)
        shapely.prepare(geom)
        return lambda x0, y0, x1, y1: bool(geom.intersects(box(x0, y0, x1, y1)))

    # ------------------------------------------------------------------ extract
    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        rel = self.raw_rel(ref, aoi)
        keys = subset_keys(self.sm_source)
        if self.lake.exists(rel):
            try:
                with self.lake.local_path(rel, "r") as p:
                    sub = read_subset(p)
                missing = [k for k in keys if k not in sub["arrays"]]
                if sub["version"] == SUBSET_VERSION and not missing:
                    return [rel]
                log.info("re-extracting %s (subset version %s, missing %s)", rel, sub["version"], missing)
            except Exception as exc:
                log.warning("re-extracting %s: %s", rel, exc)
        url = ref.urls[0]
        opts = self.dataset.options
        block = int(opts.get("http_block_size", DEFAULT_BLOCK_SIZE))
        fetch = range_fetcher(url, self.settings, concurrency=int(opts.get("http_concurrency", 16)))
        with ed.open_h5(url, self.settings, block_size=block) as f:      # HDF5 metadata only
            subset = read_sme2_subset(f, aoi.grid, pad_cells=int(opts.get("pad_cells", 2)),
                                      keys=keys, keep=self._chunk_filter(aoi), fetch=fetch)
        st = fetch.stats
        log.info("fetched %.1f MB of chunks in %d range reads for %s", st["bytes"] / 1e6, st["requests"], ref.key)
        if subset is None:
            self._mark_empty(ref, aoi, f"the SME2 grid does not intersect AOI {aoi.id} inside the region")
            return []
        with self.lake.local_path(rel, "w") as path:
            write_subset(path, subset, url)
        return [rel]

    # ------------------------------------------------------------------ transform
    def _native_selection(self, sub: dict[str, Any], native: Grid, aoi: AOI) -> np.ndarray:
        """Native cells whose centres lie inside the AOI grid and the region (statistics only)."""
        minx, miny, maxx, maxy = transform_bounds(aoi.grid.crs, native.crs, *aoi.grid.bounds(), densify_pts=21)
        x, y = native.x_coords(), native.y_coords()
        sel = ((y >= miny) & (y <= maxy))[:, None] & ((x >= minx) & (x <= maxx))[None, :]
        region = self._region()
        if region is not None:
            sel &= region.pixel_mask(native)
        return sel

    def _properties(self, sub, values, flag, sel) -> dict[str, Any]:
        props: dict[str, Any] = {}
        if flag is not None:
            dec = decode_quality_flag(flag)
            ok = dec["valid"] & sel
            props["nisar:recommended_fraction"] = _fraction(dec["recommended"], ok)
            props["nisar:recommended_fraction_by_algorithm"] = {
                a: _fraction(dec[f"recommended_{a}"], ok) for a in ALGORITHMS}
            lc = sub["arrays"].get("ancillary/landCover")
            if lc is not None and ok.any():
                by = {}
                for code, name in LAND_COVER.items():
                    m = ok & (lc == code)
                    if m.sum() >= max(1, 0.01 * ok.sum()):
                        by[name] = {"cell_fraction": _fraction(m, ok), "recommended": _fraction(dec["recommended"], m)}
                props["nisar:recommended_fraction_by_land_cover"] = by
            sq = sub["arrays"].get("ancillary/surfaceQualityFlag")
            if sq is not None and ok.any():
                dense = ok & (sq != FILL) & ((np.asarray(sq).astype("int64") & SURFACE_DENSE_VEGETATION) != 0)
                props["nisar:dense_vegetation_fraction"] = _fraction(dense, ok)
                props["nisar:recommended_fraction_dense_vegetation"] = _fraction(dec["recommended"], dense)
        props["nisar:algorithm_stats"] = {a: value_stats(v[sel]) for a, v in values.items() if v is not None}
        return props

    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        if not raw:
            return []
        with self.lake.local_path(raw[0], "r") as p:
            sub = read_subset(p)
        check_georeferencing(sub)
        opts = self.dataset.options
        source = self.sm_source
        src_key = source_key(source)
        bits = [int(b) for b in opts.get("quality_mask_bits", []) or []]
        bitmask = int(sum(1 << b for b in bits))

        primary = layer_values(sub, f"{src_key}/soilMoisture")
        if primary is None:
            raise ValueError(f"SME2 source {source!r} has no soilMoisture layer in this granule")
        if is_unpopulated(sub, f"{src_key}/soilMoisture", primary):
            raise UnpopulatedLayerError(
                f"SME2 layer {source_group(source)}/soilMoisture is unpopulated (all zeros) in this granule; "
                "set options.sm_source in config/datasets/nisar-sme2.yaml to DSG, PMI or TSR for the whole series")

        grid = aoi.grid
        src_crs = f"EPSG:{sub['epsg']}"
        src_t = ed.transform_from_centres(sub["x0"], sub["y0"], sub["dx"], sub["dy"])
        native = Grid(crs=src_crs, transform=src_t, width=primary.shape[1], height=primary.shape[0],
                      resolution=abs(sub["dx"]))
        offset = aligned_offset(src_crs, src_t, grid)
        if aoi.reference_grid and offset is None:
            raise ValueError(f"SME2 granule is not aligned with the {aoi.reference_grid} grid of AOI {aoi.id}; "
                             "refusing to resample it")
        sel = self._native_selection(sub, native, aoi)
        if not np.isfinite(primary[sel]).any():
            self._mark_empty(ref, aoi, f"no valid SME2 {src_key} soil moisture inside AOI {aoi.id}")
            return []

        flag = sub["arrays"].get("product/retrievalQualityFlag")
        algs = {a: layer_values(sub, f"{a}/soilMoisture") for a in ALGORITHMS}
        algs = {a: v for a, v in algs.items() if v is not None and not is_unpopulated(sub, f"{a}/soilMoisture", v)}
        spread, count = algorithm_spread(algs) if len(algs) >= 2 else (None, None)
        native_values = {
            "soil_moisture": primary,
            "soil_moisture_uncertainty": layer_values(sub, f"{src_key}/soilMoistureUncertainty"),
            "sm_algorithm_spread": spread,
            **{f"soil_moisture_{a.lower()}": v for a, v in algs.items()},
        }
        if flag is not None and bitmask:
            bad = (np.asarray(flag).astype("int64") & bitmask) != 0
            for v in native_values.values():
                if v is not None:
                    v[bad] = np.nan

        iid = self.item_id(aoi, ref, ed.item_discriminator(ref))
        assets, fractions, staged = [], [], {}
        for var in self.dataset.variables:
            if var.name not in VARIABLE_SOURCES:
                continue
            layer, override = VARIABLE_SOURCES[var.name]
            if var.name == "soil_moisture_quality_flag":
                if flag is None:
                    continue
                arr = np.asarray(flag).astype("int16")
                out = reproject_array(arr, src_crs, src_t, grid, resampling="nearest", src_nodata=int(var.nodata))
                frac = 0.0
                write_cog(self.lake, self.cog_path(aoi, iid, var.name), out, grid, int(var.nodata), var.dtype,
                          description="SME2 retrievalQualityFlag (top level: bit0 1=not recommended; "
                                      "bits 1/2/3 PMI/TSR/DSG in the average)", unit=var.unit)
                staged[var.name] = "product/retrievalQualityFlag"
            else:
                vals = native_values.get(var.name)
                if vals is None:
                    continue
                out = reproject_array(vals, src_crs, src_t, grid, resampling=var.resampling)
                out, frac = mask_out_of_range(out, var.valid_range)
                if not np.isfinite(out).any():
                    continue
                desc = "SME2 algorithm spread (max - min of DSG, PMI, TSR)" if override == "spread" \
                    else f"SME2 {layer} ({override or src_key})"
                write_cog(self.lake, self.cog_path(aoi, iid, var.name), out, grid, np.nan, var.dtype,
                          description=desc, unit=var.unit)
                staged[var.name] = "spread(DSG,PMI,TSR)" if override == "spread" else f"{override or src_key}/{layer}"
            assets.append(self.cog_asset(var.name, self.cog_path(aoi, iid, var.name)))
            fractions.append(frac)
        if "soil_moisture" not in staged:
            self._mark_empty(ref, aoi, f"no valid soil moisture on the grid of AOI {aoi.id}")
            return []

        props = ed.nisar_item_properties(ref, "SME2", list(ref.meta.get("polarizations") or []) or ["HH", "HV"])
        ident = sub.get("identification", {})
        if ident.get("compositeReleaseId"):
            props["fuellake:processing_id"] = ident["compositeReleaseId"]
        if ident.get("productVersion"):
            props["nisar:product_version"] = ident["productVersion"]
        if ident.get("productSpecificationVersion"):
            props["nisar:product_specification_version"] = ident["productSpecificationVersion"]
        sm_cog, _ = read_cog(self.lake, self.cog_path(aoi, iid, "soil_moisture"))
        stats_values = {a: algs.get(a) for a in ALGORITHMS}
        props.update(self._properties(sub, stats_values, flag, sel))
        if count is not None:
            n_sel = int((sel & (count >= 1)).sum())
            props["nisar:spread_algorithm_count"] = {
                str(k): round(float((sel & (count == k)).sum() / n_sel), 5) if n_sel else None for k in (3, 2)}
        grid_note = (f"copied cell by cell onto the {aoi.reference_grid} grid (offset row {offset[0]}, col {offset[1]})"
                     if offset is not None else "bilinear (flag: nearest) to the AOI grid")
        props.update({
            "fuellake:qc_masked_fraction": float(max(fractions)) if fractions else 0.0,
            "fuellake:valid_fraction": float(np.isfinite(sm_cog).sum() / sm_cog.size),
            "nisar:sm_source": source_group(source) + "/soilMoisture",
            "nisar:sm_source_option": source,
            "nisar:asset_sources": staged,
            "nisar:quality_mask_bits": bits,
            "nisar:quality_mask_note": "masked cells whose top-level flag has these bits" if bits
            else "fill/NaN masked only; top-level retrievalQualityFlag staged as soil_moisture_quality_flag",
            "nisar:spread_definition": SPREAD_DEFINITION,
            "nisar:region_overlap": ref.meta.get("region_overlap"),
            "nisar:subset_blocks": {"read": sub["blocks"][0], "skipped_outside_region": sub["blocks"][1]},
            "nisar:grid": f"EASE-Grid 2.0 Global EPSG:6933, {abs(sub['dx']):.4f} m cells, {grid_note}",
        })
        bbox = None
        if aoi.scale == "region":      # the granule's part of the AOI, not the whole state
            w, s, e, n = transform_bounds(src_crs, "EPSG:4326", *native.bounds(), densify_pts=21)
            ab = aoi.bbox
            bbox = (max(w, ab[0]), max(s, ab[1]), min(e, ab[2]), min(n, ab[3]))
        return [StagedItem(item_id=iid, datetime=ref.datetime, properties={k: v for k, v in props.items()
                                                                           if v is not None}, assets=assets,
                           bbox=bbox)]
