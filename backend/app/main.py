import hashlib
import io
import json
import math
import os
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import numpy as np
import rasterio
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from PIL import Image
from pydantic import BaseModel, ConfigDict
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import reproject, transform
from .data import BBOX, LAYERS, RESULT_ID, generate
from .archive import Fetcher
from .results import (GRID_DEFAULT_CELLS, GRID_MAX_CELLS, SPREAD_MAX_HOURS, SPREAD_MAX_STEPS, SPREAD_MAX_WIND,
    VECTOR_IDS, VIEW_VECTORS_DEFAULT, VIEW_VECTORS_MAX, Registry, SpreadError)
from .grid.api import router as grid_router, start_background_sync
from .grid.service import RiskService
from .grid.store import GridStore
from .grid.sync import sync as grid_sync

ROOT = Path(os.getenv('DATA_DIR', Path(__file__).resolve().parents[1]/'data')).resolve()
BASE = '/v1/burnability/results/' + RESULT_ID

@asynccontextmanager
async def lifespan(app):
    app.state.info = generate(ROOT)
    image_bytes.cache_clear()
    app.state.vectors = json.loads((ROOT/'vectors.geojson').read_text())
    # Real result bundles from machine-learning (RESULTS_DIR unset: only the synthetic demo is served).
    app.state.results = Registry(os.getenv('RESULTS_DIR','').strip() or None,
        os.getenv('RESULTS_CACHE_DIR','').strip() or ROOT/'results-cache')
    app.state.results.scan(force=True)
    # RESULTS_ARCHIVE_URL: bundles downloaded into RESULTS_DIR in the background (hosts with no way to upload files).
    app.state.archive = Fetcher(app.state.results)
    app.state.archive.start()
    # FUEL_RISK_RESULT=<result_id|latest>: /v1/grid samples that real result at fires and threatened lines.
    # FUEL_RISK_DRIVES=spread,tiers additionally lets it scale the spread envelope / replace the demo score in tiers;
    # simulate adds the fire-growth simulation of the result (isochrones for the live wind) next to each envelope.
    fuel_id=os.getenv('FUEL_RISK_RESULT','').strip()
    fuel_source=(lambda: app.state.results.latest() if fuel_id=='latest' else app.state.results.get(fuel_id)) if fuel_id else None
    drives={d.strip() for d in os.getenv('FUEL_RISK_DRIVES','').split(',')}&{'spread','tiers','simulate'}
    app.state.grid = RiskService(GridStore(), ROOT/'susceptibility_index.tif', tuple(BBOX), fuel_source, drives)
    app.state.grid_syncing = False
    if os.getenv('GRID_AUTO_SYNC', '1') != '0':
        start_background_sync(app, grid_sync)
    yield
    app.state.grid.client.close()

app = FastAPI(title='Burnability prototype API', version='0.2.0', lifespan=lifespan,
    description='Synthetic forest demo plus live transmission-grid wildfire risk (/v1/grid).')
app.include_router(grid_router)
# Deployed frontend plus any local dev port; extend with CORS_ORIGINS (comma-separated).
DEFAULT_ORIGINS = 'https://frontend-production-873d5e10.up.railway.app,http://localhost:5173,http://127.0.0.1:5173'
app.add_middleware(CORSMiddleware,
    allow_origins=sorted({o.strip() for o in f"{DEFAULT_ORIGINS},{os.getenv('CORS_ORIGINS', '')}".split(',') if o.strip()}),
    allow_origin_regex=r'http://(localhost|127\.0\.0\.1)(:\d+)?',
    allow_methods=['GET','POST','OPTIONS'],allow_headers=['Content-Type'],
    expose_headers=['ETag','X-Synthetic','Content-Disposition'])

def check(result_id, layer=None):
    if result_id != RESULT_ID: raise HTTPException(404,'Unknown result')
    if layer is not None and layer not in LAYERS: raise HTTPException(404,'Unknown layer')

def resolve(result_id, layer=None):
    """None for the synthetic demo, else the real bundle. ``latest`` = newest real result, or the demo if none."""
    registry=app.state.results
    real=registry.latest() if result_id=='latest' else registry.get(result_id) if result_id!=RESULT_ID else None
    if real is None:
        check(RESULT_ID if result_id=='latest' else result_id,layer)
    elif layer is not None and layer not in real.layers: raise HTTPException(404,'Unknown layer')
    return real

