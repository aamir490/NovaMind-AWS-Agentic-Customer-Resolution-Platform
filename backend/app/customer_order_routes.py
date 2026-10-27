"""
Customer-facing "My Orders" HTTP surface.

POST /api/my/orders   — create an order as the authenticated customer
GET  /api/my/orders   — list all orders belonging to the authenticated customer

Security:
- Both endpoints require Role.CUSTOMER.
- customer_id is ALWAYS derived from the authenticated identity; any attempt
  to supply customer_id in the request body is rejected by Pydantic (extra="forbid").
- Customers cannot list or create orders on behalf of another customer.
- The existing admin POST /api/orders and GET /api/orders/:id routes are
  preserved unchanged in routes.py.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from .domain import Order
from .customer_orders import CustomerOrderCreate, CustomerOrderService
from .security import Role, current_identity, require_roles

router = APIRouter(prefix="/api/my", tags=["customer-orders"])


def get_customer_order_service(request: Request) -> CustomerOrderService:
    return request.app.state.customer_order_service


CustomerOrders = Annotated[CustomerOrderService, Depends(get_customer_order_service)]


@router.post("/orders", response_model=Order, status_code=201)
def create_my_order(body: CustomerOrderCreate, svc: CustomerOrders) -> Order:
    """
    Create an order for the authenticated customer.

    The customer_id is taken from the server-side authenticated identity and
    must NOT be supplied in the request body — extra fields are rejected (422).
    Every item SKU must exist in the product catalog.
    """
    identity = require_roles(Role.CUSTOMER)
    return svc.create_order(identity, body)


@router.get("/orders", response_model=list[Order])
def list_my_orders(svc: CustomerOrders) -> list[Order]:
    """
    Return all orders belonging to the authenticated customer.

    Only the caller's own orders are returned; there is no way to request
    another customer's orders through this endpoint.
    """
    identity = require_roles(Role.CUSTOMER)
    return svc.list_my_orders(identity)
