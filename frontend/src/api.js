import { auditBinding, auditCases, auditRuns, auditPage } from './auditContracts.js';
import { diagnosticsSnapshot, durableInvestigationPage, diagnosticsEventPage } from './diagnosticsContracts.js';
import { analyticsResponse } from './analyticsContracts.js';

// Same-origin authenticated reads, explicit run starts, and explicit human decisions.
const isText = (value) => typeof value === 'string' && value.trim().length > 0;
const isCount = (value) => Number.isSafeInteger(value) && value >= 0;
const isTextList = (value) => Array.isArray(value) && value.every(isText);
const isUuid = (value) => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const isOptionalUuid = (value) => value === null || isUuid(value);
const isOptionalText = (value) => value === null || typeof value === 'string';
const isTimestamp = (value) => typeof value === 'string' && Number.isFinite(Date.parse(value));
const runStatuses = ['RUNNING', 'RESUMING', 'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED'];
const eventStates = [...runStatuses, 'STARTED', 'PAUSED'];
const eventNodes = ['load_case', 'evaluate_refund', 'reason', 'execute_tool', 'record_result', 'finalize',
  'fail_safely', 'prepare_review', 'human_review'];
const productCategories = ['laptops', 'displays', 'docks_hubs', 'input_devices',
  'audio', 'storage', 'bags_protection', 'warranties_services'];
const localProductImage = /^\/products\/[a-z0-9-]+\.(?:svg|webp)$/;

function requireResponse(valid, resource) {
  if (!valid) throw new Error(`The API returned an invalid ${resource} response. No result was loaded`);
}

function customerRecord(payload) {
  requireResponse(isUuid(payload?.id) && isText(payload.name)
    && Array.from(payload.name).length <= 120, 'customer');
  return { id: payload.id, name: payload.name };
}

function catalogProduct(product) {
  requireResponse(isUuid(product?.id) && isText(product.sku) && isText(product.name)
    && isText(product.description) && typeof product.available === 'boolean'
    && productCategories.includes(product.category)
    && typeof product.image_path === 'string' && localProductImage.test(product.image_path)
    && isCount(product.stock_quantity) && product.stock_quantity <= 1000
    && product.available === (product.stock_quantity > 0)
    && product.unit_price != null && isText(product.unit_price.currency)
    && typeof product.unit_price.amount === 'string' && product.unit_price.amount.length <= 20,
  'catalog product');
  return { id: product.id, sku: product.sku, name: product.name, description: product.description,
    category: product.category, image_path: product.image_path, stock_quantity: product.stock_quantity,
    available: product.available,
    unit_price: { amount: product.unit_price.amount, currency: product.unit_price.currency } };
}

function myCase(supportCase) {
  requireResponse(isUuid(supportCase?.id) && isUuid(supportCase.order_id)
    && isText(supportCase.subject) && isText(supportCase.description)
    && isText(supportCase.status) && isTimestamp(supportCase.created_at)
    && isTimestamp(supportCase.updated_at), 'my support request');
  // Only customer-facing fields; never expose workflow or reviewer metadata.
  return { id: supportCase.id, order_id: supportCase.order_id,
    subject: supportCase.subject, description: supportCase.description,
    status: supportCase.status, created_at: supportCase.created_at,
    updated_at: supportCase.updated_at };
}

const resolutionActions = ['APPROVE_REPLACEMENT', 'DECLINE_REPLACEMENT', 'REQUEST_MORE_EVIDENCE', 'ESCALATE'];
const draftIssueTypes = ['damaged', 'wrong_item', 'missing_item', 'delayed_delivery', 'malfunction', 'return', 'refund_not_received'];
const draftDecisions = ['REQUEST_EVIDENCE', 'ESCALATE'];
const draftResponseSources = ['BEDROCK_VALIDATED', 'DETERMINISTIC_FALLBACK'];
const customerLifecycleProgress = ['PENDING', 'INVESTIGATING', 'AI_REVIEW_COMPLETED',
  'AWAITING_ADMIN_DECISION', 'ADDITIONAL_EVIDENCE_REQUIRED', 'RESOLVED'];

function customerResolution(payload, caseId) {
  const supportCase = myCase(payload?.case);
  requireResponse(supportCase.id === caseId, 'case resolution');
  const decision = payload.latest;
  requireResponse(decision === null || (decision && resolutionActions.includes(decision.action)
    && isText(decision.customer_message) && Array.from(decision.customer_message).length <= 2000
    && isTimestamp(decision.decided_at)), 'case resolution');
  return { case: supportCase, latest: decision === null ? null : {
    action: decision.action, customerMessage: decision.customer_message,
    decidedAt: decision.decided_at,
  } };
}

function customerCommunication(payload) {
  if (payload === null) return null;
  requireResponse(payload && isText(payload.message)
    && Array.from(payload.message).length <= 2000
    && isTimestamp(payload.sent_at), 'customer communication');
  // Closed projection: discard any unexpected internal persistence or actor fields.
  return { message: payload.message, sentAt: payload.sent_at };
}

