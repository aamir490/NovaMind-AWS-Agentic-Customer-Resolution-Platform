import { useCallback, useEffect, useRef, useState } from 'react';
import { operationLabels } from './diagnosticsContracts.js';

// ─── Helpers ──────────────────────────────────────────────────────────────────

function fmtNum(n) {
  return n == null ? '\u2014' : n.toLocaleString();
}

function fmtTime(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' }); }
  catch { return iso; }
}

function fmtDuration(ms) {
  if (ms == null) return '\u2014';
  return `${ms.toLocaleString(undefined, { maximumFractionDigits: 1 })} ms`;
}

function errRate(count, errors) {
  if (!count) return null;
  const pct = ((errors / count) * 100).toFixed(1);
  return `${fmtNum(errors)} (${pct}%)`;
}

// Derive a tone from a CaseStatus value
function caseTone(status) {
  if (status === 'escalated') return 'error';
  if (status === 'in_review')  return 'warning';
  return 'open';
}

// Derive a tone from a run status value
function runTone(status) {
  if (status === 'FAILED') return 'error';
  if (status === 'REVIEW_REQUIRED' || status === 'REVIEWED') return 'warning';
  if (status === 'COMPLETED') return 'success';
  return 'active';
}

// ─── Presentational primitives ────────────────────────────────────────────────

function StatTile({ label, value, tone, sub, icon }) {
  return (
    <div className={`db-stat-tile db-stat-tile--${tone ?? 'default'}`} aria-label={`${label}: ${value}`}>
      {icon && (
        <span className="db-stat-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round">
            <path d={icon} />
          </svg>
        </span>
      )}
      <div className="db-stat-body">
        <span className="db-stat-label">{label}</span>
        <strong className="db-stat-value">{value}</strong>
        {sub && <span className="db-stat-sub">{sub}</span>}
      </div>
    </div>
  );
}

function SectionHeader({ title, note }) {
  return (
    <div className="db-section-header">
      <h3 className="db-section-title">{title}</h3>
      {note && <span className="db-section-note">{note}</span>}
    </div>
  );
}

function LoadingRow({ label }) {
  return (
    <p className="db-loading" role="status">
      <span className="case-loading-dot" aria-hidden="true" />
      {label}
    </p>
  );
}

function ErrorRow({ message }) {
  return (
    <p className="db-error" role="alert">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
        aria-hidden="true">
        <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
      </svg>
      {message}
    </p>
  );
}

// ─── Section: Status tiles ─────────────────────────────────────────────────────

function CaseTiles({ cases }) {
  const total    = cases.length;
  const open      = cases.filter(c => c.status === 'open').length;
  const inReview  = cases.filter(c => c.status === 'in_review').length;
  const escalated = cases.filter(c => c.status === 'escalated').length;
  return (
    <div className="db-tiles" role="list">
      <div role="listitem">
        <StatTile label="Total cases" value={fmtNum(total)} tone="default"
          icon="M4 5h16v15H4zM9 5V3h6v2M8 10h8M8 14h5" />
      </div>
      <div role="listitem">
        <StatTile label="Open" value={fmtNum(open)} tone="open"
          icon="M12 22C12 22 4 16.5 4 10.5a8 8 0 1116 0C20 16.5 12 22 12 22z" />
      </div>
      <div role="listitem">
        <StatTile label="In review" value={fmtNum(inReview)} tone="warning"
          icon="M9 4H5v17h14V4h-4M9 3h6v4H9zM8 13l3 3 5-6" />
      </div>
      <div role="listitem">
        <StatTile label="Escalated" value={fmtNum(escalated)} tone="error"
          icon="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0zM12 9v4M12 17h.01" />
      </div>
    </div>
  );
}

function ReviewTile({ reviews }) {
  const count = reviews?.items?.length ?? 0;
  return (
    <div className="db-tiles db-tiles--single" role="list">
      <div role="listitem">
        <StatTile label="Pending human reviews" value={fmtNum(count)}
          tone={count > 0 ? 'warning' : 'success'}
          sub={count > 0 ? 'Awaiting decision in Reviews workspace' : 'No proposals awaiting review'}
          icon="M9 4H5v17h14V4h-4M9 3h6v4H9zM8 13l3 3 5-6" />
      </div>
    </div>
  );
}

// ─── Section: System / provider status ────────────────────────────────────────

