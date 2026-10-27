import { useState } from 'react';

function uncertain(error) {
  return error?.name === 'AbortError' || error?.name === 'TimeoutError'
    || /timed out|timeout|aborted/i.test(error?.message ?? '');
}
export function CustomerProvisioning({ api }) {
  const [name, setName] = useState('');
  const [phase, setPhase] = useState('idle');
  const [message, setMessage] = useState('');
  const [customer, setCustomer] = useState(null);
  const [verificationId, setVerificationId] = useState('');
  const [verification, setVerification] = useState(null);
  const [copyState, setCopyState] = useState('idle');

  async function create(event) {
    event.preventDefault();
    if (phase === 'submitting') return;
    setPhase('submitting');
    setMessage('Creating one customer record…');
    setCustomer(null);
    setCopyState('idle');
    try {
      const result = await api.createCustomer(name);
      setCustomer(result);
      setName('');
      setPhase('success');
      setMessage('Customer record created. Record the UUID before continuing.');
    } catch (error) {
      setPhase(uncertain(error) ? 'uncertain' : 'error');
      setMessage(uncertain(error)
        ? 'The request timed out or its response was lost. Do not retry. Reconcile by customer UUID or approved database inspection.'
        : error.message || 'Customer creation failed.');
    }
  }

  async function copyId() {
    if (!customer?.id || !globalThis.navigator?.clipboard?.writeText) {
      setCopyState('unsupported');
      return;
    }
    try {
      await globalThis.navigator.clipboard.writeText(customer.id);
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
  }

  async function verify(event) {
    event.preventDefault();
    setVerification(null);
    try {
      setVerification(await api.getCustomer(verificationId.trim()));
    } catch (error) {
      setMessage(error.message || 'Customer verification failed.');
      setVerification(null);
    }
  }

  return <section className="customer-provisioning card" aria-labelledby="customer-provisioning-title">
    <div className="panel-heading">
      <div>
        <p className="eyebrow">Admin only</p>
        <h2 id="customer-provisioning-title">Customer management</h2>
      </div>
      <span className="badge">Durable records</span>
    </div>
    <p className="customer-provisioning-intro">
      Create a customer record, capture its server-generated UUID, and verify it before any Cognito binding is prepared.
      This panel never assigns groups or changes authentication configuration.
    </p>
    <form className="customer-provisioning-form" onSubmit={create}>
      <label htmlFor="customer-provisioning-name">Approved display name
        <input id="customer-provisioning-name" value={name} maxLength={120}
          onChange={(event) => setName(event.target.value)} required
          disabled={phase === 'submitting'} autoComplete="off" />
      </label>
      <button type="submit" disabled={phase === 'submitting' || !name.trim()}>
        {phase === 'submitting' ? 'Creating…' : 'Create customer record'}
      </button>
    </form>
    <p className={`customer-provisioning-message customer-provisioning-message--${phase}`} role="status" aria-live="polite">
      {message}
    </p>
    {customer && <div className="customer-provisioning-result" role="region" aria-label="Created customer">
      <p className="eyebrow">Server-generated UUID</p>
      <code>{customer.id}</code>
      <span className="customer-provisioning-result-name">{customer.name}</span>
      <button type="button" className="button-secondary" onClick={copyId}>
        {copyState === 'copied' ? 'UUID copied' : 'Copy UUID'}
      </button>
      {copyState === 'unsupported' && <small>Clipboard access is unavailable; select the UUID manually.</small>}
      {copyState === 'failed' && <small>Copy failed; select the UUID manually.</small>}
    </div>}
    <div className="customer-provisioning-verify">
      <p className="eyebrow">Read-only verification</p>
      <form className="customer-provisioning-form" onSubmit={verify}>
        <label htmlFor="customer-provisioning-id">Customer UUID
          <input id="customer-provisioning-id" value={verificationId}
            onChange={(event) => setVerificationId(event.target.value)}
            placeholder="Paste a returned UUID" autoComplete="off" />
        </label>
        <button type="submit" className="button-secondary" disabled={!verificationId.trim()}>Verify record</button>
      </form>
      {verification && <p className="customer-provisioning-verified" role="status">
        Verified: <strong>{verification.name}</strong> <code>{verification.id}</code>
      </p>}
    </div>
  </section>;
}
