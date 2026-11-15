"""Real result bundles (backend-result-1, as written by machine-learning ``ml.map``) served next to the demo."""
import asyncio
import io
import json
import math
import os
import tempfile
import unittest
from pathlib import Path

import httpx
import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

os.environ['GRID_AUTO_SYNC'] = '0'      # no network in tests
import app.main as api
import app.results as results

BREAKS = [0.2, 0.4, 0.6, 0.8]
CA_BBOX = [-119.0, 34.0, -118.6, 34.3]


def write_bundle(root, name, result_id, crs, transform, shape, valid_to, bbox, manifest=None, layer_paths=None):
    """A small bundle with the layers, dtypes and nodata of ml.map; values are smooth gradients."""
    directory = Path(root) / name
    directory.mkdir(parents=True)
    height, width = shape
    yy, xx = np.mgrid[0:height, 0:width]
    xx, yy = xx / (width - 1), yy / (height - 1)
    hole = (slice(10, 20), slice(10, 20))            # nodata block
    risk = xx.astype('float32')
    fields = {
        'lfmc': 60 + 100 * yy, 'lfmc_p05': 40 + 100 * yy, 'lfmc_p95': 80 + 100 * yy,
        'soil_moisture': 0.1 + 0.3 * xx, 'soil_moisture_change': (xx - 0.5) * 0.1,
        'lfmc_nisar_minus_smap': (yy - 0.5) * 10, 'p_large_fire': risk * 0.02,
        'risk_index': risk, 'susceptibility_index': risk}
    for layer, values in fields.items():
        a = values.astype('float32').copy()
        a[hole] = -9999
        with rasterio.open(directory / f'{layer}.tif', 'w', driver='GTiff', height=height, width=width, count=1,
                           dtype='float32', crs=crs, transform=transform, nodata=-9999, compress='deflate',
                           tiled=True) as ds:
            ds.write(a, 1)
    classes = (np.searchsorted(BREAKS, risk, side='right') + 1).astype('uint8')
    classes[hole] = 0
    with rasterio.open(directory / 'risk_class.tif', 'w', driver='GTiff', height=height, width=width, count=1,
                       dtype='uint8', crs=crs, transform=transform, nodata=0, compress='deflate', tiled=True) as ds:
        ds.write(classes, 1)
    (directory / 'vectors.geojson').write_text('{"type":"FeatureCollection","features":[]}')
    kinds = {'risk_class': 'class', 'p_large_fire': 'probability', 'risk_index': 'index', 'susceptibility_index': 'index'}
    layers = {n: {'path': f'{n}.tif', 'kind': kinds.get(n, 'continuous')} for n in [*fields, 'risk_class']}
    for layer, path in (layer_paths or {}).items():
        layers[layer]['path'] = path
    result = dict(
        schema_version='backend-result-1', result_id=result_id, synthetic=False, research_only=True,
        as_of=valid_to, valid_from=valid_to.replace('-21T', '-20T'), valid_to=valid_to, bbox_wgs84=bbox,
        assessment_layer='risk_index', fuel_context_layer='susceptibility_index', mode='ml',
        target='P(final fire size >= 400 ha | ignition)', model_version='lightgbm-core-1',
        valid_pixels=int(height * width - 100), source_items=[f'nisar-sme2__ca__2026101{i}__t013d071' for i in range(8)],
        published={}, vector_count=0, class_breaks=BREAKS, soil_source='nisar', nisar_share=0.42,
        nisar_days_between_scenes=12.0, nisar_effect=dict(cells=10, mean_abs_lfmc_pct=0.4),
        model_sha256={'fire.txt': 'abc'}, layers=layers, notes=['risk_index is a rank.', 'Retrospective daily map.'])
    result.update(manifest or {})
    (directory / 'result.json').write_text(json.dumps(result))
    (directory / 'provenance.json').write_text(json.dumps(dict(
        result, configuration=dict(models='/home/someone/aces/models/core', out='/home/someone/aces/outputs/x'),
        training=dict(table='/home/someone/aces/points.parquet', lfmc_rows=10))))
    return directory


def ca_bundle(root, name='ca', result_id='ca-20261020-nisar', valid_to='2026-10-21T07:00:00Z', **kwargs):
    left, bottom, right, top = transform_bounds('EPSG:4326', 'EPSG:6933', *CA_BBOX)
    left, top = math.floor(left / 200) * 200, math.ceil(top / 200) * 200
    shape = (int((top - bottom) / 200) + 1, int((right - left) / 200) + 1)
    return write_bundle(root, name, result_id, 'EPSG:6933', from_origin(left, top, 200, 200), shape, valid_to, CA_BBOX, **kwargs)


