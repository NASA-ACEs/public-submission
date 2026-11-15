import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import rasterio
from fuellake.client import LakeClient
from fuellake.registry import load_aoi

from . import fuelage, gcov, grid, rothermel, spreadsim
from .data import X
from .train import logit

BREAKS = [0.2, 0.4, 0.6, 0.8]
REGROWTH = 0.7            # GCOV fuel state below which a cell is reported as regrowth
# Experimental: log-odds of a larger fire per unit of the SME2 anomaly z-score (a pixel against its own track's record),
# given the model's own prediction. Fitted on the 2026 fire season itself, fires >= 4 ha: odds ratio 0.84 [0.76, 0.90];
# for >= 400 ha 0.81 [0.55, 1.06] with 23 events (docs/research/nisar-fire-season.md). Not validated on another year.
SME2_LOG_ODDS, SME2_Z_LIMIT = float(np.log(0.84)), 3.0
GREEN_PEAK = 75           # day of year when herbaceous fuel is greenest (mid-March)
PLAIN = "_plain"                       # suffix of the fire-growth components computed without the GCOV fuel state
KINDS = {"risk_class": "class", "p_large_fire": "probability", "risk_index": "index", "susceptibility_index": "index",
         "risk_index_smap": "index", "dryness_anomaly": "anomaly", "nisar_term": "difference", "sme2_anomaly": "anomaly",
         "p_large_fire_sme2": "probability", "risk_index_sme2": "index", "risk_index_sme2_minus_main": "difference",
         "lfmc_nisar_minus_smap": "difference", "risk_index_nisar_minus_smap": "difference", "spread_permeability": "index",
         **{k: "component" for k in spreadsim.LAYERS + ("wind_speed", "wind_from")},
         **{k + PLAIN: "component" for k in spreadsim.LAYERS}}
UNITS = {"weather_factor": "x", "lfmc": "%", "lfmc_p05": "%", "lfmc_p95": "%", "soil_moisture": "m3/m3", "nisar_term": "m3/m3",
         "dryness_anomaly": "z-score", "sme2_anomaly": "z-score", "years_since_fire": "years", "fuel_state": "ratio", "rate_of_spread": "m/min",
         "flame_length": "m", "fireline_intensity": "kW/m", "spread_direction": "degrees", "spread_ros0": "m/min",
         "spread_wind_cap": "m/s", "slope": "degrees", "aspect": "degrees", "wind_speed": "m/s", "wind_from": "degrees"}
