/**
 * Film mode, side by side (the default of `?demo=nisar-film`), in two acts on two halves of the same frame.
 * Act 1, the estimate: one fire grown at the same time from the fuel map alone ("NISAR OFF", on the bare satellite
 * image) and with the fuel state from L-band radar ("NISAR ON", on that layer); one clock, an area under each label.
 * Act 2, what is measured: the frame widens to the whole 2018 burn and its mapped outline is drawn over the
 * photograph and over the radar layer, with one line on what is measured and one on what is assumed.
 * Nothing of the app's interface is on screen. Keys: space pauses, R restarts. `speed=2` plays faster;
 * `?demo=nisar-truth` loops the second act alone; `layout=sequence` is the older one-after-the-other film (Film.tsx).
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
type Act = 'run' | 'end' | 'truth';
type Frame = 'fire' | 'scar';

interface Step {
  hours: number;
  area: number;
  polygons: number[][][][];
}
interface Scars {
  features: Array<{ geometry: { coordinates: number[][][] } }>;
}

/** Film time in ms, at speed 1. Act 1: a fade in, the run, the result held. Act 2: a dip to the wide frame, the
 *  outline drawn, the two lines held. Then a fade out before the loop. */
const INTRO = 600;
const RUN = 6_000;
const HOLD = 1_500;
const TURN = 1_200;
const REVEAL = 1_300;
const TRUTH = 3_700;
const OUT = 700;
const T_HOLD = INTRO + RUN;
const T_TURN = T_HOLD + HOLD;
const T_REVEAL = T_TURN + TURN;
const T_TRUTH = T_REVEAL + REVEAL;
const T_OUT = T_TRUTH + TRUTH;
const TOTAL = T_OUT + OUT;
const LAYER_OPACITY = 0.62;

const FIRE_FILL = 'rgba(226, 54, 28, 0.5)';
const FIRE_FRONT = '#fff3c4';
const SCAR_LINE = '#ffffff';
const CASING = 'rgba(9, 9, 11, 0.8)';
const IMAGERY = 'https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer';

const COMPASS = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'];
const compass = (deg: number) => COMPASS[Math.round((((deg % 360) + 360) % 360) / 45) % 8];
const hectares = (a: number) => (a < 10 ? a.toFixed(1) : Math.round(a).toString());
const unit = (v: number) => Math.min(1, Math.max(0, v));
const ease = (v: number) => unit(v) * unit(v) * (3 - 2 * unit(v));

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

