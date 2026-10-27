import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi } from '../frontend/src/api.js';

const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const binding = { instance_id: id(1), case_id: id(2), run_id: id(3) };
const timestamp = '2026-10-04T10:00:00Z';
const event = (sequence = 1) => ({ sequence, timestamp, kind: 'AGENT', proposal_id: null,
  reviewer_user_id: null, proposal_status: null, tool: 'get_case', error: null });
const page = (events = [event()]) => ({ run_id: binding.run_id, scope: 'in_memory_workflow_audit',
  events, next_after: events.at(-1)?.sequence ?? 0, has_more: false });
const run = () => ({ ...binding, workflow_id: id(4), status: 'COMPLETED', created_at: timestamp,
  updated_at: timestamp, provider: 'local_scripted', actions_executed: false,
  message: 'PRIVATE', review: { rationale: 'PRIVATE' }, error: 'PRIVATE' });
const client = (payload) => createApi(async () => ({ ok: true, json: async () => payload }));

test('Audit uses same-origin authenticated GETs and drops non-metadata fields', async () => {
  const calls = [];
  const values = [[{ id: binding.case_id, subject: 'PRIVATE', description: 'PRIVATE' }],
    { items: [run()], next_offset: null, scope: 'case_runs_in_this_process' },
    { ...page(), raw_payload: 'PRIVATE', events: [{ ...event(), prompt: 'PRIVATE', reasoning: 'PRIVATE', secret: 'PRIVATE' }] }];
  const api = createApi(async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => values.shift() };
  }, { token: 'test-token-only' });
  const results = [await api.auditCases(), await api.auditRuns(binding), await api.runAudit(binding)];
  assert.equal(JSON.stringify(results).includes('PRIVATE'), false);
  assert.deepEqual(calls.map((call) => call.url), ['/api/cases',
    `/api/cases/${binding.case_id}/runs?offset=0&limit=50`, `/api/runs/${binding.run_id}/audit?after=0&limit=50`]);
  for (const { url, options } of calls) {
    assert.equal(options.method, 'GET');
    assert.equal(options.headers.Authorization, 'Bearer test-token-only');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.credentials, 'omit');
    assert.equal(options.redirect, 'error');
    assert.equal(options.body, undefined);
    assert.equal(url.includes('test-token-only'), false);
  }
});

test('case and run lists reject mismatches, duplicate IDs and invalid offsets', async () => {
  for (const payload of [null, [{}], [{ id: '../invalid' }], [{ id: id(2) }, { id: id(2) }]]) {
    await assert.rejects(client(payload).auditCases(), { code: 'INVALID_AUDIT_RESPONSE' });
  }
  const valid = { scope: 'case_runs_in_this_process', items: [run()], next_offset: null };
  assert.equal((await client({ ...valid, items: [{ ...run(), provider: 'local_scripted' }] }).auditRuns(binding)).items.length, 1);
  assert.equal((await client({ ...valid, items: [{ ...run(), provider: 'bedrock' }] }).auditRuns(binding)).items.length, 1);
  assert.equal((await client({ ...valid, scope: 'case_runs_with_durable_metadata' }).auditRuns(binding)).items.length, 1);
  for (const change of [{ instance_id: id(9) }, { case_id: id(9) }, { run_id: 'bad' },
    { workflow_id: 'PRIVATE' }, { status: 'PRIVATE' }, { provider: 'unsupported' }, { provider: 'gemini' },
    { actions_executed: true }, { updated_at: 'PRIVATE' }]) {
    await assert.rejects(client({ ...valid, items: [{ ...run(), ...change }] }).auditRuns(binding), { code: 'INVALID_AUDIT_RESPONSE' });
  }
  for (const change of [{ scope: 'other' }, { items: [run(), run()] }, { next_offset: 0 }, { next_offset: 50 }]) {
    await assert.rejects(client({ ...valid, ...change }).auditRuns(binding), { code: 'INVALID_AUDIT_RESPONSE' });
  }
});

