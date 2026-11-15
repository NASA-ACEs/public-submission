/**
 * Film mode `?demo=lines-film`: California's transmission lines against the model's risk map, every 200 m along every
 * line. Nothing of the app's interface is on screen. The state stands in the left half: first the lines alone, thin
 * and pale; then the risk map comes in under them and the pieces of line in the top two risk classes light up, with
 * their length. The view pushes in to the stretch of the state where such pieces are densest, holds, pulls back, and
 * closes on a card. The pieces and their classes are worked out beforehand from the backend's own sampler
 * (`public/film/lines-ca-20261020.json`, made by `scripts/line_risk.py` in the backend repository).
 * Keys: space pauses, R restarts. `speed=2` plays faster. `window.__film.seek(ms)` holds a moment for a recording.
 */
import '@arcgis/core/assets/esri/themes/dark/main.css';
import Basemap from '@arcgis/core/Basemap.js';
import * as reactiveUtils from '@arcgis/core/core/reactiveUtils.js';
import Extent from '@arcgis/core/geometry/Extent.js';
import Point from '@arcgis/core/geometry/Point.js';
import SpatialReference from '@arcgis/core/geometry/SpatialReference.js';
import TileLayer from '@arcgis/core/layers/TileLayer.js';
import ArcGISMap from '@arcgis/core/Map.js';
import Viewpoint from '@arcgis/core/Viewpoint.js';
import MapView from '@arcgis/core/views/MapView.js';
import { useEffect, useRef, useState, type CSSProperties } from 'react';
import './App.css';
import './film.css';
import type { LinesFilm } from './demo';
import { API_BASE } from './grid/api';
import { createBurnabilityRasterLayer, type BurnabilityMetadata } from './grid/burnability';
import { lonLatToMercator } from './grid/geo';

interface Pose {
  x: number;
  y: number;
  scale: number;
  res: number;
}
interface LinesData {
  summary: {
    lines: number;
    km_total: number;
    km_covered: number;
    km_flagged: number;
    region: { lon: number; lat: number; width_km: number; km: number; km_covered: number; km_flagged: number };
  };
  /** [class, x0, y0, dx, dy, ...]: one run of pieces of one class, in units of 10 m of Web Mercator. */
  runs: number[][];
}

/** Film time in ms, at speed 1. */
const INTRO = 600;
const T_LINES = 300; // the lines come in
const T_RISK = 2_400; // the risk map and the flagged pieces
const RISK = 1_600;
const T_COUNT = 2_600;
const COUNT = 2_000;
const T_PUSH = 8_200; // the push in to the region,
const PUSH = 2_600;
const T_PULL = 14_800; // held, then the pull back
const PULL = 2_200;
const T_CARD = 17_300; // the card's lines
const LINE = 1_100;
const FADE = 600;
const STAY = 3_600;
const OUT = 700;

const IMAGERY = 'https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer';
const PALE = '#e6ecfa';
const CLASS4 = '#ff8a3d';
const CLASS5 = '#ff3b1f';
const CASING = 'rgba(9, 9, 11, 0.85)';
const RISK_OPACITY = 0.34;
const unit = (v: number) => Math.min(1, Math.max(0, v));
const ease = (v: number) => unit(v) * unit(v) * (3 - 2 * unit(v));
const km = (v: number) => Math.round(v).toLocaleString('en-US');

function between(near: Pose, far: Pose, k: number): Pose {
  if (far.scale === near.scale) return near;
  const scale = near.scale * Math.pow(far.scale / near.scale, k);
  const stay = (far.scale - scale) / (far.scale - near.scale);
  return { x: far.x + (near.x - far.x) * stay, y: far.y + (near.y - far.y) * stay, scale, res: (near.res * scale) / near.scale };
}

