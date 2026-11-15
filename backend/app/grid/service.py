"""Builds the complete per-state risk picture the frontend renders as-is."""
import hashlib
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from ..assessment import assess_lines
from . import fires as fires_mod
from . import forest as forest_mod
from . import weather as weather_mod
from .fuel import FuelRisk
from .geo import lonlat_to_merc, merc_to_lonlat
from .spread import simulate
from .store import GridStore
from .threats import exposure, report

FIRES_TTL = 10 * 60
WEATHER_TTL = 30 * 60
SITUATION_TTL = 5 * 60
BURN_PAD_M = 25000


class TTLCache:
    def __init__(self, ttl, max_items=256):
        self.ttl, self.max_items = ttl, max_items
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
            return None

    def put(self, key, value):
        with self._lock:
            if len(self._data) >= self.max_items:
                self._data.pop(min(self._data, key=lambda k: self._data[k][0]))
            self._data[key] = (time.time(), value)
        return value


class RiskService:
    def __init__(self, store: GridStore, burn_raster: Path | None = None, burn_bbox=None, fuel_source=None, fuel_drives=()):
        self.store = store
        self.burn_raster = burn_raster
        self.burn_bbox = burn_bbox
        # Real fuel-risk result (FUEL_RISK_RESULT): a callable returning the bundle or None. Off by default.
        self.fuel_source = fuel_source
        self.fuel_drives = frozenset(fuel_drives)
        self._fuel = None
        self._fuel_lines = {}
        self.fires = TTLCache(FIRES_TTL)
        self.weather = TTLCache(WEATHER_TTL)
        self.situations = TTLCache(SITUATION_TTL, 64)
        self.bases = TTLCache(FIRES_TTL, 32)
        self.burn = {}
        self.tiles = OrderedDict()
        self._tile_lock = threading.Lock()
        self.client = httpx.Client(timeout=30)

    def reset(self):
        self.burn.clear()
        self._fuel_lines.clear()
        self.situations = TTLCache(SITUATION_TTL, 64)
        self.bases = TTLCache(FIRES_TTL, 32)

    # --- inputs -----------------------------------------------------------------
    def load_fires(self, state, hours, forest_only=True):
        key = (state['abbr'], hours, forest_only)
        return self.fires.get(key) or self.fires.put(
            key, fires_mod.load(self.client, state, hours, self.store.forest(), forest_only))

    def load_weather(self, lon, lat):
        key = (round(lon, 1), round(lat, 1))
        return self.weather.get(key) or self.weather.put(key, weather_mod.fetch(self.client, lon, lat))

    def fuel(self):
        """Sampler of the configured real result, or None (not configured, or no such result right now)."""
        result = self.fuel_source() if self.fuel_source else None
        if result is None:
            return None
        if self._fuel is None or self._fuel.key != (result.id, result.version):
            self._fuel, self._fuel_lines = FuelRisk(result), {}
        return self._fuel

    def fuel_summaries(self, grid, fuel):
        """Real fuel summaries of every line of a state (once per state and result version)."""
        key = (grid.summary['abbr'], fuel.key)
        if key not in self._fuel_lines:
            self._fuel_lines[key] = fuel.lines(grid.lines)
        return self._fuel_lines[key]

    def burn_summaries(self, grid):
        """Fuel-model summaries for lines crossing the burnability AOI (computed once per state)."""
        abbr = grid.summary['abbr']
        if abbr in self.burn:
            return self.burn[abbr]
        out = {}
        if self.burn_raster and self.burn_raster.exists() and self.burn_bbox:
            x0, y0 = lonlat_to_merc(self.burn_bbox[0], self.burn_bbox[1])
            x1, y1 = lonlat_to_merc(self.burn_bbox[2], self.burn_bbox[3])
            x0, y0, x1, y1 = x0 - BURN_PAD_M, y0 - BURN_PAD_M, x1 + BURN_PAD_M, y1 + BURN_PAD_M
            for line, box in zip(grid.lines, grid.bboxes):
                if box[2] < x0 or box[0] > x1 or box[3] < y0 or box[1] > y1:
                    continue
                paths = []
                for path in line['paths']:
                    run = []
                    for x, y in path:
                        if x0 <= x <= x1 and y0 <= y <= y1:
                            run.append(list(merc_to_lonlat(x, y)))
                        elif len(run) >= 2:
                            paths.append(run)
                            run = []
                        else:
                            run = []
                    if len(run) >= 2:
                        paths.append(run)
                if not paths:
                    continue
                feature = dict(type='Feature', id=line['oid'], geometry=dict(type='MultiLineString', coordinates=paths))
                try:
                    out[line['oid']] = assess_lines(self.burn_raster, [feature], self.burn_bbox, 0.25)['line_summaries'][0]
                except (ValueError, TypeError, KeyError):
                    continue
        self.burn[abbr] = out
        return out

    def forest_tile(self, z, y, x):
        key = (z, y, x)
        with self._tile_lock:
            if key in self.tiles:
                self.tiles.move_to_end(key)
                return self.tiles[key]
        try:
            res = self.client.get(f'{forest_mod.TILES_URL}/tile/{z}/{y}/{x}')
            content = res.content if res.status_code == 200 and res.headers.get('content-type', '').startswith('image') else None
        except httpx.HTTPError:
            return None
        with self._tile_lock:
            self.tiles[key] = content
            while len(self.tiles) > 4000:
                self.tiles.popitem(last=False)
        return content

    # --- output -----------------------------------------------------------------
    def base(self, grid, hours, forest_only=True):
        """Fires, weather, spread and per-line exposure; shared by every buffer/operator choice."""
        fuel = self.fuel()
        key = (grid.summary['abbr'], hours, forest_only) + ((fuel.key,) if fuel else ())
        cached = self.bases.get(key)
        if cached:
            return cached
        points, perimeters, clusters, stats = self.load_fires(grid.summary, hours, forest_only)
        anchor = clusters[0]['lonlat'] if clusters else grid.summary['center']
        weather = self.load_weather(*anchor)
        burn = self.burn_summaries(grid)
        fuel_fires, boost, tier_burn = {}, None, burn
        if fuel:
            fuel_fires = {cl['id']: fuel.at_fire(*cl['centroid']) for cl in clusters}
            if 'spread' in self.fuel_drives:
                boost = lambda cl: fuel.boost(fuel_fires.get(cl['id']))
            if 'tiers' in self.fuel_drives:
                # in the tiers, the real map replaces the demo score wherever it covers a line
                tier_burn = {**burn, **self.fuel_summaries(grid, fuel)}
        # Each fire spreads with its own local forecast (cached per 0.1 deg cell).
        local = {}
        top = clusters[:20]
        with ThreadPoolExecutor(8) as pool:
            for cl, wx in zip(top, pool.map(lambda cl: self.load_weather(*cl['lonlat']), top)):
                local[cl['id']] = wx
        sims = simulate(clusters, lambda cl: local.get(cl['id'], weather), grid, self.store.forest(), burn, boost)
        index = fires_mod.FireSpatialIndex(points)
        exp = exposure(grid, index, sims, clusters)
        return self.bases.put(key, dict(points=points, perimeters=perimeters, clusters=clusters, stats=stats, weather=weather,
                                        burn=burn, tier_burn=tier_burn, sims=sims, index=index, exposure=exp,
                                        fuel=fuel, fuel_fires=fuel_fires))

    def situation(self, abbr, hours, radius_m, owners=None, forest_only=True):
        grid = self.store.grid(abbr)
        if grid is None:
            return None
        all_owners = {p['owner'] for p in grid.summary['providers']}
        owners = all_owners if not owners else set(owners) & all_owners
        digest = hashlib.sha1('|'.join(sorted(owners)).encode()).hexdigest()
        fuel = self.fuel()
        key = (grid.summary['abbr'], hours, radius_m, digest, forest_only) + ((fuel.key,) if fuel else ())
        cached = self.situations.get(key)
        if cached:
            return cached

        b = self.base(grid, hours, forest_only)
        weather, sims = b['weather'], b['sims']
        threats = report(grid, owners, b['exposure'], b['index'], sims, weather, b['tier_burn'], self.store.forest(), radius_m,
                         b['clusters'])
        forest_by_cluster = {s['clusterId']: s['forestFraction'] for s in sims}

        result = dict(
            state=grid.summary['abbr'], generatedAt=int(time.time() * 1000), hours=hours, radiusM=radius_m,
            fires=dict(
                # [x, y, frp, rank]: rank 3/2/1 = large/medium/small fire this detection belongs to.
                points=[[round(p['mx']), round(p['my']), round(p['frp'], 1), p.get('rank', 1)] for p in b['points']],
                perimeters=[dict(name=p['name'], acres=round(p['acres']), rings=p['rings']) for p in b['perimeters']],
                clusters=[dict(id=c['id'], name=c['name'], centroid=[round(v) for v in c['centroid']],
                               bbox=[round(v) for v in c['bbox']], areaAcres=c['areaAcres'],
                               minHoursAgo=c['minHoursAgo'], forestFraction=forest_by_cluster.get(c['id']),
                               sizeClass=c['sizeClass'], maxFrp=c['maxFrp'], detections=c['pixelCount'])
                          for c in b['clusters']],
                filter=b['stats']),
            weather=dict(source=weather['source'], avgWindMph=weather['avgWindMph'],
                         dominantWindLabel=weather['dominantWindLabel'], minRhPct=weather['minRhPct']),
            spread=[dict(clusterId=s['clusterId'], ring=s['hourly'][-1]['ring'], acres=s['hourly'][-1]['acres'],
                         headM=round(s['hourly'][-1]['headM']), wind=s['wind'])
                    for s in sims],
            threats=threats,
        )
        if b['fuel']:
            self._add_fuel(result, b['fuel'], b['fuel_fires'], sims)
        return self.situations.put(key, result)

    def _add_fuel(self, result, fuel, fuel_fires, sims):
        """Fields from the real fuel-risk result: per fire, per threatened line and per section."""
        result['fuelRisk'] = fuel.describe(self.fuel_drives)
        for cl in result['fires']['clusters']:
            rank = fuel_fires.get(cl['id'])
            cl['fuelRiskIndex'] = None if rank is None else round(rank, 3)
            cl['fuelRiskClass'] = fuel.risk_class(rank)
        if 'spread' in self.fuel_drives:
            boosts = {s['clusterId']: s.get('fuelBoost') for s in sims}
            for s in result['spread']:
                s['fuelBoost'] = boosts.get(s['clusterId'])
        if 'simulate' in self.fuel_drives:
            by_id = {s['clusterId']: s for s in sims}
            for s in result['spread']:
                sim = by_id[s['clusterId']]
                s['simulated'] = fuel.grow(*sim['centroid'], sim['wind']['mph'], sim['wind']['bearingDeg'])
        lines = result['threats']['lines']
        parts = iter(fuel.along([sec['path'] for line in lines for sec in line['sections']]))
        for line in lines:
            covered = weighted = high = 0.0
            peak = None
            for sec in line['sections']:
                s = next(parts)
                sec['fuelRiskIndex'] = None if s['mean'] is None else round(s['mean'], 3)
                if s['mean'] is not None:
                    covered += s['covered_length_m']
                    weighted += s['mean'] * s['covered_length_m']
                    high += s['higher_length_m']
                    peak = s['max'] if peak is None else max(peak, s['max'])
            mean = weighted / covered if covered else None
            line['fuelRisk'] = dict(index=round(mean, 3), max=round(peak, 3), riskClass=fuel.risk_class(mean),
                                    highKm=round(high / 1000, 2), coveredKm=round(covered / 1000, 2)) if covered else None
