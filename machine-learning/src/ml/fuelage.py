import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

# Fuel age: how much less a recently burned cell carries fire (docs/research/time-since-fire.md,
# fuel-age-by-vegetation.md, out-of-time.md). What held in 2016-2024 and again in 2025-2026:
# - scars younger than about 3.5 years: no ignition became a large fire in 715 (13.6 expected);
# - former forest stays protected for about 7 years (cells of its scars burn 0.26-0.46 times as often as
#   neighbouring land); grass and open land for less than 2 years. A single curve for all vegetation, fading
#   over 2-3 years, understated risk in old scars on open land in 2025-2026.
CAP = 10.0            # years; also the value where no fire is recorded (the record starts in 2015)
MIN_GAP_DAYS = 30     # a fire counts as earlier only if it ended this long before the date
YOUNG, FADE = 3.5, 1.0                                # every scar: odds x exp(-beta exp(-years / FADE)) until YOUNG
FOREST_UNTIL, FOREST_FADE, FOREST_BETA = 7.0, 3.0, 3.4  # former forest: at most exp(-FOREST_BETA exp(-years / 3))
BETA_MAX = 12.0
FOREST, SHRUB, GRASS = 1, 2, 3
BURN_TAU, BURN_FLOOR = {FOREST: 7.0, SHRUB: 2.3, GRASS: 1.3, 0: 3.1}, 0.1


def perimeters(client):
    d = client.read_table("fire_perimeters")
    g = gpd.GeoDataFrame(d.drop(columns="geometry"), geometry=shapely.make_valid(d["geometry"].to_numpy()), crs="EPSG:4326")
    g = g[~g.geometry.is_empty & g.geometry.notna()].copy()
    start = pd.to_datetime(g["start_date"])
    end = pd.to_datetime(g["containment_date"]).fillna(start)
    bad = (end < start) | ((end - start).dt.days > 400)       # a data error: fall back to the start
    g["end"] = end.where(~bad, start)
    return g[start.notna()].reset_index(drop=True)


def at_points(perims, lon, lat, dates):
    pts = gpd.GeoDataFrame({"i": np.arange(len(lon)), "date": pd.to_datetime(np.asarray(dates))},
                           geometry=gpd.points_from_xy(np.asarray(lon, float), np.asarray(lat, float)), crs="EPSG:4326")
    j = gpd.sjoin(pts, perims[["end", "geometry"]], predicate="within", how="inner")
    gap = (j["date"] - j["end"]).dt.days
    j = j.assign(years=gap / 365.25)[gap >= MIN_GAP_DAYS]
    out = np.full(len(pts), CAP, "float32")
    m = j.groupby("i")["years"].min()
    out[m.index.to_numpy()] = np.minimum(m.to_numpy(), CAP)
    return out


def on_grid(perims, day, grid):
    from rasterio.features import rasterize
    day = pd.Timestamp(day)
    p = perims[(day - perims["end"]).dt.days >= MIN_GAP_DAYS].to_crs(grid.crs)
    years = ((day - p["end"]).dt.days / 365.25).clip(upper=CAP).to_numpy()
    order = np.argsort(-years)                                 # oldest first: the newest fire is written last
    shapes = [(g, float(y)) for g, y in zip(p.geometry.values[order], years[order]) if not g.is_empty]
    if not shapes:
        return np.full(grid.shape, CAP, "float32")
    return rasterize(shapes, out_shape=grid.shape, transform=grid.transform, fill=CAP, dtype="float32")


def vegetation(code):
    # class of an FBFM40 code: the fuel model of the oldest LANDFIRE release stands for what grew before the fire
    c = np.nan_to_num(np.asarray(code, "float64"))
    return np.select([(c >= 161) & (c <= 204), ((c >= 121) & (c <= 124)) | ((c >= 141) & (c <= 149)), (c >= 101) & (c <= 109)],
                     [FOREST, SHRUB, GRASS], 0)


def odds(years, beta, veg=None):
    # multiplier on the odds that an ignition becomes a large fire; 1 where no fire is recorded
    y = np.asarray(years, "float64")
    m = np.where(y < YOUNG, np.exp(-beta * np.exp(-y / FADE)), 1.0)
    if veg is not None:
        forest = (np.asarray(veg) == FOREST) & (y < FOREST_UNTIL)
        m = np.where(forest, np.minimum(m, np.exp(-FOREST_BETA * np.exp(-y / FOREST_FADE))), m)
    return m


def apply(p, years, beta, veg=None):
    m = odds(years, beta, veg)
    return p * m / (1 - p + p * m)


def fit(p, years, y, veg=None):
    # beta that minimises the log loss of apply(); p are probabilities from years the model did not see
    from scipy.optimize import minimize_scalar
    scar = np.asarray(years) < YOUNG
    if not scar.any():
        return 0.0
    p, years, y = np.clip(p[scar], 1e-7, 1 - 1e-7), np.asarray(years)[scar], np.asarray(y)[scar]
    veg = None if veg is None else np.asarray(veg)[scar]

    def loss(beta):
        q = np.clip(apply(p, years, beta, veg), 1e-9, 1 - 1e-9)
        return -(y * np.log(q) + (1 - y) * np.log(1 - q)).sum()

    return float(minimize_scalar(loss, bounds=(0, BETA_MAX), method="bounded").x)


def permeability(years, veg=None):
    # relative probability that a cell burns when a large fire reaches its surroundings (410 fires, 2017-2025)
    y = np.asarray(years, "float64")
    tau = np.full(y.shape, BURN_TAU[0]) if veg is None else np.select([np.asarray(veg) == k for k in (FOREST, SHRUB, GRASS)],
                                                                      [BURN_TAU[k] for k in (FOREST, SHRUB, GRASS)], BURN_TAU[0])
    return np.where(y >= CAP, 1.0, np.clip(1 - np.exp(-y / tau), BURN_FLOOR, 1.0)).astype("float32")
