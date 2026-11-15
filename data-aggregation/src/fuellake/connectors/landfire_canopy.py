# fuel-lake — LANDFIRE forest canopy cover, height, base height and bulk density on the AOI grid
# Licensed under Apache-2.0.
"""LANDFIRE 2025 canopy fuel layers (CC, CH, CBH, CBD) via the keyless LANDFIRE ArcGIS ImageServers.

Services (verified live 2026-11-14), all ``https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2025/
LF2025_<X>_CONUS/ImageServer``: int16 (``S16``), nodata -9999, 30 m on EPSG:5070, same raster origin as FBFM40.
Units and scales from the FGDC metadata (``LF2025_<X>_CONUS.xml``) and the attribute CSVs (``LF2025_<X>.csv``):

====================  =======  ==================================================  =========  ==============
layer                 service  stored value                                         scale      range (codes)
====================  =======  ==================================================  =========  ==============
canopy_cover          CC       percent, 10 % bin midpoints (15, 25, ... 95)         x 1        0..95
canopy_height         CH       metres x 10, height-class midpoints (30, 70, ...)   x 0.1      0..510
canopy_base_height    CBH      metres x 10 (0.1 m steps)                            x 0.1      0..100
canopy_bulk_density   CBD      kg/m3 x 100                                          x 0.01     0..45
====================  =======  ==================================================  =========  ==============

Code 0 is "Non-Forested" in all four layers. It becomes 0 % cover, 0 m height and 0 kg/m3 bulk density, but
**NaN base height** (a base height of 0 m would read as crown fuel touching the ground; use ``canopy_cover == 0``
to tell non-forested pixels from nodata). The scale of each layer is checked at run time against the name of the
CSV's second column (``CC_PERCENT``, ``METERSX10``, ``KGM3_X_100``), so a changed product encoding fails loudly.

One static unit per AOI (period ignored), key ``landfire:<service names joined by +>``, datetime 1 January of
the product year. ``extract`` fetches the four clips on the native 30 m EPSG:5070 grid (:func:`native_clip_request`
of the FBFM40 connector) plus the CSVs and metadata XMLs into ``raw/``. ``transform`` reprojects with nearest
neighbour on the codes when the AOI grid is as fine as 30 m (keeps LANDFIRE's binned values), or averages the
physical values when the AOI grid is coarser; codes not in the CSV become nodata (``fuellake:qc_masked_fraction``).
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime

import numpy as np
import rasterio

from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedAsset, StagedItem
from fuellake.connectors.landfire import native_clip_request, parse_metadata
from fuellake.rasters import mask_out_of_range, reproject_array, write_cog
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.landfire_canopy")

BASE = "https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2025"
FILES = "https://landfire.gov/sites/default/files"
SRC_NODATA = -9999
CSV_MEDIA_TYPE = "text/csv"
# scale implied by the official CSV column naming the stored value
SCALE_BY_COLUMN = {"CC_PERCENT": 1.0, "METERSX10": 0.1, "KGM3_X_100": 0.01}
DEFAULT_LAYERS = {
    "canopy_cover": {"code": "CC", "scale": 1.0, "non_forested": 0.0},
    "canopy_height": {"code": "CH", "scale": 0.1, "non_forested": 0.0},
    "canopy_base_height": {"code": "CBH", "scale": 0.1, "non_forested": "nan"},
    "canopy_bulk_density": {"code": "CBD", "scale": 0.01, "non_forested": 0.0},
}


def default_urls(code: str, year: int = 2025) -> dict[str, str]:
    return {"service": f"{BASE}/LF{year}_{code}_CONUS/ImageServer",
            "classes_csv": f"{FILES}/CSV/LF{year}/LF{year}_{code}.csv",
            "metadata_xml": f"{FILES}/metadata/LF{year}/CONUS/LF{year}_{code}_CONUS.xml"}


def parse_value_table(text: str) -> tuple[str, dict[int, str]]:
    """``(value column name, {code: label})`` from a LANDFIRE attribute CSV (BOM tolerated; drops -9999)."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows or rows[0][0].strip().upper() != "VALUE" or len(rows[0]) < 2:
        raise ValueError(f"unexpected LANDFIRE CSV header {rows[0] if rows else None}")
    col = rows[0][1].strip()
    out = {int(r[0]): r[1].strip() for r in rows[1:] if r and r[0].strip() and int(r[0]) != SRC_NODATA}
    if not out:
        raise ValueError("LANDFIRE CSV has no classes")
    return col, out


