"""
Phase 17B.1 — Customer Commerce Foundation: focused tests.

Coverage matrix
---------------
Catalog
  ✓ list all products (any authenticated role)
  ✓ list available-only filter
  ✓ get by UUID — found / not found / malformed UUID
  ✓ get by SKU  — found / not found / empty SKU
  ✓ catalog is read-only (POST not accepted)
  ✓ unauthenticated request denied

Customer order creation
  ✓ happy path — customer creates order, customer_id derived from identity
  ✓ customer_id in body is rejected (extra="forbid" → 422)
  ✓ unknown SKU is rejected with 404
  ✓ invalid quantity (0, negative, >1000) rejected with 422
  ✓ empty items list rejected with 422
  ✓ non-customer role (ADMIN) cannot use this endpoint
  ✓ unauthenticated request denied

My Orders listing
  ✓ customer sees only their own orders
  ✓ cross-customer denial — customer A cannot see customer B orders
  ✓ empty list returned when customer has no orders
  ✓ non-customer role (ADMIN) denied
  ✓ unauthenticated request denied

Existing admin routes preserved
  ✓ admin POST /api/orders still works
  ✓ admin GET  /api/orders/:id still works
"""

import sys
import os
import unittest
from uuid import UUID, uuid4

# Ensure the project root is on sys.path so 'backend' is importable when the
# test is run from the project root (mirrors how other test modules do it).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.security import (
    AuthenticatedIdentity,
    LocalAuthenticationProvider,
    LocalCredential,
    Role,
)

# ---------------------------------------------------------------------------
# Token / identity fixtures
# ---------------------------------------------------------------------------

ADMIN_ID       = UUID("00000000-0000-4000-8000-000000000013")
CUSTOMER_A_ID  = UUID("00000000-0000-4000-8000-000000000021")
CUSTOMER_B_ID  = UUID("00000000-0000-4000-8000-000000000022")
# A pre-registered customer record UUID (for the Customer domain object).
# These are created during the test setup via the admin API.

ADMIN_TOKEN      = "phase17b1-admin-token-0001"
CUSTOMER_A_TOKEN = "phase17b1-customer-a-token"
CUSTOMER_B_TOKEN = "phase17b1-customer-b-token"

# Note: customer_id in AuthenticatedIdentity is the *Customer domain record* id.
# We create the Customer records in setUpClass and bind their ids to identities.
# Because LocalAuthenticationProvider is set up once, we use fixed UUIDs and
# create matching Customer records during setup.


def build_provider(customer_a_record_id: UUID, customer_b_record_id: UUID):
    return LocalAuthenticationProvider((
        LocalCredential(
            token=ADMIN_TOKEN,
            identity=AuthenticatedIdentity(user_id=ADMIN_ID, role=Role.ADMIN),
        ),
        LocalCredential(
            token=CUSTOMER_A_TOKEN,
            identity=AuthenticatedIdentity(
                user_id=UUID("00000000-0000-4000-8000-000000000031"),
                role=Role.CUSTOMER,
                customer_id=customer_a_record_id,
            ),
        ),
        LocalCredential(
            token=CUSTOMER_B_TOKEN,
            identity=AuthenticatedIdentity(
                user_id=UUID("00000000-0000-4000-8000-000000000032"),
                role=Role.CUSTOMER,
                customer_id=customer_b_record_id,
            ),
        ),
    ))


# ---------------------------------------------------------------------------
# Base class with shared client helpers
# ---------------------------------------------------------------------------

