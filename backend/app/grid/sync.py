"""Build the local US transmission-grid cache that the risk API serves.

Sources: HIFLD US Electric Power Transmission Lines, USA States (generalized) and the
USFS CONUS forest mask. For every line we precompute its length inside forest and the
forested sub-paths themselves, so no client ever has to sample forest tiles.

    python -m app.grid.sync                       # full download from ArcGIS (~minutes)
    python -m app.grid.sync --from-dir ../frontend/public/data   # reuse an existing export
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np

from . import forest as forest_mod
from .geo import cos_lat, lonlat_to_merc, merc_to_lonlat, points_in_rings, runs_to_paths, sample_path, simplify
from .store import GRID_DIR

POWER_URL = 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/US_Electric_Power_Transmission_Lines/FeatureServer/0/query'
STATES_URL = 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_States_Generalized_Boundaries/FeatureServer/0/query'
FOREST_STEP_M = 600


def log(*args):
    print('[grid-sync]', *args, flush=True)


def fetch_states(client):
    data = client.get(STATES_URL, params=dict(where='1=1', outFields='STATE_ABBR,STATE_NAME', outSR='3857',
                                              geometryPrecision='0', f='json')).json()
    states = []
    for f in data.get('features', []):
        abbr = str(f['attributes'].get('STATE_ABBR') or '').strip()
        name = str(f['attributes'].get('STATE_NAME') or abbr).strip()
        rings = []
        for ring in (f.get('geometry') or {}).get('rings', []):
            r = simplify(ring, 4000 if abbr == 'AK' else 1200)
            if len(r) >= 4:
                if r[0] != r[-1]:
                    r.append(list(r[0]))
                rings.append(r)
        if not abbr or not rings:
            continue
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        extent = [min(xs), min(ys), max(xs), max(ys)]
        cx, cy = (extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2
        if abbr == 'AK':
            cx, cy = lonlat_to_merc(-152, 64)
        lon, lat = merc_to_lonlat(cx, cy)
        states.append(dict(abbr=abbr, name=name, extent=extent, center=[round(lon, 4), round(lat, 4)], rings=rings))
    states.sort(key=lambda s: s['name'])
    return states


def fetch_lines(client):
    total = int(client.get(POWER_URL, params=dict(where='1=1', returnCountOnly='true', f='json')).json()['count'])

    def page(offset):
        params = dict(where='1=1', outFields='OBJECTID_1,ID,TYPE,STATUS,OWNER,VOLTAGE,SUB_1,SUB_2,Shape__Length',
                      outSR='3857', geometryPrecision='0', resultOffset=offset, resultRecordCount=2000, f='json')
        for attempt in range(3):
            try:
                res = client.get(POWER_URL, params=params)
                res.raise_for_status()
                return res.json().get('features', [])
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
        return []

    with ThreadPoolExecutor(8) as pool:
        pages = pool.map(page, range(0, total, 2000))
    raw, seen = [], set()
    for features in pages:
        for f in features:
            a = f.get('attributes') or {}
            oid = a.get('OBJECTID_1')
            paths = [simplify(p, 22) for p in (f.get('geometry') or {}).get('paths', []) if len(p) >= 2]
            if oid is None or oid in seen or not paths:
                continue
            seen.add(oid)
            raw.append(dict(oid=int(oid), id=str(a.get('ID') or oid),
                            owner=str(a.get('OWNER') or '').strip() or 'NOT AVAILABLE',
                            kv=float(a['VOLTAGE']) if (a.get('VOLTAGE') or 0) > 0 else 0,
                            status=str(a.get('STATUS') or 'IN SERVICE'),
                            sub1=str(a.get('SUB_1') or 'UNKNOWN'), sub2=str(a.get('SUB_2') or 'UNKNOWN'),
                            km=round((a.get('Shape__Length') or 0) / 1000, 2), paths=paths))
    return raw


def forest_geometry(paths, mask):
    """Forest length (ground km) and forested sub-paths of a line."""
    forest_m, out = 0.0, []
    for path in paths:
        starts, ends, mids = sample_path(path, FOREST_STEP_M)
        if not len(mids):
            continue
        hit = mask.at(mids[:, 0], mids[:, 1])
        if hit.any():
            ground = np.hypot(*(ends - starts).T) * cos_lat(mids[:, 1])
            forest_m += float(ground[hit].sum())
            out += runs_to_paths(starts, ends, hit)
    return round(forest_m / 1000, 2), out


def assign_states(line, states):
    xs = [p[0] for path in line['paths'] for p in path]
    ys = [p[1] for path in line['paths'] for p in path]
    box = (min(xs), min(ys), max(xs), max(ys))
    samples = [pt for path in line['paths'] for pt in (path[0], path[len(path) // 2], path[-1])]
    sx, sy = np.array([p[0] for p in samples]), np.array([p[1] for p in samples])
    hits = []
    for st in states:
        e = st['extent']
        if box[2] < e[0] or box[0] > e[2] or box[3] < e[1] or box[1] > e[3]:
            continue
        if points_in_rings(sx, sy, st['rings']).any():
            hits.append(st['abbr'])
    if hits:
        return hits
    mx, my = samples[min(1, len(samples) - 1)]
    best, best_d = None, float('inf')
    for st in states:
        e = st['extent']
        if not (e[0] - 15000 <= mx <= e[2] + 15000 and e[1] - 15000 <= my <= e[3] + 15000):
            continue
        d = np.hypot(mx - (e[0] + e[2]) / 2, my - (e[1] + e[3]) / 2)
        if d < best_d:
            best, best_d = st['abbr'], d
    return [best] if best else []


def write(states, lines_by_state, out: Path):
    (out / 'states').mkdir(parents=True, exist_ok=True)
    summaries = []
    for st in states:
        lines = lines_by_state.get(st['abbr'], [])
        providers = {}
        for line in lines:
            p = providers.setdefault(line['owner'], dict(owner=line['owner'], count=0, km=0.0, fKm=0.0))
            p['count'] += 1
            p['km'] += line['km']
            p['fKm'] += line['fKm']
        ordered = sorted(providers.values(), key=lambda p: (p['owner'] == 'NOT AVAILABLE', -p['km']))
        summaries.append(dict(st, lineCount=len(lines), totalKm=round(sum(l['km'] for l in lines)),
                              forestKm=round(sum(l['fKm'] for l in lines)),
                              providers=[dict(p, km=round(p['km']), fKm=round(p['fKm'])) for p in ordered]))
        (out / 'states' / f"{st['abbr']}.json").write_text(json.dumps(dict(abbr=st['abbr'], lines=lines), separators=(',', ':')))
    (out / 'states.json').write_text(json.dumps(summaries, separators=(',', ':')))
    meta = dict(syncedAt=int(time.time() * 1000), lineCount=sum(s['lineCount'] for s in summaries), stateCount=len(summaries))
    (out / 'meta.json').write_text(json.dumps(meta, indent=2))
    return meta


def sync(out: Path = GRID_DIR, from_dir: Path | None = None):
    started = time.time()
    mask_path = out / 'forest_z8.npz'
    mask = forest_mod.load(mask_path)
    if mask is None:
        log('downloading forest mask tiles...')
        mask = forest_mod.download()
        forest_mod.save(mask, mask_path)
    log(f'forest mask: {len(mask.tiles)} tiles')

    if from_dir:
        states = json.loads((from_dir / 'states.json').read_text())
        states = [{k: s[k] for k in ('abbr', 'name', 'extent', 'center', 'rings')} for s in states]
        by_state = {}
        for st in states:
            path = from_dir / 'states' / f"{st['abbr']}.json"
            lines = json.loads(path.read_text())['lines'] if path.exists() else []
            by_state[st['abbr']] = [{k: l[k] for k in ('oid', 'id', 'owner', 'kv', 'status', 'sub1', 'sub2', 'km', 'paths')}
                                    for l in lines]
        cache = {}
        for abbr, lines in by_state.items():
            for line in lines:
                if line['oid'] not in cache:
                    cache[line['oid']] = forest_geometry(line['paths'], mask)
                line['fKm'], line['fPaths'] = cache[line['oid']]
                line['fKm'] = min(line['km'], line['fKm']) if line['km'] else line['fKm']
        log(f'imported {len(cache)} lines from {from_dir}')
    else:
        with httpx.Client(timeout=60) as client:
            log('downloading states and transmission lines...')
            states = fetch_states(client)
            raw = fetch_lines(client)
        log(f'{len(states)} states, {len(raw)} lines')
        by_state = {}
        for line in raw:
            fkm, fpaths = forest_geometry(line['paths'], mask)
            line['fKm'] = min(line['km'], fkm) if line['km'] else fkm
            line['fPaths'] = fpaths
            for abbr in assign_states(line, states):
                by_state.setdefault(abbr, []).append(line)

    meta = write(states, by_state, out)
    log(f"done in {time.time() - started:.0f}s: {meta['lineCount']} line records, {meta['stateCount']} states -> {out}")
    return meta


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--from-dir', type=Path, help='reuse states.json + states/*.json from an existing export')
    parser.add_argument('--out', type=Path, default=GRID_DIR)
    args = parser.parse_args()
    sync(args.out, args.from_dir)