# weather_factor is drawn from 0.25 to 4 with white at 1: three steps of blue below it, twelve of red above
STYLE = {"weather_factor": {"range": [0.25, 4.0], "palette": ["#2166ac", "#67a9cf", "#d1e5f0", "#f7f7f7", "#faebe3", "#fce0cf", "#fbceb6", "#f7b799", "#f29f7e", "#e58268", "#d86651", "#ca4842", "#bb2a34", "#a51429", "#860a24", "#67001f"]}}
NOTES = ["risk_index is the rank (0-1) of p_large_fire among the predictions for past California ignitions on open land "
         "(2017-2024, cells at most 10 % built up; all ignitions of 2016-2024 when the training table has no vegetation "
         "shares): rank_reference.",
         "p_large_fire is conditional on an ignition; it is not the probability that a fire starts.",
         "susceptibility_index: the same rank with every weather input of the model set to its median over the training "
         "ignitions (ordinary_day): what the land contributes. weather_factor: the odds of the day divided by the odds "
         "of that ordinary day; above 1 the day's weather raises the risk of the cell.",
         "NISAR term: per track, an SME2 scene minus the mean of that track's earlier scenes (at least 6); of that "
         "anomaly, the 600 m mean minus the 9 km mean (what SMAP cannot resolve); averaged over the passes of the last "
         "24 days (newest within 12), limited to +-0.03 m3/m3. It is added to SMAP L4 surface soil moisture of the day "
         "and, divided by the spread of SME2's own 9 km mean (0.017) and weighted by its measured agreement between "
         "independent tracks (0.5 in recommended cells, 0.25 elsewhere), to the dryness anomalies. nisar_term is that "
         "term in m3/m3; nisar_effect gives what it changes in the map.",
         "dryness_anomaly is root-zone soil moisture (SMAP L4) as a z-score against the cell's previous 12 months, "
         "plus the NISAR term; negative = drier than its own last year. Whether the fine-scale term carries over to "
         "fire outcomes is under test (docs/EXPERIMENTS.md, N2).",
         "Experimental layers: sme2_anomaly is the newest NISAR SME2 pass as a z-score against the earlier scenes of its "
         "track at the cell (negative = drier than the cell's own record). p_large_fire_sme2 and risk_index_sme2 lower "
         "or raise the odds by 0.84 per unit of it. In the 2026 fire season that anomaly separated ignitions that grew "
         "to 4 ha or more (AUC 0.55 [0.52, 0.58]; SMAP's anomalies 0.45-0.46) and carried information beyond this "
         "model's prediction (odds ratio 0.84 [0.76, 0.90]); for fires of 400 ha or more the 23 events do not settle "
         "it. The coefficient was fitted on that season: these layers are a trial, not part of risk_index.",
         "Models were trained on 2016-2024 with SMAP soil moisture. Feeding them NISAR's fine-scale change is an "
         "assumption; ml.nisar_check compares it with 2026 field samples.",
         "SME2 is PROVISIONAL. Cells not flagged 'recommended' (dense vegetation) are used unless "
         "--sme2-recommended-only is given; their share is nisar_not_recommended_share.",
         "With the core models, land without burnable fuel (cropland, desert, urban) is not masked.",
         "Retrospective daily map: gridMET weather of the day, no forecast.",
         f"Fuel age: where a fire is recorded in the last {fuelage.YOUNG:g} years (years_since_fire) the odds of a large "
         f"fire are multiplied by exp(-beta exp(-years)), beta in fuel_age; where the land was forest before the fire "
         f"(fuel model of the oldest LANDFIRE release) they stay lowered for {fuelage.FOREST_UNTIL:g} years. No ignition "
         "inside a scar younger than 2 years became a large fire in 2016-2026 (12.6 expected). spread_permeability is "
         "how often a cell of a scar burned when a later large fire reached it, against neighbouring land: forest "
         "scars 0.26-0.46 for 7 years, shrub 0.29 then about 0.8, grass 0.53 and no effect after 2 years. LANDFIRE "
         "fuel models do not carry this effect.",
         "Where NISAR GCOV is staged, fuel_state is L-band HV power against unburned cells of the same pre-fire fuel "
         "model and terrain (1 = mature fuel). It is a layer and scales the woody fuel of the spread physics. It does "
         "not change p_large_fire: using it to mark unburned islands inside perimeters was tested against the "
         "Sentinel-2 NDVI drop after the 2025 fires and failed (docs/EXPERIMENTS.md, G2).",
         "rate_of_spread, flame_length, fireline_intensity and spread_direction are the head fire of Rothermel's "
         "surface model for the LANDFIRE fuel model with the day's gridMET weather, LFMC from this map as live woody "
         "moisture and, in fuel models with woody fuel, loads and bed depth scaled by the GCOV fuel state. Daily mean "
         "wind on a 4 km grid, no gusts: absolute rates are low. They are not inputs of p_large_fire.",
         "Layers of kind 'component' (spread_ros0, spread_wind_a, ...) are the wind-independent parts of that head "
         "fire; with them ml.spreadsim grows a fire from any point for any wind (minimum travel time, fuel-age "
         "permeability as a factor on the rate). The simulated area is a potential, not a predicted fire size.",
         "vectors.geojson: one line per km (sparser on large grids), the distance the head fire would run in one "
         "hour at that rate."]


def write(path, g, mask, values, dtype="float32", nodata=-9999):
    a = np.full(g.shape, nodata, dtype)
    a[mask] = np.where(np.isfinite(values), values, nodata)
    with rasterio.open(path, "w", driver="GTiff", height=g.height, width=g.width, count=1, dtype=dtype, crs=g.crs,
                       transform=g.transform, nodata=nodata, compress="deflate", tiled=True) as ds:
        ds.write(a, 1)


def predict(models, meta, df):
    q = np.sort(np.column_stack([lgb.Booster(model_file=str(models / f"lfmc_q{n}.txt")).predict(X(df, meta["lfmc_features"]))
                                 for n in ("05", "50", "95")]), axis=1)
    df["lfmc_hat"] = q[:, 1]
    x = X(df, meta["fire_features"])
    raw = np.mean([logit(lgb.Booster(model_file=str(f)).predict(x)) for f in sorted(models.glob("fire_*.txt"))], axis=0)
    p = 1 / (1 + np.exp(-(meta["a"] * raw + meta["b"])))
    if meta.get("fuel_age_beta") and "years_since_fire" in df:
        veg = df["prefire_vegetation"].to_numpy() if "prefire_vegetation" in df else None
        p = fuelage.apply(p, df["years_since_fire"].to_numpy(), meta["fuel_age_beta"], veg)
    return q, p, np.searchsorted(meta["rank_quantiles"], p, side="right") / len(meta["rank_quantiles"])