export default function FilmLines({ film }: { film: LinesFilm }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const countRef = useRef<HTMLElement>(null);
  const [act, setAct] = useState<'state' | 'region' | 'card' | null>(null);
  const [sum, setSum] = useState<LinesData['summary'] | null>(null);
  const [classes, setClasses] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    const speed = Math.min(4, Math.max(0.25, Number(query.get('speed')) || 1));
    const abort = new AbortController();
    let view: MapView | null = null;
    let raf = 0;
    let dead = false;

    const timer = window.setTimeout(async () => {
      try {
        const get = <T,>(url: string, what: string) =>
          fetch(url, { signal: abort.signal }).then((r) => (r.ok ? (r.json() as Promise<T>) : Promise.reject(new Error(`${what} ${r.status}`))));
        const [meta, data] = await Promise.all([
          get<BurnabilityMetadata>(`${API_BASE}/v1/burnability/results/${film.result}`, 'result'),
          get<LinesData>(film.data, 'lines'),
        ]);
        if (dead) return;
        setSum(data.summary);
        setClasses((meta.layers.find((l) => l.id === film.layer)?.legend?.stops ?? []).map(([, c]) => c));

        const photo = new TileLayer({ url: IMAGERY, opacity: 0.38 });
        const risk = createBurnabilityRasterLayer(meta, film.layer, 0);
        const [w, s, e, n] = meta.bbox_wgs84;
        const all = new Extent({ xmin: w, ymin: s, xmax: e, ymax: n, spatialReference: SpatialReference.WGS84 });
        view = new MapView({
          container: mapRef.current!,
          map: new ArcGISMap({ basemap: new Basemap({ baseLayers: [photo] }), layers: [risk] }),
          extent: all,
          constraints: { rotationEnabled: false, snapToZoom: false },
        });
        view.ui.components = [];
        for (const ev of ['drag', 'mouse-wheel', 'double-click', 'key-down'] as const) view.on(ev, (x) => x.stopPropagation());
        await view.when();
        if (dead) return;
        const v = view;

        // The state in the left half, the right half for the words; the region over the whole screen.
        let state: Pose = { x: 0, y: 0, scale: 1, res: 1 };
        let region = state;
        const measure = async () => {
          v.padding = { top: v.height * 0.05, bottom: v.height * 0.13, left: v.width * 0.02, right: v.width * 0.47 };
          await v.goTo(all, { animate: false });
          const m = v.toMap({ x: v.width / 2, y: v.height / 2 })!;
          state = { x: m.x, y: m.y, scale: v.scale, res: v.resolution };
          v.padding = { top: 0, right: 0, bottom: 0, left: 0 };
          const r = data.summary.region;
          const [rx, ry] = lonLatToMercator(r.lon, r.lat);
          const res = (r.width_km * 1_000) / Math.cos((r.lat * Math.PI) / 180) / v.width;
          region = { x: rx, y: ry, res, scale: (state.scale * res) / state.res };
        };
        await measure();
        let kNow = -1;
        const go = (k: number, force = false) => {
          if (!force && Math.abs(k - kNow) < 1e-6) return;
          kNow = k;
          const p = between(state, region, k);
          v.viewpoint = new Viewpoint({ targetGeometry: new Point({ x: p.x, y: p.y, spatialReference: SpatialReference.WebMercator }), scale: p.scale });
        };

        // Paths in pixels of the state frame, one per look: pale (classes 1 to 3), no model value, class 4, class 5.
        const paths = { pale: new Path2D(), none: new Path2D(), c4: new Path2D(), c5: new Path2D() };
        const build = () => {
          const W = v.width;
          const H = v.height;
          for (const key of Object.keys(paths) as (keyof typeof paths)[]) paths[key] = new Path2D();
          for (const run of data.runs) {
            const cls = run[0];
            const p = cls >= 5 ? paths.c5 : cls === 4 ? paths.c4 : cls === 0 ? paths.none : paths.pale;
            let x = 0;
            let y = 0;
            for (let i = 1; i < run.length; i += 2) {
              x += run[i] * 10;
              y += run[i + 1] * 10;
              const sx = W / 2 + (x - state.x) / state.res;
              const sy = H / 2 - (y - state.y) / state.res;
              if (i === 1) p.moveTo(sx, sy);
              else p.lineTo(sx, sy);
            }
          }
          const canvas = canvasRef.current;
          if (canvas) {
            const dpr = window.devicePixelRatio || 1;
            canvas.width = Math.round(canvas.clientWidth * dpr);
            canvas.height = Math.round(canvas.clientHeight * dpr);
          }
        };
        build();
        go(0, true);
        const settled = () => !v.updating;
        const idle = async () => {
          await new Promise((done) => window.setTimeout(done, 80));
          await reactiveUtils.whenOnce(() => v.updating === false);
        };
        // Tiles of the whole path, with the risk map on, before the film starts.
        risk.opacity = RISK_OPACITY;
        for (const k of [0, 0.2, 0.4, 0.6, 0.8, 1, 0]) {
          go(k);
          await Promise.all([idle(), document.fonts.ready]);
          if (dead) return;
        }

        const stroke = (ctx: CanvasRenderingContext2D, p: Path2D, color: string, width: number, alpha: number, u: number, casing = 0, glow = 0) => {
          if (alpha <= 0) return;
          ctx.globalAlpha = alpha;
          if (casing) {
            ctx.strokeStyle = CASING;
            ctx.lineWidth = (width + casing) * u;
            ctx.stroke(p);
          }
          if (glow) {
            ctx.shadowColor = color;
            ctx.shadowBlur = glow;
          }
          ctx.strokeStyle = color;
          ctx.lineWidth = width * u;
          ctx.stroke(p);
          ctx.shadowBlur = 0;
        };

        const total = T_CARD + LINE * 2 + FADE + STAY + OUT;
        const at = (t: number) => {
          const push = ease((t - T_PUSH) / PUSH);
          const pull = ease((t - T_PULL) / PULL);
          const k = push * (1 - pull);
          const act = (t >= T_PULL + PULL / 2 ? 'card' : t >= T_PUSH + PUSH / 2 ? 'region' : 'state') as 'state' | 'region' | 'card';
          return {
            t,
            act,
            k,
            show: Math.min(unit(t / INTRO), 1 - unit((t - (total - OUT)) / OUT)),
            lines: ease((t - T_LINES) / 1_200),
            risk: ease((t - T_RISK) / RISK),
            count: ease((t - T_COUNT) / COUNT),
            // the right column leaves for the push in and comes back with the pull back
            side: 1 - ease((t - T_PUSH) / 700) + ease((t - T_PULL - PULL + 600) / 600),
            head: ease((t - T_PUSH - PUSH + 200) / 600) * (1 - ease((t - T_PULL) / 500)),
            card: [0, 1].map((i) => ease((t - T_CARD - i * LINE) / FADE)),
          };
        };

        let actNow: string | null = null;
        const draw = (t: number) => {
          const canvas = canvasRef.current;
          const ctx = canvas?.getContext('2d');
          if (!canvas || !ctx) return;
          const f = at(t);
          go(f.k);
          risk.opacity = RISK_OPACITY * f.risk;
          const p = between(state, region, f.k);
          const a = state.res / p.res;
          const W = canvas.clientWidth;
          const H = canvas.clientHeight;
          const dpr = window.devicePixelRatio || 1;
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
          ctx.clearRect(0, 0, W, H);
          ctx.translate((W / 2) * (1 - a) + (state.x - p.x) / p.res, (H / 2) * (1 - a) - (state.y - p.y) / p.res);
          ctx.scale(a, a);
          const u = 1 / a;
          const zoom = Math.min(1, Math.log(a) / Math.log(region.scale ? state.scale / region.scale : 2)); // 0 on the state, 1 on the region
          ctx.lineJoin = 'round';
          ctx.lineCap = 'round';
          const thin = 0.9 + 0.7 * zoom;
          stroke(ctx, paths.none, PALE, thin, 0.32 * f.lines, u);
          stroke(ctx, paths.pale, PALE, thin, 0.62 * f.lines, u);
          // the flagged pieces: pale like the rest until the risk map comes in, then ember
          stroke(ctx, paths.c4, PALE, thin, 0.62 * f.lines * (1 - f.risk), u);
          stroke(ctx, paths.c5, PALE, thin, 0.62 * f.lines * (1 - f.risk), u);
          const bold = 1.9 + 1.6 * zoom;
          stroke(ctx, paths.c4, CLASS4, bold, f.risk, u, 2, 5 + 5 * zoom);
          stroke(ctx, paths.c5, CLASS5, bold + 0.4, f.risk, u, 2, 6 + 6 * zoom);
          const root = rootRef.current;
          if (root) {
            const set = (name: string, value: number) => root.style.setProperty(name, value.toFixed(3));
            set('--show', f.show);
            set('--risk', f.risk);
            set('--count', f.count);
            set('--side', unit(f.side));
            set('--head', f.head);
            set('--card0', f.card[0]);
            set('--card1', f.card[1]);
          }
          if (countRef.current) countRef.current.textContent = km(data.summary.km_flagged * f.count);
          if (f.act !== actNow) {
            actNow = f.act;
            setAct(f.act);
          }
        };

        let t = 0;
        let paused = false;
        let before = performance.now();
        const loop = (now: number) => {
          if (!paused) {
            t += (now - before) * speed;
            if (t >= total) t %= total;
          }
          before = now;
          draw(t);
          raf = requestAnimationFrame(loop);
        };
        const keys = (e: KeyboardEvent) => {
          if (e.code === 'Space') paused = !paused;
          else if (e.key === 'r' || e.key === 'R') t = 0;
          else return;
          e.preventDefault();
        };
        window.addEventListener('keydown', keys);
        abort.signal.addEventListener('abort', () => window.removeEventListener('keydown', keys));
        Object.assign(window, {
          __film: {
            layout: 'lines',
            from: 0,
            duration: total / speed,
            marks: {
              lines: T_RISK - 300,
              flagged: T_COUNT + COUNT + 1_500,
              zoom: T_PUSH + PUSH / 2,
              region: T_PULL - 300,
              final: total - OUT - 300,
            },
            settled,
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

  const r = sum?.region;
  return (
    <div className="film film-lines" data-shown={act ?? 'loading'} ref={rootRef} style={{ '--show': 0 } as CSSProperties}>
      <div className="film-map" ref={mapRef} />
      <canvas className="film-fire" ref={canvasRef} aria-hidden="true" />
      <div className="film-shade film-shade-top" />
      <div className="film-shade film-shade-side film-lines-side" />
      <div className="film-shade film-lines-bottom" />
      <div className="film-dip" />

      {sum && (
        <>
          <div className="film-pitch film-lines-col">
            <p className="film-lines-kicker">California transmission lines</p>
            <div className="film-lines-count">
              <p className="film-score-count">
                <b ref={countRef}>0</b> km
              </p>
              <p className="film-lines-of">of {km(sum.km_covered)} km of line on rated land are in the top two risk classes</p>
            </div>
            <div className="film-lines-card">
              <p className="film-pitch-line" style={{ opacity: 'var(--card0, 0)' }}>
                {film.lines[0]}
              </p>
              <div className="film-pitch-close" style={{ opacity: 'var(--card1, 0)' }}>
                <p className="film-pitch-tagline">{film.tagline}</p>
                <p className="film-pitch-team">{film.team}</p>
              </div>
            </div>
          </div>
          {r && (
            <header className="film-lines-head">
              <h1 className="film-state">{film.region}</h1>
              <p className="film-sub">
                {km(r.km_flagged)} of {km(r.km_covered)} km of line on rated land in this view are in the top two risk classes
              </p>
            </header>
          )}
        </>
      )}

      <footer className="film-foot">
        <ul className="film-legend">
          <li>
            <i className="film-key film-key-line5" />
            line in risk class 5
          </li>
          <li>
            <i className="film-key film-key-line4" />
            class 4
          </li>
          <li>
            <i className="film-key film-key-wire" />
            classes 1 to 3
          </li>
          <li>
            <i className="film-key film-key-wire film-key-unrated" />
            land not rated
          </li>
          {classes.length > 0 && (
            <li>
              risk map: lower
              <span className="film-key-classes">
                {classes.map((c) => (
                  <span key={c} style={{ display: 'inline-block', width: '1.5em', height: '0.75em', background: c, opacity: 0.8 }} />
                ))}
              </span>
              higher
            </li>
          )}
        </ul>
        <p className="film-caption">{film.caption}</p>
      </footer>
      {error && <p className="film-error">{error}</p>}
    </div>
  );
}
