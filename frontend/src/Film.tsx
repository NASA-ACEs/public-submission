/**
 * Film mode: a still map and one fire grown twice, from the fuel map alone and with NISAR's fuel state. Nothing of
 * the app's interface is on screen: a state label, a clock, a legend and a caption. Made to be recorded.
 * Keys: space pauses, R restarts, N jumps to the other estimate.
 */
import '@arcgis/core/assets/esri/themes/dark/main.css';
import Basemap from '@arcgis/core/Basemap.js';
import * as reactiveUtils from '@arcgis/core/core/reactiveUtils.js';
import Extent from '@arcgis/core/geometry/Extent.js';
import Point from '@arcgis/core/geometry/Point.js';
import SpatialReference from '@arcgis/core/geometry/SpatialReference.js';
import TileLayer from '@arcgis/core/layers/TileLayer.js';
import ArcGISMap from '@arcgis/core/Map.js';
import MapView from '@arcgis/core/views/MapView.js';
import { useEffect, useRef, useState } from 'react';
import './App.css';
import './film.css';
import type { FilmScenario } from './demo';
import { API_BASE } from './grid/api';
import { createBurnabilityRasterLayer, type BurnabilityMetadata, type FireGrowth } from './grid/burnability';
import { lonLatToMercator } from './grid/geo';

type Variant = 'on' | 'off';
type Shown = Variant | 'compare';

interface Step {
  hours: number;
  area: number;
  polygons: number[][][][];
}

/** Film time in ms, at speed 1. */
const INTRO = 900;
const RUN = 10_000;
const HOLD = 2_000;
const SWAP = 1_000;
const COMPARE = 3_500;
const OUT = 700;
const ONE = INTRO + RUN + HOLD;
const TOTAL = ONE + SWAP + ONE + COMPARE + OUT;
const LAYER_OPACITY = 0.62;

const FIRE_FILL = 'rgba(226, 54, 28, 0.5)';
const FIRE_FRONT = '#fff3c4';
const CASING = 'rgba(9, 9, 11, 0.8)';

const COMPASS = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'];
const compass = (deg: number) => COMPASS[Math.round((((deg % 360) + 360) % 360) / 45) % 8];

const hectares = (a: number) => (a < 10 ? a.toFixed(1) : Math.round(a).toString());

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

/** What the film shows at film time `t`. */
function frame(t: number, order: [Variant, Variant], total: number) {
  const run = (local: number, variant: Variant) => ({
    variant,
    shown: variant as Shown,
    hours: Math.min(1, Math.max(0, (local - INTRO) / RUN)) * total,
    fire: 1,
    layer: variant === 'on' ? 1 : 0,
    compare: 0,
  });
  if (t < ONE) return run(t, order[0]);
  if (t < ONE + SWAP) {
    const k = (t - ONE) / SWAP;
    const from = order[0] === 'on' ? 1 : 0;
    const to = order[1] === 'on' ? 1 : 0;
    return k < 0.5
      ? { ...run(ONE, order[0]), fire: 1 - k * 2, layer: from + (to - from) * k }
      : { ...run(0, order[1]), fire: 0, layer: from + (to - from) * k };
  }
  if (t < ONE + SWAP + ONE) return run(t - ONE - SWAP, order[1]);
  const last = order[1] === 'on' ? 1 : 0;
  if (t < ONE + SWAP + ONE + COMPARE) {
    const k = Math.min(1, (t - ONE - SWAP - ONE) / 500);
    return { variant: order[1], shown: 'compare' as Shown, hours: total, fire: 1 - k, layer: last + (1 - last) * k, compare: k };
  }
  const k = Math.min(1, (t - (TOTAL - OUT)) / OUT);
  const first = order[0] === 'on' ? 1 : 0;
  return { variant: order[1], shown: 'compare' as Shown, hours: total, fire: 0, layer: 1 + (first - 1) * k, compare: 1 - k };
}

