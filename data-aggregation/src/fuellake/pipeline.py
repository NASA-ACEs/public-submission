# fuel-lake — ETL pipeline: discover -> skip cataloged -> extract -> transform -> validate -> register
# Licensed under Apache-2.0.
"""Run connectors for one AOI and period.

Per dataset: load config, build the connector from its ``module:Class`` string, refresh the STAC
collection, ``discover``, drop refs whose ``key`` is already cataloged (``fuellake:source_key``) unless
``force``, then for each remaining unit ``extract`` -> ``transform`` -> **clip to the lake region** -> build
the STAC items -> validate **all** items of the unit -> register them only if all pass -> emit events.

Region (``config/lake.yaml``): the AOI must lie inside it (``load_aoi`` raises otherwise). Every staged
output is clipped centrally, whatever the connector did (:func:`fuellake.region.clip_staged_item`): raster
pixels outside become nodata, table rows outside are dropped, the item bbox is clamped to the region. The
items are then validated against the region too. Connectors of region-scale datasets still filter at the
source, so their raw subsets and item properties describe the region only.

AOI scale: a dataset runs only on AOIs whose ``scale`` (``local`` | ``region``) is in its ``scales``
(default ``[local]``). Otherwise it is refused (status ``failed``) or, in lenient mode, ``skipped``, unless
``force_scale``. This keeps heavy high-resolution datasets (NISAR, Sentinel-2, DEM, LANDFIRE) off ``ca``.

``limit`` caps the units actually **processed** (extract called) per dataset. Units skipped before any work
do not count: already cataloged keys, units the connector's cache marks as empty
(:meth:`BaseConnector.cached_empty`, recorded as ``empty``), and units skipped for missing credentials.

Per-unit error isolation: an exception or a failed contract check marks that unit ``failed`` /
``invalid`` and the run continues. A unit whose transform returns ``[]`` is ``empty``: nothing is
registered, so it is retried next run.

Lenient mode (``fuellake run all``): ``skip_stubs`` reports datasets with ``status: stub`` as ``skipped``
without running them; ``skip_missing_credentials`` asks the connector which secret env vars are missing
(:meth:`BaseConnector.missing_credentials`) and reports the dataset (if ``discover`` needs them) or the unit
as skipped (unit status ``missing_credentials``) instead of failing. Neither counts as a failure. Only env
var names are reported, never values. Explicitly named datasets run with both options off and fail loudly.

Events: ``item.created`` / ``item.updated`` for every registered item, plus ``table.updated`` for table
items; at the end ``run.completed`` if every unit succeeded or was skipped, else ``run.failed``.
The run manifest is written to ``runs/<run_id>.json``. Error texts are passed through
:func:`fuellake.config.redact` before they are stored.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from fuellake import __version__, catalog, events
from fuellake.config import Settings, redact
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.contracts import validate_item
from fuellake.rasters import COG_MEDIA_TYPE
from fuellake.region import Region, clip_staged_item
from fuellake.registry import (
    AOI,
    GOLD_DATASETS,
    DatasetConfig,
    lake_region,
    load_aoi,
    load_dataset,
    make_connector,
)
from fuellake.storage import Lake
from fuellake.tables import PARQUET_MEDIA_TYPE, parquet_n_rows

PIPELINE_VERSION = __version__
SAR_REQUIRED = ("sar:instrument_mode", "sar:frequency_band", "sar:polarizations", "sar:product_type")
log = logging.getLogger("fuellake.pipeline")


# ---------------------------------------------------------------------------- reports
UNIT_STATUSES = ("created", "updated", "empty", "invalid", "failed", "missing_credentials")


@dataclass
class UnitResult:
    key: str
    status: str                              # one of UNIT_STATUSES
    item_ids: list[str] = field(default_factory=list)
    error: str | None = None
    validation: list[dict] = field(default_factory=list)   # only failing results are kept
    reason: str | None = None                # why a unit was not processed (cache, missing credentials)


@dataclass
class DatasetReport:
    dataset_id: str
    status: str = "ok"                       # ok | partial | failed | no-connector | skipped
    discovered: int = 0
    skipped: int = 0                         # units already cataloged
    units: list[UnitResult] = field(default_factory=list)
    error: str | None = None
    reason: str | None = None                # e.g. "skipped: stub", "skipped: missing credentials (X)"
    missing_credentials: list[str] = field(default_factory=list)   # env var names only, never values

    def count(self, status: str) -> int:
        return sum(1 for u in self.units if u.status == status)


@dataclass
class RunReport:
    run_id: str
    aoi: str
    start: str
    end: str
    force: bool
    limit: int | None
    started_at: str
    finished_at: str | None = None
    pipeline_version: str = PIPELINE_VERSION
    datasets: list[DatasetReport] = field(default_factory=list)
    skip_stubs: bool = False
    skip_missing_credentials: bool = False
    force_scale: bool = False
    region: str | None = None

    @property
    def ok(self) -> bool:
        return all(d.status in ("ok", "no-connector", "skipped") for d in self.datasets)

    @property
    def manifest_path(self) -> str:
        return f"runs/{self.run_id}.json"

    def counts(self) -> dict[str, int]:
        """Unit counts per status (``created`` etc. count units) plus ``items`` registered."""
        out = {"discovered": 0, "skipped": 0, "created": 0, "updated": 0, "empty": 0, "invalid": 0,
               "failed": 0, "missing_credentials": 0, "items": 0}
        for d in self.datasets:
            out["discovered"] += d.discovered
            out["skipped"] += d.skipped
            for u in d.units:
                out[u.status] = out.get(u.status, 0) + 1
                if u.status in ("created", "updated"):
                    out["items"] += len(u.item_ids)
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        d["counts"] = self.counts()
        return d


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def _error_text(exc: BaseException) -> str:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:])
    return redact(f"{type(exc).__name__}: {exc}\n{tb}")


# ---------------------------------------------------------------------------- items
def build_stac_item(lake: Lake, dataset: DatasetConfig, aoi: AOI, ref: SourceRef, staged: StagedItem,
                    run_id: str) -> dict:
    """Turn a connector's ``StagedItem`` into a STAC item dict with the pipeline-owned properties."""
    props = dict(staged.properties)
    props.setdefault("fuellake:source_ids", list(ref.urls) or [ref.key])
    if dataset.maturity:
        props.setdefault("fuellake:maturity", dataset.maturity)
    # A connector may state redistributability per item (e.g. one source of a mixed-source table); a boolean it
    # sets is kept, anything else falls back to the dataset licence.
    if not isinstance(props.get("fuellake:redistributable"), bool):
        props["fuellake:redistributable"] = dataset.license.redistributable
    props.update({
        "fuellake:aoi": aoi.id,
        "fuellake:dataset": dataset.id,
        "fuellake:license": dataset.license.id,
        "fuellake:status": dataset.status,
        "fuellake:run_id": run_id,
        "fuellake:pipeline_version": PIPELINE_VERSION,
        "fuellake:source_key": ref.key,
    })
    grid = aoi.grid
    extensions: set[str] = set()
    assets: dict[str, dict] = {}
    for a in staged.assets:
        spec: dict = {"path": a.path, "type": a.media_type, "roles": list(a.roles), "title": a.key}
        if dataset.kind == "raster-a" and a.media_type == COG_MEDIA_TYPE:
            spec.update(grid.stac_proj())
            var = dataset.variable(a.key)
            band = a.raster_band or (var.stac_band() if var else None)
            if band:
                spec["raster:bands"] = [band]
            extensions |= {catalog.PROJ_EXT, catalog.RASTER_EXT}
        elif a.raster_band:
            spec["raster:bands"] = [a.raster_band]
            extensions.add(catalog.RASTER_EXT)
        assets[a.key] = spec
        if a.media_type == PARQUET_MEDIA_TYPE and a.key == "table" and "fuellake:n_rows" not in props:
            props["fuellake:n_rows"] = parquet_n_rows(lake, a.path)
    if any(k.startswith("sat:") for k in props):
        extensions.add(catalog.SAT_EXT)
    if all(k in props for k in SAR_REQUIRED):   # declare the schema only when it can validate
        extensions.add(catalog.SAR_EXT)
    bbox = staged.bbox or grid.bounds_wgs84()
    return catalog.make_item(dataset.id, staged.item_id, staged.datetime, props, assets, bbox, staged.geometry,
                             start=staged.start_datetime, end=staged.end_datetime, stac_extensions=extensions)


