"""Outline of older fire scars (GET .../scars): polygons of the cells of one fuel age, from the bundle's raster."""
import json
import os
import unittest

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform, transform_bounds

os.environ['GRID_AUTO_SYNC'] = '0'
try:
    from test_results import Base, write_bundle
except ImportError:                               # run as tests.test_scars
    from tests.test_results import Base, write_bundle

CRS = 'EPSG:32611'
ORIGIN, CELL, SHAPE = (330000, 3780000), 30, (160, 200)        # 4.8 km x 6 km
OLD = (slice(20, 100), slice(30, 130))                         # burned 7.9 years ago: 80 x 100 cells = 720 ha
YOUNG = (slice(110, 150), slice(140, 190))                     # burned 1.7 years ago
SPECK = (slice(5, 7), slice(180, 182))                         # four cells of the old age: noise, not a fire
ROAD = (slice(20, 100), 80)                                    # a road through the old scar: no fuel age there


def scar_bundle(root, name='scar', result_id='smm-20261130-nisar', valid_to='2026-12-21T07:00:00Z'):
    trans = from_origin(*ORIGIN, CELL, CELL)
    bbox = list(transform_bounds(CRS, 'EPSG:4326', ORIGIN[0], ORIGIN[1] - SHAPE[0] * CELL, ORIGIN[0] + SHAPE[1] * CELL, ORIGIN[1]))
    directory = write_bundle(root, name, result_id, CRS, trans, SHAPE, valid_to, bbox)
    years = np.full(SHAPE, 10.0, 'float32')
    years[OLD], years[YOUNG], years[SPECK] = 7.9, 1.7, 7.9
    years[ROAD] = -9999
    with rasterio.open(directory / 'years_since_fire.tif', 'w', driver='GTiff', height=SHAPE[0], width=SHAPE[1], count=1,
                       dtype='float32', crs=CRS, transform=trans, nodata=-9999, compress='deflate', tiled=True) as ds:
        ds.write(years, 1)
    manifest = json.loads((directory / 'result.json').read_text())
    manifest['layers']['years_since_fire'] = {'path': 'years_since_fire.tif', 'kind': 'continuous', 'units': 'years'}
    (directory / 'result.json').write_text(json.dumps(manifest))
    return directory


class ScarTests(Base):
    def bundles(self, root):
        super().bundles(root)
        scar_bundle(root)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.scar = '/v1/burnability/results/smm-20261130-nisar'

    async def test_outline_of_one_fuel_age(self):
        body = (await self.get(f'{self.scar}/scars?min_years=7.5&max_years=8.5')).json()
        self.assertEqual(body['type'], 'FeatureCollection')
        self.assertFalse(body['synthetic'])
        self.assertEqual(len(body['features']), 1)                 # the speck is dropped, the road does not split the scar
        self.assertAlmostEqual(body['area_ha'], 720, delta=1)
        ring = np.array(body['features'][0]['geometry']['coordinates'][0])
        self.assertEqual(len(body['features'][0]['geometry']['coordinates']), 1)       # no hole where the road is
        self.assertLessEqual(len(ring), 6)                         # a rectangle keeps its corners only
        x, y = transform('EPSG:4326', CRS, ring[:, 0].tolist(), ring[:, 1].tolist())
        self.assertAlmostEqual(min(x), ORIGIN[0] + 30 * CELL, delta=1)
        self.assertAlmostEqual(max(x), ORIGIN[0] + 130 * CELL, delta=1)
        self.assertAlmostEqual(max(y), ORIGIN[1] - 20 * CELL, delta=1)
        self.assertAlmostEqual(min(y), ORIGIN[1] - 100 * CELL, delta=1)

    async def test_another_age_and_none(self):
        young = (await self.get(f'{self.scar}/scars?min_years=0&max_years=7.5')).json()
        self.assertEqual(len(young['features']), 1)
        self.assertAlmostEqual(young['area_ha'], 40 * 50 * 0.09, delta=1)
        empty = (await self.get(f'{self.scar}/scars?min_years=3&max_years=5')).json()
        self.assertEqual(empty['features'], [])
        self.assertEqual(empty['area_ha'], 0)

    async def test_metadata_and_refusals(self):
        meta = (await self.get(self.scar)).json()
        self.assertTrue(meta['scars_url'].endswith('/scars'))
        self.assertNotIn('scars_url', (await self.get(self.ca)).json())            # no fuel-age layer in that bundle
        self.assertEqual((await self.get(f'{self.ca}/scars?min_years=7&max_years=8')).status_code, 404)
        self.assertEqual((await self.get('/v1/burnability/results/forest-demo-v1/scars?min_years=7&max_years=8')).status_code, 404)
        self.assertEqual((await self.get(f'{self.scar}/scars?min_years=9&max_years=8')).status_code, 422)
        self.assertEqual((await self.get(f'{self.scar}/scars?min_years=7')).status_code, 422)


if __name__ == '__main__':
    unittest.main()
