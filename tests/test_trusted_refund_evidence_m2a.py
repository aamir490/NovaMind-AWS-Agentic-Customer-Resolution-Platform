"""Focused M2A coverage for authoritative refund eligibility evidence."""

from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from backend.app.main import create_app
from backend.app.operations import AuthoritativeDeliveryFact, TrustedDeliverySource
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from pydantic import ValidationError
from security_fixtures import admin_scope


class TrustedRefundEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(admin_scope())
        self.delivery_source = TrustedDeliverySource(id="test-fulfillment")
        self.app = create_app(trusted_delivery_sources=(self.delivery_source,))
        self.cases = self.app.state.case_service
        self.operations = self.app.state.business_operations
        self.tools = self.app.state.local_tools
        self.customer = self.cases.create_customer(CustomerCreate(name="M2A customer"))
        self.order = self.cases.create_order(OrderCreate(customer_id=self.customer.id, items=[
            {"sku": "LAP-1", "name": "Laptop", "quantity": 1},
        ]))
        self.case = self.cases.create_case(CaseCreate(
            customer_id=self.customer.id, order_id=self.order.id,
            subject="Wrong item", description="Request a verified refund review.",
        ))

    def fact(self, *, days=10, **changes):
        now = datetime.now(timezone.utc)
        values = {
            "id": uuid4(), "case_id": self.case.id, "order_id": self.order.id,
            "customer_id": self.customer.id, "sku": "LAP-1", "quantity": 1,
            "delivered_at": now - timedelta(days=days), "reason": "wrong_item", "condition": "unused",
            "source_id": self.delivery_source.id, "source_reference": "test-verified-delivery",
            "verified_at": now,
        }
        values.update(changes)
        return AuthoritativeDeliveryFact(**values)

    def assessment(self):
        return self.tools.invoke("assess_refund_eligibility", {
            "case_id": str(self.case.id), "sku": "LAP-1",
        })

    def test_missing_delivery_facts_fail_closed(self):
        result = self.assessment()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "CONFLICT")
        self.assertIn("delivery facts are missing", result.error.message)

    def test_unverified_delivery_facts_fail_closed(self):
        with self.assertRaises(ValidationError):
            self.fact(verified_at=None)
        result = self.assessment()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "CONFLICT")
        self.assertIn("delivery facts are missing", result.error.message)

    def test_verified_facts_issue_case_bound_refund_evidence(self):
        self.app.state.delivery_fact_ingestion.ingest(self.delivery_source.id, self.fact())
        result = self.assessment()
        self.assertTrue(result.ok)
        self.assertTrue(result.data.refund_eligible)
        self.assertEqual(result.data.assessment_basis, "trusted_delivery_record")
        self.assertEqual(result.data.case_id, self.case.id)
        self.assertEqual(result.data.order_id, self.order.id)
        self.assertEqual(result.data.customer_id, self.customer.id)
        self.assertEqual(result.data.sku, "LAP-1")
        proposal = self.tools.invoke("create_resolution_proposal", {
            "case_id": str(self.case.id), "action": "refund", "rationale": "Human review required.",
            "eligibility_evidence_id": str(result.data.evidence_id),
        })
        self.assertTrue(proposal.ok)
        self.assertEqual(proposal.data.action, "refund")

    def test_caller_supplied_scenario_cannot_mint_refund_evidence(self):
        result = self.tools.invoke("assess_eligibility", {
            "case_id": str(self.case.id), "order_id": str(self.order.id),
            "customer_id": str(self.customer.id), "sku": "LAP-1", "quantity": 1,
            "days_since_delivery": 1, "reason": "wrong_item", "condition": "unused",
        })
        self.assertTrue(result.ok)
        self.assertTrue(result.data.refund_eligible)
        self.assertIsNone(result.data.evidence_id)
        rejected = self.tools.invoke("create_resolution_proposal", {
            "case_id": str(self.case.id), "action": "refund", "rationale": "Bypass attempt.",
        })
        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error.code, "CONFLICT")

    def test_trusted_assessment_rejects_client_controlled_policy_facts(self):
        result = self.tools.invoke("assess_refund_eligibility", {
            "case_id": str(self.case.id), "sku": "LAP-1", "days_since_delivery": 1,
        })
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
