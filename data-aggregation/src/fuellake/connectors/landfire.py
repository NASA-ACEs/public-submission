# fuel-lake — LANDFIRE FBFM40 (40 Scott & Burgan fuel models) clipped to the AOI grid, one item per release
# Licensed under Apache-2.0.
"""LANDFIRE FBFM40 via the public LANDFIRE ArcGIS ImageServers (no key, no account, no e-mail).

Releases (``options.releases`` of ``config/datasets/landfire-fbfm40.yaml``; verified live 2026-11-14), each
``https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF<yyyy>/LF<yyyy>_FBFM40_CONUS/ImageServer``: int16
(``S16``), nodata -9999, 30 m cells on EPSG:5070 (CONUS Albers) on one common lattice, values 91..204.
``exportImage`` returns a GeoTIFF clip. The services for LF 2016, 2022, 2023, 2024 and 2025 exist; the
``Landfire_LF2020`` folder is empty (LF 2019/2020 fuels are not served keyless).

=========  ==========================  ============================  ===============================
release    disturbances in the fuels   FGDC time period / pubdate    code table and FGDC metadata
=========  ==========================  ============================  ===============================
LF 2016    2010-2016 ("2020 capable")  2020 ground condition / 2020  members of the full-extent zip
LF 2022    2013 - FY 2022 (to 1 Oct)   2022 ground condition / 2023  zip member CSV; metadata URL
LF 2025    2016 - FY 2025 (to 30 Sep)  2025 ground condition / 2026  landfire.gov CSV and XML URLs
=========  ==========================  ============================  ===============================

One static unit per release and AOI (period ignored): key ``landfire:<service name>`` (the LF 2025 key is the
one of the single-release connector, so existing items stay idempotent), datetime = 1 January of the release
year (start/end datetime span that year). With the rule "latest release on or before the date", LF 2016 serves
2016-2021, LF 2022 serves 2022-2024 and LF 2025 serves 2025 on. Each release already contains the
disturbances of its own year (``fuellake:landfire_disturbances``), so a fire in that year can see its own
post-fire fuels; that property lets consumers apply a stricter rule.

- ``extract``: request the clip **on the native 30 m EPSG:5070 grid** (bbox snapped to the service's raster
  origin, padded by 2 cells, nearest neighbour), plus the official attribute CSV (code table) and the
  FGDC metadata XML; all three go to ``raw/``. A file given as ``{zip: <url>, member: <path>}`` is read out of
  the remote full-extent zip with HTTP range requests (central directory + the member only, a few hundred kB
  of a 3 GB archive); the server must answer ``206 Partial Content``.
- ``transform``: one client-side ``nearest`` reprojection to the AOI grid (categorical; 30 m -> 30 m),
  codes not in the code table are set to nodata and counted in ``fuellake:qc_masked_fraction``; writes the
  ``fuel_model`` COG (int16, nodata -9999) and attaches the code table CSV as a ``metadata`` asset.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime

import numpy as np
import rasterio
from rasterio.warp import transform_bounds

from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedAsset, StagedItem
from fuellake.rasters import choose_resampling, reproject_array, write_cog
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.landfire")

DEFAULT_SERVICE = ("https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2025/LF2025_FBFM40_CONUS/"
                   "ImageServer")
DEFAULT_CLASSES_CSV = "https://landfire.gov/sites/default/files/CSV/LF2025/LF2025_FBFM40.csv"
DEFAULT_METADATA_XML = "https://landfire.gov/sites/default/files/metadata/LF2025/CONUS/LF2025_FBFM40_CONUS.xml"
CSV_MEDIA_TYPE = "text/csv"
PAD_CELLS = 2
ZIP_BUFFER = 256 * 1024             # bytes per range request while reading a remote zip
ZIP_MEMBER_MAX = 50_000_000         # refuse to pull bigger members (the GeoTIFFs) through range requests


def parse_code_table(text: str) -> dict[int, str]:
    """``{value: label}`` from the LANDFIRE attribute CSV (columns ``VALUE,FBFM40,...``); drops nodata."""
    rows = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    out: dict[int, str] = {}
    for row in rows:
        value = int(row["VALUE"])
        label = (row.get("FBFM40") or "").strip()
        if value < 0 or label.lower().startswith("fill"):
            continue
        out[value] = label
    if not out:
        raise ValueError("code table CSV has no classes")
    return out


def parse_metadata(xml_text: str) -> dict:
    """A few facts from the FGDC metadata XML: title, edition, publication date, DOI, use constraints and the
    time period of content (``ground_condition``, e.g. ``"2022 ground condition"``).

    The DOI is searched in the dataset's own (first) ``<citation>`` only: later citations are sources such as
    RMRS-GTR-153, whose DOI is not the dataset's (LF 2016 cites no dataset DOI)."""
    def first(tag: str, text: str = xml_text) -> str | None:
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, flags=re.S)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else None

    pub = first("pubdate")
    cit = re.search(r"<citation>(.*?)</citation>", xml_text, flags=re.S)
    doi = re.search(r"https?://doi\.org/[^<\s]+", cit.group(1) if cit else xml_text)
    tp = re.search(r"<timeperd>(.*?)</timeperd>", xml_text, flags=re.S)
    ground = " ".join(x for x in (first("caldate", tp.group(1)), first("current", tp.group(1))) if x) if tp else ""
    return {
        "title": first("title"),
        "edition": first("edition"),
        "pubdate": f"{pub[:4]}-{pub[4:6]}-{pub[6:8]}" if pub and re.fullmatch(r"\d{8}", pub) else pub,
        "doi": doi.group(0) if doi else None,
        "use_constraints": first("useconst"),
        "ground_condition": ground or None,
    }


