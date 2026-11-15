import re
import warnings
from datetime import timedelta

import numpy as np
import pandas as pd
from fuellake import catalog
from fuellake.registry import load_aoi
from scipy.ndimage import uniform_filter

DAYS, MIN_SCENES = 365, 8
SHRUB = (141, 149)                      # FBFM40 SH codes: the cover the terrain fit is made in
SLOPE_BINS, U_BINS = [10, 20, 30], [-0.3, -0.1, 0.1, 0.3]
MIN_REFERENCE = 30                      # unburned cells a stratum needs
CLIP = (0.3, 1.2)
TRACK = re.compile(r"__(t\d{3}[ad])\d{3}$")


def layer(client, coll, aoi_id, key, first=False):
    items = client.search(coll, aoi=aoi_id)
    if not items:
        raise SystemExit(f"{coll} is not staged on {aoi_id} (needed for the GCOV fuel state)")
    da = client.open_asset(items[0 if first else -1], key)
    a = da.values.astype("float32")
    if da.rio.nodata is not None and np.isfinite(da.rio.nodata):
        a[a == da.rio.nodata] = np.nan
    return a


def track_means(client, aoi_id, day):
    tracks = {}
    for it in client.search("nisar-gcov", aoi=aoi_id, start=day - timedelta(days=DAYS), end=day, allow_mixed_maturity=True):
        m = TRACK.search(it["id"])
        tracks.setdefault(m.group(1) if m else "", []).append(it)
    out = {}
    for t, items in tracks.items():
        if len(items) < MIN_SCENES:
            continue
        power = np.stack([10 ** (client.open_asset(it, "gamma0_hv_db").values / 10) for it in items])
        n = np.isfinite(power).sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[t] = np.where(n >= MIN_SCENES, 10 * np.log10(np.nansum(power, axis=0) / n), np.nan).astype("float32")
    return out, [it["id"] for items in tracks.values() for it in items]


def flatten(hv, slope, aspect, shrub):
    # L-band backscatter depends on how a slope faces the radar; no layover mask exists in the product. Find the
    # facing azimuth in shrub cells, remove the quadratic dependence on u = sin(slope) cos(aspect - azimuth).
    flat = np.isfinite(aspect) & (aspect >= 0)
    m = shrub & np.isfinite(hv) & flat

    def facing(phi):
        return np.where(flat, np.sin(np.deg2rad(slope)) * np.cos(np.deg2rad(aspect - phi)), 0.0)

    phi = max(range(0, 360, 5), key=lambda p: np.corrcoef(hv[m], facing(p)[m])[0, 1])
    u = facing(phi)
    c = np.polyfit(u[m], hv[m], 2)
    return hv - (np.polyval(c, u) - c[2]), u, phi


def burned(client, aoi, day):
    import rasterio.features
    from pyproj import Transformer
    from shapely.ops import transform
    p = client.read_table("fire_perimeters")
    p = p[pd.to_datetime(p["start_date"]) < pd.Timestamp(day)]
    to_grid = Transformer.from_crs("EPSG:4326", aoi.grid.crs, always_xy=True).transform
    w, s, e, n = aoi.bbox
    shapes = [transform(to_grid, g) for g in p["geometry"] if g is not None and g.bounds[0] < e and g.bounds[2] > w
              and g.bounds[1] < n and g.bounds[3] > s]
    if not shapes:
        return np.zeros(aoi.grid.shape, bool)
    return rasterio.features.rasterize(shapes, out_shape=aoi.grid.shape, transform=aoi.grid.transform, dtype="uint8").astype(bool)


def fuel_state(client, aoi_id, day):
    """L-band HV power of a cell against unburned cells of the same pre-fire fuel model and terrain: 1 = mature
    fuel, below 1 = regrowth after fire (`state`); `state_now` is the same against the current fuel model, the
    factor by which that model's woody fuel is overstated. None where GCOV is not staged."""
    means, items = track_means(client, aoi_id, day)
    if not means:
        return None
    aoi = load_aoi(aoi_id)
    slope, aspect, elev = (layer(client, "landfire-topo", aoi_id, k) for k in ("slope", "aspect", "elevation"))
    now, before = layer(client, "landfire-fbfm40", aoi_id, "fuel_model"), layer(client, "landfire-fbfm40", aoi_id, "fuel_model", first=True)
    shrub = (now >= SHRUB[0]) & (now <= SHRUB[1])
    flat = [flatten(hv, slope, aspect, shrub) for hv in means.values()]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        hv = np.nanmean([f[0] for f in flat], axis=0)
    u = flat[0][1]
    scars = burned(client, aoi, day)

    def deficit(code):
        ok = np.isfinite(hv) & np.isfinite(code) & np.isfinite(slope) & np.isfinite(elev) & (code > 99)
        strata = (np.nan_to_num(code).astype("int64") * 1000 + np.digitize(slope, SLOPE_BINS) * 100
                  + np.digitize(u, U_BINS) * 10 + np.digitize(elev, np.nanpercentile(elev[ok], [33, 66])))
        ref = ok & ~scars
        g = pd.DataFrame({"s": strata[ref], "v": hv[ref]}).groupby("s")["v"].agg(["mean", "size"])
        d = np.full(hv.shape, np.nan, "float32")
        d[ok] = hv[ok] - pd.Series(strata[ok]).map(g["mean"][g["size"] >= MIN_REFERENCE]).to_numpy()
        good = np.isfinite(d)
        share = uniform_filter(good.astype("float32"), 3, mode="constant")
        return np.where(good, uniform_filter(np.where(good, d, 0), 3, mode="constant") / np.maximum(share, 1e-6), np.nan).astype("float32")

    db = deficit(before)
    ratio = lambda d: np.clip(10 ** (d / 10), *CLIP).astype("float32")
    good = np.isfinite(db)
    return dict(state=ratio(db), state_now=ratio(deficit(now)), deficit_db=db, items=items,
                tracks={t: int(f[2]) for t, f in zip(means, flat)}, reference_cells=int((good & ~scars).sum()),
                unburned_median=float(np.nanmedian(10 ** (db[good & ~scars] / 10))),
                burned_median=float(np.nanmedian(10 ** (db[good & scars] / 10))) if (good & scars).any() else None)
