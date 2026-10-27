"""Focused M2B tests for the server-only trusted delivery fact boundary."""

from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from backend.app.main import create_app
from backend.app.operations import AuthoritativeDeliveryFact, TrustedDeliverySource
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.service import ConflictError
from pydantic import ValidationError


class DeliveryFactIngestionTests(unittest.TestCase):
    def setUp(self):
        self.source = TrustedDeliverySource(id="test-fulfillment")
        self.app = create_app(trusted_delivery_sources=(self.source,))
        cases = self.app.state.case_service
        customer = cases.create_customer(CustomerCreate(name="Delivery fact customer"))
        self.order = cases.create_order(OrderCreate(customer_id=customer.id, items=[
            {"sku": "LAP-1", "name": "Laptop", "quantity": 1},
        ]))
        self.case = cases.create_case(CaseCreate(
            customer_id=customer.id, order_id=self.order.id,
            subject="Delivery verification", description="Trusted source fixture.",
        ))

    def fact(self, **changes):
        now = datetime.now(timezone.utc)
        values = {
            "id": uuid4(), "case_id": self.case.id, "order_id": self.order.id,
            "customer_id": self.case.customer_id, "sku": "LAP-1", "quantity": 1,
            "delivered_at": now - timedelta(days=5), "reason": "wrong_item", "condition": "unused",
            "source_id": self.source.id, "source_reference": "fulfillment-record-1",
            "verified_at": now,
        }
        values.update(changes)
        return AuthoritativeDeliveryFact(**values)

    def test_boundary_has_no_http_route_and_requires_configured_source(self):
        def registered_paths(routes, seen=None):
            seen = set() if seen is None else seen
            for route in routes:
                route_id = id(route)
                if route_id in seen:
                    continue
                seen.add(route_id)

                path = getattr(route, "path", None)
                if isinstance(path, str):
                    yield path

                nested = getattr(route, "routes", None)
                if nested is not None:
                    yield from registered_paths(nested, seen)

                original = getattr(route, "original_router", None)
                if original is not None:
                    yield from registered_paths(
                        getattr(original, "routes", ()), seen,
                    )

        self.assertFalse(any(
            "delivery" in path and "refund-eligibility" not in path
            for path in registered_paths(self.app.routes)
        ))
        unconfigured = create_app()
        with self.assertRaisesRegex(ConflictError, "not configured"):
            unconfigured.state.delivery_fact_ingestion.ingest(self.source.id, self.fact())

    def test_rejects_unverified_or_wrong_provenance(self):
        with self.assertRaises(ValidationError):
            self.fact(verified_at=None)
        with self.assertRaisesRegex(ConflictError, "provenance"):
            self.app.state.delivery_fact_ingestion.ingest(
                self.source.id, self.fact(source_id="other-fulfillment"),
            )

    def test_validates_order_item_quantity_and_timestamps(self):
        with self.assertRaisesRegex(ConflictError, "order item"):
            self.app.state.delivery_fact_ingestion.ingest(self.source.id, self.fact(quantity=2))
        now = datetime.now(timezone.utc)
        with self.assertRaisesRegex(ConflictError, "future"):
            self.app.state.delivery_fact_ingestion.ingest(
                self.source.id, self.fact(delivered_at=now + timedelta(days=1), verified_at=now + timedelta(days=1)),
            )
        with self.assertRaisesRegex(ConflictError, "cannot predate"):
            self.app.state.delivery_fact_ingestion.ingest(
                self.source.id, self.fact(verified_at=now - timedelta(days=10)),
            )

    def test_idempotent_duplicate_and_conflicting_case_item_records(self):
        fact = self.fact()
        ingestion = self.app.state.delivery_fact_ingestion
        self.assertEqual(ingestion.ingest(self.source.id, fact), fact)
        self.assertEqual(ingestion.ingest(self.source.id, fact), fact)
        with self.assertRaisesRegex(ConflictError, "Conflicting delivery fact"):
            ingestion.ingest(self.source.id, self.fact(source_reference="fulfillment-record-2"))

    def test_rejects_reused_source_reference_for_another_record(self):
        ingestion = self.app.state.delivery_fact_ingestion
        ingestion.ingest(self.source.id, self.fact())
        with self.assertRaisesRegex(ConflictError, "provenance conflicts"):
            ingestion.ingest(self.source.id, self.fact())


if __name__ == "__main__":
    unittest.main()
