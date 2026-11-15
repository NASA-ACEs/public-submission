// Per-operator shutoff reports built from the backend situation: grouping and exports.
import { isUnknownOwner, ownerLabel, type Situation, type ThreatLine } from '../grid/api';

export interface CompanyReport {
  owner: string;
  shutoff: ThreatLine[];
  psps: ThreatLine[];
  /** km to de-energize, per tier. */
  shutoffKm: number;
  pspsKm: number;
}

const sum = (xs: ThreatLine[]) => xs.reduce((a, l) => a + (l.sectionKm ?? 0), 0);

export function buildCompanyReports(s: Situation): CompanyReport[] {
  const by = new Map<string, ThreatLine[]>();
  for (const l of s.threats.lines) {
    if (l.tier !== 'SHUT OFF' && l.tier !== 'PSPS') continue;
    const list = by.get(l.owner) ?? [];
    list.push(l);
    by.set(l.owner, list);
  }
  return [...by.entries()]
    .map(([owner, lines]) => {
      const shutoff = lines.filter((l) => l.tier === 'SHUT OFF');
      const psps = lines.filter((l) => l.tier === 'PSPS');
      return { owner, shutoff, psps, shutoffKm: sum(shutoff), pspsKm: sum(psps) };
    })
    .sort(
      (a, b) =>
        Number(isUnknownOwner(a.owner)) - Number(isUnknownOwner(b.owner)) ||
        b.shutoff.length - a.shutoff.length ||
        b.psps.length - a.psps.length,
    );
}

export const lineName = (l: ThreatLine) => `${l.sub1} to ${l.sub2}`;
export const fmtKm = (km: number) => `${km.toFixed(km < 10 ? 1 : 0)} km`;
export const fmtDist = (m: number | null) => (m === null ? 'n/a' : m < 1000 ? `${m} m` : `${(m / 1000).toFixed(1)} km`);
export const fmtEta = (h: number | null) => (h === null ? 'n/a' : h === 0 ? 'now' : `${h} h`);
const coord = ([lon, lat]: [number, number]) => `${lat.toFixed(5)}, ${lon.toFixed(5)}`;

function header(r: CompanyReport, s: Situation) {
  return {
    title: `Wildfire de-energization report: ${ownerLabel(r.owner)}`,
    note: isUnknownOwner(r.owner)
      ? 'Operator not recorded in the source dataset (HIFLD transmission lines); identify the owner before acting.'
      : '',
    meta: `${s.state}, generated ${new Date(s.generatedAt).toLocaleString()}, fires from last ${s.hours} h, buffer ${s.radiusM / 1000} km`,
  };
}

export function reportText(r: CompanyReport, s: Situation): string {
  const h = header(r, s);
  const out = [h.title, h.meta, ...(h.note ? [h.note] : []), ''];
  const block = (label: string, lines: ThreatLine[], km: number) => {
    if (!lines.length) return;
    out.push(`${label}: ${lines.length} lines, ${fmtKm(km)} to de-energize`);
    lines.forEach((l, i) => {
      out.push(
        `${i + 1}. ${lineName(l)}, ${l.kv} kV, line #${l.oid}` +
          `, fire ${fmtDist(l.fireDistM)}, ETA ${fmtEta(l.etaH)}` +
          (l.fire?.wind ? `, wind ${Math.round(l.fire.wind.mph)} mph ${l.fire.wind.fromLabel}` : ''),
      );
      (l.sections ?? []).forEach((sec, j) =>
        out.push(`   ${String.fromCharCode(97 + (j % 26))}) ${fmtKm(sec.km)}: ${coord(sec.start)} to ${coord(sec.end)}`),
      );
    });
    out.push('');
  };
  block('SHUT OFF NOW', r.shutoff, r.shutoffKm);
  block('PSPS (prepare to de-energize)', r.psps, r.pspsKm);
  return out.join('\n');
}

const csvCell = (v: string | number | null | undefined) => {
  const t = v === null || v === undefined ? '' : String(v);
  return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
};

export function reportCsv(r: CompanyReport): string {
  const rows = [
    ['tier', 'line_id', 'from_substation', 'to_substation', 'kv', 'section_km', 'start_lat', 'start_lon', 'end_lat', 'end_lon',
      'fire_distance_m', 'eta_h', 'wind_mph', 'wind_from', 'nearest_fire'],
  ];
  for (const l of [...r.shutoff, ...r.psps]) {
    for (const sec of l.sections ?? []) {
      rows.push([
        l.tier, String(l.oid), l.sub1, l.sub2, String(l.kv), sec.km.toFixed(2),
        String(sec.start[1]), String(sec.start[0]), String(sec.end[1]), String(sec.end[0]),
        l.fireDistM === null ? '' : String(l.fireDistM), l.etaH === null ? '' : String(l.etaH),
        l.fire?.wind ? String(l.fire.wind.mph) : '', l.fire?.wind?.fromLabel ?? '', l.fire?.name ?? '',
      ]);
    }
  }
  return rows.map((row) => row.map(csvCell).join(',')).join('\n');
}

