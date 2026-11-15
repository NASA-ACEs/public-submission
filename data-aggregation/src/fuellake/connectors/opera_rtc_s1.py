# fuel-lake — connector: OPERA RTC-S1 C-band gamma0 (VV, VH) bursts mosaicked per pass onto the AOI grid
# Licensed under Apache-2.0.
"""OPERA L2 RTC-S1 (radiometric terrain-corrected Sentinel-1 backscatter) -> ``gamma0_vv_db`` / ``gamma0_vh_db``.

Collection ``OPERA_L2_RTC-S1_V1`` = ``C2777436413-ASF``. Each granule is one IW burst with three 30 m
GeoTIFFs: ``<granule>_VV.tif``, ``<granule>_VH.tif`` and ``<granule>_mask.tif`` (file names from the CMR
records; about 7 MB per polarization). Granule name::

    OPERA_L2_RTC-S1_<burst T137-292315-IW3>_<start>Z_<production>Z_<S1A|S1C|S1D>_30_v1.0

Discovery: CMR search (public, no token) by AOI bbox and period. Reprocessed copies of the same burst and
start time (different production time) keep the newest. Bursts are grouped into one unit per
acquisition: UTC date x platform x relative orbit x pass direction. ``SourceRef.key`` is
``<sensor>_<yyyymmdd>_T<track><A|D>_<hash of the burst granule ids>``, so a late-arriving burst creates
a new key and the item is rebuilt (``item.updated``). Groups covering less than
``options.min_aoi_coverage`` (default 0.5) of the AOI bbox are skipped.

Extract downloads the needed burst GeoTIFFs (small files) to ``raw/opera-rtc-s1/<yyyy>/<mm>/<granule>/``
with the Earthdata bearer token; existing files are reused.

Transform, per polarization: per burst, pixels with ``mask`` 255 (invalid / fill) and, when
``options.mask_layover_shadow`` is true (default), mask 1 (shadow), 2 (layover) or 3 (both) become NaN
(mask codes from the ASF OPERA RTC-S1 product guide); non-finite and ``<= 0`` values are NaN. Each burst
is resampled onto the AOI grid in **linear power** (variable ``resampling``, bilinear: the 30 m UTM burst
grid and the 30 m AOI grid have the same posting), overlapping bursts are averaged (nanmean, linear
power), then converted to dB and masked to ``valid_range``.

Unverified on real files (no token in the development session): that the VV/VH GeoTIFF values are linear
gamma0 power (the ASF guide says "gamma-0 power"; :func:`fuellake.connectors._earthdata.check_linear_power`
refuses dB-looking input) and the mask codes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import warnings
from collections import defaultdict
from typing import Any

import numpy as np
import rasterio

from fuellake.connectors import _earthdata as ed
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.rasters import mask_out_of_range, reproject_array, write_cog
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.opera_rtc_s1")

OPERA_NAME_RE = re.compile(
    r"^OPERA_L2_RTC-S1_(?P<burst>T(?P<track>\d{3})-(?P<burst_num>\d{6})-(?P<swath>IW\d))_"
    r"(?P<start>\d{8}T\d{6}Z)_(?P<production>\d{8}T\d{6}Z)_(?P<sensor>S1[A-Z])_(?P<posting>\d+)_v(?P<version>[\d.]+)$"
)
VARIABLE_POL = {"gamma0_vv_db": "VV", "gamma0_vh_db": "VH", "gamma0_hh_db": "HH", "gamma0_hv_db": "HV"}
MASK_INVALID = 255
MASK_LAYOVER_SHADOW = (1, 2, 3)
PLATFORMS = {"S1A": "sentinel-1a", "S1B": "sentinel-1b", "S1C": "sentinel-1c", "S1D": "sentinel-1d"}


def parse_opera_name(name: str) -> dict[str, Any] | None:
    m = OPERA_NAME_RE.match(name.rsplit("/", 1)[-1])
    if not m:
        return None
    d: dict[str, Any] = m.groupdict()
    d["track"] = int(d["track"])
    d["posting"] = int(d["posting"])
    return d


def group_bursts(granules: list[ed.Granule]) -> dict[tuple, list[tuple[ed.Granule, dict]]]:
    """Dedupe reprocessed bursts (keep the newest production time) and group them per acquisition."""
    parsed = [(g, parse_opera_name(g.ur)) for g in granules]
    parsed = [(g, n) for g, n in parsed if n is not None]
    parsed = ed.dedupe_latest(parsed, key=lambda t: (t[1]["burst"], t[1]["start"]),
                              version=lambda t: t[1]["production"])
    groups: dict[tuple, list[tuple[ed.Granule, dict]]] = defaultdict(list)
    for g, n in parsed:
        direction = (g.attr("ASCENDING_DESCENDING") or "").lower()
        groups[(g.start.strftime("%Y%m%d"), n["sensor"], n["track"], direction)].append((g, n))
    return dict(groups)


class OperaRtcS1Connector(BaseConnector):
    """OPERA RTC-S1 VV/VH bursts mosaicked per Sentinel-1 pass onto the AOI grid (dB)."""

    def _pols(self) -> list[str]:
        return [VARIABLE_POL[v.name] for v in self.dataset.variables if v.name in VARIABLE_POL]

    # ------------------------------------------------------------------ discover
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        from shapely.ops import unary_union

        opts = self.dataset.options
        concept = opts.get("collection_concept_id", "C2777436413-ASF")
        min_cov = float(opts.get("min_aoi_coverage", 0.5))
        session = ed.cmr_session(self.settings)
        items = ed.search_granules(session, concept, bbox=aoi.bbox, start=period.start, end=period.end)
        groups = group_bursts(ed.parse_granules(items))
        refs = []
        for (day, sensor, track, direction), members in sorted(groups.items()):
            members.sort(key=lambda t: t[1]["burst"])
            geoms = [ed.footprint(g) for g, _ in members]
            cov = ed.aoi_coverage(unary_union([x for x in geoms if x is not None]), aoi)
            if cov < min_cov:
                continue
            bursts = []
            for g, n in members:
                files = {}
                for layer in (*self._pols(), "mask"):
                    u = g.url_endswith(f"_{layer}.tif", exclude=())
                    if u:
                        files[layer] = u
                if "mask" not in files or not any(p in files for p in self._pols()):
                    continue
                bursts.append({"ur": g.ur, "burst_id": n["burst"], "start": g.start.isoformat(),
                               "production": n["production"], "files": files,
                               "polarizations": list(g.attrs.get("POLARIZATION", ()))})
            if not bursts:
                continue
            digest = hashlib.sha1("|".join(b["ur"] for b in bursts).encode()).hexdigest()[:10]
            d0 = (direction or "x")[0].upper()
            key = f"{sensor}_{day}_T{track:03d}{d0}_{digest}"
            urls = [u for b in bursts for u in b["files"].values()]
            refs.append(self.ref(
                key, min(g.start for g, _ in members), urls=urls,
                sensor=sensor, platform=PLATFORMS.get(sensor, sensor.lower()), track=track, orbit_state=direction,
                bursts=bursts, aoi_coverage=round(cov, 4), concept_id=concept,
                polarizations=sorted({p for b in bursts for p in b["polarizations"]}),
            ))
        return sorted(refs, key=lambda r: (r.datetime, r.key))

    # ------------------------------------------------------------------ extract
    def _raw_files(self, ref: SourceRef) -> list[tuple[str, str]]:
        """``(url, lake path)`` of every burst file of the unit."""
        return [(url, self.raw_path(ref, f"{b['ur']}/{url.rsplit('/', 1)[-1]}"))
                for b in ref.meta["bursts"] for _layer, url in sorted(b["files"].items())]

    def missing_credentials(self, aoi: AOI, ref: SourceRef | None = None) -> list[str]:
        """CMR discovery is public; a unit needs ``EARTHDATA_TOKEN`` for burst files not yet in ``raw/``."""
        if ref is None:
            return []
        return ed.missing_token(self.settings, [u for u, rel in self._raw_files(ref) if not self.lake.exists(rel)])

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        out = []
        for b in ref.meta["bursts"]:
            for _layer, url in sorted(b["files"].items()):
                rel = self.raw_path(ref, f"{b['ur']}/{url.rsplit('/', 1)[-1]}")
                if not self.lake.exists(rel):
                    with self.lake.local_path(rel, "w") as path:
                        part = path + ".part"
                        ed.download(url, part, self.settings)
                        os.replace(part, path)
                out.append(rel)
        return out

    # ------------------------------------------------------------------ transform
    def _read(self, rel: str) -> tuple[np.ndarray, Any, Any, Any]:
        with self.lake.local_path(rel, "r") as p, rasterio.open(p) as src:
            return src.read(1), src.crs, src.transform, src.nodata

    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        by_burst: dict[str, dict[str, str]] = defaultdict(dict)
        for rel in raw:
            parent, fname = rel.rsplit("/", 2)[-2:]
            layer = fname.rsplit("_", 1)[-1].split(".")[0]
            by_burst[parent][layer] = rel
        mask_ls = bool(self.dataset.options.get("mask_layover_shadow", True))
        grid = aoi.grid
        iid = self.item_id(aoi, ref, f"{ref.meta['sensor'].lower()}_t{int(ref.meta['track']):03d}"
                                     f"{(ref.meta.get('orbit_state') or 'x')[0]}")
        masks: dict[str, tuple] = {}
        n_valid_native = n_ls_native = 0
        for burst, layers in by_burst.items():
            if "mask" in layers:
                m, crs, tr, _ = self._read(layers["mask"])
                masks[burst] = (m, crs, tr)
                valid = m != MASK_INVALID
                n_valid_native += int(valid.sum())
                n_ls_native += int(np.isin(m, MASK_LAYOVER_SHADOW).sum())
        assets, fractions, valid_fracs, staged_pols = [], [], [], []
        for var in self.dataset.variables:
            pol = VARIABLE_POL.get(var.name)
            if pol is None:
                continue
            stack = []
            for burst, layers in sorted(by_burst.items()):
                if pol not in layers or burst not in masks:
                    continue
                arr, crs, tr, nodata = self._read(layers[pol])
                lin = arr.astype("float32", copy=True)
                if nodata is not None and not np.isnan(nodata):
                    lin[arr == nodata] = np.nan
                ed.check_linear_power(lin, f"OPERA {pol} {burst}")
                m = masks[burst][0]
                if m.shape != lin.shape:
                    raise ValueError(f"{burst}: mask shape {m.shape} != {pol} shape {lin.shape}")
                bad = ~np.isfinite(lin) | (lin <= 0) | (m == MASK_INVALID)
                if mask_ls:
                    bad |= np.isin(m, MASK_LAYOVER_SHADOW)
                lin[bad] = np.nan
                stack.append(reproject_array(lin, crs, tr, grid, resampling=var.resampling))
            if not stack:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)   # all-NaN columns
                mosaic = np.nanmean(np.stack(stack), axis=0).astype("float32")
            db, frac = mask_out_of_range(ed.to_db(mosaic), var.valid_range)
            if not np.isfinite(db).any():
                continue
            path = self.cog_path(aoi, iid, var.name)
            write_cog(self.lake, path, db, grid, np.nan, var.dtype,
                      description=f"OPERA RTC-S1 gamma0 {pol} (dB), {len(stack)} burst(s)", unit=var.unit)
            assets.append(self.cog_asset(var.name, path))
            fractions.append(frac)
            valid_fracs.append(float(np.isfinite(db).sum() / db.size))
            staged_pols.append(pol)
        if not assets:
            log.warning("%s: no valid OPERA pixels inside AOI %s", ref.key, aoi.id)
            return []
        m = ref.meta
        props = {
            "fuellake:qc_masked_fraction": float(max(fractions)),
            "fuellake:valid_fraction": float(min(valid_fracs)),
            "fuellake:aoi_coverage": m.get("aoi_coverage"),
            "fuellake:source_ids": [b["ur"] for b in m["bursts"]],
            "platform": m["platform"],
            "constellation": "sentinel-1",
            "instruments": ["c-sar"],
            "sat:relative_orbit": int(m["track"]),
            "sat:orbit_state": (m.get("orbit_state") or "").lower() or None,
            "sar:frequency_band": "C",
            "sar:center_frequency": 5.405,
            "sar:polarizations": staged_pols,
            "sar:instrument_mode": "IW",
            "sar:product_type": "RTC",
            "opera:burst_ids": [b["burst_id"] for b in m["bursts"]],
            "opera:n_bursts": len(m["bursts"]),
            "opera:mosaic": "nanmean of overlapping bursts in linear power, then 10*log10",
            "opera:layover_shadow_masked": mask_ls,
            "opera:layover_shadow_fraction": (n_ls_native / n_valid_native) if n_valid_native else None,
        }
        return [StagedItem(item_id=iid, datetime=ref.datetime, properties={k: v for k, v in props.items()
                                                                           if v is not None}, assets=assets)]
