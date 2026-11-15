"""Large-fire risk class of every 200 m piece of every transmission line of a state.

Writes the file the frontend draws as "Lines by large-fire risk" (``public/film/lines-ca-20261020.json``).

Lines: the backend's ``GET /v1/grid/states/{abbr}/lines`` (HIFLD, Web Mercator), saved to a file. Risk: the backend's
own sampler (``app.grid.fuel.FuelRisk``: the result's assessment layer ``risk_index``, one sample at the middle of
each piece of at most 200 m, the same pieces as ``densify``), classes by the result's breaks (0.2 / 0.4 / 0.6 / 0.8);
class 0 is land the result does not rate.

Run from the backend's root with its environment::

    curl -s http://127.0.0.1:8000/v1/grid/states/CA/lines > lines.json
    RESULTS_DIR=<folder of result bundles> python scripts/line_risk.py lines.json lines-ca-20261020.json \\
        --result ca-20261020-nisar-v3.3.2-ad --region -118.693 34.686

Output: ``summary`` (km of line in total, on rated land, in classes 4 and 5, per class; the window of the state with
the most km in classes 4 and 5) and ``runs``: ``[class, x0, y0, dx, dy, ...]``, one run of pieces of one class along
a path, in units of 10 m of Web Mercator, the first point whole and the rest as steps from the one before.
"""
import argparse
import json
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.getcwd())
from app.grid.fuel import STEP_M, FuelRisk  # noqa: E402
from app.grid.geo import cos_lat, lonlat_to_merc, merc_to_lonlat  # noqa: E402
from app.results import Registry  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
ap.add_argument('lines', help='JSON of GET /v1/grid/states/{abbr}/lines')
ap.add_argument('out', help='output JSON')
ap.add_argument('--result', default='ca-20261020-nisar-v3.3.2-ad', help='result bundle id under RESULTS_DIR')
ap.add_argument('--results-dir', default=os.getenv('RESULTS_DIR'), help='folder of result bundles (default: RESULTS_DIR)')
ap.add_argument('--region', nargs=2, type=float, metavar=('LON', 'LAT'), help='centre of a 110 km window to count')
args = ap.parse_args()
if not args.results_dir:
    ap.error('set RESULTS_DIR or --results-dir')

reg = Registry(args.results_dir, tempfile.mkdtemp(prefix='line-risk-cache-'))
reg.scan()
result = reg.get(args.result)
if result is None:
    ap.error(f'no result {args.result} under {args.results_dir}')
f = FuelRisk(result)
breaks = np.asarray(f.breaks, dtype='float64')
with open(args.lines) as fh:
    lines = json.load(fh)['lines']

runs, mids_all, len_all, cls_all = [], [], [], []
for line in lines:
    for path in line['paths']:
        pts = np.asarray(path, dtype='float64')
        if len(pts) < 2:
            continue
        a, d = pts[:-1], np.diff(pts, axis=0)
        ground = np.hypot(d[:, 0], d[:, 1]) * cos_lat(a[:, 1] + d[:, 1] / 2)
        counts = np.maximum(1, np.ceil(ground / STEP_M)).astype(int)  # the same pieces as densify()
        seg = np.repeat(np.arange(len(a)), counts)
        k = np.concatenate([np.arange(c) for c in counts])
        t0 = (k / counts[seg])[:, None]
        t1 = ((k + 1) / counts[seg])[:, None]
        p0, p1 = a[seg] + d[seg] * t0, a[seg] + d[seg] * t1
        mids = (p0 + p1) / 2
        lengths = (ground / counts)[seg]
        v = f.sample(mids)
        cls = np.where(np.isfinite(v), np.searchsorted(breaks, np.nan_to_num(v), side='right') + 1, 0)
        mids_all.append(mids)
        len_all.append(lengths)
        cls_all.append(cls)
        # runs of one class along the path: the path's own vertices and the points where the class changes
        start = 0
        for i in range(1, len(cls) + 1):
            if i == len(cls) or cls[i] != cls[start]:
                keep = [j for j in range(start, i) if j == start or seg[j] != seg[j - 1]]
                xy = np.round(np.vstack([p0[keep], p1[i - 1:i]]) / 10).astype(int)
                steps = np.vstack([xy[:1], np.diff(xy, axis=0)])
                runs.append([int(cls[start])] + steps.ravel().tolist())
                start = i

mids = np.concatenate(mids_all)
lengths = np.concatenate(len_all)
cls = np.concatenate(cls_all)


def km(mask):
    return float(lengths[mask].sum() / 1000)


summary = dict(result=args.result, lines=len(lines), pieces=int(len(cls)), step_m=STEP_M,
               km_total=km(np.ones_like(cls, bool)), km_covered=km(cls > 0), km_flagged=km(cls >= 4),
               km_by_class={str(c): km(cls == c) for c in range(0, 6)})

# windows of the state with the most km in classes 4 and 5: 16:9, 110 km wide
W, H = 110_000, 110_000 * 9 / 16
c = cos_lat(mids[:, 1])
gx, gy = mids[:, 0] * c, mids[:, 1] * c  # rough ground metres, enough for a search at this size
flag = cls >= 4
best = []
for x in np.arange(gx.min(), gx.max(), 10_000):
    for y in np.arange(gy.min(), gy.max(), 10_000):
        m = (gx >= x) & (gx < x + W) & (gy >= y) & (gy < y + H)
        if m.sum() < 50:
            continue
        best.append((float(lengths[m & flag].sum() / 1000), float(lengths[m].sum() / 1000), x + W / 2, y + H / 2))
best.sort(reverse=True)
top = []
for b in best:
    if all(abs(b[2] - t[2]) > W or abs(b[3] - t[3]) > H for t in top):
        top.append(b)
    if len(top) == 6:
        break
for fl, tot, x, y in top:
    my = y / cos_lat(y / 0.83)
    my = y / cos_lat(my)
    lon, lat = merc_to_lonlat(x / cos_lat(my), my)
    print(f'window centre {lon:.3f} {lat:.3f}: {fl:.0f} km in classes 4-5 of {tot:.0f} km', file=sys.stderr)

if args.region:
    lon, lat = args.region
    cx, cy = lonlat_to_merc(lon, lat)
    s = 1 / float(cos_lat(cy))
    hw, hh = 110 * 500 * s, 110 * 500 * 9 / 16 * s
    m = (np.abs(mids[:, 0] - cx) <= hw) & (np.abs(mids[:, 1] - cy) <= hh)
    summary['region'] = dict(lon=lon, lat=lat, width_km=110.0, km=km(m), km_covered=km(m & (cls > 0)),
                             km_flagged=km(m & (cls >= 4)))
summary['windows'] = [dict(flagged_km=round(fl, 1), km=round(tot, 1)) for fl, tot, _, _ in top]
with open(args.out, 'w') as fh:
    json.dump(dict(summary=summary, runs=runs), fh, separators=(',', ':'))
print(json.dumps(summary), file=sys.stderr)
print('runs', len(runs), 'bytes', os.path.getsize(args.out), file=sys.stderr)
