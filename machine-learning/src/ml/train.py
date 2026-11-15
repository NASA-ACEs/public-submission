import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from . import data, fuelage
from .data import X

K = 5
SEEDS = range(5)          # the fire model is the mean of this many boosters: one seed moves PR-AUC as much as a feature group
BOOT = 500
QUANTILES = (0.05, 0.5, 0.95)
OPEN_LAND = 0.1           # built-up share of a cell up to which its ignitions are the reference of risk_index
WORKERS = int(os.environ.get("ML_WORKERS", 32))
# one thread per fit, fits side by side: LightGBM with a thread per core crawls on tables this small
COMMON = dict(subsample=0.8, subsample_freq=1, colsample_bytree=0.8, verbose=-1, n_jobs=1)
REG = dict(n_estimators=400, learning_rate=0.05, num_leaves=31, min_child_samples=40, **COMMON)
CLF = dict(n_estimators=300, learning_rate=0.03, num_leaves=7, min_child_samples=200,
           monotone_constraints_method="intermediate", **COMMON)


def pmap(fn, items):
    with ThreadPoolExecutor(WORKERS) as ex:
        return list(ex.map(fn, items))


def fit_lfmc(df, cols, q=0.5):
    return lgb.LGBMRegressor(objective="quantile", alpha=q, random_state=0, **REG).fit(X(df, cols), df["lfmc_pct"]).booster_


def fit_fire(df, cols, seeds=SEEDS):
    mono = [data.MONOTONE.get(c, 0) for c in cols]
    x, y = X(df, cols), df["large_fire"].astype(int)
    return [lgb.LGBMClassifier(random_state=s, monotone_constraints=mono, **CLF).fit(x, y).booster_ for s in seeds]


def logit(p):
    p = np.clip(p, 1e-7, 1 - 1e-7)
    return np.log(p / (1 - p))


def fire_score(boosters, x):
    # mean of the boosters' logits, one column per booster kept for the seed noise
    return np.column_stack([logit(b.predict(x)) for b in boosters])


def sigmoid(z):
    return 1 / (1 + np.exp(-z))


def oof(folds, fit_predict):
    names = np.unique(folds)
    parts = pmap(lambda f: np.asarray(fit_predict(folds != f, folds == f)), names)
    out = np.full((len(folds),) + parts[0].shape[1:], np.nan)
    for f, p in zip(names, parts):
        out[folds == f] = p
    return out


def rmse(y, p):
    return float(np.sqrt(np.mean((y - p) ** 2)))


def boot_ci(groups, stat):
    idx = list(pd.Series(np.arange(len(groups))).groupby(np.asarray(groups)).indices.values())
    rng = np.random.default_rng(0)
    draws = [np.concatenate([idx[i] for i in rng.integers(0, len(idx), len(idx))]) for _ in range(BOOT)]
    return [float(v) for v in np.nanpercentile(pmap(stat, draws), [2.5, 97.5])]


def years(df):
    return pd.to_datetime(df["date"]).dt.year.to_numpy()


def lfmc_metrics(df, p):
    y, q = df["lfmc_pct"].to_numpy(), p[:, 1]
    r = pd.DataFrame({"s": df["site"].to_numpy(), "y": y, "q": q}).groupby("s")[["y", "q"]].apply(
        lambda d: np.corrcoef(d["y"], d["q"])[0, 1] if len(d) >= 10 else np.nan)
    return dict(n=len(y), rmse=rmse(y, q), mae=float(np.abs(y - q).mean()),
                r2=float(1 - ((y - q) ** 2).sum() / ((y - y.mean()) ** 2).sum()), bias=float((q - y).mean()),
                coverage90_raw=float(((y >= p[:, 0]) & (y <= p[:, 2])).mean()), width90_raw=float((p[:, 2] - p[:, 0]).mean()),
                within_site_r_median=float(r.median()))


def lfmc_cv(df, cols, folds, quantiles=QUANTILES):
    def one(tr, te):
        return np.column_stack([fit_lfmc(df[tr], cols, q).predict(X(df[te], cols)) for q in quantiles])
    return np.sort(oof(folds, one), axis=1)


