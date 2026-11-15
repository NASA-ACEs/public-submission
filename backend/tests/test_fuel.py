"""/v1/grid with a real fuel-risk result (FUEL_RISK_RESULT): extra fields, and the opt-in spread / tier drives."""
import copy
import os
import tempfile
import unittest
from pathlib import Path

os.environ['GRID_AUTO_SYNC'] = '0'
from app.grid import fires as fires_mod
from app.grid import weather as weather_mod
from app.grid.forest import ForestMask
from app.grid.fuel import FuelRisk, densify
from app.grid.geo import lonlat_to_merc
from app.grid.service import RiskService
from app.grid.store import StateGrid
from app.results import Registry
try:
    from test_results import Base, ca_bundle      # risk_index rises from 0 in the west to 1 in the east of CA_BBOX
except ImportError:                               # run as tests.test_fuel
    from tests.test_results import Base, ca_bundle


def line(oid, lon, lat0=34.10, lat1=34.20, n=40):
    pts = [list(lonlat_to_merc(lon, lat0 + (lat1 - lat0) * i / (n - 1))) for i in range(n)]
    return dict(oid=oid, owner='UTIL', kv=115, sub1='A', sub2='B', km=round(111 * (lat1 - lat0), 1), fKm=0.0,
                paths=[pts], fPaths=[])


def fire(lon, lat, frp=20):
    x, y = lonlat_to_merc(lon, lat)
    return [dict(mx=x + dx, my=y, frp=frp, scanM=375, trackM=375, hoursOld=2.0) for dx in (-300, 0, 300)]


class FuelRiskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        ca_bundle(cls.tmp.name)
        cls.registry = Registry(cls.tmp.name, Path(cls.tmp.name) / 'cache', ttl=3600)
        cls.registry.scan(force=True)
        # one fire and one line 3 km to its west, in the low-risk west and in the high-risk east of the map
        cls.grid = StateGrid(dict(abbr='CA', center=[-118.8, 34.15], providers=[dict(owner='UTIL')]),
                             [line(1, -118.985), line(2, -118.685)])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def service(self, fuel=False, drives=()):
        grid = self.grid

        class Store:
            ready = True

            def grid(self, abbr):
                return grid if abbr == 'CA' else None

            def forest(self):
                return ForestMask({})
        service = RiskService(Store(), None, None, (lambda: self.registry.get('ca-20261020-nisar')) if fuel else None, drives)
        points = fire(-118.95, 34.15) + fire(-118.65, 34.15)
        service.load_fires = lambda state, hours, forest_only=True: (
            points, [], fires_mod.cluster(points, []), dict(totalDetections=6, keptDetections=6, forestOnly=forest_only))
        service.load_weather = lambda lon, lat: weather_mod.baseline()
        self.addCleanup(service.client.close)
        return service

    def situation(self, **kwargs):
        result = self.service(**kwargs).situation('CA', 72, 2000)
        result.pop('generatedAt')
        return result

    @staticmethod
    def stripped(result):
        """The payload without the fields the fuel-risk result adds."""
        out = copy.deepcopy(result)
        out.pop('fuelRisk', None)
        for cl in out['fires']['clusters']:
            cl.pop('fuelRiskIndex', None), cl.pop('fuelRiskClass', None)
        for sp in out['spread']:
            sp.pop('fuelBoost', None)
        for ln in out['threats']['lines']:
            ln.pop('fuelRisk', None)
            for sec in ln['sections']:
                sec.pop('fuelRiskIndex', None)
        return out

    def test_sampler(self):
        fuel = FuelRisk(self.registry.get('ca-20261020-nisar'))
        west, east, outside = lonlat_to_merc(-118.9, 34.15), lonlat_to_merc(-118.7, 34.15), lonlat_to_merc(-117, 34.15)
        values = fuel.sample([west, east, outside])
        self.assertAlmostEqual(values[0], 0.25, delta=0.02)
        self.assertAlmostEqual(values[1], 0.75, delta=0.02)
        self.assertTrue(values[2] != values[2])                           # NaN outside the map
        self.assertEqual([fuel.risk_class(v) for v in (0.1, 0.25, 0.75, 0.95, None)], [1, 2, 4, 5, None])
        self.assertIsNone(fuel.at_fire(*outside))
        self.assertIsNone(fuel.boost(None))
        mids, lengths = densify([west, east])
        self.assertAlmostEqual(lengths.sum(), 18400, delta=300)           # ground metres, not Mercator metres
        self.assertLessEqual(lengths.max(), 200)
        summary = fuel.along([[west, east], [outside, outside]])
        self.assertAlmostEqual(summary[0]['mean'], 0.5, delta=0.02)
        self.assertAlmostEqual(summary[0]['higher_length_m'] / summary[0]['covered_length_m'], 0.3, delta=0.03)
        self.assertIsNone(summary[1]['mean'])
        lines = fuel.lines(self.grid.lines)
        self.assertAlmostEqual(lines[2]['rank_mean'], 0.79, delta=0.02)
        self.assertAlmostEqual(lines[2]['mean_score_on_covered'], lines[2]['rank_mean'] * 0.25 / 0.6)
        self.assertEqual((lines[1]['higher_fraction_of_covered'], lines[2]['higher_fraction_of_covered']), (0, 1))

    def test_off_by_default(self):
        plain = self.situation()
        self.assertNotIn('fuelRisk', plain)
        self.assertNotIn('fuelRiskIndex', plain['fires']['clusters'][0])
        self.assertNotIn('fuelBoost', plain['spread'][0])
        self.assertEqual(plain, self.stripped(plain))
        missing = self.service(fuel=True)
        missing.fuel_source = lambda: None                                # configured, but no such result right now
        result = missing.situation('CA', 72, 2000)
        result.pop('generatedAt')
        self.assertEqual(result, plain)

    def test_fields_only(self):
        plain, fields = self.situation(), self.situation(fuel=True)
        self.assertEqual(self.stripped(fields), plain)                    # nothing else changes
        info = fields['fuelRisk']
        self.assertEqual((info['resultId'], info['layer'], info['threshold'], info['drives']),
                         ('ca-20261020-nisar', 'risk_index', 0.6, []))
        self.assertIsNone(info['spreadMultiplier'])
        by_x = sorted(fields['fires']['clusters'], key=lambda c: c['centroid'][0])
        self.assertAlmostEqual(by_x[0]['fuelRiskIndex'], 0.125, delta=0.02)
        self.assertAlmostEqual(by_x[1]['fuelRiskIndex'], 0.875, delta=0.02)
        self.assertEqual((by_x[0]['fuelRiskClass'], by_x[1]['fuelRiskClass']), (1, 5))

    def test_spread_drive(self):
        plain, driven = self.situation(), self.situation(fuel=True, drives={'spread'})
        head = lambda result: [s['headM'] for s in sorted(result['spread'], key=lambda s: s['ring'][0][0])]
        (west0, east0), (west1, east1) = head(plain), head(driven)
        self.assertLess(west1, west0)                                     # multiplier 0.8 + 0.6 * 0.125 < 1
        self.assertGreater(east1, east0)                                  # multiplier 0.8 + 0.6 * 0.875 > 1
        boosts = sorted(s['fuelBoost'] for s in driven['spread'])
        self.assertAlmostEqual(boosts[0], 0.875, delta=0.02)
        self.assertAlmostEqual(boosts[1], 1.325, delta=0.02)
        self.assertIn('0.8 + 0.6', driven['fuelRisk']['spreadMultiplier'])
        self.assertEqual(driven['threats']['totals'], plain['threats']['totals'])

    def test_tiers_drive(self):
        plain, driven = self.situation(), self.situation(fuel=True, drives={'tiers'})
        self.assertEqual(plain['threats']['lines'], [])                   # both lines 3 km from a fire: MONITOR only
        self.assertEqual(driven['spread'], plain['spread'])               # the envelope is not touched by this drive
        (flagged,) = driven['threats']['lines']
        self.assertEqual((flagged['oid'], flagged['tier']), (2, 'SHUT OFF'))   # high fuel within twice the buffer
        risk = flagged['fuelRisk']
        self.assertAlmostEqual(risk['index'], 0.79, delta=0.02)
        self.assertEqual(risk['riskClass'], 4)
        self.assertAlmostEqual(risk['highKm'], risk['coveredKm'])
        self.assertAlmostEqual(flagged['sections'][0]['fuelRiskIndex'], risk['index'], delta=0.01)
        self.assertEqual(driven['threats']['totals'], {'SHUT OFF': 1, 'PSPS': 0, 'MONITOR': 1})
        self.assertIn('0.25 / 0.6', driven['fuelRisk']['tierScale'])


class FuelRiskWiringTests(Base):
    async def asyncSetUp(self):
        self.saved = {k: os.environ.get(k) for k in ('FUEL_RISK_RESULT', 'FUEL_RISK_DRIVES')}
        os.environ.update(FUEL_RISK_RESULT='latest', FUEL_RISK_DRIVES='spread, nonsense')
        await super().asyncSetUp()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        for key, value in self.saved.items():
            os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)

    async def test_environment_selects_the_result(self):
        import app.main as api
        grid = api.app.state.grid
        self.assertEqual(grid.fuel_drives, {'spread'})
        self.assertEqual(grid.fuel().id, 'smm-20261101-nisar')            # latest
        self.assertEqual(grid.fuel().layer, 'risk_index')


class FuelRiskUnsetTests(Base):
    async def test_unset_environment_leaves_the_grid_alone(self):
        import app.main as api
        self.assertIsNone(api.app.state.grid.fuel_source)
        self.assertIsNone(api.app.state.grid.fuel())


if __name__ == '__main__':
    unittest.main()
