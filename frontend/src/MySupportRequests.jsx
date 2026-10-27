import { useEffect, useState } from 'react';

function date(value) {
  return <time dateTime={value}>{new Date(value).toLocaleString()}</time>;
}

const resolutionLabels = {
  APPROVE_REPLACEMENT: 'Replacement approved',
  DECLINE_REPLACEMENT: 'Replacement declined',
  REQUEST_MORE_EVIDENCE: 'More evidence requested',
  ESCALATE: 'Escalated for specialist review',
};

const lifecycleLabels = {
  PENDING: 'Pending',
  INVESTIGATING: 'Investigating',
  AI_REVIEW_COMPLETED: 'AI Review Completed',
  AWAITING_ADMIN_DECISION: 'Awaiting Admin Decision',
  ADDITIONAL_EVIDENCE_REQUIRED: 'Additional Evidence Required',
  RESOLVED: 'Resolved',
};

function inlineNumberedBlocks(line) {
  const matches = [...line.matchAll(/(^|[ \t]+)(\d{1,2})[.)][ \t]+/g)];
  if (matches.length < 2 || Number(matches[0][2]) !== 1
      || matches.some((match, index) => Number(match[2]) !== index + 1)) return null;
  const items = matches.map((match, index) => ({
    number: Number(match[2]),
    text: line.slice(match.index + match[0].length,
      index + 1 < matches.length ? matches[index + 1].index : line.length).trim(),
  }));
  if (items.some((item) => !item.text)) return null;
  const prefix = line.slice(0, matches[0].index).trim();
  return [...(prefix ? [{ type: 'paragraph', text: prefix }] : []),
    { type: 'ordered', items }];
}

export function supportMessageBlocks(message) {
  const blocks = [];
  let paragraph = [];
  let list = null;

  function flushParagraph() {
    if (paragraph.length) blocks.push({ type: 'paragraph', text: paragraph.join('\n') });
    paragraph = [];
  }
  function flushList() {
    if (list) blocks.push(list);
    list = null;
  }

  for (const line of message.replaceAll('\r\n', '\n').replaceAll('\r', '\n').split('\n')) {
    if (!line.trim()) {
      flushParagraph(); flushList();
      continue;
    }
    const numbered = line.match(/^\s*(\d{1,3})[.)]\s+(.+)$/);
    const bulleted = line.match(/^\s*[-*•]\s+(.+)$/);
    if (numbered) {
      flushParagraph();
      if (list?.type !== 'ordered') { flushList(); list = { type: 'ordered', items: [] }; }
      list.items.push({ number: Number(numbered[1]), text: numbered[2] });
      continue;
    }
    if (bulleted) {
      flushParagraph();
      if (list?.type !== 'unordered') { flushList(); list = { type: 'unordered', items: [] }; }
      list.items.push({ text: bulleted[1] });
      continue;
    }
    flushList();
    const inline = inlineNumberedBlocks(line);
    if (inline) {
      flushParagraph();
      blocks.push(...inline);
    } else {
      paragraph.push(line);
    }
  }
  flushParagraph(); flushList();
  return blocks;
}

export function SupportMessage({ message }) {
  return <div className="support-update-content">
    {supportMessageBlocks(message).map((block, index) => {
      if (block.type === 'ordered') return <ol key={index}>
        {block.items.map((item, itemIndex) => <li key={itemIndex} value={item.number}>{item.text}</li>)}
      </ol>;
      if (block.type === 'unordered') return <ul key={index}>
        {block.items.map((item, itemIndex) => <li key={itemIndex}>{item.text}</li>)}
      </ul>;
      return <p key={index}>{block.text}</p>;
    })}
  </div>;
}

// A new selection mounts a fresh read, clearing old data and cancelling its request.
export default function MySupportRequests({ api }) {
  const [caseId, setCaseId] = useState(null);
  return <SupportRequestRead key={caseId ?? 'list'} api={api} caseId={caseId} onSelect={setCaseId} />;
}

