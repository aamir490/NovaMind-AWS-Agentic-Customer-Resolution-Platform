"""Admin case-resolution flow: local HTTP only, with no model or AWS calls."""

import os
import sys
import unittest
from uuid import UUID

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from backend.app.domain import Customer
from backend.app.main import create_app
from backend.app.schemas import CaseCreate, OrderCreate
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role


ADMIN_ID = UUID("00000000-0000-4000-8000-000000000013")
REVIEWER_ID = UUID("00000000-0000-4000-8000-000000000014")
CUSTOMER_A_ID = UUID("00000000-0000-4000-8000-000000000021")
CUSTOMER_B_ID = UUID("00000000-0000-4000-8000-000000000022")

ADMIN_TOKEN = "case-resolution-admin-token"
REVIEWER_TOKEN = "case-resolution-reviewer-token"
CUSTOMER_A_TOKEN = "case-resolution-customer-a"
CUSTOMER_B_TOKEN = "case-resolution-customer-b"


class CaseResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        provider = LocalAuthenticationProvider((
            LocalCredential(token=ADMIN_TOKEN, identity=AuthenticatedIdentity(user_id=ADMIN_ID, role=Role.ADMIN)),
            LocalCredential(token=REVIEWER_TOKEN, identity=AuthenticatedIdentity(user_id=REVIEWER_ID, role=Role.REVIEWER)),
            LocalCredential(token=CUSTOMER_A_TOKEN, identity=AuthenticatedIdentity(
                user_id=UUID("00000000-0000-4000-8000-000000000041"), role=Role.CUSTOMER,
                customer_id=CUSTOMER_A_ID,
            )),
            LocalCredential(token=CUSTOMER_B_TOKEN, identity=AuthenticatedIdentity(
                user_id=UUID("00000000-0000-4000-8000-000000000042"), role=Role.CUSTOMER,
                customer_id=CUSTOMER_B_ID,
            )),
        ))
        cls.app = create_app(auth_provider=provider, startup_customers=(
            Customer(id=CUSTOMER_A_ID, name="Customer A"),
            Customer(id=CUSTOMER_B_ID, name="Customer B"),
        ))
        cls.client = TestClient(cls.app, raise_server_exceptions=True)
        cls.service = cls.app.state.case_service

    @staticmethod
    def headers(token):
        return {"Authorization": f"Bearer {token}"}

    def new_case(self, customer_id=CUSTOMER_A_ID):
        order = self.service.create_order(OrderCreate(
            customer_id=customer_id,
            items=[{"sku": "LAP-1", "name": "Test laptop", "quantity": 1}],
        ))
        return self.service.create_case(CaseCreate(
            customer_id=customer_id, order_id=order.id,
            subject="Replacement request", description="The item arrived damaged.",
        ))

    @staticmethod
    def decision(action, **changes):
        return {
            "action": action,
            "reason": "Verified damage against the submitted evidence.",
            "customer_message": "We reviewed your request and will keep you updated.",
            "evidence_reviewed": False,
            **changes,
        }

    def post(self, case_id, token, body):
        return self.client.post(f"/api/cases/{case_id}/resolution", headers=self.headers(token), json=body)

    def test_admin_can_request_evidence_then_approve_with_verified_evidence(self):
        support_case = self.new_case()
        requested = self.post(support_case.id, ADMIN_TOKEN, self.decision("REQUEST_MORE_EVIDENCE"))
        self.assertEqual(requested.status_code, 200, requested.text)
        first = requested.json()
        self.assertEqual(first["case"]["status"], "in_review")
        self.assertEqual(first["latest"]["action"], "REQUEST_MORE_EVIDENCE")
        self.assertEqual(first["latest"]["admin_user_id"], str(ADMIN_ID))
        self.assertEqual(len(first["history"]), 1)

        blocked = self.post(support_case.id, ADMIN_TOKEN, self.decision("APPROVE_REPLACEMENT"))
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(self.service.get_case(support_case.id).status.value, "in_review")

        approved = self.post(support_case.id, ADMIN_TOKEN,
            self.decision("APPROVE_REPLACEMENT", evidence_reviewed=True))
        self.assertEqual(approved.status_code, 200, approved.text)
        outcome = approved.json()
        self.assertEqual(outcome["case"]["status"], "resolved")
        self.assertEqual(outcome["latest"]["action"], "APPROVE_REPLACEMENT")
        self.assertEqual(len(outcome["history"]), 2)
        # The endpoint records an outcome only; it is not wired to inventory, payments, or AI execution.
        self.assertNotIn("actions_executed", outcome)

    def test_decline_is_terminal_and_direct_status_finalization_is_rejected(self):
        support_case = self.new_case()
        self.assertEqual(self.post(support_case.id, ADMIN_TOKEN,
            self.decision("REQUEST_MORE_EVIDENCE")).status_code, 200)
        declined = self.post(support_case.id, ADMIN_TOKEN, self.decision("DECLINE_REPLACEMENT"))
        self.assertEqual(declined.status_code, 200, declined.text)
        self.assertEqual(declined.json()["case"]["status"], "resolved")
        self.assertEqual(self.post(support_case.id, ADMIN_TOKEN,
            self.decision("REQUEST_MORE_EVIDENCE")).status_code, 409)

        another_case = self.new_case()
        direct = self.client.patch(f"/api/cases/{another_case.id}/status", headers=self.headers(ADMIN_TOKEN),
            json={"status": "resolved"})
        self.assertEqual(direct.status_code, 409)

    def test_only_admin_can_finalize_and_request_payload_cannot_spoof_actor(self):
        support_case = self.new_case()
        body = self.decision("REQUEST_MORE_EVIDENCE")
        self.assertEqual(self.post(support_case.id, CUSTOMER_A_TOKEN, body).status_code, 403)
        self.assertEqual(self.post(support_case.id, REVIEWER_TOKEN, body).status_code, 403)
        self.assertEqual(self.client.post(f"/api/cases/{support_case.id}/resolution", json=body).status_code, 401)
        self.assertEqual(self.post(support_case.id, ADMIN_TOKEN,
            {**body, "admin_user_id": str(UUID(int=1))}).status_code, 422)
        self.assertEqual(self.service.resolution_history(support_case.id), ())

    def test_reason_and_customer_response_are_required(self):
        support_case = self.new_case()
        for body in (
            {"action": "REQUEST_MORE_EVIDENCE", "customer_message": "Please attach a photo."},
            {"action": "REQUEST_MORE_EVIDENCE", "reason": "Need photo"},
            self.decision("REQUEST_MORE_EVIDENCE", reason="   "),
            self.decision("REQUEST_MORE_EVIDENCE", customer_message="   "),
        ):
            self.assertEqual(self.post(support_case.id, ADMIN_TOKEN, body).status_code, 422)
        self.assertEqual(self.service.resolution_history(support_case.id), ())

    def test_customer_sees_only_own_customer_response_without_staff_reason_or_identity(self):
        support_case = self.new_case(CUSTOMER_A_ID)
        self.assertEqual(self.post(support_case.id, ADMIN_TOKEN,
            self.decision("REQUEST_MORE_EVIDENCE")).status_code, 200)

        customer = self.client.get(f"/api/cases/{support_case.id}/resolution", headers=self.headers(CUSTOMER_A_TOKEN))
        self.assertEqual(customer.status_code, 200, customer.text)
        payload = customer.json()
        self.assertEqual(payload["case"]["status"], "in_review")
        self.assertNotIn("reason", payload["latest"])
        self.assertNotIn("history", payload)
        self.assertNotIn("admin_user_id", payload["latest"])

        staff = self.client.get(f"/api/cases/{support_case.id}/resolution", headers=self.headers(ADMIN_TOKEN))
        self.assertEqual(staff.status_code, 200)
        self.assertEqual(staff.json()["history"][0]["admin_user_id"], str(ADMIN_ID))
        self.assertEqual(self.client.get(f"/api/cases/{support_case.id}/resolution",
            headers=self.headers(CUSTOMER_B_TOKEN)).status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
