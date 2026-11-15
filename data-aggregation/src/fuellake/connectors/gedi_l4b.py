# fuel-lake — NASA GEDI L4B gridded aboveground biomass density (1 km, ORNL DAAC) clipped to the AOI grid
# Licensed under Apache-2.0.
"""GEDI L4B Gridded Aboveground Biomass Density, Version 2.1 (Dubayah et al. 2023, doi:10.3334/ORNLDAAC/2299).

Facts verified live 2026-11-14
------------------------------
- CMR collection ``C2792577683-ORNL_CLOUD`` (short name ``GEDI_L4B_Gridded_Biomass_V2_1_2299``, version 2.1,
  provider ORNL_CLOUD, collection progress COMPLETE). Ten one-time global granules for mission weeks 19-223,
  **2019-04-18 to 2023-03-16**, one cloud-optimized GeoTIFF each, named
  ``GEDI04_B_MW019MW223_02_002_02_R01000M_<VAR>.tif``.
- Grid (user guide, confirmed on the files): EASE-Grid 2.0 global, EPSG:6933, 34704 x 14616 cells of
  1000.895 m, 256 x 256 tiles with overviews.
- Variables staged here: ``MU`` mean AGBD (Mg/ha, float32, nodata -9999), ``SE`` standard error of the mean
  (Mg/ha, float32, -9999), ``QF`` quality flag (uint8, 0 = outside the GEDI domain, 1 = land surface, 2 = meets
  the L1 requirement: SE < 20 Mg/ha or < 20 %), ``NS`` number of high-quality waveforms (the files are uint32,
  the user guide says UInt16; nodata 0).
- Access: ``https://data.ornldaac.earthdata.nasa.gov/protected/...`` needs an Earthdata Login bearer token; the
  host answers 303 with a signed CloudFront URL that serves HTTP range requests (206). CMR search is public.
- Licence: no use restriction in the CMR record; NASA Earthdata data use guidance: "Unless the content is marked
  with a use restriction or license, data provided from a NASA-led mission are licensed as Creative Commons Zero
  (CC0)". Cite the dataset.

Units of work
-------------
One static unit per AOI (the run period is ignored): key ``<short name>:<version>:<granule URs joined by +>``,
datetime 2019-04-18 (start of the estimation period; ``start_datetime``/``end_datetime`` span 2019-04-18 ..
2023-03-16).

- ``extract`` reads **only the window** covering the AOI grid plus ``pad_cells`` native cells of each COG,
  through an fsspec HTTP file (block cache) handed to ``rasterio.open(..., opener=...)``. The bearer token is
  sent only to the hosts in ``options.token_hosts``; aiohttp drops ``Authorization`` on the cross-origin redirect
  to CloudFront. The windows are written as small native-grid GeoTIFFs to ``raw/`` together with a JSON note of
  the bytes transferred. Never downloads whole files (each is 15-536 MB).
- ``transform`` upsamples with **nearest neighbour** onto the AOI grid (every AOI pixel takes the value of the
  1 km cell its centre falls in, so the 1 km block structure is kept and no value is invented between cells),
  sets -9999 / 0 nodata to NaN (float) or 0 (``quality_flag``), and records the native grid in the item.
"""

from __future__ import annotations

import contextlib
import logging
import posixpath
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

import numpy as np
import rasterio
import rasterio.errors

from fuellake.config import redact
from fuellake.connectors._earthdata import (
    EarthdataAuthError,
    bearer_token,
    cmr_session,
    parse_granules,
    search_granules,
)
from fuellake.connectors._stac_source import grid_bounds_in, window_for_bounds
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedAsset, StagedItem
from fuellake.rasters import mask_out_of_range, reproject_array, write_cog
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.gedi_l4b")

