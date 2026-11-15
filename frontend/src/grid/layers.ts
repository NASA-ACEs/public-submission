// Map layers. Pure rendering of backend responses; no analysis happens here.
import Graphic from '@arcgis/core/Graphic.js';
import Point from '@arcgis/core/geometry/Point.js';
import Polygon from '@arcgis/core/geometry/Polygon.js';
import Polyline from '@arcgis/core/geometry/Polyline.js';
import SpatialReference from '@arcgis/core/geometry/SpatialReference.js';
import FeatureLayer from '@arcgis/core/layers/FeatureLayer.js';
import GraphicsLayer from '@arcgis/core/layers/GraphicsLayer.js';
import SimpleRenderer from '@arcgis/core/renderers/SimpleRenderer.js';
import SimpleFillSymbol from '@arcgis/core/symbols/SimpleFillSymbol.js';
import SimpleLineSymbol from '@arcgis/core/symbols/SimpleLineSymbol.js';
import SimpleMarkerSymbol from '@arcgis/core/symbols/SimpleMarkerSymbol.js';
import TextSymbol from '@arcgis/core/symbols/TextSymbol.js';
import type { FireSize, GridLine, Path, Situation, StateSummary, ThreatLine } from './api';
import type { BasemapMode } from './geo';
import type { Fmt } from './units';
import { SPREAD_COLORS, spreadFires, spreadHead, spreadRing } from './spreadHours';

const WM = SpatialReference.WebMercator;
const FIRE: [number, number, number] = [239, 68, 68];

/** Fire size classes from the backend (cluster rank 3/2/1). */
export const FIRE_SIZE_STYLE: Record<FireSize, { rgb: [number, number, number]; dot: number; label: string }> = {
  large: { rgb: [220, 38, 38], dot: 8, label: 'Large fire' },
  medium: { rgb: [249, 115, 22], dot: 6, label: 'Medium fire' },
  small: { rgb: [250, 204, 21], dot: 4, label: 'Small fire' },
};
const RANK_SIZE: FireSize[] = ['small', 'small', 'medium', 'large'];

/** Ground metres per Web Mercator metre at northing y. */
function groundScale(y: number): number {
  return Math.max(0.3, 1 / Math.cosh((y / 20037508.342789244) * Math.PI));
}

export interface MapLayers {
  states: FeatureLayer;
  focus: GraphicsLayer;
  lineCasing: GraphicsLayer;
  lines: FeatureLayer;
  lineHover: GraphicsLayer;
  forestLines: GraphicsLayer;
  spread: GraphicsLayer;
  spreadLabels: GraphicsLayer;
  wind: GraphicsLayer;
  fireFocus: GraphicsLayer;
  fireLabels: GraphicsLayer;
  company: GraphicsLayer;
  perimeters: GraphicsLayer;
  fires: GraphicsLayer;
  hazards: GraphicsLayer;
}

export function createLayers(): MapLayers {
  return {
    states: new FeatureLayer({
      id: 'states',
      source: [],
      objectIdField: 'OID',
      geometryType: 'polygon',
      spatialReference: WM,
      fields: [
        { name: 'OID', type: 'oid' },
        { name: 'ABBR', type: 'string' },
      ],
      outFields: ['ABBR'],
      popupEnabled: false,
    }),
    focus: new GraphicsLayer({ id: 'state-focus' }),
    lineCasing: new GraphicsLayer({ id: 'line-casing', visible: false }),
    lines: new FeatureLayer({
      id: 'lines',
      source: [],
      objectIdField: 'OID',
      geometryType: 'polyline',
      spatialReference: WM,
      fields: [
        { name: 'OID', type: 'oid' },
        // applyEdits assigns its own OIDs, so the backend line id lives in LID.
        { name: 'LID', type: 'integer' },
        { name: 'OWNER', type: 'string' },
        { name: 'KV', type: 'double' },
        { name: 'SUB1', type: 'string' },
        { name: 'SUB2', type: 'string' },
        { name: 'KM', type: 'double' },
        { name: 'FKM', type: 'double' },
      ],
      outFields: ['*'],
      // Popups open from App (pickLine): hitTest misses these thin lines in 3D.
      popupEnabled: false,
      visible: false,
    }),
    lineHover: new GraphicsLayer({ id: 'line-hover' }),
    forestLines: new GraphicsLayer({ id: 'forest-lines', visible: false }),
    spread: new GraphicsLayer({ id: 'spread' }),
    // Hour labels on the forecast rings, only when zoomed in to a few fires.
    spreadLabels: new GraphicsLayer({ id: 'spread-labels', minScale: 600_000 }),
    wind: new GraphicsLayer({ id: 'wind' }),
    fireFocus: new GraphicsLayer({ id: 'fire-focus' }),
    // Acre labels only once zoomed in past state scale, so they never pile up.
    fireLabels: new GraphicsLayer({ id: 'fire-labels', minScale: 1_200_000 }),
    company: new GraphicsLayer({ id: 'company-focus' }),
    perimeters: new GraphicsLayer({ id: 'perimeters' }),
    fires: new GraphicsLayer({ id: 'fires' }),
    hazards: new GraphicsLayer({ id: 'hazards' }),
  };
}

