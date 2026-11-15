"""Deterministic synthetic forest. These are not satellite observations."""
import json
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from scipy.ndimage import gaussian_filter
from pyproj import Transformer, Geod

RESULT_ID = 'forest-demo-v1'
BBOX = [-120., 37., -119.5, 37.5]
CRS = 'EPSG:6933'
LAYERS = {
    'soil_moisture': ('Soil moisture', 'm3/m3', [0, .5], ['#fff7bc','#41b6c4','#253494']),
    'fuel_load_kg_m2': ('Dry live surface fuel load', 'kg/m2', [0, 3], ['#ffffcc','#78c679','#006837']),
    'lfmc_pct': ('Live fuel moisture', '% dry mass', [30, 180], ['#d73027','#fee08b','#1a9850']),
    'air_temperature_2m_c': ('Air temperature (synthetic)', 'degC', [15, 40], ['#4575b4','#fee090','#d73027']),
    'energy_proxy_mj_m2': ('Live-fuel energy proxy', 'MJ/m2', [0, 54], ['#ffffb2','#fe9929','#cc4c02']),
    'susceptibility_index': ('Fuel susceptibility (not probability)', '1', [0, 1], ['#fff7bc','#fec44f','#d95f0e']),
}

def generate(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # Always regenerate on startup; files are small, deterministic, never remote data.
    left, bottom, right, top = transform_bounds('EPSG:4326', CRS, *BBOX)
    left, bottom = np.floor(np.array([left,bottom])/200)*200
    right, top = np.ceil(np.array([right,top])/200)*200
    width, height = int((right-left)/200), int((top-bottom)/200)
    transform = from_origin(left, top, 200, 200)
    yy, xx = np.mgrid[0:height,0:width]
    xx, yy = xx/width, yy/height
    noise = gaussian_filter(np.random.default_rng(42).normal(size=(height,width)), 10)
    noise = (noise-noise.min())/(noise.max()-noise.min())
    forest = np.clip(.65 + .25*np.sin(8*xx)*np.cos(7*yy)+.15*noise, 0, 1)
    fuel = .25+2.5*forest
    sm = np.clip(.08+.22*yy+.08*noise, .03, .5)
    lfmc = np.clip(35+300*sm+20*noise, 30, 180)
    energy = fuel*np.maximum(0,18-2.44*lfmc/100)
    susceptibility = np.exp(-lfmc/100)*(1-sm)*(1-np.exp(-energy/10))
    # A curved lake provides genuine nodata, distinct from a valid zero.
    valid = ((xx-.34)**2/.025**2+(yy-.55)**2/.18**2)>1
    fields = dict(soil_moisture=sm,fuel_load_kg_m2=fuel,lfmc_pct=lfmc,
                  air_temperature_2m_c=36-12*yy+2*noise,
                  energy_proxy_mj_m2=energy,susceptibility_index=susceptibility)
    profile = dict(driver='GTiff',width=width,height=height,count=1,dtype='float32',
                   crs=CRS,transform=transform,nodata=-9999,compress='deflate',tiled=True,
                   blockxsize=128,blockysize=128)
    for name, values in fields.items():
        with rasterio.open(directory/f'{name}.tif','w',**profile) as ds:
            ds.write(np.where(valid,values,-9999).astype('float32'),1)
            ds.update_tags(synthetic='true',result_id=RESULT_ID,meaning=LAYERS[name][0])
            ds.build_overviews([2,4,8,16],Resampling.average)
            ds.update_tags(ns='rio_overview',resampling='average')
    # Sparse illustrative arrows: fictional eastward wind, not output of ML or forecast.
    project = Transformer.from_crs(CRS,'EPSG:4326',always_xy=True)
    geod = Geod(ellps='WGS84')
    features=[]
    for row in range(8,height-8,12):
        for col in range(8,width-8,12):
            if not valid[row,col]: continue
            x,y=rasterio.transform.xy(transform,row,col)
            lon,lat=project.transform(x,y)
            bearing=float(90+20*np.sin(row/30))
            end_lon,end_lat,_=geod.fwd(lon,lat,bearing,600)
            features.append(dict(type='Feature',id=len(features),geometry=dict(type='LineString',
                coordinates=[[lon,lat],[end_lon,end_lat]]),properties=dict(
                bearing_deg=bearing,length_m=600,synthetic=True,wind_assumption='fictional_westerly',
                meaning='illustrative_arrow_not_fire_forecast')))
    (directory/'vectors.geojson').write_text(json.dumps(dict(type='FeatureCollection',features=features)))
    return dict(profile=profile,valid_fraction=float(valid.mean()),vector_count=len(features))