function SystemStatus({ identity }) {
  const providerOk = identity.provider !== 'unavailable';
  return (
    <div className="db-system-row">
      <div className={`db-system-pill db-system-pill--${providerOk ? 'ok' : 'error'}`}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
          aria-hidden="true">
          <path d={providerOk
            ? 'M5 13l4 4L19 7'
            : 'M18 6L6 18M6 6l12 12'} />
        </svg>
        Provider: {identity.provider === 'bedrock' ? 'Amazon Bedrock'
          : providerOk ? 'Local scripted' : 'Unavailable'}
      </div>
      <div className={`db-system-pill db-system-pill--${identity.memory_available ? 'ok' : 'muted'}`}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
          aria-hidden="true">
          <path d={identity.memory_available
            ? 'M5 13l4 4L19 7'
            : 'M17 3a2.83 2.83 0 114 4L7.5 20.5 2 22l1.5-5.5z'} />
        </svg>
        Memory: {identity.memory_available ? 'Available' : 'Disabled'}
      </div>
      <div className="db-system-pill db-system-pill--muted">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
          aria-hidden="true">
          <circle cx="12" cy="12" r="10" /><path d="M12 8v4l3 3" />
        </svg>
        Process-local · resets on restart
      </div>
    </div>
  );
}

export function InvestigationControlPanel({ control, isAdmin, phase, error, onChange }) {
  if (!control) return <LoadingRow label="Loading AI investigation control…" />;
  const auto = control.configuredMode === 'AUTO';
  const effectiveAuto = control.effectiveMode === 'AUTO';
  const counts = control.counts;
  return (
    <div className="db-investigation-control">
      <div className="db-investigation-mode">
        <div>
          <p className="eyebrow">Current mode</p>
          <strong>{auto ? 'AUTO' : 'MANUAL'}</strong>
          <p className="case-caption">
            {effectiveAuto
              ? 'Only newly submitted customer cases are queued automatically.'
              : auto
                ? 'AUTO is configured but unavailable; no cases will be started automatically.'
                : 'An ADMIN must start each pending investigation from Cases.'}
          </p>
        </div>
        <label className="db-mode-switch">
          <span>AUTO investigation</span>
          <input type="checkbox" role="switch" checked={auto}
            disabled={!isAdmin || phase === 'submitting'
              || (!auto && !control.automaticCapabilityAvailable)}
            onChange={(event) => onChange(event.target.checked ? 'AUTO' : 'MANUAL')} />
        </label>
      </div>
      <div className="db-investigation-counts" aria-label="AI investigation status counts">
        <span><strong>{counts.pending}</strong> pending</span>
        <span><strong>{counts.running}</strong> running</span>
        <span><strong>{counts.reviewRequired}</strong> human review</span>
        <span><strong>{counts.completed}</strong> completed</span>
        <span><strong>{counts.failed + counts.recoveryRequired}</strong> failed / recovery</span>
      </div>
      {!control.automaticCapabilityAvailable && (
        <p className="case-caption">Automatic capability is disabled by server configuration.</p>
      )}
      {!isAdmin && <p className="case-caption">Only an ADMIN can change this setting.</p>}
      {phase === 'submitting' && <p className="case-notice" role="status">Saving investigation mode…</p>}
      {error && <p className="db-error" role="alert">{error}</p>}
    </div>
  );
}

// ─── Section: Agent / LLM operational metrics (diagnostics required) ──────────

function MetricRow({ label, count, errors, extra }) {
  const hasErrors = errors > 0;
  return (
    <div className="db-metric-row">
      <span className="db-metric-label">{label}</span>
      <span className="db-metric-count">{fmtNum(count)} ops</span>
      {hasErrors
        ? <span className="db-metric-errors">{errRate(count, errors)} errors</span>
        : <span className="db-metric-ok">No errors</span>}
      {extra && <span className="db-metric-extra">{extra}</span>}
    </div>
  );
}

