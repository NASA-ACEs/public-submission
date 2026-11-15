import argparse
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import pandas as pd
from fuellake import catalog
from fuellake.client import LakeClient
from fuellake.features._common import WeatherSeries
from fuellake.registry import lake_region, load_aoi, load_dataset
from scipy.ndimage import uniform_filter

from . import data, fuelage
from . import series as daily
from .data import split

FORM_B = {"gridmet": data.GRIDMET, "smap-l4": data.SMAP}
RELEASE = re.compile(r"__lf(\d{4})")
REACH = max(data.LAGS) + 1
# The NISAR term (docs/research/sme2-resolution.md): per track, a scene minus the mean of that track's earlier scenes;
# of that anomaly, the part finer than one SMAP cell; averaged over the passes of the last PASS_DAYS.
SME2_WINDOW = 12          # days before the target in which the newest pass must lie
PASS_DAYS = 24
HISTORY_DAYS = 365
MIN_SCENES = 6            # earlier scenes of the track a cell needs
SME2_VALID = (0.02, 0.60)  # the retrieval's bounds: a value on them is not a retrieval
FINE_M, BLOCK_M = 600, 9000
FINE_CLIP = 0.03          # m3/m3
SCENE_SD = 3.0            # a scene whose anomaly spreads this many times the track's median is left out
SIGMA_9KM = 0.017         # m3/m3, sd in time of SME2's own 9 km mean: turns the term into z-score units
RELIABILITY = (0.5, 0.25)  # agreement of the term between independent tracks: recommended cells, the others
Z_FINE_CLIP = 1.0
Z_SD_FLOOR, Z_ANOMALY_CLIP = 0.01, 5.0   # the whole anomaly of the newest pass as a z-score against the track's record
NISAR_FED = {"sm_surface": "fine", "sf_z12": "z", "rz_z12": "z"}   # same-day columns that get the term, and its unit
TRACK = re.compile(r"__(t\d{3}[ad])\d{3}$")


def series(lake, aoi, ds_id, day):
    items = catalog.search(lake, [ds_id], bbox=aoi.bbox, start=day - timedelta(days=REACH), end=day)
    return WeatherSeries(lake, load_dataset(ds_id), items)


def cells(grid, lon, lat):
    from pyproj import Transformer
    x, y = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True).transform(np.asarray(lon), np.asarray(lat))
    col = np.floor((x - grid.transform.c) / grid.transform.a).astype(int)
    row = np.floor((y - grid.transform.f) / grid.transform.e).astype(int)
    return row, col, (row >= 0) & (row < grid.height) & (col >= 0) & (col < grid.width)


def smooth(x, size, min_share=0.0):
    ok = np.isfinite(x)
    share = uniform_filter(ok.astype("float32"), size, mode="constant")
    mean = uniform_filter(np.where(ok, x, 0).astype("float32"), size, mode="constant") / np.maximum(share, 1e-6)
    return np.where(ok & (share >= min_share), mean, np.nan).astype("float32")


def not_recommended(client, it, shape):
    if "soil_moisture_quality_flag" not in it["assets"]:
        return np.zeros(shape, bool)
    f = np.nan_to_num(client.open_asset(it, "soil_moisture_quality_flag").values, nan=1).astype("int64")
    return ((f & 1) == 1) | ((f & 8) == 0)       # DSG is recommended when bit 0 is clear and bit 3 is set


def track_fine(client, aoi, passes, t, recommended_only):
    shape = aoi.grid.shape
    cell = lambda m: max(1, int(round(m / abs(aoi.grid.transform.a))) | 1)
    n, s, s2 = np.zeros(shape, "int16"), np.zeros(shape, "float32"), np.zeros(shape, "float32")
    spread, recent = [], []
    for o, items in sorted(passes.items()):
        a, bad = np.full(shape, np.nan, "float32"), np.zeros(shape, bool)
        for it in items:                                   # frames of one pass overlap by a strip
            b = client.open_asset(it, "soil_moisture").values
            new = np.isfinite(b) & ~np.isfinite(a)
            a[new] = b[new]
            if recommended_only or o >= t - PASS_DAYS:
                bad |= new & not_recommended(client, it, shape)
        ok = (a > SME2_VALID[0]) & (a < SME2_VALID[1]) & ~(bad & recommended_only)
        known = ok & (n >= MIN_SCENES)
        if known.any():
            with np.errstate(invalid="ignore", divide="ignore"):
                mean = s / n
                anomaly = np.where(known, a - mean, np.nan).astype("float32")
                z = anomaly / np.maximum(np.sqrt(np.maximum(s2 / n - mean ** 2, 0)), Z_SD_FLOOR)
            spread.append(float(np.nanstd(anomaly)))
            if o >= t - PASS_DAYS:
                recent.append((o, anomaly, bad, spread[-1], z if o >= t - SME2_WINDOW else None))
        n[ok] += 1
        s[ok] += a[ok]
        s2[ok] += a[ok] ** 2
    total, count = np.zeros(shape, "float32"), np.zeros(shape, "int8")
    newest, flagged = np.zeros(shape, "int32"), np.zeros(shape, bool)
    z_new = np.full(shape, np.nan, "float32")
    for o, anomaly, bad, sd, z in recent:
        if sd > SCENE_SD * np.median(spread):
            continue
        fine = smooth(anomaly, cell(FINE_M)) - smooth(anomaly, cell(BLOCK_M), 0.25)
        k = np.isfinite(fine)
        total[k] += fine[k]
        count[k] += 1
        newest[k], flagged[k] = o, bad[k]
        if z is not None:
            z_new = np.where(np.isfinite(z), np.clip(z, -Z_ANOMALY_CLIP, Z_ANOMALY_CLIP), z_new).astype("float32")
    fresh = (count > 0) & (newest >= t - SME2_WINDOW)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(fresh, total / count, np.nan).astype("float32"), flagged & fresh, z_new, newest


