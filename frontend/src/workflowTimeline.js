// Closed, display-safe projection for the Admin workflow timeline.
// Inputs are already validated by the API contracts. Missing evidence stays
// absent; this module never synthesizes model, tool, token, or timing facts.

const operationStages = {
  'hitl.start': ['Case intake / investigation start', 'The server recorded the start boundary.', 'reconstructed'],
  'agent.graph': ['Investigation activity', 'The workflow recorded graph activity.', 'diagnostic'],
  llm: ['Model activity', 'A model operation was recorded; response validity is shown separately.', 'diagnostic'],
  tool: ['Tool / knowledge retrieval', 'A tool operation was recorded.', 'diagnostic'],
  'proposal.create': ['HITL proposal', 'A proposal creation operation was recorded for human oversight.', 'diagnostic'],
  'proposal.review': ['Admin decision', 'A human review operation was recorded.', 'diagnostic'],
};

function safeTimestamp(value) {
  return typeof value === 'string' && Number.isFinite(Date.parse(value)) ? value : null;
}
function auditStage(event) {
  if (event.kind === 'REVIEW_REQUIRED') {
    return ['HITL review requested', 'The workflow recorded a request for human oversight.', 'persisted'];
  }
  if (event.kind === 'HUMAN_REVIEW') {
    return ['Admin decision', 'A human review decision was recorded.', 'persisted'];
  }
  if (event.kind === 'FAILURE') {
    return ['Validation / workflow failure', 'A failure was recorded at this point in the workflow.', 'persisted'];
  }
  if (event.kind === 'AGENT' && event.tool) {
    return ['Tool / knowledge retrieval', 'The audit recorded agent tool activity. Tool success is not inferred.', 'persisted'];
  }
  if (event.kind === 'AGENT') {
    return ['Agent activity', 'The audit recorded agent activity without a more specific stage.', 'persisted'];
  }
  return null;
}
function diagnosticStage(event) {
  const stage = operationStages[event.operation];
  if (!stage) return null;
  return [stage[0], stage[1], stage[2]];
}

function eventItem(stage, timestamp, source, event, order) {
  return {
    id: `${source}-${event?.sequence ?? event?.span_id ?? order}`,
    title: stage[0], detail: stage[1], source,
    timestamp: safeTimestamp(timestamp), durationMs: event?.duration_ms ?? null,
    status: event?.error ?? event?.status ?? null,
    tool: event?.tool ?? null,
  };
}

export function buildWorkflowTimeline({ durable = null, auditEvents = [], diagnosticEvents = [] } = {}) {
  const items = [];
  if (durable) {
    items.push(eventItem(
      ['Case intake / investigation start', 'Reservation and workflow metadata provide the recorded start boundary; this milestone is reconstructed.', 'reconstructed'],
      durable.created_at, 'Durable metadata', durable, 0,
    ));
  }

  auditEvents.forEach((event, index) => {
    const stage = auditStage(event);
    if (!stage) return;
    items.push(eventItem(stage, event.timestamp, 'Persisted audit', event, index + 1));
    if (event.error === 'INVALID_RESPONSE' || event.error === 'INVALID_OUTPUT') {
      items.push(eventItem(
        ['Validation', 'The audit recorded a structured-output validation error; no malformed output is trusted.', 'persisted'],
        event.timestamp, 'Persisted audit', event, `validation-${index}`,
      ));
    }
  });

  diagnosticEvents.forEach((event, index) => {
    const stage = diagnosticStage(event);
    if (!stage) return;
    items.push(eventItem(stage, event.timestamp, 'Process diagnostic', event, index + 100));
  });

  if (durable?.response_source) {
    const fallback = durable.response_source === 'DETERMINISTIC_FALLBACK';
    items.push(eventItem(
      ['Response source', fallback
        ? 'A deterministic grounded fallback was recorded; this is not validated model completion.'
        : 'A validated model response source was recorded.', 'reconstructed'],
      durable.updated_at, 'Durable metadata + checkpoint projection', durable, 'response-source',
    ));
  }

  if (durable?.failure_category) {
    items.push(eventItem(
      ['Failure recorded', `Safe failure category: ${durable.failure_category}${durable.failure_stage ? ` · stage ${durable.failure_stage}` : ''}.`, 'persisted'],
      durable.updated_at, 'Durable metadata', durable, 'failure',
    ));
  }

  items.sort((left, right) => {
    const leftTime = left.timestamp ? Date.parse(left.timestamp) : Number.MAX_SAFE_INTEGER;
    const rightTime = right.timestamp ? Date.parse(right.timestamp) : Number.MAX_SAFE_INTEGER;
    return leftTime - rightTime;
  });
  return {
    summary: durable ? {
      workflowStatus: durable.workflow_status, provider: durable.provider,
      mode: durable.submission_mode, modeVersion: durable.mode_version,
      responseSource: durable.response_source, actionsExecuted: durable.actions_executed === false,
      failureCategory: durable.failure_category, failureStage: durable.failure_stage,
      errorSource: durable.error_source, createdAt: durable.created_at, updatedAt: durable.updated_at,
    } : null,
    items,
  hasEvidence: items.length > 0,
};
}
