import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi } from '../frontend/src/api.js';

test('case context uses existing IDs and GET requests only', async () => {
  const calls = [];
  const values = [{ customer_id: 'customer-1', order_id: 'order-1' }, { name: 'Demo' }, { items: [] }];
  const api = createApi(async (url, options) => {
    calls.push([url, options]);
    return { ok: true, json: async () => values.shift() };
  });
  const result = await api.context('case-1');
  assert.equal(result.customer.name, 'Demo');
  assert.deepEqual(calls.map(([url]) => url), ['/api/cases/case-1', '/api/customers/customer-1', '/api/orders/order-1']);
  assert.ok(calls.every(([, options]) => options.method === 'GET' && options.body === undefined));
});

test('inventory chooses the returned policy, not a hard-coded policy', async () => {
  const calls = [];
  const api = createApi(async (url) => {
    calls.push(url);
    return { ok: true, json: async () => calls.length === 1
      ? { sku: 'LAP /1', policy_id: 'policy/demo', available_quantity: 0 }
      : { id: 'policy/demo', version: 'v1', return_window_days: 30, allowed_reasons: ['wrong_item'],
        change_of_mind_requires_unused: true, refund_requires_return: true, description: 'Local policy' } };
  });
  const result = await api.item('LAP /1');
  assert.deepEqual(calls, ['/api/inventory/LAP%20%2F1', '/api/policies/policy%2Fdemo']);
  assert.equal(result.inventory.available_quantity, 0);
});

test('eligibility preserves inputs and backend decisions without frontend rules', async () => {
  const inputs = { customer_id: 'c', sku: 'A&B', quantity: '2', days_since_delivery: '31', reason: 'wrong_item', condition: 'used' };
  const decision = { order_id: 'order', customer_id: inputs.customer_id, sku: inputs.sku,
    reason: inputs.reason, condition: inputs.condition, requested_quantity: 2, days_since_delivery: 31,
    purchased_quantity: 2, policy_id: 'policy/demo', policy_version: 'v1', return_eligible: false,
    refund_eligible: false, refund_requires_return: true, denial_reasons: ['new_backend_reason'],
    assessment_basis: 'local_scenario_unverified_inputs', authorization_granted: false };
  const api = createApi(async (url, options) => {
    assert.equal(options.method, 'GET');
    assert.deepEqual(Object.fromEntries(new URL(url, 'http://localhost').searchParams), inputs);
    return { ok: true, json: async () => decision };
  });
  assert.deepEqual(await api.eligibility('order', inputs), decision);
});

test('missing inventory rejects rather than inventing zero stock', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; return { ok: false, status: 404, json: async () => ({ detail: 'Inventory item not found' }) }; });
  await assert.rejects(api.item('unknown'), /Inventory item not found/);
  assert.equal(calls, 1);
});

test('validation errors and non-JSON errors are readable', async () => {
  const api = createApi(async () => ({ ok: false, status: 422, json: async () => ({ detail: [{ msg: 'Invalid quantity' }] }) }));
  await assert.rejects(api.listCases(), /Invalid quantity/);
  const unavailable = createApi(async () => ({ ok: false, status: 502, json: async () => { throw new Error(); } }));
  await assert.rejects(unavailable.listCases(), /502/);
});

test('empty case list is preserved and network failure is not hidden', async () => {
  assert.deepEqual(await createApi(async () => ({ ok: true, json: async () => [] })).listCases(), []);
  await assert.rejects(createApi(async () => { throw new Error('Offline'); }).listCases(), /Offline/);
});

test('identity and run snapshots accept Bedrock and scripted labels but reject retired providers', async () => {
  const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
  const instance_id = id(1), case_id = id(2), run_id = id(3);
  const identity = { identity: { user_id: id(4), role: 'ADMIN', customer_id: null },
    can_review: true, can_view_diagnostics: true,
    instance_id, provider: 'bedrock', memory_available: false };
  const snapshot = { instance_id, case_id, run_id, provider: 'bedrock', status: 'RUNNING',
    workflow_id: null, conversation_id: null, trace_id: null, reviewed_status: null,
    created_at: '2026-10-15T00:00:00Z', updated_at: '2026-10-15T00:00:00Z',
    last_sequence: 0, message: null, error: null, review: null, actions_executed: false };
  const api = createApi(async (url) => ({ ok: true, json: async () => url.endsWith('/identity') ? identity : snapshot }));
  for (const provider of ['bedrock', 'local_scripted']) {
    identity.provider = provider;
    snapshot.provider = provider;
    assert.equal((await api.identity()).provider, provider);
    assert.equal((await api.startRun({ instance_id, request_id: id(5), case_id, message: 'Review' })).provider, provider);
  }
  identity.provider = 'unsupported';
  await assert.rejects(api.identity(), /Unexpected identity response/);
  snapshot.provider = 'unsupported';
  await assert.rejects(api.startRun({ instance_id, request_id: id(5), case_id, message: 'Review' }), /invalid run snapshot/);
});