function OpsMetrics({ snapshot }) {
  const m = {};
  snapshot.metrics.forEach(metric => { m[metric.operation] = metric; });

  const agentGraph  = m['agent.graph'];
  const hitlStart   = m['hitl.start'];
  const hitlResume  = m['hitl.resume'];
  const llm         = m['llm'];
  const propReview  = m['proposal.review'];
  const propCreate  = m['proposal.create'];

  // Aggregate run-level errors from both start and resume
  const runOps    = (hitlStart?.count ?? 0) + (hitlResume?.count ?? 0);
  const runErrors = (hitlStart?.errors ?? 0) + (hitlResume?.errors ?? 0);

  const tokenIn  = llm?.input_tokens_known ?? 0;
  const tokenOut = llm?.output_tokens_known ?? 0;
  const unknownCalls = llm?.input_tokens_unknown_calls ?? 0;

  return (
    <div className="db-metrics-grid">
      {/* Workflow runs */}
      <div className="db-metric-card">
        <p className="eyebrow">Agent workflow runs</p>
        <strong className="db-metric-big">{fmtNum(runOps)}</strong>
        {runErrors > 0
          ? <p className="db-metric-errors">{errRate(runOps, runErrors)} errors</p>
          : <p className="db-metric-ok">No run errors</p>}
        {hitlResume && (
          <p className="case-caption">{fmtNum(hitlResume.count)} resume operations</p>
        )}
      </div>

      {/* LLM calls */}
      <div className="db-metric-card">
        <p className="eyebrow">Model requests</p>
        <strong className="db-metric-big">{fmtNum(llm?.count ?? 0)}</strong>
        {llm && llm.errors > 0
          ? <p className="db-metric-errors">{errRate(llm.count, llm.errors)} errors</p>
          : <p className="db-metric-ok">No LLM errors</p>}
        <p className="case-caption">
          {fmtNum(tokenIn)} in / {fmtNum(tokenOut)} out tokens (known)
          {unknownCalls > 0 && ` · ${fmtNum(unknownCalls)} calls w/ unknown usage`}
        </p>
      </div>

      {/* Human reviews */}
      <div className="db-metric-card">
        <p className="eyebrow">Human review operations</p>
        <strong className="db-metric-big">{fmtNum(propReview?.count ?? 0)}</strong>
        {propReview && propReview.errors > 0
          ? <p className="db-metric-errors">{errRate(propReview.count, propReview.errors)} errors</p>
          : <p className="db-metric-ok">No review errors</p>}
        <p className="case-caption">{fmtNum(propCreate?.count ?? 0)} proposals created</p>
      </div>

      {/* Average durations */}
      {agentGraph && (
        <div className="db-metric-card">
          <p className="eyebrow">Agent graph avg duration</p>
          <strong className="db-metric-big">
            {fmtDuration(agentGraph.count > 0 ? agentGraph.duration_ms_total / agentGraph.count : 0)}
          </strong>
          <p className="case-caption">Max: {fmtDuration(agentGraph.duration_ms_max)}</p>
          <p className="case-caption">Total: {fmtDuration(agentGraph.duration_ms_total)}</p>
        </div>
      )}
    </div>
  );
}

// ─── Section: Pending reviews list ────────────────────────────────────────────

function PendingReviewsList({ reviews, onNavigate }) {
  const items = reviews?.items ?? [];
  if (items.length === 0) {
    return (
      <div className="db-empty">
        <span className="case-empty-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round">
            <path d="M9 4H5v17h14V4h-4M9 3h6v4H9zM8 13l3 3 5-6" />
          </svg>
        </span>
        <p>No proposals are awaiting review.</p>
      </div>
    );
  }
  return (
    <ul className="db-review-list" aria-label="Proposals awaiting human review">
      {items.map(item => (
        <li key={item.runId} className="db-review-item">
          <div className="db-review-body">
            <strong className="db-review-subject">{item.caseSubject}</strong>
            <span className="db-review-action">
              Action: <strong>{item.action}</strong>
            </span>
            <span className="db-review-time case-caption">Since {fmtTime(item.updatedAt)}</span>
          </div>
          <button type="button" className="button-secondary"
            onClick={() => onNavigate('reviews')}
            aria-label={`Go to Reviews workspace for ${item.caseSubject}`}>
            Review
          </button>
        </li>
      ))}
    </ul>
  );
}

// ─── Section: Recent cases ─────────────────────────────────────────────────────

function RecentCases({ cases, onNavigate }) {
  if (!cases.length) {
    return (
      <div className="db-empty">
        <span className="case-empty-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round">
            <path d="M4 5h16v15H4zM9 5V3h6v2M8 10h8M8 14h5" />
          </svg>
        </span>
        <p>No cases loaded.</p>
      </div>
    );
  }
  // Sort by updated_at descending; show most recent 8
  const sorted = [...cases].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)).slice(0, 8);
  return (
    <ul className="db-case-list" aria-label="Recent support cases">
      {sorted.map(c => (
        <li key={c.id} className="db-case-row">
          <div className="db-case-body">
            <span className="db-case-subject">{c.subject}</span>
            <span className="db-case-meta case-caption">{fmtTime(c.updated_at)}</span>
          </div>
          <span className={`db-case-status db-case-status--${caseTone(c.status)}`}>
            {c.status.replace('_', ' ')}
          </span>
        </li>
      ))}
    </ul>
  );
}

