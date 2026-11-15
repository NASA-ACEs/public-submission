import { ArrowLeft, ChevronRight, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { isUnknownOwner, ownerLabel, type GridLine, type Situation, type ThreatLine } from '../grid/api';
import {
  buildCompanyReports,
  type CompanyReport,
  download,
  fileSlug,
  fmtEta,
  lineTitle,
  printReport,
  reportCsv,
  reportGeoJson,
  reportText,
} from './report';
import { lineSnapshot } from './snapshot';
import type { Fmt } from '../grid/units';

interface Props {
  situation: Situation | null;
  lines: GridLine[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onFlyToLine: (line: ThreatLine) => void;
  /** Selected company's lines (null when none), so the map can highlight its fires. */
  onCompanyChange: (lines: ThreatLine[] | null) => void;
  /** Display name of a fire ("4 mi NNE of Anza" or the incident name). */
  fireLabel: (id: string) => string | undefined;
  fmt: Fmt;
}

export function ReportsPanel({ situation, lines, open, onOpenChange, onFlyToLine, onCompanyChange, fireLabel, fmt }: Props) {
  const reports = useMemo(() => (situation ? buildCompanyReports(situation) : []), [situation]);
  const [owner, setOwner] = useState<string | null>(null);
  const report = reports.find((r) => r.owner === owner) ?? null;
  const pathsByOid = useMemo(() => new Map(lines.map((l) => [l.oid, l.paths])), [lines]);
  const totalShutoff = reports.reduce((a, r) => a + r.shutoff.length, 0);
  const focusLines = useMemo(() => (open && report ? [...report.shutoff, ...report.psps] : null), [open, report]);
  useEffect(() => onCompanyChange(focusLines), [focusLines, onCompanyChange]);

  if (!situation) return null;

  if (!open) {
    return (
      <button type="button" className="reports-pill" onClick={() => onOpenChange(true)}>
        <span className="reports-pill-dot" aria-hidden />
        Reports
        <span className="panel-pill-count">
          {reports.length} {reports.length === 1 ? 'company' : 'companies'}
          {totalShutoff > 0 ? `, ${totalShutoff} shut off` : ''}
        </span>
      </button>
    );
  }

  return (
    <aside className="reports" aria-label="Operator reports">
      <div className="sidebar-head">
        <div className="sidebar-head-row">
          {report ? (
            <button type="button" className="link" onClick={() => setOwner(null)}>
              <ArrowLeft size={14} /> All companies
            </button>
          ) : (
            <h1 className="h1">Reports</h1>
          )}
          <button type="button" className="icon-btn" onClick={() => onOpenChange(false)} aria-label="Hide reports">
            <X size={16} />
          </button>
        </div>
        {report && <h1 className="h1 reports-title">{ownerLabel(report.owner)}</h1>}
        {report && isUnknownOwner(report.owner) && (
          <p className="reports-note">Operator not recorded in the source data. Identify the owner before acting.</p>
        )}
      </div>

      {!report ? (
        <div className="sidebar-scroll">
          {reports.length === 0 ? (
            <p className="empty">No lines need de-energizing right now.</p>
          ) : (
            reports.map((r) => (
              <button key={r.owner} type="button" className="item item-2" onClick={() => setOwner(r.owner)}>
                <span className="item-row">
                  <span className={`item-main ${isUnknownOwner(r.owner) ? 'is-unknown' : ''}`}>{ownerLabel(r.owner)}</span>
                  <span className="item-side">
                    {fmt.km(r.shutoffKm + r.pspsKm)}
                    <ChevronRight size={14} />
                  </span>
                </span>
                <span className="item-sub">
                  {r.shutoff.length > 0 && <span className="t-shutoff">{r.shutoff.length} shut off</span>}
                  {r.shutoff.length > 0 && r.psps.length > 0 && ', '}
                  {r.psps.length > 0 && <span className="t-psps">{r.psps.length} PSPS</span>}
                  {(() => {
                    const n = new Set([...r.shutoff, ...r.psps].map((l) => l.fire?.id).filter(Boolean)).size;
                    return `, ${n} ${n === 1 ? 'fire' : 'fires'}`;
                  })()}
                </span>
              </button>
            ))
          )}
        </div>
      ) : (
        <CompanyReportView
          key={report.owner}
          report={report}
          situation={situation}
          pathsByOid={pathsByOid}
          onFlyToLine={onFlyToLine}
          fireLabel={fireLabel}
          fmt={fmt}
        />
      )}
    </aside>
  );
}

function CompanyReportView({
  report,
  situation,
  pathsByOid,
  onFlyToLine,
  fireLabel,
  fmt,
}: {
  report: CompanyReport;
  situation: Situation;
  pathsByOid: Map<number, number[][][]>;
  onFlyToLine: (line: ThreatLine) => void;
  fireLabel: (id: string) => string | undefined;
  fmt: Fmt;
}) {
  const all = useMemo(() => [...report.shutoff, ...report.psps], [report]);
  const [shots, setShots] = useState<Map<number, string | null>>(new Map());
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);

  // Snapshots load progressively, a few at a time.
  useEffect(() => {
    let alive = true;
    const queue = [...all];
    const worker = async () => {
      for (let l = queue.shift(); l && alive; l = queue.shift()) {
        const img = await lineSnapshot(l, pathsByOid.get(l.oid) ?? [], situation, fmt);
        if (alive) setShots((m) => new Map(m).set(l.oid, img));
      }
    };
    void Promise.all([worker(), worker(), worker()]);
    return () => {
      alive = false;
    };
  }, [all, pathsByOid, situation, fmt]);

  const slug = fileSlug(report, situation);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(reportText(report, situation, fmt, fireLabel));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked */
    }
  };
  const pdf = async () => {
    setBusy(true);
    const map = new Map<number, string | null>();
    for (const l of all) map.set(l.oid, await lineSnapshot(l, pathsByOid.get(l.oid) ?? [], situation, fmt));
    setBusy(false);
    printReport(report, situation, map, fmt, fireLabel);
  };

  const block = (label: string, cls: string, lines: ThreatLine[], km: number) =>
    lines.length > 0 && (
      <section className="rep-block">
        <h2 className="rep-h">
          <span className={cls}>{label}</span>
          <span className="rep-h-meta">
            {lines.length} {lines.length === 1 ? 'line' : 'lines'}, {fmt.km(km)}
          </span>
        </h2>
        {lines.map((l) => {
          const img = shots.get(l.oid);
          const secs = l.sections ?? [];
          const fire = l.fire ? (fireLabel(l.fire.id) ?? l.fire.name) : null;
          return (
            <button key={l.oid} type="button" className="rep-line" onClick={() => onFlyToLine(l)}>
              <div className="rep-shot">
                {img ? <img src={img} alt={`Satellite view of ${lineTitle(l)}`} /> : img === null ? <span>No image</span> : <span className="shimmer" />}
              </div>
              <div className="rep-line-name">{lineTitle(l)}</div>
              <dl className="kv kv-facts">
                <dt>Voltage</dt>
                <dd>{l.kv} kV</dd>
                <dt>To cut</dt>
                <dd>{fmt.km(l.sectionKm ?? 0)}</dd>
                <dt>Fire</dt>
                <dd title={fire ?? undefined}>{fire ?? '-'}</dd>
                <dt>Distance</dt>
                <dd>{fmt.dist(l.fireDistM)}</dd>
                <dt>Arrives</dt>
                <dd>{fmtEta(l.etaH)}</dd>
                <dt>Wind</dt>
                <dd>{l.fire?.wind ? fmt.wind(l.fire.wind) : '-'}</dd>
                <dt>Fuel risk</dt>
                <dd>
                  {l.fuelRisk
                    ? `${l.fuelRisk.riskClass != null ? `${l.fuelRisk.riskClass}/5` : '-'}, ${fmt.km(l.fuelRisk.highKm)} high`
                    : '-'}
                </dd>
                <dt>From</dt>
                <dd>{secs.length > 0 ? `${secs[0].start[1].toFixed(4)}, ${secs[0].start[0].toFixed(4)}` : '-'}</dd>
                <dt>To</dt>
                <dd>
                  {secs.length > 0
                    ? `${secs[secs.length - 1].end[1].toFixed(4)}, ${secs[secs.length - 1].end[0].toFixed(4)}`
                    : '-'}
                </dd>
                <dt>Sections</dt>
                <dd>{secs.length || '-'}</dd>
              </dl>
            </button>
          );
        })}
      </section>
    );

  return (
    <>
      <div className="rep-actions">
        <button type="button" onClick={() => void copy()}>
          {copied ? 'Copied' : 'Copy'}
        </button>
        <button type="button" onClick={() => download(`${slug}.csv`, reportCsv(report), 'text/csv')}>
          CSV
        </button>
        <button
          type="button"
          onClick={() => download(`${slug}.geojson`, reportGeoJson(report), 'application/geo+json')}
        >
          GeoJSON
        </button>
        <button type="button" className="is-primary" onClick={() => void pdf()} disabled={busy}>
          {busy ? 'Preparing' : 'PDF'}
        </button>
      </div>
      <div className="sidebar-scroll rep-scroll">
        {block('Shut off now', 't-shutoff', report.shutoff, report.shutoffKm)}
        {block('PSPS', 't-psps', report.psps, report.pspsKm)}
      </div>
    </>
  );
}
