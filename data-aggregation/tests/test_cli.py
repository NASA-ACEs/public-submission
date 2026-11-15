# fuel-lake — CLI smoke tests (typer CliRunner)
# Licensed under Apache-2.0.
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from fuellake.cli import app

runner = CliRunner()
REAL_DATASETS = Path(__file__).resolve().parents[1] / "config" / "datasets"


def test_datasets_and_licenses_on_real_config():
    n = len(list(REAL_DATASETS.glob("*.yaml")))     # derived: new dataset configs must not break this test
    assert n >= 15
    res = runner.invoke(app, ["datasets"])
    assert res.exit_code == 0, res.output
    lines = [line for line in res.output.splitlines()[2:] if line.strip()]
    assert len(lines) == n
    assert all(any(s in line for s in ("tested", "search-only", "stub", "unverified", "interface"))
               for line in lines)
    res = runner.invoke(app, ["licenses", "--markdown"])
    assert res.exit_code == 0
    rows = [line for line in res.output.splitlines() if line.startswith("| `")]
    assert len(rows) == n and res.output.startswith("| Dataset | Provider | Licence |")
    assert "CC0-1.0" in res.output
    res = runner.invoke(app, ["datasets", "--json"])
    assert len(json.loads(res.output)) == n


def test_init_run_validate_events(test_config, lake):
    res = runner.invoke(app, ["init"])
    assert res.exit_code == 0, res.output
    assert lake.exists("catalog/catalog.json") and lake.exists("catalog/synthetic-raster/collection.json")
    assert lake.isdir("inbox/biomass")
    res = runner.invoke(app, ["run", "synthetic-raster", "synthetic-table", "--aoi", "tiny",
                              "--start", "2026-06-01", "--end", "2026-06-30"])
    assert res.exit_code == 0, res.output
    assert "created 1" in res.output
    res = runner.invoke(app, ["validate", "synthetic-raster", "synthetic-table"])
    assert res.exit_code == 0, res.output
    assert "OK: 0 errors" in res.output
    res = runner.invoke(app, ["events", "--since", "2000-01-01"])
    assert res.exit_code == 0
    types = [json.loads(line)["type"] for line in res.output.splitlines()]
    assert types[-1] == "run.completed" and "item.created" in types
    res = runner.invoke(app, ["discover", "synthetic-raster", "--aoi", "tiny", "--start", "2026-06-01",
                              "--end", "2026-06-30"])
    assert res.exit_code == 0 and "1 already cataloged" in res.output


def test_run_failure_exit_code_and_bad_date(test_config, lake):
    res = runner.invoke(app, ["run", "synthetic-badcrs", "--aoi", "tiny", "--start", "2026-06-01",
                              "--end", "2026-06-02"])
    assert res.exit_code == 1 and "invalid" in res.output
    res = runner.invoke(app, ["run", "synthetic-raster", "--aoi", "tiny", "--start", "June"])
    assert res.exit_code != 0


def test_run_all_skips_stubs_and_missing_credentials(test_config, lake, monkeypatch):
    import yaml

    from conftest import SYNTHETIC_DATASETS

    for name in ("synthetic-flaky", "synthetic-badcrs", "synthetic-unmasked"):    # keep only healthy datasets
        (test_config / "datasets" / f"{name}.yaml").unlink()
    base = SYNTHETIC_DATASETS["synthetic-raster"]
    extra = {
        "synthetic-earthdata": {**base, "id": "synthetic-earthdata",
                                "access": {"method": "none", "auth": "earthdata", "env": ["EARTHDATA_TOKEN"]}},
        "synthetic-stubbed": {**base, "id": "synthetic-stubbed", "status": "stub",
                              "options": {"fail_keys": ["synthetic-stubbed-20260601"]}},
    }
    for ds_id, cfg in extra.items():
        (test_config / "datasets" / f"{ds_id}.yaml").write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("FIRMS_MAP_KEY", "firms-key-must-not-be-printed")
    period = ["--aoi", "tiny", "--start", "2026-06-01", "--end", "2026-06-30"]
    for ids in ([], ["all"]):
        res = runner.invoke(app, ["run", *ids, *period])
        assert res.exit_code == 0, res.output
        assert "synthetic-stubbed: skipped: stub" in res.output
        assert "skipped: missing credentials (EARTHDATA_TOKEN) for 1 unit(s)" in res.output
        assert "firms-key-must-not-be-printed" not in res.output
    res = runner.invoke(app, ["run", "synthetic-earthdata", *period])          # named: fails loudly
    assert res.exit_code == 1 and "EARTHDATA_TOKEN is not set" in res.output
    res = runner.invoke(app, ["discover", "all", *period])
    assert res.exit_code == 0 and "synthetic-stubbed: skipped: stub" in res.output


