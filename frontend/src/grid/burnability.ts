import Extent from '@arcgis/core/geometry/Extent.js';
import Point from '@arcgis/core/geometry/Point.js';
import Polygon from '@arcgis/core/geometry/Polygon.js';
import Polyline from '@arcgis/core/geometry/Polyline.js';
import SpatialReference from '@arcgis/core/geometry/SpatialReference.js';
import Graphic from '@arcgis/core/Graphic.js';
import GraphicsLayer from '@arcgis/core/layers/GraphicsLayer.js';
import TileInfo from '@arcgis/core/layers/support/TileInfo.js';
import WebTileLayer from '@arcgis/core/layers/WebTileLayer.js';
import SimpleFillSymbol from '@arcgis/core/symbols/SimpleFillSymbol.js';
import SimpleLineSymbol from '@arcgis/core/symbols/SimpleLineSymbol.js';
import SimpleMarkerSymbol from '@arcgis/core/symbols/SimpleMarkerSymbol.js';
import { lonLatToMercator } from './geo';
import type { Fmt } from './units';
import { DEMO } from '../demo';
import { API_BASE, API_ORIGIN } from './api';

/** Layer ids come from the result's metadata (they differ between the demo and a model result). */
export type BurnabilityLayerId = string;

export interface BurnabilityLayerMeta {
  id: BurnabilityLayerId;
  title: string;
  units: string;
  kind: string;
  range: [number, number];
  legend: { stops: Array<[number, string]> };
  /** True for class layers: draw swatches, not a ramp. */
  discrete?: boolean;
  /** True for compass directions: the ramp runs north, east, south, west and back to north. */
  cyclic?: boolean;
  /** For a ratio (units `x`): the value at which nothing changes, marked on the ramp. */
  neutral?: number | null;
}

export interface BurnabilityMetadata {
  schema_version: string;
  result_id: string;
  synthetic: boolean;
  research_only: boolean;
  as_of: string;
  mode: string;
  bbox_wgs84: [number, number, number, number];
  layers: BurnabilityLayerMeta[];
  notices: string[];
  // Present on a model result only.
  valid_from?: string;
  model_version?: string;
  /** Share of cells that received the NISAR SME2 soil-moisture term. */
  nisar_share?: number;
  /** What the risk rank is taken among: past ignitions, on open land when a built-up limit is given. */
  rank_reference?: { ignitions?: number; built_up_share_at_most?: number } | null;
  /**
   * Weather inputs at their medians over the training ignitions. When present, `susceptibility_index` is the rank
   * on that ordinary day (what the land contributes); without it the layer is a copy of the risk rank.
   */
  ordinary_day?: Record<string, number> | null;
  vector_layers?: { id: string; feature_count: number; synthetic: boolean }[];
  /** Present when the result can grow a fire from a point (`GET .../spread`). */
  capabilities?: {
    spread?: boolean;
    spread_map_day_wind?: boolean;
    /** The same fire can be grown on the fuel map alone, without NISAR's GCOV fuel state (`fuel_state=false`). */
    spread_fuel_state?: boolean;
  };
  extra?: {
    /** Per input column fed by NISAR SME2. */
    nisar?: Record<string, { scenes?: number; share?: number }>;
    fuel_age?: { gcov?: { scenes?: number } };
  };
}

export interface BurnabilityPointResult {
  valid: boolean;
  synthetic: boolean;
  values: Record<string, number | null>;
  units?: Record<string, string>;
  sampled_cell_center: { lon: number; lat: number } | null;
  reason: string | null;
}

const BURNABILITY_API_BASE = API_BASE;

/**
 * `latest` is the newest model result of the backend, or its synthetic demo when it has none.
 * Set VITE_BURNABILITY_RESULT to pin one result (e.g. `forest-demo-v1`).
 */
export const RESULT_ID = DEMO?.result || (import.meta.env.VITE_BURNABILITY_RESULT as string | undefined) || 'latest';
/** The synthetic demo: what a backend without the `latest` alias (before `feat/real-results`) still serves. */
const DEMO_RESULT_ID = 'forest-demo-v1';