# ---------------------------------------------------------------------------- run
def _process_unit(lake: Lake, connector: BaseConnector, dataset: DatasetConfig, aoi: AOI, ref: SourceRef,
                  run_id: str, region: Region | None = None) -> UnitResult:
    unit = UnitResult(key=ref.key, status="failed")
    try:
        raw = connector.extract(ref, aoi) or []
        staged_items = connector.transform(ref, list(raw), aoi) or []
        if not staged_items:
            unit.status = "empty"
            return unit
        if region is not None:
            for s in staged_items:
                clip_staged_item(lake, s, region, default_bbox=aoi.grid.bounds_wgs84())
        items = [build_stac_item(lake, dataset, aoi, ref, s, run_id) for s in staged_items]
        failing = []
        for item in items:
            failing += [r.to_dict() for r in validate_item(lake, item, dataset, aoi, region=region) if not r.ok]
        if failing:
            unit.status = "invalid"
            unit.item_ids = [i["id"] for i in items]
            unit.validation = failing
            unit.error = redact("; ".join(f"{r['target']}: {', '.join(r['errors'])}" for r in failing))
            return unit
        any_created = False
        created_ids: list[str] = []
        for item in items:
            try:
                rel, created = catalog.add_item(lake, item)
            except Exception:
                # Do not leave a half-registered unit behind: its source key would make it skip forever.
                for iid in created_ids:
                    catalog.remove_item(lake, dataset.id, iid)
                raise
            if created:
                created_ids.append(item["id"])
            any_created |= created
            ev = dict(dataset=dataset.id, aoi=aoi.id, collection=dataset.id, item_id=item["id"], href=rel,
                      run_id=run_id)
            events.emit(lake, "item.created" if created else "item.updated", **ev)
            if dataset.kind == "table":
                events.emit(lake, "table.updated", table=dataset.table,
                            n_rows=item["properties"].get("fuellake:n_rows"), **ev)
            unit.item_ids.append(item["id"])
        unit.status = "created" if any_created else "updated"
    except Exception as exc:  # isolate this unit
        unit.status = "failed"
        unit.error = _error_text(exc)
        log.warning("unit %s/%s failed: %s", dataset.id, ref.key, unit.error.splitlines()[0])
    return unit


