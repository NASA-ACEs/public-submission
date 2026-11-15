# fuel-lake — LANDFIRE Existing Vegetation Type (EVT) as the share of 16 vegetation classes per AOI grid cell
# Licensed under Apache-2.0.
"""LANDFIRE EVT via the keyless LANDFIRE ArcGIS ImageServers, one item per release, one asset per class.

Services (``options.releases`` of ``config/datasets/landfire-evt.yaml``; verified live 2026-11-15), each
``https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF<yyyy>/LF<yyyy>_EVT_CONUS/ImageServer``: int16 (``S16``),
nodata -9999, 30 m on EPSG:5070, the lattice of FBFM40, codes 7008..9994. Everything about releases, the native
clip request and remote files is inherited from :class:`fuellake.connectors.landfire.LandfireFbfm40Connector`.

**Classes.** EVT has about a thousand codes. Each code is assigned to one of 16 classes by rule
(:func:`classify`) from three columns of the official attribute table: ``EVT_PHYS`` (physiognomy), ``EVT_LF``
(lifeform) and ``EVT_GP_N`` (group name). No labels are used. One table serves every release, so a pixel whose
code does not change keeps its class: the tables of ``options.attribute_tables`` are read in order and the first
one that lists a code decides (LF 2025, completed by LF 2016 Remap for the 20 codes LF 2025 dropped).

**Assets.** ``evt_share_<class>``, float32, NaN without data:

- AOI grid as fine as the source (30 m): the code is reprojected with nearest neighbour and each asset is 1
  where the pixel is of the class and 0 where it is of another. Average over a window to get a share.
- coarser AOI grid: each asset is the share of the class among the valid 30 m pixels of the cell (GDAL
  ``average`` of the 0/1 layers), rounded to ``options.share_steps`` steps (200: steps of 0.005).

The shares of a cell sum to 1 (within rounding). A code that no table lists is nodata and counted in
``fuellake:qc_masked_fraction``.

**Large AOIs.** The clip is requested per block of ``options.block_cells`` AOI cells (512), ``max_workers``
requests at a time, and blocks that lie outside the lake region are not requested. A 30 m AOI of the size of
``smm`` is one block. Raw files already in the lake are not requested again, so an interrupted statewide run
resumes. The block size is part of the result: GDAL approximates the coordinate transform along the rows of the
block it warps, so another ``block_cells`` moves a share by one rounding step in a few cells along class edges
(the offline test bounds it). Keep 512 to reproduce staged values.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import numpy as np
import rasterio
from affine import Affine

from fuellake.connectors.base import SourceRef, StagedAsset, StagedItem
from fuellake.connectors.landfire import (
    CSV_MEDIA_TYPE,
    LandfireFbfm40Connector,
    file_name,
    native_clip_request,
    parse_metadata,
    release_list,
    release_tag,
    spec_url,
)
from fuellake.grid import Grid
from fuellake.rasters import reproject_array, write_cog
from fuellake.registry import AOI, lake_region

log = logging.getLogger("fuellake.connectors.landfire_evt")

CLASSES = ("chaparral", "coastal_scrub", "sagebrush", "desert_scrub", "other_shrub", "oak_woodland",
           "other_hardwood", "conifer_oak", "dry_conifer", "mesic_conifer", "juniper_pinyon", "grass", "riparian",
           "sparse", "agriculture", "developed")
PREFIX = "evt_share_"
SRC_NODATA = -9999
RULE = ("Each EVT code is assigned to one of 16 classes by rule from EVT_PHYS, EVT_LF and EVT_GP_N of the LANDFIRE "
        "attribute table (fuellake.connectors.landfire_evt.classify). One table for all releases.")


def classify(group: str, physiognomy: str, lifeform: str) -> str:
    """The class of an EVT code from its group name, physiognomy and lifeform. The order of the tests matters."""
    g, p, lf = group.strip().lower(), physiognomy.strip().lower(), lifeform.strip().lower()
    if p.startswith("developed") or lf == "developed" or "quarries" in p:
        return "developed"
    if p == "agricultural" or lf == "agriculture":
        return "agriculture"
    if p in ("sparsely vegetated", "open water", "snow-ice") or lf in ("sparse", "water", "barren", "snow-ice"):
        return "sparse"
    if p == "riparian":
        return "riparian"
    if p in ("grassland", "exotic herbaceous") or lf == "herb":
        return "grass"
    if p in ("shrubland", "exotic tree-shrub") or lf == "shrub":
        if "chaparral" in g:
            return "chaparral"
        if "coastal scrub" in g:
            return "coastal_scrub"
        if "sagebrush" in g:
            return "sagebrush"
        if any(w in g for w in ("desert", "creosote", "salt", "greasewood", "blackbrush")):
            return "desert_scrub"
        return "other_shrub"
    if "juniper" in g or "pinyon" in g:
        return "juniper_pinyon"
    if "conifer-oak" in g:
        return "conifer_oak"
    if p == "hardwood":
        return "oak_woodland" if "oak" in g else "other_hardwood"
    if p == "conifer-hardwood":
        return "other_hardwood"
    if any(w in g for w in ("ponderosa", "jeffrey", "lodgepole", "pine")):
        return "dry_conifer"
    return "mesic_conifer"


def parse_attribute_table(text: str) -> dict[int, dict[str, str]]:
    """``{code: {name, physiognomy, group, lifeform}}`` from an official EVT attribute CSV; drops nodata."""
    out: dict[int, dict[str, str]] = {}
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        code = int(row["VALUE"])
        if code >= 0:
            out[code] = {"name": row["EVT_NAME"].strip(), "physiognomy": row["EVT_PHYS"].strip(),
                         "group": row["EVT_GP_N"].strip(), "lifeform": row["EVT_LF"].strip()}
    if not out:
        raise ValueError("EVT attribute CSV has no codes")
    return out


def class_table(texts: list[str]) -> dict[int, dict[str, str]]:
    """The attribute rows of every code in ``texts`` (first table that lists a code wins), each with its
    ``class``."""
    rows: dict[int, dict[str, str]] = {}
    for text in texts:
        for code, row in parse_attribute_table(text).items():
            rows.setdefault(code, row)
    return {code: {**r, "class": classify(r["group"], r["physiognomy"], r["lifeform"])}
            for code, r in sorted(rows.items())}


def class_lut(table: dict[int, dict[str, str]], classes=CLASSES) -> np.ndarray:
    """int8 array indexed by EVT code: the position of its class in ``classes``, -1 for an unlisted code."""
    lut = np.full(32768, -1, dtype="int8")
    for code, row in table.items():
        if 0 <= code < lut.size:
            lut[code] = classes.index(row["class"])
    return lut


def table_csv(table: dict[int, dict[str, str]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["evt_code", "evt_name", "evt_phys", "evt_gp_n", "evt_lf", "class"])
    for code, r in table.items():
        w.writerow([code, r["name"], r["physiognomy"], r["group"], r["lifeform"], r["class"]])
    return buf.getvalue()


def class_shares(codes: np.ndarray, src_crs, src_transform, grid: Grid, lut: np.ndarray, n_classes: int,
                 share_steps: int | None = None, nodata: int = SRC_NODATA) -> tuple[np.ndarray, dict]:
    """``(shares, facts)``: float32 ``(n_classes, height, width)`` on ``grid``; ``facts`` has ``resampling``, the
    ``unlisted`` codes and the pixel counts ``unlisted_pixels`` and ``data_pixels`` (of the source, or of the
    grid when it is as fine as the source).

    A grid as fine as the source gets 0/1 layers of the nearest pixel's class; a coarser grid the share of each
    class among the valid source pixels of the cell (optionally rounded to ``share_steps`` steps).
    """
    codes = np.asarray(codes)
    fine = grid.resolution <= abs(src_transform.a) * 1.0001
    if fine:
        codes = reproject_array(codes.astype("int16"), src_crs, src_transform, grid, "nearest", src_nodata=nodata)
    cls = np.where(codes >= 0, lut[np.clip(codes, 0, lut.size - 1)], -1)
    valid = cls >= 0
    lost = (codes >= 0) & ~valid
    facts = {"resampling": "nearest" if fine else "average", "unlisted": np.unique(codes[lost]),
             "unlisted_pixels": int(lost.sum()), "data_pixels": int((codes >= 0).sum())}
    out = np.empty((n_classes, *grid.shape), dtype="float32")
    for k in range(n_classes):
        layer = np.where(valid, cls == k, np.nan).astype("float32")
        if fine:
            out[k] = layer
            continue
        share = reproject_array(layer, src_crs, src_transform, grid, "average")
        out[k] = np.round(share * share_steps) / share_steps if share_steps else share
    return out, facts


def blocks(grid: Grid, block_cells: int) -> list[tuple[int, int, Grid]]:
    """``(row, col, sub-grid)`` of the blocks of ``grid``; one block for a grid as fine as LANDFIRE."""
    if grid.resolution <= 30.0 * 1.0001:
        return [(0, 0, grid)]
    out = []
    for r in range(0, grid.height, block_cells):
        for c in range(0, grid.width, block_cells):
            h, w = min(block_cells, grid.height - r), min(block_cells, grid.width - c)
            out.append((r, c, Grid(crs=grid.crs, transform=grid.transform @ Affine.translation(c, r), width=w,
                                   height=h, resolution=grid.resolution)))
    return out


class LandfireEvtConnector(LandfireFbfm40Connector):
    """Static LANDFIRE EVT class-share items per AOI, one per configured release."""

    def classes(self) -> tuple[str, ...]:
        names = tuple(v.name.removeprefix(PREFIX) for v in self.dataset.variables)
        unknown = sorted(set(names) - set(CLASSES))
        if unknown:
            raise ValueError(f"landfire-evt variables name classes that classify() never returns: {unknown}")
        return names

    def tables(self) -> list:
        specs = list(self.dataset.options.get("attribute_tables") or [])
        if not specs:
            raise ValueError("landfire-evt needs options.attribute_tables")
        if len({file_name(s) for s in specs}) != len(specs):
            raise ValueError("landfire-evt attribute tables must have distinct file names")
        return specs

    def releases(self) -> list[dict]:
        o = self.dataset.options
        if not o.get("releases"):
            raise ValueError("landfire-evt needs options.releases")
        return release_list({**o, "releases": [{"classes_csv": self.tables()[0], **r} for r in o["releases"]]})

    @property
    def max_workers(self) -> int:
        return max(1, int(self.dataset.options.get("max_workers", 4)))

    def _blocks(self, aoi: AOI) -> list[tuple[int, int, Grid]]:
        region = lake_region()
        out = blocks(aoi.grid, int(self.dataset.options.get("block_cells", 512)))
        return [b for b in out if region is None or region.pixel_mask(b[2]).any()]

    # ------------------------------------------------------------------ extract
    def _tile(self, ref: SourceRef, aoi: AOI, row: int, col: int) -> str:
        return self.raw_path(ref, f"{self._raw_stem(ref, aoi)}__r{row:04d}_c{col:04d}.tif")

    def _fetch_tile(self, ref: SourceRef, rel: dict, path: str, grid: Grid) -> None:
        # LZ77 is lossless: a quarter of the bytes on smm; a statewide release is about 210 MB
        params = {**native_clip_request(grid.bounds_wgs84(), ref.meta["service"]), "compression": "LZ77"}
        r = self._get(f"{rel['image_service']}/exportImage", **params)
        ctype = r.headers.get("content-type", "")
        if "tiff" not in ctype.lower():
            raise RuntimeError(f"{rel['version']} exportImage returned {ctype!r} instead of a GeoTIFF: {r.text[:300]}")
        self.lake.write_bytes(path, r.content)

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        rel = self._release_of(ref)
        todo = [(self._tile(ref, aoi, r, c), g) for r, c, g in self._blocks(aoi)]
        if not todo:
            raise ValueError(f"AOI {aoi.id} has no block inside the lake region")
        missing = [(p, g) for p, g in todo if not self.lake.exists(p)]
        with ThreadPoolExecutor(max_workers=min(self.max_workers, max(len(missing), 1))) as pool:
            list(pool.map(lambda t: self._fetch_tile(ref, rel, *t), missing))
        out = [p for p, _ in todo]
        for spec in [*self.tables(), rel.get("metadata_xml")]:
            if spec:
                path = self.raw_path(ref, file_name(spec))
                self.lake.write_bytes(path, self.fetch_file(spec))
                out.append(path)
        return out

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        rel = self._release_of(ref)
        classes, grid = self.classes(), aoi.grid
        table = class_table([self.lake.read_text(p, encoding="utf-8-sig") for s in self.tables()
                             for p in raw if p.rsplit("/", 1)[-1] == file_name(s)])
        xml_rel = next((p for p in raw if p.endswith(".xml")), None)
        meta = parse_metadata(self.lake.read_text(xml_rel, encoding="utf-8-sig")) if xml_rel else {}
        lut = class_lut(table, classes)
        steps = self.dataset.options.get("share_steps")
        by_tile = {m.groups(): p for p in raw if (m := re.search(r"__r(\d{4})_c(\d{4})\.tif$", p))}

        shares = np.full((len(classes), *grid.shape), np.nan, dtype="float32")
        unlisted: set[int] = set()
        resampling, lost, data = None, 0, 0
        for row, col, sub in blocks(grid, int(self.dataset.options.get("block_cells", 512))):
            tif = by_tile.get((f"{row:04d}", f"{col:04d}"))
            if tif is None:                              # a block outside the lake region
                continue
            with self.lake.local_path(tif, "r") as path, rasterio.open(path) as src:
                codes, crs, transform = src.read(1), src.crs.to_string(), src.transform
                src_nodata = int(src.nodata) if src.nodata is not None else SRC_NODATA
            codes[codes == src_nodata] = SRC_NODATA
            part, facts = class_shares(codes, crs, transform, sub, lut, len(classes), int(steps) if steps else None)
            shares[:, row:row + sub.height, col:col + sub.width] = part
            unlisted |= {int(v) for v in facts["unlisted"]}
            resampling, lost, data = facts["resampling"], lost + facts["unlisted_pixels"], data + facts["data_pixels"]
        valid = np.isfinite(shares[0])
        if not valid.any():
            raise ValueError(f"LANDFIRE EVT {rel['version']} clip has no data over AOI {aoi.id}")

        iid = self.item_id(aoi, ref, discriminator=release_tag(rel))
        assets = []
        for k, name in enumerate(classes):
            var = self.variable(f"{PREFIX}{name}")
            path = self.cog_path(aoi, iid, var.name)
            write_cog(self.lake, path, shares[k], grid, np.nan, var.dtype, description=var.name, unit=var.unit,
                      tags={"LANDFIRE_PRODUCT": "EVT", "LANDFIRE_RELEASE": rel["version"]})
            assets.append(self.cog_asset(var.name, path))
        classes_rel = f"{self.staged_dir(aoi, iid)}/classes.csv"
        self.lake.write_text(classes_rel, table_csv(table))
        assets.append(StagedAsset(key="classes", path=classes_rel, media_type=CSV_MEDIA_TYPE, roles=["metadata"]))

        props = {
            "fuellake:qc_masked_fraction": round(lost / data, 6) if data else 0.0,     # pixels of unlisted codes
            "fuellake:landfire_version": rel["version"],
            "fuellake:landfire_release_year": rel["year"],
            "fuellake:valid_from": rel.get("valid_from"),
            "fuellake:landfire_edition": meta.get("edition"),
            "fuellake:landfire_title": meta.get("title"),
            "fuellake:landfire_pubdate": meta.get("pubdate"),
            "fuellake:landfire_doi": meta.get("doi"),
            "fuellake:landfire_use_constraints": meta.get("use_constraints"),
            "fuellake:landfire_service": rel["image_service"],
            "fuellake:landfire_files": {"attribute_tables": [spec_url(s) for s in self.tables()],
                                        "metadata_xml": spec_url(rel["metadata_xml"]) if rel.get("metadata_xml")
                                        else None},
            "fuellake:resampling": resampling,
            "fuellake:share_steps": int(steps) if steps and resampling == "average" else None,
            "fuellake:classification": RULE,
            "fuellake:classes": list(classes),
            "fuellake:class_fractions": {n: round(float(np.nanmean(shares[k])), 6) for k, n in enumerate(classes)},
            "fuellake:valid_fraction": round(float(valid.mean()), 6),
            "fuellake:unmapped_codes": sorted(unlisted),
            "fuellake:stats_scope": "AOI grid before the pipeline's region clip (see fuellake:region_clip)",
        }
        start = datetime(rel["year"], 1, 1, tzinfo=UTC)
        end = datetime(rel["year"], 12, 31, 23, 59, 59, tzinfo=UTC)
        return [StagedItem(item_id=iid, datetime=ref.datetime, properties=props, assets=assets,
                           start_datetime=start, end_datetime=end)]
