// Annotated satellite snapshots of dangerous line sections for operator reports.
// Imagery comes from the Esri World Imagery export endpoint (CORS-enabled), and the
// line, the de-energize sections, fire detections and wind are drawn on top.
import type { Path, Situation, ThreatLine } from '../grid/api';
import type { Fmt } from '../grid/units';

const EXPORT_URL = 'https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export';
const W = 720;
const H = 440;
const WORLD_HALF = 20037508.342789244;
const cache = new Map<string, Promise<string | null>>();

function groundScale(y: number): number {
  return Math.max(0.3, 1 / Math.cosh((y / WORLD_HALF) * Math.PI));
}

/** Frame around the sections (or the whole line), padded and fitted to the image aspect. */
function frame(line: ThreatLine, linePaths: Path[], fireAt: number[] | null): [number, number, number, number] {
  const pts = (line.sections?.length ? line.sections.map((s) => s.path) : linePaths).flat();
  if (fireAt) pts.push(fireAt);
  let [x0, y0, x1, y1] = pts.length
    ? pts.reduce(
        (b, [x, y]) => [Math.min(b[0], x), Math.min(b[1], y), Math.max(b[2], x), Math.max(b[3], y)],
        [Infinity, Infinity, -Infinity, -Infinity],
      )
    : line.bbox;
  const minSpan = 2500 / groundScale((y0 + y1) / 2); // at least ~2.5 km across
  let w = Math.max(x1 - x0, minSpan) * 1.35;
  let h = Math.max(y1 - y0, minSpan) * 1.35;
  if (w / h > W / H) h = (w * H) / W;
  else w = (h * W) / H;
  const cx = (x0 + x1) / 2;
  const cy = (y0 + y1) / 2;
  [x0, x1, y0, y1] = [cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2];
  return [x0, y0, x1, y1];
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('imagery failed'));
    img.src = url;
  });
}

