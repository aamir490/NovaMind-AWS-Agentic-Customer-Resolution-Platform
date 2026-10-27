"""Read-only HTTP surface for local inventory, policy, and rule checks."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from .domain import Name
from .routes import Service
from .security import Authorization, current_identity
from .operations import (
    BusinessOperations, EligibilityInput, EligibilityResult, InventoryItem,
    ItemCondition, ReturnPolicy, ReturnReason,
)

router = APIRouter(prefix="/api", tags=["business-operations"])


def get_operations(request: Request) -> BusinessOperations:
    return request.app.state.business_operations


Operations = Annotated[BusinessOperations, Depends(get_operations)]


@router.get("/inventory/{sku}", response_model=InventoryItem)
def get_inventory(sku: str, operations: Operations) -> InventoryItem:
    current_identity()
    return operations.get_inventory(sku)


@router.get("/policies/{policy_id}", response_model=ReturnPolicy)
def get_policy(policy_id: str, operations: Operations) -> ReturnPolicy:
    current_identity()
    return operations.get_policy(policy_id)


@router.get("/orders/{order_id}/eligibility", response_model=EligibilityResult)
def check_eligibility(
    order_id: UUID,
    customer_id: UUID,
    sku: Name,
    days_since_delivery: Annotated[int, Query(ge=0, le=36500)],
    reason: ReturnReason,
    condition: ItemCondition,
    operations: Operations,
    service: Service,
    quantity: Annotated[int, Query(ge=1, le=1000)] = 1,
    case_id: UUID | None = None,
) -> EligibilityResult:
    Authorization(service).order(order_id)
    Authorization.customer(customer_id)
    if case_id is not None:
        Authorization(service).case(case_id)
    return operations.check_eligibility(order_id, EligibilityInput(
        customer_id=customer_id, sku=sku, quantity=quantity,
        days_since_delivery=days_since_delivery, reason=reason, condition=condition,
    ), case_id=case_id)


@router.get("/cases/{case_id}/refund-eligibility", response_model=EligibilityResult)
def assess_refund_eligibility(case_id: UUID, sku: Name, operations: Operations,
                              service: Service) -> EligibilityResult:
    Authorization(service).case(case_id)
    return operations.assess_refund_eligibility(case_id, sku)
