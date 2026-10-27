/**
 * Regression tests for three browser-visible bugs fixed together:
 *
 * BUG-1 (frontend/src/api.js): eventNodes was missing 'evaluate_refund'.
 *   Any run with resolution_intent='refund' emits NODE events with
 *   node='evaluate_refund'. The frontend threw INVALID_DIAGNOSTICS_RESPONSE
 *   on those events, killing live tracking for every refund-path run.
 *
 * BUG-2 (frontend/src/diagnosticsContracts.js): operationLabels was missing
 *   'refund_eligibility'. The backend OPERATIONS set includes it and emits
 *   metrics under that key. The frontend threw INVALID_DIAGNOSTICS_RESPONSE
 *   for any diagnostics snapshot that included refund eligibility data.
 *
 * BUG-3 (backend/app/frontend_runtime.py): events() accessed run.events[0]
 *   without an empty-deque guard. A concurrent read in the narrow window
 *   between run creation and the first _publish() call would produce an
 *   unhandled IndexError (HTTP 500). Now raises FrontendError(409,
 *   'NO_EVENTS_YET') for a clean, retriable error response.
 *   (BUG-3 is covered by the Python test file.)
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi } from '../frontend/src/api.js';
import { diagnosticsSnapshot } from '../frontend/src/diagnosticsContracts.js';
import { reviewRequestStatusForDisplay } from '../frontend/src/reviewPresentation.js';

// ─── helpers ────────────────────────────────────────────────────────────────

const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const instance_id = id(1);
const case_id = id(2);
const run_id = id(3);

const nodeEvent = (seq, node, state = 'STARTED') => ({
  run_id, sequence: seq, timestamp: '2026-10-15T00:00:00Z',
  kind: 'NODE', state, node,
  tool: null, ok: null, error: null, proposal_id: null,
  evidence: [], retrieval_method: null,
});

const stateEvent = (seq, state) => ({
  run_id, sequence: seq, timestamp: '2026-10-15T00:00:00Z',
  kind: 'STATE', state, node: null,
  tool: null, ok: null, error: null, proposal_id: null,
  evidence: [], retrieval_method: null,
});

// Build a self-consistent events page: snapshot.last_sequence matches the final event sequence
// so has_more=false and all cursor checks are satisfied.
const eventsPage = (events) => {
  const lastSeq = events.at(-1)?.sequence ?? 1;
  const firstSeq = events[0]?.sequence ?? 1;
  return {
    snapshot: {
      instance_id, case_id, run_id,
      provider: 'local_scripted', status: 'COMPLETED',
      workflow_id: null, conversation_id: null, trace_id: null, reviewed_status: null,
      created_at: '2026-10-15T00:00:00Z', updated_at: '2026-10-15T00:00:00Z',
      last_sequence: lastSeq, message: null, error: null, review: null, actions_executed: false,
    },
    events,
    next_after: lastSeq,
    has_more: false,
    first_available_sequence: firstSeq,
    dropped_events: firstSeq - 1,
    gap: false,
  };
};

const baseMetric = () => ({
  count: 1, errors: 0, duration_ms_total: 5, duration_ms_max: 5,
  provider_calls: 0, input_tokens_known: 0, output_tokens_known: 0,
  total_tokens_known: 0, input_tokens_unknown_calls: 0,
  output_tokens_unknown_calls: 0, total_tokens_unknown_calls: 0,
});

const baseDiagnostics = (metrics = {}) => ({
  scope: 'process_metadata_only',
  snapshot_at: '2026-10-15T00:00:00Z',
  evicted_events: 0, omitted_events: 0, logging_failures: 0,
  events: [],
  metrics,
});

// ─── BUG-1: evaluate_refund node in run events ───────────────────────────────

test('BUG-1 fixed: evaluate_refund node is accepted in run events (was rejected before fix)', async () => {
  const page = eventsPage([nodeEvent(1, 'evaluate_refund', 'STARTED'), nodeEvent(2, 'evaluate_refund', 'COMPLETED')]);
  const api = createApi(async () => ({ ok: true, json: async () => page }));
  // This call must NOT throw — before the fix it threw "invalid run event"
  const result = await api.runEvents({ instance_id, case_id, run_id }, 0);
  assert.equal(result.events.length, 2);
  assert.equal(result.events[0].node, 'evaluate_refund');
  assert.equal(result.events[0].state, 'STARTED');
  assert.equal(result.events[1].node, 'evaluate_refund');
  assert.equal(result.events[1].state, 'COMPLETED');
});

test('BUG-1 fixed: a full refund-path run event sequence is accepted end-to-end', async () => {
  // Mirrors the actual backend node sequence for a refund-intent run
  const events = [
    stateEvent(1, 'RUNNING'),
    nodeEvent(2, 'load_case', 'STARTED'),
    nodeEvent(3, 'load_case', 'COMPLETED'),
    nodeEvent(4, 'evaluate_refund', 'STARTED'),
    nodeEvent(5, 'evaluate_refund', 'COMPLETED'),
    nodeEvent(6, 'prepare_review', 'STARTED'),
    nodeEvent(7, 'prepare_review', 'COMPLETED'),
    nodeEvent(8, 'human_review', 'PAUSED'),
    stateEvent(9, 'REVIEW_REQUIRED'),
  ];
  // Build a self-consistent page manually (eventsPage() sets status: 'COMPLETED';
  // here we need status: 'REVIEW_REQUIRED' to match the run state)
  const page = {
    snapshot: {
      instance_id, case_id, run_id,
      provider: 'local_scripted', status: 'REVIEW_REQUIRED',
      workflow_id: id(10), conversation_id: null, trace_id: null, reviewed_status: null,
      created_at: '2026-10-15T00:00:00Z', updated_at: '2026-10-15T00:00:01Z',
      last_sequence: 9, message: null, error: null,
      review: { workflow_id: id(10), case_id, proposal_id: id(11), review_id: id(12),
        action: 'refund', rationale: 'Server-side evidence requires review.', status: 'PENDING_REVIEW' },
      actions_executed: false,
    },
    events,
    next_after: 9,
    has_more: false,
    first_available_sequence: 1,
    dropped_events: 0,
    gap: false,
  };
  const api = createApi(async () => ({ ok: true, json: async () => page }));
  const result = await api.runEvents({ instance_id, case_id, run_id }, 0);
  assert.equal(result.events.length, 9);
  const refundNodes = result.events.filter((e) => e.node === 'evaluate_refund');
  assert.equal(refundNodes.length, 2);
});

test('BUG-1 regression: all other existing node names remain accepted', async () => {
  const allNodes = [
    'load_case', 'evaluate_refund', 'reason', 'execute_tool', 'record_result',
    'finalize', 'fail_safely', 'prepare_review', 'human_review',
  ];
  for (const [i, node] of allNodes.entries()) {
    const page = eventsPage([nodeEvent(1, node)]);
    const api = createApi(async () => ({ ok: true, json: async () => page }));
    const result = await api.runEvents({ instance_id, case_id, run_id }, 0);
    assert.equal(result.events[0].node, node, `Node at index ${i} (${node}) was rejected`);
  }
});

test('refund investigation is accepted as a distinct non-refund review action', async () => {
  const page = eventsPage([stateEvent(1, 'REVIEW_REQUIRED')]);
  page.snapshot.status = 'REVIEW_REQUIRED';
  page.snapshot.workflow_id = id(10);
  page.snapshot.review = { workflow_id: id(10), case_id, proposal_id: id(11), review_id: id(12),
    action: 'refund_investigation', rationale: 'Evidence is missing; investigate only.',
    status: 'PENDING_REVIEW' };
  const api = createApi(async () => ({ ok: true, json: async () => page }));
  const result = await api.runEvents({ instance_id, case_id, run_id }, 0);
  assert.equal(result.snapshot.review.action, 'refund_investigation');
});

test('BUG-1 regression: an unknown node name is still rejected', async () => {
  const page = eventsPage([nodeEvent(1, 'unknown_future_node')]);
  const api = createApi(async () => ({ ok: true, json: async () => page }));
  await assert.rejects(
    api.runEvents({ instance_id, case_id, run_id }, 0),
    /invalid run event/i,
  );
});

test('BUG-1 regression: null node (STATE events) remains accepted', async () => {
  const page = eventsPage([stateEvent(1, 'RUNNING'), stateEvent(2, 'COMPLETED')]);
  const api = createApi(async () => ({ ok: true, json: async () => page }));
  const result = await api.runEvents({ instance_id, case_id, run_id }, 0);
  assert.equal(result.events[0].node, null);
  assert.equal(result.events[1].node, null);
});

// ─── BUG-2: refund_eligibility in diagnostics metrics ────────────────────────

test('BUG-2 fixed: refund_eligibility operation is accepted in diagnostics metrics (was rejected before fix)', () => {
  const payload = baseDiagnostics({ refund_eligibility: baseMetric() });
  // Must NOT throw — before the fix this threw INVALID_DIAGNOSTICS_RESPONSE
  const result = diagnosticsSnapshot(payload);
  assert.equal(result.metrics.length, 1);
  assert.equal(result.metrics[0].operation, 'refund_eligibility');
  assert.equal(result.metrics[0].count, 1);
});

test('BUG-2 regression: live refund eligibility tool telemetry is accepted', () => {
  const payload = baseDiagnostics({
    refund_eligibility: baseMetric(),
    tool: baseMetric(),
  });
  payload.events = [{
    timestamp: '2026-10-17T00:00:00Z', operation: 'tool',
    trace_id: id(20), span_id: id(21), parent_span_id: id(22),
    duration_ms: 1, error: null, tool: 'assess_refund_eligibility',
  }];

  const result = diagnosticsSnapshot(payload);

  assert.equal(result.events[0].tool, 'assess_refund_eligibility');
});

test('BUG-2 fixed: refund_eligibility and eligibility can both appear together', () => {
  const payload = baseDiagnostics({
    eligibility: baseMetric(),
    refund_eligibility: { ...baseMetric(), count: 3, errors: 1 },
  });
  const result = diagnosticsSnapshot(payload);
  assert.equal(result.metrics.length, 2);
  const names = result.metrics.map((m) => m.operation).sort();
  assert.deepEqual(names, ['eligibility', 'refund_eligibility']);
});

test('BUG-2 regression: all other known operations remain accepted', () => {
  const knownOps = ['http', 'agent.loop', 'agent.graph', 'tool', 'llm',
    'hitl.start', 'hitl.resume', 'proposal.create', 'proposal.review',
    'eligibility', 'refund_eligibility'];
  for (const op of knownOps) {
    const payload = baseDiagnostics({ [op]: baseMetric() });
    const result = diagnosticsSnapshot(payload);
    assert.equal(result.metrics[0].operation, op, `Operation '${op}' was rejected`);
  }
});

test('BUG-2 regression: unknown operation names are still rejected', () => {
  for (const op of ['PRIVATE', 'gemini', 'agent_loop', 'refundEligibility', '__proto__']) {
    const payload = baseDiagnostics({ [op]: baseMetric() });
    assert.throws(
      () => diagnosticsSnapshot(payload),
      { code: 'INVALID_DIAGNOSTICS_RESPONSE' },
      `Operation '${op}' should have been rejected`,
    );
  }
});

test('BUG-2 regression: empty metrics and events snapshot still accepted', () => {
  const result = diagnosticsSnapshot(baseDiagnostics({}));
  assert.deepEqual(result.metrics, []);
  assert.deepEqual(result.events, []);
});

test('review request status is shown only while the workflow awaits a decision', () => {
  const review = { status: 'PENDING_REVIEW' };
  assert.equal(reviewRequestStatusForDisplay(review, null), 'PENDING_REVIEW');
  assert.equal(reviewRequestStatusForDisplay(review, 'APPROVED'), null);
  assert.equal(reviewRequestStatusForDisplay(review, 'REJECTED'), null);
  assert.equal(reviewRequestStatusForDisplay(null, null), null);
});
