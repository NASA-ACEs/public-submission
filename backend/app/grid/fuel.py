"""Fuel risk from a real model result (a machine-learning bundle) at fires and along lines.

Off unless ``FUEL_RISK_RESULT`` is set. The layer is the result's ``assessment_layer`` (``risk_index``: the rank,
0-1, of P(large fire | ignition) among historical ignition-day predictions). It says how readily a fire would grow
at that place on the map's day; it is not a forecast of this fire.
"""
import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.windows import Window

from ..results import SpreadError
from .geo import cos_lat, merc_to_lonlat

STEP_M = 200            # ground spacing of samples along a line (the statewide map has 200 m cells)
FIRE_RADIUS_M = 1000    # fuel at a fire: mean over the centre and two rings out to this distance
CHUNK = 1024            # raster cells per side of one read; keeps memory flat for scattered samples
DEMO_THRESHOLD = 0.25   # the cut the heuristics in spread.py / threats.py were written for (demo score)
BOOST = (0.8, 0.6)      # spread multiplier = 0.8 + 0.6 * risk rank, so 0.8 .. 1.4
GROW_HOURS = (1, 3, 6)  # isochrones of the fire-growth simulation added to a fire (FUEL_RISK_DRIVES=simulate)
MPH = 0.44704           # m/s per mph


def densify(path, step_m=STEP_M):
    """Midpoints (n, 2, Web Mercator) and ground lengths (n, metres) of pieces of ``path`` no longer than ``step_m``."""
    pts = np.asarray(path, dtype='float64')
    if len(pts) < 2:
        return np.zeros((0, 2)), np.zeros(0)
    a, d = pts[:-1], np.diff(pts, axis=0)
    ground = np.hypot(d[:, 0], d[:, 1]) * cos_lat(a[:, 1] + d[:, 1] / 2)
    counts = np.maximum(1, np.ceil(ground / step_m)).astype(int)
    seg = np.repeat(np.arange(len(a)), counts)
    k = np.concatenate([np.arange(c) for c in counts])
    return a[seg] + d[seg] * ((k + 0.5) / counts[seg])[:, None], (ground / counts)[seg]