test('investigation control and manual start use closed contracts and idempotency headers', async () => {
  const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
  const caseId = id(1), key = id(2);
  const control = { configured_mode: 'MANUAL', effective_mode: 'MANUAL',
    automatic_capability_available: true, version: 1, updated_by: null,
    updated_at: '2026-10-21T10:00:00Z', counts: { total: 1, pending: 1, running: 0,
      review_required: 0, completed: 0, failed: 0, recovery_required: 0 } };
  const status = { case_id: caseId, job_id: id(3), state: 'PENDING', job_status: 'PENDING',
    workflow_id: null, workflow_status: null, provider: null, submission_mode: 'MANUAL',
    mode_version: 1, submitted_at: '2026-10-21T10:01:00Z', execution_started: false,
    can_start: true, actions_executed: false };
  const calls = [];
  const api = createApi(async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => url.includes('/cases/') ? status : control };
  });
  assert.deepEqual((await api.investigationControl()).counts, {
    total: 1, pending: 1, running: 0, reviewRequired: 0, completed: 0, failed: 0,
    recoveryRequired: 0,
  });
  await api.updateInvestigationControl({ mode: 'AUTO', expectedVersion: 1, idempotencyKey: key });
  assert.deepEqual(await api.caseInvestigation(caseId), {
    caseId, jobId: id(3), state: 'PENDING', jobStatus: 'PENDING', workflowId: null,
    workflowStatus: null, provider: null, submissionMode: 'MANUAL', modeVersion: 1,
    submittedAt: '2026-10-21T10:01:00Z', executionStarted: false, canStart: true,
    message: null, error: null, resolutionDraft: null,
    investigationOutcome: null,
    actionsExecuted: false,
  });
  await api.startCaseInvestigation(caseId, { idempotencyKey: key });
  assert.equal(calls[1].url, '/api/admin/investigation-control');
  assert.equal(calls[1].options.method, 'PATCH');
  assert.equal(calls[1].options.headers['Idempotency-Key'], key);
  assert.deepEqual(JSON.parse(calls[1].options.body), { mode: 'AUTO', expected_version: 1 });
  assert.equal(calls[3].url, `/api/cases/${caseId}/investigation`);
  assert.equal(calls[3].options.method, 'POST');
  assert.equal(calls[3].options.headers['Idempotency-Key'], key);
  assert.equal(calls[3].options.body, undefined);
});

test('investigation API rejects malformed status and mode responses', async () => {
  const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
  const caseId = id(1);
  const invalidControl = { configured_mode: 'AUTO', effective_mode: 'AUTO',
    automatic_capability_available: true, version: 2, updated_by: id(2),
    updated_at: '2026-10-21T10:00:00Z', counts: { total: 1, pending: 1, running: 1,
      review_required: 0, completed: 0, failed: 0, recovery_required: 0 } };
  await assert.rejects(createApi(async () => ({ ok: true, json: async () => invalidControl }))
    .investigationControl(), /invalid investigation control/);
  const invalidStatus = { case_id: caseId, job_id: null, state: 'RUNNING', job_status: null,
    workflow_id: null, workflow_status: null, provider: null, submission_mode: null,
    mode_version: null, submitted_at: null, execution_started: false, can_start: false,
    actions_executed: false };
  await assert.rejects(createApi(async () => ({ ok: true, json: async () => invalidStatus }))
    .caseInvestigation(caseId), /invalid case investigation binding/);
});

