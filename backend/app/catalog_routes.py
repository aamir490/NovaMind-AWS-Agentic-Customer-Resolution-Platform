"""
Read-only product catalog HTTP surface.

GET /api/catalog          — list all products (authenticated; optional ?available_only=true)
GET /api/catalog/{id}     — get product by UUID
GET /api/catalog/sku/{sku} — get product by SKU string

All endpoints require a valid Bearer token (any role).
No write endpoints are exposed in Phase 17B.1.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from .catalog import CatalogService, Product
from .security import current_identity

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


def get_catalog(request: Request) -> CatalogService:
    return request.app.state.catalog_service


Catalog = Annotated[CatalogService, Depends(get_catalog)]


@router.get("", response_model=list[Product])
def list_products(
    catalog: Catalog,
    available_only: Annotated[bool, Query()] = False,
) -> list[Product]:
    """Return all products. Pass ?available_only=true to filter to in-stock items."""
    current_identity()  # authentication guard — any role
    return catalog.list_products(available_only=available_only)


@router.get("/sku/{sku}", response_model=Product)
def get_product_by_sku(sku: str, catalog: Catalog) -> Product:
    """Retrieve a single product by its SKU string."""
    current_identity()
    return catalog.get_by_sku(sku)


@router.get("/{product_id}", response_model=Product)
def get_product(product_id: UUID, catalog: Catalog) -> Product:
    """Retrieve a single product by its UUID."""
    current_identity()
    return catalog.get_by_id(product_id)
