"""Focused M1 regression coverage for proposal authorization and refund evidence."""

import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

from backend.app.main import create_app
from backend.app.operations import (
    AuthoritativeDeliveryFact, TrustedDeliverySource,
)
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.security import (
    AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, authenticated,
)


class ProposalSecurityM1Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.delivery_source = TrustedDeliverySource(id="test-fulfillment")
        self.app = create_app(trusted_delivery_sources=(self.delivery_source,))
        self.cases = self.app.state.case_service
        self.customer = self.cases.create_customer(CustomerCreate(name="M1 customer"))
        self.order = self.cases.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))
        self.case = self.cases.create_case(CaseCreate(
            customer_id=self.customer.id, order_id=self.order.id,
            subject="Wrong item", description="Request a policy review.",
        ))
        self.identities = {
            "customer": AuthenticatedIdentity(
                user_id=uuid4(), role="CUSTOMER", customer_id=self.customer.id,
            ),
            "reviewer": AuthenticatedIdentity(user_id=uuid4(), role="REVIEWER"),
            "admin": AuthenticatedIdentity(user_id=uuid4(), role="ADMIN"),
        }
        self.tokens = {name: f"m1-proposal-security-{name}" for name in self.identities}
        self.provider = LocalAuthenticationProvider(tuple(
            LocalCredential(token=self.tokens[name], identity=identity)
            for name, identity in self.identities.items()
        ))
        self.app.state.auth_provider = self.provider
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://local.test",
        )
        self.addAsyncCleanup(self.client.aclose)

    async def request(self, method, path, who, body=None):
        return await self.client.request(
            method, path, json=body,
            headers={"Authorization": "Bearer " + self.tokens[who]},
        )

    async def evidence(self, who="reviewer", *, days=10):
        now = datetime.now(timezone.utc)
        self.app.state.delivery_fact_ingestion.ingest(self.delivery_source.id,
            AuthoritativeDeliveryFact(
                id=uuid4(), case_id=self.case.id, order_id=self.order.id,
                customer_id=self.customer.id, sku="LAP-1", quantity=1,
                delivered_at=now - timedelta(days=days), reason="wrong_item",
                condition="unused", source_id=self.delivery_source.id,
                source_reference="test-verified-delivery",
                verified_at=now,
            )
        )
        response = await self.request(
            "GET", f"/api/cases/{self.case.id}/refund-eligibility?sku=LAP-1", who,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def proposal(self, evidence_id=None):
        body = {
            "case_id": str(self.case.id),
            "action": "refund",
            "rationale": "Eligible refund for human review.",
        }
        if evidence_id is not None:
            body["eligibility_evidence_id"] = evidence_id
        return body

    async def test_authorized_staff_can_create_eligible_refund_proposal(self):
        evidence = await self.evidence("reviewer", days=30)
        self.assertTrue(evidence["refund_eligible"])
        response = await self.request(
            "POST", "/api/proposals", "reviewer", self.proposal(evidence["evidence_id"]),
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "PENDING_REVIEW")
        self.assertEqual(response.json()["eligibility_evidence_id"], evidence["evidence_id"])

    async def test_customer_cannot_call_direct_proposal_endpoint(self):
        evidence = await self.evidence("customer")
        response = await self.request(
            "POST", "/api/proposals", "customer", self.proposal(evidence["evidence_id"]),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.app.state.proposal_service.list_for_case(self.case.id), [])

    async def test_ineligible_evidence_fails_at_http_and_tool_service_boundary(self):
        evidence = await self.evidence("reviewer", days=31)
        self.assertFalse(evidence["refund_eligible"])
        response = await self.request(
            "POST", "/api/proposals", "admin", self.proposal(evidence["evidence_id"]),
        )
        self.assertEqual(response.status_code, 409)
        with authenticated(self.provider, self.tokens["customer"]):
            result = self.app.state.local_tools.invoke(
                "create_resolution_proposal", self.proposal(evidence["evidence_id"]),
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "CONFLICT")
        self.assertEqual(self.app.state.proposal_service.list_for_case(self.case.id), [])

    async def test_missing_or_unknown_evidence_fails_closed(self):
        missing = await self.request(
            "POST", "/api/proposals", "admin", self.proposal(),
        )
        unknown = await self.request(
            "POST", "/api/proposals", "admin", self.proposal(str(uuid4())),
        )
        supplied_flag = await self.request(
            "POST", "/api/proposals", "admin", {**self.proposal(), "refund_eligible": True},
        )
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(unknown.status_code, 409)
        self.assertEqual(supplied_flag.status_code, 422)
        self.assertEqual(self.app.state.proposal_service.list_for_case(self.case.id), [])


if __name__ == "__main__":
    unittest.main()