function customerCaseLifecycle(payload, caseId) {
  requireResponse(payload && payload.case_id === caseId && isText(payload.case_status)
    && customerLifecycleProgress.includes(payload.progress)
    && isText(payload.message) && Array.from(payload.message).length <= 2000
    && isTimestamp(payload.updated_at), 'customer case lifecycle');
  return { caseId: payload.case_id, caseStatus: payload.case_status,
    progress: payload.progress, message: payload.message, updatedAt: payload.updated_at };
}

function adminResolutionDraft(payload) {
  if (payload === null) return null;
  const citations = payload?.citations;
  const recommendation = payload?.refund_recommendation;
  requireResponse(payload && draftIssueTypes.includes(payload.issue_type)
    && draftDecisions.includes(payload.recommended_decision)
    && isTextList(payload.verified_facts) && payload.verified_facts.length >= 1 && payload.verified_facts.length <= 8
    && isTextList(payload.missing_evidence) && payload.missing_evidence.length >= 1 && payload.missing_evidence.length <= 6
    && isText(payload.decision_reason) && Array.from(payload.decision_reason).length <= 1200
    && isText(payload.customer_facing_response) && Array.from(payload.customer_facing_response).length <= 1200
    && isTextList(payload.troubleshooting_steps) && payload.troubleshooting_steps.length <= 5
    && draftResponseSources.includes(payload.response_source)
    && Array.isArray(citations) && citations.length <= 5
    && citations.every((citation) => citation && isText(citation.document_id)
      && isText(citation.chunk_id) && isText(citation.title) && isText(citation.version)
      && isText(citation.source_uri))
    && payload.actions_executed === false, 'resolution draft');
  requireResponse(recommendation === null || recommendation === undefined || (
    ['ELIGIBLE', 'NOT_ELIGIBLE', 'NEED_MORE_EVIDENCE'].includes(recommendation.outcome)
    && isText(recommendation.sku) && isTextList(recommendation.verified_evidence)
    && isTextList(recommendation.missing_evidence) && isText(recommendation.policy_id)
    && isText(recommendation.policy_version) && isText(recommendation.decision_reason)
    && Array.isArray(recommendation.citations) && recommendation.citations.length <= 5
    && recommendation.citations.every((citation) => citation && isText(citation.document_id)
      && isText(citation.chunk_id) && isText(citation.title) && isText(citation.version))
    && ['BEDROCK_VALIDATED', 'DETERMINISTIC_FALLBACK'].includes(recommendation.response_source)
    && recommendation.authorization_granted === false && recommendation.actions_executed === false
  ), 'refund recommendation');
  return { issueType: payload.issue_type, recommendedDecision: payload.recommended_decision,
    verifiedFacts: payload.verified_facts, missingEvidence: payload.missing_evidence,
    decisionReason: payload.decision_reason, customerFacingResponse: payload.customer_facing_response,
    troubleshootingSteps: payload.troubleshooting_steps,
    responseSource: payload.response_source, citations: citations.map((citation) => ({
      documentId: citation.document_id, chunkId: citation.chunk_id, title: citation.title,
      version: citation.version,
    })), ...(!recommendation ? {} : { refundRecommendation: {
      outcome: recommendation.outcome, sku: recommendation.sku,
      verifiedEvidence: recommendation.verified_evidence,
      missingEvidence: recommendation.missing_evidence,
      policyId: recommendation.policy_id, policyVersion: recommendation.policy_version,
      decisionReason: recommendation.decision_reason,
      citations: recommendation.citations.map((citation) => ({ documentId: citation.document_id,
        chunkId: citation.chunk_id, title: citation.title, version: citation.version })),
      responseSource: recommendation.response_source,
    } }) };
}

const outcomeFallbackReasons = ['MODEL_OUTPUT_INVALID', 'MODEL_OUTPUT_NOT_GROUNDED'];

