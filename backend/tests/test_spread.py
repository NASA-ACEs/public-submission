"""Fire growth from a point (GET .../spread): spreadsim on the component layers of a real bundle."""
import copy
import json
import math
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform, transform_bounds

os.environ['GRID_AUTO_SYNC'] = '0'
from app.grid import fires as fires_mod
from app.grid import weather as weather_mod
from app.grid.forest import ForestMask
from app.grid.service import RiskService
from app.grid.store import StateGrid
from app.results import Registry
try:
    from test_fuel import fire, line
    from test_results import Base, ca_bundle, write_bundle
except ImportError:                               # run as tests.test_spread
    from tests.test_fuel import fire, line
    from tests.test_results import Base, ca_bundle, write_bundle

CRS = 'EPSG:32611'
ORIGIN, CELL, SHAPE = (330000, 3780000), 30, (160, 200)        # 4.8 km x 6 km
START = (80, 70)                                               # row, col of the ignition used by the tests
BARRIER = slice(120, 126)                                      # columns: 180 m of bare ground across the whole grid
BLOCK = (slice(0, 41), slice(150, 200))                        # a non-burnable corner, wider than the snap distance
SCAR = slice(100, 160)                                         # rows where fuel age slows the fire to a quarter
ROS0, WAF = 5.0, 0.5                                           # m/min without wind; midflame wind = 0.5 x wind


def lonlat(row, col):
    x, y = ORIGIN[0] + (col + 0.5) * CELL, ORIGIN[1] - (row + 0.5) * CELL
    lon, lat = transform(CRS, 'EPSG:4326', [x], [y])
    return lon[0], lat[0]


def spread_bundle(root, name='grow', result_id='smm-20261130-nisar', valid_to='2026-12-21T07:00:00Z', plain=None):
    """A bundle as ml.map writes it, with the component layers of ml.spreadsim for one uniform fuel on flat ground:
    rate 5 m/min without wind, wind factor phi_w = U_midflame, so a 4 m/s wind triples the head rate."""
    trans = from_origin(*ORIGIN, CELL, CELL)
    bbox = list(transform_bounds(CRS, 'EPSG:4326', ORIGIN[0], ORIGIN[1] - SHAPE[0] * CELL, ORIGIN[0] + SHAPE[1] * CELL, ORIGIN[1]))
    directory = write_bundle(root, name, result_id, CRS, trans, SHAPE, valid_to, bbox)
    ros0 = np.full(SHAPE, ROS0, 'float32')
    ros0[:, BARRIER] = 0
    ros0[BLOCK] = 0
    perm = np.ones(SHAPE, 'float32')
    perm[SCAR] = 0.25
    const = dict(spread_wind_a=1.0, spread_wind_b=1.0, spread_slope_k=5.0, spread_waf=WAF, spread_wind_cap=100.0,
                 spread_intensity_k=100.0, slope=0.0, aspect=0.0, wind_speed=0.0, wind_from=0.0)
    layers = {'spread_ros0': ros0, 'spread_permeability': perm, **{k: np.full(SHAPE, v, 'float32') for k, v in const.items()}}
    if plain:       # as ml.map writes where NISAR's fuel state scaled the fuels: the same components from the fuel map alone
        layers.update({k + '_plain': v * (plain if k == 'spread_ros0' else 1) for k, v in list(layers.items())
                       if k.startswith('spread_') and k != 'spread_permeability'})
    manifest = json.loads((directory / 'result.json').read_text())
    for layer, values in layers.items():
        with rasterio.open(directory / f'{layer}.tif', 'w', driver='GTiff', height=SHAPE[0], width=SHAPE[1], count=1,
                           dtype='float32', crs=CRS, transform=trans, nodata=-9999, compress='deflate', tiled=True) as ds:
            ds.write(values, 1)
        manifest['layers'][layer] = {'path': f'{layer}.tif', 'kind': 'index' if layer == 'spread_permeability' else 'component'}
    (directory / 'result.json').write_text(json.dumps(manifest))
    return directory


def reach(feature):
    """How far a MultiPolygon feature extends from the ignition, in metres on the grid: (west, south, east, north)."""
    pts = np.array([p for poly in feature['geometry']['coordinates'] for ring in poly for p in ring])
    x, y = transform('EPSG:4326', CRS, pts[:, 0].tolist(), pts[:, 1].tolist())
    x0, y0 = ORIGIN[0] + (START[1] + 0.5) * CELL, ORIGIN[1] - (START[0] + 0.5) * CELL
    return x0 - min(x), y0 - min(y), max(x) - x0, max(y) - y0


