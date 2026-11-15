import os
# model v2 (trained on 2016-2024) on 2025-2026 WFIGS ignitions
import json, sys, numpy as np, pandas as pd, lightgbm as lgb
from pathlib import Path
from sklearn.metrics import average_precision_score, roc_auc_score
from ml import data, fuelage
from ml.data import X
from ml.train import logit, boot_ci, top_share
M = Path(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/spaceapps/ml-work/models/v2/full"))
d = data.add_derived(pd.read_parquet(os.path.expanduser("~/spaceapps/ml-work/research/oot/ignitions_2025_2026.parquet")))
meta = json.loads((M / "meta.json").read_text())
x = X(d, meta["fire_features"])
raw = np.mean([logit(lgb.Booster(model_file=str(f)).predict(x)) for f in sorted(M.glob("fire_*.txt"))], axis=0)
p0 = 1 / (1 + np.exp(-(meta["a"] * raw + meta["b"])))
p = fuelage.apply(p0, d["years_since_fire"].to_numpy(), meta["fuel_age_beta"])
week = pd.to_datetime(d["date"]).dt.strftime("%G-%V").to_numpy()
bi = d["bi"].to_numpy()
out = {"model": str(M), "n": len(d)}
def block(mask, name):
    r = {"n": int(mask.sum())}
    for label, thr in (("ge400", 400), ("ge40", 40), ("ge4", 4)):
        y = (d["fire_size_ha"].to_numpy() >= thr).astype(int)[mask]
        if y.sum() < 5: continue
        s = {"positives": int(y.sum()), "base_rate": float(y.mean())}
        for k, v in (("model", p[mask]), ("model_without_fuel_age", p0[mask]), ("bi", bi[mask])):
            s[k] = {"pr_auc": float(average_precision_score(y, v)), "roc_auc": float(roc_auc_score(y, v))}
        w = week[mask]
        s["pr_auc_ci95"] = boot_ci(w, lambda i: average_precision_score(y[i], p[mask][i]) if y[i].any() else np.nan)
        s["roc_auc_ci95"] = boot_ci(w, lambda i: roc_auc_score(y[i], p[mask][i]) if 0 < y[i].sum() < len(i) else np.nan)
        s["pr_gain_over_bi_ci95"] = boot_ci(w, lambda i: (average_precision_score(y[i], p[mask][i]) - average_precision_score(y[i], bi[mask][i])) if y[i].any() else np.nan)
        s["fuel_age_gain_ci95"] = boot_ci(w, lambda i: (average_precision_score(y[i], p[mask][i]) - average_precision_score(y[i], p0[mask][i])) if y[i].any() else np.nan)
        s["top"] = {f"{int(q*100)}%": top_share(y, p[mask], q) for q in (0.01, 0.05, 0.1, 0.2)}
        r[label] = s
    out[name] = r
every = np.ones(len(d), bool)
block(every, "2025-2026"); block((d["fire_year"] == 2025).to_numpy(), "2025"); block((d["fire_year"] == 2026).to_numpy(), "2026")
y = d["large_fire"].astype(int).to_numpy(); scar = d["years_since_fire"].to_numpy() < fuelage.CAP
out["calibration"] = {"expected_large": float(p.sum()), "observed_large": int(y.sum())}
out["fuel_age"] = {"ignitions_in_scars": int(scar.sum()), "large_in_scars": int(y[scar].sum()), "expected_before": float(p0[scar].sum()), "expected_after": float(p[scar].sum()),
                   "ge40_in_scars": int((d["fire_size_ha"].to_numpy()[scar] >= 40).sum()), "ge4_in_scars": int((d["fire_size_ha"].to_numpy()[scar] >= 4).sum())}
q = pd.qcut(p, 10, duplicates="drop"); out["reliability"] = [dict(predicted=float(p[q == c].mean()), observed=float(y[q == c].mean()), n=int((q == c).sum())) for c in q.categories]
d.assign(p=p, p_without_fuel_age=p0)[["point_id", "date", "lon", "lat", "fire_size_ha", "large_fire", "years_since_fire", "p", "p_without_fuel_age", "bi"]].to_parquet(M.parent.parent.parent / "research/oot" / f"pred_{M.parent.name}_{M.name}.parquet")
print(json.dumps(out, indent=1))
