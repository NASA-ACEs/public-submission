/**
 * Transmission lines coloured by the model's large-fire risk class, every 200 m along every line.
 *
 * The pieces and their classes are computed outside the app with the backend's own sampler
 * (`backend/scripts/line_risk.py`, the same 200 m pieces as `app.grid.fuel.densify`) and shipped as
 * `public/film/lines-ca-20261020.json`. Each run is `[class, x0, y0, dx, dy, ...]` in units of 10 m of Web Mercator;
 * class 0 is land the model does not rate, 1 to 5 are the risk classes of the map.
 */
import Graphic from '@arcgis/core/Graphic.js';
import Polyline from '@arcgis/core/geometry/Polyline.js';
import SpatialReference from '@arcgis/core/geometry/SpatialReference.js';
import GraphicsLayer from '@arcgis/core/layers/GraphicsLayer.js';
import SimpleLineSymbol from '@arcgis/core/symbols/SimpleLineSymbol.js';

export const LINE_RISK_URL = `${import.meta.env.BASE_URL}film/lines-ca-20261020.json`;

export interface LineRiskSummary {
  result: string;
  lines: number;
  step_m: number;
  km_total: number;
  km_covered: number;
  km_flagged: number;
  km_by_class: Record<string, number>;
}

interface LineRiskData {
  summary: LineRiskSummary;
  runs: number[][];
}

/** Map colours; the panel's key uses the same ones. */
export const LINE_RISK_STYLE = {
  c5: { color: '#ff3b1f', width: 2.6, label: 'Risk class 5' },
  c4: { color: '#ff8a3d', width: 2.2, label: 'Risk class 4' },
  low: { color: 'rgba(230, 236, 250, 0.55)', width: 1, label: 'Classes 1 to 3' },
  none: { color: 'rgba(161, 161, 170, 0.35)', width: 0.8, label: 'Land not rated' },
} as const;

type Group = keyof typeof LINE_RISK_STYLE;

/** The runs as polyline paths in Web Mercator metres, grouped by how they are drawn. */
export function decodeLineRisk(runs: number[][]): Record<Group, number[][][]> {
  const out: Record<Group, number[][][]> = { c5: [], c4: [], low: [], none: [] };
  for (const run of runs) {
    const cls = run[0];
    const group: Group = cls >= 5 ? 'c5' : cls === 4 ? 'c4' : cls === 0 ? 'none' : 'low';
    const path: number[][] = [];
    let x = 0;
    let y = 0;
    for (let i = 1; i < run.length; i += 2) {
      x += run[i] * 10;
      y += run[i + 1] * 10;
      path.push([x, y]);
    }
    if (path.length > 1) out[group].push(path);
  }
  return out;
}

export function createLineRiskLayer(): GraphicsLayer {
  return new GraphicsLayer({ id: 'line-risk', visible: false });
}

/** Fills the layer (once) and returns the summary of the data. */
export async function loadLineRisk(layer: GraphicsLayer, signal: AbortSignal): Promise<LineRiskSummary> {
  const res = await fetch(LINE_RISK_URL, { signal });
  if (!res.ok) throw new Error(`Line risk ${res.status}`);
  const data = (await res.json()) as LineRiskData;
  if (layer.destroyed) return data.summary;
  const groups = decodeLineRisk(data.runs);
  const sr = SpatialReference.WebMercator;
  layer.removeAll();
  // drawn from the faintest up, so the flagged pieces stay on top
  for (const key of ['none', 'low', 'c4', 'c5'] as const) {
    const style = LINE_RISK_STYLE[key];
    if (!groups[key].length) continue;
    layer.add(
      new Graphic({
        geometry: new Polyline({ paths: groups[key], spatialReference: sr }),
        symbol: new SimpleLineSymbol({ color: style.color, width: style.width, cap: 'round', join: 'round' }),
        attributes: { group: key },
      }),
    );
  }
  return data.summary;
}