/** Layers that exist because of NISAR (SME2 soil moisture, GCOV backscatter), listed first in the picker. */
const NISAR_LAYERS = [
  'dryness_anomaly',
  'nisar_term',
  'fuel_state',
  'risk_index_nisar_minus_smap',
  'lfmc_nisar_minus_smap',
  // experimental: the SME2 anomaly of a cell against its own record, and the risk scaled by it
  'sme2_anomaly',
  'risk_index_sme2',
  'p_large_fire_sme2',
  'risk_index_sme2_minus_main',
];
const RISK_LAYERS = ['risk_index', 'risk_class', 'p_large_fire', 'susceptibility_index', 'weather_factor', 'risk_index_smap'];
const SPREAD_LAYERS = [
  'rate_of_spread',
  'flame_length',
  'fireline_intensity',
  'spread_direction',
  'spread_permeability',
  'years_since_fire',
];

const LAYER_HINTS: Record<string, string> = {
  risk_index: 'Rank of large-fire probability',
  risk_class: 'Risk rank in five classes',
  p_large_fire: 'P(fire of {large} or more | ignition)',
  risk_index_smap: 'Risk rank without NISAR',
  susceptibility_index: 'Risk rank',
  weather_factor: 'Odds today divided by odds on an ordinary day',
  dryness_anomaly: 'Soil vs its own last 12 months (SMAP + NISAR SME2)',
  nisar_term: 'NISAR SME2 fine-scale soil moisture term',
  fuel_state: 'NISAR GCOV L-band regrowth after fire, 1 = mature',
  risk_index_nisar_minus_smap: 'What NISAR changes in the risk rank',
  lfmc_nisar_minus_smap: 'What NISAR changes in fuel moisture',
  sme2_anomaly: 'Experimental: NISAR SME2 soil vs the cell\'s own record, negative = drier',
  risk_index_sme2: 'Experimental: risk rank scaled by the NISAR SME2 anomaly',
  p_large_fire_sme2: 'Experimental: P(fire of {large} or more | ignition) scaled by the NISAR SME2 anomaly',
  risk_index_sme2_minus_main: 'Experimental: what the NISAR SME2 anomaly changes in the risk rank',
  lfmc: 'Live fuel moisture',
  lfmc_p05: 'Live fuel moisture, 5 % bound',
  lfmc_p95: 'Live fuel moisture, 95 % bound',
  soil_moisture: 'Surface soil moisture (SMAP + NISAR)',
  rate_of_spread: 'Head-fire spread at the map day\'s weather',
  flame_length: 'Head-fire flame length',
  fireline_intensity: 'Head-fire intensity',
  spread_direction: 'Direction the head fire runs toward',
  spread_permeability: 'Chance to burn when a large fire arrives',
  years_since_fire: 'Years since the last fire, 10 = none',
};

/** One line under the legend. The risk rank says what it is ranked among when the result tells. */
export function layerHint(id: BurnabilityLayerId, meta: BurnabilityMetadata | null | undefined, fmt: Fmt): string {
  const ref = meta?.rank_reference;
  if ((id === 'risk_index' || id === 'risk_class') && ref?.ignitions) {
    const among = `${ref.ignitions.toLocaleString('en-US')} past ignitions${
      ref.built_up_share_at_most != null && ref.built_up_share_at_most < 1 ? ' on open land' : ''
    }`;
    return id === 'risk_index' ? `Rank among ${among}` : `Five classes of the rank among ${among}`;
  }
  if (id === 'susceptibility_index' && meta?.ordinary_day) {
    return 'Rank with the weather of an ordinary ignition day: what the land contributes';
  }
  // The model's large-fire threshold is 400 ha.
  return (LAYER_HINTS[id] ?? '').replace('{large}', fmt.ha(400));
}

