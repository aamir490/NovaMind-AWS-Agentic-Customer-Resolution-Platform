import { auditTools, auditErrors } from './auditContracts.js';

export const operationLabels = Object.freeze({ http: 'HTTP requests', 'agent.loop': 'Agent loop',
  'agent.graph': 'Agent graph', tool: 'Tools', llm: 'Model requests', 'hitl.start': 'Workflow starts',
  'hitl.resume': 'Workflow resumes', 'proposal.create': 'Proposal creation',
  'proposal.review': 'Human reviews', eligibility: 'Eligibility assessments',
  refund_eligibility: 'Refund eligibility assessments' });
const operation = (value) => typeof value === 'string' && Object.hasOwn(operationLabels, value);
const count = (value) => Number.isSafeInteger(value) && value >= 0;
const duration = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER;
const uuid = (value) => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const timestamp = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(value) && Number.isFinite(Date.parse(value));
const oneOf = (values) => (value) => values.includes(value);
const schemaErrorTypes = oneOf(['missing', 'extra_forbidden', 'union_tag_invalid', 'union_tag_not_found',
  'literal_error', 'string_type', 'int_type', 'float_type', 'bool_type', 'dict_type', 'list_type',
  'tuple_type', 'model_type', 'model_attributes_type', 'json_type', 'enum', 'uuid_type', 'uuid_parsing',
  'string_too_short', 'string_too_long', 'string_pattern_mismatch', 'greater_than', 'greater_than_equal',
  'less_than', 'less_than_equal', 'too_short', 'too_long', 'finite_number', 'value_error', 'assertion_error', 'other']);
const schemaLocation = oneOf(['decision', 'kind', 'name', 'arguments', 'tool', 'final',
  '<field>', '<index>', '<unknown>', '<truncated>']);
const schemaErrors = (value) => Array.isArray(value) && value.length <= 8 && value.every((item) =>
  item && typeof item === 'object' && !Array.isArray(item)
  && Object.keys(item).length === 2 && Object.hasOwn(item, 'type') && Object.hasOwn(item, 'location')
  && schemaErrorTypes(item.type) && Array.isArray(item.location) && item.location.length <= 8
  && item.location.every(schemaLocation));
const optionalFields = {
  workflow_id: uuid, case_id: uuid, proposal_id: uuid, reviewer_user_id: uuid,
  tool: oneOf([...auditTools, 'unknown']), status: oneOf(['INFORMATIONAL', 'HUMAN_REVIEW_REQUIRED', 'FAILED',
    'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED', 'PENDING_REVIEW', 'APPROVED', 'REJECTED']),
  provider: oneOf(['bedrock', 'fake', 'other']),
  invalid_response_stage: oneOf(['provider_envelope', 'json_guardrail', 'output_schema']),
  invalid_response_category: oneOf(['bedrock_empty_output', 'bedrock_nonempty_json_rejected']),
  output_schema_errors_truncated: (value) => typeof value === 'boolean',
  method: oneOf(['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']),
  status_code: (value) => count(value) && value >= 100 && value <= 599,
  provider_status_code: (value) => count(value) && value >= 100 && value <= 599,
  input_tokens: (value) => count(value) && value <= 1e9,
  output_tokens: (value) => count(value) && value <= 1e9,
  total_tokens: (value) => count(value) && value <= 1e9,
  output_token_limit: (value) => count(value) && value <= 1e9,
  provider_called: (value) => typeof value === 'boolean',
  reviewer_authenticated: (value) => typeof value === 'boolean',
};
const metricCounts = ['count', 'errors', 'provider_calls', 'input_tokens_known', 'output_tokens_known',
  'total_tokens_known', 'input_tokens_unknown_calls', 'output_tokens_unknown_calls', 'total_tokens_unknown_calls'];
function check(valid) {
  if (!valid) {
    const error = new Error('Diagnostics metadata could not be verified.');
    error.code = 'INVALID_DIAGNOSTICS_RESPONSE';
    throw error;
  }
}