export function orderedLayers(l: MapLayers) {
  return [l.states, l.focus, l.spread, l.perimeters, l.lineCasing, l.lines, l.forestLines, l.lineHover, l.hazards, l.wind, l.fireFocus, l.fires, l.company, l.spreadLabels, l.fireLabels];
}

/**
 * Grid line style per zoom tier. Far out the grid is a quiet texture; zoomed in it is the subject.
 * Light core + dark casing (dark core + light casing on the street map) keeps luminance contrast
 * on any imagery, and stays neutral so red/amber stay the only "danger" colours.
 */
type Rgba = [number, number, number, number];
const LINE_STYLE: Record<BasemapMode, { core: Rgba; coreW: number; casing: Rgba; casingW: number }[]> = {
  satellite: [
    { core: [244, 244, 245, 0.38], coreW: 1, casing: [9, 9, 11, 0], casingW: 0 },
    { core: [250, 250, 250, 0.8], coreW: 1.5, casing: [9, 9, 11, 0.5], casingW: 3.5 },
    { core: [255, 255, 255, 0.95], coreW: 2, casing: [9, 9, 11, 0.65], casingW: 4.5 },
  ],
  map: [
    { core: [63, 63, 70, 0.55], coreW: 1, casing: [255, 255, 255, 0], casingW: 0 },
    { core: [39, 39, 42, 0.85], coreW: 1.5, casing: [255, 255, 255, 0.7], casingW: 3.5 },
    { core: [24, 24, 27, 0.95], coreW: 2, casing: [255, 255, 255, 0.85], casingW: 4.5 },
  ],
};

/** Zoom tier from the view scale: 0 state-wide, 1 region, 2 local. */
export function lineZoomTier(scale: number): 0 | 1 | 2 {
  return scale > 1_500_000 ? 0 : scale > 400_000 ? 1 : 2;
}

let current = { basemap: 'satellite' as BasemapMode, tier: 0 as 0 | 1 | 2 };
const lineStyle = () => LINE_STYLE[current.basemap][current.tier];

export function applyAppearance(l: MapLayers, basemap: BasemapMode, tier: 0 | 1 | 2 = 0): void {
  current = { basemap, tier };
  const isMap = basemap === 'map';
  l.states.renderer = new SimpleRenderer({
    symbol: new SimpleFillSymbol({
      color: isMap ? [24, 24, 27, 0.03] : [9, 9, 11, 0.14],
      outline: { color: isMap ? [63, 63, 70, 0.45] : [244, 244, 245, 0.52], width: 1.1 },
    }),
  });
  const st = lineStyle();
  l.lines.renderer = new SimpleRenderer({
    symbol: new SimpleLineSymbol({ color: st.core, width: st.coreW, cap: 'round', join: 'round' }),
  });
  const casing = l.lineCasing.graphics.getItemAt(0);
  if (casing) casing.symbol = casingSymbol();
}

function casingSymbol(): SimpleLineSymbol {
  const st = lineStyle();
  return new SimpleLineSymbol({ color: st.casing, width: st.casingW, cap: 'round', join: 'round' });
}

/** Dark casing under the visible lines (owner filter applied), styled by the zoom tier. */
export function renderLineCasing(l: MapLayers, lines: GridLine[], owners: Set<string>): void {
  if (l.lineCasing.destroyed) return;
  l.lineCasing.removeAll();
  const paths: Path[] = [];
  for (const line of lines) if (owners.has(line.owner)) paths.push(...line.paths);
  if (paths.length === 0) return;
  l.lineCasing.add(new Graphic({ geometry: new Polyline({ paths, spatialReference: WM }), symbol: casingSymbol() }));
}