def native_clip_request(aoi_bbox_wgs84, service: dict, pad_cells: int = PAD_CELLS) -> dict:
    """``exportImage`` parameters for the AOI on the service's own 30 m grid (no server resampling)."""
    wkid = service["spatialReference"].get("latestWkid") or service["spatialReference"]["wkid"]
    rx, ry = float(service["pixelSizeX"]), float(service["pixelSizeY"])
    x0, y0 = float(service["extent"]["xmin"]), float(service["extent"]["ymax"])
    w, s, e, n = transform_bounds("EPSG:4326", f"EPSG:{wkid}", *aoi_bbox_wgs84, densify_pts=21)
    xmin = x0 + (math.floor((w - x0) / rx) - pad_cells) * rx
    xmax = x0 + (math.ceil((e - x0) / rx) + pad_cells) * rx
    ymax = y0 - (math.floor((y0 - n) / ry) - pad_cells) * ry
    ymin = y0 - (math.ceil((y0 - s) / ry) + pad_cells) * ry
    width, height = int(round((xmax - xmin) / rx)), int(round((ymax - ymin) / ry))
    return {
        "bbox": f"{xmin:.3f},{ymin:.3f},{xmax:.3f},{ymax:.3f}", "bboxSR": wkid, "imageSR": wkid,
        "size": f"{width},{height}", "format": "tiff", "pixelType": "S16",
        "noData": int(service.get("noDataValue", -9999)), "noDataInterpretation": "esriNoDataMatchAny",
        "interpolation": "RSP_NearestNeighbor", "f": "image",
    }


# ---------------------------------------------------------------------------- remote zip members
class _RangeFile(io.RawIOBase):
    """Read-only, seekable file over ``fetch(start, end_inclusive) -> bytes`` (HTTP range requests)."""

    def __init__(self, fetch: Callable[[int, int], bytes], size: int):
        self._fetch, self._size, self._pos = fetch, int(size), 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def readinto(self, b) -> int:
        n = min(len(b), self._size - self._pos)
        if n <= 0:
            return 0
        data = self._fetch(self._pos, self._pos + n - 1)
        b[:len(data)] = data
        self._pos += len(data)
        return len(data)


def read_zip_member(fetch: Callable[[int, int], bytes], size: int, member: str,
                    max_bytes: int = ZIP_MEMBER_MAX) -> bytes:
    """One member of a remote zip of ``size`` bytes, reading only the central directory and that member."""
    with zipfile.ZipFile(io.BufferedReader(_RangeFile(fetch, size), buffer_size=ZIP_BUFFER)) as z:
        try:
            info = z.getinfo(member)
        except KeyError:
            raise KeyError(f"zip has no member {member!r}") from None
        if info.file_size > max_bytes:
            raise ValueError(f"zip member {member!r} has {info.file_size} bytes (> {max_bytes}); not fetched")
        return z.read(info)


def file_name(spec: str | dict) -> str:
    """Local file name of a source file given as a URL or as ``{zip: <url>, member: <path>}``."""
    return str(spec["member"] if isinstance(spec, dict) else spec).rstrip("/").rsplit("/", 1)[-1]


def spec_url(spec: str | dict) -> str:
    """Human-readable source of a file spec (``<zip url>!/<member>`` for zip members)."""
    return f"{spec['zip']}!/{spec['member']}" if isinstance(spec, dict) else str(spec)