function investigationOutcome(payload) {
  if (payload === null || payload === undefined) return null;
  requireResponse(payload && payload.provider === 'bedrock'
    && (payload.model_id === null || isText(payload.model_id))
    && ['BEDROCK_VALIDATED', 'DETERMINISTIC_FALLBACK'].includes(payload.response_source)
    && ['VALIDATED', 'DETERMINISTIC_FALLBACK'].includes(payload.validation_result)
    && (payload.fallback_reason === null || outcomeFallbackReasons.includes(payload.fallback_reason))
    && Array.isArray(payload.evidence_references) && payload.evidence_references.length <= 8
    && isTextList(payload.evidence_references)
    && Array.isArray(payload.missing_evidence) && payload.missing_evidence.length <= 6
    && isTextList(payload.missing_evidence)
    && Number.isSafeInteger(payload.model_calls) && payload.model_calls >= 0 && payload.model_calls <= 3
    && Number.isSafeInteger(payload.tool_calls) && payload.tool_calls >= 0 && payload.tool_calls <= 32
    && Array.isArray(payload.citations) && payload.citations.length <= 5
    && payload.citations.every((citation) => citation && isText(citation.document_id)
      && isText(citation.chunk_id) && isText(citation.title) && isText(citation.version)
      && isText(citation.source_uri))
    && payload.actions_executed === false, 'investigation outcome');
  if (payload.response_source === 'BEDROCK_VALIDATED') {
    requireResponse(payload.validation_result === 'VALIDATED' && payload.fallback_reason === null,
      'investigation outcome classification');
  } else {
    requireResponse(payload.validation_result === 'DETERMINISTIC_FALLBACK'
      && outcomeFallbackReasons.includes(payload.fallback_reason), 'investigation outcome classification');
  }
  return {
    provider: payload.provider, modelId: payload.model_id ?? null,
    responseSource: payload.response_source, validationResult: payload.validation_result,
    fallbackReason: payload.fallback_reason ?? null,
    evidenceReferences: payload.evidence_references, missingEvidence: payload.missing_evidence,
    citations: payload.citations.map((citation) => ({ documentId: citation.document_id,
      chunkId: citation.chunk_id, title: citation.title, version: citation.version })),
    modelCalls: payload.model_calls, toolCalls: payload.tool_calls,
  };
}

const investigationModes = ['MANUAL', 'AUTO'];
const investigationStates = ['NOT_AVAILABLE', 'PENDING', 'RUNNING', 'REVIEW_REQUIRED',
  'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED'];
const investigationJobStatuses = ['PENDING', 'CLAIMED', 'RECOVERY_REQUIRED'];
const workflowStatuses = ['RUNNING', 'RESUMING', 'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED',
  'FAILED', 'RECOVERY_REQUIRED'];

function investigationControl(payload) {
  const counts = payload?.counts;
  const countNames = ['total', 'pending', 'running', 'review_required', 'completed', 'failed',
    'recovery_required'];
  requireResponse(payload && investigationModes.includes(payload.configured_mode)
    && investigationModes.includes(payload.effective_mode)
    && typeof payload.automatic_capability_available === 'boolean'
    && Number.isSafeInteger(payload.version) && payload.version >= 1
    && (payload.updated_by === null || isUuid(payload.updated_by))
    && isTimestamp(payload.updated_at) && counts
    && countNames.every((name) => isCount(counts[name]))
    && counts.total === countNames.slice(1).reduce((sum, name) => sum + counts[name], 0),
  'investigation control');
  return { configuredMode: payload.configured_mode, effectiveMode: payload.effective_mode,
    automaticCapabilityAvailable: payload.automatic_capability_available,
    version: payload.version, updatedBy: payload.updated_by, updatedAt: payload.updated_at,
    counts: { total: counts.total, pending: counts.pending, running: counts.running,
      reviewRequired: counts.review_required, completed: counts.completed, failed: counts.failed,
      recoveryRequired: counts.recovery_required } };
}

function caseInvestigation(payload, caseId) {
  requireResponse(payload && payload.case_id === caseId && investigationStates.includes(payload.state)
    && (payload.job_id === null || isUuid(payload.job_id))
    && (payload.job_status === null || investigationJobStatuses.includes(payload.job_status))
    && (payload.workflow_id === null || isUuid(payload.workflow_id))
    && (payload.workflow_status === null || workflowStatuses.includes(payload.workflow_status))
    && (payload.provider === null || ['local_scripted', 'bedrock'].includes(payload.provider))
    && (payload.submission_mode === null || investigationModes.includes(payload.submission_mode))
    && (payload.mode_version === null
      || Number.isSafeInteger(payload.mode_version) && payload.mode_version >= 1)
    && (payload.submitted_at === null || isTimestamp(payload.submitted_at))
    && (payload.message === undefined || isOptionalText(payload.message))
    && (payload.error === undefined || isOptionalText(payload.error))
    && typeof payload.execution_started === 'boolean' && typeof payload.can_start === 'boolean'
    && payload.actions_executed === false, 'case investigation');
  requireResponse((payload.state === 'NOT_AVAILABLE') === (payload.job_id === null),
    'case investigation binding');
  return { caseId: payload.case_id, jobId: payload.job_id, state: payload.state,
    jobStatus: payload.job_status, workflowId: payload.workflow_id,
    workflowStatus: payload.workflow_status, provider: payload.provider,
    submissionMode: payload.submission_mode, modeVersion: payload.mode_version,
    submittedAt: payload.submitted_at, executionStarted: payload.execution_started,
    message: payload.message ?? null, error: payload.error ?? null,
    canStart: payload.can_start,
    resolutionDraft: payload.resolution_draft === null || payload.resolution_draft === undefined
      ? null : adminResolutionDraft(payload.resolution_draft),
    investigationOutcome: investigationOutcome(payload.investigation_outcome),
    actionsExecuted: false };
}

