import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

from .train import boot_ci, rmse


def top_share(y, p, share=0.1):
    return float(y[np.argsort(-p)[:int(len(p) * share)]].sum() / max(y.sum(), 1))


def fire(a, b):
    m = pd.read_parquet(a / "oof_fire.parquet").merge(pd.read_parquet(b / "oof_fire.parquet")[["point_id", "p"]],
                                                      on="point_id", suffixes=("_a", "_b"))
    y, pa, pb = m["large_fire"].astype(int).to_numpy(), m["p_a"].to_numpy(), m["p_b"].to_numpy()
    week = pd.to_datetime(m["date"]).dt.strftime("%G-%V").to_numpy()

    def ap(p, i):
        return average_precision_score(y[i], p[i]) if y[i].any() else np.nan

    every = np.arange(len(y))
    return dict(n=len(y), positives=int(y.sum()),
                pr_auc=[float(ap(pa, every)), float(ap(pb, every))], roc_auc=[float(roc_auc_score(y, pa)), float(roc_auc_score(y, pb))],
                top10_share=[top_share(y, pa), top_share(y, pb)],
                pr_auc_gain=float(ap(pb, every) - ap(pa, every)), pr_auc_gain_ci95=boot_ci(week, lambda i: ap(pb, i) - ap(pa, i)))


def lfmc(a, b):
    m = pd.read_parquet(a / "oof_lfmc.parquet").merge(pd.read_parquet(b / "oof_lfmc.parquet")[["site", "date", "p50"]],
                                                      on=["site", "date"], suffixes=("_a", "_b"))
    y, pa, pb = m["lfmc_pct"].to_numpy(), m["p50_a"].to_numpy(), m["p50_b"].to_numpy()
    return dict(n=len(y), rmse=[rmse(y, pa), rmse(y, pb)], rmse_change=rmse(y, pb) - rmse(y, pa),
                rmse_change_ci95=boot_ci(m["block_50km"].to_numpy(), lambda i: rmse(y[i], pb[i]) - rmse(y[i], pa[i])))


def headline(d):
    m = json.loads((d / "metrics.json").read_text())
    f = m["fire"]["unseen_year"]
    model = f.get("model") or f["with_lfmc"]            # the first version's model had lfmc_hat as an input
    return {"lfmc_rmse_new_place_and_year": m["lfmc"]["unseen_block_and_year_rmse"], "lfmc_rmse_new_block": m["lfmc"]["unseen_block"]["rmse"],
            "fire_pr_auc_new_year": model["pr_auc"], "fire_roc_auc_new_year": model["roc_auc"],
            "fire_pr_auc_with_fuel_age": m["fire"].get("fuel_age", {}).get("pr_auc"),
            "fire_pr_auc_bi": f["bi"]["pr_auc"], "fire_gain_over_bi_ci95": f["pr_auc_gain"]["model_vs_bi"]["ci95"],
            "fire_seed_noise": f["pr_auc_seed_noise"]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="regression test: candidate model directory against a reference one")
    ap.add_argument("reference")
    ap.add_argument("candidate")
    ap.add_argument("--out")
    args = ap.parse_args()
    a, b = Path(args.reference), Path(args.candidate)
    out = {"reference": str(a), "candidate": str(b), "headline": {"reference": headline(a), "candidate": headline(b)},
           "fire_new_year_paired": fire(a, b), "lfmc_new_block_paired": lfmc(a, b)}
    f, l = out["fire_new_year_paired"], out["lfmc_new_block_paired"]
    out["verdict"] = {"fire": "better" if f["pr_auc_gain_ci95"][0] > 0 else "worse" if f["pr_auc_gain_ci95"][1] < 0 else "no difference",
                      "lfmc": "better" if l["rmse_change_ci95"][1] < 0 else "worse" if l["rmse_change_ci95"][0] > 0 else "no difference"}
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
