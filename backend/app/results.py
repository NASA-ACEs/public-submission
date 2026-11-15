"""Registry of real result bundles (``backend-result-1``, written by machine-learning ``ml.map``).

A bundle is a directory with ``result.json``, one GeoTIFF per layer and an optional ``vectors.geojson``.
Bundles are read-only here: overviews for large rasters go to a cache directory, never into the bundle.
"""
import io
import json
import math
import os
import re
import shutil
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from PIL import Image
from rasterio.enums import Resampling
from rasterio.features import shapes
from scipy import ndimage
from rasterio.transform import from_bounds
from rasterio.warp import reproject, transform, transform_bounds
from rasterio.windows import Window, from_bounds as window_from_bounds, transform as window_transform

from . import spreadsim

SCHEMA = 'backend-result-1'
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
RESERVED = {'latest', 'rescan', 'forest-demo-v1'}
HALF = 20037508.342789244
OVERVIEW_MIN_PIXELS = 2_000_000      # rasters at least this large get a cached copy with overviews
STATS_SIZE = 512                     # side of the decimated read used for colour ranges
TILE_CACHE = 512
GRID_DEFAULT_CELLS, GRID_MAX_CELLS = 65_536, 262_144
VECTOR_IDS = ('vectors', 'illustrative_pathways')   # the second is what the current frontend requests
# Fire growth from a point (spreadsim): window read around the point, limits of a request, cached answers.
SPREAD_WIND = ('wind_speed', 'wind_from')            # wind of the map day, used when a request gives none
SPREAD_PLAIN = '_plain'      # ml.map: the fuel components again, from the fuel map alone (no NISAR GCOV fuel state)
SPREAD_HALF_CELLS, SPREAD_HALF_MIN_M, SPREAD_HALF_MAX_M = 225, 6000, 30000
SPREAD_MAX_HOURS, SPREAD_MAX_STEPS, SPREAD_MAX_WIND = 12, 48, 40      # 48 steps: a fire every 10 minutes for an animation
SPREAD_SNAP_CELLS = 10                               # spreadsim.snap: an ignition on a road starts beside it
SPREAD_CACHE = 64
# Head-fire arrows for the map view (Result.view_vectors): sampled cells of rate_of_spread and spread_direction.
VIEW_VECTORS = ('rate_of_spread', 'spread_direction')
VIEW_VECTORS_DEFAULT, VIEW_VECTORS_MAX = 400, 2000
VIEW_VECTORS_LONGEST_M = 5000        # as ml.map: an arrow is the head fire's run, at most this long
VIEW_VECTORS_DIRECT = 1_500_000      # windows up to this many cells are read as they are; larger ones from kept lattices
VIEW_VECTORS_KEPT = 8                # the smallest lattice step kept in memory for large rasters

PALETTES = dict(
    lfmc=['#d73027', '#fee08b', '#1a9850'], soil=['#fff7bc', '#41b6c4', '#253494'],
    risk=['#ffffcc', '#fd8d3c', '#bd0026'], continuous=['#440154', '#21908c', '#fde725'],
    diverging=['#8c510a', '#f5f5f5', '#01665e'], diverging_dry=['#01665e', '#f5f5f5', '#8c510a'],
    classes=['#ffffb2', '#fecc5c', '#fd8d3c', '#f03b20', '#bd0026'],
    # compass directions, one stop per 45 degrees from north and back to it: blue on the east side, red on the west,
    # dark at north and light at south, so the sides stay apart for red-green colour blindness
    compass=['#2b1d6e', '#2a55a8', '#2f8fd0', '#a9dbe8', '#fbe76a', '#f29a45', '#cf3b4e', '#7a2466', '#2b1d6e'])
# Outline of the land burned a given number of years ago (Result.scars), from the fuel-age layer of the bundle.
SCAR_LAYER = 'years_since_fire'
SCAR_MAX_CELLS = 4_000_000           # larger rasters are read decimated
SCAR_MIN_PATCH_HA = 4.0              # smaller patches and holes are dropped: they are raster noise at a perimeter's edge
SCAR_MAX_VERTICES = 20_000           # of the whole answer; beyond it the outline is simplified further
SCAR_CACHE = 16
COMPASS = ('direction', 'aspect', '_from')      # layer names that hold a compass direction when their units are degrees
TITLES = dict(      # short: the frontend's layer picker is about 24 characters wide, fewer in capitals
    lfmc='Live fuel moisture', lfmc_p05='Live fuel moisture, 5 %', lfmc_p95='Live fuel moisture, 95 %',
    soil_moisture='Surface soil moisture', soil_moisture_change='NISAR soil moisture change',
    lfmc_nisar_minus_smap='LFMC: NISAR minus SMAP', dryness_anomaly='Dryness anomaly',
    p_large_fire='P(large fire | ignition)', risk_index='Large-fire risk rank',
    susceptibility_index='Risk rank (fuel context)', risk_class='Large-fire risk class', weather_factor='Weather factor',
    risk_index_smap='Risk rank without NISAR', risk_index_nisar_minus_smap='Risk: NISAR minus SMAP',
    nisar_term='SME2 fine-scale term', fuel_state='Fuel state (GCOV)',
    sme2_anomaly='SME2 anomaly (exp.)', risk_index_sme2='Risk rank, SME2 (exp.)',
    p_large_fire_sme2='P(large), SME2 (exp.)', risk_index_sme2_minus_main='SME2 effect (exp.)',
    years_since_fire='Years since fire', spread_permeability='Spread permeability',
    rate_of_spread='Head-fire rate of spread', flame_length='Head-fire flame length',
    fireline_intensity='Fireline intensity', spread_direction='Head-fire direction')
DIFFERENCE = ('minus', 'change', 'anomaly', 'delta', 'diff')
# a result with ``ordinary_day`` (ml.map from v3.3.1) computes this layer with every weather input at its median over
# the training ignitions: what the land contributes. In older bundles the file is a copy of risk_index
LAND_TITLE = dict(susceptibility_index='Land susceptibility')


class BundleError(ValueError):
    pass


