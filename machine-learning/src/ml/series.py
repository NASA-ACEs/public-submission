import argparse
from datetime import date, timedelta

import numpy as np
import pandas as pd
from fuellake import catalog
from fuellake.features._common import WeatherSeries, bilinear, warp_array
from fuellake.registry import load_dataset

from . import data
from .data import split

MIN_SHARE = 0.8           # share of the window that must have data
SD_FLOOR = {"z": 0.005}   # m3/m3: a cell whose soil moisture barely moves gets no large z-scores
Z_CLIP = 6.0


def window_sum(x, w):
    # sum and count of the finite values on the w days before each day (the day itself excluded)
    ok = np.isfinite(x)
    pad = np.zeros((1,) + x.shape[1:])
    s = np.concatenate([pad, np.cumsum(np.where(ok, x, 0), axis=0, dtype="float64")])
    n = np.concatenate([pad, np.cumsum(ok, axis=0, dtype="float64")])
    t = np.arange(len(x))
    lo = np.maximum(t - w, 0)
    return s[t] - s[lo], n[t] - n[lo]


def derive(x, op, w):
    if op == "max":       # the day and the w - 1 days before it
        out = pd.DataFrame(x.reshape(len(x), -1)).rolling(w, min_periods=1).max().to_numpy()
        return out.reshape(x.shape).astype("float32")
    x = x.astype("float64")
    centre = np.nanmean(x)
    s, n = window_sum(x - centre, w)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(n >= MIN_SHARE * w, s / n, np.nan)
        if op == "sum":
            return ((mean + centre) * w).astype("float32")
        if op == "mean":
            return (mean + centre).astype("float32")
        s2, _ = window_sum((x - centre) ** 2, w)
        sd = np.sqrt(np.maximum(s2 / n - mean ** 2, 0))
        z = (x - centre - mean) / np.maximum(sd, SD_FLOOR["z"])
    return np.clip(z, -Z_CLIP, Z_CLIP).astype("float32")


class Field:
    """One derived daily variable on its dataset's native grid."""

    def __init__(self, lake, name, bbox, start, end):
        ds_id, var, op, w = data.SERIES[name]
        ds = load_dataset(ds_id)
        ds = ds.model_copy(update={"variables": [v for v in ds.variables if v.name == var]})
        items = catalog.search(lake, [ds_id], bbox=bbox, start=start - timedelta(days=w + 1), end=end)
        stores = [st for st in WeatherSeries(lake, ds, items).stores if var in st.data]
        if not stores:
            raise SystemExit(f"{ds_id} {var} is not staged for {start}..{end}")
        cells = max(st.data[var][0].size for st in stores)          # the widest grid: the state AOI
        stores = [st for st in stores if st.data[var][0].size == cells]
        self.crs, self.transform = stores[0].crs, stores[0].transform
        first = min(min(st.days) for st in stores)
        self.day0 = first.toordinal()
        x = np.full((max(max(st.days) for st in stores).toordinal() - self.day0 + 1,) + stores[0].data[var].shape[1:],
                    np.nan, "float32")
        for st in stores:
            for d, steps in st.days.items():
                x[d.toordinal() - self.day0] = st.data[var][steps].mean(axis=0)
        self.values = derive(x, op, w)

    def slice(self, day):
        i = day.toordinal() - self.day0
        return self.values[i] if 0 <= i < len(self.values) else None

    def on_grid(self, day, grid):
        a = self.slice(day)
        return None if a is None else warp_array(a, self.crs, self.transform, grid, "bilinear")

    def at_points(self, days, lon, lat):
        from pyproj import Transformer
        x, y = Transformer.from_crs("EPSG:4326", self.crs, always_xy=True).transform(np.asarray(lon), np.asarray(lat))
        out = np.full(len(x), np.nan, "float32")
        for o, idx in pd.Series(np.arange(len(x))).groupby(np.asarray(days)).indices.items():
            a = self.slice(date.fromordinal(int(o)))
            if a is not None:
                out[idx] = bilinear(a, self.transform, x[idx], y[idx])
        return out


def fields(lake, cols, bbox, start, end, optional=False):
    names = sorted({split(c)[0] for c in cols} & set(data.SERIES))
    lag = max([split(c)[1] for c in cols if split(c)[0] in names], default=0)
    out = {}
    for n in names:
        try:
            out[n] = Field(lake, n, bbox, start - timedelta(days=lag), end)
        except SystemExit as e:           # a table may be built without a dataset: its features are then left out
            if not optional:
                raise
            print(f"skipped {n}: {e}")
    return out


def statics_at_points(client, aoi_id, pts):
    # terrain, fuels and canopy of the 200 m cell each point lies in, from the release valid on the row's date
    from fuellake.registry import load_aoi

    from . import grid
    g = load_aoi(aoi_id).grid
    row, col, inside = grid.cells(g, pts["lon"], pts["lat"])
    day = pd.to_datetime(pts["date"]).dt.date.to_numpy()
    out = {}
    for coll, keys in data.STATIC_ASSETS.items():
        for it in sorted(grid.releases(client, coll, aoi_id), key=grid.valid_from):
            sel = inside & (day >= grid.valid_from(it))
            cells = grid.to_cells({k: grid.read(client, it, k) for k in keys if k in it["assets"]}, g)
            for k, a in cells.items():
                out.setdefault(k, np.full(len(pts), np.nan, "float32"))[sel] = a[row[sel], col[sel]]
    before = grid.prefire(client, aoi_id)                 # what grew there before the fires on record
    if before is not None:
        out["prefire_vegetation"] = np.where(inside, before[np.clip(row, 0, g.height - 1), np.clip(col, 0, g.width - 1)], 0).astype("int8")
    return out


def add_to_table(client, pts, cols=None, statics=None):
    from . import fuelage
    lake, cols = client.lake, cols or data.SERIES_ALL
    day = pd.to_datetime(pts["date"]).map(date.toordinal).to_numpy()
    pad = 0.2
    bbox = (pts["lon"].min() - pad, pts["lat"].min() - pad, pts["lon"].max() + pad, pts["lat"].max() + pad)
    f = fields(lake, cols, bbox, date.fromordinal(int(day.min())), date.fromordinal(int(day.max())), optional=True)
    new = {c: f[split(c)[0]].at_points(day - split(c)[1], pts["lon"], pts["lat"]) for c in cols if split(c)[0] in f}
    new["years_since_fire"] = fuelage.at_points(fuelage.perimeters(client), pts["lon"], pts["lat"], pts["date"])
    if statics:
        from . import context
        new.update(statics_at_points(client, statics, pts))
        new.update(context.at_points(client, pts))
    return pts.assign(**new)


if __name__ == "__main__":
    from fuellake.client import LakeClient
    ap = argparse.ArgumentParser(description="add the features of ml.data.SERIES to a point table")
    ap.add_argument("--table", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--statics", help="AOI whose LANDFIRE layers replace the table's point values, e.g. ca")
    args = ap.parse_args()
    out = add_to_table(LakeClient.from_env(), pd.read_parquet(args.table), statics=args.statics)
    out.to_parquet(args.out)
    made = [c for c in data.SERIES_ALL if c in out]
    print(out[made].notna().mean().round(3).to_string())
    print(out[made].describe().loc[["mean", "std", "min", "max"]].round(3).T.to_string())
    print("inside a perimeter of the last 10 years:", round(float((out["years_since_fire"] < 10).mean()), 4))
