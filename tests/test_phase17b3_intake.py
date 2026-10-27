"""
Phase 17B.3 — Customer Support Intake: focused backend tests.

Coverage matrix
---------------
POST /api/my/cases — happy path
  ✓ CUSTOMER creates case for own order → 201
  ✓ response customer_id equals authenticated CUSTOMER identity
  ✓ response order_id matches supplied order_id
  ✓ returned Case ID is a valid UUID
  ✓ case status is OPEN
  ✓ subject and description reflected in response

Authorization / ownership
  ✓ customer_id in request body rejected (extra="forbid") → 422
  ✓ cross-customer order rejected → 403
  ✓ unknown order_id safely rejected → 403 (not 404 — no id disclosure)
  ✓ ADMIN cannot use /api/my/cases → 403
  ✓ REVIEWER cannot use /api/my/cases → 403
  ✓ unauthenticated request rejected → 401

Validation
  ✓ empty subject rejected → 422
  ✓ subject > 200 chars rejected → 422
  ✓ missing subject rejected → 422
  ✓ empty description rejected → 422
  ✓ description > 4000 chars rejected → 422
  ✓ missing description rejected → 422
  ✓ invalid order_id (non-UUID) rejected → 422
  ✓ missing order_id rejected → 422

Visibility
  ✓ created case visible to ADMIN via GET /api/cases
  ✓ created case visible to owning CUSTOMER via GET /api/cases
  ✓ created case not visible to a different CUSTOMER

Existing behavior preserved
  ✓ existing POST /api/cases (admin path with explicit customer_id) unchanged
  ✓ existing GET /api/cases returns all cases for ADMIN
"""

import sys
import os
import unittest
from uuid import UUID, uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from backend.app.domain import Customer
from backend.app.main import create_app
from backend.app.schemas import OrderCreate
from backend.app.security import (
    AuthenticatedIdentity,
    LocalAuthenticationProvider,
    LocalCredential,
    Role,
)

# ---------------------------------------------------------------------------
# Fixed UUIDs
# ---------------------------------------------------------------------------
ADMIN_ID      = UUID("00000000-0000-4000-8000-000000000013")
REVIEWER_ID   = UUID("00000000-0000-4000-8000-000000000014")
CUST_A_UID    = UUID("00000000-0000-4000-8000-000000000041")  # user_id in token
CUST_B_UID    = UUID("00000000-0000-4000-8000-000000000042")
CUST_A_ID     = UUID("00000000-0000-4000-8000-000000000021")  # customer domain record
CUST_B_ID     = UUID("00000000-0000-4000-8000-000000000022")

ADMIN_TOKEN    = "phase17b3-admin-token-0001"
REVIEWER_TOKEN = "phase17b3-reviewer-tok001"
CUST_A_TOKEN   = "phase17b3-customer-a-tok1"
CUST_B_TOKEN   = "phase17b3-customer-b-tok1"


# ---------------------------------------------------------------------------
# Shared test base
# ---------------------------------------------------------------------------

