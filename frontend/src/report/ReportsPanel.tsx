import { ArrowUp, Clock, Flame, Trees, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { isUnknownOwner, ownerLabel, type GridLine, type Situation, type ThreatLine } from '../grid/api';
import {
  buildCompanyReports,
  type CompanyReport,
  download,
  fileSlug,
  fmtDist,
  fmtEta,
  fmtKm,
  lineName,
  printReport,
  reportCsv,
  reportGeoJson,
  reportText,
  sub,
} from './report';
import { lineSnapshot } from './snapshot';

interface Props {
  situation: Situation | null;
  lines: GridLine[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onFlyToLine: (line: ThreatLine) => void;
  /** Selected company's lines (null when none), so the map can highlight its fires. */
  onCompanyChange: (lines: ThreatLine[] | null) => void;
}

const lineWord = (n: number) => (n === 1 ? 'line' : 'lines');
const kmNumber = (km: number) => km.toFixed(km < 10 ? 1 : 0);

/** km to de-energize per tier, as two figures. */
function TierStats({ shutoff, shutoffKm, psps, pspsKm }: { shutoff: number; shutoffKm: number; psps: number; pspsKm: number }) {
  return (
    <div className="stats">
      <div className="stat">
        <span className="stat-fig">
          <b className="fig">{kmNumber(shutoffKm)}</b>
          <span className="unit">km</span>
        </span>
        <span className="stat-label">
          <i className="swatch swatch-shutoff" /> Shut off, {shutoff} {lineWord(shutoff)}
        </span>
      </div>
      <div className="stat">
        <span className="stat-fig">
          <b className="fig">{kmNumber(pspsKm)}</b>
          <span className="unit">km</span>
        </span>
        <span className="stat-label">
          <i className="swatch swatch-psps" /> PSPS, {psps} {lineWord(psps)}
        </span>
      </div>
    </div>
  );
}

export function ReportsPanel({ situation, lines, open, onOpenChange, onFlyToLine, onCompanyChange }: Props) {
  const reports = useMemo(() => (situation ? buildCompanyReports(situation) : []), [situation]);
  const [owner, setOwner] = useState<string | null>(null);
  const report = reports.find((r) => r.owner === owner) ?? null;
  const pathsByOid = useMemo(() => new Map(lines.map((l) => [l.oid, l.paths])), [lines]);
  const total = useMemo(
    () =>
      reports.reduce(
        (a, r) => ({
          shutoff: a.shutoff + r.shutoff.length,
          shutoffKm: a.shutoffKm + r.shutoffKm,
          psps: a.psps + r.psps.length,
          pspsKm: a.pspsKm + r.pspsKm,
        }),
        { shutoff: 0, shutoffKm: 0, psps: 0, pspsKm: 0 },
      ),
    [reports],
  );
  const focusLines = useMemo(() => (open && report ? [...report.shutoff, ...report.psps] : null), [open, report]);
  useEffect(() => onCompanyChange(focusLines), [focusLines, onCompanyChange]);

  if (!situation) return null;

  if (!open) {
    // The pill carries the answer: how much line to cut now. Without SHUT OFF lines it falls back to PSPS.
    const tier = total.shutoff > 0 ? 'shutoff' : total.psps > 0 ? 'psps' : null;
    return (
      <button type="button" className="reports-pill" onClick={() => onOpenChange(true)}>
        <span className="pill-name">Reports</span>
        {tier ? (
          <span className="pill-stat">
            <b>{tier === 'shutoff' ? total.shutoff : total.psps}</b>
            {tier === 'shutoff' ? 'shut off' : 'PSPS'}
            <b>{kmNumber(tier === 'shutoff' ? total.shutoffKm : total.pspsKm)}</b>
            km
          </span>
        ) : (
          <span className="pill-stat">no lines at risk</span>
        )}
      </button>
    );
  }

  return (
    <aside className="reports" aria-label="Operator reports">
      <div className="sidebar-head">
        <div className="sidebar-head-row">
          {report ? (
            <button type="button" className="link" onClick={() => setOwner(null)}>
              All operators
            </button>
          ) : (
            <h2 className="h1">Shutoff reports</h2>
          )}
          <button type="button" className="icon-btn" onClick={() => onOpenChange(false)} aria-label="Hide reports">
            <X size={16} />
          </button>
        </div>
        {report && <h2 className={`h1 ${isUnknownOwner(report.owner) ? 'is-unknown' : ''}`}>{ownerLabel(report.owner)}</h2>}
        {report && isUnknownOwner(report.owner) && (
          <p className="reports-note">Owner not in the HIFLD source data. Verify before acting.</p>
        )}
        {report ? (
          <TierStats shutoff={report.shutoff.length} shutoffKm={report.shutoffKm} psps={report.psps.length} pspsKm={report.pspsKm} />
        ) : (
          reports.length > 0 && <TierStats {...total} />
        )}
      </div>

      {!report ? (
        <div className="sidebar-scroll">
          {reports.length === 0 ? (
            <p className="empty">No lines need de-energizing right now</p>
          ) : (
            reports.map((r) => {
              const fires = new Set([...r.shutoff, ...r.psps].map((l) => l.fire?.id).filter(Boolean)).size;
              return (
                <button key={r.owner} type="button" className="item item-2" onClick={() => setOwner(r.owner)}>
                  <span className="item-row">
                    <span className={`item-main ${isUnknownOwner(r.owner) ? 'is-unknown' : ''}`} title={ownerLabel(r.owner)}>
                      {ownerLabel(r.owner)}
                    </span>
                    <span className="item-side" title="Fires threatening this operator">
                      <Flame size={12} aria-label="Fires" />
                      {fires}
                    </span>
                  </span>
                  <span className="tiers">
                    {r.shutoff.length > 0 && (
                      <span className="tier">
                        <i className="swatch swatch-shutoff" />
                        <b>
                          {r.shutoff.length} {lineWord(r.shutoff.length)}
                        </b>
                        {fmtKm(r.shutoffKm)}
                      </span>
                    )}
                    {r.psps.length > 0 && (
                      <span className="tier">
                        <i className="swatch swatch-psps" />
                        <b>
                          {r.psps.length} {lineWord(r.psps.length)}
                        </b>
                        {fmtKm(r.pspsKm)}
                      </span>
                    )}
                  </span>
                </button>
              );
            })
          )}
        </div>
      ) : (
        <CompanyReportView
          key={report.owner}
          report={report}
          situation={situation}
          pathsByOid={pathsByOid}
          onFlyToLine={onFlyToLine}
        />
      )}
    </aside>
  );
}

/** A line is named by its two substations; an end the source does not record reads "unknown". */
function LineName({ line }: { line: ThreatLine }) {
  const [a, b] = [sub(line.sub1), sub(line.sub2)];
  if (a === 'unknown' && b === 'unknown') return <span className="rep-line-name">Line {line.oid}</span>;
  return (
    <span className="rep-line-name" title={lineName(line)}>
      <span className={a === 'unknown' ? 'is-unknown' : ''}>{a}</span>
      <span className="unit">to</span>
      <span className={b === 'unknown' ? 'is-unknown' : ''}>{b}</span>
    </span>
  );
}

function CompanyReportView({
  report,
  situation,
  pathsByOid,
  onFlyToLine,
}: {
  report: CompanyReport;
  situation: Situation;
  pathsByOid: Map<number, number[][][]>;
  onFlyToLine: (line: ThreatLine) => void;
}) {
  const all = useMemo(() => [...report.shutoff, ...report.psps], [report]);
  const [shots, setShots] = useState<Map<number, string | null>>(new Map());
  const [copied, setCopied] = useState<'ok' | 'blocked' | null>(null);
  const [pdfDone, setPdfDone] = useState<number | null>(null);
  const [activeOid, setActiveOid] = useState<number | null>(null);

  // Snapshots load progressively, a few at a time.
  useEffect(() => {
    let alive = true;
    const queue = [...all];
    const worker = async () => {
      for (let l = queue.shift(); l && alive; l = queue.shift()) {
        const img = await lineSnapshot(l, pathsByOid.get(l.oid) ?? [], situation);
        if (alive) setShots((m) => new Map(m).set(l.oid, img));
      }
    };
    void Promise.all([worker(), worker(), worker()]);
    return () => {
      alive = false;
    };
  }, [all, pathsByOid, situation]);

  const slug = fileSlug(report, situation);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(reportText(report, situation));
      setCopied('ok');
    } catch {
      setCopied('blocked');
    }
    setTimeout(() => setCopied(null), 1500);
  };
  const pdf = async () => {
    setPdfDone(0);
    const map = new Map<number, string | null>();
    for (const l of all) {
      map.set(l.oid, await lineSnapshot(l, pathsByOid.get(l.oid) ?? [], situation));
      setPdfDone(map.size);
    }
    setPdfDone(null);
    printReport(report, situation, map);
  };

  const block = (label: string, tier: 'shutoff' | 'psps', lines: ThreatLine[], km: number) =>
    lines.length > 0 && (
      <section>
        <h3 className="rep-h">
          <i className={`swatch swatch-${tier}`} />
          {label}
          <span className="num">
            {lines.length} {lineWord(lines.length)}, {fmtKm(km)}
          </span>
        </h3>
        {lines.map((l) => {
          const img = shots.get(l.oid);
          const sections = l.sections ?? [];
          return (
            <button
              key={l.oid}
              type="button"
              className={`rep-line ${l.oid === activeOid ? 'is-active' : ''}`}
              onClick={() => {
                setActiveOid(l.oid);
                onFlyToLine(l);
              }}
            >
              <span className="rep-line-head">
                <LineName line={l} />
                <span className="stat-fig" title="Length to de-energize">
                  <b className="fig fig-sm">{kmNumber(l.sectionKm ?? 0)}</b>
                  <span className="unit">km</span>
                </span>
              </span>
              <span className="rep-line-stats">
                <span className="ico-stat" title="Distance to the fire">
                  <Flame size={12} aria-label="Distance to the fire" />
                  <b>{fmtDist(l.fireDistM)}</b>
                </span>
                <span className="ico-stat" title="The fire reaches the line in">
                  <Clock size={12} aria-label="The fire reaches the line in" />
                  <b>{fmtEta(l.etaH)}</b>
                </span>
                {l.fire?.wind && (
                  <span className="wind" title={`Wind from ${l.fire.wind.fromLabel}`}>
                    <ArrowUp size={12} style={{ transform: `rotate(${l.fire.wind.bearingDeg}deg)` }} />
                    {Math.round(l.fire.wind.mph)} mph
                  </span>
                )}
                {l.fuelRisk && (
                  <span className="ico-stat" title="Fuel risk class along the line, 1 to 5, and km in the higher classes">
                    <Trees size={12} aria-label="Fuel risk class" />
                    {l.fuelRisk.riskClass ?? 'n/a'}/5, {fmtKm(l.fuelRisk.highKm)}
                  </span>
                )}
                <span>{l.kv} kV</span>
              </span>
              <span className="rep-shot">
                {img ? <img src={img} alt={`Satellite view of ${lineName(l)}`} /> : img === null ? <span>No image</span> : <span className="shimmer" />}
              </span>
              {sections.length > 0 && (
                <span className="rep-coords" title={sections.map((s) => `${s.start[1].toFixed(4)}, ${s.start[0].toFixed(4)} to ${s.end[1].toFixed(4)}, ${s.end[0].toFixed(4)}`).join('\n')}>
                  {sections[0].start[1].toFixed(4)}, {sections[0].start[0].toFixed(4)} to {sections[0].end[1].toFixed(4)},{' '}
                  {sections[0].end[0].toFixed(4)}
                  {sections.length > 1 ? `, +${sections.length - 1} more` : ''}
                </span>
              )}
            </button>
          );
        })}
      </section>
    );

  return (
    <>
      <div className="rep-actions">
        <button type="button" className="btn" onClick={() => void copy()}>
          {copied === 'ok' ? 'Copied' : copied === 'blocked' ? 'Blocked' : 'Copy'}
        </button>
        <button type="button" className="btn" onClick={() => download(`${slug}.csv`, reportCsv(report), 'text/csv')}>
          CSV
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => download(`${slug}.geojson`, reportGeoJson(report), 'application/geo+json')}
        >
          GeoJSON
        </button>
        <button type="button" className="btn btn-primary" onClick={() => void pdf()} disabled={pdfDone !== null} aria-busy={pdfDone !== null}>
          {pdfDone !== null ? (
            <span className="num">
              {pdfDone}/{all.length}
            </span>
          ) : (
            'PDF'
          )}
        </button>
      </div>
      <div className="sidebar-scroll rep-scroll">
        {block('Shut off now', 'shutoff', report.shutoff, report.shutoffKm)}
        {block('PSPS', 'psps', report.psps, report.pspsKm)}
      </div>
    </>
  );
}
