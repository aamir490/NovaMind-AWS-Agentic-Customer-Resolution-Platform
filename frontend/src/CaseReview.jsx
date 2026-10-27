import { useEffect, useRef, useState } from 'react';
import { api as defaultApi } from './api.js';
import { draftAction, resolutionDraftPrefill } from './caseReviewDraft.js';
import WorkflowTimeline from './WorkflowTimeline.jsx';

// Human-readable denial reason labels.
const denialLabels = {
  item_not_in_order: 'Item is not part of this order.',
  quantity_exceeds_purchased: 'Requested quantity exceeds the purchased quantity.',
  outside_return_window: 'Delivery age is outside the policy return window.',
  reason_not_allowed: 'Policy does not permit this return reason.',
  change_of_mind_requires_unused: 'Change-of-mind returns require unused condition.',
};

const label = (value) => value.replaceAll('_', ' ');

const loadingMessages = {
  cases:      'Loading available support cases\u2026',
  context:    'Loading case, customer, and order details\u2026',
  item:       'Loading inventory and applicable policy\u2026',
  assessment: 'Checking eligibility with the backend\u2026',
  resolution: 'Recording the admin case decision\u2026',
};

// Status badge CSS modifier — maps backend status strings to design-system tones.
function statusClass(status) {
  const s = (status || '').toLowerCase();
  if (s === 'open')      return 'case-status--open';
  if (s === 'resolved' || s === 'closed') return 'case-status--resolved';
  if (s === 'in_review' || s === 'in_progress') return 'case-status--active';
  return '';
}