def lfmc_new_place_and_year(df, cols, folds, year):
    cells = [(f, yr) for f in np.unique(folds) for yr in np.unique(year) if ((folds == f) & (year == yr)).any()]
    parts = pmap(lambda c: fit_lfmc(df[(folds != c[0]) & (year != c[1])], cols)
                 .predict(X(df[(folds == c[0]) & (year == c[1])], cols)), cells)
    out = np.full(len(df), np.nan)
    for (f, yr), p in zip(cells, parts):
        out[(folds == f) & (year == yr)] = p
    return out


def lfmc_report(df, cols):
    y, blocks, year = df["lfmc_pct"].to_numpy(), df["block_50km"].to_numpy(), years(df)
    folds = data.block_fold(blocks)
    p = lfmc_cv(df, cols, folds)
    new = lfmc_new_place_and_year(df, cols, folds, year)
    out = {"unseen_block": lfmc_metrics(df, p), "unseen_block_rmse_ci95": boot_ci(blocks, lambda i: rmse(y[i], p[i, 1]))}
    out["unseen_year_rmse"] = rmse(y, lfmc_cv(df, cols, year, (0.5,))[:, 0])
    out["unseen_block_and_year_rmse"] = rmse(y, new)
    out["unseen_block_and_year_rmse_ci95"] = boot_ci(blocks, lambda i: rmse(y[i], new[i]))
    out["random_split"] = lfmc_metrics(df, lfmc_cv(df, cols, np.random.default_rng(0).integers(0, K, len(df))))
    b = df.reindex(columns=["vpd", "sm_surface"]).astype(float)
    b = b.fillna(b.median()).fillna(0).to_numpy()
    out["baseline_mean_rmse"] = rmse(y, oof(folds, lambda tr, te: np.full(te.sum(), y[tr].mean())))
    out["baseline_linear_rmse"] = rmse(y, oof(folds, lambda tr, te: LinearRegression().fit(b[tr], y[tr]).predict(b[te])))
    score = np.maximum(p[:, 0] - y, y - p[:, 2])
    if (year == 2024).sum() >= 50 and (year < 2024).sum() >= 50:
        tr, te = df[year < 2024], df[year == 2024]
        pt = np.sort(np.column_stack(pmap(lambda q: fit_lfmc(tr, cols, q).predict(X(te, cols)), QUANTILES)), axis=1)
        out["train_to_2023_test_fems_2024"] = lfmc_metrics(te, pt)
        out["coverage90_widened_on_2024"] = float((score[year == 2024] <= np.quantile(score[year < 2024], 0.9)).mean())
    no_soil = [c for c in cols if c not in data.SOIL]
    q = {"none": lfmc_cv(df, no_soil, folds, (0.5,))[:, 0]}
    for lag in (0,) + data.LAGS:
        names = [s if lag == 0 else f"{s}_lag{lag}d" for s in data.SMAP]
        q[str(lag)] = lfmc_cv(df, no_soil + [c for c in cols if c in names], folds, (0.5,))[:, 0]
    if any(c in cols for c in data.RAIN):
        q["no_rain_sums"] = lfmc_cv(df, [c for c in cols if c not in data.RAIN], folds, (0.5,))[:, 0]
        q["all"] = p[:, 1]

    def gain(a, b):
        return {"gain": rmse(y, q[a]) - rmse(y, q[b]), "ci95": boot_ci(blocks, lambda i: rmse(y[i], q[a][i]) - rmse(y[i], q[b][i]))}

    lags = [k for k in q if k.isdigit()]
    best = min(lags, key=lambda k: rmse(y, q[k]))
    out["soil_lag_curve"] = {k: {"rmse": rmse(y, q[k]), **({} if k == "none" else {"vs_weather_only": gain("none", k)})}
                             for k in ["none"] + lags}
    out["soil_lag_best"] = {"lag": best, "vs_lag0": gain("0", best)}
    if "all" in q:
        out["rain_sums_gain"] = gain("no_rain_sums", "all")
    return out, p, float(np.quantile(score, 0.9))


def with_lfmc_hat(lf, ig, cols, f_lf, f_ig):
    hat = np.full(len(ig), np.nan)
    names = np.unique(f_ig)
    for f, p in zip(names, pmap(lambda f: fit_lfmc(lf[f_lf != f], cols).predict(X(ig[f_ig == f], cols)), names)):
        hat[f_ig == f] = p
    return ig.assign(lfmc_hat=hat)


