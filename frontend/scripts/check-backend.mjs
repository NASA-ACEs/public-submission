// Requests the model-map endpoints with the same URLs src/grid/burnability.ts builds and checks the fields
// the app reads. Usage: node scripts/check-backend.mjs [backend url] [result id]
const base = (process.argv[2] || process.env.BACKEND_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const resultId = process.argv[3] || process.env.VITE_BURNABILITY_RESULT || 'latest';
let failed = 0;
const check = (ok, label, detail = '') => {
  if (!ok) failed += 1;
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? `  ${detail}` : ''}`);
};
const get = async (path, init) => {
  const res = await fetch(`${base}${path}`, init);
  return { res, type: res.headers.get('content-type') || '' };
};

const { res: metaRes } = await get(`/v1/burnability/results/${resultId}`);
check(metaRes.ok, `GET results/${resultId}`, `HTTP ${metaRes.status}`);
if (!metaRes.ok) process.exit(1);
const meta = await metaRes.json();
const R = `/v1/burnability/results/${meta.result_id}`;
check(typeof meta.result_id === 'string' && typeof meta.synthetic === 'boolean', 'result_id, synthetic', `${meta.result_id}, synthetic=${meta.synthetic}`);
check(Array.isArray(meta.bbox_wgs84) && meta.bbox_wgs84.length === 4, 'bbox_wgs84', JSON.stringify(meta.bbox_wgs84?.map((v) => +v.toFixed(3))));
const layerOk = (l) => l.id && l.title && typeof l.units === 'string' && l.legend?.stops?.length >= 2;
check(meta.layers?.length > 0 && meta.layers.every(layerOk), 'layers[].id/title/units/legend.stops', `${meta.layers?.length} layers`);
const has = (id) => meta.layers.some((l) => l.id === id);
const layerId = has('risk_index') ? 'risk_index' : has('susceptibility_index') ? 'susceptibility_index' : meta.layers[0].id;
check(true, 'default layer', layerId);
if (!meta.synthetic) {
  const sme2 = Math.max(0, ...Object.values(meta.extra?.nisar ?? {}).map((v) => v.scenes ?? 0));
  check(typeof meta.valid_from === 'string' && typeof meta.model_version === 'string', 'valid_from, model_version', `${meta.valid_from}, ${meta.model_version}`);
  check(typeof meta.nisar_share === 'number' && sme2 > 0, 'nisar_share, extra.nisar.*.scenes', `${(meta.nisar_share * 100).toFixed(0)} % of cells, ${sme2} SME2 scenes`);
  check(true, 'extra.fuel_age.gcov.scenes (optional)', String(meta.extra?.fuel_age?.gcov?.scenes ?? 'absent'));
}

const [w, s, e, n] = meta.bbox_wgs84;
const [lon, lat] = [(w + e) / 2, (s + n) / 2];
const z = 11;
const col = Math.floor(((lon + 180) / 360) * 2 ** z);
const rad = (lat * Math.PI) / 180;
const row = Math.floor(((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * 2 ** z);
const tile = await get(`${R}/tiles/${layerId}/${z}/${col}/${row}.png`);
check(tile.res.ok && tile.type.startsWith('image/png'), `tile ${layerId}/${z}/${col}/${row}`, `HTTP ${tile.res.status} ${tile.type} ${(await tile.res.arrayBuffer()).byteLength} bytes`);

let sample = null;
for (const [dx, dy] of [[0, 0], [0.25, 0.25], [-0.25, 0.25], [0.25, -0.25], [-0.25, -0.25]]) {
  const p = await get(`${R}/point?lon=${(lon + dx * (e - w)).toFixed(5)}&lat=${(lat + dy * (n - s)).toFixed(5)}`);
  check(p.res.ok, 'point', `HTTP ${p.res.status}`);
  const body = await p.res.json();
  if (body.valid) {
    sample = body;
    break;
  }
}
check(sample !== null && typeof sample.values[layerId] === 'number', `point values[${layerId}]`, sample ? String(sample.values[layerId]) : 'no valid cell in five tries');
if (sample) {
  const shown = ['risk_index', 'risk_class', 'lfmc', 'lfmc_pct', 'rate_of_spread', 'flame_length', 'fuel_state', 'years_since_fire', 'dryness_anomaly', 'nisar_term', 'susceptibility_index'].filter((id) => sample.values[id] != null);
  check(shown.length > 0, 'point card fields', shown.map((id) => `${id}=${(+sample.values[id]).toFixed(3)}${sample.units?.[id] && sample.units[id] !== '1' ? ` ${sample.units[id]}` : ''}`).join(', '));
}

const vectorId = meta.vector_layers?.[0]?.id ?? 'illustrative_pathways';
const vec = await get(`${R}/vectors/${vectorId}?bbox=${meta.bbox_wgs84.join(',')}&zoom=${z.toFixed(1)}&limit=1000`);
check(vec.res.ok, `vectors/${vectorId}`, `HTTP ${vec.res.status}`);
const fc = await vec.res.json();
const f = fc.features?.[0];
const key = meta.synthetic ? 'bearing_deg' : 'direction_deg';
check(!f || (f.geometry?.coordinates?.length >= 2 && typeof f.properties?.[key] === 'number'), `vector features (${key})`, `${fc.features?.length ?? 0} features${f ? `, first ${JSON.stringify(f.properties)}` : ''}`);

// Fire growth: the URLs fetchFireGrowth builds (map-day wind, a set wind, fuel age off) and the fields the panel reads.
if (meta.capabilities?.spread) {
  const number = (v) => typeof v === 'number' && Number.isFinite(v);
  const at = sample?.sampled_cell_center ?? { lon, lat };
  const spots = [[0, 0], [0.2, 0.15], [-0.2, 0.15], [0.2, -0.15], [-0.2, -0.15], [0.1, 0.3]].map(([dx, dy]) => [at.lon + dx * (e - w), at.lat + dy * (n - s)]);
  let start = null;
  for (const [x, y] of spots) {
    const r = await get(`${R}/spread?lon=${x.toFixed(5)}&lat=${y.toFixed(5)}&hours=1,3,6`);
    if (r.res.ok) {
      start = [x, y];
      break;
    }
    const body = await r.res.json();
    check(r.res.status === 422 && typeof body.detail === 'string', `spread refused at ${x.toFixed(3)}, ${y.toFixed(3)}`, `HTTP ${r.res.status} ${body.detail}`);
  }
  check(start !== null, 'spread: a point with fuel', start ? start.map((v) => v.toFixed(4)).join(', ') : 'none in six tries');
  for (const [label, query] of start ? [['map-day wind', ''], ['wind 10 m/s from 45°', '&wind_speed=10&wind_from=45'], ['wind 10 m/s from 45°, fuel age off', '&wind_speed=10&wind_from=45&fuel_age=false']] : []) {
    const r = await get(`${R}/spread?lon=${start[0].toFixed(5)}&lat=${start[1].toFixed(5)}&hours=1,3,6${query}`);
    const body = await r.res.json();
    const p = body.properties ?? {};
    const shape = r.res.ok && body.features?.length === 3 && body.features.every((f) => number(f.properties?.hours) && number(f.properties?.area_ha) && (f.geometry === null || f.geometry.type === 'MultiPolygon'));
    const fields = number(p.head_rate_m_min) && number(p.head_direction_deg) && number(p.flame_length_m) && number(p.wind_10m_m_s) && number(p.wind_from_deg) && typeof p.edge_reached === 'boolean' && ['map_day', 'request'].includes(p.wind_source);
    check(shape && fields, `spread, ${label}`, shape ? `${body.features.map((f) => `${f.properties.hours} h ${f.properties.area_ha} ha`).reverse().join(', ')}; head ${p.head_rate_m_min?.toFixed(1)} m/min to ${p.head_direction_deg?.toFixed(0)}°, flame ${p.flame_length_m?.toFixed(1)} m, wind ${p.wind_10m_m_s?.toFixed(1)} m/s from ${p.wind_from_deg?.toFixed(0)}° (${p.wind_source})` : `HTTP ${r.res.status} ${JSON.stringify(body).slice(0, 160)}`);
  }
  const off = await get(`${R}/spread?lon=${(w - 1).toFixed(5)}&lat=${lat.toFixed(5)}&hours=1,3,6`);
  const reason = await off.res.json();
  check(off.res.status === 422 && typeof reason.detail === 'string', 'spread outside the result is refused with a reason', `HTTP ${off.res.status} ${reason.detail}`);
  check(meta.layers.every((l) => l.kind !== 'component'), 'no component layers in the layer list', `${meta.component_layers?.length ?? 0} components kept out`);
} else {
  check(true, 'spread (not offered by this result)', 'capabilities.spread absent: the Fire growth switch stays hidden');
}

console.log(failed ? `${failed} check(s) failed` : 'all checks passed');
process.exit(failed ? 1 : 0);