def parse_bbox(bbox):
    if not bbox: return None
    try:
        bounds=[float(v) for v in bbox.split(',')]
        if len(bounds)!=4 or not all(math.isfinite(v) for v in bounds):raise ValueError()
        if not (-180<=bounds[0]<bounds[2]<=180 and -85.1<=bounds[1]<bounds[3]<=85.1):raise ValueError()
    except ValueError: raise HTTPException(422,'bbox must be west,south,east,north; split antimeridian requests')
    return bounds

def extent(coordinates):
    """(west, south, east, north) of any GeoJSON coordinate array."""
    if isinstance(coordinates[0],(int,float)): return coordinates[0],coordinates[1],coordinates[0],coordinates[1]
    boxes=[extent(c) for c in coordinates]
    return min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)

def listing():
    registry=app.state.results
    out={'results':[{'result_id':RESULT_ID,'synthetic':True,'metadata_url':BASE}]}
    if registry.enabled:
        real=registry.all()
        out['results']+=[r.summary('/v1/burnability/results/'+r.id) for r in real]
        out['latest']=real[0].id if real else RESULT_ID
        out['rejected']=[dict(directory=k,reason=v) for k,v in sorted(registry.rejected.items())]
    return out

def colors(values, layer):
    _,_,(lo,hi),stops = LAYERS[layer]
    valid = np.isfinite(values) & (values != -9999)
    t = np.clip((np.where(valid,values,lo)-lo)/(hi-lo),0,1)
    rgb = np.array([[int(s[i:i+2],16) for i in (1,3,5)] for s in stops])
    rgba = np.zeros((*values.shape,4),dtype='uint8')
    for channel in range(3):
        rgba[:,:,channel] = np.interp(t,[0,.5,1],rgb[:,channel]).astype('uint8')
    rgba[:,:,3] = np.where(valid,210,0)
    stream=io.BytesIO(); Image.fromarray(rgba).save(stream,format='PNG')
    return stream.getvalue()

@lru_cache(maxsize=128)
def image_bytes(layer,bounds,width,height,crs):
    dst=np.full((height,width),-9999,dtype='float32')
    with rasterio.open(ROOT/f'{layer}.tif') as ds:
        reproject(rasterio.band(ds,1),dst,src_transform=ds.transform,src_crs=ds.crs,
            src_nodata=-9999,dst_transform=from_bounds(*bounds,width,height),
            dst_crs=crs,dst_nodata=-9999,resampling=Resampling.average if width==256 else Resampling.bilinear)
    return colors(dst,layer)

def png_response(content,request,synthetic=True):
    etag='"'+hashlib.sha256(content).hexdigest()+'"'
    headers={'ETag':etag,'Cache-Control':'public, max-age=3600','X-Synthetic':'true' if synthetic else 'false'}
    if request.headers.get('if-none-match') == etag: return Response(status_code=304,headers=headers)
    return Response(content,media_type='image/png',headers=headers)

@app.get('/', include_in_schema=False)
def root(): return RedirectResponse('/docs')

@app.get('/health')
def health():
    out={'status':'ok','synthetic':True,'result_id':RESULT_ID}
    if app.state.results.enabled:
        latest=app.state.results.latest()
        out.update(real_results=len(app.state.results.results),latest=latest.id if latest else RESULT_ID)
    if app.state.archive.enabled:
        out.update(results_archive=app.state.archive.status,results_archive_detail=app.state.archive.detail)
    return out

@app.get('/v1/burnability/results')
def results(): return listing()

@app.post('/v1/burnability/results/rescan')
def rescan():
    """Re-read RESULTS_DIR now (it is also re-read every RESULTS_RESCAN_S seconds, default 15)."""
    app.state.results.scan(force=True)
    return listing()

