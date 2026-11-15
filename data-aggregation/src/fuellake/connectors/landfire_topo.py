# fuel-lake — LANDFIRE LF 2020 bare-earth topography (elevation, slope in degrees, aspect) on the AOI grid
# Licensed under Apache-2.0.
"""LANDFIRE 2020 topographic products (Elev, SlpD, Asp) via the keyless LANDFIRE ArcGIS ImageServers.

Services (verified live 2026-11-14), all ``https://lfps.usgs.gov/arcgis/rest/services/Landfire_Topo/
LF2020_<X>_CONUS/ImageServer``: int16 (``S16``), nodata -9999, 30 m on EPSG:5070 on the same raster lattice as
FBFM40. Facts from the FGDC metadata (``https://landfire.gov/sites/default/files/metadata/lf2020/CONUS/
LC20_<X>_220.xml``, edition LF 2020, published 2022-01-31, "2020 ground condition"):

==========  =======  ========================================================================  ===============
layer       service  stored value                                                              special codes
==========  =======  ========================================================================  ===============
elevation   Elev     integer metres above sea level; USGS 3DEP 1 arc-second DEM (bare earth)   0 = coastal fill
slope       SlpD     integer degrees, ArcGIS geodesic slope of the float DEM in Albers          0 = coastal fill
aspect      Asp      integer degrees 0..359 clockwise from **true north** (geodesic), downslope  -1 = flat / fill
==========  =======  ========================================================================  ===============

- Elevation: 3DEP 1 arc-second tiles (The National Map, downloaded 2021-11-09), projected to the LANDFIRE grid
  with bilinear interpolation, then converted to signed 16-bit (whole metres). The vertical datum is not stated
  (``<vertdef/>`` is empty); the 3DEP source is referenced to NAVD 88.
- Aspect -1: "Flat pixels with slope less than or equal to two (2) degrees", and also filled no-data inside the
  3 nautical mile coastal buffer. Both become NaN here (aspect is undefined). Note: Copernicus DEM aspect
  (``cop-dem``) is relative to UTM grid north and NaN only where exactly flat.
- Coastal buffer: no-data inside the LANDFIRE extent's 3 nmi sea buffer was filled with 0 (elevation, slope)
  and -1 (aspect); beyond it, -9999. The FGDC metadata of SlpD describes an unsigned 16-bit raster with nodata
  65535, but the ImageServer serves S16 with -9999 (checked on a real clip 2026-11-14); any other value outside
  ``valid_range`` is masked and counted in ``fuellake:qc_masked_fraction``.

One static unit per AOI (period ignored), key ``landfire:<service names joined by +>``, datetime 1 January of
the product year (2020). ``extract`` fetches the three clips on the native 30 m EPSG:5070 grid
(:func:`fuellake.connectors.landfire.native_clip_request`) plus the metadata XMLs into ``raw/``. ``transform``:
elevation and slope are converted to float (NaN nodata) and reprojected with the contract rule (bilinear for
30 m -> 30 m, average when the AOI grid is coarser); aspect codes are reprojected with nearest and -1 mapped to
NaN (on coarser grids: circular mean of the valid directions).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import numpy as np
import rasterio

from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.connectors.landfire import native_clip_request, parse_metadata
from fuellake.rasters import choose_resampling, mask_out_of_range, reproject_array, write_cog
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.landfire_topo")

BASE = "https://lfps.usgs.gov/arcgis/rest/services/Landfire_Topo"
META = "https://landfire.gov/sites/default/files/metadata/lf2020/CONUS"
SRC_NODATA = -9999
ASPECT_FLAT = -1
DEFAULT_LAYERS = {
    "elevation": {"code": "Elev", "kind": "continuous"},
    "slope": {"code": "SlpD", "kind": "continuous"},
    "aspect": {"code": "Asp", "kind": "aspect"},
}
ASPECT_CONVENTION = ("degrees clockwise from true north (LANDFIRE geodesic aspect), direction the slope faces "
                     "(downslope), [0, 360); NaN where LANDFIRE codes -1 (slope <= 2 degrees, or coastal fill)")


def default_urls(code: str, year: int = 2020, version_code: str = "220") -> dict[str, str]:
    return {"service": f"{BASE}/LF{year}_{code}_CONUS/ImageServer",
            "metadata_xml": f"{META}/LC{year % 100:02d}_{code}_{version_code}.xml"}


def codes_to_values(codes: np.ndarray, kind: str, nodata: int = SRC_NODATA) -> np.ndarray:
    """float32 values from the stored integers: nodata -> NaN; for ``aspect`` also -1 (flat / fill) -> NaN."""
    codes = np.asarray(codes)
    out = codes.astype("float32")
    bad = codes == nodata
    if kind == "aspect":
        bad |= codes == ASPECT_FLAT
    out[bad] = np.nan
    return out


def circular_mean_reproject(deg: np.ndarray, src_crs, src_transform, grid) -> np.ndarray:
    """Aspect (degrees, NaN = undefined) averaged onto a coarser grid as a circular mean in [0, 360)."""
    rad = np.radians(deg.astype("float64"))
    s = reproject_array(np.sin(rad).astype("float32"), src_crs, src_transform, grid, "average")
    c = reproject_array(np.cos(rad).astype("float32"), src_crs, src_transform, grid, "average")
    out = (np.degrees(np.arctan2(s, c)) % 360.0).astype("float32")
    out[~(np.isfinite(s) & np.isfinite(c)) | ((s == 0) & (c == 0))] = np.nan
    out[out >= 360.0] = 0.0
    return out


class LandfireTopoConnector(BaseConnector):
    """Static LANDFIRE LF 2020 elevation / slope / aspect item per AOI."""

    def _opts(self) -> dict:
        o = self.dataset.options
        year = int(o.get("product_year", 2020))
        version_code = str(o.get("version_code", "220"))
        layers = {}
        for name, d in {**DEFAULT_LAYERS, **(o.get("layers") or {})}.items():
            d = {**DEFAULT_LAYERS.get(name, {}), **d}
            layers[name] = {**default_urls(d["code"], year, version_code), **d}
        return {"layers": layers, "product_year": year, "product_version": o.get("product_version", f"LF {year}")}

    def _get(self, url: str, **params):
        r = self.http_session().get(url, params=params or None, timeout=self.settings.http_timeout * 3)
        r.raise_for_status()
        return r

    # ------------------------------------------------------------------ discover
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        o = self._opts()
        services: dict[str, dict] = {}
        for name, d in o["layers"].items():
            info = self._get(d["service"].rstrip("/"), f="json").json()
            if "error" in info:
                raise RuntimeError(f"LANDFIRE {d['code']} service error: {info['error']}")
            if "Image" not in str(info.get("capabilities", "")):
                raise RuntimeError(f"LANDFIRE {d['code']} service has no Image capability")
            services[name] = {k: info.get(k) for k in ("name", "pixelType", "noDataValue", "pixelSizeX",
                                                         "pixelSizeY", "extent", "spatialReference", "minValues",
                                                         "maxValues")}
        key = "landfire:" + "+".join(str(services[n]["name"]) for n in sorted(services))
        dt = datetime(o["product_year"], 1, 1, tzinfo=UTC)
        return [self.ref(key, dt, urls=[o["layers"][n]["service"] for n in sorted(services)], services=services)]

    # ------------------------------------------------------------------ extract
    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        o = self._opts()
        out = []
        for name, d in o["layers"].items():
            svc = ref.meta["services"][name]
            params = native_clip_request(aoi.grid.bounds_wgs84(), svc)
            r = self._get(f"{d['service'].rstrip('/')}/exportImage", **params)
            ctype = r.headers.get("content-type", "")
            if "tiff" not in ctype.lower():
                raise RuntimeError(f"{d['code']} exportImage returned {ctype!r}: {r.text[:300]}")
            stem = str(svc["name"]).split("/")[-1]
            tif = self.raw_path(ref, f"{stem}__{aoi.id}.tif")
            self.lake.write_bytes(tif, r.content)
            out.append(tif)
            if d.get("metadata_xml"):
                xml_rel = self.raw_path(ref, d["metadata_xml"].rsplit("/", 1)[-1])
                self.lake.write_bytes(xml_rel, self._get(d["metadata_xml"]).content)
                out.append(xml_rel)
        return out

    # ------------------------------------------------------------------ transform
    @staticmethod
    def _raw_for(raw: list[str], layer: dict) -> tuple[str, str | None]:
        tif = next(p for p in raw if p.endswith(".tif") and f"_{layer['code']}_CONUS__" in p.rsplit("/", 1)[-1])
        xml_name = str(layer.get("metadata_xml") or "").rsplit("/", 1)[-1]
        xml_rel = next((p for p in raw if xml_name and p.rsplit("/", 1)[-1] == xml_name), None)
        return tif, xml_rel

    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        o = self._opts()
        grid = aoi.grid
        iid = self.item_id(aoi, ref, discriminator=str(o["product_version"]).lower().replace(" ", ""))
        assets = []
        qc: dict[str, float] = {}
        layers_props: dict[str, dict] = {}
        resampling: dict[str, str] = {}
        arrays: dict[str, np.ndarray] = {}
        for var in self.dataset.variables:
            d = o["layers"][var.name]
            tif, xml_rel = self._raw_for(raw, d)
            meta = parse_metadata(self.lake.read_text(xml_rel, encoding="utf-8-sig")) if xml_rel else {}
            with self.lake.local_path(tif, "r") as path, rasterio.open(path) as src:
                codes = src.read(1)
                src_nodata = int(src.nodata) if src.nodata is not None else SRC_NODATA
                crs, transform, src_res = src.crs.to_string(), src.transform, abs(src.transform.a)
                src_dtype = str(codes.dtype)
            codes = codes.astype("int32")
            n_src = int((codes != src_nodata).sum())
            n_flat = int((codes == ASPECT_FLAT).sum()) if d["kind"] == "aspect" else None
            values = codes_to_values(codes, d["kind"], nodata=src_nodata)
            # implausible source values become NaN before resampling, so they cannot bleed into neighbours
            values, src_frac = mask_out_of_range(values, var.valid_range)
            codes[~np.isfinite(values) & (codes != src_nodata) & (codes != ASPECT_FLAT)] = src_nodata
            coarser = grid.resolution > src_res * 1.0001
            if d["kind"] == "aspect":
                if coarser:
                    resampling[var.name] = "circular-mean"
                    arr = circular_mean_reproject(values, crs, transform, grid)
                else:
                    resampling[var.name] = "nearest"
                    on_grid = reproject_array(codes, crs, transform, grid, "nearest", src_nodata=src_nodata)
                    arr = codes_to_values(on_grid, "aspect", nodata=src_nodata)
            else:
                resampling[var.name] = choose_resampling(False, src_res, grid.resolution)
                arr = reproject_array(values, crs, transform, grid, resampling[var.name])
            arr, frac = mask_out_of_range(arr, var.valid_range)
            qc[var.name] = max(frac, src_frac)
            if var.name != "aspect" and not np.isfinite(arr).any():
                raise ValueError(f"LANDFIRE {d['code']} clip has no data over AOI {aoi.id}")
            arrays[var.name] = arr
            path = self.cog_path(aoi, iid, var.name)
            write_cog(self.lake, path, arr, grid, np.nan, var.dtype, description=var.name, unit=var.unit,
                      tags={"LANDFIRE_PRODUCT": d["code"], "LANDFIRE_RELEASE": o["product_version"]})
            assets.append(self.cog_asset(var.name, path))
            finite = arr[np.isfinite(arr)]
            layers_props[var.name] = {
                "landfire_product": d["code"], "service": d["service"], "metadata_xml": d.get("metadata_xml"),
                "source_dtype": src_dtype, "source_nodata": src_nodata,
                "title": meta.get("title"), "edition": meta.get("edition"), "pubdate": meta.get("pubdate"),
                "ground_condition": meta.get("ground_condition"), "use_constraints": meta.get("use_constraints"),
                "source_cells": n_src,
                "stats": ({"min": float(finite.min()), "max": float(finite.max()), "mean": float(finite.mean())}
                          if finite.size else {}),
            }
            if n_flat is not None:
                layers_props[var.name]["flat_or_fill_cells_source"] = n_flat
        elev = arrays.get("elevation")
        props = {
            "fuellake:qc_masked_fraction": max(qc.values()) if qc else 0.0,
            "fuellake:qc_masked_fraction_by_variable": qc,
            "fuellake:landfire_version": o["product_version"],
            "fuellake:landfire_layers": layers_props,
            "fuellake:resampling": resampling,
            "fuellake:surface_model": ("bare-earth DTM: USGS 3DEP 1 arc-second DEM (The National Map, downloaded "
                                       "2021-11-09), bilinear to the LANDFIRE 30 m Albers grid, whole metres"),
            "fuellake:vertical_datum": ("not stated in the LANDFIRE metadata ('meters above sea level'); the 3DEP "
                                        "source is referenced to NAVD 88"),
            "fuellake:slope_method": "LANDFIRE SlpD: ArcGIS geodesic slope of the float DEM, whole degrees",
            "fuellake:aspect_convention": ASPECT_CONVENTION,
            "fuellake:coastal_fill": ("inside the 3 nmi coastal buffer LANDFIRE filled no-data with elevation 0, "
                                      "slope 0 and aspect -1 (-> NaN); the region clip removes most of it"),
            "fuellake:stats_scope": "AOI grid before the pipeline's region clip (see fuellake:region_clip)",
        }
        if elev is not None and np.isfinite(elev).any():
            props["fuellake:elevation_range_m"] = [float(np.nanmin(elev)), float(np.nanmax(elev))]
        start = datetime(o["product_year"], 1, 1, tzinfo=UTC)
        end = datetime(o["product_year"], 12, 31, 23, 59, 59, tzinfo=UTC)
        return [StagedItem(item_id=iid, datetime=ref.datetime, properties=props, assets=assets,
                           start_datetime=start, end_datetime=end)]
