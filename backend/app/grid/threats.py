"""Line threat classification, shutoff economics and the exact threatened line sections."""
import math

import numpy as np

from .geo import cos_lat, merc_to_lonlat, points_in_ring, runs_to_paths, sample_path
from .spread import eta_hours

RANK = {'SHUT OFF': 3, 'PSPS': 2, 'MONITOR': 1}


def _risk_base_per_forest_km(kv):
    return 1_850_000 if kv >= 230 else 1_150_000 if kv >= 100 else 700_000


def _psps_cost(kv, km):
    base = 240_000 if kv >= 230 else 110_000 if kv >= 100 else 48_000
    return round(base * min(2.2, 0.75 + km * 0.025))


def line_fire_distance(paths, box, index, max_m=22000):
    """Minimum ground distance (m) from a line to any fire detection, and that detection's FRP."""
    cand = index.candidates(box, max_m)
    if not cand.size:
        return math.inf, 0.0
    pts = index.xy[cand]
    best, frp = math.inf, 0.0
    c = float(cos_lat((box[1] + box[3]) / 2))
    for path in paths:
        arr = np.asarray(path, dtype='float64')
        if len(arr) < 2:
            continue
        a, b = arr[:-1], arr[1:]
        d = b - a
        len2 = (d ** 2).sum(1)
        rel = pts[:, None, :] - a[None, :, :]
        t = np.clip((rel * d[None]).sum(2) / np.where(len2 > 0, len2, 1)[None], 0, 1)
        closest = a[None] + t[..., None] * d[None]
        dist = np.hypot(*(pts[:, None, :] - closest).transpose(2, 0, 1)) * c
        k = np.unravel_index(np.argmin(dist), dist.shape)
        if dist[k] < best:
            best, frp = float(dist[k]), float(index.frp[cand[k[0]]])
    return best, frp


def _boxes_near(bboxes, boxes, pad_m):
    """Mask of line bboxes intersecting any of ``boxes`` grown by ``pad_m`` ground metres."""
    hit = np.zeros(len(bboxes), dtype=bool)
    for x0, y0, x1, y1 in boxes:
        pad = pad_m / max(0.3, float(cos_lat((y0 + y1) / 2)))
        hit |= ~((bboxes[:, 2] < x0 - pad) | (bboxes[:, 0] > x1 + pad) | (bboxes[:, 3] < y0 - pad) | (bboxes[:, 1] > y1 + pad))
    return hit


def exposure(grid, index, sims, clusters):
    """Per-line fire distance, nearest FRP and spread ETA. Independent of buffer radius and operators."""
    n = len(grid.lines)
    dist = np.full(n, math.inf)
    frp = np.zeros(n)
    eta = [None] * n
    if n and clusters:
        near = _boxes_near(grid.bboxes, [c['bbox'] for c in clusters], 22000)
        for i in np.flatnonzero(near):
            dist[i], frp[i] = line_fire_distance(grid.lines[i]['paths'], grid.bboxes[i], index)
    if n and sims:
        reach = []
        for s in sims:
            r = (s['hourly'][-1]['headM'] + 2000) / max(0.3, float(cos_lat(s['centroid'][1])))
            reach.append((s['centroid'][0] - r, s['centroid'][1] - r, s['centroid'][0] + r, s['centroid'][1] + r))
        for i in np.flatnonzero(_boxes_near(grid.bboxes, reach, 0)):
            eta[i] = eta_hours(grid.lines[i]['paths'], sims)
    return dict(dist=dist, frp=frp, eta=eta)


def classify(grid, owners, exp, weather, burn, radius_m):
    severity = 1 + max(0, 35 - weather['minRhPct']) * 0.012 + max(0, weather['avgWindMph'] - 10) * 0.018
    flagged = []
    for i, line in enumerate(grid.lines):
        if line['owner'] not in owners:
            continue
        dist, frp, eta = float(exp['dist'][i]), float(exp['frp'][i]), exp['eta'][i]
        bs = burn.get(line['oid']) or {}
        higher = bs.get('higher_fraction_of_covered') or 0
        mean = bs.get('mean_score_on_covered') or 0
        peak = bs.get('max_score') or 0
        share = line['fKm'] / line['km'] if line['km'] > 0 else 0

        if dist <= radius_m or (eta is not None and eta <= 4) or (dist <= radius_m * 2 and (higher >= 0.35 or peak >= 0.3)):
            tier = 'SHUT OFF'
        elif (eta is not None and eta <= 24) or (dist <= 10000 and (line['fKm'] >= 1.5 or higher >= 0.25)):
            tier = 'PSPS'
        elif ((dist <= 18000 and (line['fKm'] >= 2.5 or frp >= 15))
              or (line['fKm'] >= 5 and weather['minRhPct'] <= 25 and weather['avgWindMph'] >= 15)
              or higher >= 0.25 or ((bs.get('higher_length_m') or 0) >= 500 and peak >= 0.25)):
            tier = 'MONITOR'
        else:
            continue

        score = ((max(0, 42 * (1 - min(20000, dist) / 20000)) if math.isfinite(dist) else 0)
                 + (30 * (1 - eta / 25) if eta is not None else 0)
                 + min(18, line['fKm'] * 2.5 + share * 10) + (min(16, peak * 20) if peak > 0 else 0))
        prob = {'SHUT OFF': 0.72, 'PSPS': 0.38, 'MONITOR': 0.14}[tier]
        amp = 1 + min(0.35, share * 0.25) + min(0.4, mean * 0.75) + min(0.25, math.log10(1 + frp) * 0.1)
        avoided = round(line['fKm'] * _risk_base_per_forest_km(line['kv']) * prob * amp * min(1.45, severity)) if line['fKm'] > 0 else 0
        cost = _psps_cost(line['kv'], line['km'])
        justified = line['fKm'] > 0.2 and tier != 'MONITOR' and avoided > cost
        flagged.append(dict(line=line, index=i, tier=tier, fireDistM=dist, etaH=eta, riskScore=min(100, round(score * 0.95)),
                            avoided=avoided, cost=cost, justified=justified))
    flagged.sort(key=lambda f: (-RANK[f['tier']], -f['riskScore']))
    return flagged


