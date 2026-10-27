import { useCallback, useEffect, useState } from 'react';
import { operationLabels } from './diagnosticsContracts.js';
import useMetadataRead from './useMetadataRead.js';

const number = (value) => value.toLocaleString();
const milliseconds = (value) => `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ms`;

function failure(error) {
  if ([401, 403].includes(error.status)) return 'Diagnostics access was denied. Confirm an authorized reviewer or administrator identity.';
  if ([404, 503].includes(error.status)) return 'Diagnostics is unavailable. Check the local API, then refresh.';
  if (error.code === 'INVALID_DIAGNOSTICS_RESPONSE') return 'The diagnostics response could not be verified. No metadata was displayed. Refresh to try again.';
  return 'Diagnostics could not be loaded. Check the local API, then refresh.';
}

function Fact({ label, children }) {
  return <div><dt>{label}</dt><dd>{children}</dd></div>;
}

const eventTabs = ['workflow', 'model', 'tools', 'http'];

function DurableInvestigations({ api }) {
  const [filters, setFilters] = useState({ caseId: '', runId: '' });
  const [page, setPage] = useState(null);
  const [events, setEvents] = useState(null);
  const [tab, setTab] = useState('workflow');
  const [phase, setPhase] = useState('loading');
  const [eventPhase, setEventPhase] = useState('loading');
  const [message, setMessage] = useState(null);
  const [eventMessage, setEventMessage] = useState(null);

  const load = useCallback(async (offset = 0) => {
    setPhase('loading'); setMessage(null);
    try { setPage(await api.diagnosticInvestigations({ ...filters, offset })); setPhase('ready'); }
    catch (error) { setPhase('error'); setMessage(failure(error)); }
  }, [api, filters]);
  const loadEvents = useCallback(async (offset = 0) => {
    setEventPhase('loading'); setEventMessage(null);
    try { setEvents(await api.diagnosticEvents(tab, { offset })); setEventPhase('ready'); }
    catch (error) { setEventPhase('error'); setEventMessage(failure(error)); }
  }, [api, tab]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadEvents(); }, [loadEvents]);

  return <>
    <section className="run-event-section" aria-labelledby="latest-investigations-title">
      <div className="case-panel-heading">
        <div><p className="eyebrow">Durable SQLite history</p><h3 id="latest-investigations-title">Latest investigations</h3></div>
        <button className="button-secondary" onClick={() => load()} disabled={phase === 'loading'}>Refresh investigations</button>
      </div>
      <form className="audit-filters" onSubmit={(event) => { event.preventDefault(); load(); }}>
        <label>Case ID <input value={filters.caseId} onChange={(event) => setFilters({ ...filters, caseId: event.target.value })} /></label>
        <label>Run ID <input value={filters.runId} onChange={(event) => setFilters({ ...filters, runId: event.target.value })} /></label>
        <button className="button-secondary" type="submit" disabled={phase === 'loading'}>Search</button>
      </form>
      <p className="case-caption">Newest first. Metadata is durable across restart; model text, prompts, and customer data are intentionally excluded.</p>
      {phase === 'error' && <div className="case-error-banner" role="alert">{message}</div>}
      {phase === 'loading' && <p className="audit-loading" role="status">Loading investigations…</p>}
      {phase === 'ready' && page?.items.length === 0 && <p className="run-notice">No durable investigations match the search.</p>}
      {page?.items.length > 0 && <ol className="diagnostics-events" aria-label="Latest investigations">
        {page.items.map((item) => <li className="resolution-card" key={item.workflow_id}>
          <div className="audit-event-heading"><strong>{item.workflow_status}</strong><time dateTime={item.updated_at}>{new Date(item.updated_at).toLocaleString()}</time></div>
          <dl className="audit-event-facts">
            <Fact label="Case ID"><code>{item.case_id}</code></Fact><Fact label="Run ID"><code>{item.run_id}</code></Fact>
            <Fact label="Provider">{item.provider}</Fact><Fact label="Mode">{item.submission_mode ?? 'Unavailable'}</Fact>
            <Fact label="Failure">{item.failure_category ? `${item.failure_category} (${item.failure_stage ?? 'unknown stage'})` : 'None recorded'}</Fact>
            {item.error_source && <Fact label="Error source">{item.error_source}</Fact>}
            <Fact label="Response source">{item.response_source ?? 'Unavailable'}</Fact>
          </dl>
        </li>)}
      </ol>}
      {page?.has_more && <button className="button-secondary" onClick={() => load(page.next_offset)} disabled={phase === 'loading'}>Next investigations</button>}
    </section>
    <section className="run-event-section" aria-labelledby="diagnostic-events-title">
      <div className="case-panel-heading"><div><p className="eyebrow">Ephemeral process telemetry</p><h3 id="diagnostic-events-title">Event stream</h3></div><button className="button-secondary" onClick={() => loadEvents()} disabled={eventPhase === 'loading'}>Refresh events</button></div>
      <div role="tablist" aria-label="Diagnostic event categories">{eventTabs.map((name) => <button key={name} role="tab" aria-selected={tab === name} className="button-secondary" onClick={() => setTab(name)}>{name}</button>)}</div>
      <p className="case-caption">Newest first. This tabbed stream is process-local and resets on restart; durable status is shown above.</p>
      {eventMessage && <div className="case-error-banner" role="alert">{eventMessage}</div>}
      {eventPhase === 'loading' && <p className="audit-loading" role="status">Loading events…</p>}
      {eventPhase === 'ready' && events?.events.length === 0 && <p className="run-notice">No {tab} events are available.</p>}
      {events?.events.length > 0 && <ol className="diagnostics-events" aria-label={`${tab} events`}>
        {events.events.map((event, index) => <li className="resolution-card" key={`${event.span_id}-${index}`}><div className="audit-event-heading"><strong>{operationLabels[event.operation] ?? event.operation}</strong><time dateTime={event.timestamp}>{new Date(event.timestamp).toLocaleString()}</time></div><dl className="audit-event-facts"><Fact label="Error">{event.error ?? 'None recorded'}</Fact>{event.workflow_id && <Fact label="Workflow ID"><code>{event.workflow_id}</code></Fact>}{event.tool && <Fact label="Tool">{event.tool}</Fact>}</dl></li>)}
      </ol>}
      {events?.has_more && <button className="button-secondary" onClick={() => loadEvents(events.next_offset)} disabled={eventPhase === 'loading'}>Next events</button>}
    </section>
  </>;
}

