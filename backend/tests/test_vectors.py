"""Head-fire arrows for a map view (GET .../vectors?bbox=&n=&hours=): sampled cells of a real bundle."""
import json
import math
import os

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform, transform_bounds

os.environ['GRID_AUTO_SYNC'] = '0'
import app.results as results
try:
    from test_results import Base, ca_bundle, write_bundle
except ImportError:                               # run as tests.test_vectors
    from tests.test_results import Base, ca_bundle, write_bundle

CRS = 'EPSG:32611'
ORIGIN, CELL, SHAPE = (330000, 3780000), 30, (192, 256)        # 5.8 km x 7.7 km
BARE = (slice(64, 128), slice(0, 64))                          # rate 0: not burnable
HOLE = (slice(0, 32), slice(128, 192))                         # nodata
FAST = (slice(128, 192), slice(192, 256))                      # 100 m/min: an hour's run is longer than the limit


def cell(row, col):
    x, y = ORIGIN[0] + (col + 0.5) * CELL, ORIGIN[1] - (row + 0.5) * CELL
    lon, lat = transform(CRS, 'EPSG:4326', [x], [y])
    return lon[0], lat[0]


def fields():
    rows, cols = np.mgrid[0:SHAPE[0], 0:SHAPE[1]]
    ros = (1 + cols * 0.05).astype('float32')
    ros[BARE] = 0
    ros[FAST] = 100
    direction = np.where(rows < 96, 90.0, 225.0).astype('float32')
    flame = (0.5 + rows * 0.01).astype('float32')
    return ros, direction, flame


def vector_bundle(root, name='arrows', result_id='smm-20261130-nisar', valid_to='2026-12-21T07:00:00Z', flame=True):
    trans = from_origin(*ORIGIN, CELL, CELL)
    bbox = list(transform_bounds(CRS, 'EPSG:4326', ORIGIN[0], ORIGIN[1] - SHAPE[0] * CELL, ORIGIN[0] + SHAPE[1] * CELL, ORIGIN[1]))
    directory = write_bundle(root, name, result_id, CRS, trans, SHAPE, valid_to, bbox)
    manifest = json.loads((directory / 'result.json').read_text())
    layers = dict(zip(('rate_of_spread', 'spread_direction', 'flame_length'), fields()))
    for layer, values in layers.items():
        if layer == 'flame_length' and not flame:
            continue
        values = values.copy()
        values[HOLE] = -9999
        with rasterio.open(directory / f'{layer}.tif', 'w', driver='GTiff', height=SHAPE[0], width=SHAPE[1], count=1,
                           dtype='float32', crs=CRS, transform=trans, nodata=-9999, compress='deflate', tiled=True) as ds:
            ds.write(values, 1)
        manifest['layers'][layer] = {'path': f'{layer}.tif', 'kind': 'continuous'}
    (directory / 'result.json').write_text(json.dumps(manifest))
    return directory


def run_of(feature):
    """Length in metres and true azimuth in degrees of an arrow, on the sphere."""
    (lon0, lat0), (lon1, lat1) = feature['geometry']['coordinates']
    p0, p1, dl = math.radians(lat0), math.radians(lat1), math.radians(lon1 - lon0)
    a = math.sin((p1 - p0) / 2) ** 2 + math.cos(p0) * math.cos(p1) * math.sin(dl / 2) ** 2
    bearing = math.degrees(math.atan2(math.sin(dl) * math.cos(p1), math.cos(p0) * math.sin(p1) - math.sin(p0) * math.cos(p1) * math.cos(dl)))
    return 2 * 6371008.8 * math.asin(math.sqrt(a)), bearing % 360