const WH = 20037508.342789244;
const toLonLat = ([x, y]: number[]) => [
  +((x / WH) * 180).toFixed(6),
  +((Math.atan(Math.exp((y / WH) * Math.PI)) * 360) / Math.PI - 90).toFixed(6),
];

export function reportGeoJson(r: CompanyReport): string {
  const features = [...r.shutoff, ...r.psps].flatMap((l) =>
    (l.sections ?? []).map((sec) => ({
      type: 'Feature',
      geometry: { type: 'LineString', coordinates: sec.path.map(toLonLat) },
      properties: {
        tier: l.tier, lineId: l.oid, from: l.sub1, to: l.sub2, kv: l.kv, sectionKm: sec.km,
        fireDistanceM: l.fireDistM, etaH: l.etaH, windMph: l.fire?.wind?.mph ?? null,
        windFrom: l.fire?.wind?.fromLabel ?? null, nearestFire: l.fire?.name ?? null,
      },
    })),
  );
  return JSON.stringify({ type: 'FeatureCollection', features }, null, 1);
}

export function download(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const fileSlug = (r: CompanyReport, s: Situation) =>
  `${s.state}-${ownerLabel(r.owner).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')}-shutoff-${new Date(s.generatedAt).toISOString().slice(0, 10)}`;

const esc = (t: string) => t.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] ?? c);

export const sub = (name: string) => (name === 'NOT AVAILABLE' || name === 'UNKNOWN' ? 'unknown' : name);
const ll = ([lon, lat]: [number, number]) => `${lat.toFixed(4)}, ${lon.toFixed(4)}`;

/** Opens a printable report (with snapshots) in a new window; the browser saves it as PDF. */
export function printReport(r: CompanyReport, s: Situation, snapshots: Map<number, string | null>) {
  const when = new Date(s.generatedAt);
  const stamp = `${when.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}, ${when.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}`;

  const item = (l: ThreatLine) => {
    const img = snapshots.get(l.oid);
    const secs = l.sections ?? [];
    const from = secs[0]?.start;
    const to = secs[secs.length - 1]?.end;
    const facts: [string, string][] = [
      ['Voltage', `${l.kv} kV`],
      ['To cut', `${(l.sectionKm ?? 0).toFixed(1)} km`],
      ['Fire', fmtDist(l.fireDistM)],
      ['Arrives', l.etaH === null ? 'n/a' : l.etaH === 0 ? 'now' : `in ${l.etaH} h`],
    ];
    if (l.fire?.wind) facts.push(['Wind', `${Math.round(l.fire.wind.mph)} mph ${l.fire.wind.fromLabel}`]);
    return `<article>
      ${img ? `<img src="${img}" alt="">` : '<div class="noimg"></div>'}
      <div>
        <h3>${esc(sub(l.sub1))} to ${esc(sub(l.sub2))}</h3>
        <dl>${facts.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
        ${from && to ? `<p class="at">${ll(from)} to ${ll(to)}</p>` : ''}
      </div>
    </article>`;
  };
  const block = (label: string, lines: ThreatLine[], km: number) =>
    lines.length ? `<h2>${label} <span>${lines.length}, ${km.toFixed(0)} km</span></h2>${lines.map(item).join('')}` : '';

  const title = `Shutoff notice: ${ownerLabel(r.owner)}`;
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>${esc(title)}</title>
<style>
@page{margin:14mm}
body{font:12px/1.45 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;color:#111;margin:0}
h1{font-size:18px;font-weight:600;margin:0}
.meta{color:#777;margin:2px 0 0}
h2{font-size:13px;font-weight:600;margin:22px 0 10px;padding-bottom:4px;border-bottom:1px solid #111}
h2 span{font-weight:400;color:#777}
article{display:grid;grid-template-columns:300px 1fr;gap:16px;padding:10px 0;border-bottom:1px solid #eee;break-inside:avoid}
img,.noimg{width:300px;aspect-ratio:720/440;display:block;background:#f2f2f2}
h3{font-size:13px;margin:0 0 8px}
dl{display:grid;grid-template-columns:70px 1fr;gap:3px 10px;margin:0}
dt{color:#777}dd{margin:0}
.at{margin:8px 0 0;color:#777;font-size:11px}
footer{margin-top:16px;color:#777;font-size:10px}
</style></head><body>
<h1>${esc(title)}</h1>
<p class="meta">${esc(s.state)}, ${stamp}</p>
${isUnknownOwner(r.owner) ? '<p><b>Operator not recorded in the source data. Identify the owner before acting.</b></p>' : ''}
${block('Shut off now', r.shutoff, r.shutoffKm)}
${block('PSPS', r.psps, r.pspsKm)}
<footer>Red: section to shut off now. Amber: PSPS section. Dashed: 24 h spread forecast. Fires: NASA VIIRS. Wind: NOAA. Imagery: Esri.</footer>
<script>window.onload=()=>setTimeout(()=>window.print(),300)</script>
</body></html>`;
  const w = window.open('', '_blank');
  if (!w) return false;
  w.document.write(html);
  w.document.close();
  return true;
}
