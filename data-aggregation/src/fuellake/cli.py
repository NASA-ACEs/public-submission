# fuel-lake — command line interface (typer)
# Licensed under Apache-2.0.
"""``fuellake`` CLI: init · datasets · discover · run · validate · build-features · build-points · licenses ·
events · serve.

Imports of the heavier modules happen inside each command, so read-only commands (``datasets``,
``licenses``) keep working while other modules are being edited.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="fuel-lake: data lake, ETL and STAC catalog for soil moisture -> LFMC -> fire risk.")


def _date(value: str | None, default: date | None = None, name: str = "date") -> date:
    if value is None:
        if default is None:
            raise typer.BadParameter(f"--{name} is required (YYYY-MM-DD)")
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"--{name} must be YYYY-MM-DD, got {value!r}") from exc


def _period(start: str | None, end: str | None):
    from fuellake.connectors.base import Period

    end_d = _date(end, date.today(), "end")
    start_d = _date(start, end_d - timedelta(days=30), "start")
    return Period(start_d, end_d)


def _is_all(dataset_ids: list[str] | None) -> bool:
    return not dataset_ids or list(dataset_ids) == ["all"]


def _expand(dataset_ids: list[str]) -> list[str]:
    """``[]`` / ``["all"]`` -> every dataset with a connector (the gold collections have none)."""
    from fuellake.registry import list_datasets

    if _is_all(dataset_ids):
        return [d.id for d in list_datasets() if d.connector]
    return dataset_ids


def _fail(message: str, code: int = 1) -> typer.Exit:
    """Print a one-line error (secrets redacted) and return the Exit to raise."""
    from fuellake.config import redact

    typer.echo(f"error: {redact(message).splitlines()[0] if message else 'failed'}", err=True)
    return typer.Exit(code)


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Log progress to stderr.")) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")


@app.command()
def init() -> None:
    """Create the lake directories, the root STAC catalog and one collection per dataset."""
    from fuellake import catalog
    from fuellake.registry import list_datasets
    from fuellake.storage import Lake

    lake = Lake.from_env()
    lake.init()
    catalog.init_catalog(lake)
    datasets = list_datasets()
    for ds in datasets:
        catalog.upsert_collection(lake, ds)
    typer.echo(f"lake root: {lake.root}")
    typer.echo(f"catalog: {lake.url(catalog.CATALOG_REL)} ({len(datasets)} collections)")


@app.command()
def datasets(as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """List registered datasets with status and licence (reads config only)."""
    from fuellake.registry import GOLD_DATASETS, list_datasets

    rows = list_datasets()
    if as_json:
        typer.echo(json.dumps([d.model_dump(mode="json") for d in rows], indent=2, default=str))
        return
    lw = max([len("licence"), *(len(d.license.id) for d in rows)])
    header = f"{'id':<20} {'kind':<9} {'status':<12} {'connector':<9} {'licence':<{lw}} redistributable"
    typer.echo(header)
    typer.echo("-" * len(header))
    for d in rows:
        # gold: built by `fuellake build-features`; publish: interface written by LakeClient.publish_*
        conn = "yes" if d.connector else ("gold" if d.id in GOLD_DATASETS else "publish" if d.is_interface else "-")
        typer.echo(f"{d.id:<20} {d.kind:<9} {d.status:<12} {conn:<9} "
                   f"{d.license.id:<{lw}} {'yes' if d.license.redistributable else 'no'}")


@app.command()
def discover(
    dataset_ids: list[str] = typer.Argument(None, help="Dataset ids, or 'all' (datasets with a connector)."),
    aoi: str = typer.Option("smm", help="AOI id."),
    start: str = typer.Option(None, help="YYYY-MM-DD (default: end - 30 days)."),
    end: str = typer.Option(None, help="YYYY-MM-DD (default: today)."),
) -> None:
    """Run only connector discovery and show which units are already cataloged (writes nothing).

    With 'all', stub datasets and datasets that do not support the AOI's scale are skipped. A dataset whose
    discovery fails is reported on one line and the exit code is 1."""
    from fuellake import pipeline
    from fuellake.config import redact
    from fuellake.registry import load_aoi, load_dataset

    period = _period(start, end)
    try:
        aoi_cfg = load_aoi(aoi)
    except (KeyError, ValueError) as exc:
        raise _fail(str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)) from None
    failed = False
    for dataset_id in _expand(dataset_ids or []):
        if _is_all(dataset_ids) and load_dataset(dataset_id).status == "stub":
            typer.echo(f"{dataset_id}: skipped: stub")
            continue
        if _is_all(dataset_ids) and not load_dataset(dataset_id).supports(aoi_cfg):
            typer.echo(f"{dataset_id}: skipped: {pipeline.scale_mismatch(load_dataset(dataset_id), aoi_cfg)}")
            continue
        try:
            refs = pipeline.discover([dataset_id], aoi, period)[dataset_id]
        except Exception as exc:  # report and go on with the other datasets
            failed = True
            typer.echo(f"{dataset_id}: error: {redact(f'{type(exc).__name__}: {exc}').splitlines()[0]}")
            continue
        typer.echo(f"{dataset_id}: {len(refs)} units ({sum(1 for _, k in refs if k)} already cataloged)")
        for ref, known in refs:
            typer.echo(f"  {'=' if known else '+'} {ref.key}  {ref.datetime:%Y-%m-%dT%H:%MZ}")
    raise typer.Exit(1 if failed else 0)


@app.command()
def run(
    dataset_ids: list[str] = typer.Argument(None, help="Dataset ids, or 'all' (datasets with a connector)."),
    aoi: str = typer.Option("smm", help="AOI id."),
    start: str = typer.Option(None, help="YYYY-MM-DD (default: end - 30 days)."),
    end: str = typer.Option(None, help="YYYY-MM-DD (default: today)."),
    force: bool = typer.Option(False, help="Reprocess units that are already cataloged."),
    limit: int = typer.Option(None, help="Max units actually processed per dataset (cataloged, cached-empty "
                                         "and credential-skipped units do not count)."),
    force_scale: bool = typer.Option(False, "--force-scale",
                                     help="Run datasets whose 'scales' do not include the AOI's scale (e.g. a "
                                          "local-only 30 m dataset on the statewide AOI ca)."),
    as_json: bool = typer.Option(False, "--json", help="Print the full run report as JSON."),
) -> None:
    """Run the ETL pipeline. Exit code 1 if any unit failed or was invalid.

    With no dataset ids or 'all': every dataset with a connector, except that stub datasets are skipped,
    datasets that do not support the AOI's scale are skipped, and datasets / units whose credentials
    (EARTHDATA_TOKEN, FIRMS_MAP_KEY, CDSAPI_URL/CDSAPI_KEY) are not set are reported as "skipped: missing
    credentials" without failing the run. Datasets named explicitly always run and fail loudly (a scale
    mismatch is refused unless --force-scale). Every output is clipped to the lake region."""
    from fuellake import pipeline

    lenient = _is_all(dataset_ids)
    try:
        report = pipeline.run(_expand(dataset_ids or []), aoi, _period(start, end), force=force, limit=limit,
                              skip_stubs=lenient, skip_missing_credentials=lenient, force_scale=force_scale)
    except (KeyError, ValueError) as exc:          # unknown AOI, AOI outside the lake region
        raise _fail(str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)) from None
    if as_json:
        typer.echo(json.dumps(report.to_dict(), indent=2))
    else:
        typer.echo(f"run {report.run_id} ({'ok' if report.ok else 'FAILED'}): {report.counts()}")
        for d in report.datasets:
            if d.status == "skipped" and not d.units:
                typer.echo(f"  {d.dataset_id}: {d.reason}")
                continue
            typer.echo(f"  {d.dataset_id}: {d.status}; discovered {d.discovered}, skipped {d.skipped}, "
                       f"created {d.count('created')}, updated {d.count('updated')}, empty {d.count('empty')}, "
                       f"invalid {d.count('invalid')}, failed {d.count('failed')}")
            if d.reason:
                typer.echo(f"    {d.reason}")
            if d.error:
                typer.echo(f"    error: {d.error.splitlines()[0]}")
            for u in d.units:
                if u.error:
                    typer.echo(f"    {u.status} {u.key}: {u.error.splitlines()[0]}")
        typer.echo(f"manifest: {report.manifest_path}")
    raise typer.Exit(0 if report.ok else 1)


@app.command()
def validate(
    dataset_ids: list[str] = typer.Argument(None, help="Dataset ids (default: all registered)."),
    as_json: bool = typer.Option(False, "--json", help="Print all results as JSON."),
) -> None:
    """Validate every cataloged item against the data contracts and the lake region (item bboxes, raster
    pixels and table rows inside it). Exit code 1 on any error."""
    from fuellake.contracts import validate_collection
    from fuellake.registry import list_datasets, load_dataset
    from fuellake.storage import Lake

    lake = Lake.from_env()
    datasets = [load_dataset(i) for i in dataset_ids] if dataset_ids else list_datasets()
    results = []
    for ds in datasets:
        results.extend(validate_collection(lake, ds))
    n_err = sum(len(r.errors) for r in results)
    n_warn = sum(len(r.warnings) for r in results)
    if as_json:
        typer.echo(json.dumps([r.to_dict() for r in results], indent=2, default=str))
    else:
        for r in results:
            if r.check == "collection":
                typer.echo(f"{r.target}: {r.details.get('n_items', 0)} items")
            for e in r.errors:
                typer.echo(f"  ERROR {r.target}: {e}")
            for w in r.warnings:
                typer.echo(f"  warn  {r.target}: {w}")
        typer.echo(f"{'OK' if n_err == 0 else 'FAILED'}: {n_err} errors, {n_warn} warnings")
    raise typer.Exit(0 if n_err == 0 else 1)


def _int_list(value: str, name: str) -> tuple[int, ...]:
    try:
        return tuple(int(v) for v in value.replace(" ", "").split(",") if v != "")
    except ValueError as exc:
        raise typer.BadParameter(f"--{name} must be comma-separated integers, got {value!r}") from exc


@app.command("build-features")
def build_features(
    aoi: str = typer.Option("smm", help="AOI id."),
    start: str = typer.Option(None, help="YYYY-MM-DD (default: end - 30 days)."),
    end: str = typer.Option(None, help="YYYY-MM-DD (default: today)."),
    what: str = typer.Option("all", help="cube | matchups | all"),
    resolution: float = typer.Option(None, help="Cube resolution in metres (default: AOI resolution; coarser "
                                                "only)."),
    maturity: str = typer.Option(None, help="Keep only NISAR items of this maturity (BETA | PROVISIONAL | "
                                            "VALIDATED); items without one are kept."),
    allow_mixed_maturity: bool = typer.Option(False, "--allow-mixed-maturity",
                                              help="Accept a NISAR series that mixes maturities."),
    input_ids: list[str] = typer.Option(None, "--input", help="Restrict raster inputs to this dataset id "
                                                              "(repeatable; default: every raster dataset)."),
    causal: bool = typer.Option(True, "--causal/--no-causal",
                                help="Matchups: use only scenes on or before the sample date."),
    window_days: int = typer.Option(6, help="Matchups: max |scene date - target date| in days."),
    lags_days: str = typer.Option("0,15,30,45,60,90", help="Matchups: comma-separated lags in days."),
    sample_window_px: int = typer.Option(3, help="Matchups: odd k for a k x k pixel window (1 = point)."),
    nearest_valid: bool = typer.Option(True, "--nearest-valid/--nearest-any",
                                       help="Matchups: only scenes with a valid value at the site count."),
    lag_variables: str = typer.Option(None, "--lag-variables",
                                      help="Matchups: comma-separated variables to lag, or 'none' (default: "
                                           "soil_moisture, swvl1 and every variable flagged 'lag: true' in the "
                                           "dataset configs)."),
    weather_cadence_days: int = typer.Option(None, "--weather-cadence-days",
                                             help="Cube without form A scene dates: one weather time step every N "
                                                  "days (default: the features-cube option, 1 = daily)."),
) -> None:
    """Build the gold datacube and/or the label x feature matchup table.

    Errors (mixed NISAR maturities, no inputs, bad parameters) are printed on one line; exit code 1."""
    from fuellake.contracts import MixedMaturityError

    if what not in ("cube", "matchups", "all"):
        raise typer.BadParameter(f"--what must be cube, matchups or all, got {what!r}")
    period = _period(start, end)
    lags = _int_list(lags_days, "lags-days")
    lag_kw: dict = {}
    if lag_variables is not None:
        names = [v.strip() for v in lag_variables.split(",") if v.strip()]
        lag_kw["lag_variables"] = () if [n.lower() for n in names] in ([], ["none"]) else tuple(names)
    cube_kw: dict = {}
    if weather_cadence_days is not None:
        if weather_cadence_days < 1:
            raise typer.BadParameter("--weather-cadence-days must be >= 1")
        cube_kw["weather_cadence_days"] = weather_cadence_days
    common = dict(datasets=list(input_ids or []) or None, allow_mixed_maturity=allow_mixed_maturity,
                  maturity=maturity.upper() if maturity else None)
    try:
        if what in ("cube", "all"):
            from fuellake.features.cube import build_cube

            typer.echo(build_cube(aoi, period.start, period.end, resolution=resolution, **cube_kw, **common))
        if what in ("matchups", "all"):
            from fuellake.features.matchups import build_matchups

            typer.echo(build_matchups(aoi, period.start, period.end, window_days=window_days, lags_days=lags,
                                      causal=causal, sample_window_px=sample_window_px,
                                      nearest_valid=nearest_valid, **lag_kw, **common))
    except NotImplementedError as exc:
        typer.echo(f"not implemented: {exc}", err=True)
        raise typer.Exit(2) from exc
    except MixedMaturityError as exc:
        detail = str(exc).split("; pass allow_mixed", 1)[0]
        raise _fail(f"{detail}; choose one with --maturity (e.g. PROVISIONAL) or pass --allow-mixed-maturity") \
            from None
    except (LookupError, ValueError) as exc:   # no inputs, unknown AOI / dataset, bad parameters
        raise _fail(str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)) from None


@app.command("build-points")
def build_points(
    aoi: str = typer.Option("ca", help="AOI id, or the lake region id (mapped to the region-scale AOI)."),
    start: str = typer.Option(None, help="YYYY-MM-DD (default: end - 30 days)."),
    end: str = typer.Option(None, help="YYYY-MM-DD (default: today)."),
    tables: str = typer.Option("fire_ignitions,lfmc_samples", help="Comma-separated point tables."),
    causal: bool = typer.Option(True, "--causal/--no-causal",
                                help="Use nothing dated after the point's date (scenes, releases)."),
    window_days: int = typer.Option(None, help="Dynamic scenes (NISAR SME2): max |scene date - point date| "
                                               "(default: the point-features option, 12)."),
    lags_days: str = typer.Option("0,15,30,45,60,90", help="Comma-separated lags in days."),
    lag_variables: str = typer.Option(None, "--lag-variables",
                                      help="Comma-separated variables to lag, or 'none' (default: soil_moisture, "
                                           "swvl1 and every variable flagged 'lag: true')."),
    input_ids: list[str] = typer.Option(None, "--input", help="Restrict form B and scene inputs to this dataset id "
                                                              "(repeatable)."),
    maturity: str = typer.Option(None, help="Keep only NISAR items of this maturity."),
    allow_mixed_maturity: bool = typer.Option(False, "--allow-mixed-maturity",
                                              help="Accept a NISAR series that mixes maturities."),
    statics: bool = typer.Option(True, "--statics/--no-statics", help="Static layers (fuels, canopy, terrain, "
                                                                       "GEDI)."),
    remote: bool = typer.Option(True, "--remote/--no-remote",
                                help="Read statics remotely for points outside every staged AOI."),
    sentinel2: bool = typer.Option(True, "--sentinel2/--no-sentinel2", help="Sentinel-2 NDVI/NDMI per point."),
    s2_window_days: int = typer.Option(None, help="Sentinel-2 search window in days (default 8)."),
    s2_max_points: int = typer.Option(None, help="Cap the Sentinel-2 point-dates: all large fires plus a "
                                                 "stratified random sample of the rest."),
    seed: int = typer.Option(0, help="Seed of the Sentinel-2 sample."),
    large_fire_ha: float = typer.Option(None, help="Large-fire threshold in ha (default 400)."),
) -> None:
    """Build the statewide point-feature table features/<aoi>/point_features.parquet (gold collection
    point-features): one row per fire ignition and LFMC sample, features sampled at the point on native grids.

    Errors (no such AOI or table spec, mixed NISAR maturities, bad parameters) are printed on one line; exit 1."""
    from fuellake.contracts import MixedMaturityError

    period = _period(start, end)
    lags = _int_list(lags_days, "lags-days")
    kw: dict = {}
    if lag_variables is not None:
        names = [v.strip() for v in lag_variables.split(",") if v.strip()]
        kw["lag_variables"] = () if [n.lower() for n in names] in ([], ["none"]) else tuple(names)
    table_ids = tuple(t.strip() for t in tables.split(",") if t.strip())
    try:
        from fuellake.features.points import build_point_features

        typer.echo(build_point_features(
            aoi, period.start, period.end, tables=table_ids, lags_days=lags, causal=causal, window_days=window_days,
            datasets=list(input_ids or []) or None, maturity=maturity.upper() if maturity else None,
            allow_mixed_maturity=allow_mixed_maturity, statics=statics, remote=remote, sentinel2=sentinel2,
            s2_window_days=s2_window_days, s2_max_points=s2_max_points, seed=seed, large_fire_ha=large_fire_ha,
            **kw))
    except MixedMaturityError as exc:
        detail = str(exc).split("; pass allow_mixed", 1)[0]
        raise _fail(f"{detail}; choose one with --maturity (e.g. PROVISIONAL) or pass --allow-mixed-maturity") \
            from None
    except (LookupError, ValueError) as exc:   # unknown AOI / table, bad parameters
        raise _fail(str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)) from None


@app.command()
def licenses(markdown: bool = typer.Option(False, "--markdown", help="Print a Markdown table.")) -> None:
    """Licence and redistribution terms of every dataset (reads config only)."""
    from fuellake.registry import list_datasets

    rows = list_datasets()
    if markdown:
        typer.echo("| Dataset | Provider | Licence | Redistributable | Attribution | Status | Notes |")
        typer.echo("|---|---|---|---|---|---|---|")
        for d in rows:
            lic = d.license
            name = f"[{lic.id}]({lic.url})" if lic.url else lic.id
            cells = [f"`{d.id}`", d.provider, name, "yes" if lic.redistributable else "no", lic.attribution,
                     d.status, lic.notes]
            typer.echo("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
        return
    for d in rows:
        typer.echo(f"{d.id}: {d.license.id} (redistributable: {'yes' if d.license.redistributable else 'no'})"
                   f" {d.license.url or ''}")


@app.command("events")
def events_cmd(
    since: str = typer.Option(None, help="ISO date or datetime (inclusive); default: all events."),
    type_: list[str] = typer.Option(None, "--type", help="Filter by event type (repeatable)."),
) -> None:
    """Print events (JSON lines) from the lake event log."""
    from fuellake.events import read_events
    from fuellake.storage import Lake

    for ev in read_events(Lake.from_env(), since=since, types=type_ or None):
        typer.echo(json.dumps(ev))


@app.command()
def serve(host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8000)) -> None:
    """Serve the read-only HTTP API (fuellake.services.api:app) with uvicorn."""
    import uvicorn

    uvicorn.run("fuellake.services.api:app", host=host, port=port)


if __name__ == "__main__":
    app()
