import argparse
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from fuellake.client import LakeClient
from fuellake.registry import load_aoi

from . import data, grid
from .data import X
from .train import boot_ci, rmse

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", required=True, help="point table with 2026 LFMC field samples")
    ap.add_argument("--models", default="models/core")
    ap.add_argument("--aoi", default="ca")
    ap.add_argument("--max-dates", type=int, default=5)
    args = ap.parse_args()
    models, aoi, client = Path(args.models), load_aoi(args.aoi), LakeClient.from_env()
    feats = json.loads((models / "meta.json").read_text())["lfmc_features"]
    lf = data.add_derived(data.lfmc_rows(data.load_points(args.table)))
    row, col, inside = grid.cells(aoi.grid, lf["lon"], lf["lat"])
    delta = np.full(len(lf), np.nan)
    for d in lf["date"].value_counts().index[:args.max_dates]:
        day = pd.Timestamp(d).date()
        res = grid.nisar_fine(client, aoi, day)
        i = (lf["date"] == d).to_numpy() & inside
        if res is not None:
            delta[i] = res["fine"][row[i], col[i]]
    keep = np.isfinite(delta) & lf["sm_surface"].notna().to_numpy()
    lf, delta = lf[keep].reset_index(drop=True), delta[keep]
    if len(lf) < 30:
        raise SystemExit(f"only {len(lf)} field samples with a NISAR term: stage more nisar-sme2 or raise --max-dates")
    nisar = lf.assign(sm_surface=np.clip(lf["sm_surface"].astype(float) + delta, 0, 0.9))
    model, y = lgb.Booster(model_file=str(models / "lfmc_q50.txt")), lf["lfmc_pct"].to_numpy()
    p = {"smap": model.predict(X(lf, feats)), "nisar": model.predict(X(nisar, feats))}
    out = {"n": len(y), "sites": int(lf["site"].nunique()), "dates": int(lf["date"].nunique()),
           "mean_abs_soil_moisture_change": float(np.abs(delta).mean()),
           "mean_abs_lfmc_change_pct": float(np.abs(p["nisar"] - p["smap"]).mean()),
           "rmse_nisar_minus_smap": rmse(y, p["nisar"]) - rmse(y, p["smap"]),
           "rmse_diff_ci95": boot_ci(lf["site"].to_numpy(), lambda i: rmse(y[i], p["nisar"][i]) - rmse(y[i], p["smap"][i])),
           "note": "same NISAR step as the map, same-day soil moisture only; sites were seen in training, the year was "
                   "not; LFMC only (no comparable 2026 fire labels)"}
    for k, q in p.items():
        out[k] = dict(rmse=rmse(y, q), mae=float(np.abs(y - q).mean()), bias=float((q - y).mean()), r=float(np.corrcoef(y, q)[0, 1]))
    (models / "nisar_check.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