class FuelRisk:
    def __init__(self, result):
        self.id, self.key = result.id, (result.id, result.version)
        self.result = result
        self.layer = result.assessment_layer
        spec = result.layers[self.layer]
        self.path = spec['path']
        self.threshold = result.threshold
        self.breaks = result.class_breaks or []
        self.manifest = result.manifest
        self._project = Transformer.from_crs('EPSG:3857', spec['crs'], always_xy=True)

    def sample(self, xy):
        """Layer value at Web Mercator points (n, 2); NaN outside the map or on nodata."""
        xy = np.asarray(xy, dtype='float64').reshape(-1, 2)
        out = np.full(len(xy), np.nan)
        if not len(xy):
            return out
        x, y = self._project.transform(xy[:, 0], xy[:, 1])
        with rasterio.open(self.path) as ds:
            inv = ~ds.transform
            with np.errstate(invalid='ignore'):
                col, row = np.floor(inv.a * x + inv.b * y + inv.c), np.floor(inv.d * x + inv.e * y + inv.f)
                ok = np.isfinite(col) & np.isfinite(row) & (row >= 0) & (row < ds.height) & (col >= 0) & (col < ds.width)
            idx = np.flatnonzero(ok)
            r, c = row[idx].astype(int), col[idx].astype(int)
            block = (r // CHUNK) * (ds.width // CHUNK + 1) + c // CHUNK
            for b in np.unique(block):
                m = block == b
                r0, c0 = r[m].min(), c[m].min()
                a = ds.read(1, window=Window(c0, r0, c[m].max() - c0 + 1, r[m].max() - r0 + 1), masked=True)
                v = a[r[m] - r0, c[m] - c0]
                out[idx[m]] = np.where(np.ma.getmaskarray(v), np.nan, np.ma.getdata(v).astype('float64'))
        return out

    def risk_class(self, value):
        if value is None or not np.isfinite(value) or not self.breaks:
            return None
        return int(np.searchsorted(self.breaks, value, side='right') + 1)

    def at_fire(self, cx, cy):
        """Mean risk rank around a fire centroid (Web Mercator), or None without coverage."""
        r = FIRE_RADIUS_M / float(cos_lat(cy))
        angles = np.linspace(0, 2 * np.pi, 8, endpoint=False)
        ring = np.stack([np.cos(angles), np.sin(angles)], axis=1)
        values = self.sample(np.concatenate([[[cx, cy]], [cx, cy] + ring * r / 2, [cx, cy] + ring * r]))
        return float(np.nanmean(values)) if np.isfinite(values).any() else None

    def boost(self, rank):
        """Spread-run multiplier for a fire with this mean risk rank; None (keep the default) without coverage."""
        return None if rank is None else BOOST[0] + BOOST[1] * rank

    def grow(self, cx, cy, wind_mph, spread_bearing_deg):
        """Fire growth from a fire's centroid (Web Mercator) for the live wind: the answer of the result's
        ``/spread`` endpoint, or None (the result has no component layers, the fire is off the map or on no fuel)."""
        if not getattr(self.result, 'spread_ready', False):
            return None
        lon, lat = merc_to_lonlat(cx, cy)
        try:
            return self.result.spread(lon, lat, GROW_HOURS, (min(max(wind_mph, 0) * MPH, 40.0), (spread_bearing_deg + 180) % 360))
        except SpreadError:
            return None

    def along(self, paths):
        """One summary per path (Web Mercator): length-weighted mean, max and km at or above the threshold."""
        pieces = [densify(p) for p in paths]
        if not pieces:
            return []
        values = self.sample(np.concatenate([m for m, _ in pieces]))
        out, start = [], 0
        for mids, lengths in pieces:
            v = values[start:start + len(mids)]
            start += len(mids)
            ok = np.isfinite(v)
            covered = float(lengths[ok].sum())
            out.append(dict(
                total_length_m=float(lengths.sum()), covered_length_m=covered,
                mean=float((v[ok] * lengths[ok]).sum() / covered) if covered else None,
                max=float(v[ok].max()) if covered else None,
                higher_length_m=float(lengths[ok & (v >= self.threshold)].sum()) if covered else 0.0))
        return out

    def lines(self, lines):
        """Summaries keyed by line ``oid``, in the fields and scale ``spread.py`` / ``threats.py`` expect.

        Their thresholds (0.25, 0.3) were written for the demo score, so the rank is scaled by
        ``DEMO_THRESHOLD / threshold``: a rank at this result's threshold counts as 0.25.
        """
        scale = DEMO_THRESHOLD / self.threshold if self.threshold else 1.0
        spans = [(line['oid'], len(line['paths'])) for line in lines]
        parts = iter(self.along([p for line in lines for p in line['paths']]))
        out = {}
        for oid, n in spans:
            group = [next(parts) for _ in range(n)]
            covered = sum(g['covered_length_m'] for g in group)
            if not covered:
                continue
            higher = sum(g['higher_length_m'] for g in group)
            mean = sum(g['mean'] * g['covered_length_m'] for g in group if g['mean'] is not None) / covered
            peak = max(g['max'] for g in group if g['max'] is not None)
            out[oid] = dict(mean_score_on_covered=mean * scale, max_score=peak * scale,
                            higher_fraction_of_covered=higher / covered, higher_length_m=higher,
                            covered_length_m=covered, rank_mean=mean, rank_max=peak)
        return out

    def describe(self, drives):
        m = self.manifest
        return dict(resultId=self.id, layer=self.layer, target=m.get('target'), validFrom=m.get('valid_from'),
                    validTo=m.get('valid_to'), modelVersion=m.get('model_version'), classBreaks=self.breaks,
                    threshold=self.threshold, drives=sorted(drives),
                    spreadMultiplier=f'{BOOST[0]} + {BOOST[1]} * mean risk rank within {FIRE_RADIUS_M} m of the fire'
                    if 'spread' in drives else None,
                    tierScale=f'risk rank * {DEMO_THRESHOLD} / {self.threshold} replaces the demo fuel score'
                    if 'tiers' in drives else None,
                    **({'simulated': f'spread[].simulated: fire growth after {GROW_HOURS} h from the fire centroid for the '
                                     'mean forecast wind (minimum travel time on the result\'s fuels); next to the '
                                     'ellipse, it does not change it or the tiers'} if 'simulate' in drives else {}))
