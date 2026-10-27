"""Offline P1-1C1 durable investigation-job claim coverage."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from backend.app.domain import Customer, InvestigationJobStatus, InvestigationRecoveryReason
from backend.app.main import create_app
from backend.app.persistence import MIGRATIONS, SQLiteInvestigationJobRepository, SQLitePersistence
from backend.app.schemas import OrderCreate
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role


CUSTOMER_A_ID = UUID("00000000-0000-4000-8000-000000000081")
CUSTOMER_B_ID = UUID("00000000-0000-4000-8000-000000000082")
CUSTOMER_A_USER_ID = UUID("00000000-0000-4000-8000-000000000083")
CUSTOMER_B_USER_ID = UUID("00000000-0000-4000-8000-000000000084")
CUSTOMER_A_TOKEN = "p1-claim-customer-a-token"
CUSTOMER_B_TOKEN = "p1-claim-customer-b-token"
WORKER_A_ID = UUID("00000000-0000-4000-8000-000000000085")
WORKER_B_ID = UUID("00000000-0000-4000-8000-000000000086")


def _rewind_workflow_runs_to_v19(connection):
    connection.execute("DROP TRIGGER validate_workflow_run_update")
    connection.execute("ALTER TABLE workflow_runs DROP COLUMN invalid_response_category")
    connection.execute("ALTER TABLE workflow_runs DROP COLUMN invalid_response_stage")
    connection.execute("ALTER TABLE workflow_runs DROP COLUMN failure_category")
    connection.execute("ALTER TABLE workflow_runs DROP COLUMN failure_stage")
    legacy_trigger = next(statement for migration in MIGRATIONS if migration.version == 4
                          for statement in migration.statements
                          if "CREATE TRIGGER validate_workflow_run_update" in statement)
    connection.execute(legacy_trigger)


class SQLiteInvestigationJobClaimTests(unittest.TestCase):
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

    def submit(self, *, key=None, token=CUSTOMER_A_TOKEN, order_id=None):
        with TestClient(self.app) as client:
            response = client.post(
                "/api/my/cases",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": str(uuid4() if key is None else key),
                },
                json={
                    "order_id": str(self.order_a.id if order_id is None else order_id),
                    "subject": "Damaged on arrival",
                    "description": "The package and the item were damaged on delivery.",
                },
            )
        return response

    def test_competing_workers_claim_one_pending_job_with_stable_execution_identity(self):
        submitted = self.submit()
        self.assertEqual(submitted.status_code, 201, submitted.text)
        case_id = UUID(submitted.json()["id"])
        barrier = Barrier(2)
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)

        def claim(worker_id):
            repository = SQLiteInvestigationJobRepository(SQLitePersistence(self.path))
            barrier.wait()
            return repository.claim_next(worker_id=worker_id, lease_seconds=30, now=now)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, (WORKER_A_ID, WORKER_B_ID)))

        claims = [outcome for outcome in outcomes if outcome is not None]
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim.job.case_id, case_id)
        self.assertEqual(claim.job.status, InvestigationJobStatus.CLAIMED)
        self.assertEqual(claim.job.claim_id, claim.claim_id)
        self.assertEqual(claim.execution.job_id, claim.job.id)
        self.assertEqual(claim.execution.execution_id, claim.job.id)
        self.assertEqual(claim.execution.customer_id, CUSTOMER_A_ID)
        self.assertEqual(claim.execution.owner_user_id, CUSTOMER_A_USER_ID)
        self.assertEqual(self.jobs.get_by_case(case_id).claim_id, claim.claim_id)
        self.assertEqual(self.providers, [])

    def test_restart_keeps_claimed_job_out_of_queue_until_explicit_recovery(self):
        submitted = self.submit()
        case_id = UUID(submitted.json()["id"])
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        original = self.jobs.claim_next(worker_id=WORKER_A_ID, lease_seconds=60, now=now)
        self.assertIsNotNone(original)

        restarted = SQLiteInvestigationJobRepository(SQLitePersistence(self.path))
        self.assertIsNone(restarted.claim_next(worker_id=WORKER_B_ID, now=now + timedelta(seconds=1)))
        still_claimed = restarted.get_by_case(case_id)
        self.assertEqual(still_claimed.status, InvestigationJobStatus.CLAIMED)
        self.assertEqual(still_claimed.claim_id, original.claim_id)

        recovered = restarted.require_recovery(
            original.job.id, worker_id=WORKER_A_ID, claim_id=original.claim_id,
            now=now + timedelta(seconds=2),
        )
        self.assertEqual(recovered.status, InvestigationJobStatus.RECOVERY_REQUIRED)
        self.assertEqual(recovered.recovery_reason, InvestigationRecoveryReason.CLAIM_AMBIGUOUS)
        self.assertEqual(recovered.claim_id, original.claim_id)
        self.assertIsNone(restarted.claim_next(worker_id=WORKER_B_ID, now=now + timedelta(seconds=120)))
        self.assertEqual(self.providers, [])

    def test_expired_lease_requires_recovery_and_is_never_reclaimed(self):
        submitted = self.submit()
        case_id = UUID(submitted.json()["id"])
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        claim = self.jobs.claim_next(worker_id=WORKER_A_ID, lease_seconds=10, now=now)
        self.assertIsNotNone(claim)

        restarted = SQLiteInvestigationJobRepository(SQLitePersistence(self.path))
        self.assertIsNone(restarted.claim_next(worker_id=WORKER_B_ID, now=now + timedelta(seconds=10)))
        expired = restarted.get_by_case(case_id)
        self.assertEqual(expired.status, InvestigationJobStatus.RECOVERY_REQUIRED)
        self.assertEqual(expired.recovery_reason, InvestigationRecoveryReason.LEASE_EXPIRED)
        self.assertEqual(expired.claim_id, claim.claim_id)
        self.assertEqual(expired.claimed_by, WORKER_A_ID)
        self.assertIsNone(restarted.claim_next(worker_id=WORKER_B_ID, now=now + timedelta(days=1)))
        self.assertEqual(self.providers, [])

    def test_customer_ownership_and_idempotency_leave_one_claimable_job(self):
        key = uuid4()
        first = self.submit(key=key)
        duplicate = self.submit(key=key)
        denied = self.submit(token=CUSTOMER_A_TOKEN, order_id=self.order_b.id)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(duplicate.status_code, 201, duplicate.text)
        self.assertEqual(first.json()["id"], duplicate.json()["id"])
        self.assertEqual(denied.status_code, 403, denied.text)

        claim = self.jobs.claim_next(worker_id=WORKER_A_ID)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.job.customer_id, CUSTOMER_A_ID)
        self.assertEqual(claim.job.owner_user_id, CUSTOMER_A_USER_ID)
        self.assertIsNone(self.jobs.claim_next(worker_id=WORKER_B_ID))
        self.assertEqual(self.providers, [])

    def test_version_ten_jobs_upgrade_without_changing_existing_case_data(self):
        submitted = self.submit()
        case_id = UUID(submitted.json()["id"])
        with SQLitePersistence(self.path).transaction() as connection:
            connection.execute(
                """CREATE TABLE investigation_jobs_v10 (
                    job_id TEXT PRIMARY KEY NOT NULL,
                    case_id TEXT UNIQUE NOT NULL REFERENCES support_cases(case_id),
                    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
                    owner_user_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) = 36),
                    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
                    status TEXT NOT NULL CHECK(status = 'PENDING'),
                    created_at TEXT NOT NULL,
                    UNIQUE(customer_id, idempotency_key)
                )""",
            )
            connection.execute(
                """INSERT INTO investigation_jobs_v10
                   (job_id, case_id, customer_id, owner_user_id, idempotency_key,
                    request_fingerprint, status, created_at)
                   SELECT job_id, case_id, customer_id, owner_user_id, idempotency_key,
                          request_fingerprint, status, created_at
                     FROM investigation_jobs""",
            )
            connection.execute("DROP TABLE investigation_jobs")
            connection.execute("ALTER TABLE investigation_jobs_v10 RENAME TO investigation_jobs")
            connection.execute("DROP TABLE case_customer_communications")
            _rewind_workflow_runs_to_v19(connection)
            connection.execute("DELETE FROM schema_migrations WHERE version >= 11")

        reopened = SQLiteInvestigationJobRepository(SQLitePersistence(self.path))
        upgraded = reopened.get_by_case(case_id)
        self.assertEqual(upgraded.status, InvestigationJobStatus.PENDING)
        self.assertIsNone(upgraded.claim_id)
        self.assertEqual(upgraded.updated_at, upgraded.created_at)
        self.assertEqual(self.service.get_case(case_id).id, case_id)


if __name__ == "__main__":
    unittest.main()