def fire_cv(ig, folds, cols, seeds=SEEDS):
    # fuel, canopy and vegetation layers do not exist before their first release (the 2016 rows). In a fold whose training
    # rows all have them the trees never learn where to send a missing value, so such test rows are scored by a model
    # fitted without those layers; in every other fold rows without them are in the training data
    dated = [c for c in cols if c in data.DATED]
    bare = np.zeros(len(ig), bool)
    if dated and "elevation" in ig:
        bare = (ig[dated].isna().all(axis=1) & ig["elevation"].notna()).to_numpy()
    plain = [c for c in cols if c not in dated]
    tasks = []
    for f in np.unique(folds):
        te = folds == f
        alone = bare[te].any() and bare[~te].mean() < 0.01       # too few such rows in training for the trees to learn from
        tasks += [(f, s, rows, use) for s in seeds for rows, use in ([(te & ~bare, cols), (te & bare, plain)] if alone else [(te, cols)])
                  if rows.any()]
    parts = pmap(lambda t: fire_score(fit_fire(ig[folds != t[0]], t[3], [t[1]]), X(ig[t[2]], t[3]))[:, 0], tasks)
    out = np.full((len(ig), len(seeds)), np.nan)
    for (f, s, rows, _), p in zip(tasks, parts):
        out[rows, list(seeds).index(s)] = p
    return out


def fire_scores(y, z):
    return dict(pr_auc=float(average_precision_score(y, z)), roc_auc=float(roc_auc_score(y, z)))


def top_share(y, z, share):
    return float(y[np.argsort(-z)[:int(len(z) * share)]].sum() / max(y.sum(), 1))


