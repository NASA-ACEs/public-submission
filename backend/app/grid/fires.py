"""Live wildfire detections (NASA VIIRS hotspots) and incident perimeters (NIFC), clustered per state."""
import math
import time

import httpx
import numpy as np

from .geo import cos_lat, merc_to_lonlat, points_in_rings, scale

VIIRS_URL = 'https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/Satellite_VIIRS_Thermal_Hotspots_and_Fire_Activity/FeatureServer/0/query'
# NIFC's own WFIGS service (current interagency perimeters), not a copy in another organisation.
NIFC_URL = 'https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query'
CLUSTER_RADIUS = 3200
# A detection counts as a forest fire when at least this share of the ~1.5 km
# around it is USFS forest. Detections inside a NIFC incident perimeter are
# always kept: those are confirmed wildland fires (incl. brush and grass).
FOREST_MIN_FRACTION = 0.34
FOREST_STEP_M = 750
SIZE_CLASSES = {3: 'large', 2: 'medium', 1: 'small'}


def _envelope(state, pad=0.25):
    xmin, ymin, xmax, ymax = state['extent']
    lon0, lat0 = merc_to_lonlat(xmin, ymin)
    lon1, lat1 = merc_to_lonlat(xmax, ymax)
    return f'{lon0 - pad:.3f},{lat0 - pad:.3f},{lon1 + pad:.3f},{lat1 + pad:.3f}'


def fetch_points(client, envelope, hours, max_pages=4):
    cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - hours * 3600))
    now_ms = time.time() * 1000
    points = []
    for page in range(max_pages):
        res = client.get(VIIRS_URL, params=dict(
            where=f"acq_time >= TIMESTAMP '{cutoff}'", geometry=envelope, geometryType='esriGeometryEnvelope',
            inSR='4326', spatialRel='esriSpatialRelIntersects', orderByFields='acq_time DESC',
            outFields='OBJECTID,frp,acq_time,scan,track', outSR='3857',
            resultOffset=page * 2000, resultRecordCount=2000, f='json'))
        if res.status_code != 200:
            break
        data = res.json()
        feats = data.get('features') or []
        for f in feats:
            g, a = f.get('geometry') or {}, f.get('attributes') or {}
            mx, my = g.get('x'), g.get('y')
            if mx is None or my is None or not (math.isfinite(mx) and math.isfinite(my)):
                continue
            acq = a.get('acq_time')
            c = max(0.25, float(cos_lat(my)))
            scan = max(0.35, min(2.0, a.get('scan') or 0.39))
            track = max(0.35, min(2.0, a.get('track') or 0.39))
            points.append(dict(mx=float(mx), my=float(my), frp=max(0.5, float(a.get('frp') or 2)),
                               hoursOld=max(0.0, (now_ms - acq) / 3.6e6) if acq else 24.0,
                               scanM=scan * 1000 / c, trackM=track * 1000 / c))
        if not data.get('exceededTransferLimit') or len(feats) < 2000:
            break
    return points


def fetch_perimeters(client, envelope):
    try:
        res = client.get(NIFC_URL, params=dict(
            where='1=1', geometry=envelope, geometryType='esriGeometryEnvelope', inSR='4326',
            spatialRel='esriSpatialRelIntersects', outFields='OBJECTID,poly_IncidentName,poly_GISAcres,attr_IncidentSize',
            outSR='3857', resultRecordCount=500, f='json'))
        if res.status_code != 200:
            return []
        out = []
        for f in res.json().get('features') or []:
            rings = (f.get('geometry') or {}).get('rings')
            if not rings:
                continue
            a = f.get('attributes') or {}
            xs = [p[0] for r in rings for p in r]
            ys = [p[1] for r in rings for p in r]
            out.append(dict(name=str(a.get('poly_IncidentName') or 'Active wildfire'),
                            acres=float(a.get('poly_GISAcres') or a.get('attr_IncidentSize') or 0),
                            rings=[[[round(x), round(y)] for x, y in r] for r in rings],
                            bbox=[min(xs), min(ys), max(xs), max(ys)]))
        return out
    except (httpx.HTTPError, ValueError):
        return []


