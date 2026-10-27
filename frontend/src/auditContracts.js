// Closed metadata projections for Audit. Free-form strings are never displayed.
const uuid = (value) => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const optionalUuid = (value) => value === null || uuid(value);
const count = (value) => Number.isSafeInteger(value) && value >= 0 && value <= 1000000;
const timestamp = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(value) && Number.isFinite(Date.parse(value));
const statuses = ['RUNNING', 'RESUMING', 'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED'];
const kinds = ['AGENT', 'REVIEW_REQUIRED', 'HUMAN_REVIEW', 'FAILURE'];
const tools = ['get_customer', 'get_order', 'get_case', 'get_inventory', 'get_policy',
  'assess_eligibility', 'assess_refund_eligibility', 'create_resolution_proposal',
  'get_proposal_status', 'search_knowledge'];
// Matches the backend's safe_error vocabulary, with no fallback to raw error text.
const errors = ['UNAUTHENTICATED', 'FORBIDDEN', 'UNKNOWN_TOOL', 'INVALID_INPUT', 'NOT_FOUND', 'CONFLICT',
  'KNOWLEDGE_UNAVAILABLE', 'UNSAFE_CONTENT', 'PAYLOAD_LIMIT', 'INVALID_OUTPUT', 'TIMEOUT', 'UNAVAILABLE',
  'REFUSED', 'INCOMPLETE', 'INVALID_RESPONSE', 'INPUT_LIMIT', 'TOOL_ERROR', 'LLM_ERROR', 'STEP_LIMIT',
  'PROPOSAL_LIMIT', 'CASE_MISMATCH', 'INVALID_PROPOSAL_STATUS', 'GRAPH_LIMIT', 'GRAPH_ERROR', 'WORKFLOW_ERROR',
  'UNKNOWN_WORKFLOW', 'NOT_PAUSED', 'REVIEW_MISMATCH', 'ALREADY_REVIEWED', 'REVIEW_CONFLICT',
  'CONVERSATION_WRITE_FAILED', 'CONVERSATION_UNAVAILABLE', 'CONVERSATION_NOT_FOUND', 'CONVERSATION_CASE_MISMATCH',
  'REPEATED_TOOL_CALL', 'EARLY_DETERMINISTIC_FALLBACK'];

function check(valid) {
  if (!valid) {
    const error = new Error('Audit metadata could not be verified.');
    error.code = 'INVALID_AUDIT_RESPONSE';
    throw error;
  }
}

export function auditBinding(binding, cursor = 0, requireRun = false) {
  check(uuid(binding?.instance_id) && uuid(binding.case_id)
    && (requireRun ? uuid(binding.run_id) : binding.run_id === undefined || uuid(binding.run_id)) && count(cursor));
}

export { tools as auditTools, errors as auditErrors };

export function auditCases(payload) {
  check(Array.isArray(payload));
  const seen = new Set();
  return payload.map((item) => {
    check(uuid(item?.id) && !seen.has(item.id));
    seen.add(item.id);
    return { id: item.id };
  });
}

export function auditRuns(page, binding, offset) {
  check(['case_runs_in_this_process', 'case_runs_with_durable_metadata'].includes(page?.scope)
    && Array.isArray(page.items) && page.items.length <= 50
    && (page.next_offset === null || (count(page.next_offset) && page.next_offset === offset + 50 && page.items.length === 50)));
  const seen = new Set();
  const items = page.items.map((item) => {
    check(item?.instance_id === binding.instance_id && item.case_id === binding.case_id
      && uuid(item.run_id) && !seen.has(item.run_id) && optionalUuid(item.workflow_id)
      && statuses.includes(item.status) && timestamp(item.created_at) && timestamp(item.updated_at)
      && ['local_scripted', 'bedrock'].includes(item.provider) && item.actions_executed === false);
    seen.add(item.run_id);
    return { run_id: item.run_id, workflow_id: item.workflow_id, status: item.status,
      created_at: item.created_at, updated_at: item.updated_at };
  });
  return { items, next_offset: page.next_offset };
}

export function auditPage(page, binding, after) {
  check(page?.run_id === binding.run_id && ['in_memory_workflow_audit', 'durable_workflow_audit'].includes(page.scope)
    && Array.isArray(page.events) && page.events.length <= 50 && count(page.next_after)
    && typeof page.has_more === 'boolean');
  const events = page.events.map((event, index) => {
    check(event && count(event.sequence) && event.sequence === after + index + 1
      && timestamp(event.timestamp) && kinds.includes(event.kind)
      && optionalUuid(event.proposal_id) && optionalUuid(event.reviewer_user_id)
      && (event.proposal_status === null || ['PENDING_REVIEW', 'APPROVED', 'REJECTED'].includes(event.proposal_status))
      && (event.tool === null || tools.includes(event.tool)) && (event.error === null || errors.includes(event.error)));
    return { sequence: event.sequence, timestamp: event.timestamp, kind: event.kind,
      proposal_id: event.proposal_id, reviewer_user_id: event.reviewer_user_id,
      proposal_status: event.proposal_status, tool: event.tool, error: event.error };
  });
  check(page.next_after === (events.at(-1)?.sequence ?? after) && (!page.has_more || events.length === 50));
  return { events, next_after: page.next_after, has_more: page.has_more };
}