class SpreadTests(Base):
    def bundles(self, root):
        ca_bundle(root)                 # no component layers
        spread_bundle(root)             # the newest: what `latest` resolves to

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.grow = '/v1/burnability/results/smm-20261130-nisar'
        self.lon, self.lat = lonlat(*START)

    async def run_fire(self, base=None, **params):
        params = {'lon': self.lon, 'lat': self.lat, **params}
        return await self.get((base or self.grow) + '/spread', params=params)

    async def test_metadata_capability_and_hidden_components(self):
        meta = (await self.get(self.grow)).json()
        ids = [l['id'] for l in meta['layers']]
        self.assertIn('spread_permeability', ids)
        self.assertFalse([i for i in ids if i.startswith('spread_ros') or i in ('slope', 'aspect', 'wind_speed')])
        self.assertEqual(len(meta['component_layers']), 11)
        self.assertEqual(meta['capabilities'], {'spread': True, 'spread_map_day_wind': True})
        self.assertTrue(meta['spread_url'].endswith('/results/smm-20261130-nisar/spread'))
        self.assertEqual(meta['spread_limits'], {'max_hours': 12, 'max_steps': 48, 'max_wind_m_s': 40})
        listed = {r['result_id']: r for r in (await self.get('/v1/burnability/results')).json()['results']}
        self.assertEqual(listed['smm-20261130-nisar']['capabilities'], {'spread': True})
        self.assertNotIn('spread_ros0', listed['smm-20261130-nisar']['layers'])
        plain = (await self.get(self.ca)).json()                     # a bundle without the layers says nothing
        self.assertNotIn('capabilities', plain)
        self.assertNotIn('spread_url', plain)
        self.assertEqual(plain['component_layers'], [])
        self.assertNotIn('capabilities', listed['ca-20261020-nisar'])
        # components stay reachable as data
        values = (await self.get(self.grow + '/point', params={'lon': self.lon, 'lat': self.lat})).json()['values']
        self.assertEqual(values['spread_ros0'], ROS0)
        self.assertEqual((await self.get(self.grow + '/assets/spread_ros0')).status_code, 200)

    async def test_many_steps_for_an_animation(self):
        # a fire every 10 minutes for 6 hours: 36 isochrones that only grow; one more than the limit is refused
        steps = [round(k / 6, 4) for k in range(1, 37)]
        r = await self.run_fire(wind_speed=0, wind_from=0, fuel_age='false', hours=','.join(map(str, steps)))
        self.assertEqual(r.status_code, 200)
        areas = {f['properties']['hours']: f['properties']['area_ha'] for f in r.json()['features']}
        self.assertEqual(len(areas), 36)
        ordered = [areas[h] for h in sorted(areas)]
        self.assertTrue(all(a <= b for a, b in zip(ordered, ordered[1:])) and ordered[0] > 0)
        too_many = ','.join(str(round(k / 10, 2)) for k in range(1, 50))
        self.assertEqual((await self.run_fire(hours=too_many)).status_code, 422)

    async def test_no_wind_is_a_circle(self):
        r = await self.run_fire(wind_speed=0, wind_from=0, fuel_age='false')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['type'], 'FeatureCollection')
        self.assertEqual([f['properties']['hours'] for f in body['features']], [6, 3, 1])
        by_hour = {f['properties']['hours']: f for f in body['features']}
        for hours in (1, 3):
            radius = ROS0 * 60 * hours
            self.assertAlmostEqual(by_hour[hours]['properties']['area_ha'], math.pi * radius ** 2 / 1e4, delta=0.1 * math.pi * radius ** 2 / 1e4)
            for side in reach(by_hour[hours]):
                self.assertAlmostEqual(side, radius, delta=2 * CELL)
        self.assertEqual(by_hour[1]['geometry']['type'], 'MultiPolygon')
        p = body['properties']
        self.assertAlmostEqual(p['head_rate_m_min'], ROS0, places=4)
        self.assertEqual((p['wind_source'], p['wind_10m_m_s'], p['fuel_age_permeability']), ('request', 0.0, False))
        self.assertFalse(p['edge_reached'])
        self.assertIn('no suppression', p['note'])
        self.assertEqual((body['result_id'], body['synthetic']), ('smm-20261130-nisar', False))

    async def test_wind_makes_an_ellipse_and_bare_ground_stops_it(self):
        calm = (await self.run_fire(wind_speed=0, wind_from=0, fuel_age='false')).json()
        body = (await self.run_fire(wind_speed=4, wind_from=270, fuel_age='false')).json()      # from the west
        p = body['properties']
        self.assertAlmostEqual(p['head_rate_m_min'], ROS0 * (1 + 4 * WAF), places=3)            # 15 m/min
        self.assertAlmostEqual(p['head_direction_deg'], 90, places=3)
        self.assertEqual((p['wind_10m_m_s'], p['wind_from_deg']), (4.0, 270.0))
        by_hour = {f['properties']['hours']: f for f in body['features']}
        back, _, head, _ = reach(by_hour[1])
        self.assertAlmostEqual(head, 900, delta=3 * CELL)                                       # 15 m/min for an hour
        self.assertTrue(60 < back < 260, back)                                                  # backing fire: about 130 m
        area = {f['properties']['hours']: f['properties']['area_ha'] for f in calm['features']}
        self.assertGreater(by_hour[1]['properties']['area_ha'], 1.5 * area[1])
        self.assertAlmostEqual(by_hour[1]['properties']['area_ha'], 55.4, delta=6)               # ellipse 515 m x 342 m
        # after 3 h the head would be 2.7 km east; 180 m of bare ground 1.5 km east stops it
        barrier = (BARRIER.start - START[1] - 0.5) * CELL                                      # its west edge, metres east
        self.assertAlmostEqual(reach(by_hour[3])[2], barrier, delta=1)
        self.assertAlmostEqual(reach(by_hour[6])[2], barrier, delta=1)

    async def test_map_day_wind_fuel_age_and_cache(self):
        default = (await self.run_fire()).json()                                # wind of the map day (calm), fuel age on
        self.assertEqual(default['properties']['wind_source'], 'map_day')
        self.assertTrue(default['properties']['fuel_age_permeability'])
        open_fuel = (await self.run_fire(fuel_age='false')).json()
        area = lambda body, h: next(f['properties']['area_ha'] for f in body['features'] if f['properties']['hours'] == h)
        self.assertEqual(area(default, 1), area(open_fuel, 1))                  # the scar is 600 m south: not reached in 1 h
        self.assertLess(area(default, 3), 0.97 * area(open_fuel, 3))            # and slows the fire once it is
        registry = __import__('app.main').main.app.state.results
        result = registry.get('smm-20261130-nisar')
        size = len(result._spreads)
        again = (await self.run_fire()).json()
        self.assertEqual(again, default)
        self.assertEqual(len(result._spreads), size)                            # answered from the cache
        custom = (await self.run_fire(hours='2,0.5')).json()
        self.assertEqual([f['properties']['hours'] for f in custom['features']], [2, 0.5])

    async def test_latest_alias(self):
        r = await self.run_fire(base=self.latest, wind_speed=0, wind_from=0)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['result_id'], 'smm-20261130-nisar')

    async def test_results_without_the_layers_answer_404(self):
        r = await self.run_fire(base=self.ca, lon=-118.8, lat=34.15)
        self.assertEqual(r.status_code, 404)
        self.assertIn('fire-growth layers', r.json()['detail'])
        r = await self.run_fire(base='/v1/burnability/results/forest-demo-v1', lon=-119.75, lat=37.25)
        self.assertEqual(r.status_code, 404)
        self.assertIn('demo', r.json()['detail'])
        self.assertEqual((await self.run_fire(base='/v1/burnability/results/nope')).status_code, 404)

    async def test_requests_that_cannot_be_answered_are_422(self):
        for params in ({'hours': '0'}, {'hours': '13'}, {'hours': ','.join(str(k / 5) for k in range(1, 50))}, {'hours': 'soon'}, {'hours': ''},
                       {'wind_speed': 5}, {'wind_from': 90}, {'wind_speed': 41, 'wind_from': 0},
                       {'wind_speed': 5, 'wind_from': 361}, {'lon': 200}, {'lat': -91}):
            r = await self.run_fire(**params)
            self.assertEqual(r.status_code, 422, params)
        r = await self.run_fire(lon=self.lon + 1)                               # inside no result
        self.assertEqual((r.status_code, r.json()['detail']), (422, 'The point is outside this result'))
        lon, lat = lonlat(20, 175)                                              # middle of the non-burnable corner
        r = await self.run_fire(lon=lon, lat=lat, wind_speed=0, wind_from=0)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()['detail'], 'No burnable fuel within 300 m of the point')
        lon, lat = lonlat(START[0], BARRIER.start + 2)                          # on the bare strip: starts beside it
        r = await self.run_fire(lon=lon, lat=lat, wind_speed=0, wind_from=0)
        self.assertEqual(r.status_code, 200)
        self.assertGreater(r.json()['features'][-1]['properties']['area_ha'], 5)