// Presentational component takes only the validated diagnostics projection.
export function DiagnosticsSnapshot({ snapshot }) {
  return <>
    <p className="case-caption" role="status">
      Snapshot{' '}
      <time dateTime={snapshot.snapshot_at}>{new Date(snapshot.snapshot_at).toLocaleString()}</time>.
      {' '}Refresh for the latest records; times use your local time zone.
    </p>

    {/* Summary health tiles — warning/error tones when values indicate a problem */}
    <dl className="diagnostics-summary">
      <div>
        <dt>Recent events shown</dt>
        <dd>{number(snapshot.events.length)}</dd>
      </div>
      <div className={snapshot.omitted_events > 0 ? 'diagnostics-summary-tile--warning' : ''}>
        <dt>Older retained events omitted</dt>
        <dd>{number(snapshot.omitted_events)}</dd>
      </div>
      <div className={snapshot.evicted_events > 0 ? 'diagnostics-summary-tile--warning' : ''}>
        <dt>Events evicted from memory</dt>
        <dd>{number(snapshot.evicted_events)}</dd>
      </div>
      <div className={snapshot.logging_failures > 0 ? 'diagnostics-summary-tile--error' : ''}>
        <dt>Logging failures</dt>
        <dd>{number(snapshot.logging_failures)}</dd>
      </div>
    </dl>

    <section aria-labelledby="diagnostics-metrics-title">
      <p className="eyebrow">Operation metrics</p>
      <h3 id="diagnostics-metrics-title" className="diagnostics-section-heading">Cumulative in this server process</h3>
      <p className="case-caption">
        Including events no longer retained. Operations can overlap; these are not independent request totals.
      </p>
      {snapshot.metrics.length === 0
        ? <p className="run-notice">No operation metrics have been recorded.</p>
        : <ul className="diagnostics-metrics" aria-label="Operation metrics">
            {snapshot.metrics.map((metric) => (
              <li key={metric.operation}
                className={`resolution-card${metric.errors > 0 ? ' diagnostics-metric-card--has-errors' : ''}`}>
                <p className="eyebrow">{operationLabels[metric.operation]}</p>
                <strong className="diagnostics-metric-count">{number(metric.count)}</strong>
                {metric.errors > 0
                  ? <p className="diagnostics-metric-errors">{number(metric.errors)} error{metric.errors === 1 ? '' : 's'}</p>
                  : <p className="diagnostics-metric-ok">No errors</p>}
                <dl className="audit-event-facts">
                  <Fact label="Total duration">{milliseconds(metric.duration_ms_total)}</Fact>
                  <Fact label="Maximum duration">{milliseconds(metric.duration_ms_max)}</Fact>
                  {metric.provider_calls > 0 && <Fact label="Provider calls">{number(metric.provider_calls)}</Fact>}
                </dl>
                {metric.provider_calls > 0 && (
                  <div className="diagnostics-usage">
                    <p className="eyebrow">Model token usage</p>
                    <p className="case-caption">Known counts are subtotals. Calls with unknown usage are not counted as zero.</p>
                    <dl className="audit-event-facts">
                      {['input', 'output', 'total'].map((part) => (
                        <Fact key={part} label={`${part[0].toUpperCase()}${part.slice(1)} tokens`}>
                          {number(metric[`${part}_tokens_known`])} known
                          {' \u00b7 '}{number(metric[`${part}_tokens_unknown_calls`])} calls unknown
                        </Fact>
                      ))}
                    </dl>
                  </div>
                )}
              </li>
            ))}
          </ul>}
    </section>

    <section className="run-event-section" aria-labelledby="diagnostics-events-title">
      <p className="eyebrow">Recent activity</p>
      <h3 id="diagnostics-events-title" className="diagnostics-section-heading">Operational events</h3>
      <p className="case-caption">
        Up to 50 recent records in backend recording order.
        Trace IDs correlate available records; omitted or evicted spans may leave gaps.
      </p>
      {snapshot.events.length === 0
        ? <p className="run-notice">No recent operational events are available.</p>
        : <ol className="diagnostics-events" aria-label="Recent operational events" tabIndex={0}>
            {snapshot.events.map((event, index) => (
              <li key={`${event.span_id}-${index}`}
                className={`resolution-card${event.error ? ' diagnostics-event-card--error' : ''}`}>
                <div className="audit-event-heading">
                  <div>
                    <p className={`diagnostics-event-op-label${event.error ? ' diagnostics-event-op-label--error' : ''}`}>
                      {operationLabels[event.operation] ?? event.operation}
                    </p>
                    {event.error && (
                      <p className="diagnostics-event-error-code">{event.error}</p>
                    )}
                  </div>
                  <time dateTime={event.timestamp}>{new Date(event.timestamp).toLocaleString()}</time>
                </div>
                <dl className="audit-event-facts">
                  <Fact label="Duration">{milliseconds(event.duration_ms)}</Fact>
                  {!event.error && <Fact label="Error code">No error recorded</Fact>}
                  {event.tool !== null && <Fact label="Tool">{event.tool}</Fact>}
                  {event.status !== null && <Fact label="Recorded status">{event.status}</Fact>}
                  {event.method !== null && <Fact label="HTTP method">{event.method}</Fact>}
                  {event.status_code !== null && <Fact label="HTTP status">{event.status_code}</Fact>}
                  {event.provider !== null && <Fact label="Provider category">{event.provider}</Fact>}
                  {event.provider_called !== null && <Fact label="Provider call recorded">{event.provider_called ? 'Yes' : 'No'}</Fact>}
                  {event.provider_status_code !== null && <Fact label="Provider HTTP status">{event.provider_status_code}</Fact>}
                  {event.invalid_response_stage !== null && <Fact label="Invalid response stage">{event.invalid_response_stage}</Fact>}
                  {event.invalid_response_category !== null && <Fact label="Invalid response category">{event.invalid_response_category}</Fact>}
                  {event.output_schema_errors?.map((issue, index) => (
                    <Fact key={index} label={`Output schema issue ${index + 1}`}>
                      {issue.location.join('.') || '<unknown>'}: {issue.type}
                    </Fact>
                  ))}
                  {event.output_schema_errors_truncated === true && <Fact label="Output schema issues">Additional issues omitted</Fact>}
                  {event.reviewer_authenticated !== null && <Fact label="Reviewer authenticated at event">{event.reviewer_authenticated ? 'Yes' : 'No'}</Fact>}
                </dl>
                {(event.operation === 'llm' || event.provider_called === true
                  || event.input_tokens !== null || event.output_tokens !== null
                  || event.total_tokens !== null || event.output_token_limit !== null) && (
                  <dl className="audit-event-facts">
                    <Fact label="Input tokens">{event.input_tokens === null ? 'Unknown' : number(event.input_tokens)}</Fact>
                    <Fact label="Output tokens">{event.output_tokens === null ? 'Unknown' : number(event.output_tokens)}</Fact>
                    <Fact label="Total tokens">{event.total_tokens === null ? 'Unknown' : number(event.total_tokens)}</Fact>
                    {event.output_token_limit !== null && <Fact label="Output token limit">{number(event.output_token_limit)}</Fact>}
                  </dl>
                )}
                <details className="diagnostics-trace">
                  <summary>Trace and context identifiers</summary>
                  <dl className="audit-event-facts">
                    <Fact label="Trace ID"><code>{event.trace_id}</code></Fact>
                    <Fact label="Span ID"><code>{event.span_id}</code></Fact>
                    {event.parent_span_id !== null && <Fact label="Parent span ID"><code>{event.parent_span_id}</code></Fact>}
                    {event.case_id !== null && <Fact label="Case ID"><code>{event.case_id}</code></Fact>}
                    {event.workflow_id !== null && <Fact label="Workflow ID"><code>{event.workflow_id}</code></Fact>}
                    {event.proposal_id !== null && <Fact label="Proposal ID"><code>{event.proposal_id}</code></Fact>}
                    {event.reviewer_user_id !== null && <Fact label="Reviewer user ID"><code>{event.reviewer_user_id}</code></Fact>}
                  </dl>
                </details>
              </li>
            ))}
          </ol>}
    </section>
  </>;
}