export default function FilmSplit({ film }: { film: FilmScenario }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const offRef = useRef<HTMLDivElement>(null);
  const onRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const clockRef = useRef<HTMLParagraphElement>(null);
  const areaOffRef = useRef<HTMLSpanElement>(null);
  const areaOnRef = useRef<HTMLSpanElement>(null);
  const [act, setAct] = useState<Act | null>(null);
  const [ratio, setRatio] = useState('');
  const [ramp, setRamp] = useState('');
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
        const base = `${API_BASE}/v1/burnability/results/${film.result}`;
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
        const [meta, withNisar, alone, scars, later] = await Promise.all([
          json<BurnabilityMetadata>(base, 'result'),
          spread(true),
          spread(false),
          // A backend without the outline, or a result without fuel age, leaves the film with its first act only.
          film.scar
            ? json<Scars>(`${base}/scars?min_years=${film.scar.minYears}&max_years=${film.scar.maxYears}`, 'scars').catch(() => null)
            : Promise.resolve(null),
          // what burned after that fire: the radar shows regrowth there too, so those places are marked as well
          film.scar
            ? json<Scars>(`${base}/scars?min_years=0&max_years=${film.scar.minYears}`, 'scars').catch(() => null)
            : Promise.resolve(null),
        ]);
        if (dead) return;
        const data: Record<Variant, Step[]> = { on: steps(withNisar), off: steps(alone) };
        if (!data.on.length || !data.off.length) throw new Error('no fire at the ignition');
        const last = { on: data.on[data.on.length - 1], off: data.off[data.off.length - 1] };
        const scar = (scars?.features ?? []).map((f) => f.geometry.coordinates).filter((rings) => rings.length);
        const since = (later?.features ?? []).map((f) => f.geometry.coordinates).filter((rings) => rings.length);
        const truth = scar.length > 0;
        if (film.truthOnly && !truth) throw new Error('this result has no outline of the old fire');
        const total = truth ? TOTAL : T_TURN + OUT;
        const from = film.truthOnly ? T_TURN + TURN / 2 : 0; // where the loop starts
        const stops = meta.layers.find((l) => l.id === film.layer)?.legend?.stops ?? [];
        setRamp(stops.length ? `linear-gradient(to right, ${stops.map(([, c]) => c).join(', ')})` : '');
        setRatio(
          last.on.area <= last.off.area
            ? `${(last.off.area / last.on.area).toFixed(1)}x smaller with NISAR`
            : `${(last.on.area / last.off.area).toFixed(1)}x larger with NISAR`,
        );

        // Two still views of the same frame: the satellite image alone, and the same with NISAR's layer over it.
        const hosts: Record<Variant, HTMLDivElement> = { off: offRef.current!, on: onRef.current! };
        const view: Record<Variant, MapView> = {} as Record<Variant, MapView>;
        for (const v of ['off', 'on'] as Variant[]) {
          const map = new ArcGISMap({
            basemap: new Basemap({ baseLayers: [new TileLayer({ url: IMAGERY })] }),
            layers: v === 'on' ? [createBurnabilityRasterLayer(meta, film.layer, LAYER_OPACITY)] : [],
          });
          view[v] = new MapView({ container: hosts[v], map, constraints: { rotationEnabled: false, snapToZoom: false } });
          view[v].ui.components = [];
          view[v].on('drag', (e) => e.stopPropagation());
          view[v].on('mouse-wheel', (e) => e.stopPropagation());
          view[v].on('double-click', (e) => e.stopPropagation());
          view[v].on('key-down', (e) => e.stopPropagation());
          views.push(view[v]);
        }
        await Promise.all([view.off.when(), view.on.when()]);
        if (dead) return;

        // Two frames, each the same in both halves. Act 1: everything either fire reaches, with room for the labels
        // above and the legend below. Act 2: the whole burn of the old fire, with room for the two lines under it.
        const reach = bounds([...last.off.polygons, ...last.on.polygons].flat(2).concat([[film.fire.lon, film.fire.lat]]), 1.18);
        const frames: Record<Frame, { extent: Extent; top: number; bottom: number }> = {
          fire: { extent: reach, top: 0.25, bottom: 0.15 },
          // Tight on purpose: zoomed out any further the imagery service switches to an older, coarser picture taken
          // soon after the fire, in which the burn is plain to see; the recent photograph is what this act is about.
          scar: { extent: truth ? bounds(scar.flat(2), 1.02) : reach, top: 0.2, bottom: 0.31 },
        };
        let frameNow: Frame = 'fire';
        const place = async (frame: Frame = frameNow) => {
          frameNow = frame;
          const f = frames[frame];
          for (const v of ['off', 'on'] as Variant[]) {
            view[v].padding = { top: view[v].height * f.top, bottom: view[v].height * f.bottom, left: 0, right: 0 };
          }
          await Promise.all([view.off.goTo(f.extent, { animate: false }), view.on.goTo(f.extent, { animate: false })]);
        };
        const settled = () => !view.off.updating && !view.on.updating;
        const idle = () =>
          Promise.all([
            reactiveUtils.whenOnce(() => view.off.updating === false),
            reactiveUtils.whenOnce(() => view.on.updating === false),
          ]);
        await place('fire');

        // Screen paths in the coordinates of each half; even-odd fills leave unburned islands open.
        const paths: Record<Variant, Path2D[]> = { on: [], off: [] };
        const rings: Record<Variant, Path2D[]> = { on: [], off: [] };
        const outline: Record<Variant, Path2D> = { on: new Path2D(), off: new Path2D() };
        const newer: Record<Variant, Path2D> = { on: new Path2D(), off: new Path2D() };
        const start: Record<Variant, { x: number; y: number }> = { on: { x: 0, y: 0 }, off: { x: 0, y: 0 } };
        const project = (v: Variant) => {
          const corner = (lon: number, lat: number) => {
            const [x, y] = lonLatToMercator(lon, lat);
            return { x, y, s: view[v].toScreen(new Point({ x, y, spatialReference: SpatialReference.WebMercator }))! };
          };
          const a = corner(reach.xmin, reach.ymin);
          const b = corner(reach.xmax, reach.ymax);
          const kx = (b.s.x - a.s.x) / (b.x - a.x);
          const ky = (b.s.y - a.s.y) / (b.y - a.y);
          const screen = (lon: number, lat: number): [number, number] => {
            const [x, y] = lonLatToMercator(lon, lat);
            return [a.s.x + (x - a.x) * kx, a.s.y + (y - a.y) * ky];
          };
          const trace = (p: Path2D, ring: number[][]) => {
            ring.forEach(([lon, lat], i) => {
              const [x, y] = screen(lon, lat);
              if (i) p.lineTo(x, y);
              else p.moveTo(x, y);
            });
            p.closePath();
          };
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
          outline[v] = new Path2D();
          for (const polygon of scar) for (const ring of polygon) trace(outline[v], ring);
          newer[v] = new Path2D();
          for (const polygon of since) for (const ring of polygon) trace(newer[v], ring);
          const [x, y] = screen(film.fire.lon, film.fire.lat);
          start[v] = { x, y };
        };
        const fit = () => {
          const canvas = canvasRef.current;
          if (!canvas) return;
          const dpr = window.devicePixelRatio || 1;
          canvas.width = Math.round(canvas.clientWidth * dpr);
          canvas.height = Math.round(canvas.clientHeight * dpr);
          project('off');
          project('on');
        };
        fit();
        reactiveUtils.watch(
          () => [view.off.extent, view.on.extent, view.off.width, view.off.height],
          () => fit(),
        );
        view.off.on('resize', () => void place());

        // The tiles of both frames are fetched before the film starts, so that neither act opens on a blurred map.
        await Promise.all([idle(), document.fonts.ready]);
        if (truth) {
          await place('scar');
          await idle();
          if (!film.truthOnly) {
            await place('fire');
            await idle();
          }
        }
        if (dead) return;

        const stroke = (ctx: CanvasRenderingContext2D, p: Path2D, alpha: number, color: string, width: number) => {
          if (alpha <= 0) return;
          ctx.globalAlpha = alpha;
          ctx.lineJoin = 'round';
          ctx.strokeStyle = CASING;
          ctx.lineWidth = width + 2.5;
          ctx.stroke(p);
          ctx.strokeStyle = color;
          ctx.lineWidth = width;
          ctx.stroke(p);
        };
        const ignition = (ctx: CanvasRenderingContext2D, v: Variant, r: number) => {
          ctx.globalAlpha = 1;
          ctx.lineCap = 'round';
          for (const [color, width] of [
            [CASING, r * 0.8],
            ['#ffffff', r / 3],
          ] as [string, number][]) {
            ctx.strokeStyle = color;
            ctx.lineWidth = width;
            ctx.beginPath();
            ctx.moveTo(start[v].x - r, start[v].y - r);
            ctx.lineTo(start[v].x + r, start[v].y + r);
            ctx.moveTo(start[v].x + r, start[v].y - r);
            ctx.lineTo(start[v].x - r, start[v].y + r);
            ctx.stroke();
          }
        };
        const fire = (ctx: CanvasRenderingContext2D, v: Variant, hrs: number) => {
          const s = data[v];
          const next = s.findIndex((st) => st.hours > hrs + 1e-9);
          const k = next < 0 ? s.length - 1 : next - 1; // the last front already passed
          const from0 = k < 0 ? 0 : s[k].hours;
          const part = next < 0 ? 0 : (hrs - from0) / (s[next].hours - from0);
          ctx.fillStyle = FIRE_FILL;
          if (k >= 0) {
            ctx.globalAlpha = 1;
            ctx.fill(paths[v][k], 'evenodd');
          }
          if (next >= 0 && part > 0) {
            ctx.globalAlpha = part;
            ctx.fill(rings[v][next], 'evenodd');
          }
          if (k >= 0) stroke(ctx, paths[v][k], next < 0 ? 1 : 1 - part, FIRE_FRONT, 2);
          if (next >= 0) stroke(ctx, paths[v][next], part, FIRE_FRONT, 2);
          ignition(ctx, v, 9);
        };

        // What is on screen at film time t. `show` fades everything through black: at the loop and between the acts.
        const at = (t: number) => {
          const turn = truth ? (t - T_TURN) / TURN : -1;
          const second = turn >= 0.5;
          const out = t >= total - OUT ? (t - (total - OUT)) / OUT : 0;
          return {
            act: (second ? 'truth' : t >= T_HOLD ? 'end' : 'run') as Act,
            frame: (second ? 'scar' : 'fire') as Frame,
            hrs: unit((t - INTRO) / RUN) * film.hours,
            show: Math.min(
              from === 0 ? unit(t / INTRO) : 1,
              turn >= 0 && turn < 1 ? Math.abs(turn - 0.5) * 2 : 1,
              1 - out,
            ),
            line: ease((t - T_REVEAL) / REVEAL),
            measured: ease((t - T_REVEAL) / 600),
            assumed: ease((t - T_REVEAL - 700) / 600),
          };
        };

        let actNow: Act | null = null;
        const draw = (t: number) => {
          const canvas = canvasRef.current;
          const ctx = canvas?.getContext('2d');
          if (!canvas || !ctx) return;
          const f = at(t);
          if (f.frame !== frameNow) void place(f.frame); // under the black of the dip, so the cut is not seen
          const dpr = window.devicePixelRatio || 1;
          const half = canvas.clientWidth / 2;
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
          ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
          (['off', 'on'] as Variant[]).forEach((v, i) => {
            ctx.save();
            ctx.beginPath();
            ctx.rect(i * half, 0, half, canvas.clientHeight);
            ctx.clip();
            ctx.translate(i * half, 0);
            if (f.act !== 'truth') fire(ctx, v, f.hrs);
            else {
              // where the simulated fire of act 1 lies in the old burn, then the burn's own outline
              if (!film.truthOnly) {
                stroke(ctx, paths[v][paths[v].length - 1], 0.9, FIRE_FRONT, 1.6);
                ignition(ctx, v, 6);
              }
              ctx.setLineDash([7, 6]);
              stroke(ctx, newer[v], f.line * 0.85, SCAR_LINE, 1.4);
              ctx.setLineDash([]);
              stroke(ctx, outline[v], f.line, SCAR_LINE, 3);
            }
            ctx.restore();
          });
          const root = rootRef.current;
          if (root) {
            root.style.setProperty('--show', f.show.toFixed(3));
            root.style.setProperty('--measured', f.measured.toFixed(3));
            root.style.setProperty('--assumed', f.assumed.toFixed(3));
          }
          if (clockRef.current) clockRef.current.textContent = clock(f.hrs);
          if (areaOffRef.current) areaOffRef.current.textContent = `${hectares(areaAt(data.off, f.hrs))} ha`;
          if (areaOnRef.current) areaOnRef.current.textContent = `${hectares(areaAt(data.on, f.hrs))} ha`;
          if (f.act !== actNow) {
            actNow = f.act;
            setAct(f.act);
          }
        };

        let t = still ? (truth ? T_TRUTH + 500 : T_HOLD + 500) : from;
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
            marks: {
              start: INTRO + 100,
              h2: INTRO + (RUN * 2) / film.hours,
              h4: INTRO + (RUN * 4) / film.hours,
              end: T_TURN - 300,
              ...(truth ? { photo: T_REVEAL - 50, truth: T_OUT - 300 } : {}),
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
  const second = act === 'truth';
  return (
    <div className="film film-split" data-shown={act ?? 'loading'} ref={rootRef}>
      <div className="film-half film-half-off" ref={offRef} />
      <div className="film-half film-half-on" ref={onRef} />
      <canvas className="film-fire" ref={canvasRef} aria-hidden="true" />
      <div className="film-shade film-shade-top" />
      <div className="film-shade film-shade-bottom" />
      <div className="film-divide" />
      <div className="film-dip" />

      <header className="film-side film-side-off">
        <h1 className="film-state">{second ? 'Optical image' : 'NISAR OFF'}</h1>
        <p className="film-sub">{second ? (film.scar?.photo ?? 'satellite photograph') : 'fuel map alone'}</p>
        {!second && (
          <p className="film-readout">
            <span ref={areaOffRef} />
          </p>
        )}
      </header>
      <header className="film-side film-side-on">
        <h1 className="film-state">{second ? 'NISAR L-band radar' : 'NISAR ON'}</h1>
        <p className="film-sub">{second ? 'weaker echo where the fuel is still regrowing' : 'fuel state from L-band radar'}</p>
        {!second && (
          <p className="film-readout">
            <span ref={areaOnRef} />
          </p>
        )}
      </header>
      {!second && (
        <div className="film-centre">
          <p className="film-clock" ref={clockRef} />
          {act === 'end' && (
            <p className="film-verdict">
              <b>Same ignition, same wind</b>
              <span>{ratio}</span>
            </p>
          )}
        </div>
      )}

      {second && film.scar && (
        <div className="film-truth">
          <p className="film-truth-measured">
            <b>Measured</b>
            <span>
              NISAR sees the 2018 burn scar. Detection {film.scar.detection.nisar.toFixed(2)}, optical greenness{' '}
              {film.scar.detection.optical.toFixed(2)}.
            </span>
            <small>Outline: mapped perimeters of the {film.scar.fires}, 2018. 1.0 is perfect, 0.5 is chance.</small>
          </p>
          <p className="film-truth-assumed">
            <b>Assumed</b>
            <span>How much slower a fire runs in that regrowth. No fire has tested it yet.</span>
          </p>
        </div>
      )}

      <footer className="film-foot">
        <ul className="film-legend">
          {second ? (
            <>
              <li>
                <i className="film-key film-key-scar" />
                burned in 2018 (mapped perimeter)
              </li>
              <li>
                <i className="film-key film-key-newer" />
                burned since
              </li>
              {!film.truthOnly && (
                <li>
                  <i className="film-key film-key-outline" />
                  simulated fire
                </li>
              )}
            </>
          ) : (
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
            </>
          )}
          {ramp && (
            <li className="film-legend-right">
              NISAR fuel state: regrowth
              <i className="film-key film-key-ramp" style={{ background: ramp }} />
              mature
            </li>
          )}
        </ul>
        <p className="film-caption">
          {second && film.scar
            ? `Santa Monica Mountains. Outline from mapped fire perimeters (CAL FIRE FRAP, NIFC). ${film.scar.radar}. ` +
              'Detection: cells burned in November 2018 against unburned cells.'
            : `${film.place}, ${wind}. Surface-fire potential, no suppression; fuel-state scaling is a model assumption.`}
        </p>
      </footer>
      {error && <p className="film-error">{error}</p>}
    </div>
  );
}
