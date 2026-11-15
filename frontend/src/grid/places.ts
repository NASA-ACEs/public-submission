// Fire location as dispatch reports give it: distance and direction from the nearest town ("4 mi NE of Anza").
// Places: U.S. Census populated places (cities, towns and CDPs), loaded once per state.
import { compass16 } from './geo';
import { M_PER_MI, type Fmt } from './units';

const PLACES_URL =
  'https://services.arcgis.com/P3ePLMYs2RVChkJx/ArcGIS/rest/services/USA_Census_Populated_Places/FeatureServer/0/query';
const PAGE = 2000;

interface Place {
  name: string;
  lon: number;
  lat: number;
}

const cache = new Map<string, Promise<Place[]>>();

async function loadPlaces(state: string): Promise<Place[]> {
  const out: Place[] = [];
  for (let offset = 0; ; offset += PAGE) {
    const q = new URLSearchParams({
      where: `ST='${state.replace(/'/g, "''")}'`,
      outFields: 'NAME',
      returnGeometry: 'false',
      returnCentroid: 'true',
      outSR: '4326',
      resultOffset: String(offset),
      resultRecordCount: String(PAGE),
      f: 'json',
    });
    const res = await fetch(`${PLACES_URL}?${q}`);
    if (!res.ok) throw new Error(`places ${res.status}`);
    const data = await res.json();
    for (const f of data.features ?? []) {
      if (f.centroid) out.push({ name: f.attributes.NAME, lon: f.centroid.x, lat: f.centroid.y });
    }
    if (!data.exceededTransferLimit) return out;
  }
}

/** Census places of a state (cached; an empty list if the service is down). */
export function statePlaces(state: string): Promise<Place[]> {
  let p = cache.get(state);
  if (!p) {
    p = loadPlaces(state).catch(() => {
      cache.delete(state);
      return [];
    });
    cache.set(state, p);
  }
  return p;
}

/** "4 mi NE of Anza" ("6 km NE of Anza" in metric), or "Anza" when the fire is within a mile of the town centre. */
export function describeLocation(places: Place[], lon: number, lat: number, fmt: Fmt): string | null {
  const R = 3958.8; // miles
  const rad = Math.PI / 180;
  let best: { p: Place; d: number } | null = null;
  for (const p of places) {
    const dLat = (lat - p.lat) * rad;
    const dLon = (lon - p.lon) * rad * Math.cos(((lat + p.lat) / 2) * rad);
    const d = R * Math.hypot(dLat, dLon);
    if (!best || d < best.d) best = { p, d };
  }
  if (!best || best.d > 60) return null;
  if (best.d < 1) return best.p.name;
  // Bearing from the town to the fire.
  const y = Math.sin((lon - best.p.lon) * rad) * Math.cos(lat * rad);
  const x =
    Math.cos(best.p.lat * rad) * Math.sin(lat * rad) -
    Math.sin(best.p.lat * rad) * Math.cos(lat * rad) * Math.cos((lon - best.p.lon) * rad);
  const brg = ((Math.atan2(y, x) / rad) + 360) % 360;
  const d = fmt.units === 'imperial' ? Math.round(best.d) : Math.round((best.d * M_PER_MI) / 1000);
  return `${d} ${fmt.longUnit} ${compass16(brg)} of ${best.p.name}`;
}
