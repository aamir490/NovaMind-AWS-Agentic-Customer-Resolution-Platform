"""Offline P1-1B SQLite complaint-intake and inert investigation-job coverage."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from backend.app.domain import CaseStatus, Customer, SupportCase
from backend.app.main import create_app
from backend.app.persistence import (
    MIGRATIONS, PersistenceError, SQLiteCaseRepository, SQLiteInvestigationJobRepository, SQLitePersistence,
)
from backend.app.schemas import CaseCreate, OrderCreate
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role
from backend.app.service import NotFoundError


CUSTOMER_A_ID = UUID("00000000-0000-4000-8000-000000000071")
CUSTOMER_B_ID = UUID("00000000-0000-4000-8000-000000000072")
CUSTOMER_A_USER_ID = UUID("00000000-0000-4000-8000-000000000073")
CUSTOMER_B_USER_ID = UUID("00000000-0000-4000-8000-000000000074")
CUSTOMER_A_TOKEN = "p1-intake-customer-a-token"
CUSTOMER_B_TOKEN = "p1-intake-customer-b-token"


def _rewind_workflow_runs_to_v19(connection):
    """Make the physical fixture match its version-19 migration ledger."""
    connection.execute("DROP TRIGGER validate_workflow_run_update")
    connection.execute("ALTER TABLE workflow_runs DROP COLUMN invalid_response_category")
    connection.execute("ALTER TABLE workflow_runs DROP COLUMN invalid_response_stage")
    connection.execute("ALTER TABLE workflow_runs DROP COLUMN failure_category")
    connection.execute("ALTER TABLE workflow_runs DROP COLUMN failure_stage")
    legacy_trigger = next(statement for migration in MIGRATIONS if migration.version == 4
                          for statement in migration.statements
                          if "CREATE TRIGGER validate_workflow_run_update" in statement)
    connection.execute(legacy_trigger)


class SQLiteComplaintIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "novamind.sqlite"
        self.providers = []
        auth = LocalAuthenticationProvider((
            LocalCredential(token=CUSTOMER_A_TOKEN, identity=AuthenticatedIdentity(
                user_id=CUSTOMER_A_USER_ID, role=Role.CUSTOMER, customer_id=CUSTOMER_A_ID)),
            LocalCredential(token=CUSTOMER_B_TOKEN, identity=AuthenticatedIdentity(
                user_id=CUSTOMER_B_USER_ID, role=Role.CUSTOMER, customer_id=CUSTOMER_B_ID)),
        ))
        self.app = create_app(
            auth_provider=auth, sqlite_path=self.path,
            startup_customers=(
                Customer(id=CUSTOMER_A_ID, name="Customer A"),
                Customer(id=CUSTOMER_B_ID, name="Customer B"),
            ),
            local_provider_factory=lambda: self.providers.append(object()),
        )
        self.service = self.app.state.case_service
        self.jobs = self.app.state.investigation_jobs
        self.order_a = self.service.create_order(OrderCreate(
            customer_id=CUSTOMER_A_ID,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))
        self.order_b = self.service.create_order(OrderCreate(
            customer_id=CUSTOMER_B_ID,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))

    @staticmethod
    def headers(token, key):
        return {"Authorization": f"Bearer {token}", "Idempotency-Key": str(key)}

    @staticmethod
    def body(order_id, **changes):
        return {
            "order_id": str(order_id),
            "subject": "Damaged on arrival",
            "description": "The package and the item were damaged on delivery.",
            **changes,
        }

    def post(self, client, *, token=CUSTOMER_A_TOKEN, key, order_id=None, **changes):
        return client.post(
            "/api/my/cases",
            headers=self.headers(token, key),
            json=self.body(self.order_a.id if order_id is None else order_id, **changes),
        )

    def test_same_customer_key_and_payload_return_the_original_case_and_job(self):
        key = uuid4()
        with TestClient(self.app) as client:
            first = self.post(client, key=key)
            second = self.post(client, key=key)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(len(self.service.list_cases(CUSTOMER_A_ID)), 1)
        job = self.jobs.get_by_case(UUID(first.json()["id"]))
        self.assertEqual(job.customer_id, CUSTOMER_A_ID)
        self.assertEqual(job.owner_user_id, CUSTOMER_A_USER_ID)
        self.assertEqual(str(job.idempotency_key), str(key))
        self.assertEqual(job.status.value, "PENDING")
        self.assertEqual(self.providers, [])

    def test_changed_payload_reusing_customer_key_is_rejected_without_a_second_case_or_job(self):
        key = uuid4()
        with TestClient(self.app) as client:
            first = self.post(client, key=key)
            changed = self.post(client, key=key, description="The item is damaged differently.")
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(len(self.service.list_cases(CUSTOMER_A_ID)), 1)
        self.assertIsNotNone(self.jobs.get_by_case(UUID(first.json()["id"])))
        self.assertEqual(self.providers, [])

    def test_cross_customer_order_creates_neither_case_nor_job(self):
        with TestClient(self.app) as client:
            denied = self.post(client, token=CUSTOMER_A_TOKEN, key=uuid4(), order_id=self.order_b.id)
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(self.service.list_cases(CUSTOMER_A_ID), [])
        self.assertEqual(self.providers, [])

    def test_case_and_job_write_roll_back_together_when_job_insert_fails(self):
        persistence = SQLitePersistence(self.path)
        with persistence.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER reject_investigation_job_insert
                   BEFORE INSERT ON investigation_jobs
                   BEGIN SELECT RAISE(ABORT, 'forced job insert failure'); END""",
            )
        timestamp = datetime.now(timezone.utc)
        support_case = SupportCase(
            id=uuid4(), customer_id=CUSTOMER_A_ID, order_id=self.order_a.id,
            subject="Atomic intake", description="Both records must commit together.",
            status=CaseStatus.OPEN, created_at=timestamp, updated_at=timestamp,
        )
        with self.assertRaises(PersistenceError):
            self.jobs.submit(support_case, owner_user_id=CUSTOMER_A_USER_ID, idempotency_key=uuid4())
        with self.assertRaises(NotFoundError):
            SQLiteCaseRepository(SQLitePersistence(self.path)).get_case(support_case.id)
        with self.assertRaises(NotFoundError):
            self.jobs.get_by_case(support_case.id)

    def test_existing_case_remains_readable_after_the_additive_migration(self):
        existing = self.service.create_case(CaseCreate(
            customer_id=CUSTOMER_A_ID, order_id=self.order_a.id,
            subject="Existing case", description="Created outside the complaint-intake queue.",
        ))
        # Recreate a version-9 database in this temporary test file, then let
        # SQLitePersistence apply the new additive migration to existing data.
        with SQLitePersistence(self.path).transaction() as connection:
            connection.execute("DROP TABLE case_customer_communications")
            connection.execute("DROP TABLE investigation_jobs")
            _rewind_workflow_runs_to_v19(connection)
            connection.execute("DELETE FROM schema_migrations WHERE version >= 10")
        reopened = SQLiteCaseRepository(SQLitePersistence(self.path))
        self.assertEqual(reopened.get_case(existing.id), existing)


if __name__ == "__main__":
    unittest.main()