async function replaceFeatures(layer: FeatureLayer, graphics: Graphic[]): Promise<void> {
  if (layer.destroyed) return;
  const existing = await layer.queryFeatures();
  if (layer.destroyed) return;
  await layer.applyEdits({ deleteFeatures: existing.features, addFeatures: graphics });
}

/** A line as a popup feature; lengths are pre-formatted in the chosen units. */
export function lineGraphic(line: GridLine, fmt: Fmt): Graphic {
  return new Graphic({
    geometry: new Polyline({ paths: line.paths, spatialReference: WM }),
    attributes: { OWNER: line.owner, KV: line.kv, SUB1: line.sub1, SUB2: line.sub2, LEN: fmt.km(line.km), FLEN: fmt.km(line.fKm) },
    popupTemplate: {
      title: '{SUB1} to {SUB2}',
      content: [
        {
          type: 'fields',
          fieldInfos: [
            { fieldName: 'OWNER', label: 'Operator' },
            { fieldName: 'KV', label: 'kV' },
            { fieldName: 'LEN', label: 'Length' },
            { fieldName: 'FLEN', label: 'In forest' },
          ],
        },
      ],
    },
  });
}

export function setStates(l: MapLayers, states: StateSummary[]): Promise<void> {
  return replaceFeatures(
    l.states,
    states.map(
      (s, i) =>
        new Graphic({
          geometry: new Polygon({ rings: s.rings, spatialReference: WM }),
          attributes: { OID: i + 1, ABBR: s.abbr },
        }),
    ),
  );
}

export function renderFocus(
  l: MapLayers,
  states: StateSummary[],
  hovered: string | null,
  selected: string | null,
  basemap: BasemapMode,
): void {
  if (l.focus.destroyed) return;
  l.focus.removeAll();
  const isMap = basemap === 'map';
  const add = (abbr: string | null, fillAlpha: number, width: number) => {
    const st = abbr ? states.find((s) => s.abbr === abbr) : null;
    if (!st) return;
    l.focus.add(
      new Graphic({
        geometry: new Polygon({ rings: st.rings, spatialReference: WM }),
        symbol: new SimpleFillSymbol({
          color: isMap ? [24, 24, 27, fillAlpha] : [250, 250, 250, fillAlpha],
          outline: { color: isMap ? [24, 24, 27, 1] : [250, 250, 250, 1], width },
        }),
      }),
    );
  };
  if (hovered !== selected) add(hovered, 0.1, 1.8);
  add(selected, 0.18, 2.4);
}

/** Hovered transmission line: drawn above everything, one step bolder than the current grid style. */
export function renderLineHover(l: MapLayers, paths: Path[] | null, basemap: BasemapMode): void {
  l.lineHover.removeAll();
  if (!paths) return;
  const isMap = basemap === 'map';
  const st = lineStyle();
  const geometry = new Polyline({ paths, spatialReference: WM });
  l.lineHover.addMany([
    new Graphic({
      geometry,
      symbol: new SimpleLineSymbol({
        color: isMap ? [255, 255, 255, 0.9] : [9, 9, 11, 0.75],
        width: st.coreW + 4,
        cap: 'round',
        join: 'round',
      }),
    }),
    new Graphic({
      geometry,
      symbol: new SimpleLineSymbol({
        color: isMap ? [9, 9, 11, 1] : [255, 255, 255, 1],
        width: st.coreW + 1.5,
        cap: 'round',
        join: 'round',
      }),
    }),
  ]);
}

export function setLines(l: MapLayers, lines: GridLine[]): Promise<void> {
  l.lineHover.removeAll();
  return replaceFeatures(
    l.lines,
    lines.map(
      (line) =>
        new Graphic({
          geometry: new Polyline({ paths: line.paths, spatialReference: WM }),
          attributes: {
            LID: line.oid,
            OWNER: line.owner,
            KV: line.kv,
            SUB1: line.sub1,
            SUB2: line.sub2,
            KM: line.km,
            FKM: line.fKm,
          },
        }),
    ),
  );
}