class FuelStateTests(Base):
    """A bundle that also carries the components without the NISAR fuel state: twice the rate of spread there."""

    def bundles(self, root):
        ca_bundle(root)
        spread_bundle(root)                                                     # no *_plain layers
        spread_bundle(root, name='both', result_id='smm-20261201-nisar', valid_to='2026-12-22T07:00:00Z', plain=2.0)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.both, self.grow = '/v1/burnability/results/smm-20261201-nisar', '/v1/burnability/results/smm-20261130-nisar'
        lon, lat = lonlat(*START)
        self.params = {'lon': lon, 'lat': lat, 'wind_speed': 0, 'wind_from': 0, 'fuel_age': 'false', 'hours': '1'}

    async def test_capability_and_hidden_layers(self):
        meta = (await self.get(self.both)).json()
        self.assertEqual(meta['capabilities'], {'spread': True, 'spread_map_day_wind': True, 'spread_fuel_state': True})
        self.assertFalse([l['id'] for l in meta['layers'] if l['id'].endswith('_plain')])
        self.assertEqual(len(meta['component_layers']), 18)
        self.assertNotIn('spread_fuel_state', (await self.get(self.grow)).json()['capabilities'])

    async def test_fuel_map_alone_is_another_fire(self):
        with_state = (await self.get(self.both + '/spread', params=self.params)).json()
        alone = (await self.get(self.both + '/spread', params={**self.params, 'fuel_state': 'false'})).json()
        self.assertEqual((with_state['properties']['nisar_fuel_state'], alone['properties']['nisar_fuel_state']), (True, False))
        self.assertAlmostEqual(with_state['properties']['head_rate_m_min'], ROS0, places=4)
        self.assertAlmostEqual(alone['properties']['head_rate_m_min'], 2 * ROS0, places=4)
        area = lambda body: body['features'][0]['properties']['area_ha']
        self.assertAlmostEqual(area(alone) / area(with_state), 4, delta=0.4)    # twice the radius of the circle
        again = (await self.get(self.both + '/spread', params=self.params)).json()
        self.assertEqual(again, with_state)                                      # the cache keeps the two apart

    async def test_result_without_the_plain_layers_is_422(self):
        r = await self.get(self.grow + '/spread', params={**self.params, 'fuel_state': 'false'})
        self.assertEqual(r.status_code, 422)
        self.assertIn('without the NISAR fuel state', r.json()['detail'])
        ok = (await self.get(self.grow + '/spread', params=self.params)).json()
        self.assertFalse(ok['properties']['nisar_fuel_state'])                   # nothing to switch off there