test('completed investigation preserves the persisted admin resolution draft', async () => {
  const caseId = '00000000-0000-4000-8000-000000000001';
  const draft = {
    issue_type: 'damaged', recommended_decision: 'REQUEST_EVIDENCE',
    verified_facts: ['case_id:1'], missing_evidence: ['damage evidence'],
    decision_reason: 'Evidence is required before support can decide the case.',
    customer_facing_response: 'Please provide the requested evidence.',
    troubleshooting_steps: [], response_source: 'DETERMINISTIC_FALLBACK',
    citations: [], actions_executed: false,
  };
  const payload = {
    case_id: caseId, job_id: '00000000-0000-4000-8000-000000000002', state: 'COMPLETED',
    job_status: 'PENDING', workflow_id: '00000000-0000-4000-8000-000000000003',
    workflow_status: 'COMPLETED', provider: 'bedrock', submission_mode: 'AUTO', mode_version: 1,
    submitted_at: '2026-10-21T10:01:00Z', execution_started: true, can_start: false,
    message: draft.customer_facing_response, error: null, resolution_draft: draft,
    actions_executed: false,
  };
  const api = createApi(async () => ({ ok: true, json: async () => payload }));
  assert.deepEqual((await api.caseInvestigation(caseId)).resolutionDraft, {
    issueType: 'damaged', recommendedDecision: 'REQUEST_EVIDENCE',
    verifiedFacts: draft.verified_facts, missingEvidence: draft.missing_evidence,
    decisionReason: draft.decision_reason, customerFacingResponse: draft.customer_facing_response,
    troubleshootingSteps: [], responseSource: 'DETERMINISTIC_FALLBACK', citations: [],
  });
});

test('admin resolution sends an explicit human decision without actor or AI execution fields', async () => {
  const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
  const caseId = id(1);
  const decision = { action: 'APPROVE_REPLACEMENT', reason: 'Verified damage from supplied evidence.',
    customer_message: 'Your replacement request has been approved.', admin_user_id: id(2),
    decided_at: '2026-10-19T00:00:00Z' };
  const calls = [];
  const api = createApi(async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => ({ case: { id: caseId, status: 'resolved' }, latest: decision,
      history: [decision] }) };
  });
  const result = await api.resolveCase(caseId, { action: decision.action, reason: decision.reason,
    customerMessage: decision.customer_message, evidenceReviewed: true });
  assert.equal(result.case.status, 'resolved');
  assert.equal(result.latest.adminUserId, id(2));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `/api/cases/${caseId}/resolution`);
  assert.equal(calls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    action: 'APPROVE_REPLACEMENT', reason: decision.reason,
    customer_message: decision.customer_message, evidence_reviewed: true,
  });
});

test('admin draft projection is bounded and never accepts an executed action', async () => {
  const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
  const caseId = id(1);
  const draft = { issue_type: 'delayed_delivery', recommended_decision: 'ESCALATE',
    verified_facts: ['order_id:00000000-0000-4000-8000-000000000002'],
    missing_evidence: ['trusted shipment tracking status'],
    decision_reason: 'Tracking evidence is required before a delivery conclusion.',
    customer_facing_response: 'We are escalating this request for review.',
    troubleshooting_steps: [],
    response_source: 'BEDROCK_VALIDATED', citations: [{
      document_id: 'delivery-policy', chunk_id: 'a'.repeat(64), title: 'Delivery policy',
      version: 'v1', source_uri: 'local-knowledge://delivery-policy',
    }], actions_executed: false };
  const calls = [];
  const api = createApi(async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => draft };
  });
  assert.deepEqual(await api.caseResolutionDraft(caseId), {
    issueType: 'delayed_delivery', recommendedDecision: 'ESCALATE',
    verifiedFacts: draft.verified_facts, missingEvidence: draft.missing_evidence,
    decisionReason: draft.decision_reason, customerFacingResponse: draft.customer_facing_response,
    troubleshootingSteps: [],
    responseSource: draft.response_source, citations: [{ documentId: 'delivery-policy',
      chunkId: 'a'.repeat(64), title: 'Delivery policy', version: 'v1' }],
  });
  assert.equal(calls[0].url, `/api/cases/${caseId}/resolution-draft`);
  assert.equal(calls[0].options.method, 'GET');
  draft.actions_executed = true;
  await assert.rejects(api.caseResolutionDraft(caseId), /invalid resolution draft/);
});

