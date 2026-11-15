"""Fire growth from an ignition point by minimum travel time over a grid of Rothermel head-fire ellipses.

components()  per cell, everything of Rothermel's surface spread that does not depend on the wind
head()        head-fire rate, direction and ellipse shape for any wind, from those components
arrival()     minutes until the fire reaches each cell from the start cells (Dijkstra on an 8-, 16- or 32-neighbour graph)
isochrones()  GeoJSON polygons (WGS84) of the area reached after given hours
simulate()    the same from a result bundle (directory of GeoTIFFs) or a dict of arrays, for any wind

components/head/arrival need numpy and scipy only (components also the sibling rothermel.py). isochrones and
simulate read GeoTIFFs and reproject with rasterio. Surface fire only: no crown fire, no spotting, one wind for the
whole run, no suppression, no change of weather during the run.
"""
import numpy as np
from scipy.ndimage import binary_opening, distance_transform_edt
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

FT = 0.3048
MPH_PER_MS = 60 * 0.0372840909091          # the constant rothermel.spread uses, per m/s
FLAME = 0.07747042253266703                # Byram 1959: flame length (m) = FLAME * intensity (kW/m) ** 0.46
LAYERS = ("spread_ros0", "spread_wind_a", "spread_wind_b", "spread_slope_k", "spread_waf", "spread_wind_cap",
          "spread_intensity_k", "slope", "aspect")
OFFSETS = {8: [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]}
OFFSETS[16] = OFFSETS[8] + [(a * i, b * j) for i, j in ((1, 2), (2, 1)) for a in (-1, 1) for b in (-1, 1)]
OFFSETS[32] = OFFSETS[16] + [(a * i, b * j) for i, j in ((1, 3), (3, 1), (2, 3), (3, 2)) for a in (-1, 1) for b in (-1, 1)]