def _missing(connector: BaseConnector, aoi: AOI, ref: SourceRef | None) -> list[str]:
    try:
        return sorted(connector.missing_credentials(aoi, ref))
    except Exception as exc:  # a broken check must not hide the unit: let it run (and fail loudly)
        log.warning("%s: missing_credentials check failed: %s", connector.dataset.id, redact(exc))
        return []


def _cached_empty(connector: BaseConnector, aoi: AOI, ref: SourceRef) -> bool:
    try:
        return bool(connector.cached_empty(ref, aoi))
    except Exception as exc:
        log.warning("%s/%s: cached_empty check failed: %s", connector.dataset.id, ref.key, redact(exc))
        return False


def _credentials_reason(names: list[str]) -> str:
    return f"skipped: missing credentials ({', '.join(names)})"


def scale_mismatch(dataset: DatasetConfig, aoi: AOI) -> str | None:
    """Why ``dataset`` does not run on ``aoi`` (its ``scales`` lack the AOI's ``scale``), or None."""
    if dataset.supports(aoi):
        return None
    return (f"{dataset.id} supports AOI scales {list(dataset.scales)}; AOI {aoi.id} is {aoi.scale!r} "
            "(pass --force-scale to run it anyway)")


def _run_dataset(lake: Lake, settings: Settings, dataset_id: str, aoi: AOI, period: Period, force: bool,
                 limit: int | None, run_id: str, skip_stubs: bool = False,
                 skip_missing_credentials: bool = False, force_scale: bool = False,
                 region: Region | None = None) -> DatasetReport:
    rep = DatasetReport(dataset_id=dataset_id)
    try:
        dataset = load_dataset(dataset_id)
    except Exception as exc:
        rep.status, rep.error = "failed", _error_text(exc)
        return rep
    if not dataset.connector:
        rep.status = "no-connector"
        rep.error = f"dataset {dataset.id} has no connector (status: {dataset.status})"
        if dataset.id in GOLD_DATASETS:
            rep.error += ("; it is a gold collection: build it with `fuellake "
                          f"{'build-points' if dataset.id == 'point-features' else 'build-features'}`")
        return rep
    if skip_stubs and dataset.status == "stub":
        rep.status, rep.reason = "skipped", "skipped: stub (run it by name to try it)"
        return rep
    mismatch = None if force_scale else scale_mismatch(dataset, aoi)
    if mismatch:
        if skip_stubs or skip_missing_credentials:       # lenient mode (`run all`): not a failure
            rep.status, rep.reason = "skipped", f"skipped: {mismatch}"
        else:
            rep.status, rep.error = "failed", f"refused: {mismatch}"
        return rep
    try:
        connector = make_connector(dataset, lake, settings)
        catalog.upsert_collection(lake, dataset, bbox=aoi.bbox)
        if skip_missing_credentials:
            missing = _missing(connector, aoi, None)
            if missing:
                rep.status, rep.reason, rep.missing_credentials = "skipped", _credentials_reason(missing), missing
                return rep
        refs = connector.discover(aoi, period)
    except Exception as exc:
        rep.status, rep.error = "failed", _error_text(exc)
        return rep
    rep.discovered = len(refs)
    known = set() if force else set(catalog.source_keys(lake, dataset.id, aoi=aoi.id))
    todo = [r for r in refs if r.key not in known]
    rep.skipped = len(refs) - len(todo)
    processed = 0
    for ref in todo:
        if limit is not None and processed >= max(0, limit):
            break
        if _cached_empty(connector, aoi, ref):
            rep.units.append(UnitResult(key=ref.key, status="empty", reason="cached: the connector's cache says "
                                                                             "this unit stages nothing"))
            continue
        if skip_missing_credentials:
            missing = _missing(connector, aoi, ref)
            if missing:
                rep.units.append(UnitResult(key=ref.key, status="missing_credentials",
                                            reason=_credentials_reason(missing)))
                rep.missing_credentials = sorted(set(rep.missing_credentials) | set(missing))
                continue
        rep.units.append(_process_unit(lake, connector, dataset, aoi, ref, run_id, region))
        processed += 1
    bad = sum(1 for u in rep.units if u.status in ("failed", "invalid"))
    if bad:
        processed_units = [u for u in rep.units if u.status != "missing_credentials"]
        rep.status = "failed" if bad == len(processed_units) else "partial"
    no_creds = rep.count("missing_credentials")
    if no_creds:
        rep.reason = f"{_credentials_reason(rep.missing_credentials)} for {no_creds} unit(s)"
        if no_creds == len(rep.units) and not bad:
            rep.status = "skipped"
    return rep