@app.get('/v1/burnability/results/{result_id}')
def metadata(result_id,request:Request):
    real=resolve(result_id)
    origin=str(request.base_url).rstrip('/')
    if real: return real.metadata(origin)
    layers=[]
    for key,(title,units,limits,palette) in LAYERS.items():
        path=origin+BASE
        layers.append(dict(id=key,title=title,units=units,kind='index' if key=='susceptibility_index' else 'continuous',
            range=limits,nodata=-9999,synthetic=True,
            tiles_url=path+'/tiles/'+key+'/{z}/{x}/{y}.png',
            arcgis_tiles_url=path+'/tiles/'+key+'/{level}/{col}/{row}.png',
            tilejson_url=path+'/tilejson/'+key,preview_url=path+'/preview/'+key+'.png',
            download_url=path+'/assets/'+key,
            legend=dict(stops=[[limits[0],palette[0]],[sum(limits)/2,palette[1]],[limits[1],palette[2]]])))
    return dict(schema_version='0.1',result_id=RESULT_ID,synthetic=True,research_only=True,
        as_of='2026-10-01T00:00:00Z',mode='synthetic_demo',bbox_wgs84=BBOX,
        grid=dict(crs='EPSG:6933',pixel_spacing_m=200),
        tile_scheme='xyz',tile_size=256,minzoom=0,maxzoom=18,max_native_zoom=10,
        layers=layers,valid_fraction=app.state.info['valid_fraction'],
        vector_layers=[dict(id='illustrative_pathways',features_url=origin+BASE+'/vectors/illustrative_pathways',
            download_url=origin+BASE+'/vectors/illustrative_pathways',feature_count=app.state.info['vector_count'],
            meaning='fictional wind arrows; not ML output or fire forecast',synthetic=True)],
        provenance_url=origin+BASE+'/provenance',
        line_assessment_url=origin+BASE+'/assess-lines',
        notices=['SYNTHETIC DEMO — no NASA or weather observations',
                 'Temperature is illustrative and does not enter the legacy susceptibility score',
                 'Arrows use fictional wind, not a validated propagation algorithm',
                 '200m source detail; high zoom only enlarges existing information'])

@app.get('/v1/burnability/results/{result_id}/tilejson/{layer}')
def tilejson(result_id,layer,request:Request):
    real=resolve(result_id,layer)
    if real:
        b=real.bbox
        return dict(tilejson='3.0.0',name=real.layers[layer]['title'],scheme='xyz',minzoom=0,maxzoom=18,bounds=b,
            center=[(b[0]+b[2])/2,(b[1]+b[3])/2,8],
            tiles=[f"{str(request.base_url).rstrip('/')}/v1/burnability/results/{real.id}/tiles/{layer}/{{z}}/{{x}}/{{y}}.png"],
            attribution=real.manifest.get('target') or 'NASA-ACEs model output')
    return dict(tilejson='3.0.0',name=LAYERS[layer][0],scheme='xyz',minzoom=0,maxzoom=18,
        bounds=BBOX,center=[-119.75,37.25,10],tiles=[str(request.base_url).rstrip('/')+BASE+'/tiles/'+layer+'/{z}/{x}/{y}.png'],
        attribution='Synthetic forest demo; not a fire forecast')

@app.get('/v1/burnability/results/{result_id}/tiles/{layer}/{z}/{x}/{y}.png')
def tile(result_id,layer,z:int,x:int,y:int,request:Request):
    real=resolve(result_id,layer)
    if not 0<=z<=18 or not 0<=x<2**z or not 0<=y<2**z: raise HTTPException(422,'Invalid XYZ tile')
    if real: return png_response(real.tile(layer,z,x,y),request,False)
    half=20037508.342789244; span=2*half/(2**z)
    bounds=(-half+x*span,half-(y+1)*span,-half+(x+1)*span,half-y*span)
    return png_response(image_bytes(layer,bounds,256,256,'EPSG:3857'),request)

@app.get('/v1/burnability/results/{result_id}/preview/{layer}.png')
def preview(result_id,layer,request:Request,width:int=Query(512,ge=64,le=2048)):
    real=resolve(result_id,layer)
    if real: return png_response(real.preview(layer,width),request,False)
    with rasterio.open(ROOT/f'{layer}.tif') as ds:
        bounds=tuple(ds.bounds);height=max(1,round(width*ds.height/ds.width))
        crs=str(ds.crs)
    return png_response(image_bytes.__wrapped__(layer,bounds,width,height,crs),request)

