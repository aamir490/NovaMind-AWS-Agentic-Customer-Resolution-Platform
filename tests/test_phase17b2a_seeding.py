"""
Phase 17B.2a — Startup Customer Seeding: focused tests.

Coverage matrix
---------------
CaseService.seed_customer()
  ✓ seeded customer is retrievable by exact UUID
  ✓ seeded UUID matches the supplied Customer.id exactly
  ✓ idempotent: seeding the same record twice is a no-op
  ✓ conflict: same id, different name raises ValueError
  ✓ conflict: same id, different UUID raises ValueError
  ✓ seed_customer is not exposed via any HTTP route

create_app(startup_customers=...)
  ✓ no startup_customers → CaseService starts empty (existing behavior)
  ✓ single CUSTOMER startup record seeded correctly
  ✓ multiple startup customers all seeded
  ✓ seeded customer is accessible via GET /api/customers/:id with ADMIN token
  ✓ seeded customer is NOT accessible without authentication (401)
  ✓ startup_customers default is empty tuple — no behavior change

Bootstrap extraction
  ✓ CUSTOMER credential → customer_id seeded as Customer record
  ✓ seeded UUID matches credential customer_id exactly
  ✓ ADMIN credential → no Customer record created
  ✓ REVIEWER credential → no Customer record created
  ✓ no credentials → no startup customers
  ✓ multiple CUSTOMER credentials → all customer_ids seeded
  ✓ customer name is the generic demo label, not from auth config

Customer order APIs after fresh startup (authorization preserved)
  ✓ CUSTOMER with seeded record can call POST /api/my/orders
  ✓ CUSTOMER with seeded record can call GET /api/my/orders
  ✓ CUSTOMER without a seeded record → 404 (customer not found)
  ✓ bearer token alone does not bypass customer_id ownership check

Existing auth/authorization behavior unchanged
  ✓ seeded record does not grant authentication by itself
  ✓ valid ADMIN token still works after seeding
  ✓ create_app() with no arguments is still default-deny (no regressions)
"""

import sys
import os
import json
import tempfile
import unittest
from uuid import UUID, uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from backend.app.domain import Customer
from backend.app.main import create_app
from backend.app.service import CaseService
from backend.app.security import (
    AuthenticatedIdentity,
    LocalAuthenticationProvider,
    LocalCredential,
    Role,
)
from backend.container.bootstrap import _authentication_provider


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

ADMIN_ID   = UUID("00000000-0000-4000-8000-000000000013")
CUST_A_ID  = UUID("00000000-0000-4000-8000-000000000021")
CUST_B_ID  = UUID("00000000-0000-4000-8000-000000000022")
REVIEWER_ID = UUID("00000000-0000-4000-8000-000000000031")

ADMIN_TOKEN    = "phase17b2a-admin-token-0001"
CUSTOMER_A_TOKEN = "phase17b2a-customer-a-tok"
CUSTOMER_B_TOKEN = "phase17b2a-customer-b-tok"
REVIEWER_TOKEN = "phase17b2a-reviewer-tokn1"


def make_provider(*credentials):
    return LocalAuthenticationProvider(tuple(credentials))


def admin_credential():
    return LocalCredential(token=ADMIN_TOKEN,
        identity=AuthenticatedIdentity(user_id=ADMIN_ID, role=Role.ADMIN))


def customer_a_credential():
    return LocalCredential(token=CUSTOMER_A_TOKEN,
        identity=AuthenticatedIdentity(user_id=UUID("00000000-0000-4000-8000-000000000041"),
                                       role=Role.CUSTOMER, customer_id=CUST_A_ID))


def customer_b_credential():
    return LocalCredential(token=CUSTOMER_B_TOKEN,
        identity=AuthenticatedIdentity(user_id=UUID("00000000-0000-4000-8000-000000000042"),
                                       role=Role.CUSTOMER, customer_id=CUST_B_ID))


def reviewer_credential():
    return LocalCredential(token=REVIEWER_TOKEN,
        identity=AuthenticatedIdentity(user_id=REVIEWER_ID, role=Role.REVIEWER))


def app_with_seeded_customer_a():
    """App with admin + customer-A credentials and customer-A seeded."""
    provider = make_provider(admin_credential(), customer_a_credential())
    return create_app(
        auth_provider=provider,
        startup_customers=(Customer(id=CUST_A_ID, name="Demo Customer"),),
    )