class GridSimulateTests(unittest.TestCase):
    """FUEL_RISK_DRIVES=simulate: /v1/grid adds the fire-growth simulation next to each spread envelope."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        spread_bundle(cls.tmp.name)
        cls.registry = Registry(cls.tmp.name, Path(cls.tmp.name) / 'cache', ttl=3600)
        cls.registry.scan(force=True)
        cls.lon, cls.lat = lonlat(*START)
        cls.grid = StateGrid(dict(abbr='CA', center=[cls.lon, cls.lat], providers=[dict(owner='UTIL')]),
                             [line(1, cls.lon - 0.03), line(2, -118.585)])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def situation(self, drives):
        grid = self.grid

        class Store:
            ready = True

            def grid(self, abbr):
                return grid if abbr == 'CA' else None

            def forest(self):
                return ForestMask({})
        service = RiskService(Store(), None, None, lambda: self.registry.get('smm-20261130-nisar'), drives)
        points = fire(self.lon, self.lat) + fire(-118.55, 34.15)             # on the map, and 25 km east of it
        service.load_fires = lambda state, hours, forest_only=True: (
            points, [], fires_mod.cluster(points, []), dict(totalDetections=6, keptDetections=6, forestOnly=forest_only))
        service.load_weather = lambda lon, lat: weather_mod.baseline()
        self.addCleanup(service.client.close)
        result = service.situation('CA', 72, 2000)
        result.pop('generatedAt')
        return result

    def test_simulation_is_added_only_with_the_drive(self):
        fields, driven = self.situation(()), self.situation({'simulate'})
        self.assertNotIn('simulated', fields['fuelRisk'])
        self.assertFalse([s for s in fields['spread'] if 'simulated' in s])
        self.assertIn('fire growth after (1, 3, 6) h', driven['fuelRisk']['simulated'])
        west, east = sorted(driven['spread'], key=lambda s: s['ring'][0][0])
        self.assertIsNone(east['simulated'])                                 # no model result under that fire
        grown = west['simulated']
        self.assertEqual([f['properties']['hours'] for f in grown['features']], [6, 3, 1])
        self.assertGreater(grown['features'][0]['properties']['area_ha'], grown['features'][-1]['properties']['area_ha'])
        p = grown['properties']
        self.assertEqual(p['wind_source'], 'request')
        self.assertAlmostEqual(p['wind_10m_m_s'], west['wind']['mph'] * 0.44704, places=2)
        self.assertAlmostEqual(p['wind_from_deg'], (west['wind']['bearingDeg'] + 180) % 360, places=3)
        self.assertEqual(grown['result_id'], 'smm-20261130-nisar')
        stripped = copy.deepcopy(driven)                                     # everything else is as without the drive
        stripped['fuelRisk'].pop('simulated')
        stripped['fuelRisk']['drives'] = []
        for s in stripped['spread']:
            s.pop('simulated')
        self.assertEqual(stripped, fields)


if __name__ == '__main__':
    unittest.main()