function DiagnosticsRead({ api, onDenied }) {
  const load = useCallback((signal) => api.diagnostics({ signal }), [api]);
  const state = useMetadataRead(load, onDenied, failure);
  return (
    <section className="case-panel" aria-labelledby="diagnostics-title" aria-busy={state.phase === 'loading'}>
      <div className="case-panel-heading">
        <div>
          <p className="eyebrow">Operational metadata</p>
          <h2 id="diagnostics-title">Process snapshot</h2>
        </div>
        <button className="button-secondary" onClick={state.refresh} disabled={state.phase === 'loading'}>
          Refresh diagnostics
        </button>
      </div>
      {state.phase === 'loading' && (
        <p className="audit-loading" role="status">
          <span className="case-loading-dot" aria-hidden="true" />
          Loading diagnostics\u2026
        </p>
      )}
      {state.phase === 'error' && (
        <div className="case-error-banner" role="alert">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
            aria-hidden="true">
            <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
          </svg>
          <span>{state.message}</span>
        </div>
      )}
      {state.data && <DiagnosticsSnapshot snapshot={state.data} />}
    </section>
  );
}

export default function DiagnosticsWorkspace({ api, identity }) {
  const [denied, setDenied] = useState(false);
  const allowed = api && identity && identity.role === 'ADMIN';
  return (
    <div className="diagnostics-workspace">
      <p className="run-notice">
        Read-only operational metadata for this server process. Records reset on restart.
        This snapshot is not a health assessment, an audit of business decisions,
        or authorization to execute actions.
      </p>
      {(!api || !identity) ? (
        <section className="view-placeholder" aria-labelledby="diag-signin-title">
          <p className="eyebrow">Diagnostics</p>
          <h2 id="diag-signin-title">Confirm your identity to open Diagnostics.</h2>
          <p>Use the token form above with a reviewer or administrator account.</p>
        </section>
      ) : (!allowed || denied) ? (
        <section className="view-placeholder" aria-labelledby="diag-access-title">
          <p className="eyebrow">Restricted workspace</p>
        <h2 id="diag-access-title">Administrator access required.</h2>
          <p>Durable investigation diagnostics are restricted to authorized administrators.</p>
        </section>
      ) : (
        <><DurableInvestigations api={api} /><DiagnosticsRead api={api} onDenied={setDenied} /></>
      )}
    </div>
  );
}
