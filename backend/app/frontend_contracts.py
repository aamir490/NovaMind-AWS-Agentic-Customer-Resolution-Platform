"""Small, closed HTTP projections; never graph checkpoints or model payloads."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from .agent import AgentRequest, Contract
from .conversations import Conversation, ConversationMessage
from .hitl import HumanDecision, ReviewRequired
from .knowledge import RetrievalHit
from .llm import InvalidResponseCategory, InvalidResponseStage
from .observability import ERRORS, MAX_SCHEMA_ERRORS, MAX_SCHEMA_LOCATION
from .security import AuthenticatedIdentity
from .resolution_drafts import InvestigationOutcome, ResolutionDraft
from .domain import CaseStatus, InvestigationJobStatus, InvestigationMode
from .workflow_metadata import FailureCategory, FailureStage, WorkflowMetadataStatus


class IdentityResponse(Contract):
    identity: AuthenticatedIdentity
    can_review: bool
    can_view_diagnostics: bool
    instance_id: UUID
    provider: Literal["unavailable", "local_scripted", "bedrock"]
    memory_available: bool


class InvestigationStatusCounts(Contract):
    total: int = Field(strict=True, ge=0)
    pending: int = Field(strict=True, ge=0)
    running: int = Field(strict=True, ge=0)
    review_required: int = Field(strict=True, ge=0)
    completed: int = Field(strict=True, ge=0)
    failed: int = Field(strict=True, ge=0)
    recovery_required: int = Field(strict=True, ge=0)


class InvestigationControlUpdate(Contract):
    mode: InvestigationMode
    expected_version: int = Field(strict=True, ge=1)


class InvestigationControlSnapshot(Contract):
    configured_mode: InvestigationMode
    effective_mode: InvestigationMode
    automatic_capability_available: bool
    version: int = Field(strict=True, ge=1)
    updated_by: UUID | None = None
    updated_at: datetime
    counts: InvestigationStatusCounts


class InvestigationModeAuditItem(Contract):
    sequence: int = Field(strict=True, ge=1)
    previous_mode: InvestigationMode | None = None
    mode: InvestigationMode
    version: int = Field(strict=True, ge=1)
    admin_user_id: UUID | None = None
    changed_at: datetime


class InvestigationModeAuditPage(Contract):
    items: tuple[InvestigationModeAuditItem, ...] = Field(max_length=100)
    next_after: int = Field(strict=True, ge=0)
    has_more: bool


class CaseInvestigationSnapshot(Contract):
    case_id: UUID
    job_id: UUID | None = None
    state: Literal[
        "NOT_AVAILABLE", "PENDING", "RUNNING", "REVIEW_REQUIRED", "COMPLETED",
        "FAILED", "RECOVERY_REQUIRED",
    ]
    job_status: InvestigationJobStatus | None = None
    workflow_id: UUID | None = None
    workflow_status: WorkflowMetadataStatus | None = None
    provider: Literal["local_scripted", "bedrock"] | None = None
    submission_mode: InvestigationMode | None = None
    mode_version: int | None = Field(default=None, strict=True, ge=1)
    submitted_at: datetime | None = None
    execution_started: bool
    can_start: bool
    message: str | None = None
    error: str | None = Field(default=None, strict=True)
    error_source: Literal["workflow_metadata", "checkpoint", "projection_fallback"] | None = None
    resolution_draft: ResolutionDraft | None = None
    investigation_outcome: InvestigationOutcome | None = None
    actions_executed: Literal[False] = False

    @field_validator("error")
    @classmethod
    def known_error(cls, value):
        if value is not None and value not in ERRORS:
            raise ValueError("Invalid diagnostic code")
        return value


class RunStart(AgentRequest):
    instance_id: UUID
    request_id: UUID


class RunDecision(HumanDecision):
    instance_id: UUID


class RunSnapshot(Contract):
    instance_id: UUID
    run_id: UUID
    case_id: UUID
    workflow_id: UUID | None = None
    conversation_id: UUID | None = None
    provider: Literal["local_scripted", "bedrock"] = "local_scripted"
    status: Literal["RUNNING", "RESUMING", "REVIEW_REQUIRED", "REVIEWED", "COMPLETED", "FAILED", "RECOVERY_REQUIRED"]
    created_at: datetime
    updated_at: datetime
    last_sequence: int = 0
    trace_id: UUID | None = None
    review: ReviewRequired | None = None
    reviewed_status: Literal["APPROVED", "REJECTED"] | None = None
    message: str | None = None
    resolution_draft: ResolutionDraft | None = None
    error: str | None = None
    failure_category: FailureCategory | None = None
    failure_stage: FailureStage | None = None
    invalid_response_stage: InvalidResponseStage | None = None
    invalid_response_category: InvalidResponseCategory | None = None
    actions_executed: Literal[False] = False


class RunEvent(Contract):
    run_id: UUID
    sequence: int
    timestamp: datetime
    kind: Literal["STATE", "NODE", "TOOL"]
    state: Literal["RUNNING", "RESUMING", "REVIEW_REQUIRED", "REVIEWED", "COMPLETED", "FAILED", "STARTED", "PAUSED"]
    node: Literal["load_case", "evaluate_refund", "reason", "execute_tool", "record_result", "finalize",
                  "fail_safely", "prepare_review", "human_review"] | None = None
    tool: str | None = None
    ok: bool | None = None
    error: str | None = None
    proposal_id: UUID | None = None
    # Validated local references only, with no query, prompts, or arbitrary tool data.
    evidence: tuple[RetrievalHit, ...] = Field(default=(), max_length=5)
    retrieval_method: Literal["local_token_cosine_v1"] | None = None


class EventPage(Contract):
    snapshot: RunSnapshot
    events: tuple[RunEvent, ...]
    next_after: int
    has_more: bool
    first_available_sequence: int
    dropped_events: int
    gap: bool


class RunPage(Contract):
    items: tuple[RunSnapshot, ...]
    next_offset: int | None
    scope: Literal["case_runs_in_this_process", "case_runs_with_durable_metadata"] = "case_runs_in_this_process"


class PendingReviewRun(Contract):
    run_id: UUID
    case_id: UUID
    case_subject: str
    workflow_id: UUID
    status: Literal["REVIEW_REQUIRED"] = "REVIEW_REQUIRED"
    updated_at: datetime
    review: ReviewRequired
    actions_executed: Literal[False] = False


class PendingReviewPage(Contract):
    instance_id: UUID
    snapshot_at: datetime
    scope: Literal["pending_reviews_in_this_process"] = "pending_reviews_in_this_process"
    # The runtime retains at most 1,024 runs. One bounded read avoids skipping
    # pending entries when reviews change between offset-based page requests.
    items: tuple[PendingReviewRun, ...] = Field(max_length=1024)


class ConversationPage(Contract):
    items: tuple[Conversation, ...]
    next_offset: int | None


class MessagePage(Contract):
    conversation: Conversation
    messages: tuple[ConversationMessage, ...]
    next_after: int
    has_more: bool
    trust: Literal["untrusted_conversation_text"] = "untrusted_conversation_text"


class AuditSummary(Contract):
    sequence: int
    timestamp: datetime
    kind: str
    proposal_id: UUID | None = None
    reviewer_user_id: UUID | None = None
    proposal_status: Literal["PENDING_REVIEW", "APPROVED", "REJECTED"] | None = None
    tool: str | None = None
    error: str | None = None
    invalid_response_stage: InvalidResponseStage | None = None
    invalid_response_category: InvalidResponseCategory | None = None


class AuditPage(Contract):
    run_id: UUID
    events: tuple[AuditSummary, ...]
    next_after: int
    has_more: bool
    scope: Literal["in_memory_workflow_audit", "durable_workflow_audit"] = "in_memory_workflow_audit"


class OutputSchemaError(Contract):
    # Mirror the observer's fixed vocabulary; never accept arbitrary error text.
    type: Literal["missing", "extra_forbidden", "union_tag_invalid", "union_tag_not_found",
        "literal_error", "string_type", "int_type", "float_type", "bool_type", "dict_type", "list_type",
        "tuple_type", "model_type", "model_attributes_type", "json_type", "enum", "uuid_type", "uuid_parsing",
        "string_too_short", "string_too_long", "string_pattern_mismatch", "greater_than", "greater_than_equal",
        "less_than", "less_than_equal", "too_short", "too_long", "finite_number", "value_error", "assertion_error",
        "other"]
    location: tuple[Literal["decision", "kind", "name", "arguments", "tool", "final",
                            "<field>", "<index>", "<unknown>", "<truncated>"], ...] = Field(max_length=MAX_SCHEMA_LOCATION)


class TelemetryEvent(Contract):
    timestamp: datetime
    operation: str
    trace_id: UUID
    span_id: UUID
    parent_span_id: UUID | None
    duration_ms: float
    error: str | None
    workflow_id: UUID | None = None
    case_id: UUID | None = None
    proposal_id: UUID | None = None
    reviewer_user_id: UUID | None = None
    tool: str | None = None
    status: str | None = None
    provider: Literal["bedrock", "fake", "other"] | None = None
    invalid_response_stage: Literal["provider_envelope", "json_guardrail", "output_schema"] | None = None
    invalid_response_category: Literal["bedrock_empty_output", "bedrock_nonempty_json_rejected"] | None = None
    output_schema_errors: tuple[OutputSchemaError, ...] | None = Field(default=None, max_length=MAX_SCHEMA_ERRORS)
    output_schema_errors_truncated: bool | None = Field(default=None, strict=True)
    method: str | None = None
    status_code: int | None = None
    provider_status_code: int | None = Field(default=None, strict=True, ge=100, le=599)
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    output_token_limit: int | None = None
    provider_called: bool | None = None
    reviewer_authenticated: bool | None = None


class OperationMetrics(Contract):
    count: int
    errors: int
    duration_ms_total: float
    duration_ms_max: float
    provider_calls: int
    input_tokens_known: int
    output_tokens_known: int
    total_tokens_known: int
    input_tokens_unknown_calls: int
    output_tokens_unknown_calls: int
    total_tokens_unknown_calls: int


class DiagnosticsResponse(Contract):
    snapshot_at: datetime
    scope: Literal["process_metadata_only"] = "process_metadata_only"
    events: tuple[TelemetryEvent, ...]
    metrics: dict[str, OperationMetrics]
    evicted_events: int
    omitted_events: int
    logging_failures: int


class CustomerCaseLifecycle(Contract):
    """Closed, customer-safe progress projection derived from durable state."""

    case_id: UUID
    case_status: CaseStatus
    progress: Literal[
        "PENDING", "INVESTIGATING", "AI_REVIEW_COMPLETED",
        "AWAITING_ADMIN_DECISION", "ADDITIONAL_EVIDENCE_REQUIRED", "RESOLVED",
    ]
    message: str = Field(strict=True, min_length=1, max_length=2000)
    updated_at: datetime


class DurableInvestigationItem(Contract):
    """Admin-only metadata projection; no checkpoint or model content."""

    case_id: UUID
    workflow_id: UUID
    run_id: UUID
    workflow_status: WorkflowMetadataStatus
    provider: Literal["local_scripted", "bedrock"]
    submission_mode: InvestigationMode | None = None
    mode_version: int | None = Field(default=None, strict=True, ge=1)
    created_at: datetime
    updated_at: datetime
    failure_category: FailureCategory | None = None
    failure_stage: FailureStage | None = None
    error_source: Literal["workflow_metadata", "checkpoint", "projection_fallback"] | None = None
    response_source: Literal["BEDROCK_VALIDATED", "DETERMINISTIC_FALLBACK"] | None = None
    actions_executed: Literal[False] = False


class DurableInvestigationPage(Contract):
    items: tuple[DurableInvestigationItem, ...] = Field(max_length=100)
    next_offset: int | None = Field(default=None, strict=True, ge=0)
    has_more: bool
    scope: Literal["durable_workflow_metadata"] = "durable_workflow_metadata"


class DiagnosticsEventPage(Contract):
    events: tuple[TelemetryEvent, ...] = Field(max_length=50)
    next_offset: int | None = Field(default=None, strict=True, ge=0)
    has_more: bool
    category: Literal["workflow", "model", "tools", "http"]
    scope: Literal["process_metadata_only"] = "process_metadata_only"


class AnalyticsResponse(Contract):
    """Admin-only aggregate projection; no customer, prompt, or model payloads."""

    scope: Literal["durable_analytics"] = "durable_analytics"
    from_date: str
    to_date: str
    case_status_counts: dict[str, int]
    workflow_status_counts: dict[str, int]
    mode_counts: dict[str, int]
    provider_counts: dict[str, int]
    response_source_counts: dict[str, int]
    decision_counts: dict[str, int]
    workflow_count: int = Field(ge=0)
    hitl_decision_count: int = Field(ge=0)
    duration_count: int = Field(ge=0)
    duration_min_ms: float | None = Field(default=None, ge=0)
    duration_avg_ms: float | None = Field(default=None, ge=0)
    duration_max_ms: float | None = Field(default=None, ge=0)
    duration_measurement_available: bool
    duration_measurement_sampled: bool
