# fuel-lake — connector base classes and staging data types
# Licensed under Apache-2.0.
"""What a source connector implements, and what it hands back to the pipeline.

A connector does three things for one dataset:

1. ``discover(aoi, period)`` -> ``list[SourceRef]``: list units of work (granules, months, queries). Each
   ``SourceRef.key`` must be **stable** across runs: the pipeline skips refs whose key is already
   cataloged (``fuellake:source_key``), unless ``force``.
2. ``extract(ref, aoi)`` -> lake-relative paths written under ``raw/<dataset>/<yyyy>/<mm>/`` (may be ``[]``
   when the transform streams directly from the source).
3. ``transform(ref, raw, aoi)`` -> ``list[StagedItem]``: write staged files (form A COG via
   :func:`fuellake.rasters.write_cog`, form B Zarr via :func:`fuellake.rasters.write_zarr`, table part via
   :func:`fuellake.tables.write_geoparquet`) and describe them.

The pipeline then fills the common STAC properties, validates every output against the contracts, and
registers the items. A ``transform`` returning ``[]`` means "nothing to stage for this unit": no item is
written, so that unit is retried on the next run (not idempotent by design; return an item if you want
the unit remembered).

STAC properties
---------------
Filled by the pipeline (do not set): ``fuellake:aoi``, ``fuellake:dataset``, ``fuellake:license``,
``fuellake:status``, ``fuellake:run_id``, ``fuellake:pipeline_version``,
``fuellake:source_key``; ``fuellake:redistributable`` defaults to the dataset licence value (a boolean the
connector sets for one item, e.g. per source of a mixed table, is kept); ``fuellake:source_ids`` defaults to
``ref.urls`` (or ``[ref.key]``);
``fuellake:maturity`` defaults to the dataset config value; form A assets get ``proj:*`` from the AOI
grid and ``raster:bands`` from the variable spec unless you pass ``raster_band``; table items get
``fuellake:n_rows`` from the parquet footer.

Set by the connector in ``StagedItem.properties``: ``fuellake:qc_masked_fraction`` (form A, from
:func:`fuellake.rasters.mask_out_of_range`), and for NISAR ``sat:relative_orbit``, ``fuellake:frame``,
``sat:orbit_state``, ``fuellake:processing_id``, ``sar:polarizations``, ``sar:frequency_band`` (``"L"``)
and the per-granule ``fuellake:maturity``. Anything else source-specific is welcome (prefix it).

Optional hooks (defaults are fine for most connectors)
-------------------------------------------------------
- ``cached_empty(ref, aoi)``: return True when the connector's own cache (under ``raw/``) already says that
  the unit stages nothing (e.g. a Sentinel-2 date recorded as too cloudy). The pipeline then records the
  unit as ``empty`` without calling ``extract``/``transform``, and the unit does not use up ``--limit``.
- ``missing_credentials(aoi, ref=None)``: names (never values) of the secret env vars that are needed but not
  set, for ``discover`` (``ref=None``) or for one unit. ``fuellake run all`` skips such datasets / units and
  reports them as "skipped: missing credentials"; explicitly named datasets still run and fail loudly. The
  default asks for every ``access.env`` variable of an ``earthdata`` / ``api_key`` dataset for each unit and
  nothing for ``discover``.

Asset keys: for form A the asset key **equals the variable name** in the dataset config, and the file is
``staged/<dataset>/<aoi>/<item_id>/<variable>.tif``. Table items have exactly one asset, key ``table``.
Form B items have one asset, key ``data``, at ``.../<item_id>/data.zarr``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from fuellake.config import Settings
from fuellake.rasters import COG_MEDIA_TYPE, ZARR_MEDIA_TYPE
from fuellake.registry import AOI, DatasetConfig, Variable
from fuellake.storage import Lake
from fuellake.tables import PARQUET_MEDIA_TYPE, table_part_path


@dataclass(frozen=True)
class Period:
    start: date
    end: date   # inclusive

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"period end {self.end} is before start {self.start}")


@dataclass(frozen=True)
class SourceRef:
    dataset_id: str
    key: str                       # stable id of one unit of work (granule id, month, query hash)
    datetime: datetime             # UTC
    urls: tuple[str, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)   # e.g. track, frame, maturity, size_bytes


@dataclass
class StagedAsset:
    key: str
    path: str                      # lake-relative
    media_type: str
    roles: list[str]
    raster_band: dict | None = None   # raster:bands entry; default from the variable spec


@dataclass
class StagedItem:
    item_id: str
    datetime: datetime
    properties: dict
    assets: list[StagedAsset]
    bbox: tuple | None = None      # WGS84; default: the AOI grid's WGS84 bounds
    # Optional extras (keyword use only): an interval for period items (form B months, tables).
    start_datetime: datetime | None = None
    end_datetime: datetime | None = None
    geometry: dict | None = None


class BaseConnector(ABC):
    """Base class for source connectors. Subclasses are referenced from the dataset YAML as
    ``connector: fuellake.connectors.<module>:<Class>`` and are constructed by the pipeline."""

    def __init__(self, dataset: DatasetConfig, lake: Lake, settings: Settings):
        self.dataset = dataset
        self.lake = lake
        self.settings = settings

    # ------------------------------------------------------------------ to implement
    @abstractmethod
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]: ...

    @abstractmethod
    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]: ...  # writes raw/; lake-relative paths (may be [])

    @abstractmethod
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]: ...

    # ------------------------------------------------------------------ optional hooks
    def cached_empty(self, ref: SourceRef, aoi: AOI) -> bool:
        """True if this connector's own cache already says that ``ref`` stages nothing (see the module
        docstring). Must be cheap and must not touch the network. Default: False."""
        return False

    def missing_credentials(self, aoi: AOI, ref: SourceRef | None = None) -> list[str]:
        """Names of the secret env vars needed for ``discover`` (``ref=None``) or for unit ``ref`` that are not
        set. Never returns or logs values. Default: none for discover; for a unit, the dataset's ``access.env``
        when ``access.auth`` is ``earthdata`` or ``api_key``."""
        if ref is None or self.dataset.access.auth not in ("earthdata", "api_key"):
            return []
        return [name for name in self.dataset.access.env if not self.settings.has_secret(name)]

    # ------------------------------------------------------------------ helpers
    def item_id(self, aoi: AOI, ref: SourceRef, discriminator: str | None = None) -> str:
        """``<dataset>__<aoi>__<yyyymmdd>[__<discriminator>]``."""
        iid = f"{self.dataset.id}__{aoi.id}__{ref.datetime:%Y%m%d}"
        return f"{iid}__{discriminator}" if discriminator else iid

    def ref(self, key: str, dt: datetime, urls: tuple[str, ...] | list[str] = (), **meta: Any) -> SourceRef:
        """Shorthand to build a ``SourceRef`` for this dataset."""
        return SourceRef(dataset_id=self.dataset.id, key=key, datetime=dt, urls=tuple(urls), meta=meta)

    def raw_path(self, ref: SourceRef, filename: str) -> str:
        """``raw/<dataset>/<yyyy>/<mm>/<filename>``."""
        return f"raw/{self.dataset.id}/{ref.datetime:%Y}/{ref.datetime:%m}/{filename}"

    def staged_dir(self, aoi: AOI, item_id: str) -> str:
        return f"staged/{self.dataset.id}/{aoi.id}/{item_id}"

    def cog_path(self, aoi: AOI, item_id: str, variable: str) -> str:
        """``staged/<dataset>/<aoi>/<item_id>/<variable>.tif`` (form A)."""
        return f"{self.staged_dir(aoi, item_id)}/{variable}.tif"

    def zarr_path(self, aoi: AOI, item_id: str) -> str:
        """``staged/<dataset>/<aoi>/<item_id>/data.zarr`` (form B)."""
        return f"{self.staged_dir(aoi, item_id)}/data.zarr"

    def table_path(self, key: str) -> str:
        """``staged/tables/<table>/<dataset>/part-<key>.parquet``."""
        if not self.dataset.table:
            raise ValueError(f"dataset {self.dataset.id} is not a table dataset")
        return table_part_path(self.dataset.table, self.dataset.id, key)

    def variable(self, name: str) -> Variable:
        v = self.dataset.variable(name)
        if v is None:
            raise KeyError(f"variable {name!r} is not declared in config/datasets/{self.dataset.id}.yaml")
        return v

    def cog_asset(self, variable: str, path: str) -> StagedAsset:
        """Form A asset for ``variable`` (asset key = variable name)."""
        return StagedAsset(key=variable, path=path, media_type=COG_MEDIA_TYPE, roles=["data"],
                           raster_band=self.variable(variable).stac_band())

    def zarr_asset(self, path: str) -> StagedAsset:
        return StagedAsset(key="data", path=path, media_type=ZARR_MEDIA_TYPE, roles=["data"])

    def table_asset(self, path: str) -> StagedAsset:
        return StagedAsset(key="table", path=path, media_type=PARQUET_MEDIA_TYPE, roles=["data"])

    def http_session(self, bearer_env: str | None = None):
        """``requests.Session`` with ``User-Agent: fuel-lake/0.1`` and, optionally, a bearer token read
        from the named secret env var (e.g. ``EARTHDATA_TOKEN``). Never log the session headers."""
        import requests

        s = requests.Session()
        s.headers.update(self.settings.http_headers())
        if bearer_env:
            token = self.settings.secret(bearer_env)
            if token:
                s.headers["Authorization"] = f"Bearer {token}"
        return s


class StubConnector(BaseConnector):
    """Convenience base for datasets that are registered but not implemented."""

    reason = "connector not implemented"

    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        raise NotImplementedError(f"{self.dataset.id}: {self.reason}")

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        raise NotImplementedError(f"{self.dataset.id}: {self.reason}")

    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        raise NotImplementedError(f"{self.dataset.id}: {self.reason}")
