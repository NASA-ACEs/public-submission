# fuel-lake — biomass drop-zone connector (GeoTIFF + JSON sidecar from the team's biomass subsystem)
# Licensed under Apache-2.0.
"""Ingest flammable-biomass rasters dropped by the team's biomass subsystem into ``inbox/biomass/``.

Drop contract (schema: ``config/schemas/biomass_drop.schema.json``, version ``1.0``)
------------------------------------------------------------------------------------
A drop is one JSON sidecar ``inbox/biomass/[<subdir>/]<name>.json`` plus the GeoTIFF(s) it names in
``file`` (and optionally ``uncertainty.file``), in the **same directory** as the sidecar. Example::

    {"schema_version": "1.0", "aoi": "smm", "variable": "biomass", "file": "smm_live_20260815.tif",
     "unit": "kg/m2", "mass_basis": "dry", "date": "2026-08-15", "component": "live",
     "fuel_size_classes": ["live_herbaceous", "live_woody"], "crs": "EPSG:32611",
     "producer": {"name": "biomass-subsystem", "version": "0.3.1"},
     "uncertainty": {"file": "smm_live_20260815_sd.tif", "kind": "std"}}

The runtime validator is :class:`BiomassSidecar` (pydantic; ``jsonschema`` is not a runtime dependency).
The JSON Schema file is the published contract; ``tests/test_biomass_drop.py`` checks that both accept
and reject the same sidecars.

Behaviour
---------
- ``discover``: every sidecar for this AOI whose ``date`` is inside the period is one unit. The key is
  ``biomass:<sidecar path>:<sha1 of sidecar + data files>``, so a changed drop is re-ingested. A sidecar
  that cannot be parsed or validated is still returned (with the error), and its unit **fails** in
  ``transform`` with a clear message; other drops are not affected. ``discover`` never raises for a bad drop.
- ``extract``: copies the drop unchanged to ``raw/biomass/<yyyy>/<mm>/<sidecar stem>__<hash8>/``
  (the inbox is never modified).
- ``transform``: checks the file CRS against the sidecar ``crs``, converts the unit to **kg/m2 dry mass**
  (``kg/m2`` x1, ``g/m2`` x0.001, ``Mg/ha`` and ``t/ha`` x0.1), reprojects onto the AOI grid (contract
  resampling rule: ``average`` when the drop's pixels are finer than the AOI grid, ``bilinear`` when they
  are coarser), masks values outside the variable's valid range, and writes ``biomass`` (and
  ``biomass_uncertainty``).
- Item id ``biomass__<aoi>__<yyyymmdd>__<component>-<producer version>``; two sidecars mapping to the
  same item id are both failed as duplicates (remove one), so drops never silently overwrite each other.
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
from datetime import UTC, date, datetime, time
from typing import Literal

import numpy as np
import rasterio
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from rasterio.crs import CRS

from fuellake.connectors._stac_source import metres_per_pixel
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.rasters import choose_resampling, mask_out_of_range, reproject_array, write_cog
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.biomass_drop")

SCHEMA_VERSION = "1.0"
INBOX = "inbox/biomass"
UNIT_TO_KG_M2 = {"kg/m2": 1.0, "g/m2": 0.001, "Mg/ha": 0.1, "t/ha": 0.1}
DEAD_CLASSES = ("1h", "10h", "100h", "1000h", "litter", "duff")
LIVE_CLASSES = ("live_herbaceous", "live_woody")
SIZE_CLASSES = DEAD_CLASSES + LIVE_CLASSES + ("all",)
FILE_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(tif|tiff|TIF|TIFF)$"

Unit = Literal["kg/m2", "g/m2", "Mg/ha", "t/ha"]
SizeClass = Literal["1h", "10h", "100h", "1000h", "litter", "duff", "live_herbaceous", "live_woody", "all"]


# ---------------------------------------------------------------------------- sidecar model
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Producer(_Strict):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._+-]+$")
    git_commit: str | None = None


class Uncertainty(_Strict):
    file: str = Field(pattern=FILE_RE)
    band: int = Field(default=1, ge=1)
    kind: Literal["std", "ci95_halfwidth"]
    unit: Unit | None = None          # default: the main unit


class DropLicense(_Strict):
    id: str = Field(min_length=1)
    redistributable: bool
    notes: str | None = None


class BiomassSidecar(_Strict):
    """Runtime validator of the drop sidecar (mirrors ``config/schemas/biomass_drop.schema.json``)."""

    schema_version: Literal["1.0"]
    aoi: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    variable: Literal["biomass"]
    file: str = Field(pattern=FILE_RE)
    band: int = Field(default=1, ge=1)
    unit: Unit
    mass_basis: Literal["dry"]
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    component: Literal["live", "dead", "both"]
    fuel_size_classes: list[SizeClass] = Field(min_length=1)
    crs: str = Field(pattern=r"^EPSG:[0-9]+$")
    nodata: float | int | None = None
    producer: Producer
    uncertainty: Uncertainty | None = None
    inputs: list[str] | None = None
    license: DropLicense | None = None
    notes: str | None = None

    @field_validator("date")
    @classmethod
    def _date(cls, v: str) -> str:
        date.fromisoformat(v)
        return v

    @field_validator("fuel_size_classes")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("fuel_size_classes must not repeat")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> BiomassSidecar:
        classes = set(self.fuel_size_classes)
        if self.component == "live" and not classes <= set(LIVE_CLASSES) | {"all"}:
            raise ValueError(f"component 'live' allows only {LIVE_CLASSES} or 'all', got {sorted(classes)}")
        if self.component == "dead" and not classes <= set(DEAD_CLASSES) | {"all"}:
            raise ValueError(f"component 'dead' allows only {DEAD_CLASSES} or 'all', got {sorted(classes)}")
        if "all" in classes and len(classes) > 1:
            raise ValueError("'all' cannot be combined with other fuel size classes")
        if self.uncertainty is not None and self.uncertainty.file == self.file:
            raise ValueError("uncertainty.file must differ from file")
        return self

    @property
    def day(self) -> date:
        return date.fromisoformat(self.date)


def parse_sidecar(data: bytes | str | dict) -> BiomassSidecar:
    """Parse and validate a sidecar; raises ``ValueError`` with a readable message."""
    try:
        obj = json.loads(data) if isinstance(data, (bytes, str)) else data
    except json.JSONDecodeError as exc:
        raise ValueError(f"sidecar is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("sidecar must be a JSON object")
    try:
        return BiomassSidecar.model_validate(obj)
    except ValidationError as exc:
        msgs = "; ".join(f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors())
        raise ValueError(f"sidecar does not match biomass drop schema {SCHEMA_VERSION}: {msgs}") from exc


def discriminator(sc: BiomassSidecar) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", f"{sc.component}-{sc.producer.version}")


# ---------------------------------------------------------------------------- connector
class BiomassDropConnector(BaseConnector):
    """Drop-zone connector: ``inbox/biomass/*.json`` sidecars + GeoTIFFs -> ``biomass`` form A items."""

    @property
    def inbox(self) -> str:
        return str(self.dataset.options.get("inbox", INBOX)).strip("/")

    def _sidecars(self) -> list[str]:
        return [p for p in self.lake.find(self.inbox) if p.lower().endswith(".json")]

    def _drop_files(self, sidecar_rel: str, sc: BiomassSidecar) -> list[str]:
        folder = posixpath.dirname(sidecar_rel)
        names = [sc.file] + ([sc.uncertainty.file] if sc.uncertainty else [])
        return [posixpath.join(folder, n) for n in names]

    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        refs: list[SourceRef] = []
        by_item: dict[str, list[int]] = {}
        for rel in self._sidecars():
            raw = self.lake.read_bytes(rel)
            h = hashlib.sha1(raw)
            try:
                sc = parse_sidecar(raw)
            except ValueError as exc:
                refs.append(self.ref(f"biomass:{rel}:{h.hexdigest()[:16]}", datetime(1970, 1, 1, tzinfo=UTC),
                                     urls=[rel], sidecar=rel, error=str(exc)))
                continue
            if sc.aoi != aoi.id:
                continue
            if not period.start <= sc.day <= period.end:
                continue
            files = self._drop_files(rel, sc)
            missing = [f for f in files if not self.lake.exists(f)]
            error = f"drop files missing next to the sidecar: {missing}" if missing else None
            for f in files:
                if not missing:
                    with self.lake.open(f, "rb") as fh:
                        for chunk in iter(lambda fh=fh: fh.read(1 << 20), b""):
                            h.update(chunk)
            dt = datetime.combine(sc.day, time(12, 0), tzinfo=UTC)
            iid = self.item_id(aoi, self.ref("tmp", dt), discriminator(sc))
            meta = dict(sidecar=rel, files=files, item_id=iid)
            if error:
                meta["error"] = error
            by_item.setdefault(iid, []).append(len(refs))
            refs.append(self.ref(f"biomass:{rel}:{h.hexdigest()[:16]}", dt, urls=[rel, *files], **meta))
        for iid, idxs in by_item.items():   # two sidecars for one item id: fail both, never overwrite
            if len(idxs) > 1:
                names = [refs[i].meta["sidecar"] for i in idxs]
                for i in idxs:
                    r = refs[i]
                    refs[i] = SourceRef(r.dataset_id, r.key, r.datetime, r.urls,
                                        {**r.meta, "error": f"duplicate drops for item {iid}: {names}; remove one"})
        return sorted(refs, key=lambda r: (r.datetime, r.key))

    def _raw_dir(self, ref: SourceRef) -> str:
        stem = posixpath.splitext(posixpath.basename(ref.meta["sidecar"]))[0]
        return self.raw_path(ref, f"{stem}__{ref.key.rsplit(':', 1)[-1][:8]}")

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        if ref.meta.get("error"):
            return []
        out = []
        folder = self._raw_dir(ref)
        for src in [ref.meta["sidecar"], *ref.meta["files"]]:
            dst = f"{folder}/{posixpath.basename(src)}"
            self.lake.write_bytes(dst, self.lake.read_bytes(src))
            out.append(dst)
        return out

    # ------------------------------------------------------------------ transform
    def _read_layer(self, rel: str, band: int, sc: BiomassSidecar, unit: str, aoi: AOI,
                    valid_range) -> tuple[np.ndarray, dict]:
        """Read one drop band, convert to kg/m2, mask implausible values **at native resolution** (so they
        never leak into averages), reproject to the AOI grid. Returns ``(array, info)``."""
        grid = aoi.grid
        with self.lake.local_path(rel, "r") as path, rasterio.open(path) as src:
            if src.crs is None:
                raise ValueError(f"{posixpath.basename(rel)}: GeoTIFF has no CRS (sidecar says {sc.crs})")
            if CRS.from_user_input(src.crs) != CRS.from_user_input(sc.crs):
                raise ValueError(f"{posixpath.basename(rel)}: file CRS {src.crs.to_string()} != sidecar crs {sc.crs}")
            if not 1 <= band <= src.count:
                raise ValueError(f"{posixpath.basename(rel)}: band {band} not in 1..{src.count}")
            arr = src.read(band).astype("float64")
            nodata = sc.nodata if sc.nodata is not None else src.nodata
            crs, transform = src.crs.to_string(), src.transform
            lat = (aoi.bbox[1] + aoi.bbox[3]) / 2
            src_m = metres_per_pixel(crs, transform, lat=lat)
        if nodata is not None and not np.isnan(nodata):
            arr[arr == nodata] = np.nan
        arr = (arr * UNIT_TO_KG_M2[unit]).astype("float32")
        arr, native_frac = mask_out_of_range(arr, valid_range)
        resampling = choose_resampling(False, src_m, grid.resolution)
        out = reproject_array(arr, crs, transform, grid, resampling)
        return out, {"resampling": resampling, "source_pixel_size_m": round(src_m, 3),
                     "native_masked_fraction": native_frac}

    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        if ref.meta.get("error"):
            raise ValueError(f"biomass drop {ref.meta.get('sidecar')}: {ref.meta['error']}")
        sidecar_rel = next(p for p in raw if p.lower().endswith(".json"))
        sc = parse_sidecar(self.lake.read_bytes(sidecar_rel))
        by_name = {posixpath.basename(p): p for p in raw}
        iid = ref.meta["item_id"]

        layers = [("biomass", by_name[sc.file], sc.band, sc.unit)]
        if sc.uncertainty:
            layers.append(("biomass_uncertainty", by_name[sc.uncertainty.file], sc.uncertainty.band,
                           sc.uncertainty.unit or sc.unit))
        assets, qc, info = [], {}, {}
        for name, rel, band, unit in layers:
            var = self.variable(name)
            arr, info[name] = self._read_layer(rel, band, sc, unit, aoi, var.valid_range)
            if name == "biomass" and not np.isfinite(arr).any():
                raise ValueError(f"biomass drop {ref.meta['sidecar']}: no valid pixels over AOI {aoi.id} "
                                 "(check crs and extent)")
            arr, frac = mask_out_of_range(arr, var.valid_range)
            qc[name] = max(frac, info[name]["native_masked_fraction"])
            path = self.cog_path(aoi, iid, name)
            write_cog(self.lake, path, arr, aoi.grid, np.nan, var.dtype, description=name, unit=var.unit)
            assets.append(self.cog_asset(name, path))

        props = {
            "fuellake:qc_masked_fraction": max(qc.values()),
            "fuellake:biomass_component": sc.component,
            "fuellake:fuel_size_classes": list(sc.fuel_size_classes),
            "fuellake:mass_basis": sc.mass_basis,
            "fuellake:source_unit": sc.unit,
            "fuellake:unit_factor_to_kg_m2": UNIT_TO_KG_M2[sc.unit],
            "fuellake:producer": sc.producer.model_dump(exclude_none=True),
            "fuellake:producer_version": sc.producer.version,
            "fuellake:drop_schema_version": sc.schema_version,
            "fuellake:drop_sidecar": ref.meta["sidecar"],
            "fuellake:resampling": info["biomass"]["resampling"],
            "fuellake:source_pixel_size_m": info["biomass"]["source_pixel_size_m"],
        }
        if sc.uncertainty:
            props["fuellake:uncertainty_kind"] = sc.uncertainty.kind
        if sc.inputs:
            props["fuellake:inputs"] = list(sc.inputs)
        if sc.license:
            props["fuellake:drop_license"] = sc.license.model_dump(exclude_none=True)
        return [StagedItem(item_id=iid, datetime=ref.datetime, properties=props, assets=assets)]