/** Layers of a result in picker groups; empty groups are dropped. Inputs of the fire-growth simulation are not maps. */
export function groupLayers(all: BurnabilityLayerMeta[]): { label: string; layers: BurnabilityLayerMeta[] }[] {
  const layers = all.filter((l) => l.kind !== 'component');
  const pick = (ids: string[]) => ids.flatMap((id) => layers.filter((l) => l.id === id));
  const known = new Set([...RISK_LAYERS, ...NISAR_LAYERS, ...SPREAD_LAYERS]);
  return [
    { label: 'Risk', layers: pick(RISK_LAYERS) },
    { label: 'NISAR', layers: pick(NISAR_LAYERS) },
    { label: 'Spread', layers: pick(SPREAD_LAYERS) },
    { label: 'Fuel', layers: layers.filter((l) => !known.has(l.id)) },
  ].filter((g) => g.layers.length > 0);
}

/** Preferred first layer: the model's risk rank, else the demo's susceptibility, else whatever exists. */
export function defaultLayerId(meta: BurnabilityMetadata): BurnabilityLayerId {
  const has = (id: string) => meta.layers.some((l) => l.id === id);
  return has('risk_index') ? 'risk_index' : has('susceptibility_index') ? 'susceptibility_index' : (meta.layers[0]?.id ?? '');
}

/** NISAR scenes behind a model result: SME2 (soil moisture) and GCOV (backscatter). */
export function nisarScenes(meta: BurnabilityMetadata): { sme2: number; gcov: number } {
  const sme2 = Math.max(0, ...Object.values(meta.extra?.nisar ?? {}).map((v) => v.scenes ?? 0));
  return { sme2, gcov: meta.extra?.fuel_age?.gcov?.scenes ?? 0 };
}

export function createBurnabilityVectorsLayer(): GraphicsLayer {
  return new GraphicsLayer({ id: 'burnability-vectors', visible: false });
}

export function getResultExtentMercator(meta: BurnabilityMetadata): Extent {
  const [xmin, ymin] = lonLatToMercator(meta.bbox_wgs84[0], meta.bbox_wgs84[1]);
  const [xmax, ymax] = lonLatToMercator(meta.bbox_wgs84[2], meta.bbox_wgs84[3]);
  return new Extent({
    xmin,
    ymin,
    xmax,
    ymax,
    spatialReference: SpatialReference.WebMercator,
  });
}

export async function fetchBurnabilityMetadata(
  signal?: AbortSignal,
): Promise<BurnabilityMetadata | null> {
  for (const id of RESULT_ID === DEMO_RESULT_ID ? [RESULT_ID] : [RESULT_ID, DEMO_RESULT_ID]) {
    try {
      const res = await fetch(`${BURNABILITY_API_BASE}/v1/burnability/results/${id}`, { signal });
      if (res.ok) return (await res.json()) as BurnabilityMetadata;
    } catch {
      return null;
    }
  }
  return null;
}

export function createBurnabilityRasterLayer(
  meta: BurnabilityMetadata,
  layerId: BurnabilityLayerId,
  opacity = 0.65,
): WebTileLayer {
  const title = meta.layers.find((l) => l.id === layerId)?.title ?? layerId;
  return new WebTileLayer({
    id: 'burnability-raster-overlay',
    title: `NASA-ACEs: ${title}${meta.synthetic ? ' (SYNTHETIC DEMO)' : ''}`,
    // The concrete result id, not the `latest` alias: tiles are cached per result.
    urlTemplate: `${API_ORIGIN}/v1/burnability/results/${meta.result_id}/tiles/${layerId}/{level}/{col}/{row}.png`,
    tileInfo: TileInfo.create({ numLODs: 19 }),
    // In the tiles' own coordinates: an extent in degrees is compared with tile bounds in metres, and no tile is asked for.
    fullExtent: getResultExtentMercator(meta),
    opacity,
  });
}