DEFAULT_CONCEPT_ID = "C2792577683-ORNL_CLOUD"
DEFAULT_TOKEN_HOSTS = ("data.ornldaac.earthdata.nasa.gov",)
PERIOD_START = date(2019, 4, 18)
PERIOD_END = date(2023, 3, 16)
BLOCK_SIZE = 256 * 1024
JSON_MEDIA_TYPE = "application/json"
# staged variable -> (file suffix, source nodata)
VARIABLES = {"agbd": ("MU", -9999.0), "agbd_se": ("SE", -9999.0), "quality_flag": ("QF", 0),
             "n_samples": ("NS", 0)}
CITATION = ("Dubayah, R.O., J. Armston, S.P. Healey, Z. Yang, P.L. Patterson, S. Saarela, G. Stahl, L. Duncanson, "
            "J.R. Kellner, J. Bruening, and A. Pascual. 2023. GEDI L4B Gridded Aboveground Biomass Density, "
            "Version 2.1. ORNL DAAC, Oak Ridge, Tennessee, USA. https://doi.org/10.3334/ORNLDAAC/2299")


# ---------------------------------------------------------------------------- helpers
def select_granules(granules, suffixes) -> dict[str, Any]:
    """``{suffix: Granule}`` for the wanted variable suffixes (``MU``, ``SE`` ...); raises if one is missing or
    ambiguous."""
    out: dict[str, Any] = {}
    for g in granules:
        name = g.ur.rsplit(".", 2)[-2] if g.ur.endswith(".tif") else g.ur
        sfx = name.rsplit("_", 1)[-1]
        if sfx in suffixes:
            if sfx in out:
                raise ValueError(f"two GEDI L4B granules for variable {sfx}: {out[sfx].ur}, {g.ur}")
            out[sfx] = g
    missing = [s for s in suffixes if s not in out]
    if missing:
        raise ValueError(f"GEDI L4B variables not found in CMR: {missing}")
    return out


def https_url(granule) -> str:
    url = next((u for u in granule.urls if u.endswith(".tif") and "/protected/" in u), None) or \
        next((u for u in granule.urls if u.endswith(".tif")), None)
    if url is None:
        raise ValueError(f"granule {granule.ur} has no https .tif link")
    return url


def wants_token(url: str, hosts) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc in set(hosts)


class _CountingFile:
    """File wrapper that counts the bytes GDAL reads (to report and budget transfers)."""

    def __init__(self, f):
        self.f, self.bytes_read = f, 0

    def read(self, size=-1):
        b = self.f.read(size)
        self.bytes_read += len(b)
        return b

    def seek(self, *args):
        return self.f.seek(*args)

    def tell(self):
        return self.f.tell()

    def close(self):
        return self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _Transfer:
    """Bytes read through every file object GDAL opened for one dataset."""

    def __init__(self):
        self.files: list[_CountingFile] = []

    @property
    def bytes_read(self) -> int:
        return sum(f.bytes_read for f in self.files)


