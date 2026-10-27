import { useCallback, useEffect, useRef, useState } from 'react';

import ReviewRunResult from './ReviewRunResult.jsx';

const maxReviewEvents = 64;
const terminalStatuses = new Set(['REVIEWED', 'FAILED', 'COMPLETED']);

function validateReviewSnapshot(snapshot, item, instanceId, previous) {
  const bound = snapshot.instance_id === instanceId && snapshot.run_id === item.runId
    && snapshot.case_id === item.caseId && snapshot.workflow_id === item.workflowId
    && snapshot.review?.workflow_id === item.workflowId && snapshot.review?.case_id === item.caseId
    && snapshot.review?.proposal_id === item.proposalId && snapshot.review?.review_id === item.reviewId;
  const stale = previous && (snapshot.last_sequence < previous.last_sequence
    || Date.parse(snapshot.updated_at) < Date.parse(previous.updated_at)
    || (terminalStatuses.has(previous.status) && snapshot.status !== previous.status)
    || (previous.reviewed_status !== null && snapshot.reviewed_status !== previous.reviewed_status));
  if (!bound || stale) {
    const error = new Error('Review tracking could not be verified');
    error.code = bound ? 'STALE_REVIEW_SNAPSHOT' : 'REVIEW_BINDING_MISMATCH';
    throw error;
  }
}
function sameReview(first, second) {
  return second && ['runId', 'caseId', 'workflowId', 'proposalId', 'reviewId', 'action', 'rationale']
    .every((key) => first[key] === second[key]);
}
function decisionResult(snapshot, decision) {
  if (snapshot.status === 'FAILED') return { phase: 'failed',
    message: 'The resumed workflow failed. Any backend-confirmed human decision is shown separately below. Review the existing records; do not replay the decision.' };
  if (snapshot.status === 'RESUMING') return { phase: 'accepted',
    message: 'The last confirmed run status is RESUMING. A final reviewed result is not yet confirmed.' };
  if (snapshot.reviewed_status) {
    const expected = decision === 'APPROVE' ? 'APPROVED' : 'REJECTED';
    return { phase: snapshot.status === 'REVIEWED' && snapshot.reviewed_status === expected ? 'success' : 'stale',
      message: `The backend reports this proposal as ${snapshot.reviewed_status === 'APPROVED' ? 'approved' : 'rejected'}.`
        + (snapshot.reviewed_status !== expected ? ' This differs from your requested decision; do not resubmit.' : '')
        + (snapshot.status !== 'REVIEWED' ? ' The run has not confirmed the expected REVIEWED status.' : ' The workflow has finished human review.') };
  }
  if (snapshot.status === 'REVIEW_REQUIRED') return { phase: 'stale',
    message: 'The workflow still reports review required, but this attempt has no confirmed decision. It may have conflicted with another review. Inspect existing records; do not resubmit automatically.' };
  return { phase: 'uncertain',
    message: 'The workflow stopped without a confirmed review outcome. Inspect existing records before taking further action; do not resubmit this decision.' };
}