def spread(client, aoi, day, df, mask, lfmc, state):
    try:
        code, slope, aspect = (gcov.layer(client, c, aoi.id, k)[mask] for c, k in (
            ("landfire-fbfm40", "fuel_model"), ("landfire-topo", "slope"), ("landfire-topo", "aspect")))
        cover, height = (gcov.layer(client, "landfire-canopy", aoi.id, k)[mask] for k in ("canopy_cover", "canopy_height"))
    except SystemExit:
        return {}
    th = grid.series(client.lake, aoi, "gridmet", day).grid_slice("th", day, aoi.grid)[mask]
    green = 0.5 + 0.5 * np.cos(2 * np.pi * (day.timetuple().tm_yday - GREEN_PEAK) / 365.25)
    plain = (code, df["rmin"], df["tmmx"], df["fm100"], lfmc, green, cover, height)
    out = rothermel.grid_layers(code, df["vs"], th, df["rmin"], df["tmmx"], df["fm100"], lfmc, slope, aspect, green, cover, height,
                                1.0 if state is None else state)
    # what ml.spreadsim needs to grow a fire from any point for any wind, without the lake
    out.update(spreadsim.components(*plain, 1.0 if state is None else state))
    if state is not None:              # the same from the fuel map alone, to show what the GCOV fuel state changes
        out.update({f"{k}{PLAIN}": v for k, v in spreadsim.components(*plain, 1.0).items()})
    return dict(out, slope=slope, aspect=aspect, wind_speed=df["vs"].to_numpy(), wind_from=th)