class SpreadError(ValueError):
    """A fire-growth request this result cannot answer; ``status`` is the HTTP status to return."""

    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def _finite(value):
    """JSON-safe copy: NaN and infinities become null, numpy scalars become Python numbers."""
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    return value


def _percentiles(path, band_nodata, qs):
    with rasterio.open(path) as ds:
        shape = (min(ds.height, STATS_SIZE), min(ds.width, STATS_SIZE))
        a = ds.read(1, out_shape=shape, resampling=Resampling.nearest, masked=True).astype('float64').compressed()
    a = a[np.isfinite(a)]
    return [float(v) for v in np.percentile(a, qs)] if a.size else None


def _style(layer_id, spec, path, nodata):
    """Title, units, colour range and palette of a layer: from the manifest when given, else from kind and name."""
    kind = str(spec.get('kind') or 'continuous')
    name = layer_id.lower()
    difference = any(word in name for word in DIFFERENCE)
    units, limits, palette, discrete = '', None, PALETTES['continuous'], False
    if kind == 'class':
        units, limits, palette, discrete = 'class', [1, 5], PALETTES['classes'], True
    elif difference:
        top = _percentiles(path, nodata, [2, 98])
        top = max(abs(top[0]), abs(top[1])) if top else 0
        limits = [-(top or 1.0), top or 1.0]
        palette = PALETTES['diverging_dry' if 'dry' in name else 'diverging']
    elif kind == 'probability':
        top = _percentiles(path, nodata, [98])
        units, limits, palette = 'probability', [0, max(top[0] if top else 0, 1e-4)], PALETTES['risk']
    elif kind == 'index':
        units, limits, palette = '1', [0, 1], PALETTES['risk']
    elif name.startswith('lfmc'):
        limits, palette = [30, 200], PALETTES['lfmc']
    elif name.startswith('soil_moisture'):
        limits, palette = [0, 0.5], PALETTES['soil']
    if name.startswith('lfmc'):
        units = '% dry mass'
    elif name.startswith('soil_moisture'):
        units = 'm3/m3'
    units = str(spec.get('units') or units)
    compass = kind != 'class' and units.lower().startswith('deg') and any(word in name for word in COMPASS)
    if compass:         # 355 and 0 degrees are neighbours: a fixed full circle and a palette that closes on itself
        limits, palette = [0, 360], PALETTES['compass']
    if limits is None:
        limits = _percentiles(path, nodata, [2, 98]) or [0, 1]
        if limits[1] <= limits[0]:
            limits = [limits[0], limits[0] + 1]
    given = spec.get('range')
    if isinstance(given, (list, tuple)) and len(given) == 2 and all(isinstance(v, (int, float)) for v in given) \
            and given[0] < given[1]:
        limits = [float(given[0]), float(given[1])]
    colours = spec.get('palette')
    if isinstance(colours, list) and len(colours) >= 2 and all(re.fullmatch(r'#[0-9a-fA-F]{6}', str(c)) for c in colours):
        palette = colours
    # a mean of compass directions is not a direction (350 and 10 give 180): such layers are never averaged.
    # cyclic tells a client to label the legend with compass points; it holds only for the full circle and a closed palette
    # neutral: for a ratio (units x) whose range spans 1, the value where the day changes nothing; a client marks it
    return dict(kind=kind, title=str(spec.get('title') or TITLES.get(layer_id) or layer_id.replace('_', ' ').capitalize()),
                units=units, range=[float(limits[0]), float(limits[1])], palette=list(palette), discrete=discrete,
                nearest=discrete or compass,
                cyclic=compass and list(limits) == [0, 360] and str(palette[0]).lower() == str(palette[-1]).lower(),
                neutral=1.0 if units.lower() == 'x' and limits[0] < 1 < limits[1] else None)


def _simplify(ring, tolerance):
    """Douglas-Peucker on a closed ring of (x, y): vertices nearer than ``tolerance`` to the simplified line go."""
    pts = np.asarray(ring[:-1], dtype='float64')
    if len(pts) < 5:
        return ring
    far = int(np.argmax(((pts - pts[0]) ** 2).sum(axis=1)))        # split the ring at its two most distant vertices
    keep = np.zeros(len(pts), dtype=bool)
    keep[[0, far]] = True
    stack = [(0, far), (far, len(pts))]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        start, end = pts[a], pts[b % len(pts)]
        seg = end - start
        rel = pts[a + 1:b] - start
        norm = math.hypot(*seg)
        dist = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / norm if norm else np.hypot(rel[:, 0], rel[:, 1])
        i = int(np.argmax(dist))
        if dist[i] > tolerance:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    out = pts[keep].tolist()
    return out + [out[0]] if len(out) >= 3 else ring


def _iso(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def _redact(value):
    """Server paths in a provenance file become file names."""
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str) and value.startswith('/') and '\n' not in value:
        return Path(value).name
    return value


def _png(rgba):
    stream = io.BytesIO()
    Image.fromarray(rgba).save(stream, format='PNG')
    return stream.getvalue()


BLANK_TILE = _png(np.zeros((256, 256, 4), dtype='uint8'))


def colorize(values, layer):
    valid = np.isfinite(values)
    lo, hi = layer['range']
    rgb = np.array([[int(s[i:i + 2], 16) for i in (1, 3, 5)] for s in layer['palette']])
    stops = np.linspace(0, 1, len(rgb))
    t = np.clip((np.where(valid, values, lo) - lo) / (hi - lo), 0, 1)
    if layer['discrete']:
        t = np.round(t * (len(rgb) - 1)) / (len(rgb) - 1)
    rgba = np.zeros((*values.shape, 4), dtype='uint8')
    for channel in range(3):
        rgba[:, :, channel] = np.interp(t, stops, rgb[:, channel]).astype('uint8')
    rgba[:, :, 3] = np.where(valid, 210, 0)
    return _png(rgba)


def _merc_to_lonlat(x, y):
    return x / HALF * 180, math.degrees(2 * math.atan(math.exp(y / HALF * math.pi)) - math.pi / 2)


