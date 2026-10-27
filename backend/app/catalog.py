"""
Product catalog: deterministic demo data and a read-only in-memory store.

SKUs match the items already used across the existing test fixtures, operations,
and knowledge base so catalog lookups are consistent with existing order data.
Persistence belongs to Phase 20; this module is intentionally process-local.
"""

from decimal import Decimal
from threading import RLock
from typing import Annotated
from uuid import UUID

from pydantic import Field, StringConstraints, computed_field

from .catalog_seed import PRODUCT_SEEDS, ProductCategory
from .domain import Name, Record
from .service import NotFoundError


# ---------------------------------------------------------------------------
# Domain records
# ---------------------------------------------------------------------------

class Money(Record):
    """Immutable money value; currency is always USD in Phase 17B.1."""
    amount: Annotated[Decimal, Field(ge=Decimal("0.01"), max_digits=12, decimal_places=2)]
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "USD"


class Product(Record):
    """Read-only product that can be referenced when creating a customer order."""
    id: UUID
    sku: Name
    name: Name
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    unit_price: Money
    category: ProductCategory
    image_path: Annotated[str, StringConstraints(
        pattern=r"^/products/[a-z0-9-]+\.(?:svg|webp)$", max_length=160,
    )]
    stock_quantity: int = Field(strict=True, ge=0, le=1000)

    @computed_field(return_type=bool)
    @property
    def available(self) -> bool:
        """Backward-compatible availability derived from authoritative stock."""
        return self.stock_quantity > 0


# ---------------------------------------------------------------------------
# Deterministic demo seed data
# ---------------------------------------------------------------------------
# SKUs deliberately mirror those in operations.py (LAP-1, LAP-2) and
# the knowledge base (ACC-1, WAR-1) so existing tests remain coherent.

_SEED_PRODUCTS: tuple[Product, ...] = tuple(Product(
    id=seed.id,
    sku=seed.sku,
    name=seed.name,
    description=seed.description,
    unit_price=Money(amount=seed.amount),
    category=seed.category,
    image_path=seed.image_path,
    stock_quantity=seed.stock_quantity,
) for seed in PRODUCT_SEEDS)


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

class CatalogService:
    """
    Read-only product catalog backed by deterministic seed data.
    Write operations (add/update/remove) belong to Phase 20+.
    Thread-safe: all reads go through an RLock for consistent future extension.
    """

    def __init__(self, seed: tuple[Product, ...] = _SEED_PRODUCTS) -> None:
        self._lock = RLock()
        # Index by id and sku for O(1) lookups in both directions.
        self._by_id: dict[UUID, Product] = {}
        self._by_sku: dict[str, Product] = {}
        for product in seed:
            self._by_id[product.id] = product
            self._by_sku[product.sku] = product

    def list_products(self, *, available_only: bool = False) -> list[Product]:
        """Return all products, optionally filtered to available-only."""
        with self._lock:
            products = list(self._by_id.values())
        if available_only:
            products = [p for p in products if p.available]
        return products

    def get_by_id(self, product_id: UUID) -> Product:
        with self._lock:
            product = self._by_id.get(product_id)
        if product is None:
            raise NotFoundError("Product not found")
        return product

    def get_by_sku(self, sku: str) -> Product:
        with self._lock:
            product = self._by_sku.get(sku)
        if product is None:
            raise NotFoundError(f"Product with SKU '{sku}' not found")
        return product

    def sku_exists(self, sku: str) -> bool:
        with self._lock:
            return sku in self._by_sku