class IntakeTestBase(unittest.TestCase):
    """
    One app + TestClient shared across test methods.
    Customer A and B both have seeded Customer records.
    Each test creates its own order(s) via the service directly.
    """

    @classmethod
    def setUpClass(cls):
        provider = LocalAuthenticationProvider((
            LocalCredential(token=ADMIN_TOKEN,
                identity=AuthenticatedIdentity(user_id=ADMIN_ID, role=Role.ADMIN)),
            LocalCredential(token=REVIEWER_TOKEN,
                identity=AuthenticatedIdentity(user_id=REVIEWER_ID, role=Role.REVIEWER)),
            LocalCredential(token=CUST_A_TOKEN,
                identity=AuthenticatedIdentity(user_id=CUST_A_UID, role=Role.CUSTOMER,
                                               customer_id=CUST_A_ID)),
            LocalCredential(token=CUST_B_TOKEN,
                identity=AuthenticatedIdentity(user_id=CUST_B_UID, role=Role.CUSTOMER,
                                               customer_id=CUST_B_ID)),
        ))
        cls.app = create_app(
            auth_provider=provider,
            startup_customers=(
                Customer(id=CUST_A_ID, name="Demo Customer"),
                Customer(id=CUST_B_ID, name="Demo Customer"),
            ),
        )
        cls.svc = cls.app.state.case_service
        cls.client = TestClient(cls.app, raise_server_exceptions=True)

    def hdrs(self, token):
        return {"Authorization": f"Bearer {token}"}

    def make_order(self, customer_id, sku="LAP-1"):
        """Create an order directly in the service (bypasses admin HTTP route)."""
        return self.svc.create_order(OrderCreate(
            customer_id=customer_id,
            items=[{"sku": sku, "name": "Test Item", "quantity": 1}],
        ))

    def post_my_case(self, token, body):
        headers = self.hdrs(token)
        headers["Idempotency-Key"] = str(uuid4())
        return self.client.post("/api/my/cases", headers=headers, json=body)

    def valid_body(self, order_id):
        return {
            "order_id": str(order_id),
            "subject": "Item arrived damaged",
            "description": "The item was visibly damaged when I opened the package.",
        }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class HappyPathTests(IntakeTestBase):

    def test_customer_creates_case_for_own_order_201(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN, self.valid_body(order.id))
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_response_customer_id_equals_authenticated_identity(self):
        """Backend derives customer_id from identity — never from request body."""
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN, self.valid_body(order.id))
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["customer_id"], str(CUST_A_ID))

    def test_response_order_id_matches_supplied(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN, self.valid_body(order.id))
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["order_id"], str(order.id))

    def test_returned_case_id_is_valid_uuid(self):
        import re
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN, self.valid_body(order.id))
        self.assertEqual(resp.status_code, 201)
        case_id = resp.json().get("id", "")
        self.assertRegex(case_id,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def test_case_status_is_open(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN, self.valid_body(order.id))
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["status"], "open")

    def test_subject_and_description_reflected(self):
        order = self.make_order(CUST_A_ID)
        body = self.valid_body(order.id)
        resp = self.post_my_case(CUST_A_TOKEN, body)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["subject"], body["subject"])
        self.assertEqual(resp.json()["description"], body["description"])


# ---------------------------------------------------------------------------
# Authorization / ownership
# ---------------------------------------------------------------------------