def fire_report(lf, ig, lfmc_cols, cols):
    y, year = ig["large_fire"].astype(int).to_numpy(), ig["fire_year"].astype(int).to_numpy()
    week = pd.to_datetime(ig["date"]).dt.strftime("%G-%V").to_numpy()
    variants = {"model": cols, "with_lfmc_hat": cols + ["lfmc_hat"], "weather_only": [c for c in cols if c in data.GRIDMET],
                "without_dryness": [c for c in cols if c not in data.DRYNESS],
                "without_antecedent": [c for c in cols if c not in data.ANTECEDENT],
                "without_gusts": [c for c in cols if c not in data.WIND],
                "without_terrain_and_fuel": [c for c in cols if c not in data.STATIC],
                "without_vegetation_type": [c for c in cols if c not in data.EVT],
                "without_surroundings": [c for c in cols if c not in data.CONTEXT]}
    variants = {k: c for k, c in variants.items() if k in ("model", "with_lfmc_hat") or (c and c != cols)}
    out = {"n": len(y), "positives": int(y.sum()), "base_rate": float(y.mean())}

    # headline: a year never seen, as for the 2026 map; lfmc_hat from an LFMC model that did not see the year either
    ig_y = with_lfmc_hat(lf, ig, lfmc_cols, years(lf), year)
    z = {k: fire_cv(ig_y, year, c) for k, c in variants.items()}
    p = {k: v.mean(axis=1) for k, v in z.items()}
    p["bi"] = ig["bi"].astype(float).fillna(ig["bi"].astype(float).min()).to_numpy()
    out["unseen_year"] = {k: fire_scores(y, v) for k, v in p.items()}

    def ap(v, i):
        return average_precision_score(y[i], v[i]) if y[i].any() else np.nan

    every = np.arange(len(y))
    pairs = {"dryness": ("model", "without_dryness"), "antecedent": ("model", "without_antecedent"), "gusts": ("model", "without_gusts"),
             "terrain_and_fuel": ("model", "without_terrain_and_fuel"), "vegetation_type": ("model", "without_vegetation_type"),
             "surroundings": ("model", "without_surroundings"),
             "weather_to_model": ("model", "weather_only"),
             "lfmc_hat": ("with_lfmc_hat", "model"), "model_vs_bi": ("model", "bi")}
    out["unseen_year"]["pr_auc_gain"] = {
        name: {"gain": float(ap(p[a], every) - ap(p[b], every)), "ci95": boot_ci(week, lambda i, a=a, b=b: ap(p[a], i) - ap(p[b], i))}
        for name, (a, b) in pairs.items() if a in p and b in p}
    single = [average_precision_score(y, z["model"][:, s]) for s in range(z["model"].shape[1])]
    out["unseen_year"]["pr_auc_single_seed"] = {"mean": float(np.mean(single)), "sd": float(np.std(single, ddof=1))}
    other = fire_cv(ig_y, year, cols, [s + len(SEEDS) for s in SEEDS]).mean(axis=1)
    out["unseen_year"]["pr_auc_seed_noise"] = float(abs(average_precision_score(y, other) - ap(p["model"], every)))
    late = year >= 2022
    if late.any() and (~late).any():
        out["unseen_year"]["years_2022_on"] = fire_scores(y[late], p["model"][late])
    out["unseen_year"]["large_fires_in_top"] = {f"{int(s * 100)}%": top_share(y, p["model"], s) for s in (0.01, 0.05, 0.1, 0.2)}
    out["unseen_year"]["roc_auc_gain"] = {
        name: {"gain": float(roc_auc_score(y, p[a]) - roc_auc_score(y, p[b])),
               "ci95": boot_ci(week, lambda i, a=a, b=b: (roc_auc_score(y[i], p[a][i]) - roc_auc_score(y[i], p[b][i])) if 0 < y[i].sum() < len(i) else np.nan)}
        for name, (a, b) in pairs.items() if a in p and b in p}

    # secondary: places never seen (whole blocks), over 5 fold assignments; and a naive random split
    blocks = [fire_scores(y, fire_cv(ig, data.block_fold(ig["block_50km"], K, s), cols).mean(axis=1)) for s in SEEDS]
    out["unseen_block"] = {m: {"mean": float(np.mean([s[m] for s in blocks])), "sd": float(np.std([s[m] for s in blocks], ddof=1))}
                           for m in ("pr_auc", "roc_auc")}
    out["random_split"] = fire_scores(y, fire_cv(ig, np.random.default_rng(0).integers(0, K, len(ig)), cols).mean(axis=1))

    platt = LogisticRegression(C=1e6, max_iter=1000).fit(p["model"].reshape(-1, 1), y)
    a, b = float(platt.coef_[0, 0]), float(platt.intercept_[0])
    cal = sigmoid(a * p["model"] + b)
    beta = 0.0
    if "years_since_fire" in ig and (ig["years_since_fire"] < fuelage.CAP).any():
        # fuel age: odds lowered inside recent fire perimeters; beta for a year is fitted on the other years
        age, aged = ig["years_since_fire"].to_numpy(), cal.copy()
        veg = ig["prefire_vegetation"].to_numpy() if "prefire_vegetation" in ig else None
        part = lambda m: None if veg is None else veg[m]
        for yr in np.unique(year):
            te = year == yr
            aged[te] = fuelage.apply(cal[te], age[te], fuelage.fit(cal[~te], age[~te], y[~te], part(~te)), part(te))
        beta, scar = fuelage.fit(cal, age, y, veg), age < fuelage.CAP
        young = age < fuelage.YOUNG
        out["fuel_age"] = dict(
            beta=beta, ignitions_in_scars=int(scar.sum()), large_in_scars=int(y[scar].sum()),
            expected_large_in_scars_before=float(cal[scar].sum()), expected_large_in_scars_after=float(aged[scar].sum()),
            young_scars=dict(ignitions=int(young.sum()), large=int(y[young].sum()), expected_before=float(cal[young].sum()),
                             expected_after=float(aged[young].sum())),
            pr_auc=float(average_precision_score(y, aged)),
            pr_auc_gain={"gain": float(ap(aged, every) - ap(cal, every)), "ci95": boot_ci(week, lambda i: ap(aged, i) - ap(cal, i))})
        cal = aged
    out["brier"], out["brier_base_rate"] = float(brier_score_loss(y, cal)), float(np.mean((y - y.mean()) ** 2))
    bins = pd.qcut(cal, 10, duplicates="drop")
    out["reliability"] = [dict(predicted=float(cal[bins == c].mean()), observed=float(y[bins == c].mean()),
                               n=int((bins == c).sum())) for c in bins.categories]
    # risk_index ranks against ignitions on open land: most ignitions start in built-up cells, and against all of them
    # nearly every wildland cell of a map falls in the top fifth
    open_land = (ig["evt_share_developed"] <= OPEN_LAND).to_numpy() if "evt_share_developed" in ig else np.zeros(len(ig), bool)
    limit = OPEN_LAND if open_land.any() else None               # no vegetation shares in the table: all ignitions
    open_land = open_land if limit else np.ones(len(ig), bool)
    ref = np.sort(cal[open_land])
    out["rank_reference"] = dict(ignitions=int(open_land.sum()), large=int(y[open_land].sum()), built_up_share_at_most=limit,
                                 median_p=float(np.median(ref)), median_p_all_ignitions=float(np.median(cal)))
    return out, ig_y, cal, dict(a=a, b=b, fuel_age_beta=beta, rank_quantiles=ref[np.linspace(0, len(ref) - 1, 1001).astype(int)].tolist(),
                                rank_reference=out["rank_reference"])


