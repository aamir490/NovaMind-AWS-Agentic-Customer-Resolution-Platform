import { useCallback, useEffect, useRef, useState } from 'react';

import AuditTimeline from './AuditTimeline.jsx';
import useMetadataRead from './useMetadataRead.js';

const date = (value) => new Date(value).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' });

// Short UUID tail for run selectors — keeps options scannable.
const shortId = (id) => id ? `\u2026${id.slice(-8)}` : '';

// Map RunSnapshot status to a design-system tone class.
function runStatusTone(status) {
  if (status === 'FAILED')                            return 'error';
  if (status === 'REVIEW_REQUIRED' || status === 'REVIEWED') return 'warning';
  if (status === 'COMPLETED')                         return 'success';
  return 'open';   // RUNNING / RESUMING
}

function failure(error) {
  if ([401, 403].includes(error.status)) return 'Audit access was denied. Confirm an authorized reviewer or administrator identity.';
  if (error.code === 'AUDIT_PENDING_USE_EVENTS') return 'Audit metadata is pending while this run executes or resumes. Refresh after it pauses or finishes.';
  if (error.status === 404) return 'This resource is no longer available. Refresh the case list and confirm your identity again.';
  if (error.code === 'INVALID_AUDIT_RESPONSE' || [409, 422].includes(error.status)) return 'The selection or audit response could not be verified. Refresh the case list. If the server restarted, confirm your identity again.';
  if (error.status === 503) return 'Audit data or the service is unavailable. Refresh when the service is available.';
  return 'Audit data could not be loaded or verified. Check the local API and refresh to try again.';
}

// Each selection owns a cancellable request. Keyed children drop old results on
// case/run/page changes; unmounting or signing out aborts in-flight reads.
function useAuditRead(load, onDenied) {
  return useMetadataRead(load, onDenied, failure);
}

function usePageHeading(focusHeading) {
  const heading = useRef(null);
  useEffect(() => { if (focusHeading) heading.current?.focus(); }, [focusHeading]);
  return heading;
}

function LoadingRow({ label }) {
  return (
    <p className="audit-loading" role="status">
      <span className="case-loading-dot" aria-hidden="true" />
      {label}
    </p>
  );
}

function ErrorRow({ message }) {
  return (
    <div className="case-error-banner" role="alert">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
        aria-hidden="true">
        <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
      </svg>
      <span>{message}</span>
    </div>
  );
}

function ReadStatus({ state, loading }) {
  return <>
    {state.phase === 'loading' && <LoadingRow label={loading} />}
    {state.phase === 'error'   && <ErrorRow message={state.message} />}
  </>;
}

function AuditEvents({ api, binding, onDenied }) {
  const [cursors, setCursors] = useState([0]);
  const [focusHeading, setFocusHeading] = useState(false);
  const after = cursors.at(-1);
  return <AuditEventPage key={after} api={api} binding={binding} onDenied={onDenied} after={after} focusHeading={focusHeading}
    previous={cursors.length > 1 ? () => { setFocusHeading(true); setCursors((values) => values.slice(0, -1)); } : null}
    next={(cursor) => { setFocusHeading(true); setCursors((values) => [...values, cursor]); }} />;
}

function AuditEventPage({ api, binding, onDenied, after, previous, next, focusHeading }) {
  const load = useCallback((signal) => api.runAudit(binding, after, { signal }), [api, binding, after]);
  const state = useAuditRead(load, onDenied);
  const heading = usePageHeading(focusHeading);
  return <section className="case-panel" aria-labelledby="audit-events-title" aria-busy={state.phase === 'loading'}>
    <div className="case-panel-heading">
      <div>
        <p className="eyebrow">Workflow history</p>
        <h3 id="audit-events-title" ref={heading} tabIndex={-1}>Run audit timeline</h3>
      </div>
      <button className="button-secondary" onClick={state.refresh} disabled={state.phase === 'loading'}>
        Refresh audit page
      </button>
    </div>
    <p className="case-caption">Run <code className="case-mono">{binding.run_id}</code></p>
    <ReadStatus state={state} loading="Loading audit timeline\u2026" />
    {state.data && <>
      <AuditTimeline events={state.data.events} after={after} hasMore={state.data.has_more} />
      <p className="case-caption" role="status">
        {state.data.events.length} {state.data.events.length === 1 ? 'record' : 'records'} on this page.
        {' '}{state.data.has_more ? 'More records are available.' : 'End of the available audit history.'}
      </p>
    </>}
    <div className="audit-pagination">
      {previous && <button className="button-secondary" onClick={previous}>Previous audit page</button>}
      {state.data?.has_more && <button className="button-secondary" onClick={() => next(state.data.next_after)}>Next audit page</button>}
    </div>
  </section>;
}