def run(dataset_ids: list[str], aoi_id: str, period: Period, *, force: bool = False, limit: int | None = None,
        lake: Lake | None = None, settings: Settings | None = None, skip_stubs: bool = False,
        skip_missing_credentials: bool = False, force_scale: bool = False) -> RunReport:
    """Run the given datasets for one AOI and period.

    ``limit`` caps the processed units per dataset (see the module docstring). ``skip_stubs`` and
    ``skip_missing_credentials`` turn on the lenient mode used by ``fuellake run all``. ``force_scale`` runs
    datasets whose ``scales`` do not include the AOI's scale. Raises ``KeyError`` for an unknown AOI and
    :class:`fuellake.region.OutsideRegionError` for an AOI outside the lake region (before writing anything)."""
    settings = settings or Settings.from_env()
    lake = lake or Lake(settings.lake_root, storage_options=settings.storage_options())
    run_id = new_run_id()
    aoi = load_aoi(aoi_id)
    region = lake_region()
    report = RunReport(run_id=run_id, aoi=aoi_id, start=period.start.isoformat(), end=period.end.isoformat(),
                       force=force, limit=limit, started_at=catalog.iso(datetime.now(UTC)),
                       skip_stubs=skip_stubs, skip_missing_credentials=skip_missing_credentials,
                       force_scale=force_scale, region=region.id if region else None)
    catalog.init_catalog(lake)
    for dataset_id in dataset_ids:
        log.info("run %s: dataset %s", run_id, dataset_id)
        report.datasets.append(_run_dataset(lake, settings, dataset_id, aoi, period, force, limit, run_id,
                                            skip_stubs=skip_stubs,
                                            skip_missing_credentials=skip_missing_credentials,
                                            force_scale=force_scale, region=region))
    report.finished_at = catalog.iso(datetime.now(UTC))
    lake.write_json(report.manifest_path, report.to_dict())
    events.emit(lake, "run.completed" if report.ok else "run.failed", aoi=aoi_id, run_id=run_id,
                href=report.manifest_path, datasets=list(dataset_ids), counts=report.counts())
    return report


def discover(dataset_ids: list[str], aoi_id: str, period: Period, *, lake: Lake | None = None,
             settings: Settings | None = None) -> dict[str, list[tuple[SourceRef, bool]]]:
    """Run only ``discover``; returns ``{dataset_id: [(ref, already_cataloged), ...]}``. Writes nothing."""
    settings = settings or Settings.from_env()
    lake = lake or Lake(settings.lake_root, storage_options=settings.storage_options())
    aoi = load_aoi(aoi_id)
    out: dict[str, list[tuple[SourceRef, bool]]] = {}
    for dataset_id in dataset_ids:
        dataset = load_dataset(dataset_id)
        connector = make_connector(dataset, lake, settings)
        known = set(catalog.source_keys(lake, dataset.id, aoi=aoi.id))
        out[dataset_id] = [(r, r.key in known) for r in connector.discover(aoi, period)]
    return out
