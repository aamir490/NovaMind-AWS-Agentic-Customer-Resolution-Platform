const count = (value) => Number.isSafeInteger(value) && value >= 0;
const date = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(`${value}T00:00:00Z`));
const bucket = (value, allowed) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.entries(value).every(([key, amount]) => allowed.includes(key) && count(amount));
const durations = (value) => value === null || (typeof value === 'number' && Number.isFinite(value) && value >= 0);
const check = (valid) => {
  if (!valid) {
    const error = new Error('Analytics response could not be verified.');
    error.code = 'INVALID_ANALYTICS_RESPONSE';
    throw error;
  }
};

const statuses = ['open', 'in_review', 'escalated', 'resolved'];
const workflowStatuses = ['RUNNING', 'RESUMING', 'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED'];
const modes = ['MANUAL', 'AUTO', 'UNAVAILABLE'];
const providers = ['local_scripted', 'bedrock'];
const responseSources = ['BEDROCK_VALIDATED', 'DETERMINISTIC_FALLBACK', 'UNAVAILABLE'];
const decisions = ['APPROVE_REPLACEMENT', 'DECLINE_REPLACEMENT', 'REQUEST_MORE_EVIDENCE', 'ESCALATE'];

export function analyticsResponse(payload) {
  check(payload?.scope === 'durable_analytics' && date(payload.from_date) && date(payload.to_date)
    && bucket(payload.case_status_counts, statuses)
    && bucket(payload.workflow_status_counts, workflowStatuses)
    && bucket(payload.mode_counts, modes)
    && bucket(payload.provider_counts, providers)
    && bucket(payload.response_source_counts, responseSources)
    && bucket(payload.decision_counts, decisions)
    && count(payload.workflow_count) && count(payload.hitl_decision_count)
    && count(payload.duration_count)
    && durations(payload.duration_min_ms) && durations(payload.duration_avg_ms) && durations(payload.duration_max_ms)
    && typeof payload.duration_measurement_available === 'boolean'
    && typeof payload.duration_measurement_sampled === 'boolean');
  return { ...payload };
}
