import os

import numpy as np
import pandas as pd
import scipy.fft as sf
from scipy.ndimage import distance_transform_edt
from fuellake.registry import load_aoi

from . import data, grid

AOI = "ca"                             # the discs are taken on this 200 m grid, for a table and for a map of any AOI
RADII = {"r1km": 5, "r3km": 15}        # cells
FOREST = ["oak_woodland", "other_hardwood", "conifer_oak", "dry_conifer", "mesic_conifer", "juniper_pinyon"]
SHRUB = ["chaparral", "coastal_scrub", "sagebrush", "desert_scrub", "other_shrub"]
COVER = {"landfire-evt": ["evt_share_developed", "evt_share_agriculture"] + [f"evt_share_{k}" for k in FOREST + SHRUB],
         "landfire-fuel-load": ["burnable_share", "total_load"], "landfire-canopy": ["canopy_cover"]}
THREADS = min(16, os.cpu_count() or 1)


def disc_mean(a, r):
    # mean over the cells of the disc that have data, where the centre has data
    y, x = np.mgrid[-r:r + 1, -r:r + 1]
    pad = [sf.next_fast_len(n + 2 * r, real=True) for n in a.shape]
    k = sf.rfft2((x * x + y * y <= r * r).astype("float64"), pad, workers=THREADS)

    def total(b):
        return sf.irfft2(sf.rfft2(b, pad, workers=THREADS) * k, pad, workers=THREADS)[r:r + a.shape[0], r:r + a.shape[1]]

    ok = np.isfinite(a)
    n = np.rint(total(ok.astype("float64")))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(ok & (n > 0), total(np.where(ok, a, 0.0).astype("float64")) / n, np.nan)


def tidy(a):
    return (np.round(a, 5) + 0.0).astype("float32")              # the transform leaves noise around zero


def terrain(client):
    elev = grid.read(client, grid.releases(client, "landfire-topo", AOI)[0], "elevation").astype("float64")
    m1, m3 = disc_mean(elev, RADII["r1km"]), disc_mean(elev, RADII["r3km"])
    sd = np.sqrt(np.maximum(disc_mean(elev * elev, RADII["r1km"]) - m1 * m1, 0))
    return {"elevation_sd_r1km": tidy(sd), "tpi_r1km": tidy(elev - m1), "tpi_r3km": tidy(elev - m3)}


def cover(client, day):
    raw = {}
    for coll, keys in COVER.items():
        items = [it for it in grid.releases(client, coll, AOI) if grid.valid_from(it) <= day]
        if not items:
            return {}
        it = max(items, key=grid.valid_from)
        raw.update({k: grid.read(client, it, k) for k in keys})
    src = {"evt_share_developed": raw["evt_share_developed"], "evt_share_agriculture": raw["evt_share_agriculture"],
           "burnable": raw["burnable_share"], "total_load": raw["total_load"], "canopy_cover": raw["canopy_cover"],
           "forest_share": sum(raw[f"evt_share_{k}"] for k in FOREST), "shrub_share": sum(raw[f"evt_share_{k}"] for k in SHRUB)}
    return {f"{name}_{k}": tidy(disc_mean(a.astype("float64"), r)) for name, a in src.items() for k, r in RADII.items()}


def at_points(client, pts):
    if not grid.releases(client, "landfire-evt", AOI):
        return {}
    row, col, inside = grid.cells(load_aoi(AOI).grid, pts["lon"], pts["lat"])
    day = pd.to_datetime(pts["date"]).dt.date.to_numpy()
    out = {c: np.full(len(pts), np.nan, "float32") for c in data.CONTEXT}
    for c, a in terrain(client).items():
        out[c][inside] = a[row[inside], col[inside]]
    for start in sorted({grid.valid_from(it) for coll in COVER for it in grid.releases(client, coll, AOI)}):
        sel = inside & (day >= start)
        for c, a in cover(client, start).items():
            out[c][sel] = a[row[sel], col[sel]]
    return out


def on_grid(client, aoi_id, day, cols):
    want = [c for c in cols if c in data.CONTEXT]
    if not want:
        return {}
    got = {**terrain(client), **cover(client, day)}
    if set(want) - set(got):
        raise SystemExit(f"context layers missing on {AOI}: {sorted(set(want) - set(got))}; use models without them")
    if aoi_id == AOI:
        return {c: got[c] for c in want}
    from pyproj import Transformer
    g = load_aoi(aoi_id).grid
    x, y = np.meshgrid(g.transform.c + g.transform.a * (np.arange(g.width) + 0.5), g.transform.f + g.transform.e * (np.arange(g.height) + 0.5))
    lon, lat = Transformer.from_crs(g.crs, "EPSG:4326", always_xy=True).transform(x, y)
    row, col, inside = grid.cells(load_aoi(AOI).grid, lon, lat)
    row, col = np.where(inside, row, 0), np.where(inside, col, 0)
    out = {}
    for c in want:
        # a cell at the edge of the California grid (the coast) takes the value of the nearest cell that has one:
        # the models know a missing surrounding only from the rows of 2016
        a = np.where(inside, got[c][row, col], np.nan)
        near = distance_transform_edt(np.isnan(a), return_distances=False, return_indices=True)
        out[c] = a[tuple(near)].astype("float32")
    return out