function SupportRequestRead({ api, caseId, onSelect }) {
  const [state, setState] = useState({ phase: 'loading' });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ phase: 'loading' });
    async function load() {
      try {
        let data;
        if (caseId) {
          const supportCase = await api.getMyCase(caseId, { signal: controller.signal });
          const [resolution, lifecycle, latestSupportUpdate] = await Promise.all([
            typeof api.getMyCaseResolution === 'function'
              ? api.getMyCaseResolution(caseId, { signal: controller.signal }) : null,
            typeof api.getMyCaseLifecycle === 'function'
              ? api.getMyCaseLifecycle(caseId, { signal: controller.signal }) : null,
            typeof api.latestCustomerGuidance === 'function'
              ? api.latestCustomerGuidance(caseId, { signal: controller.signal }) : null,
          ]);
          data = { ...supportCase, resolution, lifecycle, latestSupportUpdate };
        } else {
          data = await api.listMyCases({ signal: controller.signal });
        }
        if (!controller.signal.aborted) setState({ phase: 'ready', data });
      } catch (error) {
        if (!controller.signal.aborted) setState({ phase: 'error',
          message: error.message || 'Could not load your support requests. Please try again.' });
      }
    }
    load();
    return () => controller.abort();
  }, [api, caseId, attempt]);

  function retry() {
    if (state.phase === 'loading') return;
    setState({ phase: 'loading' });
    setAttempt((value) => value + 1);
  }

  const lifecycleDuplicatesApprovedResponse = state.phase === 'ready'
    && state.data.lifecycle && state.data.resolution?.latest
    && state.data.lifecycle.message === state.data.resolution.latest.customerMessage;

  return (
    <section className="my-orders" aria-labelledby="my-support-requests-heading">
      <div className="orders-header">
        <h2 id="my-support-requests-heading">{caseId ? 'Support request details' : 'My Support Requests'}</h2>
        <div className="case-toolbar-actions">
          <button type="button" className="button-secondary" onClick={retry}
            disabled={state.phase === 'loading'} aria-busy={state.phase === 'loading'}>
            {state.phase === 'loading' ? 'Refreshing…' : caseId ? 'Refresh request' : 'Refresh requests'}
          </button>
          {caseId && <button type="button" className="button-secondary" onClick={() => onSelect(null)}>
            Back to My Support Requests
          </button>}
        </div>
      </div>

      {state.phase === 'loading' && <p className="customer-notice" role="status">
        {caseId ? 'Loading your support request…' : 'Loading your support requests…'}
      </p>}

      {state.phase === 'error' && <div>
        <p className="customer-error" role="alert">{state.message}</p>
        <button type="button" className="button-secondary" onClick={retry}>Try again</button>
      </div>}

      {state.phase === 'ready' && (caseId ? (
        <div className="case-panel">
          <dl className="case-facts support-request-facts">
            <div><dt>Case ID</dt><dd className="order-id">{state.data.id}</dd></div>
            <div><dt>Order ID</dt><dd className="order-id">{state.data.order_id}</dd></div>
            <div><dt>Subject</dt><dd>{state.data.subject}</dd></div>
            <div><dt>Description</dt><dd className="support-request-description">{state.data.description}</dd></div>
            <div><dt>Status</dt><dd>{state.data.status}</dd></div>
            <div><dt>Created</dt><dd>{date(state.data.created_at)}</dd></div>
            <div><dt>Updated</dt><dd>{date(state.data.updated_at)}</dd></div>
          </dl>
          {state.data.lifecycle && <section className="customer-resolution"
            aria-labelledby="customer-investigation-progress-heading">
            <h3 id="customer-investigation-progress-heading">Investigation Progress</h3>
            <dl className="case-facts">
              <div><dt>Progress</dt><dd>{lifecycleLabels[state.data.lifecycle.progress]}</dd></div>
              {!lifecycleDuplicatesApprovedResponse && <div><dt>Update</dt><dd>{state.data.lifecycle.message}</dd></div>}
              <div><dt>Last updated</dt><dd>{date(state.data.lifecycle.updatedAt)}</dd></div>
            </dl>
          </section>}
          {state.data.latestSupportUpdate && <section className="customer-resolution"
            aria-labelledby="customer-support-update-heading">
            <h3 id="customer-support-update-heading">Latest Support Update</h3>
            <dl className="case-facts">
              <div><dt>Message</dt><dd><SupportMessage message={state.data.latestSupportUpdate.message} /></dd></div>
              <div><dt>Sent</dt><dd>{date(state.data.latestSupportUpdate.sentAt)}</dd></div>
            </dl>
          </section>}
          {state.data.resolution?.latest && <section className="customer-resolution" aria-labelledby="customer-resolution-heading">
            <h3 id="customer-resolution-heading">Latest case decision</h3>
            <dl className="case-facts">
              <div><dt>Status</dt><dd>{state.data.resolution.case.status}</dd></div>
              <div><dt>Decision</dt><dd>{resolutionLabels[state.data.resolution.latest.action]}</dd></div>
              <div><dt>Response</dt><dd>{state.data.resolution.latest.customerMessage}</dd></div>
              <div><dt>Decided</dt><dd>{date(state.data.resolution.latest.decidedAt)}</dd></div>
            </dl>
          </section>}
        </div>
      ) : state.data.length === 0 ? (
        <div className="customer-empty" role="status">
          <h3>No support requests yet</h3>
          <p>You can report a problem from My Orders when you need help with an order.</p>
        </div>
      ) : (
        <ul className="orders-list" aria-label="Your support requests">
          {state.data.map((supportCase) => <li key={supportCase.id} className="order-row case-panel">
            <dl className="case-facts support-request-facts order-row-body">
              <div><dt>Subject</dt><dd>{supportCase.subject}</dd></div>
              <div><dt>Status</dt><dd>{supportCase.status}</dd></div>
              <div><dt>Created date</dt><dd>{date(supportCase.created_at)}</dd></div>
            </dl>
            <button type="button" className="button-secondary order-detail-btn"
              aria-label={`View details for ${supportCase.subject}`} onClick={() => onSelect(supportCase.id)}>
              View details
            </button>
          </li>)}
        </ul>
      ))}
    </section>
  );
}