export async function fetchBurnabilityPoint(
  meta: BurnabilityMetadata,
  lon: number,
  lat: number,
  signal?: AbortSignal,
): Promise<BurnabilityPointResult | null> {
  try {
    const url = `${BURNABILITY_API_BASE}/v1/burnability/results/${meta.result_id}/point?lon=${lon.toFixed(5)}&lat=${lat.toFixed(5)}`;
    const res = await fetch(url, { signal });
    if (!res.ok) return null;
    return (await res.json()) as BurnabilityPointResult;
  } catch {
    return null;
  }
}

interface VectorProps {
  // model result: the head fire's one-hour run
  rate_of_spread_m_min?: number;
  direction_deg?: number;
  flame_length_m?: number;
  // synthetic demo
  bearing_deg?: number;
}

interface VectorCollection {
  features?: Array<{ geometry?: { coordinates?: number[][] }; properties?: VectorProps }>;
}

/** Arrows asked for per view: the backend returns at most this many, on a lattice of the result's grid. */
const VIEW_VECTORS = 1500;
/** Results whose backend has no arrows-for-a-view endpoint (older than `GET .../vectors?bbox=`). */
const noViewVectors = new Set<string>();

/** True when the arrows are sampled for the map view and have to be asked for again after a pan or zoom. */
export function vectorsFollowView(meta: BurnabilityMetadata): boolean {
  return !meta.synthetic && !noViewVectors.has(meta.result_id);
}

/** Arrows of a model result inside the view; null when the backend cannot make them (then the bundle's file is used). */
async function fetchViewVectors(
  meta: BurnabilityMetadata,
  view: [number, number, number, number],
  signal?: AbortSignal,
): Promise<VectorCollection | null> {
  const bbox = [
    Math.max(-180, view[0]),
    Math.max(-85, view[1]),
    Math.min(180, view[2]),
    Math.min(85, view[3]),
  ];
  if (!(bbox[0] < bbox[2] && bbox[1] < bbox[3])) return { features: [] };
  const url = `${BURNABILITY_API_BASE}/v1/burnability/results/${meta.result_id}/vectors?bbox=${bbox.map((v) => v.toFixed(5)).join(',')}&n=${VIEW_VECTORS}&hours=1`;
  const res = await fetch(url, { signal });
  if (res.status === 404) noViewVectors.add(meta.result_id);
  return res.ok ? ((await res.json()) as VectorCollection) : null;
}

/**
 * Draws the result's arrows. `view` is the visible box (west, south, east, north in degrees): a model result is
 * sampled for it, so the number of arrows on screen stays about the same at every zoom.
 */
