"""Offline P1-1C2 durable claim-to-workflow reservation coverage."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from backend.app.agent import AgentRequest
from backend.app.domain import Customer, InvestigationJobStatus, InvestigationRecoveryReason
from backend.app.main import create_app
from backend.app.persistence import (
    MIGRATIONS,
    PersistenceError, SQLiteInvestigationJobRepository, SQLitePersistence,
    SQLiteWorkflowMetadataRepository,
)
from backend.app.schemas import OrderCreate
from backend.app.security import (
    AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role,
    authenticated, current_identity, current_investigation_execution,
)
from backend.app.service import ConflictError, NotFoundError
from backend.app.hitl import HITLWorkflow
from backend.app.workflow_metadata import WorkflowRunReservation


CUSTOMER_A_ID = UUID("00000000-0000-4000-8000-000000000091")
CUSTOMER_B_ID = UUID("00000000-0000-4000-8000-000000000092")
CUSTOMER_A_USER_ID = UUID("00000000-0000-4000-8000-000000000093")
CUSTOMER_B_USER_ID = UUID("00000000-0000-4000-8000-000000000094")
CUSTOMER_A_TOKEN = "p1-binding-customer-a-token"
CUSTOMER_B_TOKEN = "p1-binding-customer-b-token"
STAFF_A_ID = UUID("00000000-0000-4000-8000-000000000095")
STAFF_B_ID = UUID("00000000-0000-4000-8000-000000000096")
STAFF_A_TOKEN = "p1-binding-staff-a-token"
STAFF_B_TOKEN = "p1-binding-staff-b-token"


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


class SQLiteInvestigationJobBindingTests(unittest.TestCase):
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
            LocalCredential(token=STAFF_A_TOKEN, identity=AuthenticatedIdentity(
                user_id=STAFF_A_ID, role=Role.REVIEWER)),
            LocalCredential(token=STAFF_B_TOKEN, identity=AuthenticatedIdentity(
                user_id=STAFF_B_ID, role=Role.ADMIN)),
        ))
        self.auth = auth
        self.app = create_app(
            auth_provider=auth, sqlite_path=self.path,
            startup_customers=(
                Customer(id=CUSTOMER_A_ID, name="Customer A"),
                Customer(id=CUSTOMER_B_ID, name="Customer B"),
            ),
            local_provider_factory=lambda: self.providers.append(object()),
        )
        self.jobs = self.app.state.investigation_jobs
        self.service = self.app.state.case_service
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
        self.assertEqual(response.status_code, 201, response.text)
        return UUID(response.json()["id"])

    def claim(self, *, lease_seconds=60, now=None):
        case_id = self.submit()
        timestamp = now or datetime(2030, 1, 1, tzinfo=timezone.utc)
        claim = self.jobs.claim_next(
            worker_id=STAFF_A_ID, lease_seconds=lease_seconds, now=timestamp,
        )
        self.assertIsNotNone(claim)
        self.assertEqual(claim.job.case_id, case_id)
        return case_id, claim, timestamp

    def test_claim_binds_once_to_a_stable_job_derived_reservation(self):
        case_id, claim, now = self.claim()
        bound = self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        self.assertTrue(bound.created)
        self.assertEqual(bound.run.workflow_id, claim.job.id)
        self.assertEqual(bound.run.run_id, claim.job.id)
        self.assertEqual(bound.run.request_id, claim.job.id)
        self.assertEqual(bound.run.case_id, case_id)
        self.assertEqual(bound.run.owner_user_id, STAFF_A_ID)
        linked = self.jobs.get_by_case(case_id)
        self.assertEqual(linked.status, InvestigationJobStatus.CLAIMED)
        self.assertEqual(linked.workflow_id, claim.job.id)
        self.assertIsNotNone(linked.bound_at)
        self.assertIsNotNone(linked.agent_request_json)
        self.assertIsNotNone(linked.agent_request_fingerprint)
        request = self.jobs.execution_request(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            now=now + timedelta(seconds=2),
        )
        self.assertEqual(request.case_id, case_id)
        self.assertEqual(HITLWorkflow._fingerprint(request), linked.agent_request_fingerprint)
        self.assertEqual(bound.run.request_fingerprint, linked.agent_request_fingerprint)
        self.assertEqual(json.loads(linked.agent_request_json)["case_id"], str(case_id))
        events = SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).events(claim.job.id)
        self.assertEqual([event.kind for event in events], ["CREATED"])
        self.assertEqual(self.providers, [])

    def test_restart_and_competing_bind_attempts_return_the_single_existing_reservation(self):
        _, claim, now = self.claim()
        barrier = Barrier(2)

        def bind():
            repository = SQLiteInvestigationJobRepository(SQLitePersistence(self.path))
            barrier.wait()
            return repository.bind_claim(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                provider="local_scripted", now=now + timedelta(seconds=1),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: bind(), range(2)))
        self.assertEqual(sum(result.created for result in results), 1)
        self.assertEqual({result.run.workflow_id for result in results}, {claim.job.id})

        reopened = SQLiteInvestigationJobRepository(SQLitePersistence(self.path))
        replay = reopened.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=2),
        )
        self.assertFalse(replay.created)
        self.assertEqual(
            reopened.execution_request(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                now=now + timedelta(seconds=3),
            ).case_id,
            claim.job.case_id,
        )
        metadata = SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path))
        self.assertEqual(len(metadata.events(claim.job.id)), 1)
        self.assertEqual(self.providers, [])

    def test_wrong_worker_or_claim_cannot_bind_the_reservation(self):
        _, claim, now = self.claim()
        with self.assertRaises(ConflictError):
            self.jobs.bind_claim(
                claim.job.id, worker_id=STAFF_B_ID, claim_id=claim.claim_id,
                provider="local_scripted", now=now + timedelta(seconds=1),
            )
        with self.assertRaises(ConflictError):
            self.jobs.bind_claim(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=uuid4(),
                provider="local_scripted", now=now + timedelta(seconds=1),
            )
        with self.assertRaises(NotFoundError):
            SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(claim.job.id)
        self.assertIsNone(self.jobs.get_by_case(claim.job.case_id).workflow_id)
        self.assertEqual(self.providers, [])

    def test_bind_endpoint_is_staff_only_and_uses_the_authenticated_staff_identity(self):
        _, claim, _ = self.claim()
        path = f"/api/investigation-jobs/{claim.job.id}/workflow-reservation"
        with TestClient(self.app) as client:
            denied = client.post(
                path, headers={"Authorization": f"Bearer {CUSTOMER_A_TOKEN}"},
                json={"claim_id": str(claim.claim_id)},
            )
            allowed = client.post(
                path, headers={"Authorization": f"Bearer {STAFF_A_TOKEN}"},
                json={"claim_id": str(claim.claim_id)},
            )
            marker_endpoint = client.post(
                f"/api/investigation-jobs/{claim.job.id}/execution-attempt",
                headers={"Authorization": f"Bearer {STAFF_A_TOKEN}"},
            )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertTrue(allowed.json()["created"])
        self.assertEqual(allowed.json()["run"]["owner_user_id"], str(STAFF_A_ID))
        self.assertEqual(marker_endpoint.status_code, 404, marker_endpoint.text)
        self.assertEqual(self.providers, [])

    def test_binding_write_failure_rolls_back_the_reservation_and_link(self):
        _, claim, now = self.claim()
        with SQLitePersistence(self.path).transaction() as connection:
            connection.execute(
                """CREATE TRIGGER reject_investigation_job_binding
                   BEFORE UPDATE ON investigation_jobs
                   WHEN NEW.workflow_id IS NOT NULL
                   BEGIN SELECT RAISE(ABORT, 'forced binding failure'); END""",
            )
        with self.assertRaises(PersistenceError):
            self.jobs.bind_claim(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                provider="local_scripted", now=now + timedelta(seconds=1),
            )
        self.assertIsNone(self.jobs.get_by_case(claim.job.case_id).workflow_id)
        with self.assertRaises(NotFoundError):
            SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(claim.job.id)
        self.assertEqual(self.providers, [])

    def test_expiring_a_bound_claim_marks_both_durable_records_for_recovery(self):
        case_id, claim, now = self.claim(lease_seconds=10)
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        with self.assertRaises(ConflictError):
            self.jobs.bind_claim(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                provider="local_scripted", now=now + timedelta(seconds=10),
            )
        self.assertEqual(
            self.jobs.get_by_case(case_id).status, InvestigationJobStatus.RECOVERY_REQUIRED,
        )
        metadata = SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path))
        self.assertEqual(metadata.get(claim.job.id).status.value, "RECOVERY_REQUIRED")
        self.assertEqual(
            [event.kind for event in metadata.events(claim.job.id)],
            ["CREATED", "RECOVERY_REQUIRED"],
        )
        self.assertEqual(self.providers, [])

    def test_expired_or_collision_claim_requires_recovery_without_another_execution(self):
        case_id, expired_claim, now = self.claim(lease_seconds=10)
        with self.assertRaises(ConflictError):
            self.jobs.bind_claim(
                expired_claim.job.id, worker_id=STAFF_A_ID, claim_id=expired_claim.claim_id,
                provider="local_scripted", now=now + timedelta(seconds=10),
            )
        expired = self.jobs.get_by_case(case_id)
        self.assertEqual(expired.status, InvestigationJobStatus.RECOVERY_REQUIRED)
        self.assertEqual(expired.recovery_reason, InvestigationRecoveryReason.LEASE_EXPIRED)
        with self.assertRaises(NotFoundError):
            SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(expired_claim.job.id)

        collision_case_id, collision_claim, later = self.claim()
        metadata = SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path))
        metadata.reserve(WorkflowRunReservation(
            workflow_id=collision_claim.job.id, run_id=uuid4(), case_id=collision_case_id,
            owner_user_id=STAFF_A_ID, request_id=uuid4(), request_fingerprint="0" * 64,
            provider="local_scripted",
        ))
        with self.assertRaises(ConflictError):
            self.jobs.bind_claim(
                collision_claim.job.id, worker_id=STAFF_A_ID, claim_id=collision_claim.claim_id,
                provider="local_scripted", now=later + timedelta(seconds=1),
            )
        collision = self.jobs.get_by_case(collision_case_id)
        self.assertEqual(collision.status, InvestigationJobStatus.RECOVERY_REQUIRED)
        self.assertEqual(collision.recovery_reason, InvestigationRecoveryReason.CLAIM_AMBIGUOUS)
        self.assertIsNone(collision.workflow_id)
        self.assertEqual(self.providers, [])

    def test_descriptor_tampering_is_rejected_and_the_original_request_remains_usable(self):
        case_id, claim, now = self.claim()
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        with self.assertRaises(PersistenceError):
            with SQLitePersistence(self.path).transaction() as connection:
                connection.execute(
                    """UPDATE investigation_jobs SET agent_request_json = ?
                       WHERE job_id = ?""",
                    (json.dumps({"case_id": str(case_id), "message": "tampered"}, sort_keys=True,
                                separators=(",", ":")), str(claim.job.id)),
                )
        request = self.jobs.execution_request(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            now=now + timedelta(seconds=2),
        )
        self.assertEqual(request.case_id, case_id)
        self.assertEqual(self.jobs.get_by_case(case_id).status, InvestigationJobStatus.CLAIMED)
        self.assertEqual(
            SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(claim.job.id).status.value,
            "RUNNING",
        )
        self.assertEqual(self.providers, [])

    def test_invalid_legacy_descriptor_requires_recovery_without_reconstructing_request(self):
        case_id, claim, now = self.claim()
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        with SQLitePersistence(self.path).transaction() as connection:
            connection.execute("DROP TRIGGER immutable_investigation_job_request_descriptor_update")
            connection.execute(
                """UPDATE investigation_jobs SET agent_request_json = ?
                   WHERE job_id = ?""",
                (json.dumps({"case_id": str(case_id), "message": "tampered"}, sort_keys=True,
                            separators=(",", ":")), str(claim.job.id)),
            )
        with self.assertRaises(ConflictError):
            self.jobs.execution_request(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                now=now + timedelta(seconds=2),
            )
        self.assertEqual(self.jobs.get_by_case(case_id).status, InvestigationJobStatus.RECOVERY_REQUIRED)
        self.assertEqual(
            SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(claim.job.id).status.value,
            "RECOVERY_REQUIRED",
        )
        self.assertEqual(self.providers, [])

    def test_version_twelve_binding_without_descriptor_is_recovered_without_identity_rewrite(self):
        case_id, claim, now = self.claim()
        metadata = SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path))
        metadata.reserve(WorkflowRunReservation(
            workflow_id=claim.job.id, run_id=claim.job.id, request_id=claim.job.id,
            case_id=case_id, owner_user_id=STAFF_A_ID, request_fingerprint="a" * 64,
            provider="local_scripted",
        ))
        with SQLitePersistence(self.path).transaction() as connection:
            connection.execute(
                """UPDATE investigation_jobs
                   SET workflow_id = ?, bound_at = ?, updated_at = ?
                   WHERE job_id = ?""",
                (str(claim.job.id), (now + timedelta(seconds=1)).isoformat(),
                 (now + timedelta(seconds=1)).isoformat(), str(claim.job.id)),
            )
            connection.execute(
                """CREATE TABLE investigation_jobs_v12 (
                    job_id TEXT PRIMARY KEY NOT NULL, case_id TEXT UNIQUE NOT NULL,
                    customer_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL, claim_id TEXT, claimed_by TEXT, claimed_at TEXT,
                    lease_expires_at TEXT, recovery_reason TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, workflow_id TEXT, bound_at TEXT,
                    UNIQUE(customer_id, idempotency_key)
                )""",
            )
            connection.execute(
                """INSERT INTO investigation_jobs_v12
                   SELECT job_id, case_id, customer_id, owner_user_id, idempotency_key,
                          request_fingerprint, status, claim_id, claimed_by, claimed_at,
                          lease_expires_at, recovery_reason, created_at, updated_at,
                          workflow_id, bound_at
                   FROM investigation_jobs""",
            )
            connection.execute("DROP TABLE investigation_jobs")
            connection.execute("ALTER TABLE investigation_jobs_v12 RENAME TO investigation_jobs")
            connection.execute("CREATE UNIQUE INDEX investigation_jobs_workflow ON investigation_jobs(workflow_id) WHERE workflow_id IS NOT NULL")
            connection.execute("DROP TABLE case_customer_communications")
            _rewind_workflow_runs_to_v19(connection)
            connection.execute("DELETE FROM schema_migrations WHERE version >= 13")

        reopened = SQLiteInvestigationJobRepository(SQLitePersistence(self.path))
        legacy = reopened.get_by_case(case_id)
        self.assertIsNone(legacy.agent_request_json)
        self.assertIsNone(legacy.execution_attempt_id)
        self.assertIsNone(legacy.execution_attempted_at)
        with self.assertRaises(ConflictError):
            reopened.execution_request(
                legacy.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                now=now + timedelta(seconds=2),
            )
        recovered = reopened.get_by_case(case_id)
        self.assertEqual(recovered.status, InvestigationJobStatus.RECOVERY_REQUIRED)
        self.assertEqual(recovered.workflow_id, claim.job.id)
        run = SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(claim.job.id)
        self.assertEqual((run.workflow_id, run.run_id, run.request_id),
                         (claim.job.id, claim.job.id, claim.job.id))
        self.assertEqual(run.status.value, "RECOVERY_REQUIRED")
        self.assertEqual(self.providers, [])

    def test_execution_attempt_is_once_only_and_returns_the_bound_descriptor(self):
        case_id, claim, now = self.claim()
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        attempt = self.jobs.mark_execution_attempt(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            now=now + timedelta(seconds=2),
        )
        job = self.jobs.get_by_case(case_id)
        self.assertEqual(attempt.job_id, claim.job.id)
        self.assertEqual(attempt.workflow_id, claim.job.id)
        self.assertEqual(attempt.attempt_id, job.execution_attempt_id)
        self.assertEqual(attempt.attempted_at, job.execution_attempted_at)
        self.assertEqual(attempt.agent_request_json, job.agent_request_json)
        self.assertEqual(attempt.agent_request_fingerprint, job.agent_request_fingerprint)
        with self.assertRaises(ConflictError):
            self.jobs.mark_execution_attempt(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                now=now + timedelta(seconds=3),
            )
        with self.assertRaises(ConflictError):
            self.jobs.execution_request(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                now=now + timedelta(seconds=3),
            )
        self.assertEqual(self.providers, [])

    def test_competing_execution_attempt_markers_have_one_winner(self):
        _, claim, now = self.claim()
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        barrier = Barrier(2)

        def mark():
            repository = SQLiteInvestigationJobRepository(SQLitePersistence(self.path))
            barrier.wait()
            try:
                return repository.mark_execution_attempt(
                    claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                    now=now + timedelta(seconds=2),
                )
            except ConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = list(pool.map(lambda _: mark(), range(2)))
        winners = [attempt for attempt in attempts if attempt is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(
            self.jobs.get_by_case(claim.job.case_id).execution_attempt_id,
            winners[0].attempt_id,
        )
        self.assertEqual(self.providers, [])

    def test_execution_attempt_requires_the_active_claim_owner_and_token(self):
        case_id, claim, now = self.claim()
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        with self.assertRaises(ConflictError):
            self.jobs.mark_execution_attempt(
                claim.job.id, worker_id=STAFF_B_ID, claim_id=claim.claim_id,
                now=now + timedelta(seconds=2),
            )
        with self.assertRaises(ConflictError):
            self.jobs.mark_execution_attempt(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=uuid4(),
                now=now + timedelta(seconds=2),
            )
        self.assertIsNone(self.jobs.get_by_case(case_id).execution_attempt_id)
        self.assertEqual(self.providers, [])

    def test_invalid_descriptor_prevents_attempt_and_requires_recovery(self):
        case_id, claim, now = self.claim()
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        with SQLitePersistence(self.path).transaction() as connection:
            connection.execute("DROP TRIGGER immutable_investigation_job_request_descriptor_update")
            connection.execute(
                """UPDATE investigation_jobs SET agent_request_json = ?
                   WHERE job_id = ?""",
                (json.dumps({"case_id": str(case_id), "message": "tampered"}, sort_keys=True,
                            separators=(",", ":")), str(claim.job.id)),
            )
        with self.assertRaises(ConflictError):
            self.jobs.mark_execution_attempt(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                now=now + timedelta(seconds=2),
            )
        job = self.jobs.get_by_case(case_id)
        self.assertEqual(job.status, InvestigationJobStatus.RECOVERY_REQUIRED)
        self.assertIsNone(job.execution_attempt_id)
        self.assertEqual(
            SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(claim.job.id).status.value,
            "RECOVERY_REQUIRED",
        )
        self.assertEqual(self.providers, [])

    def test_restart_after_marker_requires_explicit_recovery_without_replay(self):
        case_id, claim, now = self.claim()
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        attempt = self.jobs.mark_execution_attempt(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            now=now + timedelta(seconds=2),
        )
        reopened = SQLiteInvestigationJobRepository(SQLitePersistence(self.path))
        with self.assertRaises(ConflictError):
            reopened.mark_execution_attempt(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                now=now + timedelta(seconds=3),
            )
        with self.assertRaises(ConflictError):
            reopened.execution_request(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                now=now + timedelta(seconds=3),
            )
        recovered = reopened.require_recovery(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            now=now + timedelta(seconds=4),
        )
        self.assertEqual(recovered.status, InvestigationJobStatus.RECOVERY_REQUIRED)
        self.assertEqual(recovered.execution_attempt_id, attempt.attempt_id)
        self.assertEqual(
            SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(claim.job.id).status.value,
            "RECOVERY_REQUIRED",
        )
        with self.assertRaises(ConflictError):
            reopened.mark_execution_attempt(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                now=now + timedelta(seconds=5),
            )
        self.assertEqual(self.providers, [])

    def test_expired_claim_cannot_receive_an_execution_attempt(self):
        case_id, claim, now = self.claim(lease_seconds=10)
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        with self.assertRaises(ConflictError):
            self.jobs.mark_execution_attempt(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                now=now + timedelta(seconds=10),
            )
        job = self.jobs.get_by_case(case_id)
        self.assertEqual(job.status, InvestigationJobStatus.RECOVERY_REQUIRED)
        self.assertEqual(job.recovery_reason, InvestigationRecoveryReason.LEASE_EXPIRED)
        self.assertIsNone(job.execution_attempt_id)
        self.assertEqual(
            SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(claim.job.id).status.value,
            "RECOVERY_REQUIRED",
        )
        self.assertEqual(self.providers, [])

    def test_attempt_write_failure_rolls_back_the_marker(self):
        case_id, claim, now = self.claim()
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        with SQLitePersistence(self.path).transaction() as connection:
            connection.execute(
                """CREATE TRIGGER reject_execution_attempt
                   BEFORE UPDATE ON investigation_jobs
                   WHEN NEW.execution_attempt_id IS NOT NULL
                   BEGIN SELECT RAISE(ABORT, 'forced attempt failure'); END""",
            )
        with self.assertRaises(PersistenceError):
            self.jobs.mark_execution_attempt(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                now=now + timedelta(seconds=2),
            )
        job = self.jobs.get_by_case(case_id)
        self.assertIsNone(job.execution_attempt_id)
        self.assertIsNone(job.execution_attempted_at)
        self.assertEqual(job.status, InvestigationJobStatus.CLAIMED)
        self.assertEqual(
            SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(claim.job.id).status.value,
            "RUNNING",
        )
        self.assertEqual(self.providers, [])

    def test_marked_execution_context_is_persisted_and_scopes_langgraph_tools(self):
        case_id, claim, now = self.claim()
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        attempt = self.jobs.mark_execution_attempt(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            now=now + timedelta(seconds=2),
        )
        same_customer_case = self.submit()
        other_order = self.service.create_order(OrderCreate(
            customer_id=CUSTOMER_A_ID,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))
        tools = self.app.state.local_tools
        with self.jobs.execution_context(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            attempt_id=attempt.attempt_id, now=now + timedelta(seconds=3),
        ) as execution:
            self.assertEqual(execution.execution_id, claim.job.id)
            self.assertEqual(execution.case_id, case_id)
            self.assertEqual(execution.customer_id, CUSTOMER_A_ID)
            self.assertEqual(execution.order_id, self.order_a.id)
            self.assertEqual(execution.agent_request_fingerprint, attempt.agent_request_fingerprint)
            self.assertEqual(
                HITLWorkflow._fingerprint(AgentRequest.model_validate_json(execution.agent_request_json)),
                execution.agent_request_fingerprint,
            )
            self.assertEqual(current_investigation_execution(), execution)
            self.assertEqual(current_identity().user_id, STAFF_A_ID)
            self.assertEqual(current_identity().role, Role.REVIEWER)
            self.assertTrue(tools.invoke("get_case", {"case_id": str(case_id)}).ok)
            self.assertTrue(tools.invoke("get_order", {"order_id": str(self.order_a.id)}).ok)
            self.assertEqual(
                tools.invoke("get_case", {"case_id": str(same_customer_case)}).error.code,
                "FORBIDDEN",
            )
            self.assertEqual(
                tools.invoke("get_order", {"order_id": str(other_order.id)}).error.code,
                "FORBIDDEN",
            )
            self.assertEqual(
                tools.invoke("get_customer", {"customer_id": str(CUSTOMER_B_ID)}).error.code,
                "FORBIDDEN",
            )
        self.assertIsNone(current_investigation_execution())
        self.assertEqual(
            tools.invoke("get_case", {"case_id": str(case_id)}).error.code,
            "UNAUTHENTICATED",
        )
        self.assertEqual(self.providers, [])

    def test_execution_context_rejects_spoofed_claim_attempt_and_http_identity(self):
        _, claim, now = self.claim()
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        attempt = self.jobs.mark_execution_attempt(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            now=now + timedelta(seconds=2),
        )
        with self.assertRaises(ConflictError):
            with self.jobs.execution_context(
                claim.job.id, worker_id=STAFF_B_ID, claim_id=claim.claim_id,
                attempt_id=attempt.attempt_id, now=now + timedelta(seconds=3),
            ):
                pass
        with self.assertRaises(ConflictError):
            with self.jobs.execution_context(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=uuid4(),
                attempt_id=attempt.attempt_id, now=now + timedelta(seconds=3),
            ):
                pass
        with self.assertRaises(ConflictError):
            with self.jobs.execution_context(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                attempt_id=uuid4(), now=now + timedelta(seconds=3),
            ):
                pass
        with authenticated(self.auth, CUSTOMER_A_TOKEN):
            with self.assertRaises(RuntimeError):
                with self.jobs.execution_context(
                    claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                    attempt_id=attempt.attempt_id, now=now + timedelta(seconds=3),
                ):
                    pass
        self.assertEqual(self.providers, [])

    def test_execution_context_survives_restart_but_expiry_requires_recovery(self):
        case_id, claim, now = self.claim(lease_seconds=10)
        self.jobs.bind_claim(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            provider="local_scripted", now=now + timedelta(seconds=1),
        )
        attempt = self.jobs.mark_execution_attempt(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            now=now + timedelta(seconds=2),
        )
        reopened = SQLiteInvestigationJobRepository(SQLitePersistence(self.path))
        with reopened.execution_context(
            claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
            attempt_id=attempt.attempt_id, now=now + timedelta(seconds=3),
        ) as execution:
            self.assertEqual(execution.case_id, case_id)
            self.assertEqual(execution.execution_attempt_id, attempt.attempt_id)
        with self.assertRaises(ConflictError):
            with reopened.execution_context(
                claim.job.id, worker_id=STAFF_A_ID, claim_id=claim.claim_id,
                attempt_id=attempt.attempt_id, now=now + timedelta(seconds=10),
            ):
                pass
        self.assertEqual(
            reopened.get_by_case(case_id).status, InvestigationJobStatus.RECOVERY_REQUIRED,
        )
        self.assertEqual(
            SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(claim.job.id).status.value,
            "RECOVERY_REQUIRED",
        )
        self.assertEqual(self.providers, [])
