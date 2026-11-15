// Hourly spread envelopes (0..24 h) rebuilt from the backend's per-hour ellipse parameters.
// Mirrors backend app/grid/spread.py `_ellipse` (n = 72, closed ring of 73 points).
import type { Path, Situation, SpreadHourly } from './api';
import { WORLD_HALF } from './geo';

/** Forecast hours the user can toggle on the map (isochrones within the next 24 h). */
export const SPREAD_STEPS = [1, 3, 6, 12, 24] as const;
export const SPREAD_DEFAULT: number[] = [3, 6, 12, 24];

/**
 * One colour per isochrone, ordered as a hue walk (pink, violet, indigo, cyan, lime) so the sequence
 * reads as time. The hues avoid red/amber (SHUT OFF/PSPS), the wind blue and the forest greens; lightness
 * also steps, so they stay distinct for colour-blind viewers. Darker variants for the light street map.
 */
export const SPREAD_COLORS: Record<'satellite' | 'map', Record<number, [number, number, number]>> = {
  satellite: { 1: [244, 114, 182], 3: [192, 132, 252], 6: [129, 140, 248], 12: [34, 211, 238], 24: [190, 242, 100] },
  map: { 1: [219, 39, 119], 3: [147, 51, 234], 6: [79, 70, 229], 12: [8, 145, 178], 24: [77, 124, 15] },
};

export const spreadCss = (h: number, basemap: 'satellite' | 'map' = 'satellite') =>
  `rgb(${SPREAD_COLORS[basemap][h].join(', ')})`;

const N = 72;
const COS = new Float64Array(N + 1);
const SIN = new Float64Array(N + 1);
for (let i = 0; i <= N; i++) {
  const t = (2 * Math.PI * i) / N;
  COS[i] = Math.cos(t);
  SIN[i] = Math.sin(t);
}

/** Per-fire constants, computed once per situation. */
export interface SpreadFire {
  clusterId: string;
  cx: number;
  cy: number;
  /** Mercator metres per ground metre (1 / k). */
  inv: number;
  ux: number;
  uy: number;
  hourly: SpreadHourly;
}

const cache = new WeakMap<Situation, SpreadFire[]>();

/** Fires that carry hourly envelopes, with centroid, scale and wind direction precomputed. */
export function spreadFires(s: Situation): SpreadFire[] {
  let out = cache.get(s);
  if (out) return out;
  out = [];
  const centroids = new Map(s.fires.clusters.map((c) => [c.id, c.centroid]));
  for (const e of s.spread) {
    const c = centroids.get(e.clusterId);
    if (!c || !e.wind || !e.hourly?.a?.length) continue;
    const k = Math.max(0.3, 1 / Math.cosh((c[1] / WORLD_HALF) * Math.PI));
    const rad = ((e.hourly.brg ?? e.wind.bearingDeg) * Math.PI) / 180;
    out.push({ clusterId: e.clusterId, cx: c[0], cy: c[1], inv: 1 / k, ux: Math.sin(rad), uy: Math.cos(rad), hourly: e.hourly });
  }
  cache.set(s, out);
  return out;
}

/** Ellipse ring for forecast hour h (Web Mercator), or null when that hour is missing. */
export function spreadRing(f: SpreadFire, h: number): Path | null {
  const { a: A, b: B, off: O } = f.hourly;
  const a = A[h];
  const b = B[h];
  const off = O[h];
  if (a == null || b == null || off == null || a <= 0) return null;
  // px = cos β = uy, py = -sin β = -ux
  const { cx, cy, inv, ux, uy } = f;
  const ring: Path = new Array(N + 1);
  for (let i = 0; i <= N; i++) {
    const along = off + a * COS[i];
    const side = b * SIN[i];
    ring[i] = [cx + (ux * along + uy * side) * inv, cy + (uy * along - ux * side) * inv];
  }
  return ring;
}

/** Head point (furthest downwind) of hour h, Web Mercator. */
export function spreadHead(f: SpreadFire, h: number): [number, number] {
  const d = (f.hourly.off[h] + f.hourly.a[h]) * f.inv;
  return [f.cx + f.ux * d, f.cy + f.uy * d];
}