def smm_bundle(root, name='smm', result_id='smm-20261101-nisar', valid_to='2026-11-21T07:00:00Z'):
    transform, shape = from_origin(330000, 3780000, 30, 30), (120, 160)
    bbox = list(transform_bounds('EPSG:32611', 'EPSG:4326', 330000, 3780000 - 120 * 30, 330000 + 160 * 30, 3780000))
    return write_bundle(root, name, result_id, 'EPSG:32611', transform, shape, valid_to, bbox)


def tile_of(lon, lat, z):
    return int((lon + 180) / 360 * 2 ** z), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2 ** z)


class Base(unittest.IsolatedAsyncioTestCase):
    results_dir = True

    def bundles(self, root):
        ca_bundle(root)
        smm_bundle(root)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        api.ROOT = Path(self.tmp.name) / 'data'
        self.root = Path(self.tmp.name) / 'results'
        self.root.mkdir()
        self.bundles(self.root)
        self.env = {k: os.environ.get(k) for k in ('RESULTS_DIR', 'RESULTS_RESCAN_S', 'RESULTS_CACHE_DIR')}
        os.environ.pop('RESULTS_CACHE_DIR', None)
        os.environ['RESULTS_RESCAN_S'] = '3600'
        if self.results_dir:
            os.environ['RESULTS_DIR'] = str(self.root)
        else:
            os.environ.pop('RESULTS_DIR', None)

        asyncio.get_running_loop().slow_callback_duration = 30     # fixtures and the first scan are slow, not stuck

        async def heartbeat():
            while True:
                await asyncio.sleep(.01)
        self.pulse = asyncio.create_task(heartbeat())
        self.lifespan = api.app.router.lifespan_context(api.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test')
        self.ca = '/v1/burnability/results/ca-20261020-nisar'
        self.smm = '/v1/burnability/results/smm-20261101-nisar'
        self.latest = '/v1/burnability/results/latest'

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.pulse.cancel()
        for key, value in self.env.items():
            os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
        self.tmp.cleanup()

    async def get(self, path, **kwargs):
        return await self.client.get(path, **kwargs)


class RealResultTests(Base):
    async def test_listing_and_latest(self):
        body = (await self.get('/v1/burnability/results')).json()
        ids = [r['result_id'] for r in body['results']]
        self.assertEqual(ids, ['forest-demo-v1', 'smm-20261101-nisar', 'ca-20261020-nisar'])   # demo, then newest first
        self.assertEqual(body['latest'], 'smm-20261101-nisar')
        self.assertEqual(body['rejected'], [])
        real = body['results'][2]
        self.assertFalse(real['synthetic'])
        self.assertEqual(real['valid_to'], '2026-10-21T07:00:00Z')
        self.assertEqual(real['model_version'], 'lightgbm-core-1')
        self.assertEqual(real['bbox_wgs84'], CA_BBOX)
        self.assertEqual(real['metadata_url'], self.ca)
        health = (await self.get('/health')).json()
        self.assertEqual((health['real_results'], health['latest']), (2, 'smm-20261101-nisar'))
        latest = (await self.get(self.latest)).json()
        self.assertEqual(latest['result_id'], 'smm-20261101-nisar')
        self.assertIn('/results/smm-20261101-nisar/tiles/', latest['layers'][0]['tiles_url'])   # canonical URLs
        self.assertEqual(latest['grid']['crs'], 'EPSG:32611')
        self.assertEqual(latest['grid']['pixel_spacing_m'], 30)

    async def test_metadata(self):
        meta = (await self.get(self.ca)).json()
        self.assertFalse(meta['synthetic'])
        self.assertEqual(meta['manifest_schema_version'], 'backend-result-1')
        layers = {l['id']: l for l in meta['layers']}
        self.assertEqual(len(layers), 10)
        self.assertEqual(layers['lfmc']['range'], [30, 200])
        self.assertEqual(layers['lfmc']['units'], '% dry mass')
        self.assertEqual(layers['soil_moisture']['range'], [0, 0.5])
        self.assertEqual(layers['risk_index']['range'], [0, 1])
        self.assertEqual(layers['risk_index']['kind'], 'index')
        self.assertTrue(layers['risk_class']['discrete'])
        self.assertEqual([s[0] for s in layers['risk_class']['legend']['stops']], [1, 2, 3, 4, 5])
        self.assertEqual(layers['risk_class']['nodata'], 0)
        lo, hi = layers['soil_moisture_change']['range']                 # difference layer: symmetric about zero
        self.assertAlmostEqual(lo, -hi)
        self.assertTrue(0.03 < hi <= 0.05)
        self.assertTrue(0.015 < layers['p_large_fire']['range'][1] <= 0.02)   # small probabilities: percentile scale
        self.assertTrue(all(not l['synthetic'] for l in meta['layers']))
        self.assertEqual(meta['assessment_layer'], 'risk_index')
        self.assertEqual(meta['class_breaks'], BREAKS)
        self.assertEqual(meta['default_threshold'], 0.6)
        self.assertEqual(meta['target'], 'P(final fire size >= 400 ha | ignition)')
        self.assertEqual(meta['soil_source'], 'nisar')
        self.assertEqual(meta['nisar_share'], 0.42)
        self.assertEqual(meta['nisar_effect']['cells'], 10)
        self.assertEqual(meta['source_item_count'], 8)
        self.assertEqual(len(meta['source_items_sample']), 5)
        self.assertEqual(meta['notices'], ['risk_index is a rank.', 'Retrospective daily map.'])
        self.assertEqual(meta['extra']['nisar_days_between_scenes'], 12.0)   # unknown manifest keys pass through
        self.assertIn('model_sha256', meta['extra'])
        self.assertEqual(meta['grid']['crs'], 'EPSG:6933')
        self.assertEqual(meta['max_native_zoom'], 10)
        self.assertEqual((await self.get('/v1/burnability/results/nope')).status_code, 404)

    async def test_downloads_previews_tilejson(self):
        for base in (self.ca, self.smm):
            meta = (await self.get(base)).json()
            for layer in meta['layers']:
                r = await self.get(layer['download_url'])
                self.assertEqual((r.status_code, r.headers['x-synthetic']), (200, 'false'))
                self.assertTrue(r.content.startswith((b'II', b'MM')))
                r = await self.get(layer['preview_url'] + '?width=128')
                im = Image.open(io.BytesIO(r.content))
                self.assertEqual(im.width, 128)
                self.assertGreater(im.getextrema()[3][1], 0)
                tj = (await self.get(layer['tilejson_url'])).json()
                self.assertEqual(tj['bounds'], meta['bbox_wgs84'])
        self.assertEqual((await self.get(f'{self.ca}/assets/missing')).status_code, 404)
        self.assertEqual((await self.get(f'{self.ca}/preview/lfmc.png?width=10000')).status_code, 422)

    async def test_tiles(self):
        for base, (lon, lat) in ((self.ca, (-118.8, 34.15)), (self.smm, (-118.82, 34.13))):
            for layer in ('risk_index', 'risk_class', 'lfmc_nisar_minus_smap'):
                for z in (6, 9, 12, 15):
                    x, y = tile_of(lon, lat, z)
                    path = f'{base}/tiles/{layer}/{z}/{x}/{y}.png'
                    r = await self.get(path)
                    self.assertEqual((r.status_code, r.headers['x-synthetic']), (200, 'false'))
                    im = Image.open(io.BytesIO(r.content))
                    self.assertEqual(im.size, (256, 256))
                    self.assertGreater(im.getextrema()[3][1], 0, path)
                    self.assertEqual((await self.get(path, headers={'If-None-Match': r.headers['etag']})).status_code, 304)
        blank = await self.get(f'{self.smm}/tiles/lfmc/8/0/0.png')        # far from the UTM zone of the raster
        self.assertEqual(Image.open(io.BytesIO(blank.content)).getextrema()[3], (0, 0))
        x, y = tile_of(-118.82, 34.13, 12)
        self.assertEqual((await self.get(f'{self.latest}/tiles/lfmc/12/{x}/{y}.png')).content,
                         (await self.get(f'{self.smm}/tiles/lfmc/12/{x}/{y}.png')).content)
        self.assertEqual((await self.get(f'{self.ca}/tiles/lfmc/19/0/0.png')).status_code, 422)
        self.assertEqual((await self.get(f'{self.ca}/tiles/nope/8/0/0.png')).status_code, 404)

    async def test_tile_colours_follow_values(self):
        # risk_index rises west to east: the east side of the tile is redder (lower green) than the west
        x, y = tile_of(-118.8, 34.15, 9)
        im = np.asarray(Image.open(io.BytesIO((await self.get(f'{self.ca}/tiles/risk_index/9/{x}/{y}.png')).content)))
        cols = np.flatnonzero((im[:, :, 3] > 0).any(0))
        west, east = im[:, cols[2], :], im[:, cols[-3], :]
        self.assertGreater(west[west[:, 3] > 0][:, 1].mean(), east[east[:, 3] > 0][:, 1].mean() + 40)

    async def test_point(self):
        p = (await self.get(f'{self.ca}/point?lon=-118.7&lat=34.1')).json()
        self.assertTrue(p['valid'])
        self.assertFalse(p['synthetic'])
        self.assertAlmostEqual(p['values']['risk_index'], 0.75, delta=0.02)        # 3/4 of the way east
        self.assertAlmostEqual(p['values']['p_large_fire'], p['values']['risk_index'] * 0.02, places=5)
        self.assertEqual(p['values']['risk_class'], 4)
        self.assertEqual(p['units']['lfmc'], '% dry mass')
        self.assertIsNotNone(p['sampled_cell_center'])
        outside = (await self.get(f'{self.ca}/point?lon=0&lat=0')).json()
        self.assertEqual((outside['valid'], outside['reason']), (False, 'OUTSIDE_AOI'))
        self.assertTrue(all(v is None for v in outside['values'].values()))
        with rasterio.open(self.root / 'ca' / 'risk_index.tif') as ds:           # centre of the nodata block
            lon, lat = rasterio.warp.transform(ds.crs, 'EPSG:4326', *[[v] for v in ds.xy(15, 15)])
        hole = (await self.get(f'{self.ca}/point?lon={lon[0]}&lat={lat[0]}')).json()
        self.assertEqual((hole['valid'], hole['reason']), (False, 'NO_VALID_DATA'))
        smm = (await self.get(f'{self.smm}/point?lon=-118.82&lat=34.13')).json()
        self.assertTrue(smm['valid'])

    async def test_numeric_grid_is_bounded(self):
        grid = (await self.get(f'{self.ca}/grid/risk_index')).json()
        self.assertEqual(grid['crs'], 'EPSG:3857')
        self.assertEqual(len(grid['values']), grid['width'] * grid['height'])
        self.assertLessEqual(len(grid['values']), grid['max_cells'])
        self.assertIn(None, grid['values'])
        self.assertTrue(all(0 <= v <= 1 for v in grid['values'] if v is not None))
        self.assertFalse(grid['synthetic'])
        self.assertEqual(grid['threshold'], 0.6)
        self.assertEqual((grid['transform'][1], grid['native_support_m']), (0, 200))
        self.assertLess(grid['transform'][4], 0)
        small = (await self.get(f'{self.ca}/grid/risk_index?max_cells=100')).json()
        self.assertTrue(small['decimated'])
        self.assertLessEqual(len(small['values']), 100)
        self.assertGreater(small['decimation'], 1)
        self.assertGreater(small['cell_size_m'], 200)
        window = (await self.get(f'{self.ca}/grid/lfmc?bbox=-118.9,34.1,-118.8,34.2')).json()
        self.assertEqual(window['bbox_wgs84'], [-118.9, 34.1, -118.8, 34.2])
        self.assertFalse(window['decimated'])
        self.assertLess(len(window['values']), len(grid['values']))
        self.assertIsNone(window['threshold'])
        classes = (await self.get(f'{self.ca}/grid/risk_class')).json()
        self.assertTrue(all(v in (1, 2, 3, 4, 5) for v in classes['values'] if v is not None))
        self.assertEqual((await self.get(f'{self.ca}/grid/risk_index?bbox=0,0,1,1')).status_code, 422)
        self.assertEqual((await self.get(f'{self.ca}/grid/risk_index?max_cells=9999999')).status_code, 422)
        self.assertEqual((await self.get(f'{self.ca}/grid/nope')).status_code, 404)

    async def test_assess_lines(self):
        features = [{'type': 'Feature', 'id': 'line-A', 'geometry': {
            'type': 'LineString', 'coordinates': [[-119.05, 34.15], [-118.55, 34.15]]}}]
        r = await self.client.post(f'{self.ca}/assess-lines', json={'features': features})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body['layer_id'], body['synthetic'], body['threshold']), ('risk_index', False, 0.6))
        self.assertEqual(body['region_bbox_wgs84'], CA_BBOX)
        s = body['line_summaries'][0]
        self.assertGreater(s['unknown_length_m'], 0)                       # the line starts and ends outside the map
        self.assertAlmostEqual(s['higher_fraction_of_covered'], 0.4, delta=0.03)   # rank >= 0.6 on a west-east ramp
        self.assertAlmostEqual(s['mean_score_on_covered'], 0.5, delta=0.03)
        self.assertAlmostEqual(s['total_length_m'], s['higher_length_m'] + s['lower_length_m'] + s['unknown_length_m'], places=4)
        low = (await self.client.post(f'{self.ca}/assess-lines', json={'features': features, 'threshold': 0})).json()
        self.assertAlmostEqual(low['line_summaries'][0]['higher_fraction_of_covered'], 1)
        prob = await self.client.post(f'{self.ca}/assess-lines', json={'features': features, 'layer': 'p_large_fire', 'threshold': 0.01})
        self.assertAlmostEqual(prob.json()['line_summaries'][0]['higher_fraction_of_covered'], 0.5, delta=0.03)
        for bad in ({'features': features, 'layer': 'lfmc'}, {'features': features, 'layer': 'p_large_fire'},
                    {'features': []}, {'features': features, 'threshold': 2}):
            self.assertEqual((await self.client.post(f'{self.ca}/assess-lines', json=bad)).status_code, 422)
        via_latest = (await self.client.post(f'{self.latest}/assess-lines', json={'features': features})).json()
        self.assertEqual(via_latest['result_id'], 'smm-20261101-nisar')

    async def test_provenance_hides_server_paths(self):
        body = (await self.get(f'{self.ca}/provenance')).json()
        self.assertFalse(body['synthetic'])
        self.assertEqual(body['configuration']['models'], 'core')
        self.assertEqual(body['training'], {'table': 'points.parquet', 'lfmc_rows': 10})
        self.assertNotIn('/home/', json.dumps(body))

    async def test_vectors(self):
        for vector_id in ('vectors', 'illustrative_pathways'):
            body = (await self.get(f'{self.ca}/vectors/{vector_id}')).json()
            self.assertEqual((body['features'], body['synthetic'], body['total']), ([], False, 0))
        self.assertEqual((await self.get(f'{self.ca}/vectors/nope')).status_code, 404)
        self.assertEqual((await self.get('/v1/burnability/results/forest-demo-v1/vectors/vectors')).status_code, 404)