function strokePaths(
  ctx: CanvasRenderingContext2D,
  paths: Path[],
  toPx: (x: number, y: number) => [number, number],
  color: string,
  width: number,
) {
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (const path of paths) {
    ctx.beginPath();
    path.forEach(([x, y], i) => {
      const [px, py] = toPx(x, y);
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
  }
}

function arrow(ctx: CanvasRenderingContext2D, cx: number, cy: number, bearingDeg: number, len: number) {
  const r = (bearingDeg * Math.PI) / 180;
  const dx = Math.sin(r);
  const dy = -Math.cos(r);
  const tx = cx + dx * len;
  const ty = cy + dy * len;
  ctx.beginPath();
  ctx.moveTo(cx - dx * len, cy - dy * len);
  ctx.lineTo(tx, ty);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(tx, ty);
  ctx.lineTo(tx - dx * 7 - dy * 5, ty - dy * 7 + dx * 5);
  ctx.lineTo(tx - dx * 7 + dy * 5, ty - dy * 7 - dx * 5);
  ctx.closePath();
  ctx.fill();
}

async function render(line: ThreatLine, linePaths: Path[], s: Situation, fmt: Fmt): Promise<string | null> {
  const fires = s.fires.points;
  const cluster = line.fire ? s.fires.clusters.find((c) => c.id === line.fire?.id) : undefined;
  const spread = line.fire ? s.spread.find((e) => e.clusterId === line.fire?.id) : undefined;
  const [x0, y0, x1, y1] = frame(line, linePaths, cluster?.centroid ?? null);
  const url =
    `${EXPORT_URL}?bbox=${x0},${y0},${x1},${y1}&bboxSR=3857&imageSR=3857` +
    `&size=${W},${H}&format=jpg&f=image`;
  const img = await loadImage(url);
  const canvas = document.createElement('canvas');
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  ctx.drawImage(img, 0, 0, W, H);
  const toPx = (x: number, y: number): [number, number] => [((x - x0) / (x1 - x0)) * W, ((y1 - y) / (y1 - y0)) * H];
  const wind = line.fire?.wind;

  // 24h spread forecast of the threatening fire.
  if (spread) {
    ctx.save();
    ctx.beginPath();
    spread.ring.forEach(([x, y], i) => {
      const [px, py] = toPx(x, y);
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.closePath();
    ctx.fillStyle = 'rgba(239, 68, 68, 0.16)';
    ctx.fill();
    ctx.setLineDash([7, 5]);
    ctx.lineWidth = 2;
    ctx.strokeStyle = 'rgba(254, 202, 202, 0.95)';
    ctx.stroke();
    ctx.restore();
  }

  // Whole line, then the sections to de-energize on top.
  strokePaths(ctx, linePaths, toPx, 'rgba(0,0,0,0.55)', 5);
  strokePaths(ctx, linePaths, toPx, 'rgba(255,255,255,0.85)', 2);
  const sec = (line.sections ?? []).map((s) => s.path);
  const color = line.tier === 'SHUT OFF' ? '#ef4444' : '#f59e0b';
  strokePaths(ctx, sec, toPx, 'rgba(0,0,0,0.7)', 9);
  strokePaths(ctx, sec, toPx, color, 5.5);

  // Fire detections in frame.
  for (const [x, y, , rank] of fires) {
    if (x < x0 || x > x1 || y < y0 || y > y1) continue;
    const [px, py] = toPx(x, y);
    ctx.beginPath();
    ctx.arc(px, py, rank === 3 ? 5 : rank === 2 ? 4 : 3, 0, Math.PI * 2);
    ctx.fillStyle = rank === 3 ? '#dc2626' : rank === 2 ? '#f97316' : '#facc15';
    ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(0,0,0,0.6)';
    ctx.stroke();
  }

  // Fire centre and the direction the wind pushes it.
  if (cluster) {
    const [fx, fy] = toPx(cluster.centroid[0], cluster.centroid[1]);
    if (wind && wind.mph >= 1) {
      const r = (wind.bearingDeg * Math.PI) / 180;
      const len = 40 + Math.min(50, wind.mph * 3);
      ctx.save();
      ctx.lineCap = 'round';
      ctx.strokeStyle = 'rgba(0,0,0,0.7)';
      ctx.fillStyle = 'rgba(0,0,0,0.7)';
      ctx.lineWidth = 7;
      arrow(ctx, fx + Math.sin(r) * len * 0.5, fy - Math.cos(r) * len * 0.5, wind.bearingDeg, len * 0.5 + 2);
      ctx.strokeStyle = '#93c5fd';
      ctx.fillStyle = '#93c5fd';
      ctx.lineWidth = 3.5;
      arrow(ctx, fx + Math.sin(r) * len * 0.5, fy - Math.cos(r) * len * 0.5, wind.bearingDeg, len * 0.5);
      ctx.restore();
    }
    ctx.beginPath();
    ctx.arc(fx, fy, 9, 0, Math.PI * 2);
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = '#fff';
    ctx.stroke();
  }

  // Scale bar (bottom left).
  const mPerPx = ((x1 - x0) * groundScale((y0 + y1) / 2)) / W;
  const step = fmt.scaleStep(70 * mPerPx);
  const barPx = step.m / mPerPx;
  ctx.fillStyle = 'rgba(0,0,0,0.6)';
  ctx.fillRect(10, H - 30, barPx + 20, 20);
  ctx.fillStyle = '#fff';
  ctx.fillRect(20, H - 18, barPx, 3);
  ctx.font = '600 11px "IBM Plex Sans", system-ui, sans-serif';
  ctx.fillText(step.label, 20, H - 21);

  return canvas.toDataURL('image/jpeg', 0.86);
}

/** Data URL of an annotated satellite snapshot of the line's dangerous sections (cached). */
export function lineSnapshot(
  line: ThreatLine,
  linePaths: Path[],
  situation: Situation,
  fmt: Fmt,
): Promise<string | null> {
  const key = `${line.oid}:${line.tier}:${line.sectionKm ?? 0}:${situation.generatedAt}:${fmt.units}`;
  let p = cache.get(key);
  if (!p) {
    p = render(line, linePaths, situation, fmt).catch(() => null);
    cache.set(key, p);
  }
  return p;
}
