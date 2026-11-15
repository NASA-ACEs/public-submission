import asyncio
import io
import math
import tempfile
import unittest
from pathlib import Path
import httpx
from PIL import Image
import app.main as api

class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();api.ROOT=Path(self.tmp.name)
        # Periodic timer also supports sandbox loops without cross-thread wakeups.
        async def heartbeat():
            while True: await asyncio.sleep(.01)
        self.pulse=asyncio.create_task(heartbeat())
        self.lifespan=api.app.router.lifespan_context(api.app)
        await self.lifespan.__aenter__()
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test')
        self.base=api.BASE
    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None,None,None)
        self.pulse.cancel()
        self.tmp.cleanup()
    async def test_metadata_and_all_downloads(self):
        meta=(await self.client.get(self.base)).json()
        self.assertTrue(meta['synthetic']);self.assertEqual(len(meta['layers']),6)
        for layer in meta['layers']:
            r=await self.client.get(layer['download_url']);self.assertEqual(r.status_code,200)
            self.assertEqual(r.headers['x-synthetic'],'true')
            self.assertTrue(r.content.startswith((b'II',b'MM')))
    async def test_zoom_tiles_cache_and_transparency(self):
        for z in (8,10,14,18):
            x=int((-119.75+180)/360*2**z)
            y=int((1-math.asinh(math.tan(math.radians(37.25)))/math.pi)/2*2**z)
            path=f'{self.base}/tiles/susceptibility_index/{z}/{x}/{y}.png'
            r=await self.client.get(path);self.assertEqual(r.status_code,200)
            im=Image.open(io.BytesIO(r.content));self.assertEqual(im.size,(256,256))
            self.assertGreater(im.getextrema()[3][1],0)
            self.assertEqual((await self.client.get(path,headers={'If-None-Match':r.headers['etag']})).status_code,304)
        blank=await self.client.get(f'{self.base}/tiles/soil_moisture/8/0/0.png')
        self.assertEqual(Image.open(io.BytesIO(blank.content)).getextrema()[3],(0,0))
    async def test_previews_and_validation(self):
        for width in (128,512,1024):
            r=await self.client.get(f'{self.base}/preview/fuel_load_kg_m2.png?width={width}')
            self.assertEqual(Image.open(io.BytesIO(r.content)).width,width)
        self.assertEqual((await self.client.get(f'{self.base}/preview/soil_moisture.png?width=10000')).status_code,422)
        self.assertEqual((await self.client.get(f'{self.base}/tiles/soil_moisture/19/0/0.png')).status_code,422)
        self.assertEqual((await self.client.get(f'{self.base}/assets/missing')).status_code,404)
    async def test_point_and_vectors(self):
        point=(await self.client.get(f'{self.base}/point?lon=-119.75&lat=37.25')).json()
        self.assertTrue(point['valid']);self.assertIsNotNone(point['sampled_cell_center'])
        outside=(await self.client.get(f'{self.base}/point?lon=0&lat=0')).json()
        self.assertFalse(outside['valid']);self.assertTrue(all(v is None for v in outside['values'].values()))
        low=(await self.client.get(f'{self.base}/vectors/illustrative_pathways?zoom=6')).json()
        high=(await self.client.get(f'{self.base}/vectors/illustrative_pathways?zoom=12')).json()
        self.assertLess(len(low['features']),len(high['features']))
        self.assertEqual((await self.client.get(f'{self.base}/vectors/illustrative_pathways?bbox=0,0,1,1')).json()['features'],[])
        self.assertEqual((await self.client.get(f'{self.base}/vectors/illustrative_pathways?bbox=bad')).status_code,422)
    async def test_numeric_grid_preserves_nodata(self):
        grid=(await self.client.get(f'{self.base}/grid/susceptibility_index')).json()
        self.assertEqual(grid['crs'],'EPSG:3857')
        self.assertEqual(len(grid['values']),grid['width']*grid['height'])
        self.assertIn(None,grid['values'])
        self.assertTrue(all(0<=v<=1 for v in grid['values'] if v is not None))
        self.assertTrue(grid['synthetic'])
        self.assertEqual(grid['transform'][1],0)
        self.assertLess(grid['transform'][4],0)

    async def test_map_assessment_without_hotspots(self):
        features=[{'type':'Feature','id':'line-A','geometry':{'type':'LineString',
            'coordinates':[[-119.99,37.25],[-119.51,37.25]]}}]
        body={'features':features}
        r=await self.client.post(f'{self.base}/assess-lines',json=body)
        self.assertEqual(r.status_code,200)
        result=r.json()
        self.assertFalse(result['distance_to_hotspot_used'])
        summary=result['line_summaries'][0]
        self.assertGreater(summary['covered_length_m'],0)
        self.assertGreater(summary['unknown_length_m'],0) # crosses synthetic lake
        self.assertAlmostEqual(summary['total_length_m'],sum(summary[k] for k in
            ['higher_length_m','lower_length_m','unknown_length_m']),places=5)
        low=(await self.client.post(f'{self.base}/assess-lines',json=dict(body,threshold=0))).json()
        self.assertAlmostEqual(low['line_summaries'][0]['higher_fraction_of_covered'],1)
        high=(await self.client.post(f'{self.base}/assess-lines',json=dict(body,threshold=1))).json()
        self.assertAlmostEqual(high['line_summaries'][0]['higher_fraction_of_covered'],0)
        outside=(await self.client.post(f'{self.base}/assess-lines',json=dict(body,region_bbox_wgs84=[0,0,1,1]))).json()
        self.assertEqual(outside['line_summaries'][0]['coverage_fraction'],0)
        self.assertIsNone(outside['line_summaries'][0]['mean_score_on_covered'])
        self.assertEqual((await self.client.post(f'{self.base}/assess-lines',json={'features':[]})).status_code,422)

    async def test_jobs_and_cors(self):
        self.assertEqual((await self.client.post('/v1/burnability/jobs',json={'mode':'real'})).status_code,422)
        self.assertEqual((await self.client.post('/v1/burnability/jobs',json={})).json()['status'],'succeeded')
        r=await self.client.get('/health',headers={'Origin':'http://localhost:5173'})
        self.assertEqual(r.headers['access-control-allow-origin'],'http://localhost:5173')

if __name__=='__main__':unittest.main()
