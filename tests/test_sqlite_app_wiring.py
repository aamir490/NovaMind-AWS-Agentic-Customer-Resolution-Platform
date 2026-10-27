"""Opt-in FastAPI SQLite composition tests; no container or cloud runtime."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID

from fastapi.testclient import TestClient

from backend.app.domain import Customer, ResolutionAction
from backend.app.main import create_app
from backend.app.persistence import SQLitePersistence
from backend.app.schemas import CaseCreate, OrderCreate
from backend.app.security import (
    AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role,
)


ADMIN_ID = UUID("00000000-0000-4000-8000-000000000051")
CUSTOMER_ID = UUID("00000000-0000-4000-8000-000000000052")
ADMIN_TOKEN = "sqlite-app-admin-token"
CUSTOMER_TOKEN = "sqlite-app-customer-token"


class SQLiteAppWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "novamind.sqlite"
        self.customer = Customer(id=CUSTOMER_ID, name="Persistent customer")
        self.auth = LocalAuthenticationProvider((
            LocalCredential(
                token=ADMIN_TOKEN,
                identity=AuthenticatedIdentity(user_id=ADMIN_ID, role=Role.ADMIN),
            ),
            LocalCredential(
                token=CUSTOMER_TOKEN,
                identity=AuthenticatedIdentity(
                    user_id=UUID("00000000-0000-4000-8000-000000000053"),
                    role=Role.CUSTOMER, customer_id=CUSTOMER_ID,
                ),
            ),
        ))

    def app(self):
        return create_app(
            auth_provider=self.auth,
            sqlite_path=self.path,
            startup_customers=(self.customer,),
        )

    def test_default_app_remains_in_memory(self):
        app = create_app()
        self.assertIsNone(app.state.sqlite_persistence)
        app.state.frontend_runtime.close()

    def test_sqlite_app_recovers_cases_and_decisions_after_restart(self):
        first = self.app()
        with TestClient(first):
            service = first.state.case_service
            order = service.create_order(OrderCreate(
                customer_id=CUSTOMER_ID,
                items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
            ))
            support_case = service.create_case(CaseCreate(
                customer_id=CUSTOMER_ID, order_id=order.id,
                subject="Replacement request", description="The item arrived damaged.",
            ))
            in_review, decision = service.resolve_case(
                support_case.id, action=ResolutionAction.REQUEST_MORE_EVIDENCE,
                reason="Additional photos are required.",
                customer_message="Please upload clear photos of the damaged item.",
                admin_user_id=ADMIN_ID, evidence_reviewed=False,
            )

        restarted = self.app()
        with TestClient(restarted) as client:
            service = restarted.state.case_service
            self.assertEqual(service.get_order(order.id), order)
            self.assertEqual(service.get_case(support_case.id), in_review)
            self.assertEqual(service.resolution_history(support_case.id), (decision,))
            customer_response = client.get(
                f"/api/cases/{support_case.id}/resolution",
                headers={"Authorization": f"Bearer {CUSTOMER_TOKEN}"},
            )
            self.assertEqual(customer_response.status_code, 200, customer_response.text)
            self.assertEqual(customer_response.json()["latest"]["action"], "REQUEST_MORE_EVIDENCE")
            self.assertNotIn("admin_user_id", customer_response.json()["latest"])

        self.assertIsInstance(restarted.state.sqlite_persistence, SQLitePersistence)

    def test_opt_in_path_must_be_absolute(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            create_app(sqlite_path="novamind.sqlite")


if __name__ == "__main__":
    unittest.main()