function staffResolution(payload, caseId) {
  requireResponse(payload?.case && isUuid(payload.case.id) && payload.case.id === caseId
    && isText(payload.case.status) && Array.isArray(payload.history) && payload.history.length <= 1000,
  'case resolution');
  const project = (decision) => {
    requireResponse(decision && resolutionActions.includes(decision.action)
      && isText(decision.reason) && Array.from(decision.reason).length <= 2000
      && isText(decision.customer_message) && Array.from(decision.customer_message).length <= 2000
      && isUuid(decision.admin_user_id) && isTimestamp(decision.decided_at), 'case resolution');
    return { action: decision.action, reason: decision.reason, customerMessage: decision.customer_message,
      adminUserId: decision.admin_user_id, decidedAt: decision.decided_at };
  };
  const history = payload.history.map(project);
  const latest = payload.latest === null ? null : project(payload.latest);
  requireResponse((latest === null && history.length === 0)
    || (latest !== null && history.length > 0
      && latest.decidedAt === history.at(-1).decidedAt && latest.action === history.at(-1).action), 'case resolution');
  return { case: payload.case, latest, history };
}

function knowledgeEvidence(event) {
  // Optional evidence must never interrupt run tracking. Missing or invalid
  // evidence is unavailable, distinct from a confirmed retrieval with no hits.
  if (event.kind !== 'TOOL' || event.tool !== 'search_knowledge' || event.ok !== true
      || event.state !== 'COMPLETED' || event.retrieval_method !== 'local_token_cosine_v1'
      || !Array.isArray(event.evidence) || event.evidence.length > 5) return null;
  const boundedText = (value, max) => isText(value) && Array.from(value).length <= max;
  const references = new Set();
  const evidence = [];
  for (const hit of event.evidence) {
    const chunk = hit?.chunk;
    const source = chunk?.source;
    if (!chunk || !source || chunk.trust !== 'untrusted_information' || source.authority !== 'reference_only'
        || typeof chunk.chunk_id !== 'string' || !/^[a-f0-9]{64}$/.test(chunk.chunk_id)
        || references.has(chunk.chunk_id) || !boundedText(chunk.text, 600)
        || !isCount(chunk.start_char) || !isCount(chunk.end_char)
        || chunk.end_char - chunk.start_char !== Array.from(chunk.text).length
        || typeof source.document_id !== 'string' || !/^[a-z0-9-]{1,80}$/.test(source.document_id)
        || !boundedText(source.title, 160) || !boundedText(source.version, 160)
        || typeof source.source_uri !== 'string' || !/^local-knowledge:\/\/[a-z0-9-]{1,80}$/.test(source.source_uri)) return null;
    references.add(chunk.chunk_id);
    // Closed display projection: no query, score, prompts, or arbitrary payloads.
    evidence.push({ reference: chunk.chunk_id, documentId: source.document_id,
      title: source.title, version: source.version, source: source.source_uri, snippet: chunk.text });
  }
  return evidence;
}

function runSnapshot(result, { instance_id, case_id, run_id }) {
  requireResponse(result?.instance_id === instance_id && result.case_id === case_id && isUuid(result.run_id)
    && (run_id === undefined || result.run_id === run_id) && runStatuses.includes(result.status)
    && ['local_scripted', 'bedrock'].includes(result.provider) && result.actions_executed === false
    && isOptionalUuid(result.workflow_id) && isOptionalUuid(result.conversation_id) && isOptionalUuid(result.trace_id)
    && isTimestamp(result.created_at) && isTimestamp(result.updated_at)
    && isCount(result.last_sequence) && isOptionalText(result.message) && isOptionalText(result.error), 'run snapshot');
  requireResponse(result.reviewed_status === null || ['APPROVED', 'REJECTED'].includes(result.reviewed_status), 'review outcome');
  const review = result.review;
  requireResponse(review === null || (review && isUuid(review.workflow_id) && review.workflow_id === result.workflow_id
    && review.case_id === case_id && isUuid(review.proposal_id) && isUuid(review.review_id)
    && ['return', 'refund', 'refund_investigation', 'replacement'].includes(review.action) && review.status === 'PENDING_REVIEW'
    && isText(review.rationale) && review.rationale.length <= 4000), 'run review');
  // Explicit display projection; no raw workflow state or unrestricted response dump.
  return { instance_id: result.instance_id, run_id: result.run_id, case_id: result.case_id,
    workflow_id: result.workflow_id, conversation_id: result.conversation_id, trace_id: result.trace_id,
    provider: result.provider, status: result.status, created_at: result.created_at, updated_at: result.updated_at,
    last_sequence: result.last_sequence, message: result.message, error: result.error,
    review: review === null ? null : { workflow_id: review.workflow_id, case_id: review.case_id,
      proposal_id: review.proposal_id, review_id: review.review_id, action: review.action,
      rationale: review.rationale, status: review.status },
    reviewed_status: result.reviewed_status,
    actions_executed: result.actions_executed };
}

