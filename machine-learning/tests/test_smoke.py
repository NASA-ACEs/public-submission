import json
import subprocess
import sys

import lightgbm as lgb
import numpy as np
import pandas as pd

from ml import data


def table(n=4000, seed=0):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({c: rng.random(n) for c in data.WEATHER + data.SOIL + data.STATIC[:-2] + data.SERIES_ALL})
    df["years_since_fire"] = np.where(rng.random(n) < 0.1, rng.uniform(0, 10, n), 10.0)
    df["aspect"] = rng.uniform(-1, 360, n)
    df["date"] = pd.to_datetime("2016-01-01") + pd.to_timedelta(rng.integers(0, 3285, n), "D")
    df["block_50km"] = [f"r{i:03d}c000" for i in rng.integers(0, 40, n)]
    df["point_id"] = np.arange(n).astype(str)
    lfmc = np.arange(n) < n // 2
    df["point_type"] = np.where(lfmc, "lfmc_sample", "fire_ignition")
    df["source"] = np.where(lfmc, np.where(df["date"].dt.year <= 2023, "globe-lfmc", "fems"), "fpa-fod")
    df["site_id"] = np.where(lfmc, rng.integers(0, 60, n).astype(str), None)
    df["lfmc_pct"] = np.where(lfmc, 150 - 80 * df["vpd"] + 60 * df["sm_surface_lag60d"] + rng.normal(0, 5, n), np.nan)
    df["outlier_flag"] = np.where(lfmc, False, None)
    df["qc_flags"] = np.where(lfmc, "", None)
    df["fire_year"] = df["date"].dt.year
    df["large_fire"] = pd.array(np.where(lfmc, None, rng.random(n) < 0.3 * df["vpd"] * (df["years_since_fire"] >= 10)), dtype="boolean")
    return df


def test_train_and_reload(tmp_path):
    table().to_parquet(tmp_path / "points.parquet")
    subprocess.run([sys.executable, "-m", "ml.train", "--table", str(tmp_path / "points.parquet"),
                    "--out", str(tmp_path / "models")], check=True)
    for name in ("core", "full"):
        d = tmp_path / "models" / name
        meta, metrics = json.loads((d / "meta.json").read_text()), json.loads((d / "metrics.json").read_text())
        lfmc, fire = metrics["lfmc"], metrics["fire"]["unseen_year"]
        assert lfmc["unseen_block_and_year_rmse"] < lfmc["baseline_mean_rmse"]
        assert lfmc["soil_lag_best"]["lag"] == "60" and lfmc["soil_lag_curve"]["60"]["vs_weather_only"]["ci95"][0] > 0
        assert fire["model"]["pr_auc"] > metrics["fire"]["base_rate"]
        assert {"dryness", "antecedent", "weather_to_model", "lfmc_hat", "model_vs_bi"} <= set(fire["pr_auc_gain"])
        assert metrics["fire"]["fuel_age"]["beta"] > 1 and meta["fuel_age_beta"] == metrics["fire"]["fuel_age"]["beta"]
        assert "lfmc_hat" not in meta["fire_features"] and meta["monotone"]["vpd"] == 1 and meta["monotone"]["rz_z12"] == -1
        rows = data.add_derived(table(50, 1))
        lfmc = lgb.Booster(model_file=str(d / "lfmc_q50.txt")).predict(data.X(rows, meta["lfmc_features"]))
        p = [lgb.Booster(model_file=str(f)).predict(data.X(rows, meta["fire_features"])) for f in sorted(d.glob("fire_*.txt"))]
        assert len(p) == 5 and np.isfinite(lfmc).all() and ((np.array(p) >= 0) & (np.array(p) <= 1)).all()
    assert "elevation" in meta["lfmc_features"] and "northness" in meta["fire_features"] and "terrain_and_fuel" in fire["pr_auc_gain"]