@app.get('/v1/burnability/results/{result_id}/assets/{layer}')
def asset(result_id,layer):
    real=resolve(result_id,layer)
    if real: return FileResponse(real.layers[layer]['path'],media_type='image/tiff',
        filename=f'{real.id}_{layer}.tif',headers={'X-Synthetic':'false'})
    return FileResponse(ROOT/f'{layer}.tif',media_type='image/tiff',filename=f'{RESULT_ID}_{layer}.tif',headers={'X-Synthetic':'true'})

@app.get('/v1/burnability/results/{result_id}/vectors')
def view_vectors(result_id,bbox:str|None=None,n:int=Query(VIEW_VECTORS_DEFAULT,ge=1,le=VIEW_VECTORS_MAX),
                 hours:float=Query(1,gt=0,le=SPREAD_MAX_HOURS)):
    """About ``n`` head-fire arrows inside ``bbox`` (west,south,east,north; the whole result without it), GeoJSON.

    An arrow starts at a cell of the result and is the distance the head fire would run in ``hours`` (at most 5 km),
    from ``rate_of_spread`` and ``spread_direction`` of that cell. Empty outside the result. 404 for the synthetic
    demo and for results without those layers: use ``vectors/{vector_id}`` there."""
    real=resolve(result_id)
    if real is None: raise HTTPException(404,'Arrows for a map view need a model result; the synthetic demo has vectors/illustrative_pathways')
    try:
        return real.view_vectors(parse_bbox(bbox),n,int(hours) if hours.is_integer() else hours)
    except SpreadError as error:
        raise HTTPException(error.status,str(error))

@app.get('/v1/burnability/results/{result_id}/vectors/{vector_id}')
def vectors(result_id,vector_id,bbox:str|None=None,zoom:float=Query(10,ge=0,le=18),limit:int=Query(1000,ge=1,le=5000),offset:int=Query(0,ge=0)):
    real=resolve(result_id)
    if vector_id not in (VECTOR_IDS if real else VECTOR_IDS[1:]): raise HTTPException(404,'Unknown vector layer')
    bounds=parse_bbox(bbox)
    stride=8 if zoom<7.2 else 4 if zoom<8.8 else 2 if zoom<10 else 1
    features=[]
    for feature in (real.vectors() if real else app.state.vectors['features'])[::stride]:
        if bounds:
            west,south,east,north=extent(feature['geometry']['coordinates'])
            if east<bounds[0] or west>bounds[2] or north<bounds[1] or south>bounds[3]:continue
        features.append(feature)
    return dict(type='FeatureCollection',features=features[offset:offset+limit],
        synthetic=real is None,zoom=zoom,total=len(features),next_offset=offset+limit if offset+limit<len(features) else None)

@app.get('/v1/burnability/results/{result_id}/point')
def point(result_id,lon:float=Query(ge=-180,le=180),lat:float=Query(ge=-90,le=90)):
    real=resolve(result_id)
    if real: return real.point(lon,lat)
    inside=BBOX[0]<=lon<=BBOX[2] and BBOX[1]<=lat<=BBOX[3]
    values={key:None for key in LAYERS}; center=None
    if inside:
        for key in LAYERS:
            with rasterio.open(ROOT/f'{key}.tif') as ds:
                x,y=transform('EPSG:4326',ds.crs,[lon],[lat]); row,col=ds.index(x[0],y[0])
                if not(0<=row<ds.height and 0<=col<ds.width):continue
                value=float(next(ds.sample([(x[0],y[0])]))[0])
                values[key]=None if value==-9999 or not math.isfinite(value) else value
                cx,cy=rasterio.transform.xy(ds.transform,row,col)
                cl,ct=transform(ds.crs,'EPSG:4326',[cx],[cy]);center={'lon':cl[0],'lat':ct[0]}
    valid=any(v is not None for v in values.values())
    return dict(result_id=result_id,synthetic=True,valid=valid,values=values,sampled_cell_center=center,
        reason=None if valid else 'NO_VALID_DATA' if inside else 'OUTSIDE_AOI',sample_method='nearest_cell')

