"""
Customer-facing order creation and listing.

Security contract
-----------------
- customer_id is ALWAYS derived from the authenticated identity; it is never
  accepted from the request body or any browser-supplied value.
- Customers may only see their own orders (enforced by deriving customer_id
  from identity, never from a query parameter).
- Existing admin POST /api/orders and GET /api/orders/:id routes are
  untouched; this module adds separate /api/my/orders endpoints.

Validation contract
-------------------
- Every line-item SKU must exist in the product catalog.
- Every line-item quantity must be ≥ 1 and ≤ 1000 (mirrors domain.OrderItem).
- The customer_id bound to the authenticated identity must resolve to a
  Customer record (prevents orphaned orders if a customer was deleted).
- Orders must have 1–100 items (mirrors domain.Order).
"""

from uuid import UUID

from pydantic import ConfigDict, Field

from .catalog import CatalogService
from .domain import Name, OrderItem
from .schemas import RequestModel
from .service import CaseService, ConflictError, NotFoundError
from .security import AuthenticatedIdentity, Role, SecurityError


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class CustomerOrderItem(RequestModel):
    """
    A single line-item supplied by the customer.

    Only sku and quantity are accepted. The product name is resolved from the
    catalog server-side so clients cannot inject arbitrary product names.
    """
    model_config = ConfigDict(extra="forbid")

    sku: Name
    quantity: int = Field(strict=True, ge=1, le=1000)


class CustomerOrderCreate(RequestModel):
    """
    Customer order creation request.

    Deliberately omits customer_id — that field is always derived from the
    authenticated identity by the route handler, never trusted from the body.
    """
    model_config = ConfigDict(extra="forbid")

    items: tuple[CustomerOrderItem, ...] = Field(min_length=1, max_length=100)


# ---------------------------------------------------------------------------
# Customer order service
# ---------------------------------------------------------------------------

class CustomerOrderService:
    """
    Creates and lists orders on behalf of an authenticated customer identity.

    Wraps CaseService (the authoritative order store) and CatalogService
    (for SKU validation + name resolution). This keeps Phase 17B.1 storage
    entirely in the existing process-local CaseService order store.
    """

    def __init__(self, cases: CaseService, catalog: CatalogService) -> None:
        self._cases = cases
        self._catalog = catalog

    def create_order(
        self, identity: AuthenticatedIdentity, request: CustomerOrderCreate
    ):
        """
        Create an order whose customer_id is taken exclusively from identity.

        Raises
        ------
        SecurityError(403)  if identity is not CUSTOMER role
        NotFoundError       if the customer record does not exist
        NotFoundError       if any SKU does not exist in the catalog
        ConflictError       if aggregate requested quantity exceeds stock
        ValueError          propagated from OrderItem if quantity is out of range
                            (should be caught by Pydantic before reaching here)
        """
        if identity.role != Role.CUSTOMER:
            raise SecurityError(403)

        # Derive customer_id from the server-side identity; never from request.
        customer_id: UUID = identity.customer_id  # type: ignore[assignment]

        # Verify the customer record exists (guard against stale identities).
        self._cases.get_customer(customer_id)

        # Resolve every SKU and validate aggregate quantities against the
        # authoritative catalog stock. This demo remains read-only: creating an
        # order does not reserve or decrement inventory.
        resolved_items: list[OrderItem] = []
        requested_by_sku: dict[str, int] = {}
        products_by_sku = {}
        for line in request.items:
            product = self._catalog.get_by_sku(line.sku)   # raises NotFoundError
            products_by_sku[line.sku] = product
            requested_by_sku[line.sku] = requested_by_sku.get(line.sku, 0) + line.quantity
            resolved_items.append(
                OrderItem(sku=line.sku, name=product.name, quantity=line.quantity)
            )
        for sku, requested in requested_by_sku.items():
            if requested > products_by_sku[sku].stock_quantity:
                raise ConflictError("Requested quantity exceeds available stock")

        # Delegate persistence to the existing CaseService.
        # We build the OrderCreate manually to inject the server-derived customer_id.
        from .schemas import OrderCreate
        order_create = OrderCreate(
            customer_id=customer_id,
            items=tuple(resolved_items),
        )
        return self._cases.create_order(order_create)

    def list_my_orders(self, identity: AuthenticatedIdentity):
        """
        Return only the orders owned by the authenticated customer.

        Returns an empty list rather than 403/404 when the customer has no
        orders yet; absence of data is not a security signal for customers.
        """
        if identity.role != Role.CUSTOMER:
            raise SecurityError(403)

        customer_id: UUID = identity.customer_id  # type: ignore[assignment]

        # list_orders verifies the customer exists and filters by customer_id.
        return self._cases.list_orders(customer_id)