// ─── Section: Recent activity feed (diagnostics events) ───────────────────────

const EVENT_ICONS = {
  'hitl.start':    'M5 4h14v12H9l-4 4zM8 8h8M8 12h5',
  'hitl.resume':   'M5 4h14v12H9l-4 4zM8 8h8M8 12h5',
  'proposal.review': 'M9 4H5v17h14V4h-4M9 3h6v4H9zM8 13l3 3 5-6',
  'proposal.create': 'M12 5v14M5 12h14',
  'llm':           'M12 2a10 10 0 100 20A10 10 0 0012 2zm0 6v4l3 3',
  'agent.graph':   'M3 12h4l3-7 4 14 3-7h4',
  'tool':          'M14.7 6.3a1 1 0 010 1.4l-8 8a1 1 0 01-.4.2l-3 1a1 1 0 01-1.3-1.3l1-3a1 1 0 01.2-.4l8-8a1 1 0 011.4 0z',
  'http':          'M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z',
};

function ActivityFeed({ snapshot }) {
  // Show the 10 most-recent events in descending time order
  const events = [...snapshot.events]
    .sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))
    .slice(0, 10);

  if (events.length === 0) {
    return <p className="case-caption">No recent activity events recorded.</p>;
  }
  return (
    <ol className="db-activity-list" aria-label="Recent operational activity">
      {events.map((ev, i) => {
        const hasError = !!ev.error;
        const icon = EVENT_ICONS[ev.operation] ?? 'M12 2a10 10 0 100 20A10 10 0 0012 2';
        return (
          <li key={`${ev.span_id}-${i}`}
            className={`db-activity-item${hasError ? ' db-activity-item--error' : ''}`}>
            <span className="db-activity-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round">
                <path d={icon} />
              </svg>
            </span>
            <div className="db-activity-body">
              <span className="db-activity-op">
                {operationLabels[ev.operation] ?? ev.operation}
                {ev.tool && ev.tool !== 'unknown' && <code className="db-activity-tool"> · {ev.tool}</code>}
              </span>
              {hasError && (
                <span className="db-activity-error">{ev.error}</span>
              )}
              {ev.status && !hasError && (
                <span className="db-activity-status">{ev.status}</span>
              )}
            </div>
            <span className="db-activity-time case-caption">{fmtTime(ev.timestamp)}</span>
          </li>
        );
      })}
    </ol>
  );
}

// ─── Section: Attention items ──────────────────────────────────────────────────

function AttentionItems({ cases, reviews }) {
  const escalated = cases.filter(c => c.status === 'escalated');
  const pending   = reviews?.items ?? [];
  const total     = escalated.length + pending.length;

  if (total === 0) {
    return (
      <p className="db-attention-clear">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
          aria-hidden="true">
          <path d="M22 11.08V12a10 10 0 11-5.93-9.14M22 4L12 14.01l-3-3" />
        </svg>
        No items require immediate attention.
      </p>
    );
  }
  return (
    <ul className="db-attention-list" aria-label={`${total} items requiring attention`}>
      {pending.map(item => (
        <li key={`pr-${item.runId}`} className="db-attention-item db-attention-item--warning">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
            aria-hidden="true">
            <path d="M9 4H5v17h14V4h-4M9 3h6v4H9zM8 13l3 3 5-6" />
          </svg>
          <span>
            <strong>Pending review</strong> for case: {item.caseSubject}
          </span>
        </li>
      ))}
      {escalated.map(c => (
        <li key={`esc-${c.id}`} className="db-attention-item db-attention-item--error">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
            aria-hidden="true">
            <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0zM12 9v4M12 17h.01" />
          </svg>
          <span>
            <strong>Escalated case:</strong> {c.subject}
          </span>
        </li>
      ))}
    </ul>
  );
}

// ─── Main Dashboard component ──────────────────────────────────────────────────

