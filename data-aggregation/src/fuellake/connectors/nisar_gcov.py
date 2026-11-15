# fuel-lake — connector: NISAR L2 GCOV L-band gamma0 backscatter (HH, HV) streamed for the AOI only
# Licensed under Apache-2.0.
"""NISAR L2 GCOV (Geocoded Polarimetric Covariance) -> ``gamma0_hh_db`` / ``gamma0_hv_db`` on the AOI grid.

Discovery (:func:`fuellake.connectors._earthdata.discover_nisar`): CMR granule search on the collection of
the configured maturity (default PROVISIONAL, ``C2854338529-ASF``, CRID P05023), production pipeline ``PR``
only, the AOI's preferred stack (``smm``: track 13, frame 71, descending), duplicates resolved to the
highest product counter. One unit = one granule (``SourceRef.key`` = GranuleUR).

Extract never downloads the 4.6-7.4 GB file. It opens the HDF5 over HTTPS with range reads (h5py on an
fsspec HTTP file with a bearer token, following the ASF NISAR Cookbook streaming notebook), reads the 1-D
``xCoordinates`` / ``yCoordinates`` vectors, computes the pixel window covering the AOI grid (plus a small
pad), and reads only that window of the covariance terms and of ``mask``. The window is stored as a small
HDF5 subset under ``raw/nisar-gcov/<yyyy>/<mm>/<granule>__<aoi>.h5`` and reused on later runs.

HDF5 paths (GCOV product specification JPL D-102274 Rev E, v1.2.1, section 5; dataset list identical in
Rev D; checked on the public sample product NISAR_L2_PR_GCOV_001_030_A_019_2800_SHNA_..._D00404):

- ``/science/LSAR/GCOV/grids/frequency{A|B}/{HHHH,HVHV}``: gamma0 covariance diagonal terms, float32,
  **linear power**, ``_FillValue`` NaN, ``valid_min`` 0.
- ``.../mask``: uint8. ``0`` invalid or partially focused, ``1``-``5`` valid (value = sub-swath number),
  ``255`` fill (outside the RSLC image). **It does not encode layover or shadow** (spec section 4.3.3);
  GCOV has no layover/shadow layer, so steep slopes are not masked here.
- ``.../xCoordinates``, ``.../yCoordinates`` (float64, pixel centres: on the sample the pixel edges fall on
  multiples of the posting), ``.../xCoordinateSpacing``, ``.../yCoordinateSpacing`` (negative),
  ``.../projection`` (scalar EPSG code, attribute ``epsg_code``), ``.../listOfPolarizations``.
- ``/science/LSAR/identification/{trackNumber,frameNumber,orbitPassDirection,compositeReleaseId,
  zeroDopplerStartTime,granuleId}`` (copied into the raw subset as attributes).

Transform: pixels with ``mask`` 0 or 255, non-finite or ``<= 0`` values become NaN; the linear power is
averaged onto the 30 m AOI grid (``average`` resampling, the contract rule for continuous downsampling;
10 m -> 30 m averages 3x3 valid pixels), **then** converted to dB, then masked to the variable's
``valid_range``. Averaging in linear power before the log is the correct order for backscatter.

Options (``config/datasets/nisar-gcov.yaml``): ``maturity``, ``collections``, ``stack`` (``preferred`` |
``all``), ``min_aoi_coverage``, ``strict_crid``, ``frequency`` (``A``), ``pad_m`` (window pad, metres),
``mask_policy`` (``subswath`` = spec rule, default; ``fill_only`` = reject only 255). On the public D00404
sample the whole-frame mask holds only 0 and 255 (no sub-swath numbers), so the sample was processed with
``fill_only``; with the spec rule a window whose finite pixels are all rejected raises
:class:`MaskRejectsAllError` instead of silently staging nothing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import h5py
import numpy as np
from rasterio.warp import transform_bounds

from fuellake.connectors import _earthdata as ed
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.rasters import mask_out_of_range, reproject_array, write_cog
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.nisar_gcov")

GRIDS = "/science/LSAR/GCOV/grids"
IDENT = "/science/LSAR/identification"
IDENT_FIELDS = ("trackNumber", "frameNumber", "orbitPassDirection", "compositeReleaseId", "zeroDopplerStartTime",
                "zeroDopplerEndTime", "granuleId", "productVersion", "productSpecificationVersion", "isFullFrame",
                "isJointObservation", "missionId", "instrumentName", "listOfFrequencies", "radarBand")
# variable name in the dataset config -> polarization -> covariance term
VARIABLE_POL = {"gamma0_hh_db": "HH", "gamma0_hv_db": "HV", "gamma0_vv_db": "VV", "gamma0_vh_db": "VH"}
MASK_VALID = (1, 5)          # sub-swath numbers 1..5 are valid; 0 invalid, 255 fill
MASK_FILL = 255
SUBSET_VERSION = 1


def read_gcov_subset(f: h5py.File, grid, frequency: str = "A", polarizations: list[str] | None = None,
                     pad_m: float = 60.0) -> dict[str, Any] | None:
    """Read the AOI window of a GCOV file (open h5py handle). Returns arrays + georeferencing, or ``None``
    when the AOI grid does not intersect the product grid."""
    g = f[f"{GRIDS}/frequency{frequency}"]
    avail = ed.h5_str_list(g["listOfPolarizations"][()])
    pols = [p for p in (polarizations or avail) if p in avail]
    proj = g["projection"]
    epsg = int(proj.attrs.get("epsg_code", proj[()]))
    xc = g["xCoordinates"][()]
    yc = g["yCoordinates"][()]
    dx = float(g["xCoordinateSpacing"][()])
    dy = float(g["yCoordinateSpacing"][()])
    bounds = transform_bounds(grid.crs, f"EPSG:{epsg}", *grid.bounds(), densify_pts=21)
    win = ed.window_from_centres(xc, yc, bounds, pad=max(pad_m, 2 * abs(dx)))
    if win is None:
        return None
    rs, cs = win
    out: dict[str, Any] = {
        "epsg": epsg, "x0": float(xc[cs.start]), "y0": float(yc[rs.start]), "dx": dx, "dy": dy,
        "window": [rs.start, rs.stop, cs.start, cs.stop], "full_shape": list(g["mask"].shape),
        "frequency": frequency, "polarizations": pols, "available_polarizations": avail,
        "arrays": {},
    }
    for p in pols:
        out["arrays"][p + p] = g[p + p][rs, cs]
    out["arrays"]["mask"] = g["mask"][rs, cs]
    ident = {}
    if IDENT in f:
        gi = f[IDENT]
        for k in IDENT_FIELDS:
            if k in gi:
                v = gi[k][()]
                ident[k] = ed.h5_str_list(v) if np.ndim(v) else ed.h5_str(v)
    out["identification"] = ident
    return out


def write_subset(path: str, subset: dict[str, Any], source_url: str) -> None:
    with h5py.File(path, "w") as h:
        for name, arr in subset["arrays"].items():
            h.create_dataset(name, data=arr, compression="gzip", compression_opts=4, shuffle=True)
        for k in ("epsg", "x0", "y0", "dx", "dy", "frequency"):
            h.attrs[k] = subset[k]
        h.attrs["window"] = subset["window"]
        h.attrs["full_shape"] = subset["full_shape"]
        h.attrs["polarizations"] = json.dumps(subset["polarizations"])
        h.attrs["available_polarizations"] = json.dumps(subset["available_polarizations"])
        h.attrs["identification"] = json.dumps(subset["identification"])
        h.attrs["source_url"] = source_url
        h.attrs["fuellake_subset_version"] = SUBSET_VERSION
        h.attrs["complete"] = 1


def read_subset(path: str) -> dict[str, Any]:
    with h5py.File(path, "r") as h:
        if int(h.attrs.get("complete", 0)) != 1:
            raise ValueError(f"{path}: incomplete raw subset")
        sub = {k: h.attrs[k] for k in ("epsg", "x0", "y0", "dx", "dy")}
        sub["epsg"] = int(sub["epsg"])
        sub["frequency"] = ed.h5_str(h.attrs["frequency"])
        sub["polarizations"] = json.loads(ed.h5_str(h.attrs["polarizations"]))
        sub["identification"] = json.loads(ed.h5_str(h.attrs["identification"]))
        sub["arrays"] = {k: h[k][()] for k in h.keys()}
    return sub


MASK_POLICIES = ("subswath", "fill_only")


class MaskRejectsAllError(ValueError):
    """The GCOV mask rejects every finite pixel of the window (mask semantics differ from the spec?)."""


def mask_invalid(mask: np.ndarray, policy: str = "subswath") -> np.ndarray:
    """Boolean array of pixels the ``mask`` layer marks unusable.

    ``subswath`` (default, spec section 4.3.3): only values 1-5 are valid. ``fill_only``: only 255 (outside
    the RSLC image) is rejected; for products whose mask carries no sub-swath numbers, such as the
    pre-launch D00404 sample, where every pixel is 0 or 255.
    """
    m = np.asarray(mask)
    if policy == "subswath":
        return (m < MASK_VALID[0]) | (m > MASK_VALID[1])
    if policy == "fill_only":
        return m == MASK_FILL
    raise ValueError(f"mask_policy must be one of {MASK_POLICIES}, got {policy!r}")


def gamma0_to_grid_db(lin: np.ndarray, mask: np.ndarray, src_crs: str, src_transform, grid,
                      resampling: str = "average", mask_policy: str = "subswath") -> np.ndarray:
    """Mask invalid pixels, average linear gamma0 onto ``grid``, convert to dB (float32, NaN nodata)."""
    a = np.asarray(lin, dtype="float32").copy()
    ed.check_linear_power(a, "GCOV gamma0")
    usable = np.isfinite(a) & (a > 0)
    bad_mask = mask_invalid(mask, mask_policy)
    if usable.any() and not (usable & ~bad_mask).any():
        values = sorted(int(v) for v in np.unique(np.asarray(mask)[usable])[:8])
        raise MaskRejectsAllError(
            f"GCOV mask (policy {mask_policy!r}) rejects all {int(usable.sum())} finite pixels of the window; "
            f"mask values there: {values}. Expected sub-swath numbers 1-5 (spec 4.3.3).")
    a[~usable | bad_mask] = np.nan
    avg = reproject_array(a, src_crs, src_transform, grid, resampling=resampling)
    return ed.to_db(avg)


class NisarGcovConnector(BaseConnector):
    """NISAR L2 GCOV gamma0 HH/HV, streamed for the AOI window and averaged to the AOI grid."""

    product = "GCOV"

    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        return ed.discover_nisar(self, aoi, period, self.product)

    # ------------------------------------------------------------------ extract
    def _wanted_pols(self) -> list[str]:
        return [VARIABLE_POL[v.name] for v in self.dataset.variables if v.name in VARIABLE_POL]

    def raw_rel(self, ref: SourceRef, aoi: AOI) -> str:
        return self.raw_path(ref, f"{ref.meta.get('granule_ur', ref.key)}__{aoi.id}.h5")

    def missing_credentials(self, aoi: AOI, ref: SourceRef | None = None) -> list[str]:
        """CMR discovery is public; a unit needs ``EARTHDATA_TOKEN`` unless its AOI subset is already in
        ``raw/`` or the file is in the public sample-data area."""
        if ref is None or self.lake.exists(self.raw_rel(ref, aoi)):
            return []
        return ed.missing_token(self.settings, ref.urls[:1])

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        rel = self.raw_rel(ref, aoi)
        if self.lake.exists(rel):
            try:
                with self.lake.local_path(rel, "r") as p:
                    read_subset(p)
                return [rel]
            except Exception as exc:  # corrupt or partial: fetch again
                log.warning("re-extracting %s: %s", rel, exc)
        opts = self.dataset.options
        url = ref.urls[0]
        with ed.open_h5(url, self.settings) as f:
            subset = read_gcov_subset(f, aoi.grid, frequency=str(opts.get("frequency", "A")),
                                      polarizations=self._wanted_pols(), pad_m=float(opts.get("pad_m", 60.0)))
        if subset is None:
            log.warning("%s: AOI %s does not intersect the product grid", ref.key, aoi.id)
            return []
        with self.lake.local_path(rel, "w") as path:
            write_subset(path, subset, url)
        return [rel]

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        if not raw:
            return []
        with self.lake.local_path(raw[0], "r") as p:
            sub = read_subset(p)
        grid = aoi.grid
        src_crs = f"EPSG:{sub['epsg']}"
        src_t = ed.transform_from_centres(sub["x0"], sub["y0"], sub["dx"], sub["dy"])
        mask = sub["arrays"]["mask"]
        policy = str(self.dataset.options.get("mask_policy", "subswath"))
        iid = self.item_id(aoi, ref, ed.item_discriminator(ref))
        assets, fractions, valid, staged_pols = [], [], [], []
        for var in self.dataset.variables:
            pol = VARIABLE_POL.get(var.name)
            term = (pol or "") * 2
            if pol is None or term not in sub["arrays"]:
                continue
            db = gamma0_to_grid_db(sub["arrays"][term], mask, src_crs, src_t, grid, var.resampling, policy)
            db, frac = mask_out_of_range(db, var.valid_range)
            n_valid = int(np.isfinite(db).sum())
            if n_valid == 0:
                continue
            path = self.cog_path(aoi, iid, var.name)
            write_cog(self.lake, path, db, grid, np.nan, var.dtype,
                      description=f"NISAR GCOV gamma0 {pol} (dB), frequency {sub['frequency']}", unit=var.unit)
            assets.append(self.cog_asset(var.name, path))
            fractions.append(frac)
            valid.append(n_valid / db.size)
            staged_pols.append(pol)
        if not assets:
            log.warning("%s: no valid GCOV pixels inside AOI %s", ref.key, aoi.id)
            return []
        n_mask = mask.size
        props = ed.nisar_item_properties(ref, "GCOV", staged_pols)
        ident = sub.get("identification", {})
        if ident.get("compositeReleaseId"):
            props["fuellake:processing_id"] = ident["compositeReleaseId"]
        props.update({
            "fuellake:qc_masked_fraction": float(max(fractions)),
            "fuellake:valid_fraction": float(min(valid)),
            "nisar:frequency": sub["frequency"],
            "nisar:native_posting_m": abs(float(sub["dx"])),
            "nisar:backscatter": "gamma0, averaged in linear power to the AOI grid, then 10*log10",
            "nisar:mask_invalid_fraction": float(((mask == 0)).sum() / n_mask) if n_mask else 0.0,
            "nisar:mask_policy": policy,
            "nisar:mask_note": "GCOV mask = sub-swath validity (0 invalid, 1-5 valid, 255 fill); "
                               "no layover/shadow masking applied",
        })
        return [StagedItem(item_id=iid, datetime=ref.datetime, properties=props, assets=assets)]
