"""Offline ADMIN-approved customer communications; temporary SQLite only."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from backend.app.domain import CaseStatus, Customer
from backend.app.main import create_app
from backend.app.persistence import PersistenceError
from backend.app.schemas import CaseCreate, OrderCreate
from backend.app.security import (
    AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role,
)


ADMIN_ID = UUID("00000000-0000-4000-8000-000000000101")
REVIEWER_ID = UUID("00000000-0000-4000-8000-000000000102")
CUSTOMER_A_ID = UUID("00000000-0000-4000-8000-000000000103")
CUSTOMER_B_ID = UUID("00000000-0000-4000-8000-000000000104")
CUSTOMER_A_USER_ID = UUID("00000000-0000-4000-8000-000000000105")
CUSTOMER_B_USER_ID = UUID("00000000-0000-4000-8000-000000000106")

ADMIN_TOKEN = "customer-guidance-admin-token"
REVIEWER_TOKEN = "customer-guidance-reviewer-token"
CUSTOMER_A_TOKEN = "customer-guidance-customer-a"
CUSTOMER_B_TOKEN = "customer-guidance-customer-b"


class CustomerCommunicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "novamind.sqlite"
        self.auth = LocalAuthenticationProvider((
            LocalCredential(token=ADMIN_TOKEN,
                            identity=AuthenticatedIdentity(user_id=ADMIN_ID, role=Role.ADMIN)),
            LocalCredential(token=REVIEWER_TOKEN,
                            identity=AuthenticatedIdentity(user_id=REVIEWER_ID, role=Role.REVIEWER)),
            LocalCredential(token=CUSTOMER_A_TOKEN, identity=AuthenticatedIdentity(
                user_id=CUSTOMER_A_USER_ID, role=Role.CUSTOMER, customer_id=CUSTOMER_A_ID)),
            LocalCredential(token=CUSTOMER_B_TOKEN, identity=AuthenticatedIdentity(
                user_id=CUSTOMER_B_USER_ID, role=Role.CUSTOMER, customer_id=CUSTOMER_B_ID)),
        ))

    @staticmethod
    def headers(token, key=None):
        headers = {"Authorization": f"Bearer {token}"}
        if key is not None:
            headers["Idempotency-Key"] = str(key)
        return headers

    def app(self):
        return create_app(
            auth_provider=self.auth, sqlite_path=self.path,
            startup_customers=(
                Customer(id=CUSTOMER_A_ID, name="Customer A"),
                Customer(id=CUSTOMER_B_ID, name="Customer B"),
            ),
        )

    @staticmethod
    def new_case(app, customer_id=CUSTOMER_A_ID):
        order = app.state.case_service.create_order(OrderCreate(
            customer_id=customer_id,
            items=[{"sku": "INP-1", "name": "NovaControl Input 1", "quantity": 1}],
        ))
        return app.state.case_service.create_case(CaseCreate(
            customer_id=customer_id, order_id=order.id,
            subject="Intermittent connection",
            description="The controller intermittently stops responding.",
        ))

    def test_admin_send_is_idempotent_audited_and_does_not_change_case(self):
        app = self.app()
        support_case = self.new_case(app)
        before = app.state.case_service.get_case(support_case.id)
        key = uuid4()
        body = {"message": "Reconnect the cable, try another port, and tell us whether the issue continues."}

        with TestClient(app) as client:
            first = client.post(
                f"/api/cases/{support_case.id}/customer-communications",
                headers=self.headers(ADMIN_TOKEN, key), json=body,
            )
            self.assertEqual(first.status_code, 201, first.text)
            self.assertEqual(set(first.json()), {"message", "sent_at"})
            self.assertEqual(first.json()["message"], body["message"])

            retry = client.post(
                f"/api/cases/{support_case.id}/customer-communications",
                headers=self.headers(ADMIN_TOKEN, key), json=body,
            )
            self.assertEqual(retry.status_code, 201, retry.text)
            self.assertEqual(retry.json(), first.json())

            conflict = client.post(
                f"/api/cases/{support_case.id}/customer-communications",
                headers=self.headers(ADMIN_TOKEN, key),
                json={"message": "A different message must not reuse the same request key."},
            )
            self.assertEqual(conflict.status_code, 409, conflict.text)

        history = app.state.case_service.customer_communication_history(support_case.id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].admin_user_id, ADMIN_ID)
        self.assertEqual(app.state.case_service.get_case(support_case.id), before)
        self.assertEqual(before.status, CaseStatus.OPEN)
        self.assertEqual(app.state.case_service.resolution_history(support_case.id), ())
        self.assertEqual(app.state.proposal_service.list_for_case(support_case.id), [])

    def test_authorization_ownership_and_customer_projection_are_fail_closed(self):
        app = self.app()
        support_case = self.new_case(app)
        other_case = self.new_case(app, CUSTOMER_B_ID)
        key = uuid4()
        body = {"message": "Please try the documented reset steps and report the indicator state."}
        path = f"/api/cases/{support_case.id}/customer-communications"

        with TestClient(app) as client:
            self.assertEqual(client.post(path, headers=self.headers(REVIEWER_TOKEN, key), json=body).status_code, 403)
            self.assertEqual(client.post(path, headers=self.headers(CUSTOMER_A_TOKEN, key), json=body).status_code, 403)
            self.assertEqual(client.post(path, headers={"Idempotency-Key": str(key)}, json=body).status_code, 401)
            self.assertEqual(client.post(path, headers=self.headers(ADMIN_TOKEN, key),
                                         json={**body, "admin_user_id": str(ADMIN_ID)}).status_code, 422)
            sent = client.post(path, headers=self.headers(ADMIN_TOKEN, key), json=body)
            self.assertEqual(sent.status_code, 201, sent.text)

            latest_path = f"{path}/latest"
            owner = client.get(latest_path, headers=self.headers(CUSTOMER_A_TOKEN))
            self.assertEqual(owner.status_code, 200, owner.text)
            self.assertEqual(set(owner.json()), {"message", "sent_at"})
            self.assertEqual(owner.json()["message"], body["message"])
            self.assertEqual(client.get(latest_path, headers=self.headers(CUSTOMER_B_TOKEN)).status_code, 403)
            self.assertEqual(client.get(
                f"/api/cases/{other_case.id}/customer-communications/latest",
                headers=self.headers(CUSTOMER_B_TOKEN),
            ).json(), None)

    def test_communication_survives_restart_and_database_rejects_update_or_delete(self):
        first = self.app()
        support_case = self.new_case(first)
        key = uuid4()
        message = "Power-cycle the device and test a known-good cable; no diagnosis has been assumed."
        with TestClient(first) as client:
            response = client.post(
                f"/api/cases/{support_case.id}/customer-communications",
                headers=self.headers(ADMIN_TOKEN, key), json={"message": message},
            )
            self.assertEqual(response.status_code, 201, response.text)
            sent_at = response.json()["sent_at"]

        restarted = self.app()
        with TestClient(restarted) as client:
            restored = client.get(
                f"/api/cases/{support_case.id}/customer-communications/latest",
                headers=self.headers(CUSTOMER_A_TOKEN),
            )
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(restored.json(), {"message": message, "sent_at": sent_at})

        for statement in (
            "UPDATE case_customer_communications SET message = 'changed'",
            "DELETE FROM case_customer_communications",
        ):
            with self.assertRaises(PersistenceError):
                with restarted.state.sqlite_persistence.transaction() as connection:
                    connection.execute(statement)
        self.assertEqual(
            restarted.state.case_service.latest_customer_communication(support_case.id).message,
            message,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