export function filterOwners(l: MapLayers, owners: Set<string>, total: number): void {
  if (l.lines.destroyed) return;
  if (owners.size === 0) l.lines.definitionExpression = '1=0';
  else if (owners.size >= total) l.lines.definitionExpression = '';
  else
    l.lines.definitionExpression = `OWNER IN (${[...owners]
      .map((o) => `'${o.replace(/'/g, "''")}'`)
      .join(',')})`;
}

export function renderForestLines(
  l: MapLayers,
  lines: GridLine[],
  owners: Set<string>,
  color: string,
): void {
  if (l.forestLines.destroyed) return;
  l.forestLines.removeAll();
  const paths: Path[] = [];
  for (const line of lines) if (owners.has(line.owner)) paths.push(...line.fPaths);
  if (paths.length === 0) return;
  const geometry = new Polyline({ paths, spatialReference: WM });
  // Dark casing under a solid stroke so the colour reads on satellite and map alike.
  l.forestLines.addMany([
    new Graphic({
      geometry,
      symbol: new SimpleLineSymbol({ color: [0, 0, 0, 0.55], width: 4.2, cap: 'round', join: 'round' }),
    }),
    new Graphic({
      geometry,
      symbol: new SimpleLineSymbol({ color, width: 2.6, cap: 'round', join: 'round' }),
    }),
  ]);
}

export function clearSituation(l: MapLayers): void {
  for (const layer of [l.spread, l.spreadLabels, l.wind, l.fireFocus, l.fireLabels, l.company, l.perimeters, l.fires, l.hazards]) {
    if (!layer.destroyed) layer.removeAll();
  }
}

/**
 * Spread forecast (modelled, not observed): one wind-stretched ellipse per enabled hour, each in its own
 * colour, labelled at its head like a contour line ("6 h"). Rebuilds only the spread layers.
 */
export function renderSpread(l: MapLayers, s: Situation, basemap: BasemapMode, hours: readonly number[]): void {
  if (l.spread.destroyed) return;
  l.spread.removeAll();
  l.spreadLabels.removeAll();
  const isMap = basemap === 'map';
  const halo = isMap ? [255, 255, 255, 0.95] : [0, 0, 0, 0.8];
  const fires = spreadFires(s);
  const withHourly = new Set(fires.map((f) => f.clusterId));
  const sorted = [...hours].sort((a, b) => a - b);
  const outermost = sorted[sorted.length - 1];
  const graphics: Graphic[] = [];
  const labels: Graphic[] = [];

  for (const h of sorted) {
    const rgb = SPREAD_COLORS[basemap][h] ?? SPREAD_COLORS[basemap][24];
    const symbol = new SimpleFillSymbol({
      // Only the outermost ring gets a faint fill, so nested rings stay readable.
      color: h === outermost ? [...rgb, 0.07] : [0, 0, 0, 0],
      outline: { color: [...rgb, 0.95], width: 1.6, style: 'dash' },
    });
    const text = new TextSymbol({
      text: `${h} h`,
      color: [...rgb, 1],
      haloColor: halo,
      haloSize: 1.5,
      font: { size: 10, weight: 'bold' },
    });
    for (const f of fires) {
      const ring = spreadRing(f, h);
      if (!ring) continue;
      graphics.push(new Graphic({ geometry: new Polygon({ rings: [ring], spatialReference: WM }), symbol }));
      const [x, y] = spreadHead(f, h);
      labels.push(new Graphic({ geometry: new Point({ x, y, spatialReference: WM }), symbol: text }));
    }
  }

  // Fires without hourly data keep the single 24 h ring, shown when 24 h is on.
  if (sorted.includes(24)) {
    const rgb = SPREAD_COLORS[basemap][24];
    const fallback = new SimpleFillSymbol({
      color: [...rgb, 0.07],
      outline: { color: [...rgb, 0.95], width: 1.6, style: 'dash' },
    });
    for (const e of s.spread) {
      if (withHourly.has(e.clusterId)) continue;
      graphics.push(new Graphic({ geometry: new Polygon({ rings: [e.ring], spatialReference: WM }), symbol: fallback }));
    }
  }
  l.spread.addMany(graphics);
  l.spreadLabels.addMany(labels);
}

export function renderSituation(
  l: MapLayers,
  s: Situation,
  basemap: BasemapMode,
  hours: readonly number[],
  fmt: Fmt,
): void {
  clearSituation(l);
  if (l.fires.destroyed) return;
  const isMap = basemap === 'map';

  renderSpread(l, s, basemap, hours);

  // Wind arrows: from the fire toward where the wind pushes it, length = forecast head run.
  const centroids = new Map(s.fires.clusters.map((c) => [c.id, c.centroid]));
  const arrowHalo = isMap ? [255, 255, 255, 0.95] : [0, 0, 0, 0.7];
  const arrowColor = isMap ? [30, 64, 175, 1] : [191, 219, 254, 1];
  for (const e of s.spread) {
    const c = centroids.get(e.clusterId);
    if (!c || !e.wind || !e.headM || e.wind.mph < 1) continue;
    const rad = (e.wind.bearingDeg * Math.PI) / 180;
    const len = (e.headM * 0.85) / groundScale(c[1]);
    const tip = [c[0] + Math.sin(rad) * len, c[1] + Math.cos(rad) * len];
    const geometry = new Polyline({ paths: [[c, tip]], spatialReference: WM });
    const width = Math.min(3.2, 1.4 + e.wind.mph / 10);
    const marker = { style: 'arrow' as const, placement: 'end' as const };
    l.wind.addMany([
      new Graphic({
        geometry,
        symbol: new SimpleLineSymbol({ color: arrowHalo, width: width + 2.5, cap: 'round', marker: { ...marker, color: arrowHalo } }),
      }),
      new Graphic({
        geometry,
        symbol: new SimpleLineSymbol({ color: arrowColor, width, cap: 'round', marker: { ...marker, color: arrowColor } }),
      }),
    ]);
  }

  // Observed incident perimeters (NIFC).
  const perimeterSymbol = new SimpleFillSymbol({
    color: [...FIRE, 0.12],
    outline: { color: [...FIRE, 0.9], width: 1.4 },
  });
  l.perimeters.addMany(
    s.fires.perimeters.map(
      (p) =>
        new Graphic({
          geometry: new Polygon({ rings: p.rings, spatialReference: WM }),
          symbol: perimeterSymbol,
          attributes: { NAME: p.name, AREA: fmt.acres(p.acres) },
          popupTemplate: { title: '{NAME}', content: 'Perimeter: {AREA}' },
        }),
    ),
  );

  // Fire size rings at cluster centres: ring size grows with burned area.
  const textColor = isMap ? [24, 24, 27, 1] : [250, 250, 250, 1];
  const textHalo = isMap ? [255, 255, 255, 0.9] : [0, 0, 0, 0.8];
  for (const c of s.fires.clusters) {
    const size = c.sizeClass ?? 'small';
    if (size === 'small') continue;
    const st = FIRE_SIZE_STYLE[size];
    const geometry = new Point({ x: c.centroid[0], y: c.centroid[1], spatialReference: WM });
    const ring = Math.min(48, 14 + Math.log10(Math.max(10, c.areaAcres)) * 7);
    l.fires.add(
      new Graphic({
        geometry,
        symbol: new SimpleMarkerSymbol({
          style: 'circle',
          size: size === 'large' ? ring : ring * 0.7,
          color: [...st.rgb, 0.14],
          outline: { color: [...st.rgb, 0.95], width: size === 'large' ? 2.2 : 1.4 },
        }),
        attributes: { CID: c.id },
      }),
    );
    if (size === 'large') {
      l.fireLabels.add(
        new Graphic({
          geometry,
          symbol: new TextSymbol({
            text: fmt.acres(c.areaAcres),
            color: textColor,
            haloColor: textHalo,
            haloSize: 1.5,
            yoffset: -(ring / 2 + 9),
            font: { size: 10, weight: 'bold' },
          }),
          attributes: { CID: c.id },
        }),
      );
    }
  }

  // Detections coloured by the size of the fire they belong to. Shared symbols keep this cheap.
  const outline = { color: isMap ? [24, 24, 27, 0.55] : [0, 0, 0, 0.5], width: 0.5 };
  const sym = Object.fromEntries(
    (Object.keys(FIRE_SIZE_STYLE) as FireSize[]).map((k) => [
      k,
      new SimpleMarkerSymbol({
        style: 'circle',
        size: FIRE_SIZE_STYLE[k].dot,
        color: [...FIRE_SIZE_STYLE[k].rgb, 0.92],
        outline,
      }),
    ]),
  ) as Record<FireSize, SimpleMarkerSymbol>;
  // Small fires first so large ones draw on top.
  const points = [...s.fires.points].sort((a, b) => (a[3] ?? 1) - (b[3] ?? 1));
  // Each detection knows its fire, so hovering any dot opens that fire's card.
  const pad = 400;
  const clusterOf = (x: number, y: number) =>
    s.fires.clusters.find(
      (c) => x >= c.bbox[0] - pad && x <= c.bbox[2] + pad && y >= c.bbox[1] - pad && y <= c.bbox[3] + pad,
    )?.id ?? null;
  l.fires.addMany(
    points.map(
      ([x, y, , rank]) =>
        new Graphic({
          geometry: new Point({ x, y, spatialReference: WM }),
          symbol: sym[RANK_SIZE[rank ?? 1] ?? 'small'],
          attributes: { CID: clusterOf(x, y) },
        }),
    ),
  );

  const halo = isMap ? [255, 255, 255, 0.9] : [0, 0, 0, 0.75];
  const addSections = (paths: Path[], color: string, width: number) => {
    if (paths.length === 0) return;
    const geometry = new Polyline({ paths, spatialReference: WM });
    l.hazards.addMany([
      new Graphic({
        geometry,
        symbol: new SimpleLineSymbol({ color: halo, width: width + 2.5, cap: 'round', join: 'round' }),
      }),
      new Graphic({
        geometry,
        symbol: new SimpleLineSymbol({ color, width, cap: 'round', join: 'round' }),
      }),
    ]);
  };
  addSections(s.threats.sections.psps, '#f59e0b', 2.6);
  addSections(s.threats.sections.shutoff, '#ef4444', 3.4);
}

/** Emphasises one fire: its 24h forecast gets a solid outline and the fire a soft halo. */
export function renderFireFocus(l: MapLayers, s: Situation | null, id: string | null): void {
  if (l.fireFocus.destroyed) return;
  l.fireFocus.removeAll();
  const c = s && id ? s.fires.clusters.find((x) => x.id === id) : null;
  if (!s || !c) return;
  const st = FIRE_SIZE_STYLE[c.sizeClass ?? 'small'];
  const spread = s.spread.find((e) => e.clusterId === id);
  if (spread) {
    l.fireFocus.add(
      new Graphic({
        geometry: new Polygon({ rings: [spread.ring], spatialReference: WM }),
        symbol: new SimpleFillSymbol({
          color: [...FIRE, 0.16],
          outline: { color: [254, 226, 226, 0.95], width: 2 },
        }),
      }),
    );
  }
  l.fireFocus.add(
    new Graphic({
      geometry: new Point({ x: c.centroid[0], y: c.centroid[1], spatialReference: WM }),
      symbol: new SimpleMarkerSymbol({
        style: 'circle',
        size: 56,
        color: [...st.rgb, 0.12],
        outline: { color: [255, 255, 255, 0.9], width: 1.5 },
      }),
    }),
  );
}

/**
 * Highlights one operator: the fires threatening its lines get a white halo and a dashed
 * forecast outline, its sections are emphasised, a dashed link joins each fire to the
 * nearest threatened section, and everything else is dimmed.
 */
export function renderCompanyFocus(l: MapLayers, s: Situation | null, lines: ThreatLine[] | null): void {
  if (l.company.destroyed) return;
  l.company.removeAll();
  const active = !!s && !!lines && lines.length > 0;
  const dim = active ? 0.3 : 1;
  for (const layer of [l.fires, l.spread, l.wind, l.hazards]) layer.opacity = dim;
  // 3D text layers can't fade, so labels hide while a company is focused.
  if (l.fireLabels.visible === active) l.fireLabels.visible = !active;
  if (l.spreadLabels.visible === active) l.spreadLabels.visible = !active;
  if (!active || !s || !lines) return;

  const casing = new SimpleLineSymbol({ color: [0, 0, 0, 0.75], width: 7, cap: 'round', join: 'round' });
  for (const line of lines) {
    const paths = (line.sections ?? []).map((x) => x.path);
    if (!paths.length) continue;
    const geometry = new Polyline({ paths, spatialReference: WM });
    l.company.addMany([
      new Graphic({ geometry, symbol: casing }),
      new Graphic({
        geometry,
        symbol: new SimpleLineSymbol({
          color: line.tier === 'SHUT OFF' ? '#ef4444' : '#f59e0b',
          width: 4.2,
          cap: 'round',
          join: 'round',
        }),
      }),
    ]);
  }

  const byFire = new Map<string, ThreatLine[]>();
  for (const line of lines) if (line.fire) byFire.set(line.fire.id, [...(byFire.get(line.fire.id) ?? []), line]);
  for (const [id, fireLines] of byFire) {
    const c = s.fires.clusters.find((x) => x.id === id);
    if (!c) continue;
    const st = FIRE_SIZE_STYLE[c.sizeClass ?? 'small'];
    const spread = s.spread.find((e) => e.clusterId === id);
    if (spread) {
      l.company.add(
        new Graphic({
          geometry: new Polygon({ rings: [spread.ring], spatialReference: WM }),
          symbol: new SimpleFillSymbol({
            color: [...FIRE, 0.12],
            outline: { color: [254, 226, 226, 0.95], width: 1.6, style: 'short-dash' },
          }),
        }),
      );
    }
    const [cx, cy] = c.centroid;
    let best: number[] | null = null;
    let bestD = Infinity;
    for (const line of fireLines)
      for (const sec of line.sections ?? [])
        for (const p of sec.path) {
          const d = (p[0] - cx) ** 2 + (p[1] - cy) ** 2;
          if (d < bestD) {
            bestD = d;
            best = p;
          }
        }
    if (best) {
      l.company.add(
        new Graphic({
          geometry: new Polyline({ paths: [[[cx, cy], best]], spatialReference: WM }),
          symbol: new SimpleLineSymbol({ color: [255, 255, 255, 0.85], width: 1.4, style: 'dash' }),
        }),
      );
    }
    const at = new Point({ x: cx, y: cy, spatialReference: WM });
    l.company.addMany([
      new Graphic({
        geometry: at,
        symbol: new SimpleMarkerSymbol({
          style: 'circle',
          size: 34,
          color: [...st.rgb, 0.25],
          outline: { color: [255, 255, 255, 0.95], width: 2 },
        }),
        attributes: { CID: id },
      }),
      new Graphic({
        geometry: at,
        symbol: new SimpleMarkerSymbol({
          style: 'circle',
          size: 11,
          color: [...st.rgb, 1],
          outline: { color: [0, 0, 0, 0.6], width: 1 },
        }),
        attributes: { CID: id },
      }),
    ]);
  }
}

/** Extent (Mercator) covering an operator's threatened sections and the fires near them. */
export function companyExtent(s: Situation, lines: ThreatLine[]): [number, number, number, number] | null {
  let b: [number, number, number, number] = [Infinity, Infinity, -Infinity, -Infinity];
  const add = (x: number, y: number) => {
    b = [Math.min(b[0], x), Math.min(b[1], y), Math.max(b[2], x), Math.max(b[3], y)];
  };
  for (const line of lines) {
    for (const sec of line.sections ?? []) for (const [x, y] of sec.path) add(x, y);
    const c = line.fire ? s.fires.clusters.find((x) => x.id === line.fire?.id) : undefined;
    if (c) add(c.centroid[0], c.centroid[1]);
  }
  return Number.isFinite(b[0]) ? b : null;
}

export function setStateLayersVisible(
  l: MapLayers,
  inState: boolean,
  showForestLines: boolean,
  showGrid = true,
): void {
  const grid = inState && showGrid;
  l.states.visible = !inState;
  l.focus.visible = !inState;
  l.lines.visible = grid;
  l.lineCasing.visible = grid;
  l.lineHover.visible = grid;
  l.forestLines.visible = grid && showForestLines;
  l.spread.visible = inState;
  l.spreadLabels.visible = inState;
  l.wind.visible = inState;
  l.fireFocus.visible = inState;
  l.fireLabels.visible = inState;
  l.company.visible = inState;
  l.perimeters.visible = inState;
  l.fires.visible = inState;
  l.hazards.visible = grid;
}

export function formatUsdCompact(usd: number): string {
  if (usd >= 1e9) return `$${(usd / 1e9).toFixed(2)}B`;
  if (usd >= 1e6) return `$${(usd / 1e6).toFixed(1)}M`;
  if (usd >= 1e3) return `$${Math.round(usd / 1e3)}K`;
  return `$${Math.round(usd)}`;
}