def threatened_sections(item, index, envelopes, forest, radius_m):
    """Forested parts of a flagged line that are within reach of fire.

    Falls back to the near-fire part when none of it is forest, and to the whole line
    when it was flagged by the fuel model alone.
    """
    zone = max(2000, radius_m * 2) if item['tier'] == 'SHUT OFF' else 10000
    line = item['line']
    forest_paths, near_paths = [], []
    for path in line['paths']:
        starts, ends, mids = sample_path(path, 500)
        if not len(mids):
            continue
        near = np.zeros(len(mids), dtype=bool)
        x0, y0 = mids.min(0)
        x1, y1 = mids.max(0)
        cand = index.candidates((x0, y0, x1, y1), zone)
        if cand.size:
            c = cos_lat(mids[:, 1])[:, None]
            d = np.hypot(*(mids[:, None, :] - index.xy[cand][None]).transpose(2, 0, 1)) * c
            near |= (d <= zone).any(1)
        for ring, (ex0, ey0, ex1, ey1) in envelopes:
            box = (mids[:, 0] >= ex0) & (mids[:, 0] <= ex1) & (mids[:, 1] >= ey0) & (mids[:, 1] <= ey1)
            if box.any():
                sel = np.flatnonzero(box & ~near)
                if sel.size:
                    near[sel] = points_in_ring(mids[sel, 0], mids[sel, 1], ring)
        if not near.any():
            continue
        near_paths += runs_to_paths(starts, ends, near)
        forest_paths += runs_to_paths(starts, ends, near & forest.at(mids[:, 0], mids[:, 1]))
    return forest_paths or near_paths or line['paths']


def path_km(path):
    arr = np.asarray(path, dtype='float64')
    if len(arr) < 2:
        return 0.0
    seg = np.diff(arr, axis=0)
    mid_y = (arr[1:, 1] + arr[:-1, 1]) / 2
    return float((np.hypot(seg[:, 0], seg[:, 1]) * cos_lat(mid_y)).sum() / 1000)


def _lonlat(pt):
    lon, lat = merc_to_lonlat(float(pt[0]), float(pt[1]))
    return [round(lon, 5), round(lat, 5)]


def nearest_fire(paths, clusters):
    """Closest fire cluster to any vertex of ``paths``: (cluster, ground distance m)."""
    if not clusters:
        return None, None
    pts = np.concatenate([np.asarray(p, dtype='float64') for p in paths if len(p)])
    cents = np.array([c['centroid'] for c in clusters], dtype='float64')
    d = np.hypot(*(pts[:, None, :] - cents[None]).transpose(2, 0, 1)) * cos_lat(pts[:, 1])[:, None]
    k = int(d.min(0).argmin())
    return clusters[k], float(d[:, k].min())


def report(grid, owners, exp, index, sims, weather, burn, forest, radius_m, clusters=None):
    flagged = classify(grid, owners, exp, weather, burn, radius_m)
    envelopes = []
    for sim in sims:
        ring = np.asarray(sim['hourly'][-1]['ring'], dtype='float64')
        envelopes.append((ring, (*ring.min(0), *ring.max(0))))
    shutoff, psps, lines = [], [], []
    for item in flagged:
        if item['tier'] == 'MONITOR':
            continue
        sections = threatened_sections(item, index, envelopes, forest, radius_m)
        (shutoff if item['tier'] == 'SHUT OFF' else psps).extend(sections)
        line = item['line']
        # Per-section detail for operator reports: length and end coordinates (WGS84).
        detail = []
        for path in sections:
            km = path_km(path)
            if km < 0.05:
                continue
            detail.append(dict(km=round(km, 2), start=_lonlat(path[0]), end=_lonlat(path[-1]),
                               path=[[round(float(x)), round(float(y))] for x, y in path]))
        fire, fire_m = nearest_fire(sections, clusters or [])
        wind = next((sm.get('wind') for sm in sims if fire and sm['clusterId'] == fire['id']), None)
        lines.append(dict(oid=line['oid'], tier=item['tier'], owner=line['owner'], sub1=line['sub1'], sub2=line['sub2'],
                          kv=line['kv'], km=line['km'], fKm=line['fKm'], riskScore=item['riskScore'],
                          fireDistM=round(item['fireDistM']) if math.isfinite(item['fireDistM']) else None,
                          etaH=item['etaH'], bbox=[round(float(v)) for v in grid.bboxes[item['index']]],
                          sectionKm=round(sum(d['km'] for d in detail), 2), sections=detail,
                          fire=dict(id=fire['id'], name=fire['name'], sizeClass=fire.get('sizeClass'),
                                    distM=round(fire_m), wind=wind) if fire else None))
    justified = [f for f in flagged if f['justified']]
    avoided = sum(f['avoided'] for f in justified)
    cost = sum(f['cost'] for f in justified)
    return dict(
        totals={t: sum(1 for f in flagged if f['tier'] == t) for t in RANK},
        lines=lines,
        sections=dict(shutoff=shutoff, psps=psps),
        savings=dict(netUsd=max(0, avoided - cost), avoidedLossUsd=avoided, outageCostUsd=cost,
                     forestKmProtected=round(sum(f['line']['fKm'] for f in justified), 1)),
    )
