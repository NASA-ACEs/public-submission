// Single source of data for the app: the NASA-ACEs backend (grid, fires, weather, risk, tiles).

export const API_BASE = (
  (import.meta.env.VITE_API_URL as string | undefined) || '/api'
).replace(/\/$/, '');

/** Absolute base for map tile URL templates (ArcGIS needs a full URL). */
export const API_ORIGIN = API_BASE.startsWith('http')
  ? API_BASE
  : `${typeof window !== 'undefined' ? window.location.origin : ''}${API_BASE}`;

export type Path = number[][];

/** Owner value the source dataset (HIFLD transmission lines) uses when the operator is not recorded. */
export const UNKNOWN_OWNER = 'NOT AVAILABLE';
export const isUnknownOwner = (owner: string) => owner === UNKNOWN_OWNER;
/** Human label for an owner; the raw value stays the key for filtering and the API. */
export const ownerLabel = (owner: string) => (isUnknownOwner(owner) ? 'Unknown operator' : owner);

export interface StateProvider {
  owner: string;
  count: number;
  km: number;
  fKm: number;
}

export interface StateSummary {
  abbr: string;
  name: string;
  extent: [number, number, number, number];
  center: [number, number];
  rings: Path[];
  lineCount: number;
  totalKm: number;
  forestKm: number;
  providers: StateProvider[];
}

export interface GridLine {
  oid: number;
  owner: string;
  kv: number;
  sub1: string;
  sub2: string;
  km: number;
  fKm: number;
  paths: Path[];
  /** Forested sub-paths, precomputed by the backend. */
  fPaths: Path[];
}

export type ThreatTier = 'SHUT OFF' | 'PSPS';

/**
 * Model fuel risk (rank 0-1, class 1-5) from the backend's newest model result. Present only when
 * the backend runs with FUEL_RISK_RESULT and the result covers the place.
 */
export interface LineFuelRisk {
  index: number;
  max: number;
  riskClass: number | null;
  /** km of the line in the higher risk classes, and km the result covers. */
  highKm: number;
  coveredKm: number;
}

export interface ThreatLine {
  oid: number;
  tier: ThreatTier;
  owner: string;
  sub1: string;
  sub2: string;
  kv: number;
  km: number;
  fKm: number;
  riskScore: number;
  fireDistM: number | null;
  etaH: number | null;
  bbox: [number, number, number, number];
  /** Total length to de-energize, km. */
  sectionKm?: number;
  /** Sections to de-energize: length, WGS84 ends and Mercator path. */
  sections?: { km: number; start: [number, number]; end: [number, number]; path: Path; fuelRiskIndex?: number | null }[];
  fuelRisk?: LineFuelRisk | null;
  fire?: { id: string; name: string; sizeClass?: FireSize; distM: number; wind: FireWind | null } | null;
}

export interface FireCluster {
  id: string;
  name: string;
  centroid: [number, number];
  bbox: [number, number, number, number];
  areaAcres: number;
  minHoursAgo: number;
  forestFraction: number | null;
  sizeClass: FireSize;
  maxFrp: number;
  detections: number;
  fuelRiskIndex?: number | null;
  fuelRiskClass?: number | null;
}

export type FireSize = 'large' | 'medium' | 'small';

export interface FireWind {
  mph: number;
  /** Compass label the wind blows from, e.g. "SW". */
  fromLabel: string;
  /** Direction the fire is pushed toward, degrees clockwise from north. */
  bearingDeg: number;
  lengthToBreadth: number;
  source: string;
}

export interface Situation {
  state: string;
  generatedAt: number;
  hours: number;
  radiusM: number;
  fires: {
    /** [x, y, frp, rank] in Web Mercator; rank 3/2/1 = large/medium/small fire. */
    points: [number, number, number, number][];
    perimeters: { name: string; acres: number; rings: Path[] }[];
    clusters: FireCluster[];
    filter?: { totalDetections: number; keptDetections: number; forestOnly: boolean };
  };
  weather: { source: string; avgWindMph: number; dominantWindLabel: string; minRhPct: number };
  /** 24h wind-driven spread ellipse per fire, with the local wind that shapes it. */
  spread: { clusterId: string; ring: Path; acres: number; headM?: number; wind?: FireWind; fuelBoost?: number }[];
  /** Which model result feeds the fuel-risk fields, when the backend has one. */
  fuelRisk?: { resultId: string; layer: string; validFrom?: string; modelVersion?: string; drives?: string[] };
  threats: {
    totals: { 'SHUT OFF': number; PSPS: number; MONITOR: number };
    lines: ThreatLine[];
    /** Threatened line sections, already clipped to forest near fire. */
    sections: { shutoff: Path[]; psps: Path[] };
    savings: { netUsd: number; avoidedLossUsd: number; outageCostUsd: number; forestKmProtected: number };
  };
}

async function getJson<T>(path: string, init?: RequestInit, retries = 20): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    const res = await fetch(`${API_BASE}${path}`, init);
    // 503: the backend is still building its grid cache on first start.
    if (res.status === 503 && attempt < retries) {
      await new Promise((r) => setTimeout(r, 5000));
      if (init?.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      continue;
    }
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return (await res.json()) as T;
  }
}

let statesPromise: Promise<StateSummary[]> | null = null;
const linesCache = new Map<string, Promise<GridLine[]>>();

export function fetchStates(): Promise<StateSummary[]> {
  statesPromise ??= getJson<StateSummary[]>('/v1/grid/states').catch((err) => {
    statesPromise = null;
    throw err;
  });
  return statesPromise;
}

export function fetchStateLines(abbr: string): Promise<GridLine[]> {
  let p = linesCache.get(abbr);
  if (!p) {
    p = getJson<{ lines: GridLine[] }>(`/v1/grid/states/${encodeURIComponent(abbr)}/lines`)
      .then((d) => d.lines)
      .catch((err) => {
        linesCache.delete(abbr);
        throw err;
      });
    linesCache.set(abbr, p);
  }
  return p;
}

export function fetchSituation(
  abbr: string,
  opts: { hours: number; radiusM: number; owners: string[] | null; forestOnly: boolean },
  signal?: AbortSignal,
): Promise<Situation> {
  return getJson<Situation>(`/v1/grid/states/${encodeURIComponent(abbr)}/situation`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      hours: opts.hours,
      radius_m: opts.radiusM,
      owners: opts.owners,
      forest_only: opts.forestOnly,
    }),
    signal,
  });
}

export function forestTilesTemplate(): string {
  return `${API_ORIGIN}/v1/grid/forest/tiles/{level}/{row}/{col}.png`;
}