class VectorTests(Base):
    def bundles(self, root):
        ca_bundle(root)                                         # no spread layers
        vector_bundle(root)                                     # the newest: what `latest` resolves to
        vector_bundle(root, 'plain', 'smm-20261125-nisar', '2026-12-01T07:00:00Z', flame=False)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.base = '/v1/burnability/results/smm-20261130-nisar'

    async def arrows(self, base=None, **params):
        return await self.get((base or self.base) + '/vectors', params=params)

    def cells(self, body):
        """(row, col) of every arrow's start."""
        out = []
        for f in body['features']:
            x, y = transform('EPSG:4326', CRS, [f['geometry']['coordinates'][0][0]], [f['geometry']['coordinates'][0][1]])
            out.append((int((ORIGIN[1] - y[0]) // CELL), int((x[0] - ORIGIN[0]) // CELL)))
        return out

    async def test_metadata_says_where(self):
        meta = (await self.get(self.base)).json()
        self.assertTrue(meta['view_vectors_url'].endswith('/results/smm-20261130-nisar/vectors'))
        self.assertEqual(meta['view_vectors_limits'], {'max_n': 2000, 'max_hours': 12})
        self.assertNotIn('view_vectors_url', (await self.get(self.ca)).json())

    async def test_whole_result_is_a_lattice_of_sampled_cells(self):
        r = await self.arrows(n=200)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body['type'], body['result_id'], body['synthetic']), ('FeatureCollection', 'smm-20261130-nisar', False))
        self.assertEqual((body['step_cells'], body['spacing_m'], body['run_hours'], body['longest_m']), (16, 480.0, 1, 5000))
        ros, direction, flame = fields()
        cells = self.cells(body)
        want = [(r_, c_) for r_ in range(0, SHAPE[0], 16) for c_ in range(0, SHAPE[1], 16)
                if ros[r_, c_] > 0 and not (r_ < 32 and 128 <= c_ < 192)]
        self.assertEqual(sorted(cells), want)                     # no arrow on bare ground or where there is no data
        self.assertLessEqual(len(cells), 200)
        for f, (row, col) in zip(body['features'], cells):
            p = f['properties']
            self.assertEqual(set(p), {'rate_of_spread_m_min', 'direction_deg', 'flame_length_m', 'run_hours'})
            self.assertAlmostEqual(p['rate_of_spread_m_min'], float(ros[row, col]), places=2)
            self.assertEqual(p['direction_deg'], float(direction[row, col]))
            self.assertAlmostEqual(p['flame_length_m'], float(flame[row, col]), places=2)
            lon, lat = cell(row, col)                             # the arrow starts at the cell's centre
            self.assertAlmostEqual(f['geometry']['coordinates'][0][0], lon, places=5)
            self.assertAlmostEqual(f['geometry']['coordinates'][0][1], lat, places=5)
            length, bearing = run_of(f)
            run = min(float(ros[row, col]) * 60, 5000)            # an hour's run, at most 5 km
            self.assertAlmostEqual(length, run, delta=0.01 * run + 0.3)
            self.assertAlmostEqual(bearing, float(direction[row, col]), delta=0.5)
        self.assertTrue(any(run_of(f)[0] > 4900 for f in body['features']))

    async def test_view_decides_the_spacing_and_the_lattice_stays_put(self):
        whole = self.cells((await self.arrows(n=200)).json())
        lon0, lat0 = cell(150, 20)
        lon1, lat1 = cell(100, 120)
        body = (await self.arrows(bbox=f'{lon0},{lat0},{lon1},{lat1}', n=200)).json()
        self.assertEqual(body['step_cells'], 8)                   # a quarter of the area: half the spacing
        near = self.cells(body)
        self.assertTrue(all(100 <= r <= 150 and 20 <= c <= 120 and r % 8 == 0 and c % 8 == 0 for r, c in near))
        inside = [rc for rc in whole if 100 < rc[0] < 150 and 20 < rc[1] < 120]
        self.assertTrue(inside and set(inside) <= set(near))       # zooming in keeps the arrows already shown
        shifted = (await self.arrows(bbox=f'{lon0 + 0.002},{lat0},{lon1 + 0.002},{lat1}', n=200)).json()
        common = [rc for rc in near if 30 <= rc[1] <= 115]
        self.assertTrue(set(common) <= set(self.cells(shifted)))   # panning does not move them
        few = (await self.arrows(n=1)).json()
        self.assertEqual(self.cells(few), [(0, 0)])
        many = (await self.arrows(n=2000)).json()
        self.assertEqual(many['step_cells'], 8)
        self.assertLessEqual(len(many['features']), 2000)

    async def test_hours_scale_the_run(self):
        one = (await self.arrows(n=50)).json()['features']
        three = (await self.arrows(n=50, hours=3)).json()['features']
        self.assertEqual(len(one), len(three))
        self.assertEqual(three[0]['properties']['run_hours'], 3)
        slow = [(a, b) for a, b in zip(one, three) if a['properties']['rate_of_spread_m_min'] < 20]
        self.assertTrue(slow)
        for a, b in slow:
            self.assertAlmostEqual(run_of(b)[0], 3 * run_of(a)[0], delta=0.02 * run_of(b)[0] + 0.5)
        half = (await self.arrows(n=50, hours=0.5)).json()['features']
        self.assertEqual(half[0]['properties']['run_hours'], 0.5)

    async def test_outside_and_invalid_requests(self):
        away = await self.arrows(bbox='-100,40,-99,41')
        self.assertEqual((away.status_code, away.json()['features'], away.json()['step_cells']), (200, [], None))
        for params in ({'n': 0}, {'n': 2001}, {'hours': 0}, {'hours': 13}, {'bbox': '1,2,3'}, {'bbox': '-118,35,-119,34'}):
            self.assertEqual((await self.arrows(**params)).status_code, 422, params)
        self.assertEqual((await self.arrows(self.ca)).status_code, 404)                       # no spread layers
        self.assertEqual((await self.arrows('/v1/burnability/results/forest-demo-v1')).status_code, 404)
        self.assertEqual((await self.arrows(self.latest, n=10)).json()['result_id'], 'smm-20261130-nisar')
        # the bundle's own file is still served
        self.assertEqual((await self.get(self.base + '/vectors/vectors')).status_code, 200)

    async def test_without_flame_length(self):
        body = (await self.arrows('/v1/burnability/results/smm-20261125-nisar', n=50)).json()
        self.assertTrue(body['features'])
        self.assertTrue(all(f['properties']['flame_length_m'] is None for f in body['features']))

    async def test_large_rasters_use_the_kept_lattice(self):
        direct = (await self.arrows(n=200)).json()
        old = results.VIEW_VECTORS_DIRECT
        results.VIEW_VECTORS_DIRECT = 0
        try:
            kept = (await self.arrows(n=200)).json()
            lon0, lat0 = cell(150, 20)
            lon1, lat1 = cell(20, 220)
            part = (await self.arrows(bbox=f'{lon0},{lat0},{lon1},{lat1}', n=150)).json()
        finally:
            results.VIEW_VECTORS_DIRECT = old
        self.assertEqual(kept, direct)
        self.assertEqual(part, (await self.arrows(bbox=f'{lon0},{lat0},{lon1},{lat1}', n=150)).json())
        self.assertEqual(part['step_cells'], 16)
