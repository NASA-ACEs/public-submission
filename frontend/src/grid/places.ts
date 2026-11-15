// Fire location as dispatch reports give it: distance and direction from the nearest town ("4 mi NE of Anza").
// Places: U.S. Census TIGERweb incorporated places and census designated places, loaded once per state.
import { compass16 } from './geo';
import { M_PER_MI, type Fmt } from './units';

// U.S. Census TIGERweb (public domain): incorporated places and census designated places.
const PLACES_URL = 'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer';
const PLACE_LAYERS = [4, 5];
const PAGE = 1000;
const FIPS: Record<string, string> = {
  AL: '01', AK: '02', AZ: '04', AR: '05', CA: '06', CO: '08', CT: '09', DE: '10', DC: '11', FL: '12', GA: '13',
  HI: '15', ID: '16', IL: '17', IN: '18', IA: '19', KS: '20', KY: '21', LA: '22', ME: '23', MD: '24', MA: '25',
  MI: '26', MN: '27', MS: '28', MO: '29', MT: '30', NE: '31', NV: '32', NH: '33', NJ: '34', NM: '35', NY: '36',
  NC: '37', ND: '38', OH: '39', OK: '40', OR: '41', PA: '42', RI: '44', SC: '45', SD: '46', TN: '47', TX: '48',
  UT: '49', VT: '50', VA: '51', WA: '53', WV: '54', WI: '55', WY: '56',
};

interface Place {
  name: string;
  lon: number;
  lat: number;
}

const cache = new Map<string, Promise<Place[]>>();

async function loadPlaces(state: string): Promise<Place[]> {
  const fips = FIPS[state.toUpperCase()];
  if (!fips) return [];
  const out: Place[] = [];
  for (const layer of PLACE_LAYERS) {
    for (let offset = 0; ; offset += PAGE) {
      const q = new URLSearchParams({
        where: `STATE='${fips}'`,
        outFields: 'BASENAME,INTPTLAT,INTPTLON',
        returnGeometry: 'false',
        resultOffset: String(offset),
        resultRecordCount: String(PAGE),
        f: 'json',
      });
      const res = await fetch(`${PLACES_URL}/${layer}/query?${q}`);
      if (!res.ok) throw new Error(`places ${res.status}`);
      const data = await res.json();
      const feats = data.features ?? [];
      for (const f of feats) {
        const a = f.attributes ?? {};
        const lat = parseFloat(a.INTPTLAT);
        const lon = parseFloat(a.INTPTLON);
        if (a.BASENAME && Number.isFinite(lat) && Number.isFinite(lon)) out.push({ name: a.BASENAME, lon, lat });
      }
      if (!data.exceededTransferLimit || feats.length === 0) break;
    }
  }
  return out;
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