def components(code, rmin, tmmx, fm100, lfmc, green=0.33, canopy_cover=None, canopy_height=None, fuel_state=1.0):
    """Wind-independent layers of the head fire, from the inputs of rothermel.grid_layers (arrays of one shape).

    spread_ros0         m/min   rate of spread with no wind on flat ground (0 = not burnable, NaN = unknown fuel)
    spread_wind_a, _b   -       wind factor phi_w = a * U ** b, U = midflame wind in m/s
    spread_slope_k      -       slope factor phi_s = k * tan(slope) ** 2
    spread_waf          -       midflame wind = waf * wind at 10 m
    spread_wind_cap     m/s     effective midflame wind above which the rate no longer grows (Rothermel's limit)
    spread_intensity_k  kW/m per m/min: fireline intensity = k * rate of spread
    """
    try:
        from . import rothermel as ro
    except ImportError:                    # copied next to rothermel.py outside the package
        import rothermel as ro
    shape = np.shape(code)
    flat = lambda a: np.broadcast_to(np.asarray(a, "float64"), shape).ravel()
    code, rmin, tmmx, fm100, lfmc, green, state = map(flat, (code, rmin, tmmx, fm100, lfmc, green, fuel_state))
    m1, m10 = ro.dead_moisture(rmin, tmmx)
    f = ro.bed(code, m1, m10, fm100, ro.herb_moisture(green), lfmc, state)
    cc = None if canopy_cover is None else flat(canopy_cover)
    ch = None if canopy_height is None else flat(canopy_height)
    burn = f["ok"] & (f["r0"] > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = {"spread_ros0": f["r0"] * FT, "spread_wind_a": f["c_f"] * (60 / FT) ** f["b"], "spread_wind_b": f["b"],
               "spread_slope_k": 5.275 * f["beta"] ** -0.3, "spread_waf": ro.midflame_wind(1.0, code, cc, ch),
               "spread_wind_cap": 0.9 * f["i_r"] * FT / 60, "spread_intensity_k": f["i0"] / f["r0"] * ro.BTU_FT_S / FT}
    return {k: np.where(f["known"], np.where(burn, np.nan_to_num(v), 0.0), np.nan).astype("float32").reshape(shape)
            for k, v in out.items()}


def head(c, wind_10m, wind_from, slope=None, aspect=None):
    """Head fire for a wind (m/s at 10 m, degrees it blows from; scalars or arrays) from components().
    slope and aspect (degrees) default to the layers in c. Returns rate_of_spread (m/min), spread_direction
    (degrees), lw_ratio, fireline_intensity (kW/m), flame_length (m); the same numbers as rothermel.spread."""
    g = lambda k: np.asarray(c[k], "float64")
    slope = g("slope") if slope is None else np.asarray(slope, "float64")
    aspect = g("aspect") if aspect is None else np.asarray(aspect, "float64")
    r0, a, b, cap = g("spread_ros0"), g("spread_wind_a"), g("spread_wind_b"), g("spread_wind_cap")
    with np.errstate(divide="ignore", invalid="ignore"):
        tan = np.tan(np.deg2rad(np.nan_to_num(slope)))
        az_w, az_s = np.deg2rad(np.asarray(wind_from, "float64") + 180), np.deg2rad(np.nan_to_num(aspect) + 180)
        u = np.asarray(wind_10m, "float64") * g("spread_waf")
        wx, wy, sx, sy = u * np.sin(az_w), u * np.cos(az_w), tan * np.sin(az_s), tan * np.cos(az_s)
        wz = wx * sx + wy * sy
        u3 = np.sqrt(wx ** 2 + wy ** 2 + wz ** 2)
        phi_w, phi_s = a * u3 ** b, g("spread_slope_k") * tan ** 2
        s3 = np.sqrt(sx ** 2 + sy ** 2 + tan ** 4)
        kw, ks = np.where(u3 > 0, phi_w / u3, 0.0), np.where(s3 > 0, phi_s / s3, 0.0)
        ex, ey, ez = kw * wx + ks * sx, kw * wy + ks * sy, kw * wz + ks * tan ** 2
        phi = np.sqrt(ex ** 2 + ey ** 2 + ez ** 2)
        direction = np.where(phi > 0, np.rad2deg(np.arctan2(ex, ey)) % 360, 0.0)
        u_eff = (phi / a) ** (1 / b)
        over = u_eff > cap
        phi, u_eff = np.where(over, a * cap ** b, phi), np.where(over, cap, u_eff)
        mph = np.nan_to_num(u_eff) * MPH_PER_MS
        lw = np.minimum(8.0, 0.936 * np.exp(0.1147 * mph) + 0.461 * np.exp(-0.0692 * mph) - 0.397)
    burn = r0 > 0
    ros = np.where(burn, np.nan_to_num(r0 * (1 + phi)), np.where(np.isfinite(r0), 0.0, np.nan))
    inten = ros * g("spread_intensity_k")
    return {"rate_of_spread": ros, "spread_direction": np.where(burn, direction, 0.0), "lw_ratio": np.where(burn, lw, 1.0),
            "fireline_intensity": inten, "flame_length": FLAME * inten ** 0.46}


def directional_rate(ros, direction, lw, bearing):
    """Rate of spread (same unit as ros) towards `bearing` (degrees) of a fire whose head moves towards `direction`:
    an ellipse with the ignition at its rear focus, eccentricity from the length-to-width ratio (Andrews 2018,
    RMRS-GTR-371, eq. for spread from the ignition point)."""
    e = np.sqrt(np.maximum(lw ** 2 - 1, 0)) / lw
    return ros * (1 - e) / (1 - e * np.cos(np.deg2rad(bearing - direction)))


def bridge(ros, direction, lw, gap_cells, penalty=0.5):
    """Lets fire cross non-burnable strips up to gap_cells wide (roads, a stand-in for short-range spotting): such
    cells take the behaviour of the nearest burnable cell at `penalty` times its rate. Wider non-burnable areas stay
    barriers. gap_cells=0 leaves the inputs unchanged."""
    block = ~(np.isfinite(ros) & (ros > 0))
    if gap_cells <= 0 or not block.any() or block.all():
        return ros, direction, lw
    wide = binary_opening(block, np.ones((gap_cells + 1, gap_cells + 1), bool))
    thin = block & ~wide
    r, cidx = distance_transform_edt(block, return_distances=False, return_indices=True)
    fill = lambda a, k=1.0: np.where(thin, a[r, cidx] * k, a)
    return fill(ros, penalty), fill(direction), fill(lw)


def arrival(ros, direction, lw, cell_m, starts, limit_min, neighbours=16, permeability=None, gap_cells=0):
    """Minutes until the fire reaches each cell (inf = not within limit_min). ros in m/min; starts = [(row, col), ...];
    cell_m = ground size of a cell in metres, or (north-south, east-west) where the projection is not conformal.
    Travel time between neighbouring cells = half the distance at each cell's rate towards the other. Cells with
    ros <= 0 or NaN block the fire, except strips bridged by gap_cells. permeability (0-1) multiplies the rate: a
    simplification, it was measured as the chance that a cell burns, not as a rate."""
    ros, direction, lw = bridge(np.asarray(ros, "float64"), np.asarray(direction, "float64"), np.asarray(lw, "float64"), gap_cells)
    if permeability is not None:
        ros = ros * np.nan_to_num(np.asarray(permeability, "float64"), nan=1.0)
    ok = np.isfinite(ros) & (ros > 0)
    h, w = ros.shape
    cell_y, cell_x = cell_m if np.ndim(cell_m) else (cell_m, cell_m)
    idx = np.arange(h * w).reshape(h, w)
    src, dst, cost = [], [], []
    for dr, dc in OFFSETS[neighbours]:
        a = (slice(max(0, -dr), h - max(0, dr)), slice(max(0, -dc), w - max(0, dc)))     # from
        b = (slice(max(0, dr), h - max(0, -dr)), slice(max(0, dc), w - max(0, -dc)))     # to
        m = ok[a] & ok[b]
        if abs(dr) + abs(dc) == 2:                       # diagonal: not through a closed corner
            m &= ok[a[0], b[1]] | ok[b[0], a[1]]
        elif abs(dr) + abs(dc) > 2:                      # longer move: every cell of the box it spans must burn
            for i in range(0, dr + np.sign(dr), np.sign(dr)):
                for j in range(0, dc + np.sign(dc), np.sign(dc)):
                    m &= ok[max(0, -dr) + i:h - max(0, dr) + i, max(0, -dc) + j:w - max(0, dc) + j]
        bearing = np.rad2deg(np.arctan2(dc * cell_x, -dr * cell_y))
        with np.errstate(divide="ignore", invalid="ignore"):
            t = 0.5 * np.hypot(dr * cell_y, dc * cell_x) * (1 / directional_rate(ros[a], direction[a], lw[a], bearing)
                                                   + 1 / directional_rate(ros[b], direction[b], lw[b], bearing))
        src.append(idx[a][m]); dst.append(idx[b][m]); cost.append(t[m])
    g = coo_matrix((np.concatenate(cost), (np.concatenate(src), np.concatenate(dst))), shape=(h * w, h * w)).tocsr()
    first = [idx[snap(ok, r, c)] for r, c in starts]
    return dijkstra(g, directed=True, indices=first, limit=limit_min, min_only=True).reshape(h, w)


def snap(ok, r, c, reach=10):
    """The burnable cell nearest to (r, c) within `reach` cells (an ignition on a road starts beside it)."""
    r, c = int(r), int(c)
    if ok[r, c]:
        return r, c
    r0, c0 = max(0, r - reach), max(0, c - reach)
    win = ok[r0:r + reach + 1, c0:c + reach + 1]
    if not win.any():
        return r, c
    rr, cc = np.nonzero(win)
    k = np.argmin((rr + r0 - r) ** 2 + (cc + c0 - c) ** 2)
    return rr[k] + r0, cc[k] + c0


def isochrones(arrival_min, hours, transform, crs):
    """GeoJSON FeatureCollection (WGS84): one MultiPolygon per entry of `hours`, the cells reached by then, with
    area_ha. transform: affine of the arrival grid (rasterio.Affine), crs: its CRS."""
    from rasterio.features import shapes
    from rasterio.warp import transform_geom
    cell_ha = abs(transform.a * transform.e) / 1e4
    feats = []
    for hrs in sorted(hours, reverse=True):
        m = np.isfinite(arrival_min) & (arrival_min <= hrs * 60)
        polys = [g["coordinates"] for g, v in shapes(m.astype("uint8"), mask=m, transform=transform) if v == 1]
        geom = transform_geom(crs, "EPSG:4326", {"type": "MultiPolygon", "coordinates": polys}, precision=6) if polys else None
        feats.append({"type": "Feature", "geometry": geom, "properties": {"hours": hrs, "area_ha": round(float(m.sum() * cell_ha), 1),
                                                                         "cells": int(m.sum())}})
    return {"type": "FeatureCollection", "features": feats}


def ground_cell(transform, crs, lat):
    """Ground size (north-south, east-west) of a cell at a latitude. EASE-Grid 2.0 (EPSG:6933) is equal-area, not
    conformal: its cells are stretched east-west and squeezed north-south away from 30 degrees."""
    if "6933" in str(crs):
        k = np.cos(np.deg2rad(30.0)) / np.cos(np.deg2rad(lat))
        return abs(transform.e) * k, abs(transform.a) / k
    return abs(transform.e), abs(transform.a)


def simulate(layers, lon, lat, hours=(1, 3, 6), wind=None, window_m=6000, neighbours=32, use_permeability=True, gap_m=60):
    """Isochrones of a fire started at (lon, lat). layers: a bundle directory (GeoTIFFs named as LAYERS, plus
    wind_speed, wind_from and spread_permeability) or a dict of arrays with "transform" and "crs".
    wind = (m/s at 10 m, degrees it blows from) overrides the bundle's wind of the map day."""
    import rasterio
    from rasterio.warp import transform as warp
    from rasterio.windows import Window
    names = list(LAYERS) + ["spread_permeability"] + ([] if wind else ["wind_speed", "wind_from"])
    if isinstance(layers, dict):
        tr, crs = layers["transform"], layers["crs"]
        x, y = warp("EPSG:4326", crs, [lon], [lat])
        col, row = ~tr * (x[0], y[0])
        n = int(window_m / abs(tr.a))
        r0, c0 = max(0, int(row) - n), max(0, int(col) - n)
        c = {k: np.asarray(layers[k])[r0:int(row) + n + 1, c0:int(col) + n + 1] for k in names if k in layers}
    else:
        c = {}
        for k in names:
            path = f"{layers}/{k}.tif"
            try:
                ds = rasterio.open(path)
            except rasterio.errors.RasterioIOError:
                continue
            with ds:
                tr, crs = ds.transform, ds.crs
                x, y = warp("EPSG:4326", crs, [lon], [lat])
                col, row = ~tr * (x[0], y[0])
                n = int(window_m / abs(tr.a))
                r0, c0 = max(0, int(row) - n), max(0, int(col) - n)
                a = ds.read(1, window=Window(c0, r0, min(ds.width, int(col) + n + 1) - c0, min(ds.height, int(row) + n + 1) - r0)).astype("float32")
                if ds.nodata is not None:
                    a[a == ds.nodata] = np.nan
                c[k] = a
    missing = [k for k in LAYERS if k not in c]
    if missing:
        raise ValueError(f"layers missing for the simulation: {missing}")
    speed, wfrom = wind if wind else (c["wind_speed"], c["wind_from"])
    f = head(c, speed, wfrom)
    start = (int(row) - r0, int(col) - c0)
    if not (0 <= start[0] < f["rate_of_spread"].shape[0] and 0 <= start[1] < f["rate_of_spread"].shape[1]):
        raise ValueError("the point is outside the layers")
    perm = c.get("spread_permeability") if use_permeability else None
    t = arrival(f["rate_of_spread"], f["spread_direction"], f["lw_ratio"], ground_cell(tr, crs, lat), [start], max(hours) * 60,
                neighbours, perm, int(gap_m / abs(tr.a)))
    out = isochrones(t, hours, tr * tr.translation(c0, r0), crs)
    s = snap(np.nan_to_num(f["rate_of_spread"]) > 0, *start)
    out["properties"] = {"lon": lon, "lat": lat, "wind_10m_m_s": float(np.nanmean(speed)), "wind_from_deg": float(np.nanmean(wfrom)),
                         "head_rate_m_min": float(f["rate_of_spread"][s]), "head_direction_deg": float(f["spread_direction"][s]),
                         "flame_length_m": float(f["flame_length"][s]), "fuel_age_permeability": bool(perm is not None),
                         "edge_reached": bool(np.isfinite(t[[0, -1], :]).any() or np.isfinite(t[:, [0, -1]]).any()),
                         "note": "surface fire potential for one wind and the fuels of the map day; no suppression, spotting or crown fire"}
    return out