def check_scale(layer: str, column: str, scale: float) -> None:
    """Fail if the CSV value column implies another scale than the configured one."""
    implied = SCALE_BY_COLUMN.get(column.upper())
    if implied is None:
        raise ValueError(f"{layer}: unknown LANDFIRE value column {column!r}; cannot confirm the scale")
    if abs(implied - scale) > 1e-12:
        raise ValueError(f"{layer}: configured scale {scale} but the CSV column {column!r} implies {implied}")


def codes_to_physical(codes: np.ndarray, valid_codes, scale: float, non_forested: float,
                      nodata: int = SRC_NODATA) -> tuple[np.ndarray, float]:
    """float32 physical values: ``code * scale``; code 0 -> ``non_forested``; nodata and codes not in
    ``valid_codes`` -> NaN. Returns ``(array, unknown fraction of data pixels)``."""
    codes = np.asarray(codes)
    has_data = codes != nodata
    known = has_data & np.isin(codes, np.asarray(sorted(valid_codes), dtype=codes.dtype))
    out = np.where(known, codes.astype("float64") * scale, np.nan)
    out = np.where(known & (codes == 0), non_forested, out).astype("float32")
    n = int(has_data.sum())
    return out, (float((has_data & ~known).sum()) / n if n else 0.0)


class LandfireCanopyConnector(BaseConnector):
    """Static LANDFIRE canopy item (four layers) per AOI."""

    def _opts(self) -> dict:
        o = self.dataset.options
        year = int(o.get("product_year", 2025))
        layers = {}
        for name, d in {**DEFAULT_LAYERS, **(o.get("layers") or {})}.items():
            d = {**DEFAULT_LAYERS.get(name, {}), **d}
            d = {**default_urls(d["code"], year), **d}
            nf = d.get("non_forested", 0.0)
            d["non_forested"] = float("nan") if str(nf).lower() == "nan" else float(nf)
            d["scale"] = float(d["scale"])
            layers[name] = d
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
            csv_rel = self.raw_path(ref, d["classes_csv"].rsplit("/", 1)[-1])
            self.lake.write_bytes(csv_rel, self._get(d["classes_csv"]).content)
            xml_rel = self.raw_path(ref, d["metadata_xml"].rsplit("/", 1)[-1])
            self.lake.write_bytes(xml_rel, self._get(d["metadata_xml"]).content)
            out += [tif, csv_rel, xml_rel]
        return out

    # ------------------------------------------------------------------ transform
    def _raw_for(self, raw: list[str], code: str) -> tuple[str, str, str | None]:
        tif = next(p for p in raw if p.endswith(".tif") and f"_{code}_CONUS__" in p.rsplit("/", 1)[-1])
        csv_rel = next(p for p in raw if p.rsplit("/", 1)[-1].endswith(f"_{code}.csv"))
        xml_rel = next((p for p in raw if p.rsplit("/", 1)[-1].endswith(f"_{code}_CONUS.xml")), None)
        return tif, csv_rel, xml_rel

    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        o = self._opts()
        grid = aoi.grid
        iid = self.item_id(aoi, ref, discriminator=str(o["product_version"]).lower().replace(" ", ""))
        assets: list[StagedAsset] = []
        qc: dict[str, float] = {}
        props_layers: dict[str, dict] = {}
        resampling_used = None
        cover = None
        for var in self.dataset.variables:
            d = o["layers"][var.name]
            tif, csv_rel, xml_rel = self._raw_for(raw, d["code"])
            column, classes = parse_value_table(self.lake.read_text(csv_rel, encoding="utf-8-sig"))
            check_scale(var.name, column, d["scale"])
            meta = parse_metadata(self.lake.read_text(xml_rel)) if xml_rel else {}
            with self.lake.local_path(tif, "r") as path, rasterio.open(path) as src:
                codes = src.read(1).astype("int16")
                src_nodata = int(src.nodata) if src.nodata is not None else SRC_NODATA
                crs, transform, src_res = src.crs.to_string(), src.transform, abs(src.transform.a)
            codes[codes == src_nodata] = SRC_NODATA
            if grid.resolution <= src_res * 1.0001:
                resampling_used = "nearest"
                on_grid = reproject_array(codes, crs, transform, grid, "nearest", src_nodata=SRC_NODATA)
                arr, unknown = codes_to_physical(on_grid, classes, d["scale"], d["non_forested"])
            else:
                resampling_used = "average"
                phys, unknown = codes_to_physical(codes, classes, d["scale"], d["non_forested"])
                arr = reproject_array(phys, crs, transform, grid, "average")
            arr, frac = mask_out_of_range(arr, var.valid_range)
            qc[var.name] = max(unknown, frac)
            if d["code"] == "CC":
                cover = arr
            if not np.isfinite(arr).any() and var.name != "canopy_base_height":
                raise ValueError(f"LANDFIRE {d['code']} clip has no data over AOI {aoi.id}")
            path = self.cog_path(aoi, iid, var.name)
            write_cog(self.lake, path, arr, grid, np.nan, var.dtype, description=var.name, unit=var.unit,
                      tags={"LANDFIRE_PRODUCT": d["code"], "SCALE": d["scale"], "VALUE_COLUMN": column})
            assets.append(self.cog_asset(var.name, path))
            cls_rel = f"{self.staged_dir(aoi, iid)}/{var.name}_classes.csv"
            self.lake.write_bytes(cls_rel, self.lake.read_bytes(csv_rel))
            assets.append(StagedAsset(key=f"{var.name}_classes", path=cls_rel, media_type=CSV_MEDIA_TYPE,
                                      roles=["metadata"]))
            finite = arr[np.isfinite(arr)]
            props_layers[var.name] = {
                "landfire_product": d["code"], "service": d["service"], "value_column": column, "scale": d["scale"],
                "title": meta.get("title"), "edition": meta.get("edition"), "pubdate": meta.get("pubdate"),
                "stats": ({"min": float(finite.min()), "max": float(finite.max()), "mean": float(finite.mean())}
                          if finite.size else {}),
            }
        props = {
            "fuellake:qc_masked_fraction": max(qc.values()) if qc else 0.0,
            "fuellake:qc_masked_fraction_by_variable": qc,
            "fuellake:landfire_version": o["product_version"],
            "fuellake:landfire_layers": props_layers,
            "fuellake:resampling": resampling_used,
            "fuellake:non_forested": "code 0: cover 0 %, height 0 m, bulk density 0 kg/m3, base height NaN",
            "fuellake:stats_scope": "AOI grid before the pipeline's region clip (see fuellake:region_clip)",
        }
        if cover is not None:
            n = int(np.isfinite(cover).sum())
            props["fuellake:forested_fraction"] = round(float((cover > 0).sum()) / n, 6) if n else 0.0
        start = datetime(o["product_year"], 1, 1, tzinfo=UTC)
        end = datetime(o["product_year"], 12, 31, 23, 59, 59, tzinfo=UTC)
        return [StagedItem(item_id=iid, datetime=ref.datetime, properties=props, assets=assets,
                           start_datetime=start, end_datetime=end)]
