# fuel-lake — example processing microservice: exactly-once processing across runs, direct and over HTTP
# Licensed under Apache-2.0.
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fixtures.services.demo_lake import SME2, add_sme2_item, expected_sme2_stats, serve
from fuellake.client import LakeClient
from fuellake.services import example_consumer
from fuellake.services.api import create_app
from fuellake.services.example_consumer import main, run_once


def _results(out: Path) -> pd.DataFrame:
    return pd.read_parquet(out / "aoi_stats.parquet").set_index("item_id")


def test_processes_each_item_version_exactly_once(lake, tmp_path):
    out = tmp_path / "out"
    client = LakeClient(lake)
    for day in (date(2026, 6, 5), date(2026, 6, 17)):
        add_sme2_item(lake, day)
    add_sme2_item(lake, date(2026, 6, 6), collection="nisar-other")          # other collection: ignored

    first = run_once(client, SME2, out)
    mean, valid_fraction = expected_sme2_stats()
    assert [r["item_id"] for r in first] == [f"{SME2}__tiny__20260605", f"{SME2}__tiny__20260617"]
    assert all(r["mean"] == pytest.approx(mean) and r["valid_fraction"] == pytest.approx(valid_fraction)
               for r in first)
    assert first[0]["asset"] == "soil_moisture" and first[0]["maturity"] == "PROVISIONAL"
    assert run_once(client, SME2, out) == []                                  # second run: nothing new

    # a new item (other maturity: per-item stats are not a series, so it is accepted and recorded)
    add_sme2_item(lake, date(2026, 6, 29), maturity="VALIDATED", nan_rows=10)
    third = run_once(client, SME2, out)
    assert [r["item_id"] for r in third] == [f"{SME2}__tiny__20260629"] and third[0]["maturity"] == "VALIDATED"
    assert third[0]["valid_fraction"] == pytest.approx(expected_sme2_stats(nan_rows=10)[1])
    assert run_once(client, SME2, out) == []

    # an updated item (new run_id) is processed once more and its row replaced, not duplicated
    add_sme2_item(lake, date(2026, 6, 5), value=0.5, run_id="demo-run-2")
    updated = run_once(client, SME2, out)
    assert [(r["item_id"], r["mean"]) for r in updated] == [(f"{SME2}__tiny__20260605", pytest.approx(0.5))]
    assert run_once(client, SME2, out) == []
    df = _results(out)
    assert len(df) == 3 and df.loc[f"{SME2}__tiny__20260605", "mean"] == pytest.approx(0.5)
    assert df.loc[f"{SME2}__tiny__20260605", "version"] == "demo-run-2"

    state = json.loads((out / "checkpoint.json").read_text())
    assert state["done"] == {f"{SME2}__tiny__20260605": "demo-run-2", f"{SME2}__tiny__20260617": "demo-run-1",
                             f"{SME2}__tiny__20260629": "demo-run-1"}
    assert state["cursor"] == client.events(types=["item.created", "item.updated"])[-1]["time"]
    assert not any(p.name.startswith("catalog") for p in out.iterdir()), "output stays out of the lake"


def test_all_nan_item_gives_null_mean(lake, tmp_path):
    add_sme2_item(lake, date(2026, 6, 5), nan_rows=10_000)
    (row,) = run_once(LakeClient(lake), SME2, tmp_path / "out")
    assert row["mean"] is None and row["valid_fraction"] == 0.0
    assert _results(tmp_path / "out")["mean"].isna().all()


def test_item_without_run_id_is_processed_once(lake, tmp_path):
    from fixtures.services.demo_lake import add_cog_item, tiny_grid

    grid = tiny_grid()
    add_cog_item(lake, SME2, f"{SME2}__tiny__20260605", datetime(2026, 6, 5, tzinfo=UTC),
                 np.full(grid.shape, 0.3, "float32"), grid, properties={"fuellake:run_id": None})
    client = LakeClient(lake)
    assert [r["version"] for r in run_once(client, SME2, tmp_path / "out")] == [None]
    assert run_once(client, SME2, tmp_path / "out") == []


def test_main_over_http_and_from_env(lake, tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    add_sme2_item(lake, date(2026, 6, 5))
    with serve(create_app(lake)) as url:
        main(["--once", "--api-url", url, "--out", str(out)])
        assert "nisar-sme2: processed 1 item(s)" in capsys.readouterr().out
        add_sme2_item(lake, date(2026, 6, 17))
        monkeypatch.setenv("FUELLAKE_API_URL", url)                        # configured only by environment
        monkeypatch.setenv("FUELLAKE_CONSUMER_OUT", str(out))
        main(["--once"])
        assert "processed 1 item(s)" in capsys.readouterr().out
        main(["--once"])
        assert "processed 0 item(s)" in capsys.readouterr().out
    assert sorted(_results(out).index) == [f"{SME2}__tiny__20260605", f"{SME2}__tiny__20260617"]


def test_consumer_stays_small():
    lines = Path(example_consumer.__file__).read_text().splitlines()
    assert len(lines) <= 120, f"example consumer has {len(lines)} lines; keep it a short example"