export async function loadBurnabilityVectors(
  burnabilityVectorsLayer: GraphicsLayer,
  meta: BurnabilityMetadata,
  zoom: number,
  fmt: Fmt,
  signal?: AbortSignal,
  view?: [number, number, number, number],
): Promise<number> {
  if (burnabilityVectorsLayer.destroyed) return 0;
  try {
    let fc = vectorsFollowView(meta) ? await fetchViewVectors(meta, view ?? meta.bbox_wgs84, signal) : null;
    if (!fc) {
      const clampedZoom = Math.max(0, Math.min(18, zoom));
      const vectorId = meta.vector_layers?.[0]?.id ?? 'illustrative_pathways';
      const url = `${BURNABILITY_API_BASE}/v1/burnability/results/${meta.result_id}/vectors/${vectorId}?bbox=${meta.bbox_wgs84.join(',')}&zoom=${clampedZoom.toFixed(1)}&limit=1000`;
      const res = await fetch(url, { signal });
      if (!res.ok) return 0;
      fc = (await res.json()) as VectorCollection;
    }
    const graphics: Graphic[] = [];
    for (const f of fc.features ?? []) {
      const coords = f.geometry?.coordinates;
      if (!coords || coords.length < 2) continue;
      const p = f.properties ?? {};
      const bearing = p.direction_deg ?? p.bearing_deg ?? 90;
      graphics.push(
        new Graphic({
          geometry: new Polyline({
            paths: [coords],
            spatialReference: SpatialReference.WGS84,
          }),
          symbol: new SimpleLineSymbol({
            color: [14, 165, 233, 0.72],
            width: 1.5,
          }),
          attributes: meta.synthetic
            ? { note: 'Synthetic illustrative pathway; not forecast', bearing: Math.round(bearing) }
            : {
                note: 'Head-fire potential at the map day\'s weather; not a forecast',
                bearing: Math.round(bearing),
                ros: fmt.rate(p.rate_of_spread_m_min),
                flame: fmt.short(p.flame_length_m),
              },
          popupTemplate: meta.synthetic
            ? { title: 'Illustrative Pathway ({bearing}°)', content: '{note}' }
            : {
                title: 'One-hour head-fire run ({bearing}°)',
                content: '{ros}, flame {flame}<br>{note}',
              },
        }),
      );
      const end = coords[coords.length - 1];
      graphics.push(
        new Graphic({
          geometry: new Point({
            longitude: end[0],
            latitude: end[1],
            spatialReference: SpatialReference.WGS84,
          }),
          symbol: new SimpleMarkerSymbol({
            style: 'triangle',
            size: 6,
            color: [56, 189, 248, 0.9],
            angle: bearing,
            outline: { color: [15, 23, 42, 0.8], width: 0.5 },
          }),
        }),
      );
    }
    // Replaced in one step, after the answer: the arrows of the last view stay on screen while the next ones load.
    if (!burnabilityVectorsLayer.destroyed && !signal?.aborted) {
      burnabilityVectorsLayer.removeAll();
      burnabilityVectorsLayer.addMany(graphics);
    }
    return fc.features?.length ?? 0;
  } catch {
    return 0;
  }
}

/** Hours after ignition the fire-growth tool asks for and draws. */
export const GROWTH_HOURS = [1, 3, 6];
/** Shortest run first: the app's large / medium / small fire colours. */
const GROWTH_RGB: [number, number, number][] = [
  [220, 38, 38],
  [249, 115, 22],
  [250, 204, 21],
];
const growthRgb = (hours: number) => GROWTH_RGB[Math.min(GROWTH_RGB.length - 1, Math.max(0, GROWTH_HOURS.indexOf(hours)))];
export const growthColor = (hours: number): string => `rgb(${growthRgb(hours).join(' ')})`;

/** Answer of `GET .../spread`: where a fire started at a point would be after each of the hours. */
export interface FireGrowth {
  features: Array<{
    geometry: { type: string; coordinates: number[][][][] } | null;
    properties: { hours: number; area_ha: number };
  }>;
  properties: {
    wind_10m_m_s: number | null;
    wind_from_deg: number | null;
    wind_source: 'map_day' | 'request';
    head_rate_m_min: number | null;
    head_direction_deg: number | null;
    flame_length_m: number | null;
    fuel_age_permeability: boolean;
    /** False: grown on the fuel map alone, without NISAR's fuel state. */
    nisar_fuel_state?: boolean;
    /** The fire reached the edge of the simulated window: the largest area is a lower bound. */
    edge_reached: boolean;
  };
}

export interface FireGrowthOptions {
  /** m/s at 10 m and the direction it blows from; null = the wind of the map day. */
  wind: { speed: number; from: number } | null;
  fuelAge: boolean;
  /** False: the fuel map alone, without NISAR's GCOV fuel state (where the result has `spread_fuel_state`). */
  fuelState?: boolean;
}

export function createFireGrowthLayer(): GraphicsLayer {
  return new GraphicsLayer({ id: 'fire-growth', visible: false });
}

