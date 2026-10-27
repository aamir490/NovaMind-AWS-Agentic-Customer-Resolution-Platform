import { useRef, useState } from 'react';

/**
 * Report a Problem — customer support intake form.
 *
 * Displays the selected order for context, then collects a subject and
 * description. Submits to POST /api/my/cases via api.createMyCase().
 *
 * Security: customer_id is NEVER included in the request — the backend
 * derives it from the authenticated identity.
 *
 * Props
 * -----
 * api       — createApi() instance
 * order     — the Order object selected from My Orders
 * onSuccess — called with the created SupportCase on success
 * onCancel  — called when the customer navigates back
 */

const SUBJECT_MAX = 200;
const DESC_MAX = 4000;

function createIdempotencyKey() {
  if (typeof crypto?.randomUUID === 'function') return crypto.randomUUID();
  throw new Error('Your browser cannot safely submit this support request.');
}

export default function ReportProblem({ api, order, onSuccess, onCancel }) {
  const [subject, setSubject]         = useState('');
  const [description, setDescription] = useState('');
  const [submitting, setSubmitting]   = useState(false);
  const [submitted, setSubmitted]     = useState(false);  // duplicate-submit guard
  const [result, setResult]           = useState(null);
  const [error, setError]             = useState(null);
  const [fieldErrors, setFieldErrors] = useState({});
  const idempotencyKey = useRef(null);

  function validate() {
    const errs = {};
    if (!subject.trim()) errs.subject = 'Please enter a subject.';
    else if (Array.from(subject).length > SUBJECT_MAX) errs.subject = `Subject must be ${SUBJECT_MAX} characters or fewer.`;
    if (!description.trim()) errs.description = 'Please describe the problem.';
    else if (Array.from(description).length > DESC_MAX) errs.description = `Description must be ${DESC_MAX} characters or fewer.`;
    return errs;
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (submitting || submitted) return;

    const errs = validate();
    if (Object.keys(errs).length > 0) {
      setFieldErrors(errs);
      return;
    }
    setFieldErrors({});
    setSubmitting(true);
    setSubmitted(true);
    setError(null);
    if (idempotencyKey.current === null) idempotencyKey.current = createIdempotencyKey();

    try {
      const supportCase = await api.createMyCase({
        orderId: order.id,
        subject: subject.trim(),
        description: description.trim(),
        idempotencyKey: idempotencyKey.current,
      });
      setResult(supportCase);
      onSuccess?.(supportCase);
    } catch (err) {
      setError(err.message || 'Could not submit your support request. Please try again.');
      setSubmitted(false);   // allow retry after server error
    } finally {
      setSubmitting(false);
    }
  }

  // -----------------------------------------------------------------
  // Success state
  // -----------------------------------------------------------------
  if (result) {
    return (
      <section className="report-problem" aria-labelledby="intake-success-heading">
        <div className="intake-success" role="status" aria-live="polite">
          <span className="intake-success-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
              strokeLinejoin="round" strokeLinecap="round">
              <path d="M22 11.08V12a10 10 0 11-5.93-9.14M22 4L12 14.01l-3-3" />
            </svg>
          </span>
          <h2 id="intake-success-heading">Support request created</h2>
          <p>Your support request has been received by our team.</p>
          <dl className="case-facts intake-success-facts">
            <div>
              <dt>Case ID</dt>
              <dd className="order-id">{result.id}</dd>
            </div>
            <div>
              <dt>Subject</dt>
              <dd>{result.subject}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd className="intake-status-open">{result.status.toUpperCase()}</dd>
            </div>
          </dl>
          <p className="case-caption">
            Please note your Case ID. Our support team will review your request.
          </p>
          <div className="intake-success-actions">
            <button type="button" onClick={onCancel}>Back to my orders</button>
          </div>
        </div>
      </section>
    );
  }

  // -----------------------------------------------------------------
  // Form state
  // -----------------------------------------------------------------
  return (
    <section className="report-problem" aria-labelledby="report-problem-heading">
      <div className="report-problem-header">
        <div>
          <p className="eyebrow">Support intake</p>
          <h2 id="report-problem-heading">Report a problem</h2>
        </div>
        <button type="button" className="button-secondary" onClick={onCancel}>
          ← Back to order
        </button>
      </div>

      {/* Order context — makes it clear which order is being reported */}
      <div className="report-order-context case-panel">
        <p className="eyebrow">Order being reported</p>
        <p className="order-id">{order.id}</p>
        <ul className="case-item-list" aria-label="Order items">
          {order.items.map((item, i) => (
            <li key={`${item.sku}-${i}`}>
              <div>
                <strong>{item.name}</strong>
                <span className="case-item-sku">{item.sku}</span>
              </div>
              <span className="case-item-quantity">Qty {item.quantity}</span>
            </li>
          ))}
        </ul>
      </div>

      <form className="report-problem-form case-panel" onSubmit={handleSubmit} noValidate>
        <fieldset disabled={submitting}>
          <label htmlFor="rp-subject">
            Subject
            <input
              id="rp-subject"
              type="text"
              required
              maxLength={SUBJECT_MAX}
              value={subject}
              onChange={(e) => { idempotencyKey.current = null; setSubject(e.target.value); setFieldErrors((p) => ({ ...p, subject: undefined })); }}
              aria-describedby={fieldErrors.subject ? 'rp-subject-error' : 'rp-subject-help'}
              aria-invalid={!!fieldErrors.subject}
              placeholder="e.g. Received wrong item"
            />
            {fieldErrors.subject
              ? <span id="rp-subject-error" className="intake-field-error" role="alert">{fieldErrors.subject}</span>
              : <span id="rp-subject-help" className="case-caption">Briefly describe the issue ({SUBJECT_MAX} characters max).</span>}
          </label>

          <label htmlFor="rp-description">
            Description
            <textarea
              id="rp-description"
              required
              maxLength={DESC_MAX}
              rows={5}
              value={description}
              onChange={(e) => { idempotencyKey.current = null; setDescription(e.target.value); setFieldErrors((p) => ({ ...p, description: undefined })); }}
              aria-describedby={fieldErrors.description ? 'rp-desc-error' : 'rp-desc-help'}
              aria-invalid={!!fieldErrors.description}
              placeholder="Please describe what happened and how we can help."
            />
            {fieldErrors.description
              ? <span id="rp-desc-error" className="intake-field-error" role="alert">{fieldErrors.description}</span>
              : <span id="rp-desc-help" className="case-caption">{DESC_MAX} characters max.</span>}
          </label>
        </fieldset>

        {error && (
          <p className="customer-error" role="alert">{error}</p>
        )}

        <div className="report-problem-actions">
          <button
            type="submit"
            disabled={submitting || submitted}
            aria-busy={submitting}
          >
            {submitting ? 'Submitting…' : 'Submit support request'}
          </button>
          <span className="case-caption">
            {submitting ? 'Sending your request…' : 'Your request will be reviewed by our team.'}
          </span>
        </div>
      </form>
    </section>
  );
}