class AuthorizationTests(IntakeTestBase):

    def test_customer_id_in_body_rejected_422(self):
        """customer_id must never come from the browser — extra='forbid'."""
        order = self.make_order(CUST_A_ID)
        body = {**self.valid_body(order.id), "customer_id": str(CUST_A_ID)}
        resp = self.post_my_case(CUST_A_TOKEN, body)
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_cross_customer_order_rejected_403(self):
        """Customer A cannot create a case against Customer B's order."""
        order_b = self.make_order(CUST_B_ID)
        resp = self.post_my_case(CUST_A_TOKEN, self.valid_body(order_b.id))
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_unknown_order_rejected_safely(self):
        """Unknown order_id must not reveal its non-existence to a CUSTOMER — returns 403."""
        resp = self.post_my_case(CUST_A_TOKEN, self.valid_body(uuid4()))
        # CUSTOMER role gets 403 from Authorization._owned() for both missing
        # and cross-owned orders — no information disclosure.
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_admin_cannot_use_my_cases_403(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(ADMIN_TOKEN, self.valid_body(order.id))
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_reviewer_cannot_use_my_cases_403(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(REVIEWER_TOKEN, self.valid_body(order.id))
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_unauthenticated_request_rejected_401(self):
        order = self.make_order(CUST_A_ID)
        body = self.valid_body(order.id)
        resp = self.client.post("/api/my/cases", json=body)
        self.assertEqual(resp.status_code, 401)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class ValidationTests(IntakeTestBase):

    def test_empty_subject_rejected_422(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN,
            {**self.valid_body(order.id), "subject": ""})
        self.assertEqual(resp.status_code, 422)

    def test_whitespace_only_subject_rejected_422(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN,
            {**self.valid_body(order.id), "subject": "   "})
        self.assertEqual(resp.status_code, 422)

    def test_subject_over_200_chars_rejected_422(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN,
            {**self.valid_body(order.id), "subject": "x" * 201})
        self.assertEqual(resp.status_code, 422)

    def test_subject_exactly_200_chars_accepted(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN,
            {**self.valid_body(order.id), "subject": "x" * 200})
        self.assertEqual(resp.status_code, 201)

    def test_missing_subject_rejected_422(self):
        order = self.make_order(CUST_A_ID)
        body = self.valid_body(order.id)
        del body["subject"]
        resp = self.post_my_case(CUST_A_TOKEN, body)
        self.assertEqual(resp.status_code, 422)

    def test_empty_description_rejected_422(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN,
            {**self.valid_body(order.id), "description": ""})
        self.assertEqual(resp.status_code, 422)

    def test_whitespace_only_description_rejected_422(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN,
            {**self.valid_body(order.id), "description": "   "})
        self.assertEqual(resp.status_code, 422)

    def test_description_over_4000_chars_rejected_422(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN,
            {**self.valid_body(order.id), "description": "x" * 4001})
        self.assertEqual(resp.status_code, 422)

    def test_description_exactly_4000_chars_accepted(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN,
            {**self.valid_body(order.id), "description": "x" * 4000})
        self.assertEqual(resp.status_code, 201)

    def test_missing_description_rejected_422(self):
        order = self.make_order(CUST_A_ID)
        body = self.valid_body(order.id)
        del body["description"]
        resp = self.post_my_case(CUST_A_TOKEN, body)
        self.assertEqual(resp.status_code, 422)

    def test_invalid_order_id_non_uuid_rejected_422(self):
        resp = self.post_my_case(CUST_A_TOKEN,
            {**self.valid_body(uuid4()), "order_id": "not-a-uuid"})
        self.assertEqual(resp.status_code, 422)

    def test_missing_order_id_rejected_422(self):
        body = self.valid_body(uuid4())
        del body["order_id"]
        resp = self.post_my_case(CUST_A_TOKEN, body)
        self.assertEqual(resp.status_code, 422)


# ---------------------------------------------------------------------------
# Case visibility
# ---------------------------------------------------------------------------

class VisibilityTests(IntakeTestBase):
    """Cases created via /api/my/cases must be visible through existing routes."""

    def _create_case_a(self):
        order = self.make_order(CUST_A_ID)
        resp = self.post_my_case(CUST_A_TOKEN, self.valid_body(order.id))
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def test_case_visible_to_admin_via_existing_list(self):
        case_id = self._create_case_a()
        resp = self.client.get("/api/cases", headers=self.hdrs(ADMIN_TOKEN))
        self.assertEqual(resp.status_code, 200)
        ids = [c["id"] for c in resp.json()]
        self.assertIn(case_id, ids)

    def test_case_visible_to_owning_customer(self):
        case_id = self._create_case_a()
        resp = self.client.get("/api/cases", headers=self.hdrs(CUST_A_TOKEN))
        self.assertEqual(resp.status_code, 200)
        ids = [c["id"] for c in resp.json()]
        self.assertIn(case_id, ids)

    def test_case_not_visible_to_different_customer(self):
        case_id = self._create_case_a()
        resp = self.client.get("/api/cases", headers=self.hdrs(CUST_B_TOKEN))
        self.assertEqual(resp.status_code, 200)
        ids = [c["id"] for c in resp.json()]
        self.assertNotIn(case_id, ids)

    def test_admin_can_retrieve_case_by_id(self):
        case_id = self._create_case_a()
        resp = self.client.get(f"/api/cases/{case_id}", headers=self.hdrs(ADMIN_TOKEN))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], case_id)


# ---------------------------------------------------------------------------
# Existing POST /api/cases preserved
# ---------------------------------------------------------------------------

class ExistingAdminRoutePreservedTests(IntakeTestBase):
    """The staff POST /api/cases with explicit customer_id must remain unchanged."""

    def test_admin_can_create_case_via_original_endpoint(self):
        order = self.make_order(CUST_A_ID)
        resp = self.client.post("/api/cases",
            headers=self.hdrs(ADMIN_TOKEN),
            json={
                "customer_id": str(CUST_A_ID),
                "order_id": str(order.id),
                "subject": "Admin-created case",
                "description": "Created via the existing staff endpoint.",
            })
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["customer_id"], str(CUST_A_ID))

    def test_existing_endpoint_still_requires_explicit_customer_id(self):
        """The original endpoint without customer_id still returns 422."""
        order = self.make_order(CUST_A_ID)
        resp = self.client.post("/api/cases",
            headers=self.hdrs(ADMIN_TOKEN),
            json={
                "order_id": str(order.id),
                "subject": "Missing customer_id",
                "description": "Should fail.",
            })
        self.assertEqual(resp.status_code, 422)

    def test_customer_cannot_inject_server_fields_in_original_endpoint(self):
        """Customer A supplying Customer B's customer_id → 403 from existing auth."""
        order_b = self.make_order(CUST_B_ID)
        resp = self.client.post("/api/cases",
            headers=self.hdrs(CUST_A_TOKEN),
            json={
                "customer_id": str(CUST_B_ID),
                "order_id": str(order_b.id),
                "subject": "Cross-customer attempt",
                "description": "Should fail.",
            })
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