@app.get('/v1/burnability/results/{result_id}/spread')
def spread(result_id,lon:float=Query(ge=-180,le=180),lat:float=Query(ge=-90,le=90),hours:str='1,3,6',
           wind_speed:float|None=Query(None,ge=0,le=SPREAD_MAX_WIND),wind_from:float|None=Query(None,ge=0,le=360),
           fuel_age:bool=True,fuel_state:bool=True):
    """Where a fire started at the point would be after ``hours`` (isochrones, GeoJSON, WGS84).

    Surface-fire potential for one wind and the fuels of the map day: no suppression, spotting or crown fire.
    Without ``wind_speed`` (m/s at 10 m) and ``wind_from`` (degrees it blows from) the wind of the map day is used.
    ``fuel_age=false`` leaves out the slowing inside recent fire scars. ``fuel_state=false`` grows the fire on the
    fuel map alone, without the NISAR GCOV fuel state, where the result has both (``capabilities.spread_fuel_state``).
    A sync endpoint: it runs in a worker thread."""
    real=resolve(result_id)
    if real is None: raise HTTPException(404,'Fire growth needs a model result with component layers; the synthetic demo has none')
    try:
        steps=sorted({float(v) for v in hours.split(',') if v.strip()})
    except ValueError: raise HTTPException(422,'hours must be numbers separated by commas, e.g. 1,3,6')
    if not 1<=len(steps)<=SPREAD_MAX_STEPS or not all(math.isfinite(h) and 0<h<=SPREAD_MAX_HOURS for h in steps):
        raise HTTPException(422,f'hours: 1 to {SPREAD_MAX_STEPS} values, each above 0 and at most {SPREAD_MAX_HOURS}')
    if (wind_speed is None)!=(wind_from is None): raise HTTPException(422,'Pass wind_speed and wind_from together, or neither')
    try:
        return real.spread(lon,lat,steps,None if wind_speed is None else (wind_speed,wind_from%360),fuel_age,fuel_state)
    except SpreadError as error:
        raise HTTPException(error.status,str(error))

@app.get('/v1/burnability/results/{result_id}/scars')
def scars(result_id,min_years:float=Query(ge=0,le=100),max_years:float=Query(ge=0,le=100)):
    """Outline of the land that last burned between ``min_years`` and ``max_years`` before the map day (GeoJSON, WGS84).

    From the result's fuel-age layer, which machine-learning makes from mapped fire perimeters; 404 for the synthetic
    demo and for results without that layer. A sync endpoint: it runs in a worker thread."""
    real=resolve(result_id)
    if real is None: raise HTTPException(404,'Fire scars need a model result with a fuel-age layer; the synthetic demo has none')
    if min_years>max_years: raise HTTPException(422,'min_years must not exceed max_years')
    try:
        return real.scars(min_years,max_years)
    except SpreadError as error:
        raise HTTPException(error.status,str(error))

@app.get('/v1/burnability/results/{result_id}/provenance')
def provenance(result_id):
    real=resolve(result_id)
    if real: return real.provenance()
    return dict(result_id=RESULT_ID,synthetic=True,seed=42,source='Procedural smooth forest-like fields',
        real_satellite_data=False,real_weather_data=False,trained_model=False,
        susceptibility='exp(-LFMC/100)*(1-soil_moisture)*(1-exp(-energy/10))',
        temperature_used_in_susceptibility=False,vectors='illustrative eastward wind arrows, no fire simulation',
        downloads='Tiled GeoTIFF with average overviews; not certified COG')

class DemoJob(BaseModel):
    model_config=ConfigDict(extra='forbid')
    mode:str='synthetic_demo'

@app.post('/v1/burnability/jobs',status_code=202)
def create_job(body:DemoJob):
    if body.mode!='synthetic_demo':raise HTTPException(422,'Only synthetic_demo implemented; real acquisition/ML jobs unavailable')
    return dict(job_id='demo-job',status='succeeded',result_url=BASE,status_url='/v1/burnability/jobs/demo-job')

@app.get('/v1/burnability/jobs/{job_id}')
def job(job_id):
    if job_id!='demo-job':raise HTTPException(404,'Unknown job')
    return dict(job_id=job_id,status='succeeded',result_url=BASE,synthetic=True)