export default function ReviewQueue({ api, identity, active }) {
  const allowed = Boolean(api && identity && ['REVIEWER', 'ADMIN'].includes(identity.role));
  const [state, setState] = useState({ phase: 'loading', items: [], snapshotAt: null });
  const request = useRef(null);
  const decisionRequest = useRef(null);
  const attempts = useRef({});
  const [decisions, setDecisions] = useState({});
  const [selected, setSelected] = useState(null);
  const [busy, setBusy] = useState(false);
  const [accessDenied, setAccessDenied] = useState(false);
  const focusTarget = useRef(null);
  const instanceId = identity?.instance_id;

  useEffect(() => {
    if (active && focusTarget.current) {
      document.getElementById(focusTarget.current)?.focus();
      focusTarget.current = null;
    }
  }, [active, selected, decisions]);

  function selectDecision(item, decision) {
    focusTarget.current = `confirm-review-${item.runId}`;
    setSelected({ item, decision });
  }

  const loadQueue = useCallback(async () => {
    if (!allowed || accessDenied || request.current || decisionRequest.current) return;
    const controller = new AbortController();
    request.current = controller;
    setSelected(null);
    setState({ phase: 'loading', items: [], snapshotAt: null });
    try {
      const page = await api.pendingReviews(instanceId, { signal: controller.signal });
      if (!controller.signal.aborted) setState({ phase: 'ready', ...page });
    } catch (error) {
      if (controller.signal.aborted) return;
      const denied = [401, 403].includes(error.status);
      setState({ phase: denied ? 'denied' : 'error', items: [], snapshotAt: null,
        error: denied ? 'Review access could not be verified. Confirm an authorized reviewer or administrator identity.'
          : [404, 503].includes(error.status) ? 'The review queue is unavailable. Try refreshing when the service is available.'
            : 'The review queue could not be verified. Refresh to try again. If the server restarted, confirm your identity again.' });
    } finally {
      if (request.current === controller) request.current = null;
    }
  }, [api, allowed, accessDenied, instanceId]);

  useEffect(() => {
    if (active) loadQueue();
    else setSelected(null);
  }, [active, loadQueue]);

  useEffect(() => {
    return () => {
      request.current?.abort();
      request.current = null;
      decisionRequest.current?.abort();
      decisionRequest.current = null;
    };
  }, []);

  function publishDecision(item, decision, result) {
    const record = { ...attempts.current[item.runId], item, decision, ...result };
    attempts.current = { ...attempts.current, [item.runId]: record };
    setDecisions(attempts.current);
  }

  function locked(item) {
    const attempt = attempts.current[item.runId];
    return attempt && attempt.phase !== 'rejected';
  }

  async function submitDecision() {
    if (!allowed || accessDenied || !selected || request.current || decisionRequest.current
        || state.phase !== 'ready' || locked(selected.item)) return;
    const { item, decision } = selected;
    const controller = new AbortController();
    // Synchronous lock covers double clicks before disabled controls render.
    decisionRequest.current = controller;
    setBusy(true);
    setSelected(null);
    focusTarget.current = `decision-status-${item.runId}`;
    publishDecision(item, decision, { phase: 'checking', message: 'Checking that the selected proposal is still pending…' });
    let posted = false;
    let refresh = false;
    try {
      const page = await api.pendingReviews(instanceId, { signal: controller.signal });
      if (controller.signal.aborted) return;
      const current = page.items.find((candidate) => candidate.runId === item.runId);
      if (!sameReview(item, current)) {
        publishDecision(item, decision, { phase: 'stale',
          message: 'This proposal is no longer pending or its review details changed. No decision was sent. Refresh the queue to inspect current records.' });
        refresh = true;
        return;
      }
      posted = true;
      publishDecision(item, decision, { phase: 'submitting',
        message: `Submitting ${decision === 'APPROVE' ? 'approval' : 'rejection'}… Do not submit another decision.` });
      const snapshot = await api.decideReview(instanceId, item, decision, { signal: controller.signal });
      if (controller.signal.aborted) return;
      validateReviewSnapshot(snapshot, item, instanceId);
      publishDecision(item, decision, { ...decisionResult(snapshot, decision), needsHistory: true,
        tracking: { snapshot, events: [], after: 0, gap: false, trimmed: false } });
      setState((previous) => ({ ...previous, items: previous.items.filter((entry) => entry.runId !== item.runId) }));
      refresh = true;
    } catch (error) {
      if (controller.signal.aborted) return;
      if ([401, 403].includes(error.status)) {
        setAccessDenied(true);
        setState({ phase: 'denied', items: [], snapshotAt: null });
        publishDecision(item, decision, { phase: 'unauthorized',
          message: 'Review access was denied. No successful decision is confirmed. Confirm an authorized reviewer or administrator identity.' });
      } else if ([404, 409].includes(error.status)) {
        publishDecision(item, decision, { phase: 'stale',
          message: error.code === 'INSTANCE_CHANGED_DO_NOT_REPLAY'
            ? 'The server instance changed. Confirm your identity and inspect current records; do not replay this decision.'
            : 'The review is unavailable, has changed, or was already handled by another request. Refresh the queue and inspect existing records; do not resubmit this decision.' });
        refresh = true;
      } else if (!posted || error.status === 422 || (error.status === 503 && error.code === 'RUN_CAPACITY_UNAVAILABLE')) {
        publishDecision(item, decision, { phase: 'rejected',
          message: !posted ? 'The pending review could not be verified. No decision was sent. Refresh the queue before trying again.'
            : 'The backend did not accept this decision request. Refresh the queue before making another explicit decision.' });
        refresh = true;
      } else {
        publishDecision(item, decision, { phase: 'uncertain', needsHistory: true,
          message: 'The decision response could not be verified. It may have been accepted. This proposal is locked against resubmission; check its status using a read-only request.' });
      }
    } finally {
      if (decisionRequest.current === controller) {
        decisionRequest.current = null;
        setBusy(false);
        if (refresh) await loadQueue();
      }
    }
  }

  async function checkDecision(record, automatic = false) {
    if (!allowed || accessDenied || decisionRequest.current || request.current) return;
    const { item, decision } = record;
    const controller = new AbortController();
    decisionRequest.current = controller;
    setBusy(true);
    let refresh = false;
    let current = record.tracking ?? { snapshot: null, events: [], after: 0, gap: false, trimmed: false };
    // Background polling should announce outcome changes, not each loading cycle.
    publishDecision(item, decision, { phase: automatic ? record.phase : 'checking', checkingRun: true,
      checks: (record.checks ?? 0) + 1,
      message: automatic ? record.message : 'Loading the same run’s status and workflow events using read-only requests…' });
    try {
      const binding = { instance_id: instanceId, run_id: item.runId, case_id: item.caseId };
      const snapshot = await api.runSnapshot(binding, { signal: controller.signal });
      if (controller.signal.aborted) return;
      validateReviewSnapshot(snapshot, item, instanceId, current.snapshot);
      current = { ...current, snapshot };
      publishDecision(item, decision, { tracking: current, ...(automatic ? decisionResult(snapshot, decision) : {}) });
      let page;
      do {
        page = await api.runEvents(binding, current.after, { signal: controller.signal });
        if (controller.signal.aborted) return;
        validateReviewSnapshot(page.snapshot, item, instanceId, current.snapshot);
        // The existing API validates run binding and contiguous event sequences.
        // Retain STATE events only, without tool payloads or diagnostic errors.
        const events = [...current.events, ...page.events.filter((event) => event.kind === 'STATE')
          .map((event) => ({ sequence: event.sequence, state: event.state, timestamp: event.timestamp }))];
        current = { snapshot: page.snapshot, after: page.next_after, events: events.slice(-maxReviewEvents),
          gap: current.gap || page.gap, trimmed: current.trimmed || events.length > maxReviewEvents };
        publishDecision(item, decision, { tracking: current, ...(automatic ? decisionResult(page.snapshot, decision) : {}) });
      } while (page.has_more);
      // Drain available events even when the snapshot is already terminal.
      const result = decisionResult(current.snapshot, decision);
      publishDecision(item, decision, { ...result, tracking: current, needsHistory: false, canCheck: true });
      refresh = result.phase !== 'accepted';
    } catch (error) {
      if (controller.signal.aborted) return;
      const denied = [401, 403].includes(error.status);
      if (denied) {
        setAccessDenied(true);
        setState({ phase: 'denied', items: [], snapshotAt: null });
      }
      const stale = error.status === 409 || ['STALE_REVIEW_SNAPSHOT', 'REVIEW_BINDING_MISMATCH'].includes(error.code);
      publishDecision(item, decision, { phase: denied ? 'unauthorized' : stale ? 'stale' : 'unavailable',
        needsHistory: false, canCheck: !denied && error.status !== 404 && !stale,
        message: denied ? 'Access to the review was denied. Confirm an authorized identity before checking existing records.'
          : stale ? 'Tracking returned stale or mismatched review data. Updates have stopped and the last verified data remains shown. Inspect existing records; do not resubmit.'
            : error.status === 404 ? 'This run is no longer available from the server. Its last verified data remains shown. Do not replay the decision.'
              : 'Run tracking is unavailable. The last verified data remains shown. Retry tracking to read the same run; the decision will not be submitted again.' });
    } finally {
      if (decisionRequest.current === controller) {
        decisionRequest.current = null;
        setBusy(false);
        publishDecision(item, decision, { checkingRun: false });
        if (refresh && !controller.signal.aborted) await loadQueue();
      }
    }
  }

  useEffect(() => {
    if (!active || !allowed || busy || selected || accessDenied || ['loading', 'denied'].includes(state.phase)) return;
    const pending = Object.values(decisions).find((record) => (record.phase === 'accepted' || record.needsHistory)
      && (record.checks ?? 0) < 20);
    if (!pending) return;
    // Bounded, read-only confirmation after HTTP acceptance. Never replay POSTs.
    const timer = setTimeout(() => checkDecision(pending, true), 1500);
    return () => clearTimeout(timer);
  }, [active, allowed, busy, selected, accessDenied, state.phase, decisions]);

  if (!api || !identity) return <section className="view-placeholder" aria-labelledby="reviews-signin-title">
    <p className="eyebrow">Human review queue</p>
    <h2 id="reviews-signin-title">Confirm your reviewer identity.</h2>
    <p>Use the token form above with a reviewer or administrator account to view pending workflows.</p>
  </section>;

  if (!allowed) return <section className="view-placeholder" aria-labelledby="reviews-access-title">
    <p className="eyebrow">Restricted workspace</p>
    <h2 id="reviews-access-title">Reviewer access required.</h2>
    <p>The human review queue is available to reviewers and administrators. Customer accounts cannot access this queue.</p>
  </section>;

  const loading = state.phase === 'loading';
  return <section className="case-panel review-queue" aria-labelledby="review-queue-title">
    <div className="case-panel-heading">
      <div><p className="eyebrow">Human review</p><h2 id="review-queue-title">Pending review queue</h2></div>
      <button type="button" className="button-secondary" onClick={loadQueue}
        disabled={loading || busy || Boolean(selected) || accessDenied || state.phase === 'denied'} aria-busy={loading} aria-describedby="review-queue-status">
        {loading ? 'Loading reviews…' : 'Refresh reviews'}
      </button>
    </div>
    <p className="run-notice" id="review-decision-boundary">Approve or reject a proposal only after assessing its details.
      Approval and rejection record a human review decision. Neither executes the proposed business action.</p>
    {Object.keys(decisions).length > 0 && <section className="review-decisions" aria-labelledby="review-decisions-title">
      <h3 id="review-decisions-title">Decision &amp; final run result</h3>
      {Object.values(decisions).map((record) => <div key={record.item.runId} className="resolution-card">
        <p className="case-caption">{record.decision === 'APPROVE' ? 'Approval' : 'Rejection'} request for proposal <code>{record.item.proposalId}</code></p>
        <p className={['stale', 'uncertain', 'unauthorized', 'rejected', 'failed', 'unavailable'].includes(record.phase) ? 'run-error' : 'run-notice'}
          id={`decision-status-${record.item.runId}`} tabIndex={-1}
          role="status" aria-atomic="true">{record.message} No business action is executed by review.</p>
        <ReviewRunResult record={record} active={active} />
        {record.canCheck !== false && (record.tracking || ['accepted', 'uncertain', 'stale', 'unavailable'].includes(record.phase))
          && <button type="button" className="button-secondary"
          disabled={busy || loading || Boolean(selected) || accessDenied || state.phase === 'denied'} aria-busy={Boolean(record.checkingRun)}
          aria-describedby={`decision-status-${record.item.runId}`} onClick={() => checkDecision(record)}>
          {record.checkingRun ? 'Loading run tracking…' : 'Refresh run tracking'}
        </button>}
      </div>)}
    </section>}
    <p id="review-queue-status" className="case-caption" role="status" aria-atomic="true">
      {loading ? <><span className="case-loading-dot" aria-hidden="true" />Checking the backend for workflows awaiting human review\u2026</>
        : accessDenied || state.phase === 'denied' ? 'Review access is unavailable. Confirm an authorized reviewer or administrator identity.'
        : state.phase === 'ready' ? `${state.items.length} ${state.items.length === 1 ? 'workflow' : 'workflows'} awaiting review at the last refresh.`
          : 'Pending reviews could not be loaded. No queue data is shown.'}
    </p>
    {state.error && <p className="run-error" role="alert">{state.error}</p>}
    {state.snapshotAt && <p className="case-caption">Queue checked at (server): <time dateTime={state.snapshotAt}>{state.snapshotAt}</time>.
      {' '}Refresh to check for changes.</p>}
    {state.phase === 'ready' && state.items.length === 0 && <div className="case-empty">
      <span className="case-empty-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
          strokeLinejoin="round" strokeLinecap="round">
          <path d="M9 4H5v17h14V4h-4M9 3h6v4H9zM8 13l3 3 5-6" />
        </svg>
      </span>
      <h3>No workflows awaiting review</h3>
      <p>No review-required workflows with pending proposals were returned by this server. Refresh after a workflow requests review.</p>
    </div>}
    {state.items.length > 0 && <ol className="review-queue-list" aria-label="Workflows awaiting human review">
      {state.items.map((item) => <li key={item.runId} className="resolution-card">
        <div className="case-panel-heading">
          <h3 id={`review-case-${item.runId}`}>{item.caseSubject}</h3><span className="case-status">Waiting for human review</span>
        </div>
        <h4>Proposed action</h4>
        <p className="proposal-action">{item.action}</p>
        <h4>Proposal rationale</h4>
        <p className="run-message">{item.rationale}</p>
        <p className="case-caption">Proposal content is untrusted and requires independent human assessment.</p>
        <dl className="case-facts run-identifiers">
          <div><dt>Case ID</dt><dd>{item.caseId}</dd></div>
          <div><dt>Run ID</dt><dd>{item.runId}</dd></div>
          <div><dt>Workflow ID</dt><dd>{item.workflowId}</dd></div>
          <div><dt>Proposal ID</dt><dd>{item.proposalId}</dd></div>
          <div><dt>Review ID</dt><dd>{item.reviewId}</dd></div>
          <div><dt>Run status at refresh</dt><dd>{item.status}</dd></div>
          <div><dt>Review status at refresh</dt><dd>{item.reviewStatus}</dd></div>
          <div><dt>Run updated at (server)</dt><dd><time dateTime={item.updatedAt}>{item.updatedAt}</time></dd></div>
        </dl>
        {selected?.item.runId === item.runId ? <div className={`review-confirmation review-confirmation--${selected.decision === 'APPROVE' ? 'approve' : 'reject'}`}>
          <p id={`confirm-review-${item.runId}`} tabIndex={-1}>Confirm {selected.decision === 'APPROVE' ? 'approval' : 'rejection'} of the proposed {item.action}
            {' '}for proposal <code>{item.proposalId}</code>. This records your decision without executing the action.</p>
          <div className="review-decision-actions">
            <button type="button" disabled={busy || loading || accessDenied}
              aria-describedby={`confirm-review-${item.runId}`} onClick={submitDecision}>
              {selected.decision === 'APPROVE' ? 'Confirm approval' : 'Confirm rejection'}
            </button>
            <button type="button" className="button-secondary" disabled={busy} onClick={() => {
              focusTarget.current = `${selected.decision}-${item.runId}`;
              setSelected(null);
            }}>Cancel</button>
          </div>
        </div> : <>
          <div className="review-decision-actions">
            <button type="button" disabled={busy || loading || accessDenied || Boolean(selected) || Boolean(locked(item))}
              id={`APPROVE-${item.runId}`} aria-describedby={`review-case-${item.runId} review-decision-boundary`}
              onClick={() => selectDecision(item, 'APPROVE')}>Approve</button>
            <button type="button" className="button-secondary" disabled={busy || loading || accessDenied || Boolean(selected) || Boolean(locked(item))}
              id={`REJECT-${item.runId}`} aria-describedby={`review-case-${item.runId} review-decision-boundary`}
              onClick={() => selectDecision(item, 'REJECT')}>Reject</button>
          </div>
          {locked(item) && <p className="case-caption">Decision controls are locked for this attempt. See Decision &amp; final run result above.</p>}
        </>}
      </li>)}
    </ol>}
    <p className="run-footnote">This queue covers runs retained in the current server process. Server restarts clear these runs.
      Leaving this view does not cancel a submitted decision. If its outcome is uncertain, check existing records before taking further action.
      Reloading or signing out clears local decision tracking.</p>
  </section>;
}
