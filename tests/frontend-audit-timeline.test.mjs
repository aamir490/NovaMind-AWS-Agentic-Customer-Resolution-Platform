import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createApi } from '../frontend/src/api.js';
import { diagnosticsSnapshot } from '../frontend/src/diagnosticsContracts.js';

// Render the real JSX using the existing Vite/React dependencies; no test packages.
const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
let server, AuditTimeline, AuditWorkspace, DiagnosticsWorkspace, DiagnosticsSnapshot;
const integratedViews = {};
before(async () => {
  const { createServer } = await import(pathToFileURL(require.resolve('vite')).href);
  const { default: react } = await import(pathToFileURL(require.resolve('@vitejs/plugin-react')).href);
  server = await createServer({ root: fileURLToPath(new URL('../frontend', import.meta.url)),
    configFile: false, plugins: [react()], server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  AuditTimeline = (await server.ssrLoadModule('/src/AuditTimeline.jsx')).default;
  AuditWorkspace = (await server.ssrLoadModule('/src/AuditWorkspace.jsx')).default;
  const diagnostics = await server.ssrLoadModule('/src/DiagnosticsWorkspace.jsx');
  DiagnosticsWorkspace = diagnostics.default;
  DiagnosticsSnapshot = diagnostics.DiagnosticsSnapshot;
  for (const name of ['CaseReview', 'AIWorkspace', 'Dashboard', 'ReviewQueue', 'ReviewRunResult', 'KnowledgeEvidence']) {
    integratedViews[name] = (await server.ssrLoadModule(`/src/${name}.jsx`)).default;
  }
});
after(async () => { await server?.close(); });

const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const binding = { instance_id: id(1), case_id: id(2), run_id: id(3) };
const event = (sequence, changes = {}) => ({ sequence, timestamp: '2026-10-04T10:00:00Z',
  kind: 'AGENT', tool: null, error: null, proposal_id: null, proposal_status: null, reviewer_user_id: null, ...changes });
async function render(events, { after = 0, hasMore = false } = {}) {
  const api = createApi(async () => ({ ok: true, json: async () => ({ run_id: binding.run_id,
    scope: 'in_memory_workflow_audit', events, next_after: events.at(-1)?.sequence ?? after, has_more: hasMore }) }));
  const page = await api.runAudit(binding, after);
  return renderToStaticMarkup(createElement(AuditTimeline, { events: page.events, after, hasMore: page.has_more }));
}

test('timeline conveys recorded progression with timestamps, kinds and safe metadata', async () => {
  const html = await render([
    event(1, { tool: 'get_case' }),
    event(2, { kind: 'REVIEW_REQUIRED', proposal_id: id(4) }),
    event(3, { kind: 'HUMAN_REVIEW', proposal_id: id(4), proposal_status: 'APPROVED', reviewer_user_id: id(5) }),
    event(4, { kind: 'FAILURE', error: 'WORKFLOW_ERROR' }),
  ]);
  const headings = ['Tool activity recorded', 'Human review requested', 'Approval recorded', 'Workflow failure recorded'];
  assert.deepEqual([...html.matchAll(/<h4[^>]*>(.*?)<\/h4>/g)].map((match) => match[1]), headings);
  for (const text of ['Agent activity', 'Review request', 'Human review', 'Failure', 'get_case', 'APPROVED',
    id(4), id(5), 'WORKFLOW_ERROR', 'Events 1–4', 'does not confirm business action execution']) assert.ok(html.includes(text), text);
  assert.equal((html.match(/<time dateTime="2026-10-04T10:00:00Z"/g) ?? []).length, 4);
  assert.match(html, /<ol[^>]*aria-label="Run audit timeline"[^>]*start="1"[^>]*tabindex="0"/);
  assert.doesNotMatch(html, /<button|<form/);
});

test('sequence remains authoritative when timestamps tie or the clock moves backwards', async () => {
  const html = await render([event(1), event(2), event(3, { timestamp: '2026-10-04T09:00:00Z' })]);
  assert.deepEqual([...html.matchAll(/<li value="(\d+)"/g)].map((match) => Number(match[1])), [1, 2, 3]);
  await assert.rejects(render([event(2), event(1)]), { code: 'INVALID_AUDIT_RESPONSE' });
  await assert.rejects(render([event(1), event(1)]), { code: 'INVALID_AUDIT_RESPONSE' });
});

test('optional metadata is omitted without inventing success or a review outcome', async () => {
  const html = await render([event(1), event(2, { kind: 'HUMAN_REVIEW' }),
    event(3, { kind: 'HUMAN_REVIEW', proposal_status: 'REJECTED' }), event(4, { error: 'TOOL_ERROR', tool: 'get_case' })]);
  for (const text of ['Agent activity recorded', 'Human review recorded', 'Rejection recorded', 'Agent error recorded', 'TOOL_ERROR']) {
    assert.ok(html.includes(text), text);
  }
  assert.doesNotMatch(html, /Approval recorded|Reviewer user ID|Proposal ID|undefined|>null</);
  assert.match(html, /does not confirm tool success/);
  assert.match(html, /does not indicate that the run completed/);
});

test('page ranges and list numbering continue across cursor pages without implying completion', async () => {
  const first = await render(Array.from({ length: 50 }, (_, i) => event(i + 1)), { hasMore: true });
  assert.match(first, /Events 1–50/);
  assert.match(first, /timeline continues on the next page/);
  assert.doesNotMatch(first, /Continued from the previous page/);
  const last = await render([event(51, { kind: 'REVIEW_REQUIRED' })], { after: 50 });
  assert.match(last, /Events 51–51/);
  assert.match(last, /start="51"/);
  assert.match(last, /Continued from the previous page/);
  assert.match(last, /does not indicate that the run completed/);
  assert.doesNotMatch(last, /Event #1 ·/);
});

test('empty initial and later pages render distinct safe states without a timeline', async () => {
  const initial = await render([]);
  const later = await render([], { after: 50 });
  assert.match(initial, /role="status".*No audit records are available for this run/);
  assert.match(later, /role="status".*No further audit records are available/);
  assert.doesNotMatch(initial + later, /<ol|<article|Events /);
});

test('private fields never reach rendered markup and unsafe metadata is rejected', async () => {
  const privateText = 'PRIVATE_SENTINEL_<script>secret</script>';
  const html = await render([event(1, { prompt: privateText, reasoning: privateText, raw_payload: privateText,
    secret: privateText, message: privateText, rationale: privateText })]);
  assert.doesNotMatch(html, /PRIVATE_SENTINEL|<script|secret/);
  for (const field of ['kind', 'tool', 'error', 'proposal_status', 'reviewer_user_id']) {
    await assert.rejects(render([event(1, { [field]: privateText })]), { code: 'INVALID_AUDIT_RESPONSE' });
  }
});

test('workspace still gates signed-out and customer identities and starts with loading', () => {
  const signedOut = renderToStaticMarkup(createElement(AuditWorkspace));
  const customer = renderToStaticMarkup(createElement(AuditWorkspace, { api: {}, identity: { role: 'CUSTOMER' } }));
  const reviewer = renderToStaticMarkup(createElement(AuditWorkspace, { api: {}, identity: { role: 'REVIEWER' } }));
  assert.match(signedOut, /Confirm your identity to open Audit/);
  assert.match(customer, /Audit requires an authorized reviewer or administrator/);
  assert.match(reviewer, /Loading available cases/);
  assert.doesNotMatch(signedOut + customer + reviewer, /class="audit-timeline"/);
});

test('Diagnostics gates identities and renders loading without starting server-rendered requests', () => {
  const signedOut = renderToStaticMarkup(createElement(DiagnosticsWorkspace));
  const customer = renderToStaticMarkup(createElement(DiagnosticsWorkspace, { api: {}, identity: { role: 'CUSTOMER' } }));
  const reviewer = renderToStaticMarkup(createElement(DiagnosticsWorkspace, { api: {}, identity: { role: 'REVIEWER' } }));
  const admin = renderToStaticMarkup(createElement(DiagnosticsWorkspace, { api: {}, identity: { role: 'ADMIN' } }));
  assert.match(signedOut, /Confirm your identity to open Diagnostics/);
  assert.match(customer, /Administrator access required/);
  assert.match(reviewer, /Administrator access required/);
  assert.match(admin, /Loading diagnostics/);
  assert.match(admin, /aria-busy="true"/);
  assert.doesNotMatch(signedOut + customer + reviewer + admin, /Recent operational events/);
});

test('Diagnostics renders known zero, unknown usage, trace identifiers and retention gaps separately', () => {
  const snapshot = diagnosticsSnapshot({ scope: 'process_metadata_only', snapshot_at: '2026-10-05T01:00:00Z',
    events: [{ timestamp: '2026-10-05T00:00:00Z', operation: 'llm', trace_id: id(1), span_id: id(2),
      parent_span_id: null, duration_ms: 8, error: null, provider: 'fake', input_tokens: null, output_tokens: 0,
      raw_payload: 'PRIVATE', prompt: 'PRIVATE', model: 'PRIVATE' }],
    metrics: { llm: { count: 1, errors: 0, duration_ms_total: 8, duration_ms_max: 8, provider_calls: 1,
      input_tokens_known: 0, output_tokens_known: 0, total_tokens_known: 0,
      input_tokens_unknown_calls: 1, output_tokens_unknown_calls: 0, total_tokens_unknown_calls: 1 } },
    evicted_events: 2, omitted_events: 3, logging_failures: 4 });
  const html = renderToStaticMarkup(createElement(DiagnosticsSnapshot, { snapshot }));
  for (const text of ['Trace ID', id(1), 'Span ID', id(2), '8 ms', '0 known · 1 calls unknown',
    'Events evicted from memory', 'Older retained events omitted', 'Logging failures', 'fake']) assert.ok(html.includes(text), text);
  assert.match(html, /<dt>Input tokens<\/dt><dd>Unknown<\/dd>/);
  assert.match(html, /<dt>Output tokens<\/dt><dd>0<\/dd>/);
  assert.doesNotMatch(html, /PRIVATE|<form|<button|Healthy|p95/);
  const empty = renderToStaticMarkup(createElement(DiagnosticsSnapshot, { snapshot: { ...snapshot, metrics: [], events: [] } }));
  assert.match(empty, /No operation metrics have been recorded/);
  assert.match(empty, /No recent operational events are available/);
});

test('Diagnostics renders only closed output schema field and error categories', () => {
  const snapshot = diagnosticsSnapshot({ scope: 'process_metadata_only', snapshot_at: '2026-10-05T01:00:00Z',
    events: [{ timestamp: '2026-10-05T00:00:00Z', operation: 'llm', trace_id: id(1), span_id: id(2),
      parent_span_id: null, duration_ms: 8, error: 'INVALID_RESPONSE', provider: 'bedrock',
      invalid_response_stage: 'output_schema', output_schema_errors: [
        { type: 'dict_type', location: ['decision', 'tool', 'arguments'] }],
      output_schema_errors_truncated: true, prompt: 'PRIVATE' }],
    metrics: {}, evicted_events: 0, omitted_events: 0, logging_failures: 0 });
  const html = renderToStaticMarkup(createElement(DiagnosticsSnapshot, { snapshot }));
  assert.match(html, /decision\.tool\.arguments/);
  assert.match(html, /dict_type/);
  assert.match(html, /Additional issues omitted/);
  assert.doesNotMatch(html, /PRIVATE/);
});

test('Cases, AI Workspace, Reviews, RAG and reviewed results remain renderable with shared frontend imports', () => {
  for (const name of ['CaseReview', 'AIWorkspace', 'ReviewQueue']) {
    const html = renderToStaticMarkup(createElement(integratedViews[name], { active: false }));
    assert.ok(html.length > 0, name);
    assert.doesNotMatch(html, /PRIVATE/);
  }
  const evidence = renderToStaticMarkup(createElement(integratedViews.KnowledgeEvidence, { tracking: { events: [
    { sequence: 1, kind: 'TOOL', tool: 'search_knowledge', evidence: [{ reference: 'a'.repeat(64),
      title: 'Local policy', snippet: 'Local reference excerpt', source: 'local-knowledge://policy', documentId: 'policy', version: 'v1' }] },
  ] } }));
  assert.match(evidence, /Local reference excerpt/);
  const result = renderToStaticMarkup(createElement(integratedViews.ReviewRunResult, { active: true, record: {
    item: { caseId: id(1), runId: id(2), workflowId: id(3), proposalId: id(4), reviewId: id(5) },
    decision: 'APPROVE', phase: 'success', tracking: { events: [], snapshot: { status: 'REVIEWED',
      reviewed_status: 'APPROVED', actions_executed: false, updated_at: '2026-10-05T00:00:00Z', message: null, error: null } },
  } }));
  assert.match(result, /APPROVED/);
  assert.match(result, /No business action executed/);
});

test('Bedrock identity is displayed with its own provider label', () => {
  const identity = { provider: 'bedrock', role: 'ADMIN', instance_id: id(1), memory_available: false };
  const ai = renderToStaticMarkup(createElement(integratedViews.AIWorkspace, { api: {}, identity }));
  const dashboard = renderToStaticMarkup(createElement(integratedViews.Dashboard, { api: {}, identity }));
  assert.match(ai, /Amazon Bedrock/);
  assert.match(dashboard, /Provider: Amazon Bedrock/);
  assert.doesNotMatch(ai + dashboard, /Local scripted/);
});
