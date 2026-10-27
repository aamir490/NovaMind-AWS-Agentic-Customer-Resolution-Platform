"""Explicit input models keep server-owned fields out of client requests."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .domain import CaseStatus, CustomerMessage, DecisionReason, Description, Name, OrderItem, ResolutionAction, Subject


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomerCreate(RequestModel):
    name: Name


class OrderCreate(RequestModel):
    customer_id: UUID
    items: tuple[OrderItem, ...] = Field(min_length=1, max_length=100)


class CaseCreate(RequestModel):
    customer_id: UUID
    order_id: UUID
    subject: Subject
    description: Description


class CaseStatusUpdate(RequestModel):
    status: CaseStatus


class CaseResolutionCreate(RequestModel):
    """Admin-only case outcome. Actor identity always comes from the access token."""
    action: ResolutionAction
    reason: DecisionReason
    customer_message: CustomerMessage
    evidence_reviewed: bool = False


class CustomerCommunicationCreate(RequestModel):
    """Admin-approved text only; case, actor, timestamp, and audit data are server-owned."""
    message: CustomerMessage


class CustomerCaseCreate(RequestModel):
    """
    Customer-safe case creation schema.

    Deliberately omits customer_id — that field is always derived from the
    authenticated identity by the route handler, never trusted from the body.
    """
    order_id: UUID
    subject: Subject
    description: Description


class InvestigationJobBind(RequestModel):
    """Staff supplies only the opaque lease token; identity comes from auth."""
    claim_id: UUID