def client_for(app, token):
    c = TestClient(app, raise_server_exceptions=True)
    return c, {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# CaseService.seed_customer() unit tests
# ---------------------------------------------------------------------------

class SeedCustomerTests(unittest.TestCase):

    def setUp(self):
        self.svc = CaseService()

    def test_seeded_customer_is_retrievable(self):
        customer = Customer(id=CUST_A_ID, name="Alice")
        self.svc.seed_customer(customer)
        retrieved = self.svc.get_customer(CUST_A_ID)
        self.assertEqual(retrieved.id, CUST_A_ID)
        self.assertEqual(retrieved.name, "Alice")

    def test_seeded_uuid_matches_exactly(self):
        """The seed preserves the supplied UUID — no uuid4() substitution."""
        fixed_id = UUID("aaaaaaaa-bbbb-4000-8000-cccccccccccc")
        self.svc.seed_customer(Customer(id=fixed_id, name="Fixed"))
        self.assertEqual(self.svc.get_customer(fixed_id).id, fixed_id)

    def test_idempotent_same_record(self):
        """Seeding the identical record twice must silently succeed."""
        customer = Customer(id=CUST_A_ID, name="Demo Customer")
        self.svc.seed_customer(customer)
        self.svc.seed_customer(customer)   # must not raise
        self.assertEqual(self.svc.get_customer(CUST_A_ID).name, "Demo Customer")

    def test_conflict_different_name_raises(self):
        """Same id, different name must raise ValueError."""
        self.svc.seed_customer(Customer(id=CUST_A_ID, name="Demo Customer"))
        with self.assertRaises(ValueError):
            self.svc.seed_customer(Customer(id=CUST_A_ID, name="Other Name"))

    def test_conflict_different_id_object_raises(self):
        """
        Two distinct Customer objects with the same id but any differing field
        must raise ValueError.
        """
        original = Customer(id=CUST_A_ID, name="Demo Customer")
        self.svc.seed_customer(original)
        with self.assertRaises(ValueError):
            self.svc.seed_customer(Customer(id=CUST_A_ID, name="Tampered"))

    def test_multiple_customers_seeded_independently(self):
        """Seeding two distinct customers works without interference."""
        ca = Customer(id=CUST_A_ID, name="Alpha")
        cb = Customer(id=CUST_B_ID, name="Beta")
        self.svc.seed_customer(ca)
        self.svc.seed_customer(cb)
        self.assertEqual(self.svc.get_customer(CUST_A_ID).id, CUST_A_ID)
        self.assertEqual(self.svc.get_customer(CUST_B_ID).id, CUST_B_ID)

    def test_seed_customer_not_in_app_routes(self):
        """
        Confirm seed_customer is not reachable via any registered HTTP route.
        None of the registered route paths should expose 'seed'.
        """
        from fastapi.routing import APIRoute
        app = create_app()
        route_paths = [r.path for r in app.router.routes if isinstance(r, APIRoute)]
        for path in route_paths:
            self.assertNotIn("seed", path.lower(),
                f"Route '{path}' must not expose the seed_customer method")


# ---------------------------------------------------------------------------
# create_app(startup_customers=...) integration tests
# ---------------------------------------------------------------------------

class CreateAppStartupCustomersTests(unittest.TestCase):

    def test_no_startup_customers_empty_service(self):
        """Default behavior: no startup_customers → CaseService starts empty."""
        app = create_app(auth_provider=make_provider(admin_credential()))
        svc = app.state.case_service
        # list_orders with no customer_id returns all orders (empty)
        self.assertEqual(svc.list_orders(), [])
        # Trying to get a known UUID fails — no customer was seeded
        from backend.app.service import NotFoundError
        with self.assertRaises(NotFoundError):
            svc.get_customer(CUST_A_ID)

    def test_single_startup_customer_seeded(self):
        """A single startup customer is available immediately after app creation."""
        app = create_app(
            auth_provider=make_provider(admin_credential()),
            startup_customers=(Customer(id=CUST_A_ID, name="Demo Customer"),),
        )
        c = app.state.case_service.get_customer(CUST_A_ID)
        self.assertEqual(c.id, CUST_A_ID)

    def test_multiple_startup_customers_seeded(self):
        """All startup customers are seeded when more than one is provided."""
        app = create_app(
            auth_provider=make_provider(admin_credential()),
            startup_customers=(
                Customer(id=CUST_A_ID, name="Demo Customer"),
                Customer(id=CUST_B_ID, name="Demo Customer"),
            ),
        )
        self.assertEqual(app.state.case_service.get_customer(CUST_A_ID).id, CUST_A_ID)
        self.assertEqual(app.state.case_service.get_customer(CUST_B_ID).id, CUST_B_ID)

    def test_startup_customers_default_empty_tuple(self):
        """startup_customers defaults to () — calling create_app() without it is safe."""
        import inspect
        sig = inspect.signature(create_app)
        default = sig.parameters["startup_customers"].default
        self.assertEqual(default, ())

    def test_seeded_customer_accessible_via_admin_api(self):
        """A seeded customer is retrievable via GET /api/customers/:id with ADMIN token."""
        app = app_with_seeded_customer_a()
        c, hdrs = client_for(app, ADMIN_TOKEN)
        resp = c.get(f"/api/customers/{CUST_A_ID}", headers=hdrs)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], str(CUST_A_ID))

    def test_seeded_customer_inaccessible_without_auth(self):
        """Seeded records are NOT accessible without a valid bearer token."""
        app = app_with_seeded_customer_a()
        tc = TestClient(app, raise_server_exceptions=True)
        resp = tc.get(f"/api/customers/{CUST_A_ID}")
        self.assertEqual(resp.status_code, 401)

    def test_seeded_record_does_not_grant_authentication(self):
        """
        The Customer domain record alone grants nothing.
        Using a random/unknown bearer token still returns 401.
        """
        app = app_with_seeded_customer_a()
        tc = TestClient(app, raise_server_exceptions=True)
        resp = tc.get(f"/api/customers/{CUST_A_ID}",
                      headers={"Authorization": "Bearer not-a-registered-token-x"})
        self.assertEqual(resp.status_code, 401)


