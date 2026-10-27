import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi } from '../frontend/src/api.js';

const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
export const diagnosticsFixture = () => ({ scope: 'process_metadata_only', snapshot_at: '2026-10-05T01:00:00Z',
  evicted_events: 2, omitted_events: 3, logging_failures: 0,
  metrics: { llm: { count: 2, errors: 1, duration_ms_total: 12, duration_ms_max: 8, provider_calls: 2,
    input_tokens_known: 8, output_tokens_known: 0, total_tokens_known: 8,
    input_tokens_unknown_calls: 1, output_tokens_unknown_calls: 1, total_tokens_unknown_calls: 1 } },
  events: [{ timestamp: '2026-10-05T00:59:00Z', operation: 'llm', trace_id: id(1), span_id: id(2), parent_span_id: null,
    duration_ms: 8, error: 'INVALID_RESPONSE', provider: 'fake', provider_called: true,
    input_tokens: null, output_tokens: 0, total_tokens: null, invalid_response_stage: 'output_schema' }] });
const client = (value) => createApi(async () => ({ ok: true, json: async () => value }));

test('diagnostics uses authenticated bounded same-origin GET and a closed display projection', async () => {
  const raw = diagnosticsFixture();
  raw.secret = 'PRIVATE'; raw.metrics.llm.payload = 'PRIVATE';
  Object.assign(raw.events[0], { prompt: 'PRIVATE', reasoning: 'PRIVATE', credentials: 'PRIVATE',
    model: 'PRIVATE', raw_payload: 'PRIVATE',
    output_schema_errors: [{ type: 'dict_type', location: ['decision', 'tool', 'arguments'] }],
    output_schema_errors_truncated: false });
  const api = createApi(async (url, options) => {
    assert.equal(url, '/api/diagnostics?limit=50');
    assert.equal(options.method, 'GET'); assert.equal(options.body, undefined);
    assert.equal(options.headers.Authorization, 'Bearer local-test-only');
    assert.equal(options.cache, 'no-store'); assert.equal(options.credentials, 'omit'); assert.equal(options.redirect, 'error');
    return { ok: true, json: async () => raw };
  }, { token: 'local-test-only' });
  const value = await api.diagnostics();
  assert.equal(JSON.stringify(value).includes('PRIVATE'), false);
  assert.equal(value.events[0].input_tokens, null);
  assert.equal(value.events[0].output_tokens, 0);
  assert.equal(value.events[0].invalid_response_category, null);
  assert.deepEqual(value.events[0].output_schema_errors,
    [{ type: 'dict_type', location: ['decision', 'tool', 'arguments'] }]);
  assert.equal(value.events[0].output_schema_errors_truncated, false);
  assert.equal(value.metrics[0].input_tokens_unknown_calls, 1);
});

test('diagnostics accepts empty records and safe fallback vocabularies', async () => {
  const raw = diagnosticsFixture();
  raw.metrics = {}; raw.events = [];
  assert.deepEqual((await client(raw).diagnostics()).metrics, []);
  raw.events = [{ ...diagnosticsFixture().events[0], error: 'ERROR', tool: 'unknown' }];
  assert.equal((await client(raw).diagnostics()).events[0].error, 'ERROR');
  for (const provider of ['bedrock', 'fake', 'other', null]) {
    raw.events[0].provider = provider;
    assert.equal((await client(raw).diagnostics()).events[0].provider, provider);
  }
  for (const category of ['bedrock_empty_output', 'bedrock_nonempty_json_rejected']) {
    raw.events[0].invalid_response_category = category;
    assert.equal((await client(raw).diagnostics()).events[0].invalid_response_category, category);
  }
  assert.equal((await client(raw).diagnostics()).events[0].output_schema_errors, null);
});

test('diagnostics rejects unsafe text and invalid values rather than rendering payloads', async () => {
  for (const change of [{ operation: '__proto__' }, { operation: 'PRIVATE' }, { error: 'PRIVATE' },
    { provider: 'PRIVATE' }, { provider: 'gemini' }, { tool: 'PRIVATE' }, { status: 'PRIVATE' }, { method: 'PRIVATE' },
    { trace_id: 'PRIVATE' }, { reviewer_user_id: 'PRIVATE' }, { timestamp: 'PRIVATE' },
    { duration_ms: -1 }, { duration_ms: Infinity }, { input_tokens: -1 }, { input_tokens: 1e9 + 1 },
    { provider_called: 'true' }, { provider_status_code: 600 }, { invalid_response_stage: 'PRIVATE' },
    { invalid_response_category: 'PRIVATE' }, { output_schema_errors_truncated: 'true' }]) {
    const raw = diagnosticsFixture(); Object.assign(raw.events[0], change);
    await assert.rejects(client(raw).diagnostics(), { code: 'INVALID_DIAGNOSTICS_RESPONSE' });
  }
  for (const errors of [
    [{ type: 'PRIVATE', location: ['decision'] }],
    [{ type: 'dict_type', location: ['PRIVATE'] }],
    [{ type: 'dict_type', location: ['decision'], input: 'PRIVATE' }],
    [{ type: 'dict_type', location: Array(9).fill('decision') }],
    Array(9).fill({ type: 'dict_type', location: ['decision'] }),
    'PRIVATE',
  ]) {
    const raw = diagnosticsFixture(); raw.events[0].output_schema_errors = errors;
    await assert.rejects(client(raw).diagnostics(), { code: 'INVALID_DIAGNOSTICS_RESPONSE' });
  }
  for (const change of [{ errors: 3 }, { provider_calls: 3 }, { input_tokens_unknown_calls: 3 },
    { count: -1 }, { total_tokens_known: null }, { duration_ms_max: 13 }, { duration_ms_total: NaN }]) {
    const raw = diagnosticsFixture(); Object.assign(raw.metrics.llm, change);
    await assert.rejects(client(raw).diagnostics(), { code: 'INVALID_DIAGNOSTICS_RESPONSE' });
  }
  for (const change of [{ metrics: { PRIVATE: diagnosticsFixture().metrics.llm } }, { metrics: [] },
    { scope: 'PRIVATE' }, { snapshot_at: 'PRIVATE' }, { evicted_events: -1 },
    { events: Array.from({ length: 51 }, () => diagnosticsFixture().events[0]) }]) {
    await assert.rejects(client({ ...diagnosticsFixture(), ...change }).diagnostics(), { code: 'INVALID_DIAGNOSTICS_RESPONSE' });
  }
});

test('diagnostics does not retry failures and retains session expiry behavior', async () => {
  for (const status of [401, 403, 404, 503]) {
    let calls = 0, expired = 0;
    const api = createApi(async () => { calls++; return { ok: false, status, json: async () => ({ detail: 'PRIVATE' }) }; },
      { onUnauthorized: () => expired++ });
    await assert.rejects(api.diagnostics(), { status });
    assert.equal(calls, 1); assert.equal(expired, status === 401 ? 1 : 0);
  }
});

test('diagnostics requests are cancelled on navigation and session disposal', async () => {
  const signals = [];
  const api = createApi(async (_url, options) => {
    signals.push(options.signal); return { ok: true, json: async () => diagnosticsFixture() };
  });
  const controller = new AbortController();
  await api.diagnostics({ signal: controller.signal }); controller.abort();
  assert.equal(signals[0].aborted, true);
  await api.diagnostics(); api.dispose(); assert.equal(signals[1].aborted, true);
  await assert.rejects(api.diagnostics(), /session has ended/);
});
