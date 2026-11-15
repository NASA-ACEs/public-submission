// Picks the transmission line under the cursor with a pixel tolerance.
// view.hitTest misses 1-px lines draped on terrain in the 3D SceneView, so lines are matched here
// by distance instead, which behaves the same in 2D and 3D.
import type MapView from '@arcgis/core/views/MapView.js';
import type SceneView from '@arcgis/core/views/SceneView.js';
import type { GridLine } from './api';
import { lonLatToMercator, mercatorCosLat } from './geo';

export interface LineIndexEntry {
  line: GridLine;
  bbox: [number, number, number, number];
}

export function indexLines(lines: GridLine[]): LineIndexEntry[] {
  return lines.map((line) => {
    const b: [number, number, number, number] = [Infinity, Infinity, -Infinity, -Infinity];
    for (const path of line.paths)
      for (const [x, y] of path) {
        if (x < b[0]) b[0] = x;
        if (y < b[1]) b[1] = y;
        if (x > b[2]) b[2] = x;
        if (y > b[3]) b[3] = y;
      }
    return { line, bbox: b };
  });
}

function segDist2(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax;
  const dy = by - ay;
  const len = dx * dx + dy * dy;
  const t = len === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len));
  const ex = px - ax - t * dx;
  const ey = py - ay - t * dy;
  return ex * ex + ey * ey;
}

/** Nearest line within `tolPx` screen pixels of (x, y), or null. */
export function pickLine(
  view: MapView | SceneView,
  index: LineIndexEntry[],
  screen: { x: number; y: number },
  tolPx = 6,
): GridLine | null {
  const p = view.toMap(screen);
  if (!p) return null;
  const [mx, my] = p.spatialReference?.isWGS84 ? lonLatToMercator(p.x, p.y) : [p.x, p.y];
  // Map units (Web Mercator metres) per screen pixel. SceneView has no resolution: use its scale
  // (ground metres per pixel at 96 dpi), stretched to Mercator at this latitude.
  const res = view.type === '2d' ? view.resolution : view.scale / (39.37 * 96) / mercatorCosLat(my);
  const tol = tolPx * res;
  let best: GridLine | null = null;
  let bestD = tol * tol;
  for (const { line, bbox } of index) {
    if (mx < bbox[0] - tol || mx > bbox[2] + tol || my < bbox[1] - tol || my > bbox[3] + tol) continue;
    for (const path of line.paths)
      for (let i = 0; i + 1 < path.length; i++) {
        const d = segDist2(mx, my, path[i][0], path[i][1], path[i + 1][0], path[i + 1][1]);
        if (d <= bestD) {
          bestD = d;
          best = line;
        }
      }
  }
  return best;
}