def nisar_fine(client, aoi, target, recommended_only=False):
    items = client.search("nisar-sme2", aoi=aoi.id, start=target - timedelta(days=HISTORY_DAYS), end=target,
                          allow_mixed_maturity=True)
    tracks = {}
    for it in items:
        m = TRACK.search(it["id"])
        o = catalog.item_interval(it)[0].date().toordinal()
        tracks.setdefault(m.group(1) if m else "", {}).setdefault(o, []).append(it)
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda p: track_fine(client, aoi, p, target.toordinal(), recommended_only), tracks.values()))
    ok = [np.isfinite(r[0]) for r in res]
    count = np.sum(ok, axis=0)
    if not count.any():
        return None
    with np.errstate(invalid="ignore", divide="ignore"):
        fine = np.sum([np.where(k, r[0], 0) for r, k in zip(res, ok)], axis=0) / count
    fine = np.clip(np.where(count > 0, fine, np.nan), -FINE_CLIP, FINE_CLIP).astype("float32")
    flagged = np.any([r[1] for r in res], axis=0)
    anomaly, day = np.full(aoi.grid.shape, np.nan, "float32"), np.zeros(aoi.grid.shape, "int32")
    for _, _, z_new, newest in res:                        # where tracks overlap, the most recent pass
        k = np.isfinite(z_new) & (newest > day)
        anomaly[k], day[k] = z_new[k], newest[k]
    z = np.clip(np.where(flagged, RELIABILITY[1], RELIABILITY[0]) * fine / SIGMA_9KM, -Z_FINE_CLIP, Z_FINE_CLIP).astype("float32")
    return dict(fine=fine, z=z, anomaly=anomaly, flagged=flagged, items=[it["id"] for it in items],
                tracks=int(sum(k.any() for k in ok)))


def valid_from(item):
    m = RELEASE.search(item["id"])
    if not m or item["id"].startswith("landfire-topo"):          # terrain does not change with the release
        return date.min
    return date.fromisoformat(data.LANDFIRE_FROM[int(m.group(1))])   # a release without an entry there must get one first


def releases(client, coll, aoi_id):
    return client.search(coll, aoi=aoi_id)                       # empty for a collection the lake does not have


def read(client, item, key):
    da = client.open_asset(item, key)
    a = da.values.astype("float32")
    if da.rio.nodata is not None and np.isfinite(da.rio.nodata):
        a[a == da.rio.nodata] = np.nan
    return a


def to_cells(raw, grid):
    # means over data.CELL_M windows on grids finer than that; the direction of slope as a circular mean
    size = int(round(data.CELL_M / abs(grid.transform.a))) | 1
    if "burnable" in raw and "burnable_share" not in raw:
        raw["burnable_share"] = raw["burnable"]
    if size < 3:
        return raw
    out = {k: smooth(np.where(np.isfinite(a), a, np.nan), size) for k, a in raw.items() if k != "aspect"}
    for k in out:
        out[k] = np.where(np.isfinite(out[k]), out[k], smooth_fill(raw[k], size))
    if "aspect" in raw:
        rad = np.deg2rad(np.where(raw["aspect"] >= 0, raw["aspect"], np.nan))
        sin, cos = smooth_fill(np.sin(rad), size), smooth_fill(np.cos(rad), size)
        out["aspect"] = np.where(np.isfinite(sin), np.mod(np.rad2deg(np.arctan2(sin, cos)), 360), np.nan).astype("float32")
    if "burnable" in out:
        out["burnable"] = (out["burnable_share"] >= 0.5).astype("float32")
    return out


def smooth_fill(x, size):
    ok = np.isfinite(x)
    share = uniform_filter(ok.astype("float32"), size, mode="constant")
    mean = uniform_filter(np.where(ok, x, 0).astype("float32"), size, mode="constant") / np.maximum(share, 1e-6)
    return np.where(share > 0, mean, np.nan).astype("float32")


def prefire(client, aoi_id):
    # vegetation class (ml.fuelage) of the fuel model in the oldest LANDFIRE release, by majority over a 200 m cell
    items = sorted(client.search("landfire-fbfm40", aoi=aoi_id), key=valid_from)
    if not items:
        return None
    veg = fuelage.vegetation(read(client, items[0], "fuel_model"))
    size = int(round(data.CELL_M / abs(load_aoi(aoi_id).grid.transform.a))) | 1
    if size < 3:
        return veg.astype("int8")
    share = np.stack([uniform_filter((veg == k).astype("float32"), size, mode="constant") for k in range(4)])
    return share.argmax(axis=0).astype("int8")