export function createApi(fetcher = globalThis.fetch, { token = '', onUnauthorized } = {}) {
  // One in-memory client per identity. Never persist credentials or put them in URLs.
  const lifetime = new AbortController();

  async function request(path, { method = 'GET', body, signal, idempotencyKey } = {}) {
    if (lifetime.signal.aborted) throw new Error('This browser session has ended.');
    const response = await fetcher(`/api${path}`, {
      method, cache: 'no-store', credentials: 'omit', redirect: 'error',
      headers: { ...(globalThis.location?.hostname?.endsWith('.ngrok-free.app') ? { 'ngrok-skip-browser-warning': 'true' } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}),
        ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.any([AbortSignal.timeout(5000), lifetime.signal, ...(signal ? [signal] : [])]),
    });
    if (!response.ok) {
      let detail;
      try { detail = (await response.json()).detail; } catch { /* Use status fallback. */ }
      const message = Array.isArray(detail) ? detail.map((item) => item.msg).join('; ') : detail;
      const error = new Error(typeof message === 'string' ? message : `API request failed (${response.status})`);
      error.status = response.status;
      error.code = typeof detail === 'string' ? detail : null;
      if (response.status === 401) onUnauthorized?.();
      throw error;
    }
    return response.json();
  }
  const get = (path, options) => request(path, options);
  return {
    dispose() {
      token = '';
      lifetime.abort();
    },
    async createCustomer(name) {
      requireResponse(typeof name === 'string' && name.trim().length >= 1
        && Array.from(name.trim()).length <= 120, 'customer name');
      return customerRecord(await request('/customers', {
        method: 'POST', body: { name: name.trim() },
      }));
    },
    async getCustomer(customerId) {
      requireResponse(isUuid(customerId), 'customer id');
      return customerRecord(await get(`/customers/${encodeURIComponent(customerId)}`));
    },
    async identity() {
      const payload = await get('/identity');
      const identity = payload?.identity;
      if (!identity || !isUuid(identity.user_id)
          || !['CUSTOMER', 'REVIEWER', 'ADMIN'].includes(identity.role)
          || (identity.role === 'CUSTOMER'
            ? !isUuid(identity.customer_id)
            : identity.customer_id !== null)
          || typeof payload.can_review !== 'boolean' || typeof payload.can_view_diagnostics !== 'boolean'
          || !isUuid(payload.instance_id) || !['unavailable', 'local_scripted', 'bedrock'].includes(payload.provider)
          || typeof payload.memory_available !== 'boolean') {
        throw new Error('Unexpected identity response.');
      }
      // Display only identity fields confirmed by the backend, never token contents.
      return { user_id: identity.user_id, role: identity.role, customer_id: identity.customer_id,
        instance_id: payload.instance_id, provider: payload.provider, memory_available: payload.memory_available,
        can_review: payload.can_review, can_view_diagnostics: payload.can_view_diagnostics };
    },
    async investigationControl(options) {
      return investigationControl(await get('/admin/investigation-control', options));
    },
    async updateInvestigationControl({ mode, expectedVersion, idempotencyKey }) {
      requireResponse(investigationModes.includes(mode), 'investigation mode');
      requireResponse(Number.isSafeInteger(expectedVersion) && expectedVersion >= 1,
        'investigation mode version');
      requireResponse(isUuid(idempotencyKey), 'investigation mode idempotency key');
      return investigationControl(await request('/admin/investigation-control', {
        method: 'PATCH', body: { mode, expected_version: expectedVersion }, idempotencyKey,
      }));
    },
    async caseInvestigation(caseId, options) {
      requireResponse(isUuid(caseId), 'case id');
      return caseInvestigation(await get(`/cases/${encodeURIComponent(caseId)}/investigation`, options), caseId);
    },
    async startCaseInvestigation(caseId, { idempotencyKey }) {
      requireResponse(isUuid(caseId), 'case id');
      requireResponse(isUuid(idempotencyKey), 'investigation start idempotency key');
      return caseInvestigation(await request(`/cases/${encodeURIComponent(caseId)}/investigation`, {
        method: 'POST', idempotencyKey,
      }), caseId);
    },
    async pendingReviews(instanceId, options) {
      const page = await get('/reviews', options);
      requireResponse(page?.instance_id === instanceId && isTimestamp(page.snapshot_at)
        && page.scope === 'pending_reviews_in_this_process'
        && Array.isArray(page.items) && page.items.length <= 1024, 'pending review queue');
      const runIds = new Set();
      const items = page.items.map((item) => {
        const review = item?.review;
        requireResponse(item && isUuid(item.run_id) && !runIds.has(item.run_id)
          && isUuid(item.case_id) && isUuid(item.workflow_id) && isText(item.case_subject)
          && item.status === 'REVIEW_REQUIRED' && item.actions_executed === false && isTimestamp(item.updated_at)
          && review && review.case_id === item.case_id && review.workflow_id === item.workflow_id
          && isUuid(review.proposal_id) && isUuid(review.review_id) && review.status === 'PENDING_REVIEW'
          && ['return', 'refund', 'refund_investigation', 'replacement'].includes(review.action)
          && isText(review.rationale) && Array.from(review.rationale).length <= 4000, 'pending review');
        runIds.add(item.run_id);
        return { runId: item.run_id, caseId: item.case_id, caseSubject: item.case_subject,
          workflowId: item.workflow_id, updatedAt: item.updated_at, status: item.status,
          proposalId: review.proposal_id, reviewId: review.review_id, action: review.action,
          rationale: review.rationale, reviewStatus: review.status };
      });
      return { items, snapshotAt: page.snapshot_at };
    },
    async decideReview(instanceId, item, decision, options) {
      requireResponse(['APPROVE', 'REJECT'].includes(decision), 'human decision');
      // One explicit POST. A lost response must never trigger an automatic replay.
      const result = await request(`/runs/${encodeURIComponent(item.runId)}/decision`, {
        method: 'POST', signal: options?.signal,
        body: { instance_id: instanceId, case_id: item.caseId, workflow_id: item.workflowId,
          proposal_id: item.proposalId, review_id: item.reviewId, decision },
      });
      const snapshot = runSnapshot(result, { instance_id: instanceId, case_id: item.caseId, run_id: item.runId });
      requireResponse(snapshot.workflow_id === item.workflowId && snapshot.review?.review_id === item.reviewId
        && snapshot.review.proposal_id === item.proposalId, 'decision binding');
      return snapshot;
    },
    async startRun({ instance_id, request_id, case_id, message, resolution_intent, refund_sku }) {
      // Exactly one POST per explicit submission; never retry a potentially committed run.
      const body = { instance_id, request_id, case_id, message };
      if (resolution_intent !== undefined) body.resolution_intent = resolution_intent;
      if (refund_sku !== undefined) body.refund_sku = refund_sku;
      const result = await request('/runs', { method: 'POST', body });
      return runSnapshot(result, { instance_id, case_id });
    },
    async runSnapshot(binding, options) {
      return runSnapshot(await get(`/runs/${encodeURIComponent(binding.run_id)}`, options), binding);
    },
    async runEvents(binding, after, options) {
      const page = await get(`/runs/${encodeURIComponent(binding.run_id)}/events?after=${after}&limit=100`, options);
      const snapshot = runSnapshot(page?.snapshot, binding);
      requireResponse(Array.isArray(page.events) && page.events.length <= 100
        && isCount(page.next_after) && page.next_after >= after && page.next_after <= snapshot.last_sequence
        && isCount(page.first_available_sequence) && page.first_available_sequence >= 1
        && page.first_available_sequence <= snapshot.last_sequence + 1
        && page.dropped_events === page.first_available_sequence - 1
        && page.gap === (after < page.first_available_sequence - 1)
        && page.has_more === (page.next_after < snapshot.last_sequence), 'run events');
      const events = page.events.map((event, index) => {
        requireResponse(event?.run_id === binding.run_id && isCount(event.sequence)
          && event.sequence === Math.max(after + 1, page.first_available_sequence) + index
          && event.sequence <= snapshot.last_sequence && isTimestamp(event.timestamp)
          && ['STATE', 'NODE', 'TOOL'].includes(event.kind) && eventStates.includes(event.state)
          && (event.node === null || eventNodes.includes(event.node)) && isOptionalText(event.tool)
          && (event.ok === null || typeof event.ok === 'boolean') && isOptionalText(event.error)
          && isOptionalUuid(event.proposal_id), 'run event');
        return { sequence: event.sequence, timestamp: event.timestamp, kind: event.kind, state: event.state,
          node: event.node, tool: event.tool, ok: event.ok, error: event.error, proposal_id: event.proposal_id,
          evidence: knowledgeEvidence(event) };
      });
      requireResponse(page.next_after === (events.at(-1)?.sequence ?? after)
        && (!page.has_more || events.length > 0), 'run event cursor');
      return { snapshot, events, next_after: page.next_after, has_more: page.has_more, gap: page.gap };
    },
    async diagnostics(options) {
      return diagnosticsSnapshot(await get('/diagnostics?limit=50', options));
    },
    async diagnosticInvestigations({ offset = 0, limit = 25, caseId = '', runId = '', signal } = {}) {
      const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
      if (caseId) query.set('case_id', caseId);
      if (runId) query.set('run_id', runId);
      return durableInvestigationPage(await get(`/admin/diagnostics/investigations?${query}`, { signal }));
    },
    async diagnosticEvents(category, { offset = 0, limit = 50, signal } = {}) {
      const query = new URLSearchParams({ category, offset: String(offset), limit: String(limit) });
      return diagnosticsEventPage(await get(`/admin/diagnostics/events?${query}`, { signal }), category);
    },
    async analytics({ from = '', to = '', signal } = {}) {
      const query = new URLSearchParams();
      if (from) query.set('from', from);
      if (to) query.set('to', to);
      const suffix = query.toString() ? `?${query}` : '';
      return analyticsResponse(await get(`/admin/analytics${suffix}`, { signal }));
    },
    async auditCases(options) {
      return auditCases(await get('/cases', options));
    },
    async auditRuns(binding, offset = 0, options) {
      auditBinding(binding, offset);
      return auditRuns(await get(`/cases/${encodeURIComponent(binding.case_id)}/runs?offset=${offset}&limit=50`, options), binding, offset);
    },
    async runAudit(binding, after = 0, options) {
      auditBinding(binding, after, true);
      return auditPage(await get(`/runs/${encodeURIComponent(binding.run_id)}/audit?after=${after}&limit=50`, options), binding, after);
    },
    // -----------------------------------------------------------------
    // Customer portal — Phase 17B.2
    // -----------------------------------------------------------------

    async listCatalog({ availableOnly = false } = {}) {
      const params = availableOnly ? '?available_only=true' : '';
      const products = await get(`/catalog${params}`);
      requireResponse(Array.isArray(products) && products.length <= 1000, 'catalog list');
      return products.map(catalogProduct);
    },

    async getCatalogProduct(id) {
      requireResponse(isUuid(id), 'product id');
      return catalogProduct(await get(`/catalog/${encodeURIComponent(id)}`));
    },

    async createMyOrder(items) {
      // SECURITY: customer_id is NEVER sent — the backend derives it from the
      // authenticated identity. Only items (sku + quantity) are accepted.
      requireResponse(Array.isArray(items) && items.length >= 1 && items.length <= 100,
        'order items');
      for (const item of items) {
        requireResponse(isText(item?.sku) && Number.isSafeInteger(item.quantity)
          && item.quantity >= 1 && item.quantity <= 1000, 'order item');
      }
      const body = { items: items.map(({ sku, quantity }) => ({ sku, quantity })) };
      const order = await request('/my/orders', { method: 'POST', body });
      requireResponse(isUuid(order?.id) && isUuid(order.customer_id)
        && Array.isArray(order.items) && order.items.length >= 1, 'created order');
      return { id: order.id, customer_id: order.customer_id,
        items: order.items.map((it) => ({ sku: it.sku, name: it.name, quantity: it.quantity })) };
    },

    async createMyCase({ orderId, subject, description, idempotencyKey }) {
      // SECURITY: customer_id is NEVER sent — the backend derives it from the
      // authenticated identity. Only order_id, subject, and description are sent.
      requireResponse(isUuid(orderId), 'case order_id');
      requireResponse(isText(subject) && Array.from(subject).length <= 200, 'case subject');
      requireResponse(isText(description) && Array.from(description).length <= 4000, 'case description');
      const key = idempotencyKey ?? globalThis.crypto?.randomUUID?.();
      requireResponse(isUuid(key), 'case idempotency key');
      const body = { order_id: orderId, subject: subject.trim(), description: description.trim() };
      const supportCase = await request('/my/cases', { method: 'POST', body, idempotencyKey: key });
      requireResponse(isUuid(supportCase?.id) && isUuid(supportCase.customer_id)
        && isUuid(supportCase.order_id) && isText(supportCase.subject)
        && isText(supportCase.description) && isText(supportCase.status)
        && isTimestamp(supportCase.created_at), 'created case');
      return {
        id: supportCase.id,
        customer_id: supportCase.customer_id,
        order_id: supportCase.order_id,
        subject: supportCase.subject,
        description: supportCase.description,
        status: supportCase.status,
        created_at: supportCase.created_at,
      };
    },

    async listMyCases(options) {
      // The authenticated backend scopes this read. Never send customer_id.
      const cases = await get('/cases', { signal: options?.signal });
      requireResponse(Array.isArray(cases), 'my support requests list');
      return cases.map(myCase);
    },

    async getMyCase(caseId, options) {
      requireResponse(isUuid(caseId), 'case id');
      const supportCase = myCase(await get(`/cases/${encodeURIComponent(caseId)}`, { signal: options?.signal }));
      requireResponse(supportCase.id === caseId, 'my support request');
      return supportCase;
    },

    async getMyCaseResolution(caseId, options) {
      requireResponse(isUuid(caseId), 'case id');
      return customerResolution(await get(`/cases/${encodeURIComponent(caseId)}/resolution`, {
        signal: options?.signal,
      }), caseId);
    },

    async listMyOrders() {
      const orders = await get('/my/orders');
      requireResponse(Array.isArray(orders) && orders.length <= 10000, 'my orders list');
      return orders.map((order) => {
        requireResponse(isUuid(order?.id) && isUuid(order.customer_id)
          && Array.isArray(order.items) && order.items.length >= 1, 'my order');
        return { id: order.id, customer_id: order.customer_id,
          items: order.items.map((it) => ({ sku: it.sku, name: it.name, quantity: it.quantity })) };
      });
    },

    listCases: () => get('/cases'),
    async context(id) {
      const supportCase = await get(`/cases/${encodeURIComponent(id)}`);
      const [customer, order] = await Promise.all([
        get(`/customers/${encodeURIComponent(supportCase.customer_id)}`),
        get(`/orders/${encodeURIComponent(supportCase.order_id)}`),
      ]);
      return { supportCase, customer, order };
    },
    async caseResolution(caseId, options) {
      requireResponse(isUuid(caseId), 'case id');
      return staffResolution(await get(`/cases/${encodeURIComponent(caseId)}/resolution`, {
        signal: options?.signal,
      }), caseId);
    },
    async caseResolutionDraft(caseId, options) {
      requireResponse(isUuid(caseId), 'case id');
      return adminResolutionDraft(await get(`/cases/${encodeURIComponent(caseId)}/resolution-draft`, {
        signal: options?.signal,
      }));
    },

    async getMyCaseLifecycle(caseId, options) {
      requireResponse(isUuid(caseId), 'case id');
      return customerCaseLifecycle(await get(`/cases/${encodeURIComponent(caseId)}/lifecycle`, {
        signal: options?.signal,
      }), caseId);
    },
    async latestCustomerGuidance(caseId, options) {
      requireResponse(isUuid(caseId), 'case id');
      return customerCommunication(await get(
        `/cases/${encodeURIComponent(caseId)}/customer-communications/latest`,
        { signal: options?.signal },
      ));
    },
    async sendCustomerGuidance(caseId, { message, idempotencyKey }) {
      requireResponse(isUuid(caseId), 'case id');
      requireResponse(isText(message) && Array.from(message).length <= 2000,
        'customer communication');
      requireResponse(isUuid(idempotencyKey), 'customer communication idempotency key');
      return customerCommunication(await request(
        `/cases/${encodeURIComponent(caseId)}/customer-communications`, {
          method: 'POST', body: { message: message.trim() }, idempotencyKey,
        },
      ));
    },
    async resolveCase(caseId, { action, reason, customerMessage, evidenceReviewed }) {
      requireResponse(isUuid(caseId) && resolutionActions.includes(action), 'case resolution');
      requireResponse(isText(reason) && Array.from(reason).length <= 2000, 'case resolution reason');
      requireResponse(isText(customerMessage) && Array.from(customerMessage).length <= 2000,
        'customer response');
      requireResponse(typeof evidenceReviewed === 'boolean', 'evidence review confirmation');
      return staffResolution(await request(`/cases/${encodeURIComponent(caseId)}/resolution`, {
        method: 'POST', body: { action, reason: reason.trim(), customer_message: customerMessage.trim(),
          evidence_reviewed: evidenceReviewed },
      }), caseId);
    },
    async item(sku) {
      const inventory = await get(`/inventory/${encodeURIComponent(sku)}`);
      requireResponse(inventory?.sku === sku && isCount(inventory.available_quantity)
        && isText(inventory.policy_id), 'inventory');
      const policy = await get(`/policies/${encodeURIComponent(inventory.policy_id)}`);
      requireResponse(policy?.id === inventory.policy_id && isText(policy.version)
        && isCount(policy.return_window_days) && isTextList(policy.allowed_reasons)
        && typeof policy.change_of_mind_requires_unused === 'boolean'
        && typeof policy.refund_requires_return === 'boolean'
        && typeof policy.description === 'string', 'policy');
      return { inventory, policy };
    },
    async eligibility(orderId, inputs) {
      const submitted = { ...inputs };
      const result = await get(
        `/orders/${encodeURIComponent(orderId)}/eligibility?${new URLSearchParams(submitted)}`,
      );
      // Check the returned contract and request binding, never recompute eligibility.
      // Missing booleans must not render as invented denials or authorization.
      requireResponse(result?.order_id === orderId && result.customer_id === submitted.customer_id
        && result.sku === submitted.sku && result.reason === submitted.reason
        && result.condition === submitted.condition
        && isCount(result.requested_quantity) && result.requested_quantity === Number(submitted.quantity)
        && isCount(result.days_since_delivery) && result.days_since_delivery === Number(submitted.days_since_delivery)
        && isCount(result.purchased_quantity) && isText(result.policy_id) && isText(result.policy_version)
        && typeof result.return_eligible === 'boolean' && typeof result.refund_eligible === 'boolean'
        && typeof result.refund_requires_return === 'boolean' && isTextList(result.denial_reasons)
        && result.assessment_basis === 'local_scenario_unverified_inputs'
        && result.authorization_granted === false, 'eligibility');
      return result;
    },
  };
}

export const api = createApi();