class Result:
    """One validated bundle. Raises ``BundleError`` if the manifest or a layer is not acceptable."""

    def __init__(self, directory, cache):
        self.dir = Path(directory).resolve()
        self.cache = Path(cache)
        manifest_path = self.dir / 'result.json'
        self.version = manifest_path.stat().st_mtime_ns
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            raise BundleError('result.json is not readable JSON')
        if not isinstance(manifest, dict) or manifest.get('schema_version') != SCHEMA:
            raise BundleError(f'schema_version is not {SCHEMA}')
        self.manifest = manifest
        self.id = manifest.get('result_id')
        if not isinstance(self.id, str) or not SAFE_ID.match(self.id) or self.id in RESERVED:
            raise BundleError('result_id must match [A-Za-z0-9._-]{1,100} and not be a reserved name')
        specs = manifest.get('layers')
        if not isinstance(specs, dict) or not specs:
            raise BundleError('manifest has no layers')
        self.layers = OrderedDict()
        self._locks = {}
        for layer_id, spec in specs.items():
            if not isinstance(layer_id, str) or not SAFE_ID.match(layer_id) or not isinstance(spec, dict):
                raise BundleError(f'invalid layer entry {layer_id!r}')
            path = self._inside(spec.get('path'), layer_id)
            try:
                with rasterio.open(path) as ds:
                    if ds.crs is None or ds.count < 1:
                        raise BundleError(f'layer {layer_id}: no CRS or no band')
                    info = dict(crs=ds.crs, transform=ds.transform, width=ds.width, height=ds.height,
                                bounds=tuple(ds.bounds), nodata=ds.nodata, dtype=ds.dtypes[0],
                                overviews=bool(ds.overviews(1)))
            except rasterio.errors.RasterioError:
                raise BundleError(f'layer {layer_id}: not a readable raster')
            self.layers[layer_id] = dict(id=layer_id, path=path, **info, **_style(layer_id, spec, path, info['nodata']))
            if isinstance(manifest.get('ordinary_day'), dict) and layer_id in LAND_TITLE and not spec.get('title'):
                self.layers[layer_id]['title'] = LAND_TITLE[layer_id]
            self._locks[layer_id] = threading.Lock()
        wanted = manifest.get('assessment_layer')
        self.assessment_layer = wanted if wanted in self.layers else next(
            (k for k, v in self.layers.items() if v['kind'] == 'index'), next(iter(self.layers)))
        first = self.layers[self.assessment_layer]
        self.bbox = self._bbox(manifest.get('bbox_wgs84'), first)
        breaks = manifest.get('class_breaks')
        ok = isinstance(breaks, list) and breaks and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in breaks)
        self.class_breaks = [float(v) for v in breaks] if ok else None
        # "higher" = the upper classes: with breaks 0.2/0.4/0.6/0.8 the default threshold is 0.6 (classes 4 and 5)
        self.threshold = self.class_breaks[len(self.class_breaks) // 2] if self.class_breaks else 0.5
        self.valid_to = _iso(manifest.get('valid_to')) or _iso(manifest.get('as_of')) or self.version / 1e9
        vectors = self.dir / 'vectors.geojson'
        self.vectors_path = vectors if vectors.is_file() else None
        self._vectors = None
        self._tiles = OrderedDict()
        self._tile_lock = threading.Lock()
        # Layers of kind "component" are inputs of the fire-growth simulation, not maps to look at.
        self.display = [k for k, v in self.layers.items() if v['kind'] != 'component']
        grid = lambda k: (self.layers[k]['crs'], self.layers[k]['transform'], self.layers[k]['width'], self.layers[k]['height'])
        self.spread_ready = all(k in self.layers for k in spreadsim.LAYERS) and len(
            {grid(k) for k in (*spreadsim.LAYERS, *SPREAD_WIND, 'spread_permeability') if k in self.layers}) == 1
        self.spread_wind = self.spread_ready and all(k in self.layers for k in SPREAD_WIND)
        # a bundle whose components carry the NISAR fuel state also has them without it, to compare the two fires
        self.spread_plain = [k for k in spreadsim.LAYERS if k.startswith('spread_')]
        self.spread_fuel_state = self.spread_ready and all(
            k + SPREAD_PLAIN in self.layers and grid(k + SPREAD_PLAIN) == grid(k) for k in self.spread_plain)
        self._spreads = OrderedDict()
        self._spread_lock = threading.Lock()
        self.vector_layers = [k for k in (*VIEW_VECTORS, 'flame_length') if k in self.layers]
        self.view_vectors_ready = all(k in self.layers for k in VIEW_VECTORS) and len({grid(k) for k in self.vector_layers}) == 1
        self._lattices = None
        self._lattice_lock = threading.Lock()
        self._scars = OrderedDict()
        self._scar_lock = threading.Lock()

    def _inside(self, relative, layer_id):
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise BundleError(f'layer {layer_id}: path must be relative to the bundle')
        path = (self.dir / relative).resolve()
        if not path.is_relative_to(self.dir):
            raise BundleError(f'layer {layer_id}: path leaves the bundle directory')
        if not path.is_file():
            raise BundleError(f'layer {layer_id}: file {Path(relative).name} is missing')
        return path

    @staticmethod
    def _bbox(given, layer):
        if isinstance(given, (list, tuple)) and len(given) == 4 and all(
                isinstance(v, (int, float)) and math.isfinite(v) for v in given) \
                and -180 <= given[0] < given[2] <= 180 and -90 <= given[1] < given[3] <= 90:
            return [float(v) for v in given]
        return [float(v) for v in transform_bounds(layer['crs'], 'EPSG:4326', *layer['bounds'], densify_pts=21)]

    # --- raster access ----------------------------------------------------------------------------------------
    def source(self, layer_id):
        """Path to read: the bundle file, or for a large raster without overviews a cached copy that has them."""
        layer = self.layers[layer_id]
        if layer['overviews'] or layer['width'] * layer['height'] < OVERVIEW_MIN_PIXELS:
            return layer['path']
        stat = layer['path'].stat()
        how = 'nearest' if layer['nearest'] else 'average'
        target = self.cache / self.id / f'{layer_id}-{stat.st_mtime_ns}-{stat.st_size}-{how}.tif'
        if target.exists():
            return target
        with self._locks[layer_id]:
            if target.exists():
                return target
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                for old in target.parent.glob(f'{layer_id}-*.tif'):
                    old.unlink(missing_ok=True)
                partial = target.with_suffix('.partial')
                shutil.copyfile(layer['path'], partial)
                with rasterio.open(partial, 'r+') as ds:
                    factors = [f for f in (2, 4, 8, 16, 32, 64) if min(ds.width, ds.height) // f >= 64]
                    ds.build_overviews(factors, Resampling.nearest if layer['nearest'] else Resampling.average)
                os.replace(partial, target)
            except (OSError, rasterio.errors.RasterioError) as error:
                print(f'[results] no overview cache for {self.id}/{layer_id}: {error}', flush=True)
                return layer['path']
        return target

    def _read(self, ds, layer, window, shape):
        how = Resampling.nearest if layer['nearest'] or shape == (window.height, window.width) else Resampling.average
        a = ds.read(1, window=window, out_shape=shape, resampling=how, masked=True)
        return np.where(np.ma.getmaskarray(a), np.nan, a.astype('float32')).astype('float32')

    def render(self, layer_id, bounds, width, height):
        """Layer values on a ``height`` x ``width`` Web Mercator grid over ``bounds``; NaN where there is no data.

        Reads only the source window under the grid, decimated to about the grid's resolution.
        """
        layer = self.layers[layer_id]
        out = np.full((height, width), np.nan, dtype='float32')
        west, south = _merc_to_lonlat(bounds[0], bounds[1])
        east, north = _merc_to_lonlat(bounds[2], bounds[3])
        box = (max(west, self.bbox[0]), max(south, self.bbox[1]), min(east, self.bbox[2]), min(north, self.bbox[3]))
        if not (box[0] < box[2] and box[1] < box[3]):
            return out
        share = min((box[2] - box[0]) / (east - west), 1.0), min((box[3] - box[1]) / (north - south), 1.0)
        with rasterio.open(self.source(layer_id)) as ds:
            left, bottom, right, top = transform_bounds('EPSG:4326', ds.crs, *box, densify_pts=21)
            full = Window(0, 0, ds.width, ds.height)
            try:
                window = window_from_bounds(left, bottom, right, top, ds.transform)
                window = Window(math.floor(window.col_off) - 2, math.floor(window.row_off) - 2,
                                math.ceil(window.width) + 4, math.ceil(window.height) + 4).intersection(full)
            except (rasterio.errors.WindowError, ValueError):
                return out
            if window.width < 1 or window.height < 1:
                return out
            factor = max(1, int(min(window.width / max(1.0, width * share[0]), window.height / max(1.0, height * share[1]))))
            shape = (max(1, round(window.height / factor)), max(1, round(window.width / factor)))
            source = self._read(ds, layer, window, shape)
            t, sx, sy = ds.window_transform(window), window.width / shape[1], window.height / shape[0]
            src_transform = Affine(t.a * sx, t.b * sy, t.c, t.d * sx, t.e * sy, t.f)
            reproject(source, out, src_transform=src_transform, src_crs=ds.crs, src_nodata=np.nan,
                      dst_transform=from_bounds(*bounds, width, height), dst_crs='EPSG:3857', dst_nodata=np.nan,
                      resampling=Resampling.nearest if layer['nearest'] else Resampling.bilinear)
        return out

    def tile(self, layer_id, z, x, y):
        key = (layer_id, z, x, y)
        with self._tile_lock:
            if key in self._tiles:
                self._tiles.move_to_end(key)
                return self._tiles[key]
        span = 2 * HALF / 2 ** z
        bounds = (-HALF + x * span, HALF - (y + 1) * span, -HALF + (x + 1) * span, HALF - y * span)
        values = self.render(layer_id, bounds, 256, 256)
        content = colorize(values, self.layers[layer_id]) if np.isfinite(values).any() else BLANK_TILE
        with self._tile_lock:
            self._tiles[key] = content
            while len(self._tiles) > TILE_CACHE:
                self._tiles.popitem(last=False)
        return content

    def preview(self, layer_id, width):
        layer = self.layers[layer_id]
        height = max(1, round(width * layer['height'] / layer['width']))
        with rasterio.open(self.source(layer_id)) as ds:
            values = self._read(ds, layer, Window(0, 0, ds.width, ds.height), (height, width))
        return colorize(values, layer)

    def point(self, lon, lat):
        inside = self.bbox[0] <= lon <= self.bbox[2] and self.bbox[1] <= lat <= self.bbox[3]
        values, center = {key: None for key in self.layers}, None
        for key, layer in self.layers.items() if inside else ():
            with rasterio.open(layer['path']) as ds:
                x, y = transform('EPSG:4326', ds.crs, [lon], [lat])
                row, col = ds.index(x[0], y[0])
                if not (0 <= row < ds.height and 0 <= col < ds.width):
                    continue
                value = ds.read(1, window=Window(col, row, 1, 1), masked=True)[0, 0]
                if value is not np.ma.masked and math.isfinite(float(value)):
                    values[key] = int(value) if layer['discrete'] else float(value)
                if center is None:
                    cx, cy = rasterio.transform.xy(ds.transform, row, col)
                    cl, ct = transform(ds.crs, 'EPSG:4326', [cx], [cy])
                    center = {'lon': cl[0], 'lat': ct[0]}
        valid = any(v is not None for v in values.values())
        return dict(result_id=self.id, synthetic=False, valid=valid, values=values, sampled_cell_center=center,
                    units={key: layer['units'] for key, layer in self.layers.items()},
                    reason=None if valid else 'NO_VALID_DATA' if inside else 'OUTSIDE_AOI', sample_method='nearest_cell')

    def grid(self, layer_id, bbox=None, max_cells=GRID_DEFAULT_CELLS):
        """Numeric Web Mercator grid of a layer over ``bbox`` (WGS84), with at most ``max_cells`` values."""
        layer = self.layers[layer_id]
        box = self.bbox if bbox is None else [max(bbox[0], self.bbox[0]), max(bbox[1], self.bbox[1]),
                                              min(bbox[2], self.bbox[2]), min(bbox[3], self.bbox[3])]
        if not (box[0] < box[2] and box[1] < box[3]):
            raise ValueError('bbox does not intersect the result')
        x0, y0, x1, y1 = transform_bounds('EPSG:4326', 'EPSG:3857', *box)
        native = abs(layer['transform'].a)
        if layer['crs'].is_geographic:
            native *= 111_320
        # Mercator metres per native cell at the box latitude
        cell = native / max(0.2, math.cos(math.radians((box[1] + box[3]) / 2)))
        width, height = max(1, math.ceil((x1 - x0) / cell)), max(1, math.ceil((y1 - y0) / cell))
        step = max(1.0, math.sqrt(width * height / max_cells))
        if step > 1:
            width, height = max(1, math.floor(width / step)), max(1, math.floor(height / step))
        values = self.render(layer_id, (x0, y0, x1, y1), width, height)
        affine = from_bounds(x0, y0, x1, y1, width, height)
        digits = 0 if layer['discrete'] else 5
        flat = [None if not ok else int(v) if layer['discrete'] else round(float(v), digits)
                for v, ok in zip(values.ravel(), np.isfinite(values).ravel())]
        return dict(result_id=self.id, layer_id=layer_id, synthetic=False, crs='EPSG:3857', width=width, height=height,
                    transform=list(affine)[:6], values=flat, units=layer['units'], kind=layer['kind'],
                    bbox_wgs84=box, max_cells=max_cells, decimated=step > 1, decimation=round(step, 3),
                    cell_size_m=round(native * step, 1), native_support_m=round(native, 1),
                    resampling='nearest' if layer['nearest'] else 'average then bilinear' if step > 1 else 'bilinear',
                    threshold=self.threshold if layer['kind'] == 'index' else None,
                    threshold_meaning='lower bound of the upper risk classes (class_breaks); a display policy',
                    as_of=self.manifest.get('as_of'), valid_from=self.manifest.get('valid_from'),
                    valid_to=self.manifest.get('valid_to'),
                    meaning=self.manifest.get('target') or layer['title'])

    # --- fire growth from a point -----------------------------------------------------------------------------
    def spread(self, lon, lat, hours=(1, 3, 6), wind=None, fuel_age=True, fuel_state=True):
        """Isochrones of a fire started at (lon, lat): ``spreadsim.simulate`` on a window of the component layers.

        ``wind`` = (m/s at 10 m, degrees it blows from), or None for the wind of the map day stored in the bundle.
        ``fuel_state=False`` grows the fire on the fuel map alone, without the NISAR GCOV fuel state.
        Raises ``SpreadError`` (404: the result has no component layers; 422: a request it cannot answer).
        """
        if not self.spread_ready:
            raise SpreadError('This result has no fire-growth layers (spread_ros0, ...): it was written before '
                              'machine-learning added them, or it is the synthetic demo', 404)
        if not fuel_state and not self.spread_fuel_state:
            raise SpreadError('This result has no fire-growth layers without the NISAR fuel state (spread_ros0_plain, '
                              '...): its fuels carry no GCOV fuel state, or it was written before ml.map v3.3.3')
        if wind is None and not self.spread_wind:
            raise SpreadError('This result does not store the wind of the map day: pass wind_speed and wind_from')
        hours = tuple(int(h) if float(h).is_integer() else float(h) for h in sorted({float(h) for h in hours}))
        key = (round(lon, 5), round(lat, 5), hours, None if wind is None else (round(wind[0], 2), round(wind[1], 1)),
               bool(fuel_age), bool(fuel_state))
        with self._spread_lock:
            if key in self._spreads:
                self._spreads.move_to_end(key)
                return self._spreads[key]
        first = self.layers['spread_ros0']
        x, y = transform('EPSG:4326', first['crs'], [lon], [lat])
        inv = ~first['transform']
        col, row = inv.a * x[0] + inv.b * y[0] + inv.c, inv.d * x[0] + inv.e * y[0] + inv.f
        if not (math.isfinite(col) and math.isfinite(row) and 0 <= row < first['height'] and 0 <= col < first['width']):
            raise SpreadError('The point is outside this result')
        cell = abs(first['transform'].a) * (111_320 if first['crs'].is_geographic else 1)
        half_m = min(max(SPREAD_HALF_MIN_M, SPREAD_HALF_CELLS * cell), SPREAD_HALF_MAX_M)
        n = int(half_m / abs(first['transform'].a))
        r0, c0 = max(0, int(row) - n), max(0, int(col) - n)
        window = Window(c0, r0, min(first['width'], int(col) + n + 1) - c0, min(first['height'], int(row) + n + 1) - r0)
        names = [*spreadsim.LAYERS, *(() if wind else SPREAD_WIND),
                 *(('spread_permeability',) if fuel_age and 'spread_permeability' in self.layers else ())]
        arrays = {'transform': window_transform(window, first['transform']), 'crs': first['crs']}
        for name in names:
            source = name + SPREAD_PLAIN if not fuel_state and name in self.spread_plain else name
            with rasterio.open(self.layers[source]['path']) as ds:
                a = ds.read(1, window=window, masked=True)
            arrays[name] = np.where(np.ma.getmaskarray(a), np.nan, a.astype('float32')).astype('float32')
        try:
            out = spreadsim.simulate(arrays, lon, lat, hours=hours, wind=wind, window_m=half_m, use_permeability=bool(fuel_age))
        except ValueError as error:
            raise SpreadError(str(error))
        props = out['properties']
        if not (isinstance(props.get('head_rate_m_min'), float) and math.isfinite(props['head_rate_m_min'])
                and props['head_rate_m_min'] > 0):
            raise SpreadError(f'No burnable fuel within {round(SPREAD_SNAP_CELLS * cell)} m of the point')
        props.update(wind_source='request' if wind else 'map_day', window_half_m=round(n * cell), cell_m=round(cell, 1),
                     fuel_age_permeability=bool(fuel_age and 'spread_permeability' in self.layers),
                     # true: the fuels are scaled by NISAR's L-band fuel state; false: the fuel map alone
                     nisar_fuel_state=bool(fuel_state and self.spread_fuel_state))
        m = self.manifest
        out = _finite(dict(out, result_id=self.id, synthetic=False, research_only=bool(m.get('research_only', True)),
                           valid_from=m.get('valid_from'), valid_to=m.get('valid_to'), model_version=m.get('model_version')))
        with self._spread_lock:
            self._spreads[key] = out
            while len(self._spreads) > SPREAD_CACHE:
                self._spreads.popitem(last=False)
        return out

    # --- head-fire arrows for a map view ----------------------------------------------------------------------
    def _lattice(self, step):
        """Every ``step``-th cell of the arrow layers over the whole raster, kept in memory for the coarse steps."""
        if self._lattices is None:
            with self._lattice_lock:
                if self._lattices is None:
                    first, kept = self.layers[VIEW_VECTORS[0]], {}
                    for name in self.vector_layers:
                        with rasterio.open(self.layers[name]['path']) as ds:
                            a = ds.read(1, masked=True)
                        a = np.where(np.ma.getmaskarray(a), np.nan, a.astype('float32')).astype('float32')
                        s = VIEW_VECTORS_KEPT
                        while s < 2 * max(first['width'], first['height']):
                            kept.setdefault(s, {})[name] = a[::s, ::s].copy()
                            s *= 2
                    self._lattices = kept
        return self._lattices[step]

    def view_vectors(self, bbox=None, n=VIEW_VECTORS_DEFAULT, hours=1.0):
        """About ``n`` head-fire arrows inside ``bbox`` (WGS84): cells of a regular lattice of the result's grid.

        The lattice is every ``step``-th row and column of the grid, ``step`` a power of two: arrows stay where they
        are while the map is panned, and zooming in adds arrows between those already shown. Values are those of the
        sampled cell, not averages (a direction cannot be averaged). Same feature schema as ``vectors.geojson``."""
        if not self.view_vectors_ready:
            raise SpreadError('This result has no rate_of_spread and spread_direction layers on one grid', 404)
        first = self.layers[VIEW_VECTORS[0]]
        out = dict(type='FeatureCollection', features=[], result_id=self.id, synthetic=False, run_hours=hours,
                   longest_m=VIEW_VECTORS_LONGEST_M, step_cells=None, spacing_m=None)
        box = self.bbox if bbox is None else [max(bbox[0], self.bbox[0]), max(bbox[1], self.bbox[1]),
                                              min(bbox[2], self.bbox[2]), min(bbox[3], self.bbox[3])]
        if not (box[0] < box[2] and box[1] < box[3]):
            return out
        w = window_from_bounds(*transform_bounds('EPSG:4326', first['crs'], *box, densify_pts=21), first['transform'])
        r0, c0 = max(0, math.floor(w.row_off)), max(0, math.floor(w.col_off))
        r1, c1 = min(first['height'], math.ceil(w.row_off + w.height)), min(first['width'], math.ceil(w.col_off + w.width))
        if r1 <= r0 or c1 <= c0:
            return out
        step = 1
        while ((r1 - r0) / step) * ((c1 - c0) / step) > n:
            step *= 2
        i0, i1, j0, j1 = -(-r0 // step), (r1 - 1) // step, -(-c0 // step), (c1 - 1) // step     # lattice rows i * step
        cell = abs(first['transform'].a) * (111_320 if first['crs'].is_geographic else 1)
        out.update(step_cells=step, spacing_m=round(step * cell, 1))
        if i1 < i0 or j1 < j0:
            return out
        if step >= VIEW_VECTORS_KEPT and (r1 - r0) * (c1 - c0) > VIEW_VECTORS_DIRECT:
            kept = self._lattice(step)
            values = {name: kept[name][i0:i1 + 1, j0:j1 + 1] for name in self.vector_layers}
        else:
            window = Window(j0 * step, i0 * step, (j1 - j0) * step + 1, (i1 - i0) * step + 1)
            values = {}
            for name in self.vector_layers:
                with rasterio.open(self.layers[name]['path']) as ds:
                    a = ds.read(1, window=window, masked=True)
                values[name] = np.where(np.ma.getmaskarray(a), np.nan, a.astype('float32'))[::step, ::step]
        ros, direction = values['rate_of_spread'], values['spread_direction']
        rows, cols = np.nonzero(np.isfinite(ros) & (ros > 0) & np.isfinite(direction))
        if not rows.size:
            return out
        t, cx, cy = first['transform'], (j0 + cols) * step + 0.5, (i0 + rows) * step + 0.5      # cell centres
        x, y = t.a * cx + t.b * cy + t.c, t.d * cx + t.e * cy + t.f
        lon, lat = (np.asarray(v) for v in transform(first['crs'], 'EPSG:4326', x.tolist(), y.tolist()))
        ros, direction = ros[rows, cols].astype('float64'), direction[rows, cols].astype('float64')
        flame = values['flame_length'][rows, cols] if 'flame_length' in values else np.full(rows.size, np.nan)
        inside = (lon >= box[0]) & (lon <= box[2]) & (lat >= box[1]) & (lat <= box[3])
        # the run along the true azimuth: the grid of a projected result need not keep angles (EPSG:6933 does not)
        run, rad = np.minimum(ros * 60 * hours, VIEW_VECTORS_LONGEST_M), np.deg2rad(direction)
        lon1 = lon + run * np.sin(rad) / (111_320 * np.maximum(np.cos(np.deg2rad(lat)), 0.01))
        lat1 = lat + run * np.cos(rad) / 110_574
        out['features'] = [
            dict(type='Feature', geometry=dict(type='LineString', coordinates=[[round(a, 6), round(b, 6)], [round(c, 6), round(d, 6)]]),
                 properties=dict(rate_of_spread_m_min=round(v, 2), direction_deg=round(t, 1),
                                 flame_length_m=round(float(f), 2) if math.isfinite(f) else None, run_hours=hours))
            for a, b, c, d, v, t, f in zip(lon[inside].tolist(), lat[inside].tolist(), lon1[inside].tolist(), lat1[inside].tolist(),
                                           ros[inside].tolist(), direction[inside].tolist(), flame[inside].tolist())]
        return out

    # --- outline of older fire scars ----------------------------------------------------------------------------
    def scars(self, min_years, max_years):
        """Outline of the cells whose fuel age lies between ``min_years`` and ``max_years`` (GeoJSON, WGS84).

        The fuel-age layer comes from mapped fire perimeters, so this is where the fires of those years burned inside
        the result: polygons of at least ``SCAR_MIN_PATCH_HA``, smoothed by one cell and simplified.
        Raises ``SpreadError`` 404 where the result has no fuel-age layer.
        """
        if SCAR_LAYER not in self.layers:
            raise SpreadError(f'This result has no {SCAR_LAYER} layer', 404)
        key = (round(float(min_years), 3), round(float(max_years), 3))
        with self._scar_lock:
            if key in self._scars:
                self._scars.move_to_end(key)
                return self._scars[key]
        layer = self.layers[SCAR_LAYER]
        with rasterio.open(layer['path']) as ds:
            step = max(1, math.ceil(math.sqrt(ds.width * ds.height / SCAR_MAX_CELLS)))
            shape = (max(1, ds.height // step), max(1, ds.width // step))
            a = ds.read(1, out_shape=shape, resampling=Resampling.nearest, masked=True)
            sx, sy, t = ds.width / shape[1], ds.height / shape[0], ds.transform
            t = Affine(t.a * sx, t.b * sy, t.c, t.d * sx, t.e * sy, t.f)
            crs = ds.crs
        years = np.where(np.ma.getmaskarray(a), np.nan, a.astype('float32'))
        mask = np.isfinite(years) & (years >= key[0]) & (years <= key[1])
        cell = abs(t.a) * (111_320 if crs.is_geographic else 1)
        cell_ha = cell * cell / 1e4
        # Close one-cell gaps, then drop patches and holes too small to be a fire: the outline is of perimeters, not cells.
        mask = ndimage.binary_closing(mask, structure=np.ones((3, 3), bool))
        few = max(1, round(SCAR_MIN_PATCH_HA / cell_ha))
        for value in (True, False):
            labels, n = ndimage.label(mask == value)
            if n:
                sizes = ndimage.sum_labels(np.ones_like(labels), labels, index=np.arange(1, n + 1))
                small = np.flatnonzero(sizes < few) + 1
                # a hole that touches the edge of the raster is the outside, whatever its size
                edge = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])) if not value else []
                mask[np.isin(labels, np.setdiff1d(small, edge))] = not value
        polygons = [g['coordinates'] for g, v in shapes(mask.astype('uint8'), mask=mask, transform=t) if v == 1]
        tolerance, features = cell * 0.75, []
        while True:
            rings = [[_simplify(ring, tolerance) for ring in polygon] for polygon in polygons]
            if sum(len(r) for p in rings for r in p) <= SCAR_MAX_VERTICES or tolerance > cell * 40:
                break
            tolerance *= 2
        for polygon in rings:
            lonlat = []
            for ring in polygon:
                xs, ys = transform(crs, 'EPSG:4326', [p[0] for p in ring], [p[1] for p in ring])
                lonlat.append([[round(x, 6), round(y, 6)] for x, y in zip(xs, ys)])
            features.append(dict(type='Feature', geometry=dict(type='Polygon', coordinates=lonlat), properties={}))
        m = self.manifest
        out = dict(type='FeatureCollection', features=features, result_id=self.id, synthetic=False, layer=SCAR_LAYER,
                   min_years=key[0], max_years=key[1], area_ha=round(float(mask.sum()) * cell_ha, 1),
                   cell_m=round(cell, 1), simplified_to_m=round(tolerance, 1), valid_to=m.get('valid_to'),
                   meaning='land whose last mapped fire ended that many years before the map day; from fire perimeters')
        with self._scar_lock:
            self._scars[key] = out
            while len(self._scars) > SCAR_CACHE:
                self._scars.popitem(last=False)
        return out

    # --- documents --------------------------------------------------------------------------------------------
    def vectors(self):
        if self._vectors is None:
            features = []
            if self.vectors_path:
                try:
                    data = json.loads(self.vectors_path.read_text())
                    features = [f for f in data.get('features', []) if isinstance(f, dict) and
                                isinstance(f.get('geometry'), dict) and f['geometry'].get('coordinates')]
                except (OSError, ValueError, AttributeError):
                    features = []
            self._vectors = features
        return self._vectors

    def summary(self, path):
        m = self.manifest
        return dict(result_id=self.id, synthetic=False, metadata_url=path, valid_from=m.get('valid_from'),
                    valid_to=m.get('valid_to'), as_of=m.get('as_of'), model_version=m.get('model_version'),
                    bbox_wgs84=self.bbox, aoi=m.get('aoi'), assessment_layer=self.assessment_layer,
                    layers=list(self.display), **({'capabilities': {'spread': True}} if self.spread_ready else {}))

    KNOWN = {'schema_version', 'result_id', 'synthetic', 'research_only', 'as_of', 'valid_from', 'valid_to',
             'bbox_wgs84', 'assessment_layer', 'fuel_context_layer', 'mode', 'target', 'model_version', 'valid_pixels',
             'source_items', 'layers', 'notes', 'class_breaks', 'soil_source', 'nisar_share', 'nisar_effect', 'aoi',
             'rank_reference', 'ordinary_day'}

    def metadata(self, origin):
        m, base = self.manifest, f'{origin}/v1/burnability/results/{self.id}'
        first = self.layers[self.assessment_layer]
        res = abs(first['transform'].a) * (111_320 if first['crs'].is_geographic else 1)
        lat = (self.bbox[1] + self.bbox[3]) / 2
        native_zoom = max(0, min(18, math.ceil(math.log2(156543.03392 * math.cos(math.radians(lat)) / res))))
        layers = []
        for key in self.display:
            layer = self.layers[key]
            lo, hi = layer['range']
            n = len(layer['palette'])
            stops = [[lo + (hi - lo) * i / (n - 1), c] for i, c in enumerate(layer['palette'])]
            layers.append(dict(
                id=key, title=layer['title'], units=layer['units'], kind=layer['kind'], range=layer['range'],
                nodata=layer['nodata'], synthetic=False, discrete=layer['discrete'], cyclic=layer['cyclic'],
                neutral=layer['neutral'],
                tiles_url=f'{base}/tiles/{key}/{{z}}/{{x}}/{{y}}.png',
                arcgis_tiles_url=f'{base}/tiles/{key}/{{level}}/{{col}}/{{row}}.png',
                tilejson_url=f'{base}/tilejson/{key}', preview_url=f'{base}/preview/{key}.png',
                download_url=f'{base}/assets/{key}', grid_url=f'{base}/grid/{key}', legend=dict(stops=stops)))
        items = m.get('source_items') if isinstance(m.get('source_items'), list) else []
        cells = first['width'] * first['height']
        valid = m.get('valid_pixels')
        vectors = self.vectors()
        return dict(
            schema_version='0.1', manifest_schema_version=SCHEMA, result_id=self.id, synthetic=False,
            research_only=bool(m.get('research_only', True)), as_of=m.get('as_of'), valid_from=m.get('valid_from'),
            valid_to=m.get('valid_to'), mode=m.get('mode'), model_version=m.get('model_version'), target=m.get('target'),
            aoi=m.get('aoi'), bbox_wgs84=self.bbox,
            grid=dict(crs=first['crs'].to_string(), pixel_spacing_m=res, width=first['width'], height=first['height']),
            tile_scheme='xyz', tile_size=256, minzoom=0, maxzoom=18, max_native_zoom=native_zoom, layers=layers,
            assessment_layer=self.assessment_layer, fuel_context_layer=m.get('fuel_context_layer'),
            class_breaks=self.class_breaks, default_threshold=self.threshold,
            # what the rank of the index layers is taken among (ml.train): {ignitions, large, built_up_share_at_most, ...}
            rank_reference=m.get('rank_reference') if isinstance(m.get('rank_reference'), dict) else None,
            # the weather inputs' medians over the training ignitions: the day susceptibility_index and weather_factor
            # are measured against; absent in bundles where susceptibility_index is a copy of risk_index
            ordinary_day=m.get('ordinary_day') if isinstance(m.get('ordinary_day'), dict) else None,
            valid_fraction=valid / cells if isinstance(valid, (int, float)) and cells else None,
            soil_source=m.get('soil_source'), nisar_share=m.get('nisar_share'), nisar_effect=m.get('nisar_effect'),
            source_item_count=len(items), source_items_sample=items[:5],
            vector_layers=[dict(id=VECTOR_IDS[0], features_url=f'{base}/vectors/{VECTOR_IDS[0]}',
                                download_url=f'{base}/vectors/{VECTOR_IDS[0]}', feature_count=len(vectors),
                                meaning='vectors.geojson of the bundle', synthetic=False)],
            provenance_url=f'{base}/provenance', line_assessment_url=f'{base}/assess-lines',
            # inputs of the fire-growth simulation: served by assets/point/grid, left out of the layer list
            component_layers=[k for k in self.layers if k not in self.display],
            **({'capabilities': {'spread': True, 'spread_map_day_wind': self.spread_wind,
                                 **({'spread_fuel_state': True} if self.spread_fuel_state else {})}, 'spread_url': f'{base}/spread',
                'spread_limits': {'max_hours': SPREAD_MAX_HOURS, 'max_steps': SPREAD_MAX_STEPS, 'max_wind_m_s': SPREAD_MAX_WIND}}
               if self.spread_ready else {}),
            **({'scars_url': f'{base}/scars'} if SCAR_LAYER in self.layers else {}),
            **({'view_vectors_url': f'{base}/vectors',
                'view_vectors_limits': {'max_n': VIEW_VECTORS_MAX, 'max_hours': SPREAD_MAX_HOURS}}
               if self.view_vectors_ready else {}),
            notes=m.get('notes') or [], notices=m.get('notes') or [],
            extra={k: v for k, v in m.items() if k not in self.KNOWN})

    def provenance(self):
        path = self.dir / 'provenance.json'
        try:
            body = json.loads(path.read_text()) if path.is_file() else self.manifest
        except (OSError, ValueError):
            body = self.manifest
        return dict(_redact(body), result_id=self.id, synthetic=False)


class Registry:
    """Bundles under ``root`` (one per sub-directory), rescanned when older than ``ttl`` seconds."""

    def __init__(self, root, cache, ttl=None):
        self.root = Path(root).resolve() if root else None
        self.cache = Path(cache)
        self.ttl = float(os.getenv('RESULTS_RESCAN_S', '15')) if ttl is None else ttl
        self.results, self.rejected = {}, {}
        self._seen = {}
        self._scanned = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return self.root is not None

    def scan(self, force=False):
        if not self.enabled or (not force and time.time() - self._scanned < self.ttl):
            return
        with self._lock:
            if not force and time.time() - self._scanned < self.ttl:
                return
            found, rejected, seen = {}, {}, {}
            for manifest in sorted(self.root.glob('*/result.json')) if self.root.is_dir() else []:
                name = manifest.parent.name
                try:
                    version = manifest.stat().st_mtime_ns
                    known = self._seen.get(name)
                    result = known[1] if known and known[0] == version else Result(manifest.parent, self.cache)
                except BundleError as error:
                    rejected[name] = str(error)
                    continue
                except OSError as error:
                    rejected[name] = f'not readable: {error.strerror or error}'
                    continue
                seen[name] = (version, result)
                other = found.get(result.id)
                if other is None or other.version < result.version:
                    if other is not None:
                        rejected[other.dir.name] = f'duplicate result_id {result.id}; the newer bundle is served'
                    found[result.id] = result
                else:
                    rejected[name] = f'duplicate result_id {result.id}; the newer bundle is served'
            self.results, self.rejected, self._seen = found, rejected, seen
            self._scanned = time.time()

    def get(self, result_id):
        self.scan()
        return self.results.get(result_id)

    def all(self):
        self.scan()
        return sorted(self.results.values(), key=lambda r: (r.valid_to, r.version), reverse=True)

    def latest(self):
        ranked = self.all()
        return ranked[0] if ranked else None