def arrows(g, mask, ros, direction, flame, step_m=1000, hours=1, longest_m=5000, most=5000):
    from pyproj import Transformer
    step = max(1, int(round(step_m / abs(g.transform.a))), int(np.ceil(np.sqrt(g.height * g.width / most))))
    grids = []
    for v in (ros, direction, flame):
        a = np.full(g.shape, np.nan, "float32")
        a[mask] = v
        grids.append(a[step // 2::step, step // 2::step])
    r, d, f = grids
    rows, cols = np.nonzero(np.isfinite(r) & (r > 0))
    x0 = g.transform.c + (cols * step + step // 2 + 0.5) * g.transform.a
    y0 = g.transform.f + (rows * step + step // 2 + 0.5) * g.transform.e
    run = np.minimum(r[rows, cols] * 60 * hours, longest_m)
    rad = np.deg2rad(d[rows, cols])
    to_wgs = Transformer.from_crs(g.crs, "EPSG:4326", always_xy=True).transform
    (lon0, lat0), (lon1, lat1) = to_wgs(x0, y0), to_wgs(x0 + run * np.sin(rad), y0 + run * np.cos(rad))
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[round(a, 6), round(b, 6)], [round(c, 6), round(e, 6)]]},
         "properties": {"rate_of_spread_m_min": round(float(v), 2), "direction_deg": round(float(w), 1),
                        "flame_length_m": round(float(h), 2), "run_hours": hours}}
        for a, b, c, e, v, w, h in zip(lon0, lat0, lon1, lat1, r[rows, cols], d[rows, cols], f[rows, cols])]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--aoi", default="ca")
    ap.add_argument("--models", default="models/core")
    ap.add_argument("--out", required=True)
    ap.add_argument("--soil", choices=["nisar", "smap"], default="nisar")
    ap.add_argument("--sme2-recommended-only", action="store_true")
    ap.add_argument("--name", default="", help="model version label, appended to result_id and model_version")
    args = ap.parse_args()
    day, models, out = date.fromisoformat(args.date), Path(args.models), Path(args.out)
    meta = json.loads((models / "meta.json").read_text())
    client, aoi = LakeClient.from_env(), load_aoi(args.aoi)
    g = aoi.grid
    feats = sorted(set(meta["lfmc_features"]) | set(meta["fire_features"]) - {"lfmc_hat"})
    arr, mask, info = grid.arrays(client, args.aoi, day, feats, args.soil, args.sme2_recommended_only)
    nisar = max(info["nisar"].values(), key=lambda v: v["share"], default={})
    if args.soil == "nisar" and not nisar:
        raise SystemExit(f"no usable NISAR SME2 on {args.aoi} for {day}: need a pass within {grid.SME2_WINDOW} days "
                         f"before the date with {grid.MIN_SCENES} earlier scenes of its track. Stage nisar-sme2 for the "
                         "months before the date, or pass --soil smap for the SMAP-only comparison map.")
    mask &= np.isfinite(arr["vpd"])
    if info["burnable"] is not None:
        mask &= info["burnable"] >= 0.5
    df = grid.frame(arr, mask, day)
    del arr

    years = fuelage.on_grid(fuelage.perimeters(client), day, g)[mask]
    state = gcov.fuel_state(client, args.aoi, day) if args.soil == "nisar" else None
    before = grid.prefire(client, args.aoi)
    veg = None if before is None else before[mask]
    fuel = dict(beta=meta.get("fuel_age_beta"), scar_share=float((years < fuelage.CAP).mean()),
                young_scar_share=float((years < fuelage.YOUNG).mean()),
                former_forest_share_of_scars=None if veg is None else float((veg[years < fuelage.CAP] == fuelage.FOREST).mean()))
    if state:
        scar = years < fuelage.CAP
        fuel["gcov"] = dict(scenes=len(state["items"]), facing_azimuth=state["tracks"], unburned_median=state["unburned_median"],
                            burned_median=state["burned_median"], regrowth_threshold=REGROWTH,
                            regrowth_share_in_scars=float((state["state"][mask][scar] < REGROWTH).mean()) if scar.any() else None,
                            regrowth_share_outside=float((state["state"][mask][~scar] < REGROWTH).mean()))
        info["sme2_items"] = info["sme2_items"] + state["items"]
    df["years_since_fire"] = years
    if veg is not None:
        df["prefire_vegetation"] = veg

    q, p, rank = predict(models, meta, df)
    layers = {"lfmc": q[:, 1], "p_large_fire": p, "risk_index": rank, "susceptibility_index": rank,
              "years_since_fire": years, "spread_permeability": fuelage.permeability(years, veg)}
    for name, c in {"soil_moisture": "sm_surface", "dryness_anomaly": "rz_z12"}.items():
        if c in df:
            layers[name] = df[c].to_numpy().copy()
    if meta.get("ordinary_day"):
        today = {c: df[c].to_numpy().copy() for c in meta["ordinary_day"]}
        for c, v in meta["ordinary_day"].items():
            df[c] = np.float32(v)
        _, p_land, layers["susceptibility_index"] = predict(models, meta, df)
        layers["weather_factor"] = (p / (1 - p)) / np.maximum(p_land / (1 - p_land), 1e-12)
        for c, a in today.items():
            df[c] = a
    if state:
        layers["fuel_state"] = state["state"][mask]
    fire = spread(client, aoi, day, df, mask, q[:, 1], state and state["state_now"][mask])
    layers.update(fire)
    if fire and state:
        plain = spread(client, aoi, day, df, mask, q[:, 1], None)["rate_of_spread"]
        low = (state["state_now"][mask] < REGROWTH) & (plain > 0)
        fuel["gcov"]["rate_of_spread_ratio_where_regrowth"] = float(np.median(fire["rate_of_spread"][low] / plain[low])) if low.any() else None
    effect = None
    if info["smap"]:
        with_nisar = {c: df[c].to_numpy().copy() for c in info["smap"]}
        for c, a in info["smap"].items():
            df[c] = a[mask]
        q0, _, rank0 = predict(models, meta, df)
        fed = np.any([np.isfinite(with_nisar[c]) & (with_nisar[c] != df[c].to_numpy()) for c in with_nisar], axis=0)
        effect = dict(cells=int(fed.sum()), mean_abs_lfmc_pct=float(np.abs(q[:, 1] - q0[:, 1])[fed].mean()),
                      mean_abs_risk_index=float(np.abs(rank - rank0)[fed].mean()),
                      p95_abs_risk_index=float(np.percentile(np.abs(rank - rank0)[fed], 95)),
                      **{f"mean_abs_{c}": float(np.nanmean(np.abs(with_nisar[c] - df[c].to_numpy())[fed])) for c in with_nisar})
        layers["lfmc_nisar_minus_smap"] = q[:, 1] - q0[:, 1]
        layers["risk_index_nisar_minus_smap"] = rank - rank0
        layers["risk_index_smap"] = rank0
        layers["nisar_term"] = info["fine"][mask]
    trial = None
    if info["anomaly"] is not None:
        z = info["anomaly"][mask]
        m = np.exp(SME2_LOG_ODDS * np.clip(np.nan_to_num(z), -SME2_Z_LIMIT, SME2_Z_LIMIT))
        p_trial = p * m / (1 - p + p * m)
        rank_trial = np.searchsorted(meta["rank_quantiles"], p_trial, side="right") / len(meta["rank_quantiles"])
        layers.update(sme2_anomaly=z, p_large_fire_sme2=p_trial, risk_index_sme2=rank_trial, risk_index_sme2_minus_main=rank_trial - rank)
        seen = np.isfinite(z)
        trial = dict(status="experimental", odds_ratio_per_unit=float(np.exp(SME2_LOG_ODDS)), z_limit=SME2_Z_LIMIT,
                     share=float(seen.mean()), mean_abs_z=float(np.abs(z[seen]).mean()),
                     mean_abs_risk_index=float(np.abs(rank_trial - rank)[seen].mean()),
                     p95_abs_risk_index=float(np.percentile(np.abs(rank_trial - rank)[seen], 95)))
    lo = np.clip(np.minimum(q[:, 0] - meta["conformal"], q[:, 1]), 0, 400)
    hi = np.clip(np.maximum(q[:, 2] + meta["conformal"], q[:, 1]), 0, 400)
    layers.update(lfmc=np.clip(q[:, 1], 0, 400), lfmc_p05=lo, lfmc_p95=hi)

    out.mkdir(parents=True, exist_ok=True)
    for name, v in layers.items():
        write(out / f"{name}.tif", g, mask, v)
    write(out / "risk_class.tif", g, mask, np.searchsorted(BREAKS, rank, side="right") + 1, "uint8", 0)
    vectors = arrows(g, mask, fire["rate_of_spread"], fire["spread_direction"], fire["flame_length"]) if fire else \
        {"type": "FeatureCollection", "features": []}
    (out / "vectors.geojson").write_text(json.dumps(vectors))

    start = f"{day}T07:00:00Z"
    end = f"{day + timedelta(days=1)}T07:00:00Z"
    result = dict(
        schema_version="backend-result-1", result_id="-".join(filter(None, [args.aoi, f"{day:%Y%m%d}", args.soil, args.name])), synthetic=False,
        research_only=True, as_of=end, valid_from=start, valid_to=end, bbox_wgs84=list(g.bounds_wgs84()),
        assessment_layer="risk_index", fuel_context_layer="susceptibility_index", mode="ml",
        target="P(final fire size >= 400 ha | ignition)", model_version="-".join(filter(None, ["lightgbm", models.name, args.name or "2"])), aoi=args.aoi,
        fuel_context_observed_at=start, fuel_context_max_age_days=12, valid_pixels=int(mask.sum()),
        source_items=info["sme2_items"], published={}, vector_count=len(vectors["features"]), class_breaks=BREAKS,
        rank_reference=meta.get("rank_reference"), ordinary_day=meta.get("ordinary_day"),
        soil_source=args.soil, nisar_share=nisar.get("share", 0.0),
        nisar_not_recommended_share=nisar.get("not_recommended_share"), nisar_effect=effect, nisar=info["nisar"], sme2_anomaly_trial=trial, fuel_age=fuel,
        model_sha256={f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(models.glob("*.txt"))},
        layers={n: {"path": f"{n}.tif", "kind": KINDS.get(n, "continuous"), **({"units": UNITS[n]} if n in UNITS else {}), **STYLE.get(n, {})}
                for n in [*layers, "risk_class"]}, notes=NOTES)
    (out / "result.json").write_text(json.dumps(result, indent=2))
    (out / "provenance.json").write_text(json.dumps(dict(result, configuration=vars(args), nisar=info["nisar"],
                                                         training={k: meta[k] for k in ("table", "table_sha256", "lfmc_rows", "ignition_rows")}), indent=2))
    print(out, f"{int(mask.sum())} cells, NISAR share {nisar.get('share', 0):.1%}, median LFMC {np.median(q[:, 1]):.0f} %, "
               f"median P {np.median(p):.4f}, NISAR effect {effect}, fuel {fuel}")
