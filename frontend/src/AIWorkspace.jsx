import { useEffect, useRef, useState } from 'react';
import KnowledgeEvidence from './KnowledgeEvidence.jsx';
import { reviewRequestStatusForDisplay } from './reviewPresentation.js';

function createRequestId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const rejectedMessages = {
  PROVIDER_UNAVAILABLE: 'The run provider is unavailable. No run was started.',
  RUN_CAPACITY_UNAVAILABLE: 'The service cannot accept another run right now. No run was started.',
  RUN_RETENTION_FULL: 'The service has reached its run limit. No run was started.',
  CONVERSATION_UNAVAILABLE: 'Conversation storage is unavailable. No run was started.',
};
const activeRunStatuses = new Set(['RUNNING', 'RESUMING', 'REVIEW_REQUIRED']);
const maxDisplayedEvents = 256;

const runStates = {
  RUNNING:         { label: 'Running',                  tone: 'active',   detail: 'The workflow is in progress.' },
  RESUMING:        { label: 'Resuming',                 tone: 'active',   detail: 'The workflow is resuming after a pause.' },
  REVIEW_REQUIRED: { label: 'Awaiting human review',    tone: 'waiting',  detail: 'The workflow is paused. A proposal is pending human review.' },
  REVIEWED:        { label: 'Review recorded',          tone: 'waiting',  detail: 'Human review has been recorded. This does not mean a business action was executed.' },
  COMPLETED:       { label: 'Completed',                tone: 'complete', detail: 'The workflow has finished. Its confirmed response and activity are shown below.' },
  FAILED:          { label: 'Failed',                   tone: 'error',    detail: 'The workflow has stopped with a failure.' },
};

// Workflow pipeline stages — derived entirely from backend-reported data.
// A stage is "done" when the evidence is confirmed, "active" when in progress,
// "pending" when not yet reached. Never infer or invent state.
function pipelineStages(attempt, tracking) {
  const snapshot = tracking?.snapshot ?? attempt?.snapshot;
  const status   = snapshot?.status;
  const events   = tracking?.events ?? [];
  const hasTools = events.some((e) => e.kind === 'TOOL');
  const hasKnowledge = events.some((e) => e.kind === 'TOOL' && e.tool === 'search_knowledge');
  const hasProposal  = !!snapshot?.review;
  const isActive     = activeRunStatuses.has(status);

  // Map to a coarse pipeline position (0-5)
  let pos = 0;
  if (attempt?.state === 'sending') pos = 0;
  else if (attempt?.state === 'accepted' || attempt?.state === 'uncertain') pos = 1;
  if (status === 'RUNNING' || status === 'RESUMING') pos = hasTools ? 2 : 1;
  if (hasKnowledge) pos = 2;
  if (hasProposal) pos = 3;
  if (status === 'REVIEW_REQUIRED' || status === 'REVIEWED') pos = 4;
  if (status === 'COMPLETED' || status === 'FAILED') pos = 5;

  const stage = (idx, label, icon) => ({
    idx, label, icon,
    state: idx < pos ? 'done' : idx === pos ? (status === 'FAILED' ? 'error' : isActive ? 'active' : 'reached') : 'pending',
  });

  const pipeline = [
    stage(0, 'Request',    'M12 5v14M5 12l7-7 7 7'),
    stage(1, 'Processing', 'M12 2a10 10 0 100 20A10 10 0 0012 2zm0 6v4l3 3'),
    stage(2, 'Evidence',   'M9 4H5v17h14V4h-4M9 3h6v4H9zM8 9h8M8 13h8M8 17h5'),
    stage(3, 'Proposal',   'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8zM14 2v6h6M16 13H8M16 17H8M10 9H8'),
    stage(4, 'Review',     'M9 4H5v17h14V4h-4M9 3h6v4H9zM8 13l3 3 5-6'),
    stage(5, 'Outcome',    'M22 11.08V12a10 10 0 11-5.93-9.14M22 4L12 14.01l-3-3'),
  ];
  // Evidence (idx 2) must only show "done" when search_knowledge was actually
  // reported in backend events. Status-driven pos advancement (pos > 2 when
  // Proposal/Review is reached) must not imply knowledge retrieval occurred.
  if (!hasKnowledge && pipeline[2].state === 'done') {
    pipeline[2] = { ...pipeline[2], state: 'pending' };
  }
  return pipeline;
}

