export type ViewMode = '2d' | '3d';
export type BasemapMode = 'satellite' | 'map';

export const WORLD_HALF = 20037508.342789244;
export const TWO_WORLD = 2 * WORLD_HALF;

const DEG_TO_MERC = WORLD_HALF / 180;
const RAD_360 = Math.PI / 360;
const MERC_Y_SCALE = WORLD_HALF / Math.PI;

export function lonLatToMercator(lon: number, lat: number): [number, number] {
  const clamped = Math.max(-85.05112878, Math.min(85.05112878, lat));
  return [lon * DEG_TO_MERC, Math.log(Math.tan((90 + clamped) * RAD_360)) * MERC_Y_SCALE];
}

export function mercatorToLonLat(mx: number, my: number): [number, number] {
  const lon = (mx / WORLD_HALF) * 180;
  const lat = (2 * Math.atan(Math.exp(my / MERC_Y_SCALE)) - Math.PI / 2) * (180 / Math.PI);
  return [lon, lat];
}

export function mercatorCosLat(my: number): number {
  return 1 / Math.cosh((my / WORLD_HALF) * Math.PI);
}