# ---------------------------------------------------------------------------- releases
def release_list(options: dict) -> list[dict]:
    """The configured releases, oldest first: ``options.releases`` or, without it, one release from the
    single-release keys (``image_service``, ``classes_csv``, ``metadata_xml``, ``product_year``,
    ``product_version``; defaults LF 2025)."""
    raw = options.get("releases")
    if not raw:
        year = int(options.get("product_year", 2025))
        raw = [{"version": options.get("product_version", f"LF {year}"), "year": year,
                "image_service": options.get("image_service", DEFAULT_SERVICE),
                "classes_csv": options.get("classes_csv", DEFAULT_CLASSES_CSV),
                "metadata_xml": options.get("metadata_xml", DEFAULT_METADATA_XML)}]
    out = []
    for r in raw:
        missing = [k for k in ("version", "year", "image_service", "classes_csv") if not r.get(k)]
        if missing:
            raise ValueError(f"LANDFIRE release {r.get('version')!r} lacks {missing}")
        for k in ("classes_csv", "metadata_xml"):
            spec = r.get(k)
            if isinstance(spec, dict) and not (spec.get("zip") and spec.get("member")):
                raise ValueError(f"LANDFIRE release {r['version']}: {k} needs both 'zip' and 'member'")
        out.append({**r, "year": int(r["year"]), "image_service": str(r["image_service"]).rstrip("/")})
    years = [r["year"] for r in out]
    if len(set(years)) != len(years):
        raise ValueError(f"two LANDFIRE releases share a year (item ids would collide): {years}")
    return sorted(out, key=lambda r: r["year"])


def release_tag(release: dict) -> str:
    """Item id discriminator: ``LF 2016`` -> ``lf2016``."""
    return re.sub(r"[^a-z0-9]", "", str(release["version"]).lower())