# ---------------------------------------------------------------------------
# Bootstrap _authentication_provider extraction tests
# ---------------------------------------------------------------------------

class BootstrapExtractionTests(unittest.TestCase):
    """
    Tests for _authentication_provider() in bootstrap.py.
    Uses a real temporary JSON file to exercise the actual file-reading path.
    """

    def _write_auth_file(self, records):
        """Write records to a temp file and return its absolute path."""
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(records, tmp)
        tmp.close()
        return tmp.name

    def _auth_record(self, token, role, user_id=None, customer_id=None):
        identity = {"user_id": str(user_id or uuid4()), "role": role}
        if customer_id:
            identity["customer_id"] = str(customer_id)
        return {"token": token, "identity": identity}

    def test_no_env_var_returns_none_and_empty_tuple(self):
        """Absent NOVAMIND_AUTH_FILE returns (None, ())."""
        old = os.environ.pop("NOVAMIND_AUTH_FILE", None)
        try:
            provider, customers = _authentication_provider()
            self.assertIsNone(provider)
            self.assertEqual(customers, ())
        finally:
            if old is not None:
                os.environ["NOVAMIND_AUTH_FILE"] = old

    def test_customer_credential_produces_startup_customer(self):
        """A CUSTOMER credential → one startup Customer with the matching id."""
        path = self._write_auth_file([
            self._auth_record("phase17-bootstrap-cust1", "CUSTOMER", customer_id=CUST_A_ID),
        ])
        try:
            os.environ["NOVAMIND_AUTH_FILE"] = path
            provider, customers = _authentication_provider()
            self.assertEqual(len(customers), 1)
            self.assertEqual(customers[0].id, CUST_A_ID)
        finally:
            del os.environ["NOVAMIND_AUTH_FILE"]
            os.unlink(path)

    def test_seeded_uuid_matches_credential_customer_id_exactly(self):
        """The seeded Customer.id is the same UUID object as identity.customer_id."""
        path = self._write_auth_file([
            self._auth_record("phase17-bootstrap-cust2", "CUSTOMER", customer_id=CUST_A_ID),
        ])
        try:
            os.environ["NOVAMIND_AUTH_FILE"] = path
            _, customers = _authentication_provider()
            self.assertEqual(customers[0].id, CUST_A_ID)
        finally:
            del os.environ["NOVAMIND_AUTH_FILE"]
            os.unlink(path)

    def test_admin_credential_produces_no_startup_customer(self):
        """An ADMIN credential must NOT produce a startup Customer record."""
        path = self._write_auth_file([
            self._auth_record(ADMIN_TOKEN, "ADMIN", user_id=ADMIN_ID),
        ])
        try:
            os.environ["NOVAMIND_AUTH_FILE"] = path
            _, customers = _authentication_provider()
            self.assertEqual(customers, ())
        finally:
            del os.environ["NOVAMIND_AUTH_FILE"]
            os.unlink(path)

    def test_reviewer_credential_produces_no_startup_customer(self):
        """A REVIEWER credential must NOT produce a startup Customer record."""
        path = self._write_auth_file([
            self._auth_record(REVIEWER_TOKEN, "REVIEWER", user_id=REVIEWER_ID),
        ])
        try:
            os.environ["NOVAMIND_AUTH_FILE"] = path
            _, customers = _authentication_provider()
            self.assertEqual(customers, ())
        finally:
            del os.environ["NOVAMIND_AUTH_FILE"]
            os.unlink(path)

    def test_mixed_credentials_only_customer_seeded(self):
        """ADMIN + CUSTOMER → exactly one startup customer (the CUSTOMER only)."""
        path = self._write_auth_file([
            self._auth_record(ADMIN_TOKEN, "ADMIN", user_id=ADMIN_ID),
            self._auth_record(CUSTOMER_A_TOKEN, "CUSTOMER", customer_id=CUST_A_ID),
        ])
        try:
            os.environ["NOVAMIND_AUTH_FILE"] = path
            _, customers = _authentication_provider()
            self.assertEqual(len(customers), 1)
            self.assertEqual(customers[0].id, CUST_A_ID)
        finally:
            del os.environ["NOVAMIND_AUTH_FILE"]
            os.unlink(path)

    def test_multiple_customer_credentials_all_seeded(self):
        """Two CUSTOMER credentials → two startup customers."""
        path = self._write_auth_file([
            self._auth_record(CUSTOMER_A_TOKEN, "CUSTOMER", customer_id=CUST_A_ID),
            self._auth_record(CUSTOMER_B_TOKEN, "CUSTOMER", customer_id=CUST_B_ID),
        ])
        try:
            os.environ["NOVAMIND_AUTH_FILE"] = path
            _, customers = _authentication_provider()
            ids = {c.id for c in customers}
            self.assertEqual(ids, {CUST_A_ID, CUST_B_ID})
        finally:
            del os.environ["NOVAMIND_AUTH_FILE"]
            os.unlink(path)

    def test_customer_name_is_generic_demo_label(self):
        """The seeded customer name is the generic demo label, not anything from credentials."""
        path = self._write_auth_file([
            self._auth_record(CUSTOMER_A_TOKEN, "CUSTOMER", customer_id=CUST_A_ID),
        ])
        try:
            os.environ["NOVAMIND_AUTH_FILE"] = path
            _, customers = _authentication_provider()
            # Name must be the generic label — no business name comes from auth config.
            self.assertEqual(customers[0].name, "Demo Customer")
        finally:
            del os.environ["NOVAMIND_AUTH_FILE"]
            os.unlink(path)

    def test_empty_credential_list_returns_empty_startup_customers(self):
        """An empty auth file returns no startup customers."""
        path = self._write_auth_file([])
        try:
            os.environ["NOVAMIND_AUTH_FILE"] = path
            provider, customers = _authentication_provider()
            self.assertIsNotNone(provider)
            self.assertEqual(customers, ())
        finally:
            del os.environ["NOVAMIND_AUTH_FILE"]
            os.unlink(path)


