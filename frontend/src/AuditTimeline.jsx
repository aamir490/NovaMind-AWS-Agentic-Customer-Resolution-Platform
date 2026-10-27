// Receives only the closed, validated projection from api.runAudit.
// Labels are local copy; never derive descriptions from backend free-form text.

// Human-readable labels for backend kind enum values.
const kindLabels = {
  AGENT:           'Agent activity',
  REVIEW_REQUIRED: 'Review request',
  HUMAN_REVIEW:    'Human review',
  FAILURE:         'Failure',
};

function presentation(event) {
  switch (event.kind) {
    case 'AGENT':
      if (event.error === 'EARLY_DETERMINISTIC_FALLBACK') {
        return { title: 'Early deterministic fallback',
          detail: 'Repeated knowledge search was stopped and the grounded deterministic response was retained; this is not a normal AI completion.', tone: 'error' };
      }
      if (event.error === 'REPEATED_TOOL_CALL') {
        return { title: 'Repeated knowledge search detected',
          detail: 'The model selected search_knowledge again after application-owned retrieval; no second search was executed.', tone: 'error' };
      }
      return event.error !== null
        ? { title: 'Agent error recorded', detail: 'An error was recorded during agent activity.', tone: 'error' }
        : { title: event.tool !== null ? 'Tool activity recorded' : 'Agent activity recorded',
          detail: 'The workflow recorded agent activity. This metadata does not confirm tool success.', tone: 'activity' };
    case 'REVIEW_REQUIRED':
      return { title: 'Human review requested', detail: 'The workflow recorded a request for a human decision.', tone: 'review' };
    case 'HUMAN_REVIEW':
      return { title: event.proposal_status === 'APPROVED' ? 'Approval recorded'
        : event.proposal_status === 'REJECTED' ? 'Rejection recorded' : 'Human review recorded',
        detail: 'A human review event was recorded. This does not confirm business action execution.', tone: 'decision' };
    case 'FAILURE':
      return { title: 'Workflow failure recorded', detail: 'The workflow recorded a failure at this point.', tone: 'error' };
    default:
      // The API rejects unknown kinds. Do not echo unexpected strings if reused.
      return null;
  }
}

export default function AuditTimeline({ events, after, hasMore }) {
  if (events.length === 0) return <p role="status" className="run-notice">{after === 0
    ? 'No audit records are available for this run.' : 'No further audit records are available.'}</p>;

  return <>
    <div className="audit-timeline-context">
      <p className="audit-page-range">Events {events[0].sequence}–{events.at(-1).sequence}</p>
      <p className="case-caption">Ordered by audit sequence, earliest recorded event first. Times are shown in your local time zone.</p>
      {after > 0 && <p className="case-caption">Continued from the previous page. Earlier events are available using Previous audit page.</p>}
    </div>
    <ol className="audit-timeline" aria-label="Run audit timeline" start={events[0].sequence} tabIndex={0}>
      {events.map((event) => {
        const display = presentation(event);
        if (!display) return null;
        return <li key={event.sequence} value={event.sequence} className="audit-timeline-item" data-tone={event.error !== null ? 'error' : display.tone}>
          <article className="audit-event-card" aria-labelledby={`audit-event-${event.sequence}`}>
            <div className="audit-event-heading">
              <div>
                <p className="audit-event-kind">{kindLabels[event.kind] ?? event.kind} · #{event.sequence}</p>
                <h4 id={`audit-event-${event.sequence}`}>{display.title}</h4>
              </div>
              <time dateTime={event.timestamp}>{new Date(event.timestamp).toLocaleString()}</time>
            </div>
            <p className="audit-event-description">{display.detail}</p>
            {(event.tool !== null || event.proposal_status !== null || event.proposal_id !== null
              || event.reviewer_user_id !== null || event.error !== null) && <dl className="audit-event-facts">
              {event.tool !== null && <div><dt>Tool</dt><dd><code>{event.tool}</code></dd></div>}
              {event.proposal_status !== null && <div><dt>Recorded proposal status</dt><dd>{event.proposal_status}</dd></div>}
              {event.reviewer_user_id !== null && <div><dt>Reviewer user ID</dt><dd><code>{event.reviewer_user_id}</code></dd></div>}
              {event.error !== null && <div><dt>Error code</dt><dd><code>{event.error}</code></dd></div>}
              {event.proposal_id !== null && <div><dt>Proposal ID</dt><dd><code>{event.proposal_id}</code></dd></div>}
            </dl>}
          </article>
        </li>;
      })}
    </ol>
    <p className="case-caption">{hasMore ? 'The timeline continues on the next page.'
      : 'End of the available records at this refresh. This does not indicate that the run completed.'}</p>
  </>;
}