def test_build_features_calls_the_feature_layer(test_config, monkeypatch):
    from datetime import date

    import fuellake.features.cube as cube
    import fuellake.features.matchups as matchups

    calls = []
    monkeypatch.setattr(cube, "build_cube",
                        lambda aoi, start, end, **kw: calls.append(("cube", aoi, start, end, kw))
                        or f"features/{aoi}/cube.zarr")
    monkeypatch.setattr(matchups, "build_matchups",
                        lambda aoi, start, end, **kw: calls.append(("matchups", aoi, start, end, kw))
                        or f"features/{aoi}/matchups.parquet")
    res = runner.invoke(app, ["build-features", "--aoi", "tiny", "--start", "2026-06-01", "--end", "2026-06-30",
                              "--resolution", "100"])
    assert res.exit_code == 0, res.output
    assert "features/tiny/cube.zarr" in res.output and "features/tiny/matchups.parquet" in res.output
    common = {"datasets": None, "allow_mixed_maturity": False, "maturity": None}
    assert calls[0] == ("cube", "tiny", date(2026, 6, 1), date(2026, 6, 30), {"resolution": 100.0, **common})
    assert calls[1] == ("matchups", "tiny", date(2026, 6, 1), date(2026, 6, 30),
                        {"window_days": 6, "lags_days": (0, 15, 30, 45, 60, 90), "causal": True,
                         "sample_window_px": 3, "nearest_valid": True, **common})
    calls.clear()
    res = runner.invoke(app, ["build-features", "--what", "matchups", "--aoi", "tiny", "--start", "2026-06-01",
                              "--end", "2026-06-30", "--no-causal", "--window-days", "3", "--lags-days", "0, 30",
                              "--sample-window-px", "1", "--nearest-any", "--maturity", "validated",
                              "--allow-mixed-maturity", "--input", "s2-test", "--input", "nisar-sm"])
    assert res.exit_code == 0, res.output
    assert calls == [("matchups", "tiny", date(2026, 6, 1), date(2026, 6, 30),
                      {"window_days": 3, "lags_days": (0, 30), "causal": False, "sample_window_px": 1,
                       "nearest_valid": False, "datasets": ["s2-test", "nisar-sm"], "allow_mixed_maturity": True,
                       "maturity": "VALIDATED"})]
    res = runner.invoke(app, ["build-features", "--aoi", "tiny", "--lags-days", "a,b"])
    assert res.exit_code != 0
    calls.clear()
    res = runner.invoke(app, ["build-features", "--aoi", "tiny", "--start", "2026-06-01", "--end", "2026-06-30",
                              "--weather-cadence-days", "3", "--lag-variables", "sm_rootzone, swvl1"])
    assert res.exit_code == 0, res.output
    assert calls[0][4]["weather_cadence_days"] == 3 and calls[1][4]["lag_variables"] == ("sm_rootzone", "swvl1")
    assert runner.invoke(app, ["build-features", "--aoi", "tiny", "--weather-cadence-days", "0"]).exit_code != 0


def test_build_features_errors_are_one_line(test_config, monkeypatch):
    import fuellake.features.cube as cube
    from fuellake.contracts import MixedMaturityError

    def mixed(*a, **kw):
        raise MixedMaturityError("time series mixes maturities ['PROVISIONAL', 'VALIDATED']; pass allow_mixed=True "
                                 "to accept")

    monkeypatch.setattr(cube, "build_cube", mixed)
    res = runner.invoke(app, ["build-features", "--what", "cube", "--aoi", "tiny", "--start", "2026-06-01"])
    assert res.exit_code == 1, res.output
    assert "Traceback" not in res.output
    lines = [line for line in res.output.splitlines() if line.strip()]
    assert len(lines) == 1 and lines[0].startswith("error: time series mixes maturities")
    assert "--allow-mixed-maturity" in lines[0]
    monkeypatch.setattr(cube, "build_cube", lambda *a, **kw: (_ for _ in ()).throw(LookupError("no cube inputs")))
    res = runner.invoke(app, ["build-features", "--what", "cube", "--aoi", "tiny", "--start", "2026-06-01"])
    assert res.exit_code == 1 and res.output.strip() == "error: no cube inputs"


def test_build_features_not_implemented_exit_code(test_config, monkeypatch):
    import fuellake.features.cube as cube

    def not_yet(*a, **kw):
        raise NotImplementedError("pending")

    monkeypatch.setattr(cube, "build_cube", not_yet)
    res = runner.invoke(app, ["build-features", "--what", "cube", "--aoi", "tiny", "--start", "2026-06-01"])
    assert res.exit_code == 2 and "not implemented" in res.output


def test_api_health():
    from fastapi.testclient import TestClient

    from fuellake.services.api import app as api

    assert TestClient(api).get("/health").json()["status"] == "ok"