# ---------------------------------------------------------------------------
# Customer order API access after fresh startup
# ---------------------------------------------------------------------------

class CustomerOrderApiAfterSeedingTests(unittest.TestCase):
    """
    These tests verify the full end-to-end: credential is present in the auth
    provider AND customer record is seeded, so the customer-safe order APIs work
    without any manual POST /api/customers step.
    """

    @classmethod
    def setUpClass(cls):
        provider = make_provider(admin_credential(), customer_a_credential())
        cls.app = create_app(
            auth_provider=provider,
            startup_customers=(Customer(id=CUST_A_ID, name="Demo Customer"),),
        )
        cls.client = TestClient(cls.app, raise_server_exceptions=True)

    def hdrs(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_customer_can_list_my_orders_after_startup(self):
        """GET /api/my/orders succeeds immediately after startup — no manual setup needed."""
        resp = self.client.get("/api/my/orders", headers=self.hdrs(CUSTOMER_A_TOKEN))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])   # empty, but not an error

    def test_customer_can_place_order_after_startup(self):
        """POST /api/my/orders succeeds using the seeded customer record."""
        resp = self.client.post("/api/my/orders",
            headers=self.hdrs(CUSTOMER_A_TOKEN),
            json={"items": [{"sku": "LAP-1", "quantity": 1}]})
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        # customer_id in response must match the seeded UUID
        self.assertEqual(body["customer_id"], str(CUST_A_ID))
        self.assertEqual(body["items"][0]["sku"], "LAP-1")

    def test_order_appears_in_my_orders_after_placement(self):
        """Orders placed appear in GET /api/my/orders."""
        self.client.post("/api/my/orders",
            headers=self.hdrs(CUSTOMER_A_TOKEN),
            json={"items": [{"sku": "ACC-1", "quantity": 2}]})
        resp = self.client.get("/api/my/orders", headers=self.hdrs(CUSTOMER_A_TOKEN))
        self.assertEqual(resp.status_code, 200)
        skus = [item["sku"] for order in resp.json() for item in order["items"]]
        self.assertIn("ACC-1", skus)

    def test_customer_without_seeded_record_gets_404(self):
        """
        A CUSTOMER token whose customer_id was NOT seeded at startup
        correctly gets 404 when trying to list orders.
        """
        # customer-B has a valid token but NO seeded Customer record in this app.
        provider = make_provider(admin_credential(), customer_b_credential())
        app = create_app(
            auth_provider=provider,
            startup_customers=(),  # deliberately empty — no seeding
        )
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.get("/api/my/orders",
                     headers={"Authorization": f"Bearer {CUSTOMER_B_TOKEN}"})
        self.assertEqual(resp.status_code, 404)

    def test_customer_id_ownership_enforced_after_seeding(self):
        """
        Customer A cannot access Customer B's resources even after both are seeded.
        The ownership check is against the live record, not the token alone.
        """
        provider = make_provider(
            admin_credential(), customer_a_credential(), customer_b_credential()
        )
        app = create_app(
            auth_provider=provider,
            startup_customers=(
                Customer(id=CUST_A_ID, name="Demo Customer"),
                Customer(id=CUST_B_ID, name="Demo Customer"),
            ),
        )
        tc = TestClient(app, raise_server_exceptions=True)
        # Customer B places an order
        resp = tc.post("/api/my/orders",
            headers={"Authorization": f"Bearer {CUSTOMER_B_TOKEN}"},
            json={"items": [{"sku": "WAR-1", "quantity": 1}]})
        self.assertEqual(resp.status_code, 201)
        order_id = resp.json()["id"]

        # Customer A tries to access Customer B's order via admin endpoint
        resp_a = tc.get(f"/api/orders/{order_id}",
                        headers={"Authorization": f"Bearer {CUSTOMER_A_TOKEN}"})
        # Must be denied — 403 from ownership check
        self.assertEqual(resp_a.status_code, 403)