export default function Film({ film }: { film: FilmScenario }) {
  const mapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const clockRef = useRef<HTMLSpanElement>(null);
  const areaRef = useRef<HTMLSpanElement>(null);
  const [shown, setShown] = useState<Shown | null>(null);
  const [finals, setFinals] = useState<{ on: number; off: number } | null>(null);
  const [ramp, setRamp] = useState<string>('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    const order: [Variant, Variant] = query.get('order') === 'on-off' ? ['on', 'off'] : ['off', 'on'];
    const speed = Math.min(4, Math.max(0.25, Number(query.get('speed')) || 1));
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const abort = new AbortController();
    let view: MapView | null = null;
    let raf = 0;
    let dead = false;

    const timer = window.setTimeout(async () => {
      try {
        const base = `${API_BASE}/v1/burnability/results/${film.result}`;
        const count = Math.round((film.hours * 60) / film.stepMinutes);
        const hours = Array.from({ length: count }, (_, k) => (((k + 1) * film.stepMinutes) / 60).toFixed(4)).join(',');
        const spread = (fuelState: boolean) =>
          fetch(
            `${base}/spread?lon=${film.fire.lon}&lat=${film.fire.lat}&hours=${hours}&wind_speed=${film.fire.wind.speed}` +
              `&wind_from=${film.fire.wind.from}${fuelState ? '' : '&fuel_state=false'}`,
            { signal: abort.signal },
          ).then((r) => (r.ok ? (r.json() as Promise<FireGrowth>) : Promise.reject(new Error(`fire growth ${r.status}`))));
        const [meta, withNisar, alone] = await Promise.all([
          fetch(base, { signal: abort.signal }).then((r) =>
            r.ok ? (r.json() as Promise<BurnabilityMetadata>) : Promise.reject(new Error(`result ${r.status}`)),
          ),
          spread(true),
          spread(false),
        ]);
        if (dead) return;
        const data: Record<Variant, Step[]> = { on: steps(withNisar), off: steps(alone) };
        if (!data.on.length || !data.off.length) throw new Error('no fire at the ignition');
        const stops = meta.layers.find((l) => l.id === film.layer)?.legend?.stops ?? [];
        setRamp(stops.length ? `linear-gradient(to right, ${stops.map(([, c]) => c).join(', ')})` : '');
        setFinals({ on: data.on[data.on.length - 1].area, off: data.off[data.off.length - 1].area });

        const layer = createBurnabilityRasterLayer(meta, film.layer, 0);
        const map = new ArcGISMap({
          basemap: new Basemap({
            baseLayers: [new TileLayer({ url: 'https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer' })],
          }),
          layers: [layer],
        });
        view = new MapView({ container: mapRef.current!, map, constraints: { rotationEnabled: false, snapToZoom: false } });
        view.ui.components = [];
        // A still frame: the map does not answer the mouse or the keys.
        view.on('drag', (e) => e.stopPropagation());
        view.on('mouse-wheel', (e) => e.stopPropagation());
        view.on('double-click', (e) => e.stopPropagation());
        view.on('key-down', (e) => e.stopPropagation());
        await view.when();
        if (dead) return;

        // Everything either fire reaches, with room for the label above and the legend below.
        const all = [...data.off[data.off.length - 1].polygons, ...data.on[data.on.length - 1].polygons].flat(2);
        const xs = all.map((p) => p[0]).concat(film.fire.lon);
        const ys = all.map((p) => p[1]).concat(film.fire.lat);
        const reach = new Extent({
          xmin: Math.min(...xs),
          xmax: Math.max(...xs),
          ymin: Math.min(...ys),
          ymax: Math.max(...ys),
          spatialReference: SpatialReference.WGS84,
        }).expand(1.22);
        const place = async () => {
          if (!view) return;
          view.padding = { top: view.height * 0.2, bottom: view.height * 0.12, left: 0, right: 0 };
          await view.goTo(reach, { animate: false });
        };
        await place();

        // Screen paths of every front, rebuilt when the view changes. Even-odd fills leave unburned islands open.
        let paths: Record<Variant, Path2D[]> = { on: [], off: [] };
        let rings: Record<Variant, Path2D[]> = { on: [], off: [] };
        let start = { x: 0, y: 0 };
        const project = () => {
          if (!view) return;
          const corner = (lon: number, lat: number) => {
            const [x, y] = lonLatToMercator(lon, lat);
            return { x, y, s: view!.toScreen(new Point({ x, y, spatialReference: SpatialReference.WebMercator }))! };
          };
          const a = corner(reach.xmin, reach.ymin);
          const b = corner(reach.xmax, reach.ymax);
          const kx = (b.s.x - a.s.x) / (b.x - a.x);
          const ky = (b.s.y - a.s.y) / (b.y - a.y);
          const screen = (lon: number, lat: number): [number, number] => {
            const [x, y] = lonLatToMercator(lon, lat);
            return [a.s.x + (x - a.x) * kx, a.s.y + (y - a.y) * ky];
          };
          const path = (polygons: number[][][][]) => {
            const p = new Path2D();
            for (const polygon of polygons)
              for (const ring of polygon) {
                ring.forEach(([lon, lat], i) => {
                  const [x, y] = screen(lon, lat);
                  if (i) p.lineTo(x, y);
                  else p.moveTo(x, y);
                });
                p.closePath();
              }
            return p;
          };
          for (const v of ['on', 'off'] as Variant[]) {
            paths[v] = data[v].map((s) => path(s.polygons));
            rings[v] = paths[v].map((p, i) => {
              const ring = new Path2D(p);
              if (i) ring.addPath(paths[v][i - 1]);
              return ring;
            });
          }
          const [x, y] = screen(film.fire.lon, film.fire.lat);
          start = { x, y };
        };
        const fit = () => {
          const canvas = canvasRef.current;
          if (!canvas || !view) return;
          const dpr = window.devicePixelRatio || 1;
          canvas.width = Math.round(view.width * dpr);
          canvas.height = Math.round(view.height * dpr);
          project();
        };
        fit();
        reactiveUtils.watch(
          () => [view?.extent, view?.width, view?.height],
          () => fit(),
        );
        view.on('resize', () => void place());

        // Tiles of the NISAR layer are fetched now, invisible, so that the layer fades in without gaps later.
        await Promise.all([reactiveUtils.whenOnce(() => view?.updating === false), document.fonts.ready]);
        if (dead) return;

        const front = (ctx: CanvasRenderingContext2D, p: Path2D, alpha: number, dash = false) => {
          if (alpha <= 0) return;
          ctx.globalAlpha = alpha;
          ctx.setLineDash([]);
          ctx.lineJoin = 'round';
          ctx.strokeStyle = CASING;
          ctx.lineWidth = dash ? 5 : 4.5;
          ctx.stroke(p);
          ctx.setLineDash(dash ? [10, 7] : []);
          ctx.strokeStyle = dash ? '#fafafa' : FIRE_FRONT;
          ctx.lineWidth = dash ? 2.4 : 2;
          ctx.stroke(p);
          ctx.setLineDash([]);
        };
        const fire = (ctx: CanvasRenderingContext2D, v: Variant, hours: number, alpha: number) => {
          if (alpha <= 0) return;
          const s = data[v];
          const next = s.findIndex((st) => st.hours > hours + 1e-9);
          const k = next < 0 ? s.length - 1 : next - 1; // the last front already passed
          const from = k < 0 ? 0 : s[k].hours;
          const part = next < 0 ? 0 : (hours - from) / (s[next].hours - from);
          ctx.fillStyle = FIRE_FILL;
          if (k >= 0) {
            ctx.globalAlpha = alpha;
            ctx.fill(paths[v][k], 'evenodd');
          }
          if (next >= 0 && part > 0) {
            ctx.globalAlpha = alpha * part;
            ctx.fill(rings[v][next], 'evenodd');
          }
          if (k >= 0) front(ctx, paths[v][k], alpha * (next < 0 ? 1 : 1 - part));
          if (next >= 0) front(ctx, paths[v][next], alpha * part);
        };
        const ignition = (ctx: CanvasRenderingContext2D) => {
          const r = 9;
          ctx.globalAlpha = 1;
          ctx.lineCap = 'round';
          for (const [color, width] of [
            [CASING, 7],
            ['#ffffff', 3],
          ] as [string, number][]) {
            ctx.strokeStyle = color;
            ctx.lineWidth = width;
            ctx.beginPath();
            ctx.moveTo(start.x - r, start.y - r);
            ctx.lineTo(start.x + r, start.y + r);
            ctx.moveTo(start.x + r, start.y - r);
            ctx.lineTo(start.x - r, start.y + r);
            ctx.stroke();
          }
        };

        let shownNow: Shown | null = null;
        const draw = (t: number) => {
          const canvas = canvasRef.current;
          const ctx = canvas?.getContext('2d');
          if (!canvas || !ctx) return;
          const f = frame(t, order, film.hours);
          const dpr = window.devicePixelRatio || 1;
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
          ctx.clearRect(0, 0, canvas.width, canvas.height);
          fire(ctx, f.variant, f.hours, f.fire);
          if (f.compare > 0) {
            // Both final fronts at once: NISAR's filled, the fuel map's as a dashed outline.
            fire(ctx, 'on', film.hours, f.compare);
            front(ctx, paths.off[paths.off.length - 1], f.compare, true);
          }
          ignition(ctx);
          layer.opacity = LAYER_OPACITY * f.layer;
          if (clockRef.current) clockRef.current.textContent = clock(f.hours);
          if (areaRef.current) areaRef.current.textContent = `${hectares(areaAt(data[f.variant], f.hours))} ha`;
          if (f.shown !== shownNow) {
            shownNow = f.shown;
            setShown(f.shown);
          }
        };

        // Film time advances with the screen clock unless paused; keys and the recording hook move it.
        let t = still ? ONE + SWAP + ONE + 600 : 0;
        let paused = still;
        let before = performance.now();
        const loop = (now: number) => {
          if (!paused) t = (t + (now - before) * speed) % TOTAL;
          before = now;
          draw(t);
          raf = requestAnimationFrame(loop);
        };
        const keys = (e: KeyboardEvent) => {
          if (e.code === 'Space') paused = !paused;
          else if (e.key === 'r' || e.key === 'R') t = 0;
          else if (e.key === 'n' || e.key === 'N') t = t < ONE + SWAP ? ONE + SWAP : 0;
          else return;
          e.preventDefault();
        };
        window.addEventListener('keydown', keys);
        abort.signal.addEventListener('abort', () => window.removeEventListener('keydown', keys));
        // For a recording script: put the film at a moment and hold it there, or let it run from the start.
        Object.assign(window, {
          __film: {
            duration: TOTAL / speed,
            marks: {
              first_start: INTRO * 0.5,
              first_half: INTRO + RUN / 2,
              first_end: ONE - 300,
              second_half: ONE + SWAP + INTRO + RUN / 2,
              second_end: ONE + SWAP + ONE - 300,
              compare: ONE + SWAP + ONE + COMPARE - 800,
            },
            seek: (ms: number) => {
              t = ms;
              paused = true;
            },
            play: (ms = 0) => {
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
      view?.destroy();
    };
  }, [film]);

  const wind = `wind ${film.fire.wind.speed} m/s from ${compass(film.fire.wind.from)}`;
  return (
    <div className="film" data-shown={shown ?? 'loading'}>
      <div className="film-map" ref={mapRef} />
      <canvas className="film-fire" ref={canvasRef} aria-hidden="true" />
      <div className="film-shade film-shade-top" />
      <div className="film-shade film-shade-bottom" />

      <header className="film-head">
        {shown === 'compare' && finals ? (
          <>
            <h1 className="film-state">Same ignition, same wind</h1>
            <div className="film-pair">
              <p>
                <b>{hectares(finals.on)} ha</b>
                <span>with NISAR</span>
              </p>
              <p>
                <b>{hectares(finals.off)} ha</b>
                <span>without</span>
              </p>
            </div>
          </>
        ) : (
          <>
            <h1 className="film-state">{shown === 'on' ? 'NISAR ON' : shown === 'off' ? 'NISAR OFF' : ''}</h1>
            <p className="film-sub">
              {shown === 'on' ? 'fuel state from L-band radar' : shown === 'off' ? 'fuel map alone' : ''}
            </p>
            <p className="film-readout" hidden={!shown}>
              <span ref={clockRef} />
              <span ref={areaRef} />
            </p>
          </>
        )}
      </header>

      <footer className="film-foot">
        <ul className="film-legend">
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
          {shown === 'compare' && (
            <li>
              <i className="film-key film-key-other" />
              final extent, other estimate
            </li>
          )}
          {(shown === 'on' || shown === 'compare') && ramp && (
            <li>
              NISAR fuel state: regrowth
              <i className="film-key film-key-ramp" style={{ background: ramp }} />
              mature
            </li>
          )}
        </ul>
        <p className="film-caption">
          {film.place}, {wind}. Surface-fire potential, no suppression; fuel-state scaling is a model assumption.
        </p>
      </footer>
      {error && <p className="film-error">{error}</p>}
    </div>
  );
}