function Detail({ title, children }) {
  return (
    <div>
      <dt>{title}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function EmptyPanel({ icon, title, children }) {
  return (
    <div className="case-empty">
      <span className="case-empty-icon" aria-hidden="true">
        {icon ?? (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round">
            <path d="M3 7V5h7l2 2h9v13H3zM8 12h8M8 16h5" />
          </svg>
        )}
      </span>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}

// Inline spinner for loading states inside panels.
function LoadingDot() {
  return <span className="case-loading-dot" aria-hidden="true" />;
}

const investigationStateLabels = {
  NOT_AVAILABLE: 'No durable investigation job',
  PENDING: 'Pending',
  RUNNING: 'Running',
  REVIEW_REQUIRED: 'Human review required',
  COMPLETED: 'Completed',
  FAILED: 'Failed',
  RECOVERY_REQUIRED: 'Recovery required',
};

export function AdminInvestigation({ api, supportCase, investigation, resolutionDraft, onComplete }) {
  const [state, setState] = useState({ phase: 'idle', message: '' });
  const idempotencyKey = useRef(null);
  const draft = resolutionDraft ?? investigation.resolutionDraft;

  useEffect(() => {
    setState({ phase: 'idle', message: '' });
    idempotencyKey.current = null;
  }, [supportCase.id]);

  async function start() {
    if (!investigation.canStart || state.phase === 'submitting') return;
    setState({ phase: 'submitting', message: '' });
    try {
      if (idempotencyKey.current === null) {
        idempotencyKey.current = createGuidanceIdempotencyKey();
      }
      const updated = await api.startCaseInvestigation(supportCase.id, {
        idempotencyKey: idempotencyKey.current,
      });
      setState({ phase: 'idle', message: 'Investigation start accepted.' });
      onComplete(updated);
    } catch (error) {
      setState({ phase: 'error', message: error.message || 'The investigation could not be started.' });
    }
  }

  async function refresh() {
    if (state.phase === 'submitting') return;
    setState({ phase: 'submitting', message: '' });
    try {
      const updated = await api.caseInvestigation(supportCase.id);
      let refreshedDraft;
      if (updated.state === 'COMPLETED' && typeof api.caseResolutionDraft === 'function') {
        try {
          refreshedDraft = await api.caseResolutionDraft(supportCase.id);
        } catch (error) {
          onComplete(updated);
          setState({ phase: 'error', message: error.message || 'Investigation refreshed, but its recommendation could not be loaded.' });
          return;
        }
      }
      setState({ phase: 'idle', message: 'Investigation status refreshed.' });
      onComplete(updated, refreshedDraft);
    } catch (error) {
      setState({ phase: 'error', message: error.message || 'Investigation status could not be refreshed.' });
    }
  }

  return <section className="case-panel admin-investigation" aria-labelledby="admin-investigation-title">
    <div className="case-panel-heading">
      <div>
        <p className="eyebrow">Agentic AI investigation</p>
        <h3 id="admin-investigation-title">Investigation status</h3>
      </div>
      <span className={`case-status case-investigation--${investigation.state.toLowerCase()}`}>
        {investigationStateLabels[investigation.state]}
      </span>
    </div>
    <p className="case-caption">
      Submitted in {investigation.submissionMode ?? 'legacy'} mode. AI output remains a recommendation;
      no refund, return, replacement, status change, or customer message is executed automatically.
    </p>
    {investigation.workflowStatus && <p className="case-caption">
      Workflow: <strong>{label(investigation.workflowStatus)}</strong>
      {investigation.provider && <> · Provider: <strong>{label(investigation.provider)}</strong></>}
    </p>}
    {investigation.investigationOutcome && <p className="case-caption">
      Result: <strong>{investigation.investigationOutcome.responseSource === 'BEDROCK_VALIDATED'
        ? 'Bedrock output validated' : 'Deterministic fallback'}</strong>
      {' · '}Validation: <strong>{label(investigation.investigationOutcome.validationResult)}</strong>
      {investigation.investigationOutcome.modelId && <> · Model: <strong>{investigation.investigationOutcome.modelId}</strong></>}
      {investigation.investigationOutcome.fallbackReason && <> · Reason: <strong>{label(investigation.investigationOutcome.fallbackReason)}</strong></>}
    </p>}
    {(investigation.message || investigation.error || draft) && <>
      <p className="eyebrow">Workflow diagnostics</p>
      {investigation.error && <p className="run-error" role="alert">Diagnostic: {label(investigation.error)}</p>}
      {draft && <dl className="case-facts">
        <Detail title="AI-recommended decision">{resolutionLabels[draftAction[draft.recommendedDecision]]}</Detail>
        <Detail title="Internal reason">{draft.decisionReason}</Detail>
        <Detail title="Response source">{draft.responseSource === 'DETERMINISTIC_FALLBACK'
          ? 'Deterministic fallback (not Bedrock reasoning)' : 'Bedrock output validated against grounding'}</Detail>
        {draft.citations.length > 0 && <Detail title="Grounding">
          <ul>{draft.citations.map((citation) => <li key={citation.chunkId}>
            {citation.title} ({citation.documentId}, {citation.version})
          </li>)}</ul>
        </Detail>}
      </dl>}
      {(draft?.customerFacingResponse || investigation.message) && <p className="case-caption"><strong>Customer-facing response:</strong> {draft?.customerFacingResponse ?? investigation.message}</p>}
    </>}
    <div className="case-toolbar-actions">
      {investigation.canStart && <button type="button" onClick={start}
        disabled={state.phase === 'submitting'}>
        {state.phase === 'submitting' ? 'Starting…' : 'Investigate with AI'}
      </button>}
      <button type="button" className="button-secondary" onClick={refresh}
        disabled={state.phase === 'submitting'}>Refresh status</button>
    </div>
    {state.message && <p className={state.phase === 'error' ? 'run-error' : 'case-notice'}
      role={state.phase === 'error' ? 'alert' : 'status'}>{state.message}</p>}
  </section>;
}

const resolutionLabels = {
  APPROVE_REPLACEMENT: 'Approve replacement',
  DECLINE_REPLACEMENT: 'Decline replacement',
  REQUEST_MORE_EVIDENCE: 'Request more evidence',
  ESCALATE: 'Escalate for specialist review',
};

function createGuidanceIdempotencyKey() {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID();
  throw new Error('This browser cannot safely send customer guidance.');
}

export function AdminRefundRecommendation({ recommendation }) {
  if (!recommendation) return null;
  const labels = {
    ELIGIBLE: 'Eligible for staff review',
    NOT_ELIGIBLE: 'Not eligible',
    NEED_MORE_EVIDENCE: 'More evidence required',
  };
  return <section className="case-panel admin-resolution" aria-labelledby="admin-refund-recommendation-title">
    <div className="case-panel-heading"><div>
      <p className="eyebrow">Python-owned assessment</p>
      <h3 id="admin-refund-recommendation-title">Refund recommendation</h3>
    </div><span className="badge">Admin only</span></div>
    <p className="case-caption">
      {labels[recommendation.outcome]}. This is advisory only: it does not approve, issue, or send a refund,
      change case status, or send a customer message.
    </p>
    <dl className="case-facts">
      <Detail title="Purchased SKU"><span className="case-mono">{recommendation.sku}</span></Detail>
      <Detail title="Outcome">{labels[recommendation.outcome]}</Detail>
      <Detail title="Policy">{recommendation.policyId} · {recommendation.policyVersion}</Detail>
      <Detail title="Reason">{recommendation.decisionReason}</Detail>
      <Detail title="Source">{recommendation.responseSource === 'BEDROCK_VALIDATED'
        ? 'Bedrock wording validated against Python assessment' : 'Deterministic Python fallback'}</Detail>
      {recommendation.verifiedEvidence.length > 0 && <Detail title="Verified evidence"><ul>{recommendation.verifiedEvidence.map((fact) => <li key={fact}>{fact}</li>)}</ul></Detail>}
      {recommendation.missingEvidence.length > 0 && <Detail title="Missing evidence"><ul>{recommendation.missingEvidence.map((fact) => <li key={fact}>{fact}</li>)}</ul></Detail>}
      {recommendation.citations.length > 0 && <Detail title="Verified policy citations"><ul>{recommendation.citations.map((citation) => (
        <li key={citation.chunkId}>{citation.title} ({citation.documentId}, {citation.version})</li>
      ))}</ul></Detail>}
    </dl>
    <p className="case-boundary-notice">Use the existing Reviews workflow for any pending proposal. A review decision remains separate from final case resolution and customer communication.</p>
  </section>;
}

export function AdminCustomerGuidance({ api, supportCase, draft, latest, onComplete }) {
  const [message, setMessage] = useState('');
  const [state, setState] = useState({ phase: 'idle', message: '' });
  const idempotencyKey = useRef(null);

  useEffect(() => {
    setMessage(draft?.customerFacingResponse ?? '');
    setState({ phase: 'idle', message: '' });
    idempotencyKey.current = null;
  }, [supportCase.id, draft?.customerFacingResponse]);

  async function send(event) {
    event.preventDefault();
    if (!draft || state.phase === 'submitting' || state.phase === 'sent') return;
    setState({ phase: 'submitting', message: '' });
    try {
      if (idempotencyKey.current === null) {
        idempotencyKey.current = createGuidanceIdempotencyKey();
      }
      const communication = await api.sendCustomerGuidance(supportCase.id, {
        message, idempotencyKey: idempotencyKey.current,
      });
      setState({ phase: 'sent', message: 'Guidance sent.' });
      onComplete(communication);
    } catch (error) {
      setState({ phase: 'error', message: error.message || 'The guidance could not be sent.' });
    }
  }

  if (!draft) return null;
  const locked = state.phase === 'submitting' || state.phase === 'sent';
  return <section className="case-panel admin-resolution" aria-labelledby="admin-guidance-title">
    <div className="case-panel-heading">
      <div>
        <p className="eyebrow">Grounded AI draft</p>
        <h3 id="admin-guidance-title">Review and send customer guidance</h3>
      </div>
      <span className="badge">Admin only</span>
    </div>
    <p className="case-caption">
      Review and edit this draft before sending. Nothing is sent automatically, and sending guidance does not
      change case status, approve a proposal, or perform a business action.
    </p>
    <dl className="case-facts">
      <Detail title="Issue type">{label(draft.issueType)}</Detail>
      <Detail title="Recommended decision">{resolutionLabels[draftAction[draft.recommendedDecision]]}</Detail>
      <Detail title="Response source">{draft.responseSource === 'BEDROCK_VALIDATED'
        ? 'Bedrock validated' : 'Deterministic fallback'}</Detail>
      <Detail title="Decision reason">{draft.decisionReason}</Detail>
      <Detail title="Verified facts"><ul>{draft.verifiedFacts.map((fact) => <li key={fact}>{fact}</li>)}</ul></Detail>
      <Detail title="Missing evidence"><ul>{draft.missingEvidence.map((evidence) => <li key={evidence}>{evidence}</li>)}</ul></Detail>
      {draft.troubleshootingSteps.length > 0 && <Detail title="Approved troubleshooting steps"><ol>{draft.troubleshootingSteps.map((step) => (
        <li key={step}>{step}</li>
      ))}</ol></Detail>}
      {draft.citations.length > 0 && <Detail title="Policy citations"><ul>{draft.citations.map((citation) => (
        <li key={citation.chunkId}>{citation.title} ({citation.documentId}, {citation.version})</li>
      ))}</ul></Detail>}
    </dl>
    {latest && <div className="admin-resolution-latest">
      <strong>Latest sent guidance</strong>
      <span>{new Date(latest.sentAt).toLocaleString()}</span>
      <span>{latest.message}</span>
    </div>}
    <form className="admin-resolution-form" onSubmit={send}>
      <label>Customer-facing guidance
        <textarea required minLength="1" maxLength="2000" value={message}
          onChange={(event) => {
            idempotencyKey.current = null;
            setMessage(event.target.value);
            setState({ phase: 'idle', message: '' });
          }} disabled={locked}
          placeholder="Provide safe troubleshooting guidance and any evidence needed next." />
      </label>
      {state.phase === 'error' && <p className="run-error" role="alert">{state.message}</p>}
      {state.phase === 'sent' && <p className="run-notice" role="status">{state.message}</p>}
      <button type="submit" disabled={locked || !message.trim()}>
        {state.phase === 'submitting' ? 'Sending guidance…'
          : state.phase === 'sent' ? 'Guidance sent' : 'Send guidance'}
      </button>
    </form>
  </section>;
}

function AdminCaseResolution({ api, supportCase, resolution, draft, onComplete }) {
  const [action, setAction] = useState('REQUEST_MORE_EVIDENCE');
  const [reason, setReason] = useState('');
  const [customerMessage, setCustomerMessage] = useState('');
  const [evidenceReviewed, setEvidenceReviewed] = useState(false);
  const [state, setState] = useState({ phase: 'idle', message: '' });
  const edits = useRef(false);
  const prefilledDraft = useRef(null);
  const canRequestEvidence = ['open', 'in_review'].includes(supportCase.status);
  const canEscalate = ['open', 'in_review'].includes(supportCase.status);
  const canFinalize = supportCase.status === 'in_review';
  const options = [
    ...(canRequestEvidence ? ['REQUEST_MORE_EVIDENCE'] : []),
    ...(canEscalate ? ['ESCALATE'] : []),
    ...(canFinalize ? ['APPROVE_REPLACEMENT', 'DECLINE_REPLACEMENT'] : []),
  ];

  useEffect(() => {
    if (!options.includes(action)) setAction(options[0] ?? 'REQUEST_MORE_EVIDENCE');
  }, [action, options.join('|')]);

  useEffect(() => {
    edits.current = false;
    prefilledDraft.current = null;
  }, [supportCase.id]);

  useEffect(() => {
    const prefill = resolutionDraftPrefill(draft, options, {
      hasRecordedResolution: Boolean(resolution?.latest), edited: edits.current,
      previousFingerprint: prefilledDraft.current,
    });
    if (!prefill) return;
    setAction(prefill.action);
    setReason(prefill.reason);
    setCustomerMessage(prefill.customerMessage);
    setEvidenceReviewed(false);
    setState({ phase: 'idle', message: '' });
    prefilledDraft.current = prefill.fingerprint;
  }, [draft?.recommendedDecision, draft?.decisionReason, draft?.customerFacingResponse,
    resolution?.latest?.decidedAt, options.join('|')]);

  async function submit(event) {
    event.preventDefault();
    if (!options.includes(action) || state.phase === 'submitting') return;
    setState({ phase: 'submitting', message: '' });
    try {
      const next = await api.resolveCase(supportCase.id, { action, reason, customerMessage, evidenceReviewed });
      setState({ phase: 'idle', message: '' });
      setReason(''); setCustomerMessage(''); setEvidenceReviewed(false);
      onComplete(next);
    } catch (error) {
      setState({ phase: 'error', message: error.message || 'The decision could not be recorded.' });
    }
  }

  return <section className="case-panel admin-resolution" aria-labelledby="admin-resolution-title">
    <div className="case-panel-heading">
      <div>
        <p className="eyebrow">Admin case resolution</p>
        <h3 id="admin-resolution-title">Record a customer outcome</h3>
      </div>
      <span className="badge">Admin only</span>
    </div>
    <p className="case-caption">
      Bedrock and workflow proposals are advisory. This records a human decision only; it never creates a shipment,
      reserves inventory, issues a refund, or performs another business action.
    </p>
    {draft && !resolution?.latest && <p className="case-caption">
      Prefilled from {draft.responseSource === 'BEDROCK_VALIDATED'
        ? 'validated Bedrock output grounded in verified facts and citations'
        : 'a deterministic grounded fallback, not Bedrock reasoning'}. Review and edit before recording a decision.
    </p>}
    {resolution?.latest && <div className="admin-resolution-latest">
      <strong>Latest: {resolutionLabels[resolution.latest.action]}</strong>
      <span>{new Date(resolution.latest.decidedAt).toLocaleString()}</span>
    </div>}
    {options.length === 0 ? <p className="run-notice" role="status">
      This case is {label(supportCase.status)} and cannot receive another resolution decision.
    </p> : <form className="admin-resolution-form" onSubmit={submit}>
      <label>Decision
        <select value={action} onChange={(event) => { edits.current = true; setAction(event.target.value); setState({ phase: 'idle', message: '' }); }} disabled={state.phase === 'submitting'}>
          {options.map((option) => <option key={option} value={option}>{resolutionLabels[option]}</option>)}
        </select>
      </label>
      <label>Internal reason
        <textarea required minLength="1" maxLength="2000" value={reason} onChange={(event) => { edits.current = true; setReason(event.target.value); }}
          disabled={state.phase === 'submitting'} placeholder="Record the verified facts and policy basis for this decision." />
      </label>
      <label>Customer-facing response
        <textarea required minLength="1" maxLength="2000" value={customerMessage} onChange={(event) => { edits.current = true; setCustomerMessage(event.target.value); }}
          disabled={state.phase === 'submitting'} placeholder="Explain the outcome and any next steps for the customer." />
      </label>
      {action === 'APPROVE_REPLACEMENT' && <label className="admin-resolution-check">
        <input type="checkbox" checked={evidenceReviewed} onChange={(event) => setEvidenceReviewed(event.target.checked)}
          disabled={state.phase === 'submitting'} required />
        I confirmed the supporting evidence. This is a case decision only, not replacement execution.
      </label>}
      {['APPROVE_REPLACEMENT', 'DECLINE_REPLACEMENT'].includes(action) && !canFinalize && <p className="run-notice">Final replacement decisions require the case to be in review.</p>}
      {state.phase === 'error' && <p className="run-error" role="alert">{state.message}</p>}
      <button type="submit" disabled={state.phase === 'submitting' || !options.includes(action)}>
        {state.phase === 'submitting' ? 'Recording decision…' : resolutionLabels[action]}
      </button>
    </form>}
    {resolution?.history?.length > 0 && <details className="admin-resolution-history">
      <summary>Decision audit history ({resolution.history.length})</summary>
      <ol>
        {resolution.history.map((entry) => <li key={`${entry.decidedAt}-${entry.adminUserId}`}>
          <strong>{resolutionLabels[entry.action]}</strong><span>{entry.reason}</span>
          <small>Recorded {new Date(entry.decidedAt).toLocaleString()} by {entry.adminUserId}</small>
        </li>)}
      </ol>
    </details>}
  </section>;
}

export default function CaseReview({ api = defaultApi, identity }) {
  const [cases, setCases]     = useState(null);
  const [selected, setSelected] = useState('');
  const [context, setContext] = useState(null);
  const [item, setItem]       = useState(null);
  const [sku, setSku]         = useState('');
  const [inputs, setInputs]   = useState({
    quantity: '1', days_since_delivery: '', reason: '', condition: '',
  });
  const [result, setResult]   = useState(null);
  const [pending, setPending] = useState(null);
  const [failure, setFailure] = useState(null);
  const requestVersion = useRef(0);
  const busy  = pending !== null;
  const error = failure?.message || '';

  useEffect(() => () => { requestVersion.current += 1; }, [api]);

  async function run(stage, request, applyResult) {
    const version = ++requestVersion.current;
    setPending(stage);
    setFailure(null);
    try {
      const data = await request();
      if (version === requestVersion.current) applyResult(data);
    } catch (cause) {
      if (version === requestVersion.current) {
        setFailure({
          stage,
          message: `${cause.message || 'Request failed'}. Check the local backend and retry.`,
        });
      }
    } finally {
      if (version === requestVersion.current) setPending(null);
    }
  }

  function clearReview() {
    requestVersion.current += 1;
    setPending(null); setFailure(null);
    setContext(null); setItem(null); setSku(''); setResult(null);
    setInputs({ quantity: '1', days_since_delivery: '', reason: '', condition: '' });
  }

  function edit(name, value) {
    requestVersion.current += 1;
    setPending(null);
    setInputs((previous) => ({ ...previous, [name]: value }));
    setResult(null); setFailure(null);
  }

  function loadItem(nextSku) {
    setItem(null); setResult(null);
    run('item', () => api.item(nextSku), setItem);
  }

  return (
    <section className="review case-resolution" aria-labelledby="review-title" aria-busy={busy}>

      {/* ── Case selector panel ──────────────────────────────────── */}
      <div className="case-selector-panel">
        <div className="review-heading">
          <div>
            <p className="eyebrow">Operations console</p>
            <h2 id="review-title" tabIndex={-1}>Case review</h2>
          </div>
          <div className="case-toolbar-actions">
            {cases !== null && (
              <span className="case-count">
                {cases.length} {cases.length === 1 ? 'case' : 'cases'} loaded
              </span>
            )}
            <button
              className="button-secondary"
              disabled={busy}
              onClick={() => {
                clearReview(); setSelected(''); setCases(null);
                run('cases', () => api.listCases(), setCases);
              }}
            >
              {pending === 'cases' ? <><LoadingDot /> Loading\u2026</> : 'Refresh cases'}
            </button>
          </div>
        </div>

        {!!cases?.length && (
          <form
            className="case-selector-form"
            onSubmit={(event) => {
              event.preventDefault(); clearReview();
              run('context', async () => {
                const details = await api.context(selected);
                const investigation = typeof api.caseInvestigation === 'function'
                  ? await api.caseInvestigation(selected) : null;
                if (identity?.role !== 'ADMIN' || typeof api.caseResolution !== 'function') {
                  return { ...details, investigation };
                }
                const [resolution, resolutionDraft, latestGuidance] = await Promise.all([
                  api.caseResolution(selected),
                  typeof api.caseResolutionDraft === 'function' ? api.caseResolutionDraft(selected) : null,
                  typeof api.latestCustomerGuidance === 'function' ? api.latestCustomerGuidance(selected) : null,
                ]);
                return { ...details, investigation, resolution, resolutionDraft, latestGuidance };
              }, setContext);
            }}
          >
            <fieldset disabled={busy}>
              <label>
                Support case
                <select
                  required
                  value={selected}
                  onChange={(event) => { setSelected(event.target.value); clearReview(); }}
                >
                  <option value="">Choose a case\u2026</option>
                  {cases.map((entry) => (
                    <option key={entry.id} value={entry.id}>
                      {entry.subject} \u2014 {entry.id}
                    </option>
                  ))}
                </select>
              </label>
              <button type="submit" disabled={!selected || busy}>
                {pending === 'context' ? 'Loading\u2026' : 'Load case'}
              </button>
            </fieldset>
          </form>
        )}

        <p className="review-description">
          {identity?.role === 'ADMIN'
            ? 'Review evidence and record a customer outcome. Case decisions never execute stock, refunds, or replacements.'
            : 'Read-only review workspace. No case updates, stock reservations, refunds, or approvals are performed here.'}
        </p>
      </div>

      {/* ── Global status / error ────────────────────────────────── */}
      {busy && pending !== 'cases' && pending !== 'context' && (
        <p className="case-notice" role="status">
          <LoadingDot />{loadingMessages[pending]}
        </p>
      )}
      {error && (
        <div className="case-error-banner" role="alert">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
            aria-hidden="true">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v4M12 16h.01" />
          </svg>
          <span>{error}</span>
        </div>
      )}

      {/* ── Pre-context states ───────────────────────────────────── */}
      {!context && !busy && !error && (
        <div className="case-panel case-start" role="status">
          {cases === null ? (
            <EmptyPanel title="Start a case review">
              Refresh cases to load the records available to your identity.
              Then choose a case to review its customer, order, and item details.
            </EmptyPanel>
          ) : cases.length === 0 ? (
            <EmptyPanel
              title="No cases available"
              icon={
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round">
                  <path d="M20 7H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V9a2 2 0 00-2-2zM16 3H8l-2 4h12z" />
                </svg>
              }
            >
              No support cases were returned. Refresh when case records are available in this process.
            </EmptyPanel>
          ) : (
            <EmptyPanel title="Select a case to begin">
              Choose a support case above and load its customer, order, and item details.
            </EmptyPanel>
          )}
        </div>
      )}

      {/* ── Loaded case content ──────────────────────────────────── */}
      {context && (
        <>
          {/* Case summary header */}
          <section className="case-panel case-summary" aria-labelledby="case-summary-title">
            <div className="case-panel-heading">
              <div>
                <p className="eyebrow">Case details</p>
                <h3 id="case-summary-title">{context.supportCase.subject}</h3>
              </div>
              <span className={`case-status ${statusClass(context.supportCase.status)}`}>
                {label(context.supportCase.status)}
              </span>
            </div>

            <p className="case-description">{context.supportCase.description}</p>

            <dl className="case-reference">
              <Detail title="Case ID">
                <span className="case-mono">{context.supportCase.id}</span>
              </Detail>
            </dl>
          </section>

          {identity?.role === 'ADMIN' && context.investigation
              && typeof api.startCaseInvestigation === 'function' && (
            <AdminInvestigation api={api} supportCase={context.supportCase}
              investigation={context.investigation}
              resolutionDraft={context.resolutionDraft}
              onComplete={(investigation, resolutionDraft) => setContext((current) => current && ({
                ...current, investigation,
                ...(resolutionDraft === undefined ? {} : { resolutionDraft }),
              }))} />
          )}

          {identity?.role === 'ADMIN' && context.investigation
              && typeof api.diagnosticInvestigations === 'function' && (
            <WorkflowTimeline api={api} identity={identity} caseId={context.supportCase.id} />
          )}

          {identity?.role === 'ADMIN' && typeof api.sendCustomerGuidance === 'function' && (
            <AdminRefundRecommendation recommendation={context.resolutionDraft?.refundRecommendation} />
          )}

          {identity?.role === 'ADMIN' && typeof api.sendCustomerGuidance === 'function' && (
            <AdminCustomerGuidance api={api} supportCase={context.supportCase}
              draft={context.resolutionDraft} latest={context.latestGuidance}
              onComplete={(latestGuidance) => setContext((current) => current && ({
                ...current, latestGuidance,
              }))} />
          )}

          {identity?.role === 'ADMIN' && context.resolution && typeof api.resolveCase === 'function' && (
            <AdminCaseResolution api={api} supportCase={context.supportCase} resolution={context.resolution}
              draft={context.resolutionDraft}
              onComplete={(resolution) => setContext((current) => current && ({
                ...current, supportCase: resolution.case, resolution,
              }))} />
          )}

          {/* Context + policy grid */}
          <div className="case-details-grid">

            {/* Customer & order context */}
            <section className="case-panel" aria-labelledby="case-context-title">
              <div className="case-panel-heading">
                <div>
                  <p className="eyebrow">Customer &amp; order</p>
                  <h3 id="case-context-title">Review context</h3>
                </div>
              </div>

              <dl className="case-facts">
                <Detail title="Customer name">{context.customer.name}</Detail>
                <Detail title="Customer ID">
                  <span className="case-mono">{context.customer.id}</span>
                </Detail>
                <Detail title="Order ID">
                  <span className="case-mono">{context.order.id}</span>
                </Detail>
              </dl>

              <div className="case-subsection">
                <h4>Ordered items</h4>
                <ul className="case-item-list">
                  {context.order.items.map((entry, index) => (
                    <li key={`${entry.sku}-${index}`}>
                      <div>
                        <strong>{entry.name}</strong>
                        <span className="case-item-sku">{entry.sku}</span>
                      </div>
                      <span className="case-item-quantity">Qty {entry.quantity}</span>
                    </li>
                  ))}
                </ul>

                <label className="case-item-select">
                  Item to review
                  <select
                    disabled={busy}
                    value={sku}
                    onChange={(event) => {
                      const next = event.target.value;
                      requestVersion.current += 1;
                      setPending(null); setFailure(null);
                      setSku(next); setItem(null); setResult(null);
                      setInputs({ quantity: '1', days_since_delivery: '', reason: '', condition: '' });
                      if (next) loadItem(next);
                    }}
                  >
                    <option value="">Choose an item\u2026</option>
                    {[...new Set(context.order.items.map((entry) => entry.sku))].map((value) => (
                      <option key={value} value={value}>{value}</option>
                    ))}
                  </select>
                </label>
              </div>
            </section>

            {/* Inventory & policy reference */}
            <section className="case-panel" aria-labelledby="case-policy-title">
              <div className="case-panel-heading">
                <div>
                  <p className="eyebrow">Policy reference</p>
                  <h3 id="case-policy-title">Inventory &amp; applicable policy</h3>
                </div>
              </div>

              {item ? (
                <>
                  {/* Stock indicator */}
                  <div className={`case-stock ${item.inventory.available_quantity > 0 ? 'case-stock--in' : 'case-stock--out'}`}>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                      strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
                      aria-hidden="true">
                      <path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z" />
                      <polyline points="9 22 9 12 15 12 15 22" />
                    </svg>
                    <div>
                      <strong>{item.inventory.available_quantity} units</strong>
                      <span>available &mdash; {item.inventory.sku}</span>
                    </div>
                    <span className={`case-stock-badge ${item.inventory.available_quantity > 0 ? 'case-stock-badge--in' : 'case-stock-badge--out'}`}>
                      {item.inventory.available_quantity > 0 ? 'In stock' : 'Out of stock'}
                    </span>
                  </div>
                  <p className="case-caption">
                    Stock level is informational for this assessment and does not affect eligibility.
                  </p>

                  {/* Policy facts */}
                  <dl className="case-facts case-policy-facts">
                    <Detail title="Policy ID &amp; version">
                      <span className="case-mono">{item.policy.id}</span>
                      {' '}&middot; {item.policy.version}
                    </Detail>
                    <Detail title="Return window">
                      <span className="case-policy-value">{item.policy.return_window_days} days</span>
                      <span className="case-policy-note">(from delivery date, inclusive)</span>
                    </Detail>
                    <Detail title="Allowed return reasons">
                      <ul className="case-policy-reasons">
                        {item.policy.allowed_reasons.map((r) => (
                          <li key={r}>{label(r)}</li>
                        ))}
                      </ul>
                    </Detail>
                    <Detail title="Unused condition for change of mind">
                      <span className={item.policy.change_of_mind_requires_unused ? 'case-policy-req' : ''}>
                        {item.policy.change_of_mind_requires_unused ? 'Required' : 'Not required'}
                      </span>
                    </Detail>
                    <Detail title="Return required for refund">
                      <span className={item.policy.refund_requires_return ? 'case-policy-req' : ''}>
                        {item.policy.refund_requires_return ? 'Required' : 'Not required'}
                      </span>
                    </Detail>
                  </dl>

                  <p className="case-caption case-policy-description">{item.policy.description}</p>
                </>
              ) : (
                <>
                  <EmptyPanel
                    title={
                      pending === 'item' ? 'Loading item details\u2026'
                      : failure?.stage === 'item' ? 'Item details unavailable'
                      : 'Select an item above'
                    }
                    icon={
                      pending === 'item' ? (
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                          strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round">
                          <circle cx="12" cy="12" r="10" />
                          <path d="M12 6v6l4 2" />
                        </svg>
                      ) : undefined
                    }
                  >
                    {pending === 'item'
                      ? 'Fetching inventory and policy from the backend.'
                      : failure?.stage === 'item'
                        ? 'Check the error above before retrying.'
                        : 'Choose an ordered item to view its inventory level and applicable return policy.'}
                  </EmptyPanel>
                  {failure?.stage === 'item' && sku && (
                    <button
                      type="button"
                      className="button-secondary"
                      disabled={busy}
                      onClick={() => loadItem(sku)}
                    >
                      Retry item details
                    </button>
                  )}
                </>
              )}
            </section>
          </div>

          {/* Assessment grid — only shown once an item is loaded */}
          {item && (
            <div className="case-assessment-grid">

              {/* Assessment inputs */}
              <form
                className="case-panel"
                aria-labelledby="assessment-inputs-title"
                onSubmit={(event) => {
                  event.preventDefault(); setResult(null);
                  run('assessment', () => api.eligibility(context.order.id, {
                    customer_id: context.customer.id, sku, ...inputs,
                  }), setResult);
                }}
              >
                <div className="case-panel-heading">
                  <div>
                    <p className="eyebrow">Assessment inputs</p>
                    <h3 id="assessment-inputs-title">Scenario assumptions</h3>
                  </div>
                </div>

                <p className="case-caption case-assessment-note">
                  Enter the scenario inputs below. Delivery age, return reason, and item condition
                  are <strong>unverified</strong> — they have not been confirmed against order evidence
                  or payment history.
                </p>

                <fieldset disabled={busy}>
                  <div className="input-grid">
                    <label>
                      Requested quantity
                      <input
                        type="number" min="1" max="1000" step="1" required
                        value={inputs.quantity}
                        onChange={(event) => edit('quantity', event.target.value)}
                      />
                    </label>
                    <label>
                      Days since delivery
                      <input
                        type="number" min="0" max="36500" step="1" required
                        value={inputs.days_since_delivery}
                        onChange={(event) => edit('days_since_delivery', event.target.value)}
                      />
                    </label>
                    <label>
                      Return reason
                      <select
                        required
                        value={inputs.reason}
                        onChange={(event) => edit('reason', event.target.value)}
                      >
                        <option value="">Choose a reason\u2026</option>
                        {item.policy.allowed_reasons.map((value) => (
                          <option key={value} value={value}>{label(value)}</option>
                        ))}
                      </select>
                    </label>
                    <label>
                      Item condition
                      <select
                        required
                        value={inputs.condition}
                        onChange={(event) => edit('condition', event.target.value)}
                      >
                        <option value="">Choose a condition\u2026</option>
                        {['unused', 'used', 'damaged'].map((value) => (
                          <option key={value} value={value}>{value}</option>
                        ))}
                      </select>
                    </label>
                  </div>

                  <div className="case-form-footer">
                    <button type="submit" disabled={busy} aria-busy={pending === 'assessment'}>
                      {pending === 'assessment' ? 'Checking\u2026' : 'Check eligibility'}
                    </button>
                    <span>Assessment only \u00b7 no action execution</span>
                  </div>
                </fieldset>
              </form>

              {/* Assessment result */}
              <section
                className={`case-panel${result ? ' assessment' : ''}`}
                aria-labelledby="assessment-result-title"
              >
                <div className="case-panel-heading">
                  <div>
                    <p className="eyebrow">Backend assessment</p>
                    <h3 id="assessment-result-title">Eligibility result</h3>
                  </div>
                  {result && (
                    <span className={`case-assessment-verdict ${result.return_eligible || result.refund_eligible ? 'case-assessment-verdict--partial' : 'case-assessment-verdict--denied'}`}>
                      {result.return_eligible || result.refund_eligible ? 'Conditionally eligible' : 'Ineligible'}
                    </span>
                  )}
                </div>

                {result ? (
                  <div role="status">
                    {/* Eligibility outcome tiles */}
                    <div className="eligibility-outcomes">
                      <div className={result.return_eligible ? 'eligibility-tile--eligible' : 'eligibility-tile--ineligible'}>
                        <span className="eligibility-tile-label">Return</span>
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                          strokeWidth="2" strokeLinejoin="round" strokeLinecap="round"
                          aria-hidden="true">
                          {result.return_eligible
                            ? <path d="M22 11.08V12a10 10 0 11-5.93-9.14M22 4L12 14.01l-3-3" />
                            : <><circle cx="12" cy="12" r="10" /><path d="M15 9l-6 6M9 9l6 6" /></>}
                        </svg>
                        <strong>{result.return_eligible ? 'Eligible' : 'Ineligible'}</strong>
                      </div>
                      <div className={result.refund_eligible ? 'eligibility-tile--eligible' : 'eligibility-tile--ineligible'}>
                        <span className="eligibility-tile-label">Refund</span>
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                          strokeWidth="2" strokeLinejoin="round" strokeLinecap="round"
                          aria-hidden="true">
                          {result.refund_eligible
                            ? <path d="M22 11.08V12a10 10 0 11-5.93-9.14M22 4L12 14.01l-3-3" />
                            : <><circle cx="12" cy="12" r="10" /><path d="M15 9l-6 6M9 9l6 6" /></>}
                        </svg>
                        <strong>{result.refund_eligible ? 'Eligible' : 'Ineligible'}</strong>
                      </div>
                    </div>

                    {/* Denial reasons */}
                    {result.denial_reasons.length > 0 && (
                      <div className="case-denials">
                        <h4>
                          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                            strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"
                            aria-hidden="true">
                            <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0zM12 9v4M12 17h.01" />
                          </svg>
                          Denial reasons
                        </h4>
                        <ul>
                          {result.denial_reasons.map((reason) => (
                            <li key={reason}>
                              {denialLabels[reason] || label(reason)}
                              {' '}<code>({reason})</code>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Operational boundary notice */}
                    <p className="case-boundary-notice">
                      Eligibility is not authorization. No action is executed by this workspace.
                    </p>

                    {/* Full evaluated inputs — collapsible for space efficiency */}
                    <details className="case-assessment-details">
                      <summary>Inputs evaluated by the API</summary>
                      <dl className="case-facts">
                        <Detail title="Order / Customer">
                          <span className="case-mono">{result.order_id}</span>
                          {' / '}
                          <span className="case-mono">{result.customer_id}</span>
                        </Detail>
                        <Detail title="SKU">
                          <span className="case-mono">{result.sku}</span>
                        </Detail>
                        <Detail title="Requested / Purchased quantity">
                          {result.requested_quantity} / {result.purchased_quantity}
                        </Detail>
                        <Detail title="Days since delivery">{result.days_since_delivery}</Detail>
                        <Detail title="Reason / Condition">
                          {label(result.reason)} / {label(result.condition)}
                        </Detail>
                        <Detail title="Policy">
                          <span className="case-mono">{result.policy_id}</span>
                          {' '}&middot; {result.policy_version}
                        </Detail>
                        <Detail title="Assessment basis">{label(result.assessment_basis)}</Detail>
                        <Detail title="Return required for refund">
                          {result.refund_requires_return ? 'Yes' : 'No'}
                        </Detail>
                        <Detail title="Authorization granted">
                          <span className={result.authorization_granted ? 'case-auth-yes' : 'case-auth-no'}>
                            {result.authorization_granted ? 'Yes' : 'No'}
                          </span>
                        </Detail>
                      </dl>
                    </details>
                  </div>
                ) : (
                  <EmptyPanel
                    title={
                      pending === 'assessment' ? 'Assessment in progress\u2026'
                      : failure?.stage === 'assessment' ? 'Assessment unavailable'
                      : 'No assessment yet'
                    }
                    icon={
                      pending === 'assessment' ? (
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                          strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round">
                          <circle cx="12" cy="12" r="10" />
                          <path d="M12 6v6l4 2" />
                        </svg>
                      ) : (
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                          strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round">
                          <path d="M9 4H5v17h14V4h-4M9 3h6v4H9zM8 13l3 3 5-6" />
                        </svg>
                      )
                    }
                  >
                    {pending === 'assessment'
                      ? 'Waiting for the backend eligibility result.'
                      : failure?.stage === 'assessment'
                        ? 'Check the error above before retrying.'
                        : 'Enter the scenario inputs and check eligibility to see the backend result here.'}
                  </EmptyPanel>
                )}
              </section>
            </div>
          )}
        </>
      )}
    </section>
  );
}