@app.get('/v1/burnability/results/{result_id}/grid/{layer}')
def numeric_grid(result_id,layer,bbox:str|None=None,max_cells:int=Query(GRID_DEFAULT_CELLS,ge=1,le=GRID_MAX_CELLS)):
    """Numeric display grid: clients must not derive values from PNG colours.

    Real results: any layer, optional WGS84 ``bbox`` window, at most ``max_cells`` values (decimated beyond that).
    The synthetic demo publishes ``susceptibility_index`` whole, as before."""
    real=resolve(result_id,layer)
    if real:
        try: return real.grid(layer,parse_bbox(bbox),max_cells)
        except ValueError as error: raise HTTPException(422,str(error))
    if layer!='susceptibility_index': raise HTTPException(404,'The demo publishes a numeric grid for susceptibility_index only')
    with rasterio.open(ROOT/'susceptibility_index.tif') as ds:
        from rasterio.warp import transform_bounds
        bounds=transform_bounds(ds.crs,'EPSG:3857',*ds.bounds)
        affine=from_bounds(*bounds,ds.width,ds.height)
        values=np.full(ds.shape,-9999,dtype='float32')
        reproject(rasterio.band(ds,1),values,src_transform=ds.transform,src_crs=ds.crs,
            src_nodata=-9999,dst_transform=affine,dst_crs='EPSG:3857',dst_nodata=-9999,
            resampling=Resampling.nearest)
        valid=np.isfinite(values)&(values!=-9999)
        return dict(result_id=RESULT_ID,synthetic=True,crs='EPSG:3857',
            width=ds.width,height=ds.height,transform=list(affine)[:6],
            values=[round(float(v),5) if ok else None for v,ok in zip(values.ravel(),valid.ravel())],
            threshold=.25,threshold_meaning='prototype screening policy, not probability',
            as_of='2026-10-01T00:00:00Z',native_support_m=200,
            meaning='synthetic fuel susceptibility only; not power-line damage risk')

class LineAssessmentRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    features:list[dict]
    # Defaults: the result's bounds; 0.25 for the demo, the upper risk classes (class_breaks) for a real result.
    region_bbox_wgs84:tuple[float,float,float,float]|None=None
    threshold:float|None=None
    layer:str|None=None

@app.post('/v1/burnability/results/{result_id}/assess-lines')
def assess_result_lines(result_id, body:LineAssessmentRequest):
    from .assessment import assess_lines
    real=resolve(result_id)
    layer=body.layer or (real.assessment_layer if real else 'susceptibility_index')
    if real and (layer not in real.layers or real.layers[layer]['kind'] not in ('index','probability')):
        raise HTTPException(422,'layer must be an index or probability layer of this result')
    if not real and layer!='susceptibility_index': raise HTTPException(422,'The demo assesses susceptibility_index only')
    threshold=body.threshold if body.threshold is not None else real.threshold if real and real.layers[layer]['kind']=='index' else .25 if not real else None
    if threshold is None: raise HTTPException(422,'threshold is required for a probability layer')
    if not 1<=len(body.features)<=200 or not 0<=threshold<=1:
        raise HTTPException(422,'Submit 1..200 features and threshold in [0,1]')
    bbox=body.region_bbox_wgs84 or tuple(real.bbox if real else BBOX)
    try:
        result=assess_lines(real.layers[layer]['path'] if real else ROOT/'susceptibility_index.tif',body.features,bbox,threshold)
    except (ValueError,TypeError,KeyError,OverflowError) as error:
        raise HTTPException(422,str(error))
    if real:
        m=real.manifest
        return dict(result,result_id=real.id,synthetic=False,as_of=m.get('as_of'),valid_from=m.get('valid_from'),
            valid_to=m.get('valid_to'),layer_id=layer,research_only=bool(m.get('research_only',True)),target=m.get('target'),
            threshold_meaning='rank cut at the lower bound of the upper risk classes (class_breaks); a display policy' if body.threshold is None
                else 'caller-supplied cut on the layer value')
    return dict(result, result_id=RESULT_ID,synthetic=True,as_of='2026-10-01T00:00:00Z',
        layer_id='susceptibility_index',research_only=True,
        threshold_meaning='exploratory policy, not a probability cutoff')