function RunPage({ api, instanceId, caseId, offset, onDenied, previous, next, focusHeading }) {
  const load = useCallback((signal) => api.auditRuns({ instance_id: instanceId, case_id: caseId }, offset, { signal }),
    [api, instanceId, caseId, offset]);
  const state = useAuditRead(load, onDenied);
  const [selected, setSelected] = useState(null);
  const heading = usePageHeading(focusHeading);
  function refresh() { setSelected(null); state.refresh(); }
  return <>
    <section className="case-panel" aria-labelledby="audit-runs-title" aria-busy={state.phase === 'loading'}>
      <div className="case-panel-heading">
        <div>
          <p className="eyebrow">Select a run</p>
          <h3 id="audit-runs-title" ref={heading} tabIndex={-1}>Case runs</h3>
        </div>
        <button className="button-secondary" onClick={refresh} disabled={state.phase === 'loading'}>
          Refresh run page
        </button>
      </div>
      <ReadStatus state={state} loading="Loading case runs\u2026" />
      {state.data && <>
        {state.data.items.length === 0
          ? <p className="run-notice" role="status">No runs are available on this page for this case.</p>
          : <label htmlFor="audit-run">
              Run
              <select id="audit-run" value={selected?.run_id ?? ''} onChange={(event) => {
                const run = state.data.items.find((item) => item.run_id === event.target.value);
                setSelected(run ? { ...run, case_id: caseId, instance_id: instanceId } : null);
              }}>
                <option value="">Select a run\u2026</option>
                {state.data.items.map((run) => (
                  <option key={run.run_id} value={run.run_id}>
                    {date(run.created_at)} \u00b7 {run.status} \u00b7 {shortId(run.run_id)}
                  </option>
                ))}
              </select>
            </label>}
        <p className="case-caption">
          Showing {state.data.items.length} run{state.data.items.length === 1 ? '' : 's'} from position {offset + 1}.
          {' '}Run statuses reflect the last run-list refresh.
        </p>
      </>}
      <div className="audit-pagination">
        {previous && <button className="button-secondary" onClick={previous}>Previous run page</button>}
        {state.data?.next_offset !== null && state.data?.next_offset !== undefined
          && <button className="button-secondary" onClick={() => next(state.data.next_offset)}>Next run page</button>}
      </div>
      {/* Selected run summary with tonal status badge */}
      {selected && (
        <dl className="case-facts audit-run-facts">
          <div>
            <dt>Last loaded run status</dt>
            <dd>
              <span className={`case-status case-status--${runStatusTone(selected.status)}`}>
                {selected.status}
              </span>
            </dd>
          </div>
          <div><dt>Workflow ID</dt><dd className="case-mono">{selected.workflow_id ?? 'Not available'}</dd></div>
          <div><dt>Run last updated</dt><dd><time dateTime={selected.updated_at}>{new Date(selected.updated_at).toLocaleString()}</time></dd></div>
        </dl>
      )}
    </section>
    {selected && <AuditEvents key={selected.run_id} api={api} binding={selected} onDenied={onDenied} />}
  </>;
}

function CaseRuns({ api, instanceId, caseId, onDenied }) {
  const [offset, setOffset] = useState(0);
  const [focusHeading, setFocusHeading] = useState(false);
  return <RunPage key={offset} api={api} instanceId={instanceId} caseId={caseId} onDenied={onDenied} offset={offset} focusHeading={focusHeading}
    previous={offset > 0 ? () => { setFocusHeading(true); setOffset((value) => value - 50); } : null}
    next={(value) => { setFocusHeading(true); setOffset(value); }} />;
}

