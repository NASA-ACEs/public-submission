// Unit calculator: the backend sends SI (m, km, ha, m/s, m/min) plus acres and mph; the UI shows
// either US customary or metric. Data and API requests stay in backend units; only display converts.
import { compass16 } from './geo';

export type Units = 'imperial' | 'metric';
export const UNITS_KEY = 'units';
export const UNITS_DEFAULT: Units = 'imperial';

export const M_PER_MI = 1609.344;
export const M_PER_FT = 0.3048;
export const M_PER_KM = 1000;
export const HA_PER_AC = 0.40468564224;
export const MS_PER_MPH = 0.44704;
export const KMH_PER_MPH = 1.609344;
export const MS_PER_KMH = 1 / 3.6;

export const readUnits = (): Units => {
  try {
    const v = localStorage.getItem(UNITS_KEY);
    return v === 'metric' || v === 'imperial' ? v : UNITS_DEFAULT;
  } catch {
    return UNITS_DEFAULT;
  }
};

export const saveUnits = (u: Units) => {
  try {
    localStorage.setItem(UNITS_KEY, u);
  } catch {
    /* storage blocked */
  }
};

const int = (n: number) => Math.round(n).toLocaleString('en-US');
/** One decimal under 10, whole numbers above. */
const dec = (n: number) => (Math.abs(n) < 10 ? String(Number(n.toFixed(1))) : int(n));

export interface Fmt {
  units: Units;
  /** Long-distance unit name: "mi" or "km". */
  longUnit: 'mi' | 'km';
  /** A distance in metres: "850 ft", "1.2 mi" / "850 m", "1.2 km". */
  dist(m: number | null | undefined): string;
  /** A length given in km (line km, km to cut): "7.5 mi" / "12 km". */
  km(km: number | null | undefined): string;
  /** A short length in metres (flame length): "4.3 ft" / "1.3 m". */
  short(m: number | null | undefined): string;
  /** An area in acres: "1,234 ac" / "499 ha". */
  acres(ac: number | null | undefined): string;
  /** An area in hectares. */
  ha(ha: number | null | undefined): string;
  /** A speed in mph: "12 mph" / "19 km/h". */
  mph(mph: number | null | undefined): string;
  /** A speed in m/s. */
  ms(ms: number | null | undefined): string;
  /** A rate of spread in m/min: "14 ft/min" / "4.2 m/min". */
  rate(mPerMin: number | null | undefined): string;
  /** Wind as fire-weather forecasts give it, from the direction it blows FROM: "NE 045° 10 mph". */
  wind(w: { mph: number; bearingDeg: number }): string;
  /** Wind-speed input: unit label, range, and conversion to m/s for the API. */
  windInput: { unit: string; max: number; toMs(v: number): number };
  /** A backend unit label ('m', 'm/min', 'degC', ...): the label to show and how to convert a value into it. */
  measure(unit: string | null | undefined): { unit: string; conv(v: number): number };
  /** Scale-bar step for a length of about `m` metres or more: label and its length in metres. */
  scaleStep(minM: number): { m: number; label: string };
}

const none = (v: number | null | undefined): v is null | undefined => v == null || !Number.isFinite(v);

const IMPERIAL_STEPS: [number, string][] = [
  [500 * M_PER_FT, '500 ft'],
  [1000 * M_PER_FT, '1,000 ft'],
  [0.5 * M_PER_MI, '0.5 mi'],
  [1 * M_PER_MI, '1 mi'],
  [2 * M_PER_MI, '2 mi'],
  [5 * M_PER_MI, '5 mi'],
  [10 * M_PER_MI, '10 mi'],
];
const METRIC_STEPS: [number, string][] = [
  [250, '250 m'],
  [500, '500 m'],
  [1000, '1 km'],
  [2000, '2 km'],
  [5000, '5 km'],
  [10000, '10 km'],
  [20000, '20 km'],
];

const same = (v: number) => v;
/** Backend unit -> US customary unit and conversion. Units not listed (%, m3/m3, x, 1) stay as they are. */
const TO_IMPERIAL: Record<string, [string, (v: number) => number]> = {
  m: ['ft', (v) => v / M_PER_FT],
  km: ['mi', (v) => (v * M_PER_KM) / M_PER_MI],
  'm/min': ['ft/min', (v) => v / M_PER_FT],
  'm/s': ['mph', (v) => v / MS_PER_MPH],
  'km/h': ['mph', (v) => v / KMH_PER_MPH],
  ha: ['ac', (v) => v / HA_PER_AC],
  'kW/m': ['BTU/ft/s', (v) => v * 0.288894],
  'kg/m2': ['t/ac', (v) => v * 4.46089], // US short tons per acre (fuel load)
  'MJ/m2': ['BTU/ft²', (v) => v * 88.0551],
  degC: ['°F', (v) => v * 1.8 + 32],
  '°C': ['°F', (v) => v * 1.8 + 32],
};

export function makeFmt(units: Units): Fmt {
  const imp = units === 'imperial';
  const steps = imp ? IMPERIAL_STEPS : METRIC_STEPS;
  const mph = (v: number | null | undefined) => (none(v) ? '-' : imp ? `${int(v)} mph` : `${int(v * KMH_PER_MPH)} km/h`);
  const km = (v: number | null | undefined) => (none(v) ? '-' : imp ? `${dec((v * M_PER_KM) / M_PER_MI)} mi` : `${dec(v)} km`);
  return {
    units,
    longUnit: imp ? 'mi' : 'km',
    dist: (m) => {
      if (none(m)) return '-';
      if (imp) return m < 0.1 * M_PER_MI ? `${int(m / M_PER_FT)} ft` : `${dec(m / M_PER_MI)} mi`;
      return m < 1000 ? `${int(m)} m` : `${dec(m / 1000)} km`;
    },
    km,
    short: (m) => (none(m) ? '-' : imp ? `${dec(m / M_PER_FT)} ft` : `${dec(m)} m`),
    acres: (ac) => (none(ac) ? '-' : imp ? `${int(ac)} ac` : `${int(ac * HA_PER_AC)} ha`),
    ha: (ha) => (none(ha) ? '-' : imp ? `${int(ha / HA_PER_AC)} ac` : `${int(ha)} ha`),
    mph,
    ms: (ms) => (none(ms) ? '-' : imp ? `${dec(ms / MS_PER_MPH)} mph` : `${dec(ms * 3.6)} km/h`),
    rate: (r) => (none(r) ? '-' : imp ? `${dec(r / M_PER_FT)} ft/min` : `${dec(r)} m/min`),
    wind: (w) => {
      const from = (Math.round(w.bearingDeg) + 180 + 360) % 360;
      return `${compass16(from)} ${String(from).padStart(3, '0')}° ${mph(w.mph)}`;
    },
    windInput: imp
      ? { unit: 'mph', max: 90, toMs: (v) => v * MS_PER_MPH }
      : { unit: 'km/h', max: 145, toMs: (v) => v * MS_PER_KMH },
    measure: (unit) => {
      const u = unit ?? '';
      if (imp && TO_IMPERIAL[u]) return { unit: TO_IMPERIAL[u][0], conv: TO_IMPERIAL[u][1] };
      return { unit: u === 'degC' ? '°C' : u, conv: same };
    },
    scaleStep: (minM) => {
      const [m, label] = steps.find(([s]) => s >= minM) ?? steps[steps.length - 1];
      return { m, label };
    },
  };
}