/** A `growth` on success, else the backend's reason (no fuel at the point, outside the result, ...). */
export async function fetchFireGrowth(
  meta: BurnabilityMetadata,
  lon: number,
  lat: number,
  options: FireGrowthOptions,
  signal?: AbortSignal,
): Promise<{ growth: FireGrowth | null; error: string | null }> {
  const wind = options.wind ? `&wind_speed=${options.wind.speed}&wind_from=${options.wind.from}` : '';
  const url = `${BURNABILITY_API_BASE}/v1/burnability/results/${meta.result_id}/spread?lon=${lon.toFixed(5)}&lat=${lat.toFixed(5)}&hours=${GROWTH_HOURS.join(',')}${wind}${options.fuelAge ? '' : '&fuel_age=false'}${options.fuelState === false ? '&fuel_state=false' : ''}`;
  try {
    const res = await fetch(url, { signal });
    const body = (await res.json()) as FireGrowth & { detail?: unknown };
    if (res.ok) return { growth: body, error: null };
    return { growth: null, error: typeof body.detail === 'string' ? body.detail : `Fire growth failed (${res.status})` };
  } catch {
    return { growth: null, error: signal?.aborted ? null : 'Fire growth is not available' };
  }
}

/** ArcGIS wants outer rings clockwise and holes counter-clockwise; GeoJSON does not promise either. */
function arcgisRings(polygons: number[][][][]): number[][][] {
  const clockwise = (ring: number[][]) => {
    let twice = 0;
    for (let i = 0; i < ring.length - 1; i += 1) twice += (ring[i + 1][0] - ring[i][0]) * (ring[i + 1][1] + ring[i][1]);
    return twice > 0;
  };
  return polygons.flatMap((polygon) =>
    polygon.map((ring, i) => (clockwise(ring) === (i === 0) ? ring : [...ring].reverse())),
  );
}

/** Isochrones (longest run at the bottom) and the ignition point; clears the layer when there is nothing to show. */
/** Colour of the dashed outline: the other estimate of the same fire (with or without NISAR's fuel state). */
export const GROWTH_ALONE_COLOR = '#fafafa';

