# fuel-lake — AOI and dataset registry (YAML configs -> pydantic models)
# Licensed under Apache-2.0.
"""Load ``config/lake.yaml``, ``config/regions/<id>.yaml``, ``config/aois/<id>.yaml`` and
``config/datasets/<id>.yaml``.

The config directory is ``$FUELLAKE_CONFIG_DIR`` or ``<repo>/config``. Files are re-read on every call
(no cache; the region polygon is cached until its files change), so tests and long-running services always
see the current YAML.

Lake region: ``lake.yaml`` names the monitored region (:func:`lake_region`). :func:`load_aoi` refuses an AOI
that does not lie inside it (:class:`fuellake.region.OutsideRegionError`). A config directory without
``lake.yaml`` (or with ``region: null``) has no region: nothing is checked or clipped.

AOI scales: an AOI is ``local`` (default; small, high resolution, e.g. ``smm`` at 30 m) or ``region`` (the
whole monitored region, ``ca`` on the NISAR SME2 grid: EASE-Grid 2.0, 200.179 m). A dataset lists the scales it
supports in ``scales`` (default ``[local]``); the pipeline refuses other AOIs unless forced.

Connectors are referenced by the string ``module:Class`` in the dataset ``connector`` field and loaded
with :func:`load_connector_class`; core code never imports a concrete connector.
"""

from __future__ import annotations

import importlib
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fuellake.config import Settings
from fuellake.grid import REFERENCE_GRIDS, Grid, grid_from_bbox

if TYPE_CHECKING:
    from fuellake.connectors.base import BaseConnector
    from fuellake.region import Region
    from fuellake.storage import Lake

DatasetKind = Literal["raster-a", "raster-b", "table"]
# ``interface``: no connector; the team's systems publish into it with LakeClient.publish_raster / publish_table.
DatasetStatus = Literal["tested", "search-only", "stub", "unverified", "interface"]
Scale = Literal["local", "region"]
SCALES = ("local", "region")
MATURITIES = ("BETA", "PROVISIONAL", "VALIDATED")
# Gold collections: registered like datasets (config/datasets/<id>.yaml) but written by `fuellake
# build-features` / `fuellake build-points` (fuellake.features), so they have no connector.
GOLD_DATASETS = ("features-cube", "matchups", "point-features")


# ---------------------------------------------------------------------------- AOI
class NisarStack(BaseModel):
    model_config = ConfigDict(extra="allow")
    track: int
    frame: int
    direction: Literal["ascending", "descending"]