def gain_share(booster, names):
    g = dict(zip(booster.feature_name(), booster.feature_importance("gain")))
    return float(sum(g.get(n, 0) for n in names) / max(sum(g.values()), 1e-12))


def run(lf, ig, lfmc_cols, fire_cols, out, info):
    out.mkdir(parents=True, exist_ok=True)
    lfmc, p, conformal = lfmc_report(lf, lfmc_cols)
    fire, ig_y, cal, calib = fire_report(lf, ig, lfmc_cols, fire_cols)
    models = dict(zip(QUANTILES, pmap(lambda q: fit_lfmc(lf, lfmc_cols, q), QUANTILES)))
    for q, m in models.items():
        m.save_model(str(out / f"lfmc_q{int(q * 100):02d}.txt"))
    boosters = fit_fire(ig, fire_cols)
    for s, m in zip(SEEDS, boosters):
        m.save_model(str(out / f"fire_{s}.txt"))
    lfmc["soil_gain_share"] = gain_share(models[0.5], data.SOIL)
    fire["dryness_gain_share"] = float(np.mean([gain_share(m, data.DRYNESS) for m in boosters]))
    lf.assign(p05=p[:, 0], p50=p[:, 1], p95=p[:, 2])[["site", "date", "block_50km", "lfmc_pct", "p05", "p50", "p95"]] \
        .to_parquet(out / "oof_lfmc.parquet")
    ig_y.assign(p=cal)[["point_id", "date", "block_50km", "large_fire", "p", "bi", "lfmc_hat"]].to_parquet(out / "oof_fire.parquet")
    (out / "metrics.json").write_text(json.dumps({"lfmc": lfmc, "fire": fire}, indent=2))
    (out / "meta.json").write_text(json.dumps({**info, "lfmc_features": lfmc_cols, "fire_features": fire_cols,
                                               "monotone": {c: data.MONOTONE[c] for c in fire_cols if c in data.MONOTONE},
                                               "conformal": conformal, **calib,
                                               # each weather input at its median over the ignitions: the day against which
                                               # ml.map separates what the land contributes from what the weather does
                                               "ordinary_day": {c: float(ig[c].median()) for c in fire_cols
                                                                if c in data.GRIDMET + data.SERIES_ALL}}))
    g = fire["unseen_year"]["pr_auc_gain"]["model_vs_bi"]
    print(out, "| LFMC RMSE new place + year", round(lfmc["unseen_block_and_year_rmse"], 1), "vs linear baseline",
          round(lfmc["baseline_linear_rmse"], 1), "| fire PR-AUC new year", round(fire["unseen_year"]["model"]["pr_auc"], 4),
          "gain over bi", round(g["gain"], 4), [round(v, 4) for v in g["ci95"]])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--table")
    ap.add_argument("--out", default="models")
    ap.add_argument("--sets", help="comma-separated names from ml.data.FEATURES (default: all)")
    args = ap.parse_args()
    pts = data.load_points(args.table)
    lf, ig = data.add_derived(data.lfmc_rows(pts)), data.add_derived(data.ignition_rows(pts))
    info = {"table": args.table, "lfmc_rows": len(lf), "ignition_rows": len(ig),
            "table_sha256": hashlib.sha256(Path(args.table).read_bytes()).hexdigest() if args.table else None}
    done = []
    for name, sets in data.FEATURES.items():
        if args.sets and name not in args.sets.split(","):
            continue
        cols = [[c for c in s if c in lf and c in ig and lf[c].notna().any() and ig[c].notna().any()]
                for s in (sets["lfmc"], sets["fire"])]
        if cols in done:
            continue
        done.append(cols)
        fill = {c: [round(float(lf[c].notna().mean()), 3), round(float(ig[c].notna().mean()), 3)] for c in set(cols[0] + cols[1])}
        low = {c: v for c, v in fill.items() if min(v) < 0.8}
        if low:
            print(f"WARNING {name}: features filled below 80 % (LFMC rows, ignition rows): {low}")
        run(lf, ig, *cols, Path(args.out) / name, {**info, "fill_rates_lfmc_fire": fill})