/** `growth` is drawn as isochrones; `other`, the second estimate of the same fire, as one dashed outline. */
export function drawFireGrowth(
  layer: GraphicsLayer,
  growth: FireGrowth | null,
  start: { lon: number; lat: number } | null,
  fmt: Fmt,
  other: FireGrowth | null = null,
  lines: number[][][] | null = null,
): void {
  if (layer.destroyed) return;
  layer.removeAll();
  const graphics: Graphic[] = [];
  const lineGraphics: Graphic[] = [];
  // Transmission lines near the fire (paths in Web Mercator), plain and readable, and the part of them inside the
  // area this fire burns by its last hour, red as a line to shut off.
  const lastOwn = [...(growth?.features ?? [])].sort((a, b) => b.properties.hours - a.properties.hours)[0];
  if (lines?.length && lastOwn?.geometry?.coordinates?.length) {
    const rings = lastOwn.geometry.coordinates.flatMap((polygon) => polygon.map((ring) => ring.map(([lon, lat]) => lonLatToMercator(lon, lat))));
    const xs = rings.flat().map((p) => p[0]);
    const ys = rings.flat().map((p) => p[1]);
    const box = { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) };
    const pad = 4_000;
    const near = lines.filter((path) =>
      path.some(([x, y]) => x > box.x0 - pad && x < box.x1 + pad && y > box.y0 - pad && y < box.y1 + pad),
    );
    const inside = (x: number, y: number) => {
      if (x < box.x0 || x > box.x1 || y < box.y0 || y > box.y1) return false;
      let odd = false;
      for (const ring of rings)
        for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
          const [xi, yi] = ring[i];
          const [xj, yj] = ring[j];
          if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) odd = !odd;
        }
      return odd;
    };
    const reached: number[][][] = [];
    for (const path of near) {
      let run: number[][] = [];
      for (let i = 1; i < path.length; i++) {
        const [ax, ay] = path[i - 1];
        const [bx, by] = path[i];
        const pieces = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / 12));
        for (let k = 0; k < pieces; k++) {
          const p0 = [ax + ((bx - ax) * k) / pieces, ay + ((by - ay) * k) / pieces];
          const p1 = [ax + ((bx - ax) * (k + 1)) / pieces, ay + ((by - ay) * (k + 1)) / pieces];
          if (inside((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)) {
            if (!run.length) run.push(p0);
            run.push(p1);
          } else if (run.length) {
            reached.push(run);
            run = [];
          }
        }
      }
      if (run.length) reached.push(run);
    }
    const wire = (paths: number[][][], color: number[], width: number, casing: number) =>
      paths.length
        ? [
            new Graphic({
              geometry: new Polyline({ paths, spatialReference: SpatialReference.WebMercator }),
              symbol: new SimpleLineSymbol({ color: [9, 9, 11, 0.85], width: width + casing, cap: 'round', join: 'round' }),
            }),
            new Graphic({
              geometry: new Polyline({ paths, spatialReference: SpatialReference.WebMercator }),
              symbol: new SimpleLineSymbol({ color, width, cap: 'round', join: 'round' }),
            }),
          ]
        : [];
    lineGraphics.push(...wire(near, [236, 241, 250, 1], 2.2, 2.6), ...wire(reached, [239, 68, 68, 1], 4, 2.6));
  }
  // A dark line under an outline keeps it readable on a bright layer (a yellow outline on a yellow ramp).
  const casing = (rings: number[][][], width: number) =>
    new Graphic({
      geometry: new Polygon({ rings, spatialReference: SpatialReference.WGS84 }),
      symbol: new SimpleFillSymbol({ color: [0, 0, 0, 0], outline: { color: [9, 9, 11, 0.55], width } }),
    });
  // The comparison: how far the same fire gets after the last hour by the other estimate, as a dashed outline.
  const last = [...(other?.features ?? [])].sort((a, b) => b.properties.hours - a.properties.hours)[0];
  if (last?.geometry?.coordinates?.length) {
    const rings = arcgisRings(last.geometry.coordinates);
    graphics.push(
      casing(rings, 3.2),
      new Graphic({
        geometry: new Polygon({ rings, spatialReference: SpatialReference.WGS84 }),
        symbol: new SimpleFillSymbol({
          color: [0, 0, 0, 0],
          outline: { color: [250, 250, 250, 1], width: 1.5, style: 'dash' },
        }),
        attributes: {
          hours: last.properties.hours,
          area: fmt.ha(last.properties.area_ha),
          which: other?.properties.nisar_fuel_state === false ? 'Fuel map alone' : 'With NISAR’s fuel state',
        },
        popupTemplate: { title: '{which}, after {hours} h', content: '{area}, the other estimate of the same fire' },
      }),
    );
  }
  for (const f of [...(growth?.features ?? [])].sort((a, b) => b.properties.hours - a.properties.hours)) {
    if (!f.geometry?.coordinates?.length) continue;
    const [r, g, b] = growthRgb(f.properties.hours);
    const rings = arcgisRings(f.geometry.coordinates);
    graphics.push(
      casing(rings, 3.4),
      new Graphic({
        geometry: new Polygon({ rings, spatialReference: SpatialReference.WGS84 }),
        symbol: new SimpleFillSymbol({ color: [r, g, b, 0.3], outline: { color: [r, g, b, 0.95], width: 1.6 } }),
        attributes: { hours: f.properties.hours, area: fmt.ha(f.properties.area_ha) },
        popupTemplate: {
          title: 'Fire growth after {hours} h',
          content: '{area}, surface-fire potential, not a forecast',
        },
      }),
    );
  }
  graphics.push(...lineGraphics); // over the zones, so that the line and its reached part stay in sight
  if (start) {
    graphics.push(
      new Graphic({
        geometry: new Point({ longitude: start.lon, latitude: start.lat, spatialReference: SpatialReference.WGS84 }),
        symbol: new SimpleMarkerSymbol({
          style: 'x',
          size: 9,
          color: [255, 255, 255, 1],
          outline: { color: [255, 255, 255, 1], width: 2 },
        }),
      }),
    );
  }
  layer.addMany(graphics);
}