class RegistryTests(Base):
    def bundles(self, root):
        ca_bundle(root)
        ca_bundle(root, 'escape', 'escape-1', layer_paths={'lfmc': '../ca/lfmc.tif'})
        ca_bundle(root, 'absolute', 'absolute-1', layer_paths={'lfmc': str(Path(root) / 'ca' / 'lfmc.tif')})
        linked = ca_bundle(root, 'linked', 'linked-1')
        (linked / 'lfmc.tif').unlink()
        (linked / 'lfmc.tif').symlink_to(Path(root) / 'ca' / 'lfmc.tif')
        missing = ca_bundle(root, 'missing', 'missing-1')
        (missing / 'risk_class.tif').unlink()
        ca_bundle(root, 'schema', 'schema-1', manifest={'schema_version': 'something-else'})
        ca_bundle(root, 'reserved', 'latest')
        ca_bundle(root, 'badid', '../evil')
        broken = Path(root) / 'broken'
        broken.mkdir()
        (broken / 'result.json').write_text('{not json')
        notraster = ca_bundle(root, 'notraster', 'notraster-1')
        (notraster / 'lfmc.tif').write_text('not a tiff')

    async def test_invalid_bundles_are_rejected(self):
        body = (await self.get('/v1/burnability/results')).json()
        self.assertEqual([r['result_id'] for r in body['results']], ['forest-demo-v1', 'ca-20261020-nisar'])
        reasons = {r['directory']: r['reason'] for r in body['rejected']}
        self.assertEqual(set(reasons), {'escape', 'absolute', 'linked', 'missing', 'schema', 'reserved', 'badid',
                                        'broken', 'notraster'})
        self.assertIn('leaves the bundle', reasons['escape'])
        self.assertIn('relative', reasons['absolute'])
        self.assertIn('leaves the bundle', reasons['linked'])
        self.assertIn('missing', reasons['missing'])
        self.assertNotIn(str(self.root), json.dumps(body))                 # no server paths in the response
        for result_id in ('escape-1', 'absolute-1', 'linked-1', 'missing-1', 'schema-1', 'notraster-1'):
            self.assertEqual((await self.get(f'/v1/burnability/results/{result_id}')).status_code, 404)
        self.assertEqual((await self.get('/v1/burnability/results/..%2Fevil')).status_code, 404)

    async def test_rescan_picks_up_new_and_changed_bundles(self):
        smm_bundle(self.root)
        self.assertEqual((await self.get(self.smm)).status_code, 404)       # not rescanned yet (long TTL in tests)
        body = (await self.client.post('/v1/burnability/results/rescan')).json()
        self.assertEqual(body['latest'], 'smm-20261101-nisar')
        self.assertEqual((await self.get(self.smm)).status_code, 200)
        manifest = self.root / 'ca' / 'result.json'
        data = json.loads(manifest.read_text())
        data['model_version'] = 'lightgbm-full-2'
        manifest.write_text(json.dumps(data))
        os.utime(manifest, ns=(manifest.stat().st_atime_ns, manifest.stat().st_mtime_ns + 10_000_000))
        await self.client.post('/v1/burnability/results/rescan')
        self.assertEqual((await self.get(self.ca)).json()['model_version'], 'lightgbm-full-2')
        api.app.state.results.ttl = 0                                       # TTL-driven rescan, no explicit call
        (self.root / 'missing' / 'risk_class.tif').write_bytes((self.root / 'ca' / 'risk_class.tif').read_bytes())
        self.assertEqual((await self.get('/v1/burnability/results/missing-1')).status_code, 200)

    async def test_overviews_go_to_the_cache_not_the_bundle(self):
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in (self.root / 'ca').iterdir()}
        limit, results.OVERVIEW_MIN_PIXELS = results.OVERVIEW_MIN_PIXELS, 1000
        try:
            x, y = tile_of(-118.8, 34.15, 7)
            first = await self.get(f'{self.ca}/tiles/lfmc/7/{x}/{y}.png')
            self.assertGreater(Image.open(io.BytesIO(first.content)).getextrema()[3][1], 0)
            cached = list((api.ROOT / 'results-cache' / 'ca-20261020-nisar').glob('lfmc-*.tif'))
            self.assertEqual(len(cached), 1)
            with rasterio.open(cached[0]) as ds:
                self.assertTrue(ds.overviews(1))
            with rasterio.open(self.root / 'ca' / 'lfmc.tif') as ds:
                self.assertFalse(ds.overviews(1))
        finally:
            results.OVERVIEW_MIN_PIXELS = limit
        after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in (self.root / 'ca').iterdir()}
        self.assertEqual(before, after)

    async def test_direction_layers_are_never_averaged(self):
        # compass directions alternating between 350 and 10 degrees: any mean of them would point south
        with rasterio.open(self.root / 'ca' / 'lfmc.tif') as ds:
            profile, shape = ds.profile, ds.shape
        a = np.where(np.indices(shape).sum(axis=0) % 2 == 0, 350, 10).astype('float32')
        with rasterio.open(self.root / 'ca' / 'spread_direction.tif', 'w', **profile) as ds:
            ds.write(a, 1)
        manifest = json.loads((self.root / 'ca' / 'result.json').read_text())
        manifest['layers']['spread_direction'] = {'path': 'spread_direction.tif', 'kind': 'continuous', 'units': 'degrees'}
        (self.root / 'ca' / 'result.json').write_text(json.dumps(manifest))
        self.assertEqual((await self.client.post('/v1/burnability/results/rescan')).status_code, 200)
        limit, results.OVERVIEW_MIN_PIXELS = results.OVERVIEW_MIN_PIXELS, 1000
        try:
            body = (await self.get(f'{self.ca}/grid/spread_direction?max_cells=400')).json()
        finally:
            results.OVERVIEW_MIN_PIXELS = limit
        self.assertTrue(body['decimated'])
        self.assertEqual(body['resampling'], 'nearest')
        self.assertTrue({v for v in body['values'] if v is not None} <= {350.0, 10.0})

    async def test_direction_layers_get_a_full_circle_and_a_closed_palette(self):
        with rasterio.open(self.root / 'ca' / 'lfmc.tif') as ds:
            profile = ds.profile
            a = np.where(ds.read(1) == ds.nodata, ds.nodata, 200).astype('float32')     # every direction is 200: percentiles would collapse
        for name in ('spread_direction', 'slope'):
            with rasterio.open(self.root / 'ca' / f'{name}.tif', 'w', **profile) as ds:
                ds.write(a, 1)
        manifest = json.loads((self.root / 'ca' / 'result.json').read_text())
        manifest['layers']['spread_direction'] = {'path': 'spread_direction.tif', 'kind': 'continuous', 'units': 'degrees'}
        manifest['layers']['slope'] = {'path': 'slope.tif', 'kind': 'continuous', 'units': 'degrees'}
        manifest['rank_reference'] = {'ignitions': 13333, 'large': 268, 'built_up_share_at_most': 0.1}
        (self.root / 'ca' / 'result.json').write_text(json.dumps(manifest))
        await self.client.post('/v1/burnability/results/rescan')
        body = (await self.get(self.ca)).json()
        layers = {layer['id']: layer for layer in body['layers']}
        direction, stops = layers['spread_direction'], layers['spread_direction']['legend']['stops']
        self.assertEqual(direction['range'], [0, 360])
        self.assertTrue(direction['cyclic'])
        self.assertEqual([stops[0][0], stops[-1][0]], [0, 360])
        self.assertEqual(stops[0][1], stops[-1][1])
        self.assertFalse(layers['slope']['cyclic'])                 # degrees, but not a compass direction
        self.assertFalse(layers['risk_index']['cyclic'])
        self.assertEqual(body['rank_reference']['ignitions'], 13333)
        self.assertNotIn('rank_reference', body['extra'])
        # 1 and 359 degrees are neighbours: nearly the same colour, unlike with a linear ramp
        near = [np.asarray(Image.open(io.BytesIO(results.colorize(np.full((1, 1), v, 'float32'), api.app.state.results.get('ca-20261020-nisar').layers['spread_direction']))))[0, 0, :3].astype(int)
                for v in (1.0, 359.0, 180.0)]
        self.assertLess(np.abs(near[0] - near[1]).max(), 12)
        self.assertGreater(np.abs(near[0] - near[2]).max(), 100)
        # a manifest may still give its own range and palette; the legend is then not a compass
        manifest['layers']['spread_direction'].update(range=[90, 270], palette=['#000000', '#ffffff'])
        (self.root / 'ca' / 'result.json').write_text(json.dumps(manifest))
        await self.client.post('/v1/burnability/results/rescan')
        layers = {layer['id']: layer for layer in (await self.get(self.ca)).json()['layers']}
        self.assertEqual(layers['spread_direction']['range'], [90, 270])
        self.assertFalse(layers['spread_direction']['cyclic'])

    async def test_land_and_weather_layers(self):
        # ml.map from v3.3.1: susceptibility_index is the rank on an ordinary day, weather_factor the day's odds against it
        palette = ['#2166ac', '#67a9cf', '#d1e5f0', '#f7f7f7', '#faebe3', '#fce0cf', '#fbceb6', '#f7b799', '#f29f7e',
                   '#e58268', '#d86651', '#ca4842', '#bb2a34', '#a51429', '#860a24', '#67001f']
        with rasterio.open(self.root / 'ca' / 'lfmc.tif') as ds:
            profile = ds.profile
            a = np.where(ds.read(1) == ds.nodata, ds.nodata, 0.6).astype('float32')     # percentiles would give a range far from 0.25-4
        with rasterio.open(self.root / 'ca' / 'weather_factor.tif', 'w', **profile) as ds:
            ds.write(a, 1)
        path = self.root / 'ca' / 'result.json'
        manifest = json.loads(path.read_text())
        before = {layer['id']: layer for layer in (await self.get(self.ca)).json()['layers']}
        self.assertEqual(before['susceptibility_index']['title'], 'Risk rank (fuel context)')     # a copy of risk_index
        manifest['layers']['weather_factor'] = {'path': 'weather_factor.tif', 'kind': 'continuous', 'units': 'x',
                                                'range': [0.25, 4.0], 'palette': palette}
        manifest['ordinary_day'] = {'vs': 3.05, 'vpd': 1.74}
        path.write_text(json.dumps(manifest))
        await self.client.post('/v1/burnability/results/rescan')
        body = (await self.get(self.ca)).json()
        layers = {layer['id']: layer for layer in body['layers']}
        factor = layers['weather_factor']
        self.assertEqual(factor['title'], 'Weather factor')
        self.assertEqual(factor['range'], [0.25, 4.0])                 # the manifest's, not percentiles of the raster
        self.assertEqual(factor['neutral'], 1.0)
        self.assertEqual([c for _, c in factor['legend']['stops']], palette)
        self.assertEqual(factor['legend']['stops'][3], [1.0, '#f7f7f7'])        # white where the day changes nothing
        self.assertIsNone(layers['risk_index']['neutral'])
        self.assertEqual(layers['susceptibility_index']['title'], 'Land susceptibility')
        self.assertEqual(body['ordinary_day'], {'vs': 3.05, 'vpd': 1.74})
        self.assertNotIn('ordinary_day', body['extra'])
        layer = api.app.state.results.get('ca-20261020-nisar').layers['weather_factor']
        colour = lambda v: np.asarray(Image.open(io.BytesIO(results.colorize(np.full((1, 1), v, 'float32'), layer))))[0, 0, :3].astype(int)
        self.assertLess(np.abs(colour(1.0) - [247, 247, 247]).max(), 3)
        self.assertGreater(colour(0.3)[2], colour(0.3)[0])              # below 1: blue
        self.assertGreater(colour(3.0)[0], colour(3.0)[2])              # above 1: red
        x, y = tile_of(-118.8, 34.15, 9)
        self.assertEqual((await self.get(f'{self.ca}/tiles/weather_factor/9/{x}/{y}.png')).status_code, 200)

    async def test_vector_features_and_bbox_filter(self):
        features = [dict(type='Feature', id=0, geometry=dict(type='LineString', coordinates=[[-118.9, 34.1], [-118.8, 34.2]]), properties={}),
                    dict(type='Feature', id=1, geometry=dict(type='Point', coordinates=[-118.65, 34.25]), properties={}),
                    dict(type='Feature', id=2, geometry=None, properties={})]
        (self.root / 'ca' / 'vectors.geojson').write_text(json.dumps(dict(type='FeatureCollection', features=features)))
        manifest = self.root / 'ca' / 'result.json'
        os.utime(manifest, ns=(manifest.stat().st_atime_ns, manifest.stat().st_mtime_ns + 10_000_000))
        await self.client.post('/v1/burnability/results/rescan')
        everything = (await self.get(f'{self.ca}/vectors/vectors?zoom=12')).json()
        self.assertEqual([f['id'] for f in everything['features']], [0, 1])
        east = (await self.get(f'{self.ca}/vectors/vectors?zoom=12&bbox=-118.7,34.2,-118.6,34.3')).json()
        self.assertEqual([f['id'] for f in east['features']], [1])
        self.assertEqual((await self.get(self.ca)).json()['vector_layers'][0]['feature_count'], 2)