export function diagnosticsSnapshot(payload) {
  check(payload?.scope === 'process_metadata_only' && timestamp(payload.snapshot_at)
    && Array.isArray(payload.events) && payload.events.length <= 50
    && payload.metrics && typeof payload.metrics === 'object' && !Array.isArray(payload.metrics)
    && count(payload.evicted_events) && count(payload.omitted_events) && count(payload.logging_failures));
  const metrics = Object.entries(payload.metrics).map(([name, metric]) => {
    check(operation(name) && metric && metricCounts.every((key) => count(metric[key]))
      && duration(metric.duration_ms_total) && duration(metric.duration_ms_max)
      && metric.errors <= metric.count && metric.provider_calls <= metric.count
      && metric.duration_ms_max <= metric.duration_ms_total
      && ['input', 'output', 'total'].every((part) => metric[`${part}_tokens_unknown_calls`] <= metric.provider_calls));
    return { operation: name, ...Object.fromEntries(metricCounts.map((key) => [key, metric[key]])),
      duration_ms_total: metric.duration_ms_total, duration_ms_max: metric.duration_ms_max };
  });
  const events = payload.events.map((event) => {
    check(event && operation(event.operation) && timestamp(event.timestamp) && uuid(event.trace_id)
      && uuid(event.span_id) && (event.parent_span_id === null || uuid(event.parent_span_id))
      && duration(event.duration_ms) && (event.error === null || [...auditErrors, 'ERROR'].includes(event.error)));
    const projected = { operation: event.operation, timestamp: event.timestamp, trace_id: event.trace_id,
      span_id: event.span_id, parent_span_id: event.parent_span_id, duration_ms: event.duration_ms, error: event.error };
    for (const [key, validate] of Object.entries(optionalFields)) {
      const value = event[key] ?? null;
      check(value === null || validate(value));
      projected[key] = value;
    }
    const errors = event.output_schema_errors ?? null;
    check(errors === null || schemaErrors(errors));
    projected.output_schema_errors = errors?.map((item) => ({ type: item.type, location: [...item.location] })) ?? null;
    // No model text, headers, credentials or generic payloads.
    return projected;
  });
  return { snapshot_at: payload.snapshot_at, metrics, events, evicted_events: payload.evicted_events,
    omitted_events: payload.omitted_events, logging_failures: payload.logging_failures };
}

const workflowStatuses = ['RUNNING', 'RESUMING', 'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED'];
const safeFailure = oneOf(['TIMEOUT', 'UNAVAILABLE', 'REFUSED', 'INCOMPLETE', 'INVALID_RESPONSE', 'INPUT_LIMIT',
  'TOOL_ERROR', 'LLM_ERROR', 'STEP_LIMIT', 'GRAPH_ERROR', 'WORKFLOW_ERROR']);

export function durableInvestigationPage(payload) {
  check(payload?.scope === 'durable_workflow_metadata' && Array.isArray(payload.items)
    && payload.items.length <= 100 && typeof payload.has_more === 'boolean'
    && (payload.next_offset === null || count(payload.next_offset)));
  const items = payload.items.map((item) => {
    check(item && uuid(item.case_id) && uuid(item.workflow_id) && uuid(item.run_id)
      && workflowStatuses.includes(item.workflow_status) && ['local_scripted', 'bedrock'].includes(item.provider)
      && timestamp(item.created_at) && timestamp(item.updated_at)
      && (item.submission_mode === null || ['MANUAL', 'AUTO'].includes(item.submission_mode))
      && (item.mode_version === null || (count(item.mode_version) && item.mode_version >= 1))
      && (item.failure_category === null || safeFailure(item.failure_category))
      && (item.failure_stage === null || ['AGENT', 'WORKFLOW'].includes(item.failure_stage))
      && (item.error_source === null || ['workflow_metadata', 'checkpoint', 'projection_fallback'].includes(item.error_source))
      && (item.response_source === null || ['BEDROCK_VALIDATED', 'DETERMINISTIC_FALLBACK'].includes(item.response_source))
      && item.actions_executed === false);
    return { ...item };
  });
  return { items, next_offset: payload.next_offset, has_more: payload.has_more };
}

export function diagnosticsEventPage(payload, category) {
  check(payload?.scope === 'process_metadata_only' && payload.category === category
    && Array.isArray(payload.events) && payload.events.length <= 50
    && typeof payload.has_more === 'boolean'
    && (payload.next_offset === null || count(payload.next_offset)));
  const snapshot = diagnosticsSnapshot({
    scope: 'process_metadata_only', snapshot_at: new Date().toISOString(), events: payload.events,
    metrics: {}, evicted_events: 0, omitted_events: 0, logging_failures: 0,
  });
  return { events: snapshot.events, next_offset: payload.next_offset, has_more: payload.has_more, category };
}