function AuditCases({ api, identity, onDenied }) {
  // Load audit case IDs and the full case list in parallel so we can display
  // human-readable subjects in the selector. The case list may fail without
  // breaking the audit flow — fall back to bare UUIDs gracefully.
  const loadAudit = useCallback((signal) => api.auditCases({ signal }), [api]);
  const auditState = useAuditRead(loadAudit, onDenied);
  const [caseId, setCaseId] = useState('');

  // Subject lookup: fires once when auditState has data; independent abort.
  const [subjects, setSubjects] = useState({});
  const subjectAbort = useRef(null);
  useEffect(() => {
    if (!auditState.data?.length) return;
    subjectAbort.current?.abort();
    const controller = new AbortController();
    subjectAbort.current = controller;
    api.listCases()
      .then((cases) => {
        if (controller.signal.aborted) return;
        const map = {};
        cases.forEach((c) => { map[c.id] = c.subject; });
        setSubjects(map);
      })
      .catch(() => {/* ignore — falls back to UUID labels */});
    return () => controller.abort();
  }, [api, auditState.data]);

  // Build a scannable label: subject (if known) + short UUID
  function caseLabel(id) {
    const subject = subjects[id];
    return subject ? `${subject} \u2014 \u2026${id.slice(-8)}` : `\u2026${id.slice(-8)}`;
  }

  return <>
    <section className="case-selector-panel" aria-labelledby="audit-cases-title" aria-busy={auditState.phase === 'loading'}>
      <div className="case-panel-heading">
        <div>
          <p className="eyebrow">Select a case</p>
          <h2 id="audit-cases-title">Available cases</h2>
        </div>
        <button className="button-secondary" disabled={auditState.phase === 'loading'}
          onClick={() => { setCaseId(''); setSubjects({}); auditState.refresh(); }}>
          Refresh case list
        </button>
      </div>
      {auditState.phase === 'loading' && <LoadingRow label="Loading available cases\u2026" />}
      {auditState.phase === 'error'   && <ErrorRow message={auditState.message} />}
      {auditState.data && (
        auditState.data.length === 0
          ? <p className="run-notice" role="status">No cases are available to this identity.</p>
          : <label htmlFor="audit-case">
              Case
              <select id="audit-case" value={caseId} onChange={(event) => setCaseId(event.target.value)}>
                <option value="">Select a case\u2026</option>
                {auditState.data.map((item) => (
                  <option key={item.id} value={item.id}>{caseLabel(item.id)}</option>
                ))}
              </select>
            </label>
      )}
      {auditState.data?.length > 0 && !caseId && (
        <p className="case-caption">Select a case, then a run to view its audit metadata.</p>
      )}
    </section>
    {caseId && <CaseRuns key={caseId} api={api} instanceId={identity.instance_id} caseId={caseId} onDenied={onDenied} />}
  </>;
}

export default function AuditWorkspace({ api, identity }) {
  const [denied, setDenied] = useState(false);
  const allowed = api && identity && ['REVIEWER', 'ADMIN'].includes(identity.role);
  return (
    <div className="audit-workspace">
      <p className="run-notice">
        Read-only workflow audit metadata. When durable workflow persistence is configured, runs remain available after restart;
        in-memory runs remain process-local. Strands audit history includes persisted lifecycle events only. This is not a complete compliance log.
        A recorded approval does not mean a business action was executed.
      </p>
      {(!api || !identity) ? (
        <section className="view-placeholder" aria-labelledby="audit-signin-title">
          <p className="eyebrow">Audit</p>
          <h2 id="audit-signin-title">Confirm your identity to open Audit.</h2>
          <p>Use the token form above with a reviewer or administrator account.</p>
        </section>
      ) : (!allowed || denied) ? (
        <section className="view-placeholder" aria-labelledby="audit-access-title">
          <p className="eyebrow">Restricted workspace</p>
          <h2 id="audit-access-title">Reviewer access required.</h2>
          <p>Audit requires an authorized reviewer or administrator. Confirm an authorized identity to continue.</p>
        </section>
      ) : (
        <AuditCases api={api} identity={identity} onDenied={setDenied} />
      )}
    </div>
  );
}
