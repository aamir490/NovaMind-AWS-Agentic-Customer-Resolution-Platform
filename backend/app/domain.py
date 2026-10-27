"""Validated domain records; no HTTP, AI, or storage dependencies."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Subject = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
DecisionReason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
CustomerMessage = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaseStatus(StrEnum):
    OPEN = "open"
    IN_REVIEW = "in_review"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


class ResolutionAction(StrEnum):
    APPROVE_REPLACEMENT = "APPROVE_REPLACEMENT"
    DECLINE_REPLACEMENT = "DECLINE_REPLACEMENT"
    REQUEST_MORE_EVIDENCE = "REQUEST_MORE_EVIDENCE"
    ESCALATE = "ESCALATE"


class Customer(Record):
    id: UUID
    name: Name


class OrderItem(Record):
    sku: Name
    name: Name
    quantity: int = Field(strict=True, ge=1, le=1000)


class Order(Record):
    id: UUID
    customer_id: UUID
    items: tuple[OrderItem, ...] = Field(min_length=1, max_length=100)


class SupportCase(Record):
    id: UUID
    customer_id: UUID
    order_id: UUID
    subject: Subject
    description: Description
    status: CaseStatus
    created_at: datetime
    updated_at: datetime


class InvestigationJobStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class InvestigationMode(StrEnum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"


class InvestigationControl(Record):
    """Durable operator-selected intake mode; execution capability remains server-owned."""
    mode: InvestigationMode
    version: int = Field(strict=True, ge=1)
    updated_by: UUID | None = None
    updated_at: datetime


class InvestigationModeAuditEvent(Record):
    """Immutable administrative mode-change audit record."""
    sequence: int = Field(strict=True, ge=1)
    previous_mode: InvestigationMode | None = None
    mode: InvestigationMode
    version: int = Field(strict=True, ge=1)
    admin_user_id: UUID | None = None
    idempotency_key: UUID | None = None
    changed_at: datetime


class InvestigationRecoveryReason(StrEnum):
    LEASE_EXPIRED = "LEASE_EXPIRED"
    CLAIM_AMBIGUOUS = "CLAIM_AMBIGUOUS"


class InvestigationJob(Record):
    """A durable, inert request for a future staff-approved investigation."""
    id: UUID
    case_id: UUID
    customer_id: UUID
    owner_user_id: UUID
    idempotency_key: UUID
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: InvestigationJobStatus
    created_at: datetime
    updated_at: datetime
    claim_id: UUID | None = None
    claimed_by: UUID | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    recovery_reason: InvestigationRecoveryReason | None = None
    workflow_id: UUID | None = None
    bound_at: datetime | None = None
    agent_request_json: str | None = None
    agent_request_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_attempt_id: UUID | None = None
    execution_attempted_at: datetime | None = None
    submission_mode: InvestigationMode = InvestigationMode.MANUAL
    mode_version: int = Field(default=1, strict=True, ge=1)

    @model_validator(mode="after")
    def validate_claim_state(self) -> "InvestigationJob":
        claim_fields_present = all((
            self.claim_id is not None,
            self.claimed_by is not None,
            self.claimed_at is not None,
            self.lease_expires_at is not None,
        ))
        claim_fields_absent = all((
            self.claim_id is None,
            self.claimed_by is None,
            self.claimed_at is None,
            self.lease_expires_at is None,
        ))
        binding_fields_present = self.workflow_id is not None and self.bound_at is not None
        binding_fields_absent = self.workflow_id is None and self.bound_at is None
        descriptor_fields_present = self.agent_request_json is not None and self.agent_request_fingerprint is not None
        descriptor_fields_absent = self.agent_request_json is None and self.agent_request_fingerprint is None
        attempt_fields_present = self.execution_attempt_id is not None and self.execution_attempted_at is not None
        attempt_fields_absent = self.execution_attempt_id is None and self.execution_attempted_at is None
        if self.status is InvestigationJobStatus.PENDING:
            if (not claim_fields_absent or not binding_fields_absent or not descriptor_fields_absent
                    or not attempt_fields_absent or self.recovery_reason is not None):
                raise ValueError("Pending investigation jobs cannot have claim metadata")
        elif self.status is InvestigationJobStatus.CLAIMED:
            # Claim and reservation are separate durable operations. A crash
            # between them leaves this explicit, non-runnable claimed state;
            # it must never be auto-bound or auto-replayed after restart.
            if (not claim_fields_present or not (binding_fields_present or binding_fields_absent)
                    or not (descriptor_fields_present or descriptor_fields_absent)
                    or not (attempt_fields_present or attempt_fields_absent)
                    or self.recovery_reason is not None):
                raise ValueError("Claimed investigation jobs require complete claim metadata")
            if self.lease_expires_at <= self.claimed_at:
                raise ValueError("Investigation job lease must expire after its claim")
        elif self.status is InvestigationJobStatus.RECOVERY_REQUIRED:
            if (not claim_fields_present or not (binding_fields_present or binding_fields_absent)
                    or not (descriptor_fields_present or descriptor_fields_absent)
                    or not (attempt_fields_present or attempt_fields_absent)
                    or self.recovery_reason is None):
                raise ValueError("Recovery-required jobs retain their claim metadata and reason")
        return self


class InvestigationExecutionIdentity(Record):
    """Stable identity for a future execution; it is deliberately not a workflow run."""
    job_id: UUID
    execution_id: UUID
    customer_id: UUID
    owner_user_id: UUID


class InvestigationJobClaim(Record):
    """A lease acquisition only; no model or workflow work is initiated here."""
    job: InvestigationJob
    claim_id: UUID
    execution: InvestigationExecutionIdentity


class InvestigationExecutionAttempt(Record):
    """One durable authorization for a future server-internal graph invocation."""
    job_id: UUID
    workflow_id: UUID
    attempt_id: UUID
    attempted_at: datetime
    agent_request_json: str
    agent_request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class InvestigationExecutionContext(Record):
    """Trusted, narrowly scoped identity derived from one marked job attempt."""
    job_id: UUID
    execution_id: UUID
    execution_attempt_id: UUID
    workflow_id: UUID
    worker_id: UUID
    customer_id: UUID
    case_id: UUID
    order_id: UUID
    agent_request_json: str
    agent_request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class CaseResolutionDecision(Record):
    """An admin-authored case decision; never an inventory or payment operation."""
    id: UUID
    case_id: UUID
    action: ResolutionAction
    reason: DecisionReason
    customer_message: CustomerMessage
    admin_user_id: UUID
    decided_at: datetime


class CustomerResolutionDecision(Record):
    """Customer-safe decision projection; staff reasoning stays staff-only."""
    action: ResolutionAction
    customer_message: CustomerMessage
    decided_at: datetime


class CaseCustomerCommunication(Record):
    """Immutable admin-approved customer communication and internal audit record."""
    id: UUID
    case_id: UUID
    message: CustomerMessage
    admin_user_id: UUID
    sent_at: datetime


class CustomerCommunication(Record):
    """Customer-safe projection; actor and persistence metadata stay internal."""
    message: CustomerMessage
    sent_at: datetime


class CaseResolutionHistory(Record):
    case: SupportCase
    latest: CaseResolutionDecision | None
    history: tuple[CaseResolutionDecision, ...]


class CustomerCaseResolution(Record):
    case: SupportCase
    latest: CustomerResolutionDecision | None


ALLOWED_TRANSITIONS = {
    CaseStatus.OPEN: frozenset({CaseStatus.IN_REVIEW, CaseStatus.ESCALATED}),
    CaseStatus.IN_REVIEW: frozenset({CaseStatus.ESCALATED, CaseStatus.RESOLVED}),
    CaseStatus.ESCALATED: frozenset(),
    CaseStatus.RESOLVED: frozenset(),
}


def can_transition(current: CaseStatus, target: CaseStatus) -> bool:
    """Repeated status requests are harmless; backward transitions are forbidden."""
    return target == current or target in ALLOWED_TRANSITIONS[current]