test('audit rejects unsafe vocabulary, invalid fields, wrong run and broken cursors', async () => {
  assert.equal((await client({ ...page(), scope: 'durable_workflow_audit' }).runAudit(binding)).events.length, 1);
  for (const change of [{ kind: 'PRIVATE' }, { tool: 'PRIVATE' }, { error: 'PRIVATE' }, { proposal_status: 'PRIVATE' },
    { proposal_id: 'PRIVATE' }, { reviewer_user_id: 'PRIVATE' }, { timestamp: 'PRIVATE' },
    { sequence: 0 }, { sequence: 2 }, { error: undefined }]) {
    await assert.rejects(client(page([{ ...event(), ...change }])).runAudit(binding), { code: 'INVALID_AUDIT_RESPONSE' });
  }
  for (const change of [{ run_id: id(9) }, { scope: 'other' }, { next_after: 0 }, { next_after: 2 },
    { has_more: 'false' }, { has_more: true }, { events: [event(), event()] }, { events: null }]) {
    await assert.rejects(client({ ...page(), ...change }).runAudit(binding), { code: 'INVALID_AUDIT_RESPONSE' });
  }
});

test('empty responses stay empty and audit pagination preserves backend sequence', async () => {
  assert.deepEqual(await client([]).auditCases(), []);
  assert.deepEqual(await client({ scope: 'case_runs_in_this_process', items: [], next_offset: null }).auditRuns(binding),
    { items: [], next_offset: null });
  assert.deepEqual(await client({ scope: 'case_runs_with_durable_metadata', items: [], next_offset: null }).auditRuns(binding),
    { items: [], next_offset: null });
  assert.deepEqual(await client(page([])).runAudit(binding), { events: [], next_after: 0, has_more: false });
  const events = Array.from({ length: 50 }, (_, index) => event(index + 1));
  const first = await client({ ...page(events), has_more: true }).runAudit(binding);
  assert.equal(first.next_after, 50);
  assert.equal(first.has_more, true);
  const last = { ...event(51), kind: 'HUMAN_REVIEW', tool: null, reviewer_user_id: id(5),
    proposal_id: id(6), proposal_status: 'APPROVED' };
  assert.deepEqual((await client(page([last])).runAudit(binding, first.next_after)).events, [last]);
  await assert.rejects(client(page([event(52)])).runAudit(binding, 50), { code: 'INVALID_AUDIT_RESPONSE' });
});

test('invalid inputs never reach the transport', async () => {
  let calls = 0;
  const api = createApi(async () => { calls++; throw new Error('Unexpected request'); });
  for (const after of [-1, 1.5, 1000001, '0&limit=999']) {
    await assert.rejects(api.runAudit(binding, after), { code: 'INVALID_AUDIT_RESPONSE' });
    await assert.rejects(api.auditRuns(binding, after), { code: 'INVALID_AUDIT_RESPONSE' });
  }
  await assert.rejects(api.runAudit({ ...binding, run_id: undefined }), { code: 'INVALID_AUDIT_RESPONSE' });
  await assert.rejects(api.auditRuns({ ...binding, case_id: '../other' }), { code: 'INVALID_AUDIT_RESPONSE' });
  assert.equal(calls, 0);
});

test('authorization failures, pending and unavailable responses remain errors without retries', async () => {
  for (const [status, code] of [[401, 'UNAUTHENTICATED'], [403, 'FORBIDDEN'], [404, 'NOT_FOUND'],
    [409, 'AUDIT_PENDING_USE_EVENTS'], [422, 'INVALID_INPUT'], [503, 'AUDIT_UNAVAILABLE']]) {
    let calls = 0;
    let expired = 0;
    const api = createApi(async () => {
      calls++;
      return { ok: false, status, json: async () => ({ detail: code }) };
    }, { onUnauthorized: () => expired++ });
    await assert.rejects(api.runAudit(binding), { status, code });
    assert.equal(calls, 1);
    assert.equal(expired, status === 401 ? 1 : 0);
  }
});

test('selection cancellation and session disposal abort reads', async () => {
  const signals = [];
  const api = createApi(async (_url, options) => {
    signals.push(options.signal);
    return { ok: true, json: async () => page() };
  });
  const controller = new AbortController();
  await api.runAudit(binding, 0, { signal: controller.signal });
  controller.abort();
  assert.equal(signals[0].aborted, true);
  await api.runAudit(binding);
  api.dispose();
  assert.equal(signals[1].aborted, true);
  await assert.rejects(api.runAudit(binding), /session has ended/);
  assert.equal(signals.length, 2);
});
