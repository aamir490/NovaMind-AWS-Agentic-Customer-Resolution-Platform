import { useEffect, useState } from 'react';
import { buildWorkflowTimeline } from './workflowTimeline.js';

function formatDate(value) {
  return value ? new Date(value).toLocaleString() : 'Not recorded';
}
function formatDuration(value) {
  if (!Number.isFinite(value) || value < 0) return null;
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(1)} s`;
}
function TimelineStatus({ phase, message }) {
  if (phase === 'loading') return <p className="case-notice" role="status">Loading recorded workflow timeline…</p>;
  if (phase === 'error') return <p className="run-error" role="alert">{message}</p>;
  return null;
}

export default function WorkflowTimeline({ api, identity, caseId }) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState({ phase: 'idle', data: null, message: '' });

  useEffect(() => {
    setOpen(false);
    setState({ phase: 'idle', data: null, message: '' });
  }, [caseId]);

  async function load() {
    if (state.phase === 'loading') return;
    setOpen(true);
    setState({ phase: 'loading', data: null, message: '' });
    try {
      const durablePage = await api.diagnosticInvestigations({ caseId, limit: 25 });
      // The diagnostic response is durable and case-filtered.  Require the
      // requested case identity instead of falling back to another row, so a
      // timeline can never display a mismatched workflow/run pair.
      const durable = durablePage.items.find((item) => item.case_id === caseId) ?? null;
      let auditEvents = [];
      if (durable && identity?.instance_id && typeof api.auditRuns === 'function' && typeof api.runAudit === 'function') {
        const runs = await api.auditRuns({ instance_id: identity.instance_id, case_id: caseId });
        const run = runs.items.find((item) => item.run_id === durable.run_id);
        if (run) {
          const page = await api.runAudit({ instance_id: identity.instance_id, case_id: caseId, run_id: run.run_id });
          auditEvents = page.events;
        }
      }
      const diagnosticEvents = [];
      if (typeof api.diagnosticEvents === 'function' && durable) {
        for (const category of ['workflow', 'model', 'tools']) {
          try {
            const page = await api.diagnosticEvents(category, { limit: 50 });
            diagnosticEvents.push(...page.events.filter((event) =>
              event.workflow_id === durable.workflow_id
              || event.run_id === durable.run_id
              || event.case_id === caseId));
          } catch {
            // Process-local diagnostics are supplementary. Durable metadata and
            // audit evidence remain usable if a category is unavailable.
          }
        }
      }
      setState({ phase: 'ready', data: buildWorkflowTimeline({ durable, auditEvents, diagnosticEvents }), message: '' });
    } catch (error) {
      setState({ phase: 'error', data: null, message: error.message || 'Recorded workflow timeline could not be loaded.' });
    }
  }

  const data = state.data;
  return <section className="case-panel workflow-timeline-panel" aria-labelledby="workflow-timeline-title" aria-busy={state.phase === 'loading'}>
    <div className="case-panel-heading">
      <div><p className="eyebrow">Admin-only evidence view</p><h3 id="workflow-timeline-title">Agent workflow timeline</h3></div>
      <button type="button" className="button-secondary" onClick={load} disabled={state.phase === 'loading'}>
        {state.phase === 'loading' ? 'Loading…' : data ? 'Refresh timeline' : 'View timeline'}
      </button>
    </div>
    <p className="case-caption">Only durable workflow metadata, persisted audit events, and matching process diagnostics are shown. Missing stages are not inferred.</p>
    {open && <TimelineStatus phase={state.phase} message={state.message} />}
    {data?.summary && <dl className="case-facts workflow-timeline-summary">
      <div><dt>Workflow status</dt><dd>{data.summary.workflowStatus}</dd></div>
      <div><dt>Provider</dt><dd>{data.summary.provider}</dd></div>
      <div><dt>Submission mode</dt><dd>{data.summary.mode ?? 'Not recorded'}</dd></div>
      <div><dt>Model</dt><dd>Not recorded by this projection</dd></div>
      <div><dt>Response source</dt><dd>{data.summary.responseSource ?? 'Not recorded'}</dd></div>
      <div><dt>Actions executed</dt><dd>{data.summary.actionsExecuted ? 'No' : 'Not available'}</dd></div>
      {data.summary.failureCategory && <div><dt>Safe error</dt><dd>{data.summary.failureCategory}{data.summary.failureStage ? ` · ${data.summary.failureStage}` : ''}</dd></div>}
    </dl>}
    {state.phase === 'ready' && !data?.hasEvidence && <p className="run-notice" role="status">No recorded timeline evidence is available for this investigation.</p>}
    {data?.items?.length > 0 && <ol className="workflow-timeline" aria-label="Recorded agent workflow stages">
      {data.items.map((item, index) => <li key={item.id} className="workflow-timeline__item">
        <span className="workflow-timeline__marker" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
        <article>
          <div className="workflow-timeline__heading"><div><p className="eyebrow">{item.source}</p><h4>{item.title}</h4></div><time dateTime={item.timestamp ?? undefined}>{formatDate(item.timestamp)}</time></div>
          <p>{item.detail}</p>
          <div className="workflow-timeline__facts">
            {item.durationMs !== null && <span>Duration: {formatDuration(item.durationMs)}</span>}
            {item.tool && <span>Tool: <code>{item.tool}</code></span>}
            {item.status && <span>Status/error: <code>{item.status}</code></span>}
          </div>
        </article>
      </li>)}
    </ol>}
    {data && <p className="case-caption workflow-timeline-note">This view does not expose prompts, model text, internal reasoning, tokens, credentials, or unrecorded activity.</p>}
  </section>;
}