export default function Dashboard({ api, identity, onNavigate }) {
  const [cases,    setCases]    = useState({ phase: 'loading', data: null, error: null });
  const [reviews,  setReviews]  = useState({ phase: 'loading', data: null, error: null });
  const [diag,     setDiag]     = useState({ phase: 'loading', data: null, error: null });
  const [control,  setControl]  = useState({ phase: 'loading', data: null, error: null });
  const [controlChange, setControlChange] = useState({ phase: 'idle', error: null });
  const [revision, setRevision] = useState(0);
  const abortRef = useRef(null);
  const modeRequest = useRef(null);

  const canDiag = identity?.can_view_diagnostics === true;

  useEffect(() => {
    if (!api || !identity) return;
    // Abort any previous fetch cycle
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const { signal } = controller;

    setCases(   { phase: 'loading', data: null, error: null });
    setReviews( { phase: 'loading', data: null, error: null });
    setControl( { phase: 'loading', data: null, error: null });
    if (canDiag) setDiag({ phase: 'loading', data: null, error: null });
    else         setDiag({ phase: 'na',      data: null, error: null });

    // Fire all reads in parallel; each settles independently
    api.listCases()
      .then(data => {
        if (!signal.aborted) setCases({ phase: 'ready', data, error: null });
      })
      .catch(err => {
        if (!signal.aborted)
          setCases({ phase: 'error', data: null,
            error: [401, 403].includes(err.status)
              ? 'Case data access denied.'
              : 'Cases could not be loaded.' });
      });

    api.pendingReviews(identity.instance_id, { signal })
      .then(data => {
        if (!signal.aborted) setReviews({ phase: 'ready', data, error: null });
      })
      .catch(err => {
        if (!signal.aborted)
          setReviews({ phase: 'error', data: null,
            error: [401, 403].includes(err.status)
              ? 'Review data access denied.'
              : 'Review queue could not be loaded.' });
      });

    api.investigationControl({ signal })
      .then(data => {
        if (!signal.aborted) setControl({ phase: 'ready', data, error: null });
      })
      .catch(err => {
        if (!signal.aborted)
          setControl({ phase: 'error', data: null,
            error: [401, 403].includes(err.status)
              ? 'Investigation control access denied.'
              : 'Investigation control could not be loaded.' });
      });

    if (canDiag) {
      api.diagnostics({ signal })
        .then(data => {
          if (!signal.aborted) setDiag({ phase: 'ready', data, error: null });
        })
        .catch(err => {
          if (!signal.aborted)
            setDiag({ phase: 'error', data: null,
              error: [401, 403].includes(err.status)
                ? 'Diagnostics access denied.'
                : 'Diagnostics could not be loaded.' });
        });
    }

    return () => controller.abort();
  }, [api, identity, canDiag, revision]);

  function refresh() {
    setRevision(r => r + 1);
  }

  async function changeMode(mode) {
    if (identity.role !== 'ADMIN' || control.phase !== 'ready'
        || controlChange.phase === 'submitting') return;
    setControlChange({ phase: 'submitting', error: null });
    try {
      if (!modeRequest.current || modeRequest.current.mode !== mode) {
        const key = globalThis.crypto?.randomUUID?.();
        if (!key) throw new Error('This browser cannot safely change investigation mode.');
        modeRequest.current = { mode, key };
      }
      const updated = await api.updateInvestigationControl({ mode,
        expectedVersion: control.data.version,
        idempotencyKey: modeRequest.current.key });
      modeRequest.current = null;
      setControl({ phase: 'ready', data: updated, error: null });
      setControlChange({ phase: 'idle', error: null });
    } catch (cause) {
      setControlChange({ phase: 'error',
        error: cause.message || 'Investigation mode could not be changed.' });
    }
  }

  if (!api || !identity) {
    return (
      <section className="view-placeholder" aria-labelledby="db-signin-title">
        <p className="eyebrow">Dashboard</p>
        <h2 id="db-signin-title">Confirm your identity to load the dashboard.</h2>
        <p>Use the identity form above to sign in.</p>
      </section>
    );
  }

  const casesOk   = cases.phase === 'ready'   && cases.data;
  const reviewsOk = reviews.phase === 'ready' && reviews.data;
  const diagOk    = diag.phase === 'ready'    && diag.data;

  // Compute attention count for the badge
  const attentionCount = (
    (casesOk   ? cases.data.filter(c => c.status === 'escalated').length : 0) +
    (reviewsOk ? reviews.data.items.length : 0)
  );

  return (
    <div className="db-root" aria-labelledby="db-heading">
      {/* Header row */}
      <div className="db-header">
        <div>
          <p className="eyebrow">Operations console</p>
          <h2 id="db-heading" className="db-heading-text">Overview</h2>
        </div>
        <div className="db-header-actions">
          {attentionCount > 0 && (
            <span className="db-attention-badge" aria-label={`${attentionCount} items require attention`}>
              {attentionCount} requiring attention
            </span>
          )}
          <button type="button" className="button-secondary" onClick={refresh}
            aria-label="Refresh dashboard data">
            Refresh
          </button>
        </div>
      </div>

      {/* System / provider status strip — from identity (already fetched, zero cost) */}
      <section aria-labelledby="db-system-title">
        <SectionHeader title="System status"
          note="Provider and memory state from current session" />
        <SystemStatus identity={identity} />
      </section>

      <section aria-labelledby="db-investigation-title">
        <SectionHeader title="AI investigation control"
          note="Persistent SQLite setting; changing mode never replays existing jobs" />
        {control.phase === 'loading' && <LoadingRow label="Loading AI investigation control…" />}
        {control.phase === 'error' && <ErrorRow message={control.error} />}
        {control.phase === 'ready' && (
          <InvestigationControlPanel control={control.data} isAdmin={identity.role === 'ADMIN'}
            phase={controlChange.phase} error={controlChange.error} onChange={changeMode} />
        )}
      </section>

      {/* Case tiles */}
      <section aria-labelledby="db-cases-title">
        <SectionHeader title="Support cases"
          note="All cases in this server process" />
        {cases.phase === 'loading' && <LoadingRow label="Loading case data\u2026" />}
        {cases.phase === 'error'   && <ErrorRow message={cases.error} />}
        {casesOk && <CaseTiles cases={cases.data} />}
      </section>

      {/* Pending reviews tile */}
      <section aria-labelledby="db-reviews-title">
        <SectionHeader title="Pending human reviews"
          note="Proposals awaiting a reviewer decision" />
        {reviews.phase === 'loading' && <LoadingRow label="Loading review queue\u2026" />}
        {reviews.phase === 'error'   && <ErrorRow message={reviews.error} />}
        {reviewsOk && <ReviewTile reviews={reviews.data} />}
      </section>

      {/* Items requiring attention */}
      {(casesOk || reviewsOk) && (
        <section aria-labelledby="db-attention-title">
          <SectionHeader title="Requiring attention"
            note="Escalated cases and pending reviews" />
          <AttentionItems
            cases={casesOk   ? cases.data   : []}
            reviews={reviewsOk ? reviews.data : null}
          />
        </section>
      )}

      {/* Two-column lower grid: pending reviews list + recent cases */}
      <div className="db-lower-grid">
        <section aria-labelledby="db-review-list-title">
          <SectionHeader title="Review queue" />
          {reviews.phase === 'loading' && <LoadingRow label="Loading\u2026" />}
          {reviews.phase === 'error'   && <ErrorRow message={reviews.error} />}
          {reviewsOk && (
            <PendingReviewsList reviews={reviews.data} onNavigate={onNavigate} />
          )}
        </section>

        <section aria-labelledby="db-recent-cases-title">
          <SectionHeader title="Recent cases"
            note="Last 8 by most recently updated" />
          {cases.phase === 'loading' && <LoadingRow label="Loading\u2026" />}
          {cases.phase === 'error'   && <ErrorRow message={cases.error} />}
          {casesOk && (
            <RecentCases cases={cases.data} onNavigate={onNavigate} />
          )}
        </section>
      </div>

      {/* Operational metrics — REVIEWER / ADMIN only */}
      {canDiag && (
        <section aria-labelledby="db-metrics-title">
          <SectionHeader title="Agent &amp; model metrics"
            note="Cumulative for this server process \u00b7 resets on restart" />
          {diag.phase === 'loading' && <LoadingRow label="Loading metrics\u2026" />}
          {diag.phase === 'error'   && <ErrorRow message={diag.error} />}
          {diagOk && <OpsMetrics snapshot={diag.data} />}
        </section>
      )}

      {/* Recent activity feed — REVIEWER / ADMIN only */}
      {canDiag && (
        <section aria-labelledby="db-activity-title">
          <SectionHeader title="Recent activity"
            note="Up to 10 most recent operational events" />
          {diag.phase === 'loading' && <LoadingRow label="Loading activity\u2026" />}
          {diag.phase === 'error'   && <ErrorRow message={diag.error} />}
          {diagOk && <ActivityFeed snapshot={diag.data} />}
        </section>
      )}

      {/* Footer note */}
      <p className="db-footer-note">
        Investigation mode and workflow state are durable when SQLite is configured.
        Operational metrics reset on server restart.
        Eligibility is not authorization. No business action is performed from this dashboard.
      </p>
    </div>
  );
}
