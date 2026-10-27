"""Offline authorization coverage for the staff-only workflow-start endpoint."""

import json
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.llm import LLMResponse
from backend.app.main import create_app
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role


class _Provider:
    def generate(self, request, *, response_schema):
        return LLMResponse(text=json.dumps({"decision": {"kind": "final"}}), finish_reason="stop")


class WorkflowStartAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.providers = []

        def factory():
            provider = _Provider()
            self.providers.append(provider)
            return provider

        self.app = create_app(local_provider_factory=factory)
        service = self.app.state.case_service
        customer = service.create_customer(CustomerCreate(name="Workflow customer"))
        order = service.create_order(OrderCreate(
            customer_id=customer.id,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))
        self.case = service.create_case(CaseCreate(
            customer_id=customer.id, order_id=order.id,
            subject="Workflow authorization", description="Staff starts the investigation.",
        ))

        self.customer_token = "p1-workflow-start-customer-token"
        self.admin_token = "p1-workflow-start-admin-token"
        self.reviewer_token = "p1-workflow-start-reviewer-token"
        auth = LocalAuthenticationProvider((
            LocalCredential(token=self.customer_token, identity=AuthenticatedIdentity(
                user_id=uuid4(), role=Role.CUSTOMER, customer_id=customer.id)),
            LocalCredential(token=self.admin_token, identity=AuthenticatedIdentity(
                user_id=uuid4(), role=Role.ADMIN)),
            LocalCredential(token=self.reviewer_token, identity=AuthenticatedIdentity(
                user_id=uuid4(), role=Role.REVIEWER)),
        ))
        self.app.state.auth_provider = auth

    def body(self):
        return {
            "instance_id": str(self.app.state.frontend_runtime.instance_id),
            "request_id": str(uuid4()),
            "case_id": str(self.case.id),
            "message": "Investigate this support case.",
        }

    @staticmethod
    def headers(token):
        return {"Authorization": f"Bearer {token}"}

    def test_unauthenticated_start_is_rejected_without_provider_invocation(self):
        with TestClient(self.app) as client:
            response = client.post("/api/runs", json=self.body())
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.providers, [])

    def test_customer_start_is_rejected_without_provider_invocation(self):
        with TestClient(self.app) as client:
            response = client.post("/api/runs", json=self.body(), headers=self.headers(self.customer_token))
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json(), {"detail": "Access denied"})
        self.assertEqual(self.providers, [])

    def test_admin_and_reviewer_can_start_customer_case_workflows(self):
        with TestClient(self.app) as client:
            for token in (self.admin_token, self.reviewer_token):
                with self.subTest(token=token):
                    response = client.post("/api/runs", json=self.body(), headers=self.headers(token))
                    self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(self.providers), 2)


if __name__ == "__main__":
    unittest.main()
