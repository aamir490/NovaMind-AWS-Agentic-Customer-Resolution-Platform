const terminalStatuses = new Set(['REVIEWED', 'FAILED', 'COMPLETED']);

export default function ReviewRunResult({ record, active }) {
  const { item, decision, tracking, phase } = record;
  const snapshot = tracking?.snapshot;
  const terminal = snapshot && terminalStatuses.has(snapshot.status);
  const expectedDecision = decision === 'APPROVE' ? 'APPROVED' : 'REJECTED';
  return <div className="review-run-result">
    <dl className="case-facts">
      <div><dt>Requested human decision</dt><dd>{decision === 'APPROVE' ? 'Approve' : 'Reject'}</dd></div>
      <div><dt>Backend-confirmed human decision</dt><dd>{snapshot?.reviewed_status ?? 'Not yet confirmed'}</dd></div>
      <div><dt>Last confirmed run status</dt><dd>{snapshot?.status ?? 'Not yet loaded'}</dd></div>
      <div><dt>Business action execution</dt><dd>{snapshot?.actions_executed === false
        ? 'No business action executed (backend-confirmed)' : 'Review does not execute a business action'}</dd></div>
    </dl>
    {snapshot?.reviewed_status && snapshot.reviewed_status !== expectedDecision
      && <p className="run-error">The recorded decision differs from your request. Another review may have been recorded; do not resubmit.</p>}
    {phase === 'accepted' && <p className="case-caption" role="status">{!active
      ? 'Automatic tracking is paused while this view is hidden.'
      : (record.checks ?? 0) >= 20 && !record.checkingRun ? 'Automatic checks have reached their limit. The last confirmed status is RESUMING. Refresh run tracking to check again.'
        : 'A final result has not yet been confirmed. Status checks only read this run.'}</p>}
    {snapshot && <p className="case-caption">Last confirmed update (server): <time dateTime={snapshot.updated_at}>{snapshot.updated_at}</time>.</p>}
    <details>
      <summary aria-label={`Bound review identifiers for proposal ${item.proposalId}`}>Bound review identifiers</summary>
      <dl className="case-facts run-identifiers">
        <div><dt>Case ID</dt><dd>{item.caseId}</dd></div>
        <div><dt>Run ID</dt><dd>{item.runId}</dd></div>
        <div><dt>Workflow ID</dt><dd>{item.workflowId}</dd></div>
        <div><dt>Proposal ID</dt><dd>{item.proposalId}</dd></div>
        <div><dt>Review ID</dt><dd>{item.reviewId}</dd></div>
      </dl>
    </details>
    <h4>{terminal ? 'Final workflow result' : 'Workflow result not confirmed'}</h4>
    {terminal ? <>
      <p className={snapshot.status === 'FAILED' ? 'run-error' : 'case-caption'}>{snapshot.status === 'FAILED'
        ? 'The workflow failed. A failed run does not confirm whether a human decision was recorded; see the confirmed decision above.'
        : snapshot.status === 'REVIEWED' ? 'The workflow reports human review complete. This does not mean the proposed action was executed.'
          : 'The workflow reports completion. The recorded human decision is shown separately above.'}</p>
      {snapshot.message ? <><p className="case-caption">Backend-returned customer-facing message</p>
        <p className="run-message">{snapshot.message}</p></>
        : <p className="case-caption">No final customer-facing message was returned.</p>}
      {snapshot.error && <p className="run-error">The backend reported a workflow error. Use the bound identifiers when inspecting existing records.</p>}
    </> : <p className="case-caption">{snapshot?.status === 'REVIEW_REQUIRED'
      ? 'The workflow still requires review. No final reviewed result is confirmed for this attempt.'
      : 'A final workflow result has not been confirmed. Request acceptance alone does not confirm a recorded decision.'}</p>}
    <h4>Run status history</h4>
    <p className="case-caption">Backend-reported status events for this same run, including its earlier history.</p>
    {(tracking?.gap || tracking?.trimmed) && <p className="case-caption">History is incomplete. Earlier transitions may be missing; only retained events are shown.</p>}
    {!tracking?.events.length ? <p className="case-caption">{record.checkingRun
      ? 'Loading status events…' : record.needsHistory
        ? 'Status history has not been loaded yet. Read-only tracking will check when this view is active and controls are available.'
        : ['unavailable', 'unauthorized', 'stale'].includes(phase)
          ? 'No verified status history is available. See the tracking status above.'
          : 'No status events are available in the received history.'}</p>
      : <ol className="run-events review-status-events" aria-label={`Status history for run ${item.runId}`} tabIndex={0}>
        {tracking.events.map((event) => <li key={event.sequence}>
          <div className="run-event-heading"><strong>{event.state}</strong><span className="badge">Event #{event.sequence}</span></div>
          <time dateTime={event.timestamp}>{event.timestamp}</time>
        </li>)}
      </ol>}
  </div>;
}