class DemoUnchangedTests(Base):
    results_dir = False

    async def test_without_results_dir_only_the_demo_is_served(self):
        self.assertEqual((await self.get('/v1/burnability/results')).json(),
                         {'results': [{'result_id': 'forest-demo-v1', 'synthetic': True,
                                       'metadata_url': '/v1/burnability/results/forest-demo-v1'}]})
        self.assertEqual((await self.get('/health')).json(), {'status': 'ok', 'synthetic': True, 'result_id': 'forest-demo-v1'})
        self.assertEqual((await self.get(self.ca)).status_code, 404)
        demo = (await self.get('/v1/burnability/results/forest-demo-v1')).json()
        self.assertEqual((await self.get(self.latest)).json(), demo)        # latest falls back to the demo
        self.assertTrue(demo['synthetic'])
        self.assertEqual((await self.get('/v1/burnability/results/forest-demo-v1/grid/lfmc_pct')).status_code, 404)
        r = await self.get(f'{self.latest}/tiles/susceptibility_index/10/{tile_of(-119.75, 37.25, 10)[0]}/{tile_of(-119.75, 37.25, 10)[1]}.png')
        self.assertEqual((r.status_code, r.headers['x-synthetic']), (200, 'true'))
        rescan = await self.client.post('/v1/burnability/results/rescan')
        self.assertEqual(rescan.json()['results'][0]['result_id'], 'forest-demo-v1')


if __name__ == '__main__':
    unittest.main()