function trackingError(error) {
  if (error.status === 401) return 'Your session could not be verified. Live tracking has stopped. Confirm your identity before checking existing records.';
  if (error.status === 403) return 'Access to this run was denied. Live tracking has stopped.';
  if (error.status === 404) return 'This run is no longer available. It may have been lost after a server restart. Do not resubmit automatically.';
  if (error.status === 409) return 'The run or event cursor is no longer valid. Tracking has stopped; inspect existing records before submitting again.';
  return 'Live updates could not be verified. The last confirmed status and events are shown. Retry live tracking to check this same run. This does not cancel or resubmit the run.';
}

// Compact human-readable timestamp
function fmtTime(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch { return iso; }
}

export default function AIWorkspace({ api, identity }) {
  const [cases, setCases]           = useState(null);
  const [caseId, setCaseId]         = useState('');
  const [message, setMessage]       = useState('');
  const [refundIntent, setRefundIntent] = useState(false);
  const [refundSku, setRefundSku]   = useState('');
  const [loadingCases, setLoadingCases] = useState(false);
  const [caseError, setCaseError]   = useState('');
  const [attempt, setAttempt]       = useState(null);
  const [tracking, setTracking]     = useState(null);
  const [trackingRetry, setTrackingRetry] = useState(0);
  const [retryingTracking, setRetryingTracking] = useState(false);
  const lastTracking      = useRef(null);
  const mounted           = useRef(false);
  const caseRequest       = useRef(false);
  const submitted         = useRef(false);
  const trackingRetryRequest = useRef(false);
  const locked = attempt !== null && attempt.state !== 'rejected';

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    if (!api || attempt?.state !== 'accepted') return;
    const binding = attempt.snapshot;
    const controller = new AbortController();
    const { signal } = controller;
    let timer;
    let current = lastTracking.current?.snapshot.run_id === binding.run_id ? lastTracking.current
      : { snapshot: binding, events: [], after: 0, gap: false, trimmed: false };

    function publish(changes) {
      if (signal.aborted) return;
      current = { ...current, ...changes };
      lastTracking.current = current;
      setTracking(current);
    }

    function checkSnapshot(snapshot) {
      if (snapshot.last_sequence < current.snapshot.last_sequence
          || Date.parse(snapshot.updated_at) < Date.parse(current.snapshot.updated_at)) {
        throw new Error('Stale run snapshot');
      }
    }

    async function poll() {
      try {
        const snapshot = await api.runSnapshot(binding, { signal });
        if (signal.aborted) return;
        checkSnapshot(snapshot);
        publish({ snapshot });
        let page;
        do {
          page = await api.runEvents(binding, current.after, { signal });
          if (signal.aborted) return;
          checkSnapshot(page.snapshot);
          const events = [...current.events, ...page.events];
          publish({ snapshot: page.snapshot, after: page.next_after,
            events: events.slice(-maxDisplayedEvents), gap: current.gap || page.gap,
            trimmed: current.trimmed || events.length > maxDisplayedEvents });
        } while (page.has_more);
        if (activeRunStatuses.has(current.snapshot.status)) timer = setTimeout(poll, 1500);
        else publish({ phase: 'stopped' });
      } catch (error) {
        if (signal.aborted) return;
        publish({ phase: 'error', error: trackingError(error),
          canRetry: ![401, 403, 404, 409].includes(error.status) });
      } finally {
        if (!signal.aborted) {
          trackingRetryRequest.current = false;
          setRetryingTracking(false);
        }
      }
    }

    publish({ phase: 'polling', error: '', canRetry: false });
    poll();
    return () => { clearTimeout(timer); controller.abort(); };
  }, [api, attempt, trackingRetry]);

  function retryTracking() {
    if (!tracking?.canRetry || trackingRetryRequest.current) return;
    trackingRetryRequest.current = true;
    setRetryingTracking(true);
    setTrackingRetry((value) => value + 1);
  }

  async function loadCases() {
    if (!api || caseRequest.current || submitted.current) return;
    caseRequest.current = true;
    setLoadingCases(true); setCaseError(''); setCases(null); setCaseId('');
    try {
      const records = await api.listCases();
      if (!Array.isArray(records) || !records.every((record) => record && typeof record.id === 'string'
          && typeof record.subject === 'string')) throw new Error('Invalid case list');
      if (mounted.current) setCases(records);
    } catch {
      if (mounted.current) setCaseError('Cases could not be loaded. Check your access and the local API, then refresh.');
    } finally {
      caseRequest.current = false;
      if (mounted.current) setLoadingCases(false);
    }
  }

  async function startRun(event) {
    event.preventDefault();
    if (!api || !identity || identity.provider === 'unavailable' || submitted.current || caseRequest.current
        || !cases?.some((record) => record.id === caseId) || !message.trim() || message.length > 8000
        || (refundIntent && !refundSku.trim())) return;
    const body = { instance_id: identity.instance_id, request_id: createRequestId(),
      case_id: caseId, message: message.trim() };
    if (refundIntent) {
      body.resolution_intent = 'refund';
      body.refund_sku = refundSku.trim();
    }
    submitted.current = true;
    setAttempt({ state: 'sending', body });
    try {
      const snapshot = await api.startRun(body);
      if (mounted.current) setAttempt({ state: 'accepted', body, snapshot });
    } catch (error) {
      if (!mounted.current) return;
      const knownUnavailable = error.status === 503 && Object.hasOwn(rejectedMessages, error.code);
      const rejected = [401, 403, 404, 422].includes(error.status) || knownUnavailable;
      submitted.current = !rejected;
      setAttempt({ state: rejected ? 'rejected' : 'uncertain', body,
        error: knownUnavailable ? rejectedMessages[error.code]
          : rejected ? 'The API rejected this request. Check case access and message inputs before submitting again.'
            : error.code === 'INSTANCE_CHANGED_DO_NOT_REPLAY'
              ? 'The server instance changed. Do not replay this submission; confirm your identity again and inspect existing records first.'
              : 'The submission outcome is uncertain. A run may have started. Do not resubmit; inspect backend records using the request ID below.' });
    }
  }

  if (!api || !identity) {
    return (
      <section className="view-placeholder" aria-labelledby="ai-signin-title">
        <p className="eyebrow">AI Workspace</p>
        <h2 id="ai-signin-title">Confirm your identity to start a run.</h2>
        <p>Use the existing token form above. Case access and run permissions are checked by the backend.</p>
      </section>
    );
  }

  const snapshot        = tracking?.snapshot ?? attempt?.snapshot;
  const runState        = snapshot ? runStates[snapshot.status] : null;
  const submitting      = attempt?.state === 'sending';
  const toolEvents      = tracking?.events.filter((event) => event.kind === 'TOOL') ?? [];
  const allEvents       = tracking?.events ?? [];
  const review          = snapshot?.review;
  const reviewRequestStatus = reviewRequestStatusForDisplay(review, snapshot?.reviewed_status ?? null);
  const proposalRefs    = [...new Set(toolEvents.map((e) => e.proposal_id).filter(Boolean))]
    .filter((id) => id !== review?.proposal_id);
  const stages          = pipelineStages(attempt, tracking);
  const isLive          = snapshot && activeRunStatuses.has(snapshot.status);
  const trackingPhase   = tracking?.phase;

  const caseHelp = loadingCases ? 'Loading cases available to your identity\u2026'
    : caseError ? 'Cases are unavailable. Refresh cases to try again.'
      : cases === null ? 'Load your available cases to begin.'
        : cases.length === 0 ? 'No cases are available for your identity. You can refresh to check again.'
          : `${cases.length} ${cases.length === 1 ? 'case is' : 'cases are'} available.`;

  const submitHelp = submitting
    ? 'Submission in progress. Please wait.'
    : attempt?.state === 'uncertain'
      ? 'Submission locked — a run may have started. Use the request ID to check records before acting.'
      : attempt?.state === 'accepted'
        ? 'This request has been submitted. Follow its status in the run panel.'
        : identity.provider === 'unavailable' ? 'The provider is unavailable.'
          : loadingCases ? 'Wait for cases to finish loading.'
            : !caseId ? 'Select an available case to enable submission.'
              : !message.trim() ? 'Enter a request message to enable submission.'
                : refundIntent && !refundSku.trim() ? 'Enter the exact SKU for the requested refund.'
                : 'Ready to submit.';

  const statusTone = attempt?.error || tracking?.error
    ? 'error'
    : runState?.tone ?? (submitting ? 'active' : 'idle');

  const statusHeading = snapshot
    ? runState.label
    : submitting ? 'Submitting\u2026'
      : attempt?.state === 'uncertain' ? 'Submission uncertain'
        : attempt?.state === 'rejected' ? 'Request not started'
          : 'No active run';

  const statusDetail = snapshot
    ? `${runState.detail} ${
        trackingPhase === 'error' ? 'Live tracking stopped \u2014 last confirmed status shown.'
        : trackingPhase === 'stopped' ? 'Automatic tracking has stopped.'
          : retryingTracking ? 'Reconnecting\u2026'
            : isLive ? 'Checking for updates\u2026'
              : 'Loading remaining events\u2026'}`
    : submitting ? 'Waiting for the API to confirm. A run has not yet been confirmed.'
      : attempt?.state === 'uncertain' ? 'A run may have started. Locked to prevent duplicate submission.'
        : attempt?.state === 'rejected' ? 'Review the error and your inputs before trying again.'
          : 'Load cases, select one, and enter a request message.';

  return (
    <div className="ai-workspace">

      {/* ── Left panel: request form ─────────────────────────────── */}
      <form className="case-panel ai-request-panel" aria-labelledby="run-request-title" onSubmit={startRun}>
        <div className="case-panel-heading">
          <div>
            <p className="eyebrow">Case-bound request</p>
            <h2 id="run-request-title">Start a resolution run</h2>
          </div>
          <span className={`ai-provider-badge ${identity.provider === 'unavailable' ? 'ai-provider-badge--unavailable' : ''}`}>
            {identity.provider === 'bedrock' ? 'Amazon Bedrock'
              : identity.provider === 'local_scripted' ? 'Local scripted' : 'Provider unavailable'}
          </span>
        </div>

        <p className="case-caption ai-workspace-note">
          Starts a real workflow for the selected case. A proposal may be generated for human review.
          <strong> No business action is executed by this workspace.</strong>
        </p>

        {identity.provider === 'unavailable' && (
          <p className="run-notice" role="status">
            The run provider is unavailable. Confirm your identity after the service becomes available.
          </p>
        )}

        <div className="ai-case-controls">
          <button
            type="button"
            className="button-secondary"
            disabled={loadingCases || locked}
            aria-busy={loadingCases}
            aria-describedby={locked ? 'run-submit-help' : 'run-case-help'}
            onClick={loadCases}
          >
            {loadingCases ? 'Loading\u2026' : cases === null && !caseError ? 'Load cases' : 'Refresh cases'}
          </button>
          <p id="run-case-help" className="case-caption" role="status" aria-atomic="true">{caseHelp}</p>
        </div>

        {caseError && <p className="run-error" role="alert">{caseError}</p>}

        <fieldset disabled={loadingCases || locked} aria-busy={loadingCases || submitting}>
          <legend className="sr-only">Resolution run request</legend>
          <label>
            Support case
            <select
              required
              value={caseId}
              disabled={!cases?.length}
              aria-describedby="run-case-help"
              onChange={(event) => setCaseId(event.target.value)}
            >
              <option value="">
                {loadingCases ? 'Loading\u2026'
                  : caseError ? 'Cases unavailable'
                    : cases === null ? 'Load cases to begin'
                      : cases.length === 0 ? 'No cases available'
                        : 'Choose a case\u2026'}
              </option>
              {cases?.map((record) => (
                <option key={record.id} value={record.id}>{record.subject} \u2014 {record.id}</option>
              ))}
            </select>
          </label>

          <label>
            Request message
            <textarea
              required
              rows={6}
              maxLength={8000}
              value={message}
              aria-describedby="run-message-help"
              onChange={(event) => setMessage(event.target.value)}
            />
          </label>
          <p id="run-message-help" className="case-caption">
            Describe the support request. Maximum 8,000 characters.
          </p>

          <label>
            <input
              type="checkbox"
              checked={refundIntent}
              onChange={(event) => setRefundIntent(event.target.checked)}
            />
            Request a refund for one item
          </label>
          {refundIntent && <label>
            Exact item SKU
            <input
              required
              maxLength={120}
              value={refundSku}
              aria-describedby="run-refund-help"
              onChange={(event) => setRefundSku(event.target.value)}
            />
          </label>}
          {refundIntent && <p id="run-refund-help" className="case-caption">
            Enter the exact SKU from the selected case's order. The server verifies the selection and trusted evidence.
          </p>}

          <div className="ai-submit-row">
            <button
              type="submit"
              disabled={loadingCases || locked || identity.provider === 'unavailable' || !caseId || !message.trim()
                || (refundIntent && !refundSku.trim())}
              aria-busy={submitting}
              aria-describedby="run-submit-help"
            >
              {submitting ? 'Submitting\u2026'
                : attempt?.state === 'accepted' ? 'Submitted'
                  : attempt?.state === 'uncertain' ? 'Locked'
                    : 'Start run'}
            </button>
            <p id="run-submit-help" className="case-caption">{submitHelp}</p>
          </div>
        </fieldset>

        <p className="run-footnote">
          Leaving this view does not stop a submitted run. If the response is lost, do not resubmit for the same request.
        </p>

        {/* Request identifiers — shown once submitted */}
        {attempt && (
          <details className="ai-request-ids">
            <summary>Request identifiers</summary>
            <dl className="case-facts">
              <div><dt>Request ID</dt><dd className="case-mono">{attempt.body.request_id}</dd></div>
              <div><dt>Case ID</dt><dd className="case-mono">{attempt.body.case_id}</dd></div>
              <div><dt>Server instance</dt><dd className="case-mono">{attempt.body.instance_id}</dd></div>
            </dl>
          </details>
        )}
      </form>

      {/* ── Right panel: workflow console ────────────────────────── */}
      <section className="case-panel ai-run-panel" aria-labelledby="run-console-title">
        <div className="case-panel-heading">
          <div>
            <p className="eyebrow">Agent workflow console</p>
            <h2 id="run-console-title">Run status &amp; events</h2>
          </div>
          {/* Live indicator dot */}
          {isLive && <span className="ai-live-indicator" aria-label="Live tracking active">
            <span aria-hidden="true" />LIVE
          </span>}
          {snapshot && !isLive && (
            <span className={`case-status case-status--${runState.tone === 'complete' ? 'resolved' : runState.tone === 'error' ? 'error' : runState.tone === 'waiting' ? 'active' : 'open'}`}>
              {runState.label}
            </span>
          )}
        </div>

        {/* Workflow pipeline strip */}
        {(attempt || snapshot) && (
          <nav className="ai-pipeline" aria-label="Workflow stage progress">
            {stages.map((s, i) => (
              <div key={s.idx} className={`ai-pipeline-stage ai-pipeline-stage--${s.state}`} aria-current={s.state === 'active' || s.state === 'reached' ? 'step' : undefined}>
                <span className="ai-pipeline-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round">
                    <path d={s.icon} />
                  </svg>
                </span>
                <span className="ai-pipeline-label">{s.label}</span>
                {i < stages.length - 1 && <span className="ai-pipeline-connector" aria-hidden="true" />}
              </div>
            ))}
          </nav>
        )}

        {/* Status summary card */}
        <div
          className="run-state-summary"
          role="status"
          aria-atomic="true"
          data-tone={statusTone}
        >
          <div className="run-state-summary-head">
            <h3>{statusHeading}</h3>
            {isLive && <span className="run-state-pulse" aria-hidden="true" />}
          </div>
          <p>{statusDetail}</p>
        </div>

        {/* Submission/tracking errors */}
        {attempt?.error && (
          <div className="run-error-banner" role="alert">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
              strokeLinejoin="round" strokeLinecap="round" aria-hidden="true">
              <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
            </svg>
            <span>{attempt.error}</span>
          </div>
        )}

        {snapshot && (
          <>
            {tracking?.error && (
              <div className="run-error-banner" role="alert">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
                  strokeLinejoin="round" strokeLinecap="round" aria-hidden="true">
                  <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
                </svg>
                <span>{tracking.error}</span>
              </div>
            )}

            {(tracking?.canRetry || retryingTracking) && (
              <button
                type="button"
                className="button-secondary"
                disabled={retryingTracking}
                aria-busy={retryingTracking}
                aria-describedby="run-tracking-help"
                onClick={retryTracking}
              >
                {retryingTracking ? 'Reconnecting\u2026' : 'Retry live tracking'}
              </button>
            )}

            {/* Run identifiers — collapsible */}
            <details className="ai-run-ids">
              <summary>Run identifiers</summary>
              <dl className="case-facts">
                <div><dt>Run ID</dt><dd className="case-mono">{snapshot.run_id}</dd></div>
                {snapshot.workflow_id && <div><dt>Workflow ID</dt><dd className="case-mono">{snapshot.workflow_id}</dd></div>}
                {snapshot.conversation_id && <div><dt>Conversation ID</dt><dd className="case-mono">{snapshot.conversation_id}</dd></div>}
                {snapshot.trace_id && <div><dt>Trace ID</dt><dd className="case-mono">{snapshot.trace_id}</dd></div>}
                <div><dt>Provider</dt><dd>{snapshot.provider === 'bedrock'
                  ? 'Amazon Bedrock' : 'Local scripted'}</dd></div>
                <div><dt>Created</dt><dd><time dateTime={snapshot.created_at}>{snapshot.created_at}</time></dd></div>
                <div><dt>Updated</dt><dd><time dateTime={snapshot.updated_at}>{snapshot.updated_at}</time></dd></div>
              </dl>
            </details>

            {/* ── Response section ───────────────────────────────── */}
            <section className="run-event-section ai-response-section" aria-labelledby="ai-response-title">
              <div className="ai-section-heading">
                <h3 id="ai-response-title">Response</h3>
                <span className="ai-section-note">Backend-confirmed customer-facing message</span>
              </div>
              <div className="resolution-card ai-response-card">
                {snapshot.message ? (
                  <p className="run-message">{snapshot.message}</p>
                ) : (
                  <p className="case-caption">
                    {isLive ? 'Waiting for a response\u2026' : 'No customer-facing response was returned.'}
                  </p>
                )}
              </div>
            </section>

            {/* ── Proposal section ───────────────────────────────── */}
            <section className="run-event-section ai-proposal-section" aria-labelledby="ai-proposal-title">
              <div className="ai-section-heading">
                <h3 id="ai-proposal-title">Proposed action</h3>
                <span className="ai-section-note">Agent-generated proposal pending human review</span>
              </div>
              {review ? (
                <div className="resolution-card ai-proposal-card">
                  <p className="ai-proposal-action-label">Action</p>
                  <p className="proposal-action">{review.action}</p>
                  <div className="ai-proposal-meta">
                    <span className="case-caption">Proposal ID</span>
                    <code className="case-mono">{review.proposal_id}</code>
                  </div>
                  <div className="ai-proposal-rationale">
                    <p className="ai-proposal-rationale-label">Rationale</p>
                    <p className="run-message ai-rationale-text">{review.rationale}</p>
                  </div>
                </div>
              ) : (
                <div className="resolution-card">
                  <p className="case-caption">
                    {isLive ? 'No proposal has been generated yet.' : 'No proposed action was returned.'}
                  </p>
                  {proposalRefs.length > 0 && (
                    <>
                      <p className="case-caption">
                        Proposal references from tool events (action and review details unavailable):
                      </p>
                      <ul className="proposal-references">
                        {proposalRefs.map((id) => <li key={id}><code className="case-mono">{id}</code></li>)}
                      </ul>
                    </>
                  )}
                </div>
              )}
              {(tracking?.gap || tracking?.trimmed) && (
                <p className="case-caption">Event history is incomplete; earlier proposal references may be missing.</p>
              )}
            </section>

            {/* ── Human review section ───────────────────────────── */}
            <section className="run-event-section ai-review-section" aria-labelledby="ai-review-title">
              <div className="ai-section-heading">
                <h3 id="ai-review-title">Human review</h3>
                {snapshot.status === 'REVIEW_REQUIRED' && (
                  <span className="ai-review-badge ai-review-badge--required">Review required</span>
                )}
                {snapshot.status === 'REVIEWED' && (
                  <span className="ai-review-badge ai-review-badge--done">Review recorded</span>
                )}
              </div>

              <div className="resolution-card ai-review-card">
                {snapshot.status === 'REVIEW_REQUIRED' && (
                  <div className="run-notice ai-hitl-notice">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                      strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
                      aria-hidden="true">
                      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                    </svg>
                    The backend reports human review is required. Review is recorded in the Reviews workspace.
                  </div>
                )}

                {review || snapshot.reviewed_status ? (
                  <dl className="case-facts">
                    {review && (
                      <>
                        {reviewRequestStatus && (
                          <div><dt>Review request status</dt><dd>{reviewRequestStatus}</dd></div>
                        )}
                        <div><dt>Review ID</dt><dd className="case-mono">{review.review_id}</dd></div>
                      </>
                    )}
                    <div>
                      <dt>Recorded decision</dt>
                      <dd>
                        <span className={`ai-decision-badge ${snapshot.reviewed_status === 'APPROVED' ? 'ai-decision-badge--approved' : snapshot.reviewed_status === 'REJECTED' ? 'ai-decision-badge--rejected' : ''}`}>
                          {snapshot.reviewed_status ?? 'Not yet recorded'}
                        </span>
                      </dd>
                    </div>
                  </dl>
                ) : (
                  <p className="case-caption">No review request or decision has been reported yet.</p>
                )}

                <div className="ai-boundary-notice">
                  A proposed action or an approval does not mean a business action was executed.
                </div>
              </div>
            </section>

            {/* ── Action & execution state ───────────────────────── */}
            <section className="run-event-section ai-action-section" aria-labelledby="ai-action-title">
              <div className="ai-section-heading">
                <h3 id="ai-action-title">Action &amp; execution state</h3>
              </div>
              <div className={`resolution-card ai-action-card${snapshot.status === 'FAILED' ? ' ai-action-card--failed' : ''}`}>
                <div className="ai-action-status">
                  <span className={`ai-action-icon ${snapshot.actions_executed === false ? 'ai-action-icon--safe' : ''}`} aria-hidden="true">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                      strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round">
                      {snapshot.actions_executed === false
                        ? <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10zM9 12l2 2 4-4" />
                        : <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />}
                    </svg>
                  </span>
                  <div>
                    {snapshot.actions_executed === false
                      ? <><strong>No business action was executed</strong><p className="case-caption">Backend-confirmed. Eligibility assessment and human review do not execute actions.</p></>
                      : <p className="case-caption">Execution state has not been confirmed by this snapshot.</p>}
                  </div>
                </div>

                {snapshot.status === 'FAILED' && (
                  <p className="run-error">
                    The workflow has failed.
                  </p>
                )}
                {snapshot.error ? (
                  <p className="run-error">
                    The backend reported an error for this run. Use the run ID when seeking support.
                  </p>
                ) : snapshot.status === 'FAILED' ? null : (
                  <p className="case-caption">No backend error is reported in this snapshot.</p>
                )}
              </div>
            </section>

            {/* ── Knowledge evidence ─────────────────────────────── */}
            <KnowledgeEvidence tracking={tracking} />

            {/* ── Tool activity timeline ─────────────────────────── */}
            <section className="run-event-section tool-activity" aria-labelledby="tool-activity-title">
              <div className="tool-activity-heading">
                <div className="ai-section-heading">
                  <h3 id="tool-activity-title">Tool activity</h3>
                  <span className="ai-section-note">
                    {toolEvents.length > 0 ? `${toolEvents.length} event${toolEvents.length === 1 ? '' : 's'}` : 'None recorded'}
                  </span>
                </div>
                {toolEvents.length > 0 && (
                  <span className="badge">{toolEvents.filter((e) => e.ok === true).length} succeeded
                    {toolEvents.some((e) => e.ok === false) && ` · ${toolEvents.filter((e) => e.ok === false).length} failed`}
                  </span>
                )}
              </div>

              {(tracking?.gap || tracking?.trimmed) && (
                <p className="case-caption">Event history is incomplete; earlier tool activity may be missing.</p>
              )}

              {toolEvents.length === 0 ? (
                <p className="case-caption">
                  {isLive ? 'Waiting for tool activity\u2026' : 'No tool activity was received for this run.'}
                </p>
              ) : (
                <ol className="tool-activity-list" aria-label="Tool activity in sequence order" tabIndex={0}>
                  {toolEvents.map((event) => {
                    const outcome = event.ok === true ? 'success' : event.ok === false ? 'failure' : 'unknown';
                    return (
                      <li key={event.sequence} className={`tool-activity-item ${outcome}`}>
                        <div className="tool-activity-card">
                          <div className="tool-activity-row">
                            <strong className="tool-activity-name">{event.tool || 'Tool'}</strong>
                            <span className="tool-activity-outcome">
                              {event.ok === true ? 'Succeeded' : event.ok === false ? 'Failed' : 'Unknown'}
                            </span>
                          </div>
                          <div className="tool-activity-meta">
                            <span>#{event.sequence}</span>
                            <span>{event.state}</span>
                            <time dateTime={event.timestamp}>{fmtTime(event.timestamp)}</time>
                          </div>
                        </div>
                      </li>
                    );
                  })}
                </ol>
              )}
            </section>

            {/* ── Workflow event log ─────────────────────────────── */}
            <section className="run-event-section" aria-labelledby="run-events-title">
              <div className="ai-section-heading">
                <h3 id="run-events-title">Workflow event log</h3>
                <span className="ai-section-note">
                  {allEvents.length > 0 ? `${allEvents.length} events` : 'No events'}
                  {tracking?.trimmed ? ` (showing latest ${maxDisplayedEvents})` : ''}
                </span>
              </div>

              {tracking?.gap && (
                <p className="run-notice">
                  Some events are no longer retained by the backend. This history has a gap.
                </p>
              )}

              {allEvents.length === 0 ? (
                <p className="case-caption">
                  {trackingPhase === 'error' ? 'No events received. Live tracking is stopped.'
                    : trackingPhase === 'stopped' ? 'No events were returned for this run.'
                      : 'Waiting for workflow events\u2026'}
                </p>
              ) : (
                <ol className="run-events" aria-label="Workflow events in sequence order" tabIndex={0}>
                  {allEvents.map((event) => (
                    <li key={event.sequence} className={`run-event-item${event.error ? ' run-event-item--error' : event.kind === 'STATE' ? ' run-event-item--state' : ''}`}>
                      <div className="run-event-heading">
                        <strong className="run-event-label">
                          #{event.sequence}{' '}
                          {event.kind === 'STATE' ? 'Status'
                            : event.kind === 'NODE' ? (event.node?.replaceAll('_', ' ') ?? 'Node')
                              : (event.tool ?? 'Tool')}
                        </strong>
                        <div className="run-event-meta">
                          <span className={`run-event-state-badge run-event-state-badge--${(event.state || '').toLowerCase()}`}>
                            {event.state}
                          </span>
                          <time dateTime={event.timestamp}>{fmtTime(event.timestamp)}</time>
                        </div>
                      </div>
                      {event.ok !== null && (
                        <p className={`run-event-result ${event.ok ? 'run-event-result--ok' : 'run-event-result--fail'}`}>
                          {event.ok ? 'Tool succeeded' : 'Tool failed'}
                        </p>
                      )}
                      {event.error && (
                        <p className="run-error run-event-error">
                          An error was reported for this event. Use the run ID and event sequence #{event.sequence} when seeking support.
                        </p>
                      )}
                    </li>
                  ))}
                </ol>
              )}
            </section>

            <p id="run-tracking-help" className="run-footnote">
              Only backend-reported status and events are shown here.
              Retrying tracking reads this run without resubmitting.
              Approval does not execute a business action.
            </p>
          </>
        )}
      </section>
    </div>
  );
}