# ---------------------------------------------------------------------------- connector
class GediL4bConnector(BaseConnector):
    """Static GEDI L4B item (mean AGBD, standard error, quality flag, sample count) per AOI."""

    def _opts(self) -> dict:
        o = self.dataset.options
        return {"concept_id": o.get("concept_id", DEFAULT_CONCEPT_ID),
                "token_hosts": tuple(o.get("token_hosts", DEFAULT_TOKEN_HOSTS)),
                "pad_cells": int(o.get("pad_cells", 2)),
                "version": str(o.get("version", "2.1"))}

    # ------------------------------------------------------------------ discover (CMR, no token)
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        o = self._opts()
        items = search_granules(cmr_session(self.settings), o["concept_id"], bbox=aoi.bbox, start=PERIOD_START,
                                end=PERIOD_END, timeout=self.settings.http_timeout)
        granules = parse_granules(items)
        if not granules:
            log.warning("gedi-l4b: no granules for AOI %s", aoi.id)
            return []
        sel = select_granules(granules, [sfx for sfx, _ in VARIABLES.values()])
        urs = sorted(g.ur for g in sel.values())
        short = urs[0].split(".", 1)[0]
        key = f"{short}:{o['version']}:{'+'.join(u.split('.', 1)[-1] for u in urs)}"
        start = min(g.start for g in sel.values())
        files = {sfx: {"ur": g.ur, "url": https_url(g), "size_bytes": g.size_of(https_url(g)) or
                       next(iter(g.sizes.values()), None)} for sfx, g in sel.items()}
        return [self.ref(key, start.astimezone(UTC), urls=[f["url"] for f in files.values()], files=files,
                         concept_id=o["concept_id"], collection=short, version=o["version"],
                         end=max((g.end or g.start) for g in sel.values()).isoformat())]

    # ------------------------------------------------------------------ extract (windowed reads)
    @contextlib.contextmanager
    def open_raster(self, url: str) -> Iterator[tuple[Any, _Transfer | None]]:
        """Open a local path or a remote COG (bearer token only for ``token_hosts``)."""
        if not url.startswith(("http://", "https://")):
            with rasterio.open(url[len("file://"):] if url.startswith("file://") else url) as src:
                yield src, None
            return
        import fsspec

        o = self._opts()
        headers = dict(self.settings.http_headers())
        if wants_token(url, o["token_hosts"]):
            token = bearer_token(self.settings)
            if not token:
                raise EarthdataAuthError("EARTHDATA_TOKEN is not set: GEDI L4B files at ORNL DAAC need an "
                                         "Earthdata Login bearer token")
            headers["Authorization"] = f"Bearer {token}"
        fs = fsspec.filesystem("http", headers=headers, skip_instance_cache=True)
        transfer = _Transfer()

        def opener(path, mode="rb"):
            f = _CountingFile(fs.open(path, "rb", block_size=BLOCK_SIZE, cache_type="blockcache"))
            transfer.files.append(f)
            return f

        try:
            with rasterio.open(url, opener=opener) as src:
                yield src, transfer
        except EarthdataAuthError:
            raise
        except rasterio.errors.RasterioIOError as exc:
            raise EarthdataAuthError(redact(f"cannot read {urlsplit(url).path[-70:]}: {exc}")) from None

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        o = self._opts()
        out, transfer = [], {}
        for sfx, f in ref.meta["files"].items():
            with self.open_raster(f["url"]) as (src, counter):
                native_res = abs(src.transform.a)
                bounds = grid_bounds_in(aoi.grid, src.crs, pad_m=o["pad_cells"] * native_res)
                window = window_for_bounds(src, bounds, pad_px=0)
                if window is None:
                    raise ValueError(f"AOI {aoi.id} is outside the GEDI L4B grid")
                arr = src.read(1, window=window)
                profile = dict(driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
                               dtype=arr.dtype.name, crs=src.crs, transform=src.window_transform(window),
                               nodata=src.nodata, compress="deflate")
                tags = {"SOURCE_GRANULE": f["ur"], "WINDOW": f"{window.col_off},{window.row_off},"
                                                            f"{window.width},{window.height}"}
            rel = self.raw_path(ref, f"{posixpath.basename(f['url']).removesuffix('.tif')}__{aoi.id}.tif")
            with self.lake.local_path(rel, "w") as path, rasterio.open(path, "w", **profile) as dst:
                dst.write(arr, 1)
                dst.update_tags(**tags)
            transfer[sfx] = {"bytes": counter.bytes_read if counter is not None else 0,
                             "file_size_bytes": f.get("size_bytes"), "window": tags["WINDOW"]}
            out.append(rel)
        note = self.raw_path(ref, f"gedi-l4b__{aoi.id}__transfer.json")
        self.lake.write_json(note, transfer)
        return [*out, note]

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        grid = aoi.grid
        iid = self.item_id(aoi, ref, discriminator=f"v{ref.meta.get('version', '2.1').replace('.', '-')}")
        note = next((p for p in raw if p.endswith("__transfer.json")), None)
        transfer = self.lake.read_json(note) if note else {}
        assets: list[StagedAsset] = []
        qc: dict[str, float] = {}
        stats: dict[str, dict] = {}
        native: dict[str, Any] = {}
        for var in self.dataset.variables:
            sfx, src_nodata = VARIABLES[var.name]
            rel = next(p for p in raw if p.endswith(f"_{sfx}__{aoi.id}.tif"))
            with self.lake.local_path(rel, "r") as path, rasterio.open(path) as src:
                arr = src.read(1)
                crs, transform = src.crs.to_string(), src.transform
                nod = src.nodata if src.nodata is not None else src_nodata
            native = {"crs": crs, "resolution_m": round(abs(transform.a), 4), "window_shape": list(arr.shape)}
            if var.is_float:
                a = arr.astype("float32")
                a[arr == nod] = np.nan
                out = reproject_array(a, crs, transform, grid, "nearest")
                out, frac = mask_out_of_range(out, var.valid_range)
                fin = out[np.isfinite(out)]
                stats[var.name] = ({"min": float(fin.min()), "max": float(fin.max()), "mean": float(fin.mean()),
                                    "median": float(np.median(fin))} if fin.size else {})
                write_cog(self.lake, self.cog_path(aoi, iid, var.name), out, grid, np.nan, var.dtype,
                          description=var.name, unit=var.unit)
            else:
                a = np.where(arr == nod, int(var.nodata), arr).astype(var.dtype)
                out = reproject_array(a, crs, transform, grid, "nearest", src_nodata=int(var.nodata))
                out, frac = mask_out_of_range(out, var.valid_range, nodata=int(var.nodata))
                vals, cnt = np.unique(out[out != int(var.nodata)], return_counts=True)
                stats[var.name] = {str(int(v)): int(c) for v, c in zip(vals, cnt, strict=True)}
                write_cog(self.lake, self.cog_path(aoi, iid, var.name), out, grid, int(var.nodata), var.dtype,
                          description=var.name, tags={"VALUES": "0=nodata/outside GEDI domain;1=land surface;"
                                                               "2=meets GEDI L1 requirement"})
            qc[var.name] = frac
            assets.append(self.cog_asset(var.name, self.cog_path(aoi, iid, var.name)))
        if not stats.get("agbd"):
            raise ValueError(f"GEDI L4B has no biomass estimate over AOI {aoi.id}")
        if note:
            assets.append(StagedAsset(key="transfer", path=note, media_type=JSON_MEDIA_TYPE, roles=["metadata"]))
        files = ref.meta.get("files", {})
        props = {
            "fuellake:qc_masked_fraction": max(qc.values()),
            "fuellake:qc_masked_fraction_by_variable": qc,
            "fuellake:source_ids": [f["ur"] for f in files.values()] + list(ref.urls),
            "fuellake:native_crs": native.get("crs"),
            "fuellake:native_resolution_m": native.get("resolution_m"),
            "fuellake:native_window_shape": native.get("window_shape"),
            "fuellake:resampling": "nearest",
            "fuellake:upsampling_factor": round(native.get("resolution_m", 0) / grid.resolution, 2),
            "fuellake:value_stats": stats,
            "fuellake:bytes_transferred": {k: v.get("bytes") for k, v in transfer.items()},
            "fuellake:stats_scope": "AOI grid before the pipeline's region clip (see fuellake:region_clip)",
            "gedi:collection_concept_id": ref.meta.get("concept_id"),
            "gedi:collection": ref.meta.get("collection"),
            "gedi:version": ref.meta.get("version"),
            "gedi:mission_weeks": "MW019-MW223",
            "gedi:citation": CITATION,
            "platform": "iss",
            "instruments": ["gedi"],
        }
        start = datetime.combine(PERIOD_START, datetime.min.time(), tzinfo=UTC)
        end = datetime(PERIOD_END.year, PERIOD_END.month, PERIOD_END.day, 23, 59, 59, tzinfo=UTC)
        return [StagedItem(item_id=iid, datetime=ref.datetime, properties=props, assets=assets,
                           start_datetime=start, end_datetime=end)]
