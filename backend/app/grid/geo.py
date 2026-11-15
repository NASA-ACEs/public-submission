"""Web Mercator helpers shared by the grid risk pipeline (all inputs in EPSG:3857 metres)."""
import math

import numpy as np

WORLD_HALF = 20037508.342789244
TWO_WORLD = 2 * WORLD_HALF
DEG_TO_MERC = WORLD_HALF / 180
MERC_Y_SCALE = WORLD_HALF / math.pi


def lonlat_to_merc(lon, lat):
    lat = max(-85.05112878, min(85.05112878, lat))
    return lon * DEG_TO_MERC, math.log(math.tan((90 + lat) * math.pi / 360)) * MERC_Y_SCALE


def merc_to_lonlat(mx, my):
    return mx / WORLD_HALF * 180, (2 * math.atan(math.exp(my / MERC_Y_SCALE)) - math.pi / 2) * 180 / math.pi


def cos_lat(my):
    """Ground metres per Mercator metre at northing ``my`` (scalar or array)."""
    return 1 / np.cosh(np.asarray(my, dtype='float64') / WORLD_HALF * math.pi)


def scale(my, floor=0.3):
    return max(floor, 1 / math.cosh(float(my) / WORLD_HALF * math.pi))


def bbox(paths):
    xs = [p[0] for path in paths for p in path]
    ys = [p[1] for path in paths for p in path]
    return min(xs), min(ys), max(xs), max(ys)


def simplify(points, tol):
    """Douglas-Peucker in Mercator metres; returns rounded integer vertices."""
    if len(points) <= 2:
        return [[round(x), round(y)] for x, y in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    tol2 = tol * tol
    while stack:
        i0, i1 = stack.pop()
        if i1 - i0 <= 1:
            continue
        ax, ay = points[i0]
        bx, by = points[i1]
        dx, dy = bx - ax, by - ay
        len2 = dx * dx + dy * dy
        best, best_d = -1, -1.0
        for i in range(i0 + 1, i1):
            px, py = points[i]
            t = 0 if len2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / len2))
            d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best_d:
                best, best_d = i, d
        if best_d > tol2:
            keep[best] = True
            stack += [(i0, best), (best, i1)]
    out = [[round(points[i][0]), round(points[i][1])] for i in range(len(points)) if keep[i]]
    return out if len(out) >= 2 else [[round(points[0][0]), round(points[0][1])], [round(points[-1][0]), round(points[-1][1])]]


def points_in_ring(xs, ys, ring):
    """Vectorised even-odd point-in-polygon for one ring."""
    ring = np.asarray(ring, dtype='float64')
    xi, yi = ring[:, 0], ring[:, 1]
    xj, yj = np.roll(xi, 1), np.roll(yi, 1)
    xs = np.asarray(xs, dtype='float64')[:, None]
    ys = np.asarray(ys, dtype='float64')[:, None]
    crosses = (yi > ys) != (yj > ys)
    with np.errstate(divide='ignore', invalid='ignore'):
        x_at = (xj - xi) * (ys - yi) / (yj - yi + 1e-12) + xi
    return (np.count_nonzero(crosses & (xs < x_at), axis=1) % 2) == 1


def points_in_rings(xs, ys, rings):
    inside = np.zeros(len(xs), dtype=bool)
    for ring in rings:
        inside ^= points_in_ring(xs, ys, ring)
    return inside


def sample_path(path, step):
    """Split a path into sub-segments of roughly ``step`` Mercator metres.

    Returns (starts, ends, mids) arrays of shape (n, 2).
    """
    pts = np.asarray(path, dtype='float64')
    if len(pts) < 2:
        empty = np.zeros((0, 2))
        return empty, empty, empty
    a, b = pts[:-1], pts[1:]
    lengths = np.hypot(*(b - a).T)
    counts = np.clip(np.ceil(lengths / step), 1, 32).astype(int)
    seg = np.repeat(np.arange(len(a)), counts)
    k = np.concatenate([np.arange(c) for c in counts])
    n = counts[seg]
    t0, t1 = (k / n)[:, None], ((k + 1) / n)[:, None]
    d = (b - a)[seg]
    starts = a[seg] + d * t0
    ends = a[seg] + d * t1
    return starts, ends, (starts + ends) / 2


def runs_to_paths(starts, ends, mask):
    """Join consecutive sub-segments where ``mask`` is true into integer polylines."""
    paths, cur, last = [], None, -2
    for i in np.flatnonzero(mask):
        if cur is not None and last == i - 1:
            cur.append(ends[i])
        else:
            if cur:
                paths.append(cur)
            cur = [starts[i], ends[i]]
        last = i
    if cur:
        paths.append(cur)
    return [[[round(float(x)), round(float(y))] for x, y in p] for p in paths]