def statics(client, aoi_id, day, cols):
    need = {"aspect" if c in ("northness", "eastness") else c for c in cols if c in data.STATIC + data.EVT}
    raw = {}
    for coll, keys in data.STATIC_ASSETS.items() if need else []:
        items = [it for it in releases(client, coll, aoi_id) if valid_from(it) <= day]
        if items:
            it = max(items, key=valid_from)
            raw.update({k: read(client, it, k) for k in keys if k in it["assets"] and (k in need or k == "burnable")})
    pixel = raw.get("burnable")
    out = to_cells(raw, load_aoi(aoi_id).grid)
    if need - set(out):
        raise SystemExit(f"static layers missing on {aoi_id}: {sorted(need - set(out))}; use the core models")
    return {k: out[k] for k in need}, pixel


def arrays(client, aoi_id, day, cols, soil="nisar", recommended_only=False):
    aoi = load_aoi(aoi_id)
    region = lake_region()
    mask = region.pixel_mask(aoi.grid) if region else np.ones(aoi.grid.shape, bool)
    out, info = {}, {"soil": soil, "nisar": {}, "sme2_items": [], "smap": {}, "fine": None, "anomaly": None, "burnable": None}
    for ds_id, names in FORM_B.items():
        s = series(client.lake, aoi, ds_id, day)
        for c in [c for c in cols if split(c)[0] in names]:
            name, lag = split(c)
            out[c] = s.grid_slice(name, day - timedelta(days=lag), aoi.grid)
            if out[c] is None:
                raise SystemExit(f"{ds_id} {day - timedelta(days=lag)} is not staged (needed for {c})")
    fields = daily.fields(client.lake, cols, aoi.bbox, day, day)
    for c in [c for c in cols if split(c)[0] in fields]:
        out[c] = fields[split(c)[0]].on_grid(day - timedelta(days=split(c)[1]), aoi.grid)
        if out[c] is None:
            raise SystemExit(f"{data.SERIES[split(c)[0]][0]} {day - timedelta(days=split(c)[1])} is not staged (needed for {c})")
    fed = {c: unit for c, unit in NISAR_FED.items() if c in out}
    res = nisar_fine(client, aoi, day, recommended_only) if soil == "nisar" and fed else None
    for c, unit in fed.items() if res else []:
        use = np.isfinite(res[unit]) & np.isfinite(out[c]) & mask
        if not use.any():
            continue
        lo, hi = (0, 0.9) if unit == "fine" else (-daily.Z_CLIP, daily.Z_CLIP)
        info["smap"][c] = out[c]
        out[c] = np.where(use, np.clip(out[c] + res[unit], lo, hi), out[c]).astype("float32")
        info["nisar"][c] = dict(share=float(use.sum() / mask.sum()), scenes=len(res["items"]), tracks=res["tracks"],
                                mean_abs_term=float(np.abs(res[unit][use]).mean()),
                                not_recommended_share=float((res["flagged"] & use).sum() / use.sum()))
        info["sme2_items"], info["fine"] = res["items"], np.where(use, res["fine"], np.nan)
        info["anomaly"] = res["anomaly"]
    static, info["burnable"] = statics(client, aoi_id, day, cols)
    out.update(static)
    from . import context
    out.update(context.on_grid(client, aoi_id, day, cols))
    return out, mask, info


def frame(arr, mask, day):
    return data.add_derived(pd.DataFrame({c: a[mask] for c, a in arr.items()}), day)


def check(client, table, aoi_id, n_days=3):
    pts = data.load_points(table)
    g = load_aoi(aoi_id).grid
    row, col, inside = cells(g, pts["lon"], pts["lat"])
    pts = pts.assign(row=row, col=col)[inside]
    cols = [c for c in data.WEATHER + data.SOIL + data.SERIES_ALL + data.STATIC + data.EVT + data.CONTEXT if c in pts]
    pts = data.add_derived(pts)
    diffs = []
    for day in pts["date"].value_counts().index[:n_days]:
        rows = pts[pts["date"] == day]
        arr, _, _ = arrays(client, aoi_id, pd.Timestamp(day).date(), cols, "smap")
        got = pd.DataFrame({c: a[rows["row"], rows["col"]] for c, a in arr.items()})
        got = data.add_derived(got, day).reindex(columns=cols)
        del arr
        diffs.append((got - rows[cols].astype(float).reset_index(drop=True)).abs())
    d = pd.concat(diffs)
    print(pd.DataFrame({"max_abs_diff": d.max(), "median": d.median(), "table_mean": pts[cols].astype(float).mean(),
                        "n": d.notna().sum()}).round(4).to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", required=True, help="training table parquet")
    ap.add_argument("--aoi", default="ca")
    args = ap.parse_args()
    check(LakeClient.from_env(), args.check, args.aoi)