class LandfireFbfm40Connector(BaseConnector):
    """Static LANDFIRE FBFM40 items per AOI, one per configured release."""

    def releases(self) -> list[dict]:
        return release_list(self.dataset.options)

    def _get(self, url: str, *, headers: dict | None = None, stream: bool = False, **params):
        s = self.http_session()
        r = s.get(url, params=params or None, headers=headers, stream=stream,
                  timeout=self.settings.http_timeout * 3)
        r.raise_for_status()
        return r

    def service_info(self, release: dict | None = None) -> dict:
        release = release or self.releases()[-1]
        return self._get(release["image_service"], f="json").json()

    # ------------------------------------------------------------------ remote files
    def _range(self, url: str, start: int, end: int) -> tuple[bytes, int]:
        """Bytes ``start..end`` (inclusive) of ``url`` and the total size; refuses a non-206 answer, so a server
        ignoring ``Range`` cannot start a multi-GB download."""
        r = self._get(url, headers={"Range": f"bytes={start}-{end}"}, stream=True)
        try:
            if r.status_code != 206:
                raise RuntimeError(f"{url}: expected 206 Partial Content for a range request, got {r.status_code}")
            total = int(str(r.headers.get("content-range", "")).rsplit("/", 1)[-1])
            return r.content, total
        finally:
            r.close()

    def fetch_file(self, spec: str | dict) -> bytes:
        """Bytes of a source file given as a URL or as ``{zip: <url>, member: <path>}``."""
        if not isinstance(spec, dict):
            return self._get(str(spec)).content
        url = spec["zip"]
        _, size = self._range(url, 0, 0)
        return read_zip_member(lambda a, b: self._range(url, a, b)[0], size, spec["member"])

    # ------------------------------------------------------------------ discover
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        refs = []
        for rel in self.releases():
            info = self.service_info(rel)
            if "error" in info:
                raise RuntimeError(f"LANDFIRE {rel['version']} service error: {info['error']}")
            if "Image" not in str(info.get("capabilities", "")):
                raise RuntimeError(f"LANDFIRE {rel['version']} service has no Image capability: "
                                   f"{info.get('capabilities')}")
            name = info.get("name") or rel["image_service"].split("/services/", 1)[-1].removesuffix("/ImageServer")
            keep = {k: info.get(k) for k in ("name", "pixelType", "noDataValue", "pixelSizeX", "pixelSizeY",
                                              "extent", "spatialReference", "minValues", "maxValues")}
            keep["name"] = name
            dt = datetime(rel["year"], 1, 1, tzinfo=UTC)
            refs.append(self.ref(f"landfire:{name}", dt, urls=[rel["image_service"]], service=keep, release=rel))
        return refs

    # ------------------------------------------------------------------ extract
    def _release_of(self, ref: SourceRef) -> dict:
        rel = ref.meta.get("release")
        if rel:
            return dict(rel)
        return self.releases()[-1]           # refs built before releases existed: the single LF 2025 release

    def _raw_stem(self, ref: SourceRef, aoi: AOI) -> str:
        name = str(ref.meta["service"]["name"]).split("/")[-1]
        return f"{name}__{aoi.id}"

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        rel = self._release_of(ref)
        stem = self._raw_stem(ref, aoi)
        params = native_clip_request(aoi.grid.bounds_wgs84(), ref.meta["service"])
        r = self._get(f"{rel['image_service']}/exportImage", **params)
        ctype = r.headers.get("content-type", "")
        if "tiff" not in ctype.lower():
            raise RuntimeError(f"{rel['version']} exportImage returned {ctype!r} instead of a GeoTIFF: {r.text[:300]}")
        tif = self.raw_path(ref, f"{stem}.tif")
        self.lake.write_bytes(tif, r.content)
        out = [tif]
        for key in ("classes_csv", "metadata_xml"):
            spec = rel.get(key)
            if not spec:
                continue
            rel_path = self.raw_path(ref, file_name(spec))
            self.lake.write_bytes(rel_path, self.fetch_file(spec))
            out.append(rel_path)
        return out

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        rel = self._release_of(ref)
        tif = next(p for p in raw if p.endswith(".tif"))
        csv_rel = next(p for p in raw if p.endswith(".csv"))
        xml_rel = next((p for p in raw if p.endswith(".xml")), None)
        var = self.variable("fuel_model")
        nodata = int(var.nodata)
        classes = parse_code_table(self.lake.read_text(csv_rel, encoding="utf-8-sig"))
        meta = parse_metadata(self.lake.read_text(xml_rel, encoding="utf-8-sig")) if xml_rel else {}

        grid = aoi.grid
        with self.lake.local_path(tif, "r") as path, rasterio.open(path) as src:
            arr = src.read(1)
            src_nodata = src.nodata if src.nodata is not None else nodata
            crs, transform, src_res = src.crs.to_string(), src.transform, abs(src.transform.a)
        arr = arr.astype(var.dtype)
        arr[arr == int(src_nodata)] = nodata
        resampling = choose_resampling(True, src_res, grid.resolution)
        out = reproject_array(arr, crs, transform, grid, resampling, src_nodata=nodata)

        has_data = out != nodata
        unknown = has_data & ~np.isin(out, np.array(sorted(classes), dtype=out.dtype))
        out[unknown] = nodata
        n = int(has_data.sum())
        qc = float(unknown.sum()) / n if n else 0.0
        if n == 0:
            raise ValueError(f"LANDFIRE {rel['version']} clip has no data over AOI {aoi.id}")

        iid = self.item_id(aoi, ref, discriminator=release_tag(rel))
        path = self.cog_path(aoi, iid, "fuel_model")
        write_cog(self.lake, path, out, grid, nodata, var.dtype,
                  description=f"fuel_model (FBFM40 code, {rel['version']})",
                  tags={"FBFM40_CODES": ";".join(f"{k}={v}" for k, v in sorted(classes.items())),
                        "LANDFIRE_RELEASE": rel["version"]})
        classes_rel = f"{self.staged_dir(aoi, iid)}/fuel_model_classes.csv"
        self.lake.write_bytes(classes_rel, self.lake.read_bytes(csv_rel))

        values, counts = np.unique(out[out != nodata], return_counts=True)
        total = int(counts.sum())
        fractions = {classes.get(int(v), str(int(v))): round(int(c) / total, 6) for v, c in zip(values, counts,
                                                                                                strict=True)}
        props = {
            "fuellake:qc_masked_fraction": qc,
            "fuellake:landfire_version": rel["version"],
            "fuellake:landfire_release_year": rel["year"],
            "fuellake:landfire_program_version": rel.get("landfire_version"),
            "fuellake:landfire_edition": meta.get("edition"),
            "fuellake:landfire_title": meta.get("title"),
            "fuellake:landfire_pubdate": meta.get("pubdate"),
            "fuellake:landfire_ground_condition": meta.get("ground_condition"),
            "fuellake:landfire_doi": meta.get("doi"),
            "fuellake:landfire_use_constraints": meta.get("use_constraints"),
            "fuellake:landfire_disturbances": rel.get("disturbances"),
            "fuellake:landfire_service": rel["image_service"],
            "fuellake:landfire_files": {"classes_csv": spec_url(rel["classes_csv"]),
                                        "metadata_xml": spec_url(rel["metadata_xml"]) if rel.get("metadata_xml")
                                        else None},
            "fuellake:resampling": resampling,
            "fuellake:nodata_fraction": round(1.0 - total / out.size, 6),
            "fuellake:class_fractions": fractions,
            "fuellake:classes": {str(k): v for k, v in sorted(classes.items())},
        }
        start = datetime(rel["year"], 1, 1, tzinfo=UTC)
        end = datetime(rel["year"], 12, 31, 23, 59, 59, tzinfo=UTC)
        assets = [self.cog_asset("fuel_model", path),
                  StagedAsset(key="classes", path=classes_rel, media_type=CSV_MEDIA_TYPE, roles=["metadata"])]
        return [StagedItem(item_id=iid, datetime=ref.datetime, properties=props, assets=assets,
                           start_datetime=start, end_datetime=end)]