# ---------------------------------------------------------------------------
# Existing auth/authorization not weakened
# ---------------------------------------------------------------------------

class AuthNotWeakenedTests(unittest.TestCase):

    def test_default_deny_with_no_args(self):
        """create_app() with no arguments remains fully default-deny."""
        app = create_app()
        tc = TestClient(app, raise_server_exceptions=True)
        resp = tc.get("/api/customers/" + str(CUST_A_ID))
        self.assertEqual(resp.status_code, 401)

    def test_admin_token_still_works_after_seeding(self):
        """Adding startup customers does not break ADMIN authentication."""
        app = app_with_seeded_customer_a()
        tc = TestClient(app, raise_server_exceptions=True)
        resp = tc.post("/api/customers",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"name": "New Customer"})
        self.assertEqual(resp.status_code, 201)

    def test_seeded_record_alone_cannot_authenticate(self):
        """
        Even if a customer_id UUID is known, an invalid/unknown bearer token
        cannot authenticate — domain records do not grant access.
        """
        app = create_app(
            auth_provider=make_provider(admin_credential()),
            startup_customers=(Customer(id=CUST_A_ID, name="Demo Customer"),),
        )
        tc = TestClient(app, raise_server_exceptions=True)
        for bad_token in ("", "short", "x" * 257):
            resp = tc.get(f"/api/customers/{CUST_A_ID}",
                headers={"Authorization": f"Bearer {bad_token}"} if bad_token else {})
            self.assertIn(resp.status_code, (401, 422),
                f"Expected auth failure for token={bad_token!r}, got {resp.status_code}")

    def test_health_endpoint_unaffected(self):
        """Health endpoint remains publicly accessible regardless of seeding."""
        app = create_app(
            startup_customers=(Customer(id=CUST_A_ID, name="Demo Customer"),),
        )
        tc = TestClient(app, raise_server_exceptions=True)
        resp = tc.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok", "service": "novamind-api"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
