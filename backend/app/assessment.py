"""Assess asset spans against a result raster, without a hotspot-distance rule."""
import math
import numpy as np
import rasterio
import rasterio.windows
from pyproj import Geod, Transformer


def assess_lines(path, features, bbox, threshold):
    if not (-180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90):
        raise ValueError('Invalid WGS84 region bbox; split antimeridian requests')
    geod = Geod(ellps='WGS84')
    segments = []
    totals = []
    vertices = 0
    for number, feature in enumerate(features):
        if not isinstance(feature, dict) or feature.get('type') != 'Feature':
            raise ValueError('Expected GeoJSON Features')
        geom = feature.get('geometry') or {}
        kind = geom.get('type')
        if kind not in ('LineString', 'MultiLineString'):
            raise ValueError('Only LineString and MultiLineString geometries are supported')
        paths = [geom.get('coordinates')] if kind == 'LineString' else geom.get('coordinates')
        if not isinstance(paths, list) or not paths:
            raise ValueError('Empty line geometry')
        total = 0.
        for points in paths:
            if not isinstance(points, list) or len(points) < 2:
                raise ValueError('Each path needs at least two positions')
            vertices += len(points)
            if vertices > 20000:
                raise ValueError('Vertex budget exceeded; submit smaller viewport batches')
            for pos in points:
                if not isinstance(pos, list) or len(pos) != 2 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in pos):
                    raise ValueError('Positions must be finite [longitude, latitude]')
                if not(-180 <= pos[0] <= 180 and -90 <= pos[1] <= 90):
                    raise ValueError('Position outside WGS84 range')
            for start, end in zip(points, points[1:]):
                _,_,length = geod.inv(*start,*end)
                count = max(1, math.ceil(length/100))
                if len(segments)+count > 20000:
                    raise ValueError('Sample budget exceeded; submit smaller viewport batches')
                if length == 0: continue
                chain = [start] + [list(p) for p in geod.npts(*start,*end,count-1)] if count>1 else [start]
                chain.append(end)
                for p,q in zip(chain,chain[1:]):
                    az,_,dist = geod.inv(*p,*q)
                    lon,lat,_ = geod.fwd(*p,az,dist/2)
                    segments.append((number,p,q,lon,lat,dist))
                    total += dist
        totals.append(total)
    statistics = [dict(total_length_m=v,covered_length_m=0.,higher_length_m=0.,lower_length_m=0.,unknown_length_m=0.,weighted_sum=0.,max_score=None) for v in totals]
    output = []
    with rasterio.open(path) as ds:
        transformer=Transformer.from_crs('EPSG:4326',ds.crs,always_xy=True)
        # Raster cell of every in-region sample first, so only the window they span is read.
        cells=[None]*len(segments)
        for i,(number,p,q,lon,lat,dist) in enumerate(segments):
            if bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]:
                x,y=transformer.transform(lon,lat)
                if math.isfinite(x) and math.isfinite(y):
                    row,col=ds.index(x,y)
                    if 0<=row<ds.height and 0<=col<ds.width: cells[i]=(row,col)
        hit=[c for c in cells if c is not None]
        row0=min((c[0] for c in hit),default=0);col0=min((c[1] for c in hit),default=0)
        values=ds.read(1,masked=True,window=rasterio.windows.Window(col0,row0,
            max(c[1] for c in hit)-col0+1,max(c[0] for c in hit)-row0+1)) if hit else None
        for i,(number,p,q,lon,lat,dist) in enumerate(segments):
            value=None
            reason=None
            if not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
                reason='OUTSIDE_REQUESTED_REGION'
            else:
                if cells[i] is not None:
                    raw=values[cells[i][0]-row0,cells[i][1]-col0]
                    if not np.ma.is_masked(raw) and np.isfinite(raw): value=float(raw)
                if value is None: reason='NO_MAP_COVERAGE_OR_NODATA'
            category='unknown' if value is None else 'higher' if value>=threshold else 'lower'
            stat=statistics[number]
            stat[category+'_length_m']+=dist
            if value is not None:
                stat['covered_length_m']+=dist
                stat['weighted_sum']+=value*dist
                stat['max_score']=value if stat['max_score'] is None else max(value,stat['max_score'])
            output.append(dict(type='Feature',geometry=dict(type='LineString',coordinates=[p,q]),
                properties=dict(source_index=number,source_id=features[number].get('id',number),
                    classification=category,score=value,length_m=dist,reason=reason)))
    summaries=[]
    for i,stat in enumerate(statistics):
        covered=stat['covered_length_m'];total=stat['total_length_m']
        weighted=stat.pop('weighted_sum')
        summaries.append(dict(source_index=i,source_id=features[i].get('id',i),**stat,
            coverage_fraction=covered/total if total else 0.,
            higher_fraction_of_covered=stat['higher_length_m']/covered if covered else None,
            mean_score_on_covered=weighted/covered if covered else None))
    return dict(type='FeatureCollection',features=output,line_summaries=summaries,
        region_bbox_wgs84=bbox,threshold=threshold,sampling_step_m=100,
        method='nearest native raster cell at each geodesic subsegment midpoint; length-weighted summaries',
        distance_to_hotspot_used=False,
        interpretation='local map susceptibility along assets; not fire arrival or damage probability')