class AOI(BaseModel):
    """An area of interest and its form A target grid.

    ``reference_grid`` (a key of :data:`fuellake.grid.REFERENCE_GRIDS`, e.g. ``ease2-200m``) takes ``crs``,
    ``resolution`` and the snap ``origin`` from a named global grid, so the AOI's cells coincide with that
    product's cells; ``crs`` / ``resolution`` may then be omitted (if given, they must match). ``origin`` (map
    units, a cell corner) can also be set on its own; default ``(0, 0)``."""

    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    bbox: tuple[float, float, float, float]   # WGS84 west, south, east, north
    crs: str
    resolution: float                          # map units (metres), target grid for form A
    origin: tuple[float, float] | None = None  # snap origin (a cell corner); None = (0, 0)
    reference_grid: str | None = None          # named global grid (fuellake.grid.REFERENCE_GRIDS)
    scale: Scale = "local"                     # local (small, high resolution) | region (whole region, coarse)
    nisar: NisarStack | None = None

    @model_validator(mode="before")
    @classmethod
    def _reference_grid(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not data.get("reference_grid"):
            return data
        name = data["reference_grid"]
        ref = REFERENCE_GRIDS.get(name)
        if ref is None:
            raise ValueError(f"AOI {data.get('id')}: unknown reference_grid {name!r}; known: {sorted(REFERENCE_GRIDS)}")
        out = dict(data)
        for key, value in (("crs", ref.crs), ("resolution", ref.resolution), ("origin", ref.origin)):
            given = out.get(key)
            if given is None:
                out[key] = value
            elif key == "crs" and crs_equal(given, value):
                out[key] = value
            elif key != "crs" and all(abs(float(a) - float(b)) < 1e-6
                                      for a, b in zip(_pair(given), _pair(value), strict=True)):
                out[key] = value              # keep the full-precision constants of the reference grid
            else:
                raise ValueError(f"AOI {data.get('id')}: {key}={given!r} contradicts reference_grid {name} "
                                 f"({key}={value!r}); leave it out")
        return out

    @property
    def grid(self) -> Grid:
        """Target grid: bbox projected to ``crs``, snapped outward to whole cells counted from ``origin``."""
        return grid_from_bbox(self.bbox, self.crs, self.resolution, self.origin)

    def grid_at(self, resolution: float) -> Grid:
        """Same AOI on a different resolution (e.g. a coarser cube), snapped to the same origin."""
        return grid_from_bbox(self.bbox, self.crs, resolution, self.origin)


def _pair(v: Any) -> tuple[float, ...]:
    return tuple(float(x) for x in v) if isinstance(v, (list, tuple)) else (float(v),)


def crs_equal(a: str, b: str) -> bool:
    from rasterio.crs import CRS

    return CRS.from_user_input(a) == CRS.from_user_input(b)


# ---------------------------------------------------------------------------- dataset
class Access(BaseModel):
    model_config = ConfigDict(extra="allow")
    method: str = "unknown"
    # "unknown" = not yet verified (the inventory's "verify"); otherwise as in ARCHITECTURE.md.
    auth: Literal["none", "earthdata", "api_key", "unknown"] = "unknown"
    env: list[str] = Field(default_factory=list)   # names of env vars, never values


class License(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    url: str | None = None
    redistributable: bool = False
    attribution: str = ""
    notes: str = ""


def _parse_number(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"nan", ".nan"}:
            return float("nan")
        if s in {"inf", "+inf", ".inf"}:
            return float("inf")
        if s in {"-inf", "-.inf"}:
            return float("-inf")
        try:
            return int(s) if s.lstrip("-").isdigit() else float(s)
        except ValueError:
            return v
    return v


class Variable(BaseModel):
    """One variable = one band = one form A asset (the asset key equals ``name``)."""

    model_config = ConfigDict(extra="allow")
    name: str
    unit: str | None = None
    dtype: str = "float32"
    nodata: float | int | None = float("nan")
    valid_range: tuple[float, float] | None = None
    resampling: str = "bilinear"
    description: str | None = None
    # Interface datasets only: a published item must carry every required variable (optional ones may be absent).
    required: bool = True
    # Matchups: lag this variable (columns <name>_lag<L>d) unless build-features is given its own list.
    lag: bool = False

    @field_validator("nodata", mode="before")
    @classmethod
    def _nodata(cls, v: Any) -> Any:
        return _parse_number(v)

    @field_validator("dtype")
    @classmethod
    def _dtype(cls, v: str) -> str:
        return np.dtype(v).name

    @property
    def is_float(self) -> bool:
        return np.issubdtype(np.dtype(self.dtype), np.floating)

    @property
    def categorical(self) -> bool:
        return not self.is_float

    @property
    def nodata_is_nan(self) -> bool:
        return isinstance(self.nodata, float) and math.isnan(self.nodata)

    def stac_band(self) -> dict:
        """``raster:bands`` entry (raster extension v1.1.0). NaN nodata is encoded as the string "nan"."""
        band: dict[str, Any] = {"data_type": self.dtype}
        if self.nodata is not None:
            band["nodata"] = "nan" if self.nodata_is_nan else self.nodata
        if self.unit:
            band["unit"] = self.unit
        return band


ITEM_PROPERTY_TYPES = ("string", "number", "integer", "boolean", "datetime", "item", "items")


class ItemProperty(BaseModel):
    """An item property an interface dataset requires from publishers (``item_properties`` in the YAML).

    ``type``: ``string`` | ``number`` | ``integer`` | ``boolean`` | ``datetime`` (ISO 8601) | ``item`` (one
    cataloged item id) | ``items`` (list of cataloged item ids). ``collection`` restricts ``item``/``items`` to
    one collection. ``maps_to`` copies the value to a STAC field (``start_datetime`` / ``end_datetime``)."""

    model_config = ConfigDict(extra="allow")
    name: str                       # full property name, e.g. "spread_risk:valid_from"
    type: Literal["string", "number", "integer", "boolean", "datetime", "item", "items"] = "string"
    required: bool = True
    description: str | None = None
    collection: str | None = None
    maps_to: Literal["start_datetime", "end_datetime"] | None = None

    @property
    def short_name(self) -> str:
        return self.name.split(":", 1)[-1]


class DatasetConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    title: str
    kind: DatasetKind
    table: str | None = None
    connector: str | None = None         # "module:Class" or null
    status: DatasetStatus = "unverified"
    provider: str = ""
    homepage: str | None = None
    access: Access = Field(default_factory=Access)
    license: License
    cadence: str | None = None
    native_resolution: str | None = None
    maturity: str | None = None          # NISAR only
    variables: list[Variable] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)   # connector-specific settings
    scales: list[Scale] = Field(default_factory=lambda: ["local"])   # AOI scales this dataset supports
    item_properties: list[ItemProperty] = Field(default_factory=list)   # interface datasets: publisher fields

    @model_validator(mode="after")
    def _check(self) -> DatasetConfig:
        if self.kind == "table" and not self.table:
            raise ValueError(f"dataset {self.id}: kind=table requires 'table'")
        if self.maturity is not None and self.maturity not in MATURITIES:
            raise ValueError(f"dataset {self.id}: maturity must be one of {MATURITIES}")
        if self.connector is not None and ":" not in self.connector:
            raise ValueError(f"dataset {self.id}: connector must look like 'package.module:Class'")
        if not self.scales:
            raise ValueError(f"dataset {self.id}: scales must list at least one of {SCALES}")
        if self.status == "interface" and self.connector is not None:
            raise ValueError(f"dataset {self.id}: status interface means no connector (outputs are published)")
        return self

    @property
    def is_interface(self) -> bool:
        """Published into by the team's systems (``LakeClient.publish_*``), not fetched by a connector."""
        return self.status == "interface"

    def supports(self, aoi: AOI) -> bool:
        """True if the dataset declares the AOI's scale in ``scales``."""
        return aoi.scale in self.scales

    def item_property(self, name: str) -> ItemProperty | None:
        for p in self.item_properties:
            if p.name == name:
                return p
        return None

    @property
    def is_nisar(self) -> bool:
        return self.id.startswith("nisar-")

    @property
    def is_static(self) -> bool:
        """Time-invariant form A layer: ``kind: raster-a`` and a ``cadence`` starting with ``static`` (cop-dem,
        landfire-fbfm40). Its collection gets ``fuellake:static: true`` and catalog searches ignore the time
        filter for it. Tables are never static here: Globe-LFMC is "static (published 2024-03-25)" but its
        rows are dated samples."""
        return self.kind == "raster-a" and (self.cadence or "").strip().lower().startswith("static")

    def variable(self, name: str) -> Variable | None:
        for v in self.variables:
            if v.name == name:
                return v
        return None


# ---------------------------------------------------------------------------- loaders
def config_dir() -> Path:
    return Settings.from_env().config_dir


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")
    return data


class LakeConfig(BaseModel):
    """``config/lake.yaml``: lake-level settings."""

    model_config = ConfigDict(extra="allow")
    region: str | None = None          # id of config/regions/<id>.yaml; None = no region (nothing clipped)


def load_lake_config() -> LakeConfig:
    """``config/lake.yaml``; an empty default (no region) when the file does not exist."""
    path = config_dir() / "lake.yaml"
    return LakeConfig(**_read_yaml(path)) if path.is_file() else LakeConfig()


def load_region(region_id: str) -> Region:
    """``config/regions/<id>.yaml`` and its polygon (see :mod:`fuellake.region`)."""
    from fuellake.region import load_region_file

    path = config_dir() / "regions" / f"{region_id}.yaml"
    if not path.is_file():
        raise KeyError(f"unknown region {region_id!r} (no {path})")
    return load_region_file(path)


def lake_region() -> Region | None:
    """The monitored region named in ``config/lake.yaml``, or None if the lake has none."""
    rid = load_lake_config().region
    return load_region(rid) if rid else None


def load_aoi(aoi_id: str, *, check_region: bool = True) -> AOI:
    """Load an AOI. Raises :class:`fuellake.region.OutsideRegionError` (a ``ValueError``) if it does not lie
    inside the lake region (``check_region=False`` skips that, e.g. to report on a bad AOI)."""
    path = config_dir() / "aois" / f"{aoi_id}.yaml"
    if not path.is_file():
        raise KeyError(f"unknown AOI {aoi_id!r} (no {path})")
    aoi = AOI(**_read_yaml(path))
    if aoi.id != aoi_id:
        raise ValueError(f"{path}: id {aoi.id!r} does not match file name")
    if check_region:
        region = lake_region()
        if region is not None:
            region.check_aoi(aoi)
    return aoi


def list_aois() -> list[AOI]:
    d = config_dir() / "aois"
    return [load_aoi(p.stem) for p in sorted(d.glob("*.yaml"))] if d.is_dir() else []


def load_dataset(dataset_id: str) -> DatasetConfig:
    path = config_dir() / "datasets" / f"{dataset_id}.yaml"
    if not path.is_file():
        raise KeyError(f"unknown dataset {dataset_id!r} (no {path})")
    ds = DatasetConfig(**_read_yaml(path))
    if ds.id != dataset_id:
        raise ValueError(f"{path}: id {ds.id!r} does not match file name")
    return ds


def list_datasets() -> list[DatasetConfig]:
    d = config_dir() / "datasets"
    return [load_dataset(p.stem) for p in sorted(d.glob("*.yaml"))] if d.is_dir() else []


def load_connector_class(spec: str) -> type[BaseConnector]:
    """Import ``package.module:Class`` and return the class."""
    from fuellake.connectors.base import BaseConnector

    module_name, _, class_name = spec.partition(":")
    if not module_name or not class_name:
        raise ValueError(f"bad connector spec {spec!r}; expected 'package.module:Class'")
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    if not (isinstance(cls, type) and issubclass(cls, BaseConnector)):
        raise TypeError(f"{spec} is not a BaseConnector subclass")
    return cls


def make_connector(dataset: DatasetConfig, lake: Lake, settings: Settings | None = None) -> BaseConnector:
    """Instantiate the dataset's connector. Raises ``LookupError`` if the dataset has none."""
    if not dataset.connector:
        raise LookupError(f"dataset {dataset.id} has no connector (status: {dataset.status})")
    cls = load_connector_class(dataset.connector)
    return cls(dataset, lake, settings or Settings.from_env())
