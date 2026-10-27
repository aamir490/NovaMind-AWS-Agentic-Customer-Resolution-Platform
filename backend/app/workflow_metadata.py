"""Durable workflow metadata contracts; never graph checkpoints or replay payloads."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator
from .domain import Record
from .llm import InvalidResponseCategory, InvalidResponseStage


Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Provider = Literal["local_scripted", "bedrock"]
FailureCategory = Literal[
    "TIMEOUT", "UNAVAILABLE", "REFUSED", "INCOMPLETE", "INVALID_RESPONSE",
    "INPUT_LIMIT", "TOOL_ERROR", "LLM_ERROR", "STEP_LIMIT", "GRAPH_ERROR",
    "WORKFLOW_ERROR",
]
FailureStage = Literal["AGENT", "WORKFLOW"]

AGENT_FAILURE_CATEGORIES = frozenset({
    "TIMEOUT", "UNAVAILABLE", "REFUSED", "INCOMPLETE", "INVALID_RESPONSE",
    "INPUT_LIMIT", "TOOL_ERROR", "LLM_ERROR", "STEP_LIMIT", "GRAPH_ERROR",
})


class WorkflowMetadataStatus(StrEnum):
    RUNNING = "RUNNING"
    RESUMING = "RESUMING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REVIEWED = "REVIEWED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


ACTIVE_WORKFLOW_STATUSES = frozenset({
    WorkflowMetadataStatus.RUNNING,
    WorkflowMetadataStatus.RESUMING,
    WorkflowMetadataStatus.REVIEW_REQUIRED,
})

WORKFLOW_STATUS_TRANSITIONS = {
    WorkflowMetadataStatus.RUNNING: frozenset({
        WorkflowMetadataStatus.REVIEW_REQUIRED, WorkflowMetadataStatus.COMPLETED,
        WorkflowMetadataStatus.FAILED, WorkflowMetadataStatus.RECOVERY_REQUIRED,
    }),
    WorkflowMetadataStatus.REVIEW_REQUIRED: frozenset({
        WorkflowMetadataStatus.RESUMING, WorkflowMetadataStatus.REVIEWED,
        WorkflowMetadataStatus.RECOVERY_REQUIRED,
    }),
    WorkflowMetadataStatus.RESUMING: frozenset({
        WorkflowMetadataStatus.REVIEW_REQUIRED, WorkflowMetadataStatus.REVIEWED,
        WorkflowMetadataStatus.FAILED, WorkflowMetadataStatus.RECOVERY_REQUIRED,
    }),
    WorkflowMetadataStatus.REVIEWED: frozenset(),
    WorkflowMetadataStatus.COMPLETED: frozenset(),
    WorkflowMetadataStatus.FAILED: frozenset(),
    WorkflowMetadataStatus.RECOVERY_REQUIRED: frozenset(),
}


class WorkflowRunReservation(Record):
    workflow_id: UUID
    run_id: UUID
    case_id: UUID
    owner_user_id: UUID
    request_id: UUID
    request_fingerprint: Fingerprint
    provider: Provider


class WorkflowRunMetadata(WorkflowRunReservation):
    status: WorkflowMetadataStatus
    created_at: datetime
    updated_at: datetime
    failure_category: FailureCategory | None = None
    failure_stage: FailureStage | None = None
    invalid_response_stage: InvalidResponseStage | None = None
    invalid_response_category: InvalidResponseCategory | None = None

    @model_validator(mode="after")
    def _failure_fields_are_safe(self):
        if (self.failure_category is None) != (self.failure_stage is None):
            raise ValueError("workflow failure metadata must be paired")
        if self.failure_category is None:
            if self.invalid_response_stage is not None or self.invalid_response_category is not None:
                raise ValueError("invalid-response diagnostics require failure metadata")
            return self
        if self.status is not WorkflowMetadataStatus.FAILED:
            raise ValueError("workflow failure metadata requires FAILED status")
        if ((self.failure_stage == "WORKFLOW" and self.failure_category != "WORKFLOW_ERROR")
                or (self.failure_stage == "AGENT"
                    and self.failure_category not in AGENT_FAILURE_CATEGORIES)):
            raise ValueError("workflow failure metadata is invalid")
        if self.invalid_response_stage is not None or self.invalid_response_category is not None:
            if self.failure_category != "INVALID_RESPONSE" or self.failure_stage != "AGENT":
                raise ValueError("invalid-response diagnostics require an agent INVALID_RESPONSE")
            if self.invalid_response_stage is None:
                raise ValueError("invalid-response category requires a stage")
            if (self.invalid_response_category is not None
                    and self.invalid_response_stage != "json_guardrail"):
                raise ValueError("invalid-response category requires json_guardrail")
        return self


class WorkflowReservationResult(Record):
    run: WorkflowRunMetadata
    created: bool
    replayed: Literal[False] = False


class WorkflowRunEvent(Record):
    workflow_id: UUID
    sequence: int = Field(ge=1)
    kind: Literal["CREATED", "STATUS_CHANGED", "RECOVERY_REQUIRED"]
    previous_status: WorkflowMetadataStatus | None = None
    status: WorkflowMetadataStatus
    created_at: datetime


class WorkflowMetadataRepository(Protocol):
    def reserve(self, reservation: WorkflowRunReservation) -> WorkflowReservationResult: ...
    def get(self, workflow_id: UUID) -> WorkflowRunMetadata: ...
    def get_by_run_id(self, run_id: UUID) -> WorkflowRunMetadata: ...
    def get_by_request(self, owner_user_id: UUID, request_id: UUID) -> WorkflowRunMetadata: ...
    def active_runs(self) -> tuple[WorkflowRunMetadata, ...]: ...
    def visible_runs_for_case(self, case_id: UUID, owner_user_id: UUID | None,
                              offset: int, limit: int) -> tuple[WorkflowRunMetadata, ...]: ...
    def recent_runs(self, *, offset: int, limit: int, case_id: UUID | None = None,
                    run_id: UUID | None = None) -> tuple[WorkflowRunMetadata, ...]: ...
    def transition(self, workflow_id: UUID, *, owner_user_id: UUID, case_id: UUID,
                   expected_status: WorkflowMetadataStatus,
                   status: WorkflowMetadataStatus,
                   failure_category: FailureCategory | None = None,
                   failure_stage: FailureStage | None = None,
                   invalid_response_stage: InvalidResponseStage | None = None,
                   invalid_response_category: InvalidResponseCategory | None = None) -> WorkflowRunMetadata: ...
    def require_recovery(self, workflow_id: UUID, *, owner_user_id: UUID,
                         case_id: UUID,
                         expected_status: WorkflowMetadataStatus) -> WorkflowRunMetadata: ...
    def events(self, workflow_id: UUID) -> tuple[WorkflowRunEvent, ...]: ...