def in_state(xs, ys, state, margin):
    """Points inside the state polygon, or within ``margin`` metres of it (4-probe test)."""
    xs, ys = np.asarray(xs, dtype='float64'), np.asarray(ys, dtype='float64')
    if xs.size == 0:
        return np.zeros(0, dtype=bool)
    e = state['extent']
    ok = (xs >= e[0] - margin) & (xs <= e[2] + margin) & (ys >= e[1] - margin) & (ys <= e[3] + margin)
    result = np.zeros(xs.size, dtype=bool)
    idx = np.flatnonzero(ok)
    if idx.size:
        hit = points_in_rings(xs[idx], ys[idx], state['rings'])
        for ox, oy in ((margin, 0), (-margin, 0), (0, margin), (0, -margin)):
            hit |= points_in_rings(xs[idx] + ox, ys[idx] + oy, state['rings'])
        result[idx] = hit
    return result


def cluster(points, perimeters):
    if not points:
        return []
    xy = np.array([[p['mx'], p['my']] for p in points])
    cells = {}
    for i, (x, y) in enumerate(xy):
        cells.setdefault((int(x // CLUSTER_RADIUS), int(y // CLUSTER_RADIUS)), []).append(i)
    visited = np.zeros(len(points), dtype=bool)
    r2 = CLUSTER_RADIUS ** 2
    clusters = []
    for i in range(len(points)):
        if visited[i]:
            continue
        visited[i] = True
        queue, group = [i], []
        while queue:
            cur = queue.pop()
            group.append(cur)
            gx, gy = int(xy[cur, 0] // CLUSTER_RADIUS), int(xy[cur, 1] // CLUSTER_RADIUS)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in cells.get((gx + dx, gy + dy), ()):
                        if not visited[j] and ((xy[j] - xy[cur]) ** 2).sum() <= r2:
                            visited[j] = True
                            queue.append(j)
        pts = [points[k] for k in group]
        w = np.array([1 + p['frp'] * 0.2 for p in pts])
        gxy = xy[group]
        cx, cy = (gxy * w[:, None]).sum(0) / w.sum()
        hx = np.array([p['scanM'] / 2 for p in pts])
        hy = np.array([p['trackM'] / 2 for p in pts])
        box = [float((gxy[:, 0] - hx).min()), float((gxy[:, 1] - hy).min()),
               float((gxy[:, 0] + hx).max()), float((gxy[:, 1] + hy).max())]
        c = cos_lat(gxy[:, 1])
        pixel_area = float((np.array([p['scanM'] for p in pts]) * c * np.array([p['trackM'] for p in pts]) * c).sum())
        cc = float(cos_lat(cy))
        area_km2 = pixel_area * (0.62 if len(pts) > 1 else 1) / 1e6
        span_km2 = (box[2] - box[0]) * cc * (box[3] - box[1]) * cc / 1e6
        area_km2 = max(area_km2, min(span_km2 * 0.55, area_km2 * 1.8))
        matched = None
        for pr in perimeters:
            b = pr['bbox']
            if b[0] - 5000 <= cx <= b[2] + 5000 and b[1] - 5000 <= cy <= b[3] + 5000:
                matched = pr
                if pr['acres'] > 0:
                    area_km2 = max(area_km2, pr['acres'] * 0.00404686)
                break
        acres = round(area_km2 * 247.105)
        max_frp = max(p['frp'] for p in pts)
        lon, lat = merc_to_lonlat(cx, cy)
        clusters.append(dict(
            name=matched['name'] if matched else f'Fire near {lat:.2f}°, {lon:.2f}°',
            centroid=[float(cx), float(cy)], lonlat=[lon, lat], bbox=box, pixelCount=len(pts),
            areaAcres=max(18, acres), maxFrp=round(max_frp, 1), totalFrp=round(sum(p['frp'] for p in pts), 1),
            minHoursAgo=round(min(p['hoursOld'] for p in pts), 1),
            rank=3 if max_frp >= 35 or len(pts) >= 8 or acres >= 400 else 2 if max_frp >= 12 or len(pts) >= 3 or acres >= 100 else 1))
        for k in group:
            points[k]['rank'] = clusters[-1]['rank']
    clusters.sort(key=lambda c: (-c['rank'], -c['totalFrp']))
    for n, c in enumerate(clusters, 1):
        c['id'] = f'fire-{n}'
        c['sizeClass'] = SIZE_CLASSES[c['rank']]
    return clusters


def forest_share(forest, xs, ys):
    """Share of forest in a 3x3 sample around every point (0..1)."""
    xs, ys = np.asarray(xs, dtype='float64'), np.asarray(ys, dtype='float64')
    step = FOREST_STEP_M / np.maximum(0.3, cos_lat(ys))
    hits = np.zeros(xs.size)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            hits += forest.at(xs + dx * step, ys + dy * step)
    return hits / 9


def inside_perimeters(xs, ys, perimeters):
    xs, ys = np.asarray(xs, dtype='float64'), np.asarray(ys, dtype='float64')
    out = np.zeros(xs.size, dtype=bool)
    for pr in perimeters:
        b = pr['bbox']
        idx = np.flatnonzero((xs >= b[0]) & (xs <= b[2]) & (ys >= b[1]) & (ys <= b[3]) & ~out)
        if idx.size:
            out[idx] = points_in_rings(xs[idx], ys[idx], pr['rings'])
    return out


class FireSpatialIndex:
    """Uniform grid over fire detections for distance queries."""

    def __init__(self, points, cell=4000):
        self.cell = cell
        self.xy = np.array([[p['mx'], p['my']] for p in points], dtype='float64').reshape(-1, 2)
        self.frp = np.array([p['frp'] for p in points], dtype='float64')
        self.buckets = {}
        for i, (x, y) in enumerate(self.xy):
            self.buckets.setdefault((int(x // cell), int(y // cell)), []).append(i)

    def candidates(self, box, pad_m):
        pad = pad_m / scale((box[1] + box[3]) / 2)
        gx0, gx1 = int((box[0] - pad) // self.cell), int((box[2] + pad) // self.cell)
        gy0, gy1 = int((box[1] - pad) // self.cell), int((box[3] + pad) // self.cell)
        if (gx1 - gx0 + 1) * (gy1 - gy0 + 1) > len(self.buckets):
            idx = [i for (gx, gy), b in self.buckets.items() if gx0 <= gx <= gx1 and gy0 <= gy <= gy1 for i in b]
        else:
            idx = [i for gx in range(gx0, gx1 + 1) for gy in range(gy0, gy1 + 1) for i in self.buckets.get((gx, gy), ())]
        return np.array(idx, dtype=int)


def load(client, state, hours, forest=None, forest_only=True):
    """Returns (points, perimeters, clusters, stats).

    With ``forest_only`` detections outside forest (industrial heat, urban and
    agricultural burns) are dropped before clustering.
    """
    envelope = _envelope(state)
    points = fetch_points(client, envelope, hours)
    perimeters = fetch_perimeters(client, envelope)
    if points:
        keep = in_state([p['mx'] for p in points], [p['my'] for p in points], state, 15000)
        points = [p for p, k in zip(points, keep) if k]
    if perimeters:
        keep = in_state([(p['bbox'][0] + p['bbox'][2]) / 2 for p in perimeters],
                        [(p['bbox'][1] + p['bbox'][3]) / 2 for p in perimeters], state, 20000)
        perimeters = [p for p, k in zip(perimeters, keep) if k]
    total = len(points)
    if points and forest is not None and forest.tiles:
        xs, ys = [p['mx'] for p in points], [p['my'] for p in points]
        share = forest_share(forest, xs, ys)
        for p, f in zip(points, share):
            p['forest'] = float(f)
        if forest_only:
            keep = (share >= FOREST_MIN_FRACTION) | inside_perimeters(xs, ys, perimeters)
            points = [p for p, k in zip(points, keep) if k]
    stats = dict(totalDetections=total, keptDetections=len(points), forestOnly=bool(forest_only))
    return points, perimeters, cluster(points, perimeters), stats
