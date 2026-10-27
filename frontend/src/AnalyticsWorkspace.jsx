import { useEffect, useMemo, useState } from 'react';

const labels = {
  open: 'Open', in_review: 'In review', escalated: 'Escalated', resolved: 'Resolved',
  RUNNING: 'Running', RESUMING: 'Resuming', REVIEW_REQUIRED: 'Review required', REVIEWED: 'Reviewed',
  COMPLETED: 'Completed', FAILED: 'Failed', RECOVERY_REQUIRED: 'Recovery required',
  MANUAL: 'Manual', AUTO: 'Auto', UNAVAILABLE: 'Unavailable',
  local_scripted: 'Offline provider', bedrock: 'Amazon Bedrock',
  BEDROCK_VALIDATED: 'Validated model output', DETERMINISTIC_FALLBACK: 'Deterministic fallback',
  APPROVE_REPLACEMENT: 'Approve replacement', DECLINE_REPLACEMENT: 'Decline replacement',
  REQUEST_MORE_EVIDENCE: 'Request more evidence', ESCALATE: 'Escalations',
};

const number = (value) => value.toLocaleString();

function BarChart({ title, values, tone = 'cyan' }) {
  const entries = Object.entries(values);
  const maximum = Math.max(1, ...entries.map(([, value]) => value));
  return <section className={`analytics-chart analytics-chart--${tone}`} aria-label={title}>
    <h3>{title}</h3>
    {entries.length === 0 ? <p className="run-notice">No recorded measurements.</p> : <div className="analytics-bars">
      {entries.map(([key, value]) => <div className="analytics-bar" key={key}>
        <div className="analytics-bar__track"><span style={{ height: `${(value / maximum) * 100}%` }} /></div>
        <strong>{number(value)}</strong><small>{labels[key] ?? key}</small>
      </div>)}
    </div>}
  </section>;
}
function Metric({ label, value, detail }) {
  return <div className="analytics-metric"><span>{label}</span><strong>{value}</strong>{detail && <small>{detail}</small>}</div>;
}
function failure(error) {
  if (error.code === 'INVALID_ANALYTICS_RESPONSE') return 'Analytics data could not be verified. No aggregate data was displayed.';
  if ([401, 403].includes(error.status)) return 'Analytics is restricted to authenticated administrators.';
  if (error.status === 422) return 'Choose a valid date range of 366 days or fewer.';
  return 'Analytics could not be loaded. Refresh to try again.';
}

export default function AnalyticsWorkspace({ api, identity }) {
  const [filters, setFilters] = useState({ from: '', to: '' });
  const [data, setData] = useState(null);
  const [phase, setPhase] = useState('loading');
  const [message, setMessage] = useState('');
  const query = useMemo(() => ({ ...filters }), [filters]);

  async function load(next = query) {
    if (phase === 'loading') return;
    setPhase('loading'); setMessage('');
    try { setData(await api.analytics(next)); setPhase('ready'); }
    catch (error) { setData(null); setMessage(failure(error)); setPhase('error'); }
  }

  useEffect(() => {
    let mounted = true;
    setPhase('loading');
    api.analytics(query).then((result) => { if (mounted) { setData(result); setPhase('ready'); } })
      .catch((error) => { if (mounted) { setMessage(failure(error)); setPhase('error'); } });
    return () => { mounted = false; };
  }, [api]); // Load once per authenticated API instance.

  if (identity?.role !== 'ADMIN') {
    return <section className="view-placeholder" aria-labelledby="analytics-access-title"><p className="eyebrow">Analytics</p><h2 id="analytics-access-title">Administrator access required.</h2><p>Aggregate resolution analytics are not available to reviewer or customer identities.</p></section>;
  }

  return <section className="analytics-workspace" aria-labelledby="analytics-title" aria-busy={phase === 'loading'}>
    <div className="workspace-section-heading"><div><p className="eyebrow">Durable SQLite history</p><h2 id="analytics-title">AI Resolution Analytics</h2><p>Aggregate measurements from persisted cases, investigation metadata, audit records, and checkpoint projections.</p></div><button className="button-secondary" onClick={() => load()} disabled={phase === 'loading'}>{phase === 'loading' ? 'Loading…' : 'Refresh analytics'}</button></div>
    <form className="analytics-filters" onSubmit={(event) => { event.preventDefault(); load(filters); }}>
      <label>From <input type="date" value={filters.from} max={filters.to || undefined} onChange={(event) => setFilters({ ...filters, from: event.target.value })} /></label>
      <label>To <input type="date" value={filters.to} min={filters.from || undefined} onChange={(event) => setFilters({ ...filters, to: event.target.value })} /></label>
      <button type="submit" className="button-secondary" disabled={phase === 'loading'}>Apply date filter</button>
    </form>
    <p className="case-caption">Window: {data ? `${data.from_date} through ${data.to_date}` : 'not loaded'}. Counts are persisted aggregates; missing measurements remain unavailable.</p>
    {phase === 'error' && <div className="case-error-banner" role="alert">{message}</div>}
    {phase === 'loading' && <p className="case-notice" role="status">Loading durable analytics…</p>}
    {data && <>
      <div className="analytics-metrics" aria-label="Analytics totals">
        <Metric label="Cases" value={number(Object.values(data.case_status_counts).reduce((a, b) => a + b, 0))} detail="Created in window" />
        <Metric label="Investigations" value={number(data.workflow_count)} detail="Durable workflow runs" />
        <Metric label="HITL decisions" value={number(data.hitl_decision_count)} detail="Persisted audit events" />
        <Metric label="Escalations" value={number(data.decision_counts.ESCALATE ?? 0)} detail="Admin case decisions" />
      </div>
      <div className="analytics-chart-grid">
        <BarChart title="Case status" values={data.case_status_counts} />
        <BarChart title="Investigation mode" values={data.mode_counts} tone="purple" />
        <BarChart title="Workflow status" values={data.workflow_status_counts} />
        <BarChart title="Response source" values={data.response_source_counts} tone="purple" />
        <BarChart title="Admin decisions" values={data.decision_counts} />
        <BarChart title="Provider" values={data.provider_counts} tone="purple" />
      </div>
      <section className="analytics-duration" aria-labelledby="analytics-duration-title">
        <div><p className="eyebrow">Measured workflow intervals</p><h3 id="analytics-duration-title">Durations</h3></div>
        {data.duration_measurement_available ? <div className="analytics-duration__values"><Metric label="Measured runs" value={number(data.duration_count)} /><Metric label="Minimum" value={`${Math.round(data.duration_min_ms)} ms`} /><Metric label="Average" value={`${Math.round(data.duration_avg_ms)} ms`} /><Metric label="Maximum" value={`${Math.round(data.duration_max_ms)} ms`} /></div>
          : <p className="run-notice">No complete duration measurement is available for this window. Workflow metadata timestamps are missing or the bounded measurement set was exceeded.</p>}
      </section>
      <p className="case-caption analytics-disclaimer">No token counts, costs, model success rate, or unrecorded Bedrock activity is inferred. “Validated model output” and “Deterministic fallback” are counted only when the durable checkpoint projection records the source.</p>
    </>}
  </section>;
}
