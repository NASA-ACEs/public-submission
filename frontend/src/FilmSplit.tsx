/**
 * Film mode, side by side (the default of `?demo=nisar-film`), three acts in one loop. Nothing of the app's interface
 * is on screen.
 * Act 1: two halves of one frame, one fire grown at the same time from the fuel map alone ("NISAR OFF", on the bare
 * satellite image) and with the fuel state from L-band radar ("NISAR ON", on that layer); one clock, an area under
 * each label. The transmission lines in the frame are drawn in both halves; the section a fire crosses turns red and
 * the time it took is told, for each estimate.
 * Act 2: both halves pull back together, without a cut, to the mountain range, and the fire fades. The halves are now
 * the recent photograph and the radar layer, with the mapped outline of the 2018 burn over both and one headline.
 * Then the halves give way to one view of the range on the radar layer, and a card of lines comes in on the right.
 * Act 3: that view pulls back to all of California on the model's risk map, dimmed. The large fires of the two test
 * years stand on it as circles; first those the fire-weather index had flagged light up, then those the model had,
 * each with its count, and the film closes on both counts and its last line.
 * Keys: space pauses, R restarts. `speed=2` plays faster; `?demo=nisar-truth` loops the scene of the outline alone;
 * `layout=sequence` is the older one-after-the-other film (Film.tsx).
 */
import '@arcgis/core/assets/esri/themes/dark/main.css';
import Basemap from '@arcgis/core/Basemap.js';
import * as reactiveUtils from '@arcgis/core/core/reactiveUtils.js';
import Extent from '@arcgis/core/geometry/Extent.js';
import Point from '@arcgis/core/geometry/Point.js';
import SpatialReference from '@arcgis/core/geometry/SpatialReference.js';
import TileLayer from '@arcgis/core/layers/TileLayer.js';
import type WebTileLayer from '@arcgis/core/layers/WebTileLayer.js';
import ArcGISMap from '@arcgis/core/Map.js';
import Viewpoint from '@arcgis/core/Viewpoint.js';
import MapView from '@arcgis/core/views/MapView.js';
import { useEffect, useRef, useState, type CSSProperties } from 'react';
import './App.css';
import './film.css';
import type { FilmScenario } from './demo';
import { API_BASE, fetchStateLines } from './grid/api';
import { createBurnabilityRasterLayer, type BurnabilityMetadata, type FireGrowth } from './grid/burnability';
import { lonLatToMercator } from './grid/geo';
import { LARGE_FIRES } from './largeFires';

type Variant = 'on' | 'off';
type Act = 'run' | 'end' | 'truth' | 'card' | 'state';
type Side = 'left' | 'right' | 'above' | 'below';

interface Step {
  hours: number;
  area: number;
  polygons: number[][][][];
}
interface Scars {
  features: Array<{ geometry: { coordinates: number[][][] } }>;
}
/** Where a view looks: the map point at its middle (Web Mercator), the scale, and map metres to a pixel. */
interface Pose {
  x: number;
  y: number;
  scale: number;
  res: number;
}
/** One path of a transmission line (Web Mercator), in short pieces where a fire can get to it: the step at which each
 *  fire's isochrones first cross the path, and the step from which each point counts as reached (`Infinity`: not
 *  within the run). */
interface Wire {
  oid: number;
  kv: number;
  points: Array<[number, number]>;
  reached: Record<Variant, number[]>;
  crossed: Record<Variant, number>;
}
interface Label {
  text: string;
  side: Side;
  x: number;
  y: number;
  /** A line back to the circle, where the label stands away from it. */
  lead?: [number, number, number, number];
}

/** Film time in ms, at speed 1. */
const INTRO = 600; // act 1: a fade in,
const RUN = 6_000; // the run,
const HOLD = 1_500; // the result held
const ZOOM = 2_000; // act 2: the halves pull back to the range,
const SPLIT = 2_400; // are held there with what NISAR sees,
const MERGE = 700; // and give way to one view
const LINE = 1_300; // whose card takes a line every so often
const FADE = 600;
const CARD_STAY = 1_300;
const SWAP = 500; // act 3: the radar layer gives way to the risk map,
const PULL = 2_400; // the view pulls back to the state,
const PHASE = 3_000; // each warning has its fires lit and held,
const LIGHT = 1_600; // of which this long for the lighting,
const STAY = 3_200; // and the last frame is held
const OUT = 700;
const T_HOLD = INTRO + RUN;
const T_ZOOM = T_HOLD + HOLD;
const T_FAR = T_ZOOM + ZOOM;
const T_CARD = T_FAR + SPLIT;
/** `nisar-truth`, the scene of the outline alone: a fade in, the outline drawn, held. */
const ONLY_IN = 600;
const ONLY_REVEAL = 1_300;
const ONLY_STAY = 4_500;

const LAYER_OPACITY = 0.62;
/** The third act's ground: the photograph and the risk classes, both turned down so that the circles carry. */
const STATE_IMAGERY = 0.3;
const STATE_RISK = 0.36;
/**
 * The widest the second act may look, in Web Mercator metres to a pixel: beyond it the imagery service switches to an
 * older, coarser picture taken soon after the 2018 fire, in which the burn is plain to see.
 */
const IMAGERY_LIMIT = 50;

const FIRE_FILL = 'rgba(226, 54, 28, 0.5)';
const FIRE_FRONT = '#fff3c4';
const SCAR_LINE = '#ffffff';
const CASING = 'rgba(9, 9, 11, 0.8)';
const WIRE = '#e6ecfa';
const WIRE_REACHED = '#ef4444'; // the app's colour of a line to shut off
/** Along a line, this much on either side of the burned stretch counts as reached, in metres. */
const WIRE_MARGIN = 100;
const WIRE_PIECE = 2;
const MODEL = '#4c9aff';
const INDEX = '#b4b4bd';
const IMAGERY = 'https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer';

const COMPASS = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'];
const compass = (deg: number) => COMPASS[Math.round((((deg % 360) + 360) % 360) / 45) % 8];
const hectares = (a: number) => (a < 10 ? a.toFixed(1) : Math.round(a).toString());
const unit = (v: number) => Math.min(1, Math.max(0, v));
const ease = (v: number) => unit(v) * unit(v) * (3 - 2 * unit(v));

/** A length of time in words, to ten minutes: "2 h 20 min", "3 h", "40 min". */
function span(hours: number): string {
  const minutes = Math.round((hours * 60) / 10) * 10;
  const h = Math.floor(minutes / 60);
  return [h ? `${h} h` : '', minutes % 60 || !h ? `${minutes % 60} min` : ''].filter(Boolean).join(' ');
}