test('admin draft exposes only a validated refund recommendation', async () => {
  const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
  const caseId = id(1);
  const citation = { document_id: 'return-policy', chunk_id: 'b'.repeat(64), title: 'Return policy',
    version: 'v1', source_uri: 'local-knowledge://return-policy' };
  const draft = { issue_type: 'return', recommended_decision: 'REQUEST_EVIDENCE',
    verified_facts: ['case_id:1'], missing_evidence: ['trusted delivery evidence'],
    decision_reason: 'More evidence is required.', customer_facing_response: 'Please provide evidence.',
    troubleshooting_steps: [], response_source: 'DETERMINISTIC_FALLBACK', citations: [citation],
    refund_recommendation: { outcome: 'NEED_MORE_EVIDENCE', sku: 'LAP-1', verified_evidence: [],
      missing_evidence: ['trusted delivery evidence'], policy_id: 'standard-return', policy_version: 'demo-v1',
      decision_reason: 'Trusted delivery evidence is unavailable.', citations: [citation],
      response_source: 'DETERMINISTIC_FALLBACK', authorization_granted: false, actions_executed: false },
    actions_executed: false };
  const api = createApi(async () => ({ ok: true, json: async () => draft }));
  const result = await api.caseResolutionDraft(caseId);
  assert.equal(result.refundRecommendation.outcome, 'NEED_MORE_EVIDENCE');
  assert.equal(result.refundRecommendation.sku, 'LAP-1');
  draft.refund_recommendation.authorization_granted = true;
  await assert.rejects(api.caseResolutionDraft(caseId), /invalid refund recommendation/);
});

test('customer guidance uses an idempotent admin POST and a closed latest projection', async () => {
  const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
  const caseId = id(1), idempotencyKey = id(2);
  const raw = { message: 'Try a known-good cable and tell us whether the indicator remains lit.',
    sent_at: '2026-10-21T10:00:00Z', admin_user_id: id(3), request_fingerprint: 'PRIVATE' };
  const calls = [];
  const api = createApi(async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => raw };
  });

  assert.deepEqual(await api.latestCustomerGuidance(caseId), {
    message: raw.message, sentAt: raw.sent_at,
  });
  assert.deepEqual(await api.sendCustomerGuidance(caseId, {
    message: `  ${raw.message}  `, idempotencyKey,
  }), { message: raw.message, sentAt: raw.sent_at });
  assert.deepEqual(calls.map(({ url }) => url), [
    `/api/cases/${caseId}/customer-communications/latest`,
    `/api/cases/${caseId}/customer-communications`,
  ]);
  assert.equal(calls[1].options.method, 'POST');
  assert.equal(calls[1].options.headers['Idempotency-Key'], idempotencyKey);
  assert.deepEqual(JSON.parse(calls[1].options.body), { message: raw.message });
});

test('customer guidance rejects malformed responses, messages, and retry keys', async () => {
  const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
  const caseId = id(1), key = id(2);
  for (const body of [{}, { message: '', sent_at: '2026-10-21T10:00:00Z' },
    { message: 'Guidance', sent_at: 'invalid' }]) {
    await assert.rejects(createApi(async () => ({ ok: true, json: async () => body }))
      .latestCustomerGuidance(caseId), /invalid customer communication/);
  }
  let calls = 0;
  const api = createApi(async () => { calls++; });
  await assert.rejects(api.sendCustomerGuidance(caseId, { message: ' ', idempotencyKey: key }),
    /customer communication/);
  await assert.rejects(api.sendCustomerGuidance(caseId, { message: 'Guidance', idempotencyKey: 'bad' }),
    /idempotency key/);
  assert.equal(calls, 0);
});

test('customer provisioning uses the authenticated API client and validates returned UUIDs', async () => {
  const customerId = '00000000-0000-4000-8000-000000000001';
  const calls = [];
  const api = createApi(async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => ({ id: customerId, name: 'Aamir Imran' }) };
  }, { token: 'in-memory-admin-token-1234' });

  assert.deepEqual(await api.createCustomer('  Aamir Imran  '), { id: customerId, name: 'Aamir Imran' });
  assert.deepEqual(await api.getCustomer(customerId), { id: customerId, name: 'Aamir Imran' });
  assert.equal(calls[0].url, '/api/customers');
  assert.equal(calls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body), { name: 'Aamir Imran' });
  assert.equal(calls[1].url, `/api/customers/${customerId}`);
  assert.equal(calls[1].options.method, 'GET');
  assert.equal(calls[0].options.credentials, 'omit');
  assert.match(calls[0].options.headers.Authorization, /^Bearer /);
});

test('customer provisioning rejects malformed records and never retries a failed POST', async () => {
  let calls = 0;
  const api = createApi(async () => {
    calls += 1;
    throw Object.assign(new Error('request timed out'), { name: 'TimeoutError' });
  });
  await assert.rejects(api.createCustomer('Bushra'), /timed out/);
  assert.equal(calls, 1);
  await assert.rejects(api.getCustomer('not-a-uuid'), /customer id/);
});