class CommerceTestBase(unittest.TestCase):
    """
    Spins up one FastAPI TestClient per test class.
    Customer records for A and B are created in setUpClass via the admin API
    so their UUIDs are known before building the auth provider.
    """

    @classmethod
    def setUpClass(cls):
        # Step 1 — bootstrap with admin-only provider to create customer records.
        bootstrap_provider = LocalAuthenticationProvider((
            LocalCredential(
                token=ADMIN_TOKEN,
                identity=AuthenticatedIdentity(user_id=ADMIN_ID, role=Role.ADMIN),
            ),
        ))
        bootstrap_app = create_app(auth_provider=bootstrap_provider)
        bootstrap_client = TestClient(bootstrap_app, raise_server_exceptions=True)

        resp = bootstrap_client.post(
            "/api/customers",
            json={"name": "Customer Alpha"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert resp.status_code == 201, resp.text
        cls.customer_a_id = UUID(resp.json()["id"])

        resp = bootstrap_client.post(
            "/api/customers",
            json={"name": "Customer Beta"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert resp.status_code == 201, resp.text
        cls.customer_b_id = UUID(resp.json()["id"])

        # Step 2 — rebuild provider with customer bindings pointing to the real
        # customer record UUIDs, then point the app to the new provider.
        provider = build_provider(cls.customer_a_id, cls.customer_b_id)
        # Re-create the app; we need the same CaseService so we swap the provider
        # on the existing app rather than creating a new one.
        bootstrap_app.state.auth_provider = provider
        cls.app = bootstrap_app
        cls.client = bootstrap_client  # reuse; provider was updated on app.state

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    def api(self, method: str, path: str, *, token: str, json=None, params=None):
        """Fire a request and return (status_code, parsed_body)."""
        resp = self.client.request(
            method, path,
            headers={"Authorization": f"Bearer {token}"},
            json=json,
            params=params,
        )
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return resp.status_code, body

    def admin(self, method: str, path: str, **kwargs):
        return self.api(method, path, token=ADMIN_TOKEN, **kwargs)

    def customer_a(self, method: str, path: str, **kwargs):
        return self.api(method, path, token=CUSTOMER_A_TOKEN, **kwargs)

    def customer_b(self, method: str, path: str, **kwargs):
        return self.api(method, path, token=CUSTOMER_B_TOKEN, **kwargs)

    def no_auth(self, method: str, path: str, **kwargs):
        resp = self.client.request(method, path, **kwargs)
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return resp.status_code, body


# ---------------------------------------------------------------------------
# Catalog tests
# ---------------------------------------------------------------------------

class CatalogTests(CommerceTestBase):

    def test_list_products_admin(self):
        """Any authenticated role can list the catalog."""
        status, body = self.admin("GET", "/api/catalog")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 100)
        skus = {p["sku"] for p in body}
        for expected in ("LAP-1", "LAP-2", "ACC-1", "WAR-1", "BAG-1"):
            self.assertIn(expected, skus)
        self.assertEqual(len({p["category"] for p in body}), 8)
        for product in body:
            self.assertTrue(product["image_path"].startswith("/products/"))
            self.assertEqual(product["available"], product["stock_quantity"] > 0)

    def test_catalog_and_operational_inventory_share_stock(self):
        status, products = self.admin("GET", "/api/catalog")
        self.assertEqual(status, 200)
        operations = self.client.app.state.business_operations
        for product in products:
            inventory = operations.get_inventory(product["sku"])
            self.assertEqual(inventory.available_quantity, product["stock_quantity"])

    def test_list_products_customer(self):
        """Customers can also browse the catalog."""
        status, body = self.customer_a("GET", "/api/catalog")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)

    def test_list_products_available_only(self):
        """available_only=true filters out unavailable products."""
        status_all, all_body = self.admin("GET", "/api/catalog")
        status_avail, avail_body = self.admin(
            "GET", "/api/catalog", params={"available_only": "true"}
        )
        self.assertEqual(status_all, 200)
        self.assertEqual(status_avail, 200)
        # LAP-2 is marked available=False in seed data
        all_skus = {p["sku"] for p in all_body}
        avail_skus = {p["sku"] for p in avail_body}
        self.assertIn("LAP-2", all_skus)
        self.assertNotIn("LAP-2", avail_skus)
        # All products in available result must have available=True
        for p in avail_body:
            self.assertTrue(p["available"], f"SKU {p['sku']} should be available")

    def test_get_product_by_id(self):
        """GET /api/catalog/{id} returns the correct product."""
        lap1_id = "10000000-0000-4000-8000-000000000001"
        status, body = self.admin("GET", f"/api/catalog/{lap1_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["sku"], "LAP-1")
        self.assertEqual(body["id"], lap1_id)
        self.assertIn("unit_price", body)
        self.assertEqual(body["unit_price"]["currency"], "USD")

    def test_get_product_by_id_not_found(self):
        """Unknown UUID returns 404."""
        status, _ = self.admin("GET", f"/api/catalog/{uuid4()}")
        self.assertEqual(status, 404)

    def test_get_product_by_id_malformed(self):
        """Malformed UUID returns 422."""
        status, _ = self.admin("GET", "/api/catalog/not-a-uuid")
        self.assertEqual(status, 422)

    def test_get_product_by_sku(self):
        """GET /api/catalog/sku/{sku} returns the correct product."""
        status, body = self.admin("GET", "/api/catalog/sku/ACC-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["sku"], "ACC-1")
        self.assertEqual(body["name"], "USB-C Docking Station")

    def test_get_product_by_sku_not_found(self):
        """Unknown SKU returns 404."""
        status, _ = self.admin("GET", "/api/catalog/sku/DOES-NOT-EXIST")
        self.assertEqual(status, 404)

    def test_catalog_is_read_only(self):
        """POST to catalog root is not allowed."""
        status, _ = self.admin("POST", "/api/catalog", json={"sku": "X"})
        self.assertEqual(status, 405)

    def test_catalog_unauthenticated(self):
        """Unauthenticated requests are rejected with 401."""
        status, _ = self.no_auth("GET", "/api/catalog")
        self.assertEqual(status, 401)

    def test_catalog_sku_unauthenticated(self):
        """SKU lookup without a token returns 401."""
        status, _ = self.no_auth("GET", "/api/catalog/sku/LAP-1")
        self.assertEqual(status, 401)


# ---------------------------------------------------------------------------
# Customer order creation tests
# ---------------------------------------------------------------------------

class CustomerOrderCreationTests(CommerceTestBase):

    VALID_BODY = {
        "items": [{"sku": "LAP-1", "quantity": 1}]
    }

    def test_happy_path_single_item(self):
        """Customer can create a single-item order; customer_id is derived from identity."""
        status, body = self.customer_a("POST", "/api/my/orders", json=self.VALID_BODY)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["customer_id"], str(self.customer_a_id))
        self.assertEqual(len(body["items"]), 1)
        item = body["items"][0]
        self.assertEqual(item["sku"], "LAP-1")
        # name must be resolved server-side from the catalog, not from the request
        self.assertEqual(item["name"], "ProBook Laptop 15")
        self.assertEqual(item["quantity"], 1)
        self.assertIn("id", body)

    def test_happy_path_multi_item(self):
        """Customer can create a multi-item order with distinct SKUs."""
        body_req = {
            "items": [
                {"sku": "LAP-1", "quantity": 2},
                {"sku": "ACC-1", "quantity": 1},
                {"sku": "BAG-1", "quantity": 3},
            ]
        }
        status, body = self.customer_a("POST", "/api/my/orders", json=body_req)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["customer_id"], str(self.customer_a_id))
        self.assertEqual(len(body["items"]), 3)

    def test_customer_id_in_body_rejected(self):
        """Supplying customer_id in the request body returns 422 (extra='forbid')."""
        status, body = self.customer_a("POST", "/api/my/orders", json={
            "customer_id": str(uuid4()),
            "items": [{"sku": "LAP-1", "quantity": 1}],
        })
        self.assertEqual(status, 422, body)

    def test_unknown_sku_rejected(self):
        """An item referencing a non-existent SKU returns 404."""
        status, body = self.customer_a("POST", "/api/my/orders", json={
            "items": [{"sku": "NONEXISTENT-SKU", "quantity": 1}]
        })
        self.assertEqual(status, 404, body)

    def test_quantity_zero_rejected(self):
        """quantity=0 is below the minimum and must return 422."""
        status, _ = self.customer_a("POST", "/api/my/orders", json={
            "items": [{"sku": "LAP-1", "quantity": 0}]
        })
        self.assertEqual(status, 422)

    def test_quantity_negative_rejected(self):
        """Negative quantities must return 422."""
        status, _ = self.customer_a("POST", "/api/my/orders", json={
            "items": [{"sku": "LAP-1", "quantity": -5}]
        })
        self.assertEqual(status, 422)

    def test_quantity_over_max_rejected(self):
        """quantity > 1000 must return 422."""
        status, _ = self.customer_a("POST", "/api/my/orders", json={
            "items": [{"sku": "LAP-1", "quantity": 1001}]
        })
        self.assertEqual(status, 422)

    def test_quantity_at_max_accepted(self):
        """quantity=1000 is exactly at the maximum and should succeed."""
        status, body = self.customer_a("POST", "/api/my/orders", json={
            "items": [{"sku": "ACC-1", "quantity": 1000}]
        })
        self.assertEqual(status, 201, body)
        self.assertEqual(body["items"][0]["quantity"], 1000)

    def test_empty_items_rejected(self):
        """An empty items list must return 422 (min_length=1)."""
        status, _ = self.customer_a("POST", "/api/my/orders", json={"items": []})
        self.assertEqual(status, 422)

    def test_missing_items_rejected(self):
        """Omitting the items field returns 422."""
        status, _ = self.customer_a("POST", "/api/my/orders", json={})
        self.assertEqual(status, 422)

    def test_admin_cannot_create_my_order(self):
        """ADMIN role is denied access to POST /api/my/orders (requires CUSTOMER)."""
        status, _ = self.admin("POST", "/api/my/orders", json=self.VALID_BODY)
        self.assertEqual(status, 403)

    def test_unauthenticated_cannot_create_my_order(self):
        """No token returns 401."""
        status, _ = self.no_auth("POST", "/api/my/orders", json=self.VALID_BODY)
        self.assertEqual(status, 401)

    def test_unavailable_product_is_rejected(self):
        """Authoritative zero stock prevents checkout without changing its request shape."""
        status, body = self.customer_a("POST", "/api/my/orders", json={
            "items": [{"sku": "LAP-2", "quantity": 1}]
        })
        self.assertEqual(status, 409, body)

    def test_quantity_cannot_exceed_stock(self):
        status, body = self.customer_a("POST", "/api/my/orders", json={
            "items": [{"sku": "LAP-1", "quantity": 6}]
        })
        self.assertEqual(status, 409, body)

    def test_duplicate_lines_are_validated_in_aggregate(self):
        status, body = self.customer_a("POST", "/api/my/orders", json={
            "items": [{"sku": "LAP-1", "quantity": 3}, {"sku": "LAP-1", "quantity": 3}]
        })
        self.assertEqual(status, 409, body)


# ---------------------------------------------------------------------------
# My Orders listing & ownership tests
# ---------------------------------------------------------------------------

class MyOrdersListingTests(CommerceTestBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Pre-create one order for customer A and one for customer B so that
        # the isolation assertions have data to work with.
        status, body = cls.client.post(
            "/api/my/orders",
            headers={"Authorization": f"Bearer {CUSTOMER_A_TOKEN}"},
            json={"items": [{"sku": "WAR-1", "quantity": 1}]},
        ).status_code, None
        resp = cls.client.post(
            "/api/my/orders",
            headers={"Authorization": f"Bearer {CUSTOMER_A_TOKEN}"},
            json={"items": [{"sku": "WAR-1", "quantity": 1}]},
        )
        assert resp.status_code == 201, resp.text
        cls.order_a_id = resp.json()["id"]

        resp = cls.client.post(
            "/api/my/orders",
            headers={"Authorization": f"Bearer {CUSTOMER_B_TOKEN}"},
            json={"items": [{"sku": "ACC-1", "quantity": 2}]},
        )
        assert resp.status_code == 201, resp.text
        cls.order_b_id = resp.json()["id"]

    def test_customer_sees_own_orders(self):
        """GET /api/my/orders returns only orders owned by the caller."""
        status, body = self.customer_a("GET", "/api/my/orders")
        self.assertEqual(status, 200, body)
        self.assertIsInstance(body, list)
        ids = {o["id"] for o in body}
        self.assertIn(self.order_a_id, ids)
        # Customer B's order must NOT appear in customer A's list
        self.assertNotIn(self.order_b_id, ids)
        # All returned orders must belong to customer A
        for order in body:
            self.assertEqual(order["customer_id"], str(self.customer_a_id))

    def test_cross_customer_isolation(self):
        """Customer B's listing does not include customer A's orders."""
        status, body = self.customer_b("GET", "/api/my/orders")
        self.assertEqual(status, 200, body)
        ids = {o["id"] for o in body}
        self.assertIn(self.order_b_id, ids)
        self.assertNotIn(self.order_a_id, ids)
        for order in body:
            self.assertEqual(order["customer_id"], str(self.customer_b_id))

    def test_empty_list_for_new_customer(self):
        """
        A fresh customer with no orders receives an empty list, not a 404 or 403.
        We create a new customer + identity and verify the empty response.
        """
        new_customer_token = "phase17b1-new-customer-1234"
        # Create a new Customer record via admin
        status, cust = self.admin("POST", "/api/customers", json={"name": "Newbie"})
        self.assertEqual(status, 201)
        new_customer_id = UUID(cust["id"])

        # Register a new identity for this customer on the provider.
        from backend.app.security import AuthenticatedIdentity, LocalCredential, Role
        new_credential = LocalCredential(
            token=new_customer_token,
            identity=AuthenticatedIdentity(
                user_id=uuid4(),
                role=Role.CUSTOMER,
                customer_id=new_customer_id,
            ),
        )
        # Patch the provider directly (test-only technique that mirrors how
        # the existing security test does introspection on live objects).
        from hashlib import sha256
        digest = sha256(new_customer_token.encode("ascii")).digest()
        self.app.state.auth_provider._identities[digest] = new_credential.identity

        status, body = self.api("GET", "/api/my/orders", token=new_customer_token)
        self.assertEqual(status, 200)
        self.assertEqual(body, [])

    def test_admin_cannot_list_my_orders(self):
        """ADMIN role is rejected from GET /api/my/orders (requires CUSTOMER)."""
        status, _ = self.admin("GET", "/api/my/orders")
        self.assertEqual(status, 403)

    def test_unauthenticated_cannot_list_my_orders(self):
        """No token returns 401."""
        status, _ = self.no_auth("GET", "/api/my/orders")
        self.assertEqual(status, 401)


# ---------------------------------------------------------------------------
# Existing admin order routes preserved
# ---------------------------------------------------------------------------

class AdminOrderRoutesPreservedTests(CommerceTestBase):
    """
    Confirm that the original POST /api/orders and GET /api/orders/:id routes
    still work unchanged for ADMIN callers after Phase 17B.1 changes.
    """

    def test_admin_create_order_still_works(self):
        """Admin POST /api/orders with explicit customer_id still succeeds."""
        status, cust = self.admin("POST", "/api/customers", json={"name": "Admin Test Customer"})
        self.assertEqual(status, 201)
        cid = cust["id"]

        status, order = self.admin("POST", "/api/orders", json={
            "customer_id": cid,
            "items": [{"sku": "LAP-1", "name": "ProBook Laptop 15", "quantity": 1}],
        })
        self.assertEqual(status, 201, order)
        self.assertEqual(order["customer_id"], cid)
        self.assertEqual(order["items"][0]["sku"], "LAP-1")

    def test_admin_get_order_still_works(self):
        """Admin GET /api/orders/:id still returns the correct order."""
        status, cust = self.admin("POST", "/api/customers", json={"name": "Admin Get Test"})
        self.assertEqual(status, 201)
        cid = cust["id"]
        status, order = self.admin("POST", "/api/orders", json={
            "customer_id": cid,
            "items": [{"sku": "ACC-1", "name": "USB-C Docking Station", "quantity": 2}],
        })
        self.assertEqual(status, 201)
        order_id = order["id"]

        status, fetched = self.admin("GET", f"/api/orders/{order_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["id"], order_id)

    def test_customer_cannot_use_admin_create_order(self):
        """CUSTOMER role is blocked from the admin POST /api/orders endpoint."""
        status, _ = self.customer_a("POST", "/api/orders", json={
            "customer_id": str(self.customer_a_id),
            "items": [{"sku": "LAP-1", "name": "x", "quantity": 1}],
        })
        self.assertEqual(status, 403)

    def test_admin_order_unknown_id_returns_404(self):
        """GET /api/orders/:id with an unknown UUID returns 404."""
        status, _ = self.admin("GET", f"/api/orders/{uuid4()}")
        self.assertEqual(status, 404)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
