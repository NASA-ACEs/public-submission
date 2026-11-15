"""24h wind-driven elliptical fire-spread envelopes (Anderson 1983 shape, heuristic spread rate; not a validated fire model)."""
import math

import numpy as np

from .geo import scale

HOURS = 25


# NWS 10 m wind -> midflame wind under forest canopy (Albini & Baughman adjustment).
MIDFLAME = 0.4
MAX_LB = 5.0


def length_to_breadth(wind_mph):
    """Anderson (1983) ellipse length-to-breadth ratio from midflame wind (mph)."""
    u = max(0.0, wind_mph) * MIDFLAME
    lb = 0.936 * math.exp(0.2566 * u) + 0.461 * math.exp(-0.1548 * u) - 0.397
    return min(MAX_LB, max(1.0, lb))


def mean_wind(periods):
    """Vector mean of the forecast.

    Returns (mean speed mph, steady speed mph, spread bearing deg, label wind blows from). The steady
    speed is the resultant vector length: a wind that swings around elongates the fire less.
    """
    vx = sum(p['windMph'] * math.sin(math.radians(p['spreadBearingDeg'])) for p in periods)
    vy = sum(p['windMph'] * math.cos(math.radians(p['spreadBearingDeg'])) for p in periods)
    bearing = (math.degrees(math.atan2(vx, vy)) + 360) % 360 if (vx or vy) else 90.0
    n = max(1, len(periods))
    speed = sum(p['windMph'] for p in periods) / n
    steady = math.hypot(vx, vy) / n
    labels = 'N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW'.split()
    from_label = labels[int(((bearing + 180) % 360 + 11.25) // 22.5) % 16]
    return speed, steady, bearing, from_label


def _ellipse(cx, cy, a, b, offset, bearing_deg, n=72):
    """Ellipse with semi-axes a (along bearing) and b, centre shifted ``offset`` downwind of (cx, cy)."""
    rad = math.radians(bearing_deg)
    ux, uy, px, py = math.sin(rad), math.cos(rad), math.cos(rad), -math.sin(rad)
    theta = np.linspace(0, 2 * math.pi, n + 1)
    along = offset + a * np.cos(theta)
    side = b * np.sin(theta)
    return np.round(np.stack([cx + ux * along + px * side, cy + uy * along + py * side], axis=1)).astype(int).tolist()


def simulate(clusters, weather, grid, forest, burn_summaries, fuel_boost=None):
    """One wind-driven elliptical envelope per hour 0..24 for the 20 most severe clusters.

    ``weather`` is a bundle, or a callable returning the bundle for a cluster
    (so each fire spreads with its own local wind). ``fuel_boost`` (optional) returns the fuel multiplier of a
    cluster from a real model result, or None to keep the default from nearby lines.
    """
    sims = []
    lines = grid.lines
    for cl in clusters[:20]:
        wx = weather(cl) if callable(weather) else weather
        cx, cy = cl['centroid']
        c = scale(cy)
        mask_frac = forest.fraction_around(cx, cy)
        near = np.flatnonzero(np.hypot(*((grid.first_points - [cx, cy]) * c).T) <= 25000) if len(lines) else []
        line_ratio = np.mean([lines[i]['fKm'] / lines[i]['km'] for i in near if lines[i]['km'] > 0] or [0])
        scores = [burn_summaries[lines[i]['oid']]['mean_score_on_covered'] for i in near
                  if burn_summaries.get(lines[i]['oid'], {}).get('mean_score_on_covered') is not None]
        forest_fraction = float(max(mask_frac, line_ratio))
        boost = 0.8 + np.mean(scores) * 1.4 if scores else 1
        real = fuel_boost(cl) if fuel_boost else None
        if real is not None:
            boost = real
        frp_scale = min(2.1, 0.78 + math.log10(1 + cl['maxFrp']) * 0.65)
        fuel = min(1.85, (0.55 + forest_fraction * 0.95) * boost)
        bw = max(250, (cl['bbox'][2] - cl['bbox'][0]) * c * 0.5)
        bh = max(250, (cl['bbox'][3] - cl['bbox'][1]) * c * 0.5)
        r0 = max(320, (bw + bh) * 0.5)

        periods = wx['periods'][:HOURS]
        speed, steady, bearing, from_label = mean_wind(periods)
        lb = length_to_breadth(steady)
        root = math.sqrt(max(0.0, lb * lb - 1))
        hb = (lb + root) / (lb - root) if root else 1.0
        run = 0.0
        hourly = []
        for h in range(HOURS):
            wp = periods[min(h, len(periods) - 1)]
            if h > 0:
                run += 145 * frp_scale * fuel * (1 + max(0, 65 - wp['rhPct']) * 0.014) * (0.6 + (wp['windMph'] / 8) ** 1.35)
            back_run = run / hb
            a = r0 + (run + back_run) / 2
            b = max(r0, (run + back_run) / (2 * lb) + r0 * 0.6)
            offset = (run - back_run) / 2
            acres = max(cl['areaAcres'], round(math.pi * a * b / 1e6 * 247.105))
            hourly.append(dict(headM=offset + a, backM=a - offset, flankM=b, aM=a, bM=b, offsetM=offset,
                               bearingDeg=bearing, acres=acres,
                               ring=_ellipse(cx, cy, a / c, b / c, offset / c, bearing)))
        sims.append(dict(clusterId=cl['id'], centroid=[cx, cy], forestFraction=round(forest_fraction, 2),
                         **({'fuelBoost': round(float(real), 3)} if real is not None else {}),
                         wind=dict(mph=round(speed, 1), fromLabel=from_label, bearingDeg=round(bearing),
                                   lengthToBreadth=round(lb, 2), source=wx['source']),
                         hourly=hourly))
    return sims


def eta_hours(paths, sims, limit=24):
    """Earliest forecast hour at which any vertex or segment midpoint of the line is inside a spread envelope."""
    pts = []
    for path in paths:
        arr = np.asarray(path, dtype='float64')
        pts.append(arr)
        if len(arr) > 1:
            pts.append((arr[1:] + arr[:-1]) / 2)
    pts = np.concatenate(pts)
    best = None
    for sim in sims:
        cx, cy = sim['centroid']
        c = scale(cy)
        peak = sim['hourly'][-1]
        d = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
        cand = pts[d <= (peak['headM'] + 2000) / c]
        if not len(cand):
            continue
        dx, dy = (cand[:, 0] - cx) * c, (cand[:, 1] - cy) * c
        top = best if best is not None else limit
        for h in range(top + 1):
            env = sim['hourly'][h]
            rad = math.radians(env['bearingDeg'])
            ux, uy = math.sin(rad), math.cos(rad)
            along = dx * ux + dy * uy - env['offsetM']
            cross = -dx * uy + dy * ux
            if np.any((along / env['aM']) ** 2 + (cross / env['bM']) ** 2 <= 1):
                best = h
                break
    return best