function clock(hours: number): string {
  const minutes = Math.round((hours * 60) / 5) * 5;
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, '0')} min`;
}

function steps(growth: FireGrowth): Step[] {
  return growth.features
    .filter((f) => f.geometry?.coordinates?.length)
    .map((f) => ({ hours: f.properties.hours, area: f.properties.area_ha, polygons: f.geometry!.coordinates }))
    .sort((a, b) => a.hours - b.hours);
}

function areaAt(s: Step[], hours: number): number {
  let h0 = 0;
  let a0 = 0;
  for (const st of s) {
    if (hours <= st.hours) return a0 + ((st.area - a0) * (hours - h0)) / (st.hours - h0 || 1);
    h0 = st.hours;
    a0 = st.area;
  }
  return a0;
}

function bounds(points: number[][], grow: number): Extent {
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  return new Extent({
    xmin: Math.min(...xs),
    xmax: Math.max(...xs),
    ymin: Math.min(...ys),
    ymax: Math.max(...ys),
    spatialReference: SpatialReference.WGS84,
  }).expand(grow);
}

/** One path from a near pose to a far one: scale changes evenly on a log scale and the middle moves with it, so one
 *  map point stays where it is on screen and nothing jumps. */
function between(near: Pose, far: Pose, k: number): Pose {
  if (far.scale === near.scale) return near;
  const scale = near.scale * Math.pow(far.scale / near.scale, k);
  const stay = (far.scale - scale) / (far.scale - near.scale);
  return {
    x: far.x + (near.x - far.x) * stay,
    y: far.y + (near.y - far.y) * stay,
    scale,
    res: (near.res * scale) / near.scale,
  };
}

export default function FilmSplit({ film }: { film: FilmScenario }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const offRef = useRef<HTMLDivElement>(null);
  const onRef = useRef<HTMLDivElement>(null);
  const wideRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const clockRef = useRef<HTMLParagraphElement>(null);
  const areaOffRef = useRef<HTMLSpanElement>(null);
  const areaOnRef = useRef<HTMLSpanElement>(null);
  const reachOffRef = useRef<HTMLParagraphElement>(null);
  const reachOnRef = useRef<HTMLParagraphElement>(null);
  const countIndexRef = useRef<HTMLElement>(null);
  const countModelRef = useRef<HTMLElement>(null);
  const [act, setAct] = useState<Act | null>(null);
  const [ratio, setRatio] = useState('');
  const [ramp, setRamp] = useState('');
  const [labels, setLabels] = useState<Label[]>([]);
  const [wired, setWired] = useState(false); // transmission lines are in the frame
  const [gain, setGain] = useState('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    const speed = Math.min(4, Math.max(0.25, Number(query.get('speed')) || 1));
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const abort = new AbortController();
    const views: MapView[] = [];
    let raf = 0;
    let dead = false;

    const timer = window.setTimeout(async () => {
      try {
        const results = `${API_BASE}/v1/burnability/results`;
        const base = `${results}/${film.result}`;
        const count = Math.round((film.hours * 60) / film.stepMinutes);
        const hours = Array.from({ length: count }, (_, k) => (((k + 1) * film.stepMinutes) / 60).toFixed(4)).join(',');
        const json = <T,>(url: string, what: string) =>
          fetch(url, { signal: abort.signal }).then((r) =>
            r.ok ? (r.json() as Promise<T>) : Promise.reject(new Error(`${what} ${r.status}`)),
          );
        const spread = (fuelState: boolean) =>
          json<FireGrowth>(
            `${base}/spread?lon=${film.fire.lon}&lat=${film.fire.lat}&hours=${hours}&wind_speed=${film.fire.wind.speed}` +
              `&wind_from=${film.fire.wind.from}${fuelState ? '' : '&fuel_state=false'}`,
            'fire growth',
          );
        const only = Boolean(film.truthOnly);
        const [meta, withNisar, alone, scars, stateMeta, grid] = await Promise.all([
          json<BurnabilityMetadata>(base, 'result'),
          spread(true),
          spread(false),
          // A backend without the outline leaves the film with its first act; one without the statewide result, with
          // the first act and the scene of the outline.
          film.scar
            ? json<Scars>(`${base}/scars?min_years=${film.scar.minYears}&max_years=${film.scar.maxYears}`, 'scars').catch(() => null)
            : Promise.resolve(null),
          film.pitch && !only
            ? json<BurnabilityMetadata>(`${results}/${film.pitch.result}`, 'statewide result').catch(() => null)
            : Promise.resolve(null),
          film.grid && !only ? fetchStateLines(film.grid).catch(() => []) : Promise.resolve([]),
        ]);
        if (dead) return;
        const data: Record<Variant, Step[]> = { on: steps(withNisar), off: steps(alone) };
        if (!data.on.length || !data.off.length) throw new Error('no fire at the ignition');
        const last = { on: data.on[data.on.length - 1], off: data.off[data.off.length - 1] };
        const scar = (scars?.features ?? []).map((f) => f.geometry.coordinates).filter((rings) => rings.length);
        const truth = scar.length > 0 && Boolean(film.scar);
        if (only && !truth) throw new Error('this result has no outline of the old fire');
        const pitch = truth && !only && stateMeta ? film.pitch : undefined;

        // The timeline after the scene of the outline, when the film has its card and its third act.
        const lines = pitch ? pitch.lines.length : 0;
        const tLines = T_CARD + 500; // the first line of the card
        const tState = tLines + Math.max(0, lines - 1) * LINE + FADE + CARD_STAY; // the third act begins
        const tIndex = tState + SWAP + PULL + 100; // the index's fires begin to light,
        const tModel = tIndex + PHASE; // then the model's,
        const tClose = tModel + PHASE; // then the closing line
        const tEnd = !truth ? T_ZOOM : only ? T_FAR + ONLY_REVEAL + ONLY_STAY : pitch ? tClose + FADE + STAY : T_CARD + STAY;
        const total = tEnd + OUT;
        const from = only ? T_FAR - ONLY_IN : 0;

        const stops = meta.layers.find((l) => l.id === film.layer)?.legend?.stops ?? [];
        setRamp(stops.length ? `linear-gradient(to right, ${stops.map(([, c]) => c).join(', ')})` : '');
        setRatio(
          last.on.area <= last.off.area
            ? `${(last.off.area / last.on.area).toFixed(1)}x smaller with NISAR`
            : `${(last.on.area / last.off.area).toFixed(1)}x larger with NISAR`,
        );

        // The large fires of the third act, largest first so that small circles lie on top. Each warning lights its
        // fires in the order they started.
        const fires = LARGE_FIRES.map((f) => {
          const [mx, my] = lonLatToMercator(f.lon, f.lat);
          return { ...f, mx, my, lit: { index: Infinity, model: Infinity } };
        }).sort((a, b) => b.ha - a.ha);
        for (const [key, start] of [
          ['index', tIndex + 400],
          ['model', tModel + 400],
        ] as ['index' | 'model', number][]) {
          const group = fires.filter((f) => f[key]).sort((a, b) => a.date.localeCompare(b.date));
          group.forEach((f, i) => (f.lit[key] = start + (LIGHT * i) / Math.max(1, group.length - 1)));
        }

        // Transmission lines in the result's area, and when each fire reaches them. A line is reached at the first
        // isochrone that crosses it. The section drawn as reached is what lies inside the burned area and a little
        // more along the line on either side. The line the film speaks of is the one the fire from the fuel map
        // alone reaches first.
        const stretch = 1 / Math.cos((film.fire.lat * Math.PI) / 180); // map metres to a metre on the ground, here
        const [gx0, gy0] = lonLatToMercator(meta.bbox_wgs84[0], meta.bbox_wgs84[1]);
        const [gx1, gy1] = lonLatToMercator(meta.bbox_wgs84[2], meta.bbox_wgs84[3]);
        const burned = (v: Variant) =>
          data[v].map((s) => {
            const rings = s.polygons.flatMap((polygon) => polygon.map((ring) => ring.map(([lon, lat]) => lonLatToMercator(lon, lat))));
            const xs = rings.flat().map((p) => p[0]);
            const ys = rings.flat().map((p) => p[1]);
            return { rings, x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) };
          });
        const fronts = { off: burned('off'), on: burned('on') };
        const holds = (b: (typeof fronts.off)[number], x: number, y: number) => {
          if (x < b.x0 || x > b.x1 || y < b.y0 || y > b.y1) return false;
          let odd = false;
          for (const ring of b.rings) {
            for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
              const [xi, yi] = ring[i];
              const [xj, yj] = ring[j];
              if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) odd = !odd;
            }
          }
          return odd;
        };
        const firstStep = (v: Variant, x: number, y: number) => {
          const b = fronts[v];
          if (!holds(b[b.length - 1], x, y)) return Infinity;
          let lo = 0;
          let hi = b.length - 1; // the burned area only grows, so the first step that holds the point is found by halves
          while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (holds(b[mid], x, y)) hi = mid;
            else lo = mid + 1;
          }
          return lo;
        };
        // Does the piece of line from a to b touch the area burned by one step: an end inside it, or a crossing of
        // its edge. This is what "the isochrone crosses the line" means, to the metre.
        const touches = (f: (typeof fronts.off)[number], ax: number, ay: number, bx: number, by: number) => {
          if (Math.max(ax, bx) < f.x0 || Math.min(ax, bx) > f.x1 || Math.max(ay, by) < f.y0 || Math.min(ay, by) > f.y1) return false;
          if (holds(f, ax, ay) || holds(f, bx, by)) return true;
          const side = (px: number, py: number, qx: number, qy: number, rx: number, ry: number) => (qx - px) * (ry - py) - (qy - py) * (rx - px);
          for (const ring of f.rings) {
            for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
              const [cx, cy] = ring[j];
              const [dx, dy] = ring[i];
              if (side(ax, ay, bx, by, cx, cy) * side(ax, ay, bx, by, dx, dy) < 0 && side(cx, cy, dx, dy, ax, ay) * side(cx, cy, dx, dy, bx, by) < 0)
                return true;
            }
          }
          return false;
        };
        const crossStep = (v: Variant, path: number[][]) => {
          const f = fronts[v];
          const hit = (step: number) => path.some(([x, y], i) => i > 0 && touches(f[step], path[i - 1][0], path[i - 1][1], x, y));
          if (!hit(f.length - 1)) return Infinity;
          let lo = 0;
          let hi = f.length - 1;
          while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (hit(mid)) hi = mid;
            else lo = mid + 1;
          }
          return lo;
        };
        // Lines are cut into short pieces only where a fire can get to them.
        const widest = { off: fronts.off[fronts.off.length - 1], on: fronts.on[fronts.on.length - 1] };
        const pad = 2 * WIRE_MARGIN * stretch;
        const fx0 = Math.min(widest.off.x0, widest.on.x0) - pad;
        const fx1 = Math.max(widest.off.x1, widest.on.x1) + pad;
        const fy0 = Math.min(widest.off.y0, widest.on.y0) - pad;
        const fy1 = Math.max(widest.off.y1, widest.on.y1) + pad;
        const wires: Wire[] = grid
          .filter((l) => l.paths.some((path) => path.some(([x, y]) => x >= gx0 && x <= gx1 && y >= gy0 && y <= gy1)))
          .flatMap((l) =>
            l.paths.map((path) => {
              const points: Array<[number, number]> = [];
              const along: number[] = []; // metres on the ground from the path's start
              path.forEach(([x, y], i) => {
                if (i) {
                  const [px, py] = path[i - 1];
                  const length = Math.hypot(x - px, y - py);
                  const close = Math.max(px, x) >= fx0 && Math.min(px, x) <= fx1 && Math.max(py, y) >= fy0 && Math.min(py, y) <= fy1;
                  const pieces = close ? Math.max(1, Math.ceil(length / (WIRE_PIECE * stretch))) : 1;
                  for (let k = 1; k <= pieces; k++) {
                    points.push([px + ((x - px) * k) / pieces, py + ((y - py) * k) / pieces]);
                    along.push(along[along.length - 1] + length / pieces / stretch);
                  }
                } else {
                  points.push([x, y]);
                  along.push(0);
                }
              });
              const reachedBy = (v: Variant) => {
                const first = points.map(([x, y]) => firstStep(v, x, y));
                return first.map((own, i) => {
                  let best = own;
                  for (let j = i - 1; j >= 0 && along[i] - along[j] <= WIRE_MARGIN; j--) best = Math.min(best, first[j]);
                  for (let j = i + 1; j < first.length && along[j] - along[i] <= WIRE_MARGIN; j++) best = Math.min(best, first[j]);
                  return best;
                });
              };
              return {
                oid: l.oid,
                kv: l.kv,
                points,
                reached: { off: reachedBy('off'), on: reachedBy('on') },
                crossed: { off: crossStep('off', path), on: crossStep('on', path) },
              };
            }),
          );
        const firstOf = (v: Variant, oid?: number) =>
          Math.min(Infinity, ...wires.filter((w) => oid == null || w.oid === oid).map((w) => w.crossed[v]));
        const told =
          wires.find((w) => w.crossed.off === firstOf('off') && firstOf('off') < Infinity) ??
          wires.find((w) => w.crossed.on === firstOf('on') && firstOf('on') < Infinity);
        const reachOf = (v: Variant) => {
          const step = told ? firstOf(v, told.oid) : Infinity;
          return step < Infinity ? data[v][step].hours : null;
        };
        const reachH = { off: reachOf('off'), on: reachOf('on') };
        const kv = told ? `${Math.round(told.kv)} kV line` : '';
        const more = reachH.off != null && reachH.on != null ? reachH.on - reachH.off : null;
        // told only when it is at least half an hour
        setGain(more != null && more >= 0.5 - 1e-9 ? `${span(more)} more to act with NISAR` : '');
        setWired(wires.length > 0);
        const lengthKm = (v: Variant, oid?: number) =>
          wires
            .filter((w) => oid == null || w.oid === oid)
            .reduce(
              (sum, w) =>
                sum +
                w.points.reduce(
                  (km, [x, y], i) =>
                    i && w.reached[v][i] < Infinity && w.reached[v][i - 1] < Infinity
                      ? km + Math.hypot(x - w.points[i - 1][0], y - w.points[i - 1][1]) / stretch / 1_000
                      : km,
                  0,
                ),
              0,
            );

        // Still views that open on the fire, so that no tile of a wider, older picture is ever fetched: the two
        // halves (the satellite image alone, and the same with NISAR's layer over it), which always look at the same
        // place, and for the card and the third act one view over the whole screen.
        const reach = bounds([...last.off.polygons, ...last.on.polygons].flat(2).concat([[film.fire.lon, film.fire.lat]]), 1.18);
        const still2d = (container: HTMLDivElement, map: ArcGISMap) => {
          const v = new MapView({ container, map, extent: reach, constraints: { rotationEnabled: false, snapToZoom: false } });
          v.ui.components = [];
          v.on('drag', (e) => e.stopPropagation());
          v.on('mouse-wheel', (e) => e.stopPropagation());
          v.on('double-click', (e) => e.stopPropagation());
          v.on('key-down', (e) => e.stopPropagation());
          views.push(v);
          return v;
        };
        const photo = () => new TileLayer({ url: IMAGERY });
        const view: Record<Variant, MapView> = {
          off: still2d(offRef.current!, new ArcGISMap({ basemap: new Basemap({ baseLayers: [photo()] }) })),
          on: still2d(
            onRef.current!,
            new ArcGISMap({
              basemap: new Basemap({ baseLayers: [photo()] }),
              layers: [createBurnabilityRasterLayer(meta, film.layer, LAYER_OPACITY)],
            }),
          ),
        };
        const halves = [view.off, view.on];
        let wide: { view: MapView; photo: TileLayer; radar: WebTileLayer; risk: WebTileLayer } | null = null;
        if (pitch && stateMeta) {
          const ground = photo();
          const radar = createBurnabilityRasterLayer(meta, film.layer, LAYER_OPACITY);
          const risk = createBurnabilityRasterLayer(stateMeta, pitch.layer, STATE_RISK);
          radar.id = 'film-radar';
          risk.id = 'film-risk';
          const map = new ArcGISMap({ basemap: new Basemap({ baseLayers: [ground] }), layers: [risk, radar] });
          wide = { view: still2d(wideRef.current!, map), photo: ground, radar, risk };
        }
        await Promise.all(views.map((v) => v.when()));
        if (dead) return;

        // Poses. A frame given as an extent with margins is fitted once by the map itself and read back; after that
        // the views have no margins and are placed by pose alone, each frame, and the drawing over them follows from
        // the same numbers.
        let near: Pose = { x: 0, y: 0, scale: 1, res: 1 }; // the halves on the fire
        let far = near; // the halves on the range
        let wideFar = near; // the one view on the state; on the range it looks where the left half does
        let kNow = -1;
        let pNow = -1;
        let busy = true; // while the frames are being measured
        let names: Label[] = []; // of the fires named in the last frame, placed on the state
        const wideNear = (): Pose => ({ ...far, x: far.x + (view.off.width / 2) * far.res });
        const place = (targets: MapView[], p: Pose) => {
          const middle = new Point({ x: p.x, y: p.y, spatialReference: SpatialReference.WebMercator });
          for (const v of targets) v.viewpoint = new Viewpoint({ targetGeometry: middle, scale: p.scale });
        };
        const go = (k: number, force = false) => {
          if (!force && Math.abs(k - kNow) < 1e-6) return;
          kNow = k;
          place(halves, between(near, far, k));
        };
        const pull = (k: number, force = false) => {
          if (!wide || (!force && Math.abs(k - pNow) < 1e-6)) return;
          pNow = k;
          place([wide.view], between(wideNear(), wideFar, k));
        };
        const fitted = async (v: MapView, extent: Extent, margin: { top: number; bottom: number; left?: number; right?: number }) => {
          v.padding = {
            top: v.height * margin.top,
            bottom: v.height * margin.bottom,
            left: v.width * (margin.left ?? 0),
            right: v.width * (margin.right ?? 0),
          };
          await v.goTo(extent, { animate: false });
          const middle = v.toMap({ x: v.width / 2, y: v.height / 2 });
          if (!middle) throw new Error('the map has no size');
          const p: Pose = { x: middle.x, y: middle.y, scale: v.scale, res: v.resolution };
          v.padding = { top: 0, right: 0, bottom: 0, left: 0 };
          return p;
        };
        // The second act's frame: the radar layer has to fill each half from edge to edge, with no edge of the
        // result's footprint in sight, and the photograph has to stay the recent one. So the frame hangs from an
        // upper edge just inside the footprint, keeps clear of its sides, and is never wider than the imagery allows.
        const range = (): Pose => {
          const { width, height } = view.off;
          const frame = film.scar!.frame;
          const [west, south, east, north] = meta.bbox_wgs84;
          const [x0] = lonLatToMercator(west, south);
          const [x1] = lonLatToMercator(east, north);
          const [middle, top] = lonLatToMercator(frame.lon, frame.top);
          const clear = 1_500 * stretch; // of the footprint's sides: it is a rectangle turned a little on the map
          const res = Math.min((frame.heightKm * 1_000 * stretch) / height, (x1 - x0 - 2 * clear) / width, IMAGERY_LIMIT);
          const x = Math.min(Math.max(middle, x0 + clear + (width * res) / 2), x1 - clear - (width * res) / 2);
          return { x, y: top - (height * res) / 2, scale: (near.scale * res) / near.res, res };
        };

        // Paths in the pixels of the first frame of one half (both halves are the same size and look at the same
        // place); a scale and a shift take them to any later pose. Even-odd fills leave unburned islands open.
        const paths: Record<Variant, Path2D[]> = { on: [], off: [] };
        const rings: Record<Variant, Path2D[]> = { on: [], off: [] };
        let outline = new Path2D();
        let wiring = new Path2D(); // every transmission line
        const marked: Record<Variant, Path2D[]> = { on: [], off: [] }; // the sections reached by each step
        let start = { x: 0, y: 0 };
        const build = () => {
          const { width, height } = view.off;
          const px = (lon: number, lat: number): [number, number] => {
            const [x, y] = lonLatToMercator(lon, lat);
            return [width / 2 + (x - near.x) / near.res, height / 2 - (y - near.y) / near.res];
          };
          const trace = (p: Path2D, ring: number[][]) => {
            ring.forEach(([lon, lat], i) => {
              const [x, y] = px(lon, lat);
              if (i) p.lineTo(x, y);
              else p.moveTo(x, y);
            });
            p.closePath();
          };
          for (const v of ['off', 'on'] as Variant[]) {
            paths[v] = data[v].map((s) => {
              const p = new Path2D();
              for (const polygon of s.polygons) for (const ring of polygon) trace(p, ring);
              return p;
            });
            rings[v] = paths[v].map((p, i) => {
              const ring = new Path2D(p);
              if (i) ring.addPath(paths[v][i - 1]);
              return ring;
            });
          }
          outline = new Path2D();
          for (const polygon of scar) for (const ring of polygon) trace(outline, ring);
          const at0 = (x: number, y: number): [number, number] => [width / 2 + (x - near.x) / near.res, height / 2 - (y - near.y) / near.res];
          wiring = new Path2D();
          for (const w of wires) {
            w.points.forEach(([mx, my], i) => {
              const [x, y] = at0(mx, my);
              if (i) wiring.lineTo(x, y);
              else wiring.moveTo(x, y);
            });
          }
          for (const v of ['off', 'on'] as Variant[]) {
            marked[v] = data[v].map(() => new Path2D());
            data[v].forEach((_, step) => {
              const p = marked[v][step];
              if (step) p.addPath(marked[v][step - 1]);
              for (const w of wires) {
                w.points.forEach(([mx, my], i) => {
                  // a piece joins at the step its later end is reached
                  if (!i || Math.max(w.reached[v][i], w.reached[v][i - 1]) !== step) return;
                  p.moveTo(...at0(w.points[i - 1][0], w.points[i - 1][1]));
                  p.lineTo(...at0(mx, my));
                });
              }
            });
          }
          const [x, y] = px(film.fire.lon, film.fire.lat);
          start = { x, y };
          const canvas = canvasRef.current;
          if (canvas) {
            const dpr = window.devicePixelRatio || 1;
            canvas.width = Math.round(canvas.clientWidth * dpr);
            canvas.height = Math.round(canvas.clientHeight * dpr);
          }
        };
        /** A circle whose area is the area burned (not to the map's scale): 400 ha is 4 px at 1920 wide. */
        const radius = (ha: number, width: number) => 2 * Math.sqrt(ha / 100) * (width / 1920);

        const measure = async () => {
          busy = true;
          near = await fitted(view.off, reach, { top: 0.25, bottom: 0.15 });
          // Alone, the scene of the outline keeps its own frame: the whole burn with room above and below.
          far = !truth ? near : only ? await fitted(view.off, bounds(scar.flat(2), 1.02), { top: 0.2, bottom: 0.31 }) : range();
          if (wide && stateMeta && pitch) {
            const [w, s, e, n] = stateMeta.bbox_wgs84;
            const all = new Extent({ xmin: w, ymin: s, xmax: e, ymax: n, spatialReference: SpatialReference.WGS84 });
            // the state in the left half, the right half for the words
            wideFar = await fitted(wide.view, all, { top: 0.05, bottom: 0.13, left: 0.02, right: 0.47 });
            const { width, height } = wide.view;
            const px = width / 1920;
            names = fires
              .filter((f) => pitch.labelled[f.name])
              .map((f) => {
                const { side, text, dx = 0, dy = 0 } = pitch.labelled[f.name];
                const r = radius(f.ha, width);
                const cx = width / 2 + (f.mx - wideFar.x) / wideFar.res;
                const cy = height / 2 - (f.my - wideFar.y) / wideFar.res;
                const ux = side === 'right' ? 1 : side === 'left' ? -1 : 0;
                const uy = side === 'below' ? 1 : side === 'above' ? -1 : 0;
                const x = cx + ux * (r + 7 * px) + dx * px;
                const y = cy + uy * (r + 7 * px) + dy * px;
                // from the circle's edge towards the label, stopping short of it
                const away = Math.hypot(x - cx, y - cy);
                const lead: Label['lead'] =
                  dx || dy
                    ? [
                        cx + ((x - cx) / away) * (r + 3 * px),
                        cy + ((y - cy) / away) * (r + 3 * px),
                        x - ((x - cx) / away) * 6 * px,
                        y - ((y - cy) / away) * 6 * px,
                      ]
                    : undefined;
                return { text: text ?? f.name, side, x, y, lead };
              });
            setLabels(names);
          }
          build();
          go(only ? 1 : Math.max(0, kNow), true);
          pull(Math.max(0, pNow), true);
          busy = false;
        };
        const settled = () => !busy && views.every((v) => !v.updating);
        const idle = async () => {
          await new Promise((done) => window.setTimeout(done, 80)); // a view takes a moment to notice it has work
          await Promise.all(views.map((v) => reactiveUtils.whenOnce(() => v.updating === false)));
        };
        await measure();
        view.off.on('resize', () => void measure().catch(() => undefined));

        // The tiles of both paths are fetched before the film starts, so that no pull back shows a tile arriving or
        // a coarser one standing in for it.
        await Promise.all([idle(), document.fonts.ready]);
        if (truth) {
          for (const k of only ? [1] : [1, 0.88, 0.76, 0.64, 0.52, 0.4, 0.28, 0.14, 0]) {
            go(k);
            await idle();
            if (dead) return;
          }
        }
        if (wide) {
          for (const k of [0, 0.12, 0.25, 0.4, 0.55, 0.7, 0.85, 1, 0]) {
            pull(k);
            await idle();
            if (dead) return;
          }
        }

        // `u` is one screen pixel in the units of the paths, so that lines keep their width at every scale.
        const stroke = (ctx: CanvasRenderingContext2D, p: Path2D, alpha: number, color: string, width: number, u: number) => {
          if (alpha <= 0) return;
          ctx.globalAlpha = alpha;
          ctx.lineJoin = 'round';
          ctx.strokeStyle = CASING;
          ctx.lineWidth = (width + 2.5) * u;
          ctx.stroke(p);
          ctx.strokeStyle = color;
          ctx.lineWidth = width * u;
          ctx.stroke(p);
        };
        const fire = (ctx: CanvasRenderingContext2D, v: Variant, hrs: number, alpha: number, u: number) => {
          const s = data[v];
          const next = s.findIndex((st) => st.hours > hrs + 1e-9);
          const k = next < 0 ? s.length - 1 : next - 1; // the last front already passed
          const from0 = k < 0 ? 0 : s[k].hours;
          const part = next < 0 ? 0 : (hrs - from0) / (s[next].hours - from0);
          ctx.fillStyle = FIRE_FILL;
          if (k >= 0) {
            ctx.globalAlpha = alpha;
            ctx.fill(paths[v][k], 'evenodd');
          }
          if (next >= 0 && part > 0) {
            ctx.globalAlpha = part * alpha;
            ctx.fill(rings[v][next], 'evenodd');
          }
          if (k >= 0) stroke(ctx, paths[v][k], (next < 0 ? 1 : 1 - part) * alpha, FIRE_FRONT, 2, u);
          if (next >= 0) stroke(ctx, paths[v][next], part * alpha, FIRE_FRONT, 2, u);
          // the ignition, the same mark at the same place in both halves
          const r = 9 * u;
          ctx.globalAlpha = alpha;
          ctx.lineCap = 'round';
          for (const [color, width] of [
            [CASING, 7],
            ['#ffffff', 3],
          ] as [string, number][]) {
            ctx.strokeStyle = color;
            ctx.lineWidth = width * u;
            ctx.beginPath();
            ctx.moveTo(start.x - r, start.y - r);
            ctx.lineTo(start.x + r, start.y + r);
            ctx.moveTo(start.x + r, start.y - r);
            ctx.lineTo(start.x - r, start.y + r);
            ctx.stroke();
          }
        };
        // Every transmission line, thin. Up close it has a dark casing and reads clearly; on the range the lines are
        // context only, and the public data draws parts of them as straight runs at right angles, so they go faint.
        const wire = (ctx: CanvasRenderingContext2D, alpha: number, k: number, u: number) => {
          if (alpha <= 0) return;
          ctx.lineJoin = 'round';
          if (k < 1) {
            ctx.globalAlpha = alpha * 0.8 * (1 - k);
            ctx.strokeStyle = CASING;
            ctx.lineWidth = 4.1 * u;
            ctx.stroke(wiring);
          }
          ctx.globalAlpha = alpha * (0.9 - 0.55 * k);
          ctx.strokeStyle = WIRE;
          ctx.lineWidth = 1.6 * u;
          ctx.stroke(wiring);
        };
        // The sections of line each fire has reached by now, coming in with the front that reaches them.
        const reached = (ctx: CanvasRenderingContext2D, v: Variant, hrs: number, alpha: number, u: number) => {
          const s = data[v];
          const next = s.findIndex((st) => st.hours > hrs + 1e-9);
          const k = next < 0 ? s.length - 1 : next - 1;
          const part = next < 0 ? 0 : (hrs - (k < 0 ? 0 : s[k].hours)) / (s[next].hours - (k < 0 ? 0 : s[k].hours));
          ctx.lineCap = 'round';
          if (next >= 0) stroke(ctx, marked[v][next], part * alpha, WIRE_REACHED, 4.5, u);
          if (k >= 0) stroke(ctx, marked[v][k], alpha, WIRE_REACHED, 4.5, u);
          ctx.lineCap = 'butt';
        };
        // A fire the index flagged gets a pale fill and a heavy ring in the index's colour; one the model flagged is
        // filled in the model's; one neither flagged stays an outline.
        const circles = (ctx: CanvasRenderingContext2D, p: Pose, t: number, alpha: number, named: number, width: number, height: number) => {
          for (const f of fires) {
            const x = width / 2 + (f.mx - p.x) / p.res;
            const y = height / 2 - (f.my - p.y) / p.res;
            const byIndex = ease((t - f.lit.index) / 300);
            const byModel = ease((t - f.lit.model) / 300);
            ctx.beginPath();
            ctx.arc(x, y, radius(f.ha, width), 0, 2 * Math.PI);
            ctx.globalAlpha = alpha * 0.35;
            ctx.fillStyle = '#09090b';
            ctx.fill();
            if (byIndex > 0) {
              ctx.globalAlpha = alpha * byIndex * 0.4;
              ctx.fillStyle = INDEX;
              ctx.fill();
            }
            if (byModel > 0) {
              ctx.globalAlpha = alpha * byModel * 0.92;
              ctx.fillStyle = MODEL;
              ctx.fill();
            }
            ctx.globalAlpha = alpha * (1 - byIndex) * 0.9;
            ctx.strokeStyle = '#ffffff';
            ctx.lineWidth = 1.5;
            ctx.stroke();
            if (byIndex > 0) {
              ctx.globalAlpha = alpha * byIndex;
              ctx.strokeStyle = INDEX;
              ctx.lineWidth = 3;
              ctx.stroke();
            }
          }
          // lines from the names that stand away from their circles
          ctx.globalAlpha = alpha * named * 0.85;
          ctx.strokeStyle = '#ffffff';
          ctx.lineWidth = 1.5;
          for (const { lead } of names) {
            if (!lead) continue;
            ctx.beginPath();
            ctx.moveTo(lead[0], lead[1]);
            ctx.lineTo(lead[2], lead[3]);
            ctx.stroke();
          }
        };

        // What is on screen at film time t. `show` fades everything through black at the loop, and nowhere else.
        const at = (t: number) => {
          const z = !truth ? 0 : only ? 1 : unit((t - T_ZOOM) / ZOOM); // the halves' pull back
          const merge = pitch ? unit((t - T_CARD) / MERGE) : 0; // the halves giving way to one view
          const third = pitch ? t - tState : -1; // ms into the third act
          const out = t >= total - OUT ? (t - (total - OUT)) / OUT : 0;
          const act: Act = third >= 200 ? 'state' : merge >= 0.5 ? 'card' : z >= 0.5 ? 'truth' : t >= T_HOLD ? 'end' : 'run';
          return {
            t,
            act,
            show: Math.min(unit((t - from) / (only ? ONLY_IN : INTRO)), 1 - out),
            // the words of an act leave before the next act's come in
            words:
              act === 'state'
                ? ease((third - SWAP - PULL + 550) / 600) // with the circles, once the state is in the frame
                : act === 'card'
                  ? Math.min(ease(merge * 2 - 1), 1 - unit(third / 200))
                  : act === 'truth'
                    ? Math.min(ease(z * 2 - 1), 1 - ease(merge * 2))
                    : 1 - ease(z * 2),
            k: ease(z),
            hrs: unit((t - INTRO) / RUN) * film.hours,
            fire: 1 - ease(z / 0.6),
            outline: only ? ease((t - T_FAR) / ONLY_REVEAL) : ease((t - T_ZOOM - ZOOM * 0.45) / 900),
            head: ease((t - (only ? T_FAR + 200 : T_FAR - 300)) / FADE),
            wide: ease(merge),
            lines: Array.from({ length: lines }, (_, i) => ease((t - tLines - i * LINE) / FADE)),
            // the radar layer and the outline give way to the risk map before the view moves, so that the edge of the
            // radar layer's footprint never comes into the frame
            swap: ease(third / SWAP),
            pull: ease((third - SWAP + 50) / PULL),
            circles: ease((third - SWAP - PULL + 550) / 600),
            index: ease((t - tIndex) / 400),
            model: ease((t - tModel) / 400),
            close: ease((t - tClose) / FADE),
          };
        };

        let actNow: Act | null = null;
        const draw = (t: number) => {
          const canvas = canvasRef.current;
          const ctx = canvas?.getContext('2d');
          if (!canvas || !ctx || busy) return;
          const f = at(t);
          go(f.k);
          pull(f.pull);
          if (wide) {
            wide.photo.opacity = 1 - (1 - STATE_IMAGERY) * f.swap;
            wide.radar.opacity = LAYER_OPACITY * (1 - f.swap);
            wide.risk.opacity = STATE_RISK * f.swap;
          }
          const width = canvas.clientWidth;
          const height = canvas.clientHeight;
          const half = width / 2;
          const dpr = window.devicePixelRatio || 1;
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
          ctx.clearRect(0, 0, width, height);
          // The paths are in the pixels of the first frame of a half; `a` is such a pixel in pixels of the pose p,
          // whose middle stands at the screen's x.
          const over = (p: Pose, x: number, body: (u: number) => void) => {
            const a = near.res / p.res;
            ctx.translate(x - (a * half) / 2 + (near.x - p.x) / p.res, (height / 2) * (1 - a) - (near.y - p.y) / p.res);
            ctx.scale(a, a);
            body(1 / a);
          };
          if (f.wide < 1) {
            const p = between(near, far, f.k);
            (['off', 'on'] as Variant[]).forEach((v, i) => {
              ctx.save();
              ctx.beginPath();
              ctx.rect(i * half, 0, half, height);
              ctx.clip();
              over(p, i * half + half / 2, (u) => {
                wire(ctx, i ? 1 - f.wide : 1, f.k, u);
                if (f.fire > 0) {
                  fire(ctx, v, f.hrs, f.fire, u);
                  reached(ctx, v, f.hrs, f.fire, u);
                }
                stroke(ctx, outline, f.outline * (i ? 1 - f.wide : 1), SCAR_LINE, 3, u);
              });
              ctx.restore();
            });
          }
          if (wide && f.wide > 0) {
            const p = between(wideNear(), wideFar, f.pull);
            ctx.save();
            if (f.wide < 1) {
              // while the halves are still there the left one draws its own outline, at the same place
              ctx.beginPath();
              ctx.rect(half, 0, half, height);
              ctx.clip();
            }
            over(p, half, (u) => {
              wire(ctx, f.wide * (1 - f.swap), 1, u);
              stroke(ctx, outline, f.wide * (1 - f.swap), SCAR_LINE, 3, u);
            });
            ctx.restore();
            if (f.circles > 0) circles(ctx, p, t, f.circles, f.close, width, height);
            ctx.globalAlpha = 1;
          }
          const root = rootRef.current;
          if (root) {
            const set = (name: string, value: number) => root.style.setProperty(name, value.toFixed(3));
            set('--show', f.show);
            set('--zoom', f.k);
            set('--words', f.words);
            set('--head', f.head);
            set('--wide', f.wide);
            set('--state', f.swap);
            f.lines.forEach((a, i) => set(`--line${i}`, a));
            set('--index', f.index);
            set('--model', f.model);
            set('--close', f.close);
          }
          if (clockRef.current) clockRef.current.textContent = clock(f.hrs);
          if (areaOffRef.current) areaOffRef.current.textContent = `${hectares(areaAt(data.off, f.hrs))} ha`;
          if (areaOnRef.current) areaOnRef.current.textContent = `${hectares(areaAt(data.on, f.hrs))} ha`;
          // under each area, once that fire is at the line; on the held result also when it never got there
          for (const [v, el] of [
            ['off', reachOffRef.current],
            ['on', reachOnRef.current],
          ] as [Variant, HTMLParagraphElement | null][]) {
            const h = reachH[v];
            const text = !told
              ? ''
              : h != null && f.hrs >= h - 1e-6
                ? `${kv} reached after ${span(h)}`
                : h == null && f.act === 'end'
                  ? `${kv} not reached in ${film.hours} h`
                  : '';
            if (el && el.textContent !== text) el.textContent = text;
          }
          if (countIndexRef.current) countIndexRef.current.textContent = String(fires.filter((c) => t >= c.lit.index).length);
          if (countModelRef.current) countModelRef.current.textContent = String(fires.filter((c) => t >= c.lit.model).length);
          if (f.act !== actNow) {
            actNow = f.act;
            setAct(f.act);
          }
        };

        let t = still ? tEnd - 500 : from;
        let paused = still;
        let before = performance.now();
        const loop = (now: number) => {
          if (!paused) {
            t += (now - before) * speed;
            if (t >= total) t = from + ((t - total) % (total - from));
          }
          before = now;
          draw(t);
          raf = requestAnimationFrame(loop);
        };
        const keys = (e: KeyboardEvent) => {
          if (e.code === 'Space') paused = !paused;
          else if (e.key === 'r' || e.key === 'R') t = from;
          else return;
          e.preventDefault();
        };
        window.addEventListener('keydown', keys);
        abort.signal.addEventListener('abort', () => window.removeEventListener('keydown', keys));
        // For a recording script: put the film at a moment and hold it there, or let it run from the start.
        Object.assign(window, {
          __film: {
            layout: 'side-by-side',
            from,
            duration: (total - from) / speed,
            marks: only
              ? { photo: T_FAR + 50, truth: tEnd - 300 }
              : {
                  start: INTRO + 100,
                  h2: INTRO + (RUN * 2) / film.hours,
                  h4: INTRO + (RUN * 4) / film.hours,
                  end: T_ZOOM - 300,
                  // just after each fire is at the line
                  ...(reachH.off != null ? { lines_off: INTRO + (RUN * reachH.off) / film.hours + 150 } : {}),
                  ...(reachH.on != null ? { lines_on: INTRO + (RUN * reachH.on) / film.hours + 150 } : {}),
                  ...(truth ? { zoom_a: T_ZOOM + ZOOM * 0.35, zoom_b: T_ZOOM + ZOOM * 0.7, truth: T_CARD - 300 } : {}),
                  ...(pitch ? { card: tState - 300, ca_index: tModel - 300, ca_model: tClose - 300, final: tEnd - 300 } : {}),
                },
            poses: () => ({ near, far, wideNear: wideNear(), wideFar, half: [view.off.width, view.off.height] }),
            // what the film tells of the line, for checking against the isochrones
            line: () =>
              told && {
                kv: told.kv,
                oid: told.oid,
                fromIgnitionM: Math.min(
                  ...wires
                    .filter((w) => w.oid === told.oid)
                    .flatMap((w) => w.points.map(([x, y]) => Math.hypot(x - lonLatToMercator(film.fire.lon, film.fire.lat)[0], y - lonLatToMercator(film.fire.lon, film.fire.lat)[1]) / stretch)),
                ),
                reachedAfterH: reachH,
                reachedKm: { off: lengthKm('off', told.oid), on: lengthKm('on', told.oid) },
                reachedKmAllLines: { off: lengthKm('off'), on: lengthKm('on') },
                wires: wires.length,
              },
            settled,
            seek: (ms: number) => {
              t = ms;
              paused = true;
            },
            play: (ms = from) => {
              t = ms;
              paused = false;
            },
          },
        });
        document.documentElement.dataset.film = 'ready';
        raf = requestAnimationFrame(loop);
      } catch (e) {
        if (!dead && !abort.signal.aborted) setError(e instanceof Error ? e.message : 'The film could not be prepared');
      }
    }, 0);

    return () => {
      dead = true;
      window.clearTimeout(timer);
      cancelAnimationFrame(raf);
      abort.abort();
      views.forEach((v) => v.destroy());
    };
  }, [film]);

  const wind = `wind ${film.fire.wind.speed} m/s from ${compass(film.fire.wind.from)}`;
  const first = act === 'run' || act === 'end';
  const second = act === 'truth';
  const pitch = film.truthOnly ? undefined : film.pitch;
  const sources = film.scar
    ? `Outline: perimeters of the ${film.scar.fires}, November 2018, mapped by CAL FIRE. ${film.scar.radar}.`
    : '';
  return (
    <div
      className="film film-split"
      data-shown={act ?? 'loading'}
      ref={rootRef}
      style={{ '--show': 0 } as CSSProperties} // black until the film starts
    >
      <div className="film-half film-half-off" ref={offRef} />
      <div className="film-half film-half-on" ref={onRef} />
      <div className="film-divide" />
      <div className="film-wide" ref={wideRef} />
      <canvas className="film-fire" ref={canvasRef} aria-hidden="true" />
      <div className="film-shade film-shade-top" />
      <div className="film-shade film-shade-bottom" />
      <div className="film-shade film-shade-band" />
      <div className="film-shade film-shade-side" />
      <div className="film-dip" />

      {(first || second) && (
        <>
          <header className="film-side film-side-off">
            <h1 className="film-state">{second ? 'Optical image' : 'NISAR OFF'}</h1>
            <p className="film-sub">{second ? (film.scar?.photo ?? 'satellite photograph') : 'fuel map alone'}</p>
            {first && (
              <>
                <p className="film-readout">
                  <span ref={areaOffRef} />
                </p>
                <p className="film-reach" ref={reachOffRef} />
              </>
            )}
          </header>
          <header className="film-side film-side-on">
            <h1 className="film-state">{second ? 'NISAR L-band radar' : 'NISAR ON'}</h1>
            <p className="film-sub">{second ? 'weaker echo where the fuel is still regrowing' : 'fuel state from L-band radar'}</p>
            {first && (
              <>
                <p className="film-readout">
                  <span ref={areaOnRef} />
                </p>
                <p className="film-reach" ref={reachOnRef} />
              </>
            )}
          </header>
        </>
      )}
      {first && (
        <div className="film-centre">
          <p className="film-clock" ref={clockRef} />
          {act === 'end' && (
            <p className="film-verdict">
              <b>Same ignition, same wind</b>
              <span>{ratio}</span>
              {gain && <span>{gain}</span>}
            </p>
          )}
        </div>
      )}

      {second && film.scar && (
        <div className="film-truth">
          <p className="film-truth-head">NISAR sees the 2018 burn scar. Optical images no longer do.</p>
          <p className="film-truth-sub">
            Detection score {film.scar.detection.nisar.toFixed(2)} for NISAR, {film.scar.detection.optical.toFixed(2)} for
            optical greenness.
          </p>
        </div>
      )}

      {act === 'card' && pitch && (
        <div className="film-pitch">
          {pitch.lines.map((line, i) => (
            <p className="film-pitch-line" style={{ opacity: `var(--line${i}, 1)` }} key={line}>
              {line}
            </p>
          ))}
        </div>
      )}

      {act === 'state' && pitch && (
        <>
          <div className="film-labels">
            {labels.map((l) => (
              <span className="film-label" data-side={l.side} style={{ left: l.x, top: l.y }} key={l.text}>
                {l.text}
              </span>
            ))}
          </div>
          <div className="film-pitch">
            <div className="film-scores">
              <div className="film-score film-score-index">
                <p className="film-score-name">{pitch.index}</p>
                <p className="film-score-count">
                  <b ref={countIndexRef}>0</b> of {LARGE_FIRES.length}
                </p>
                <p className="film-score-what">large fires warned</p>
              </div>
              <div className="film-score film-score-model">
                <p className="film-score-name">{pitch.model}</p>
                <p className="film-score-count">
                  <b ref={countModelRef}>0</b> of {LARGE_FIRES.length}
                </p>
                <p className="film-score-what">large fires warned</p>
              </div>
            </div>
            <div className="film-pitch-close">
              <p className="film-pitch-tagline">{pitch.tagline}</p>
              <p className="film-pitch-team">{pitch.team}</p>
            </div>
          </div>
        </>
      )}

      <footer className="film-foot">
        <ul className="film-legend">
          {first && (
            <>
              <li>
                <i className="film-key film-key-burned" />
                burned area
              </li>
              <li>
                <svg className="film-key-cross" viewBox="0 0 16 16" aria-hidden="true">
                  <path d="M3 3l10 10M13 3L3 13" />
                </svg>
                ignition
              </li>
              {wired && (
                <>
                  <li>
                    <i className="film-key film-key-wire" />
                    transmission line
                  </li>
                  <li>
                    <i className="film-key film-key-reached" />
                    section reached by the fire
                  </li>
                </>
              )}
            </>
          )}
          {(second || act === 'card') && (
            <li>
              <i className="film-key film-key-scar" />
              burned in 2018 (mapped perimeter)
            </li>
          )}
          {(second || act === 'card') && wired && (
            <li>
              <i className="film-key film-key-wire" />
              transmission line
            </li>
          )}
          {act === 'state' && pitch && (
            <>
              <li>
                <i className="film-key film-key-model" />
                {pitch.model.toLowerCase()} warned
              </li>
              <li>
                <i className="film-key film-key-index" />
                {pitch.index.toLowerCase()} warned
              </li>
              <li>
                <i className="film-key film-key-none" />
                neither
              </li>
              <li>circle area: area burned</li>
            </>
          )}
          {act !== 'state' && ramp && (
            <li className={act === 'card' ? undefined : 'film-legend-right'}>
              NISAR fuel state: regrowth
              <i className="film-key film-key-ramp" style={{ background: ramp }} />
              mature
            </li>
          )}
        </ul>
        <p className="film-caption">
          {act === 'state' && pitch
            ? pitch.stateCaption
            : act === 'card' && pitch
              ? `${pitch.caption} ${sources}`
              : second && film.scar
                ? `${sources} Detection score: 1.0 is perfect, 0.5 is chance. ${film.scar.note ?? ''}`
                : `${film.place}, ${wind}. Surface-fire potential, no suppression; fuel-state scaling is a model assumption.` +
                  (wired ? ' Transmission lines: HIFLD public data.' : '')}
        </p>
      </footer>
      {error && <p className="film-error">{error}</p>}
    </div>
  );
}
