"""Task 6B Phase 1 transactional PostgreSQL outbox coverage.

The offline tests inspect the additive contract and repository idempotency
without a database.  The integration tests are opt-in and destructive only
after verifying the explicitly supplied disposable database identity.
"""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
import re
from urllib.parse import unquote, urlsplit
import unittest
from uuid import UUID, uuid4

from backend.app.domain import CaseStatus, InvestigationMode, SupportCase
from backend.app.persistence import PersistenceError
from backend.app.postgres_persistence import (
    POSTGRES_MIGRATIONS, InvestigationOutboxEvent,
    PostgreSQLCaseRepository, PostgreSQLInvestigationJobRepository,
    PostgreSQLInvestigationOutboxRepository, PostgreSQLInvestigationControlRepository,
    PostgreSQLPersistence,
)
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.service import CaseService, ConflictError


class _OfflineCursor:
    def __init__(self):
        self.rows = []
        self.inserted = None
        self.statements = []

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if statement.startswith("SELECT * FROM investigation_outbox"):
            if self.inserted is None:
                self.rows = []
            else:
                self.rows = [self.inserted]
        elif statement.startswith("INSERT INTO investigation_outbox"):
            values = parameters
            self.inserted = {
                "outbox_id": values[0], "job_id": values[1], "case_id": values[2],
                "event_type": values[3], "event_version": values[4],
                "idempotency_key": values[5], "request_fingerprint": values[6],
                "payload_json": values[7], "status": values[8],
                "publication_attempts": values[9], "last_error_category": values[10],
                "message_id": values[11], "created_at": values[12],
                "published_at": values[13], "lease_id": None, "leased_by": None,
                "lease_expires_at": None, "next_attempt_at": None,
                "retry_exhausted": False,
            }
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None


class PostgreSQLOutboxOfflineTests(unittest.TestCase):
    def setUp(self):
        self.connection = _OfflineCursor()
        self.job_id = uuid4()
        self.case_id = uuid4()
        self.key = uuid4()
        self.fingerprint = "a" * 64
        self.created_at = datetime(2026, 10, 26, tzinfo=timezone.utc)

    def enqueue(self):
        return PostgreSQLInvestigationOutboxRepository.enqueue_in_transaction(
            self.connection, job_id=self.job_id, case_id=self.case_id,
            idempotency_key=self.key, request_fingerprint=self.fingerprint,
            created_at=self.created_at,
        )

    def test_additive_migration_12_defines_reference_only_pending_outbox(self):
        migration = next(item for item in POSTGRES_MIGRATIONS if item.version == 12)
        sql = "\n".join(migration.statements)
        self.assertIn("CREATE TABLE investigation_outbox", sql)
        self.assertIn("UNIQUE(job_id, event_type)", sql)
        self.assertIn("INVESTIGATION_DISPATCH_REQUESTED", sql)
        self.assertIn("status IN ('PENDING', 'PUBLISHED')", sql)

        migration = next(item for item in POSTGRES_MIGRATIONS if item.version == 13)
        sql = "\n".join(migration.statements)
        self.assertIn("lease_id", sql)
        self.assertIn("retry_exhausted", sql)
        self.assertIn("investigation_outbox_claimable", sql)

    def test_enqueue_is_reference_only_and_restart_readable(self):
        event = self.enqueue()
        self.assertIsInstance(event, InvestigationOutboxEvent)
        payload = json.loads(event.payload_json)
        self.assertEqual(payload["job_id"], str(self.job_id))
        self.assertEqual(payload["case_id"], str(self.case_id))
        self.assertEqual(payload["outbox_id"], str(event.outbox_id))
        self.assertNotIn("token", event.payload_json.lower())
        self.assertEqual(self.enqueue(), event)

    def test_duplicate_outbox_with_conflicting_fingerprint_is_rejected(self):
        self.enqueue()
        with self.assertRaises(ConflictError):
            PostgreSQLInvestigationOutboxRepository.enqueue_in_transaction(
                self.connection, job_id=self.job_id, case_id=self.case_id,
                idempotency_key=self.key, request_fingerprint="b" * 64,
                created_at=self.created_at,
            )

    def test_outbox_event_has_no_publication_side_effects(self):
        event = self.enqueue()
        self.assertEqual(event.status, "PENDING")
        self.assertEqual(event.publication_attempts, 0)
        self.assertIsNone(event.message_id)
        self.assertEqual(
            [statement for statement, _ in self.connection.statements
             if "INSERT INTO investigation_outbox" in statement].__len__(), 1,
        )


_DISPOSABLE_DATABASE = re.compile(r"novamind_phase_c_test(?:_[a-z0-9_]{1,48})?$")


@unittest.skipUnless(os.environ.get("NOVAMIND_POSTGRES_TEST_DSN"), "NOVAMIND_POSTGRES_TEST_DSN is not configured")
class PostgreSQLOutboxIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ["NOVAMIND_POSTGRES_TEST_DSN"]
        parsed = urlsplit(dsn)
        database = unquote(parsed.path.lstrip("/"))
        if (parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname
                or not _DISPOSABLE_DATABASE.fullmatch(database)):
            raise RuntimeError("NOVAMIND_POSTGRES_TEST_DSN must target a disposable novamind_phase_c_test database")
        cls.dsn = dsn
        from psycopg import connect
        connection = connect(dsn, autocommit=True, connect_timeout=5)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                if cursor.fetchone()[0] != database:
                    raise RuntimeError("PostgreSQL database identity could not be verified")
                cursor.execute("DROP SCHEMA public CASCADE")
                cursor.execute("CREATE SCHEMA public")
        finally:
            connection.close()

    @classmethod
    def reset_schema(cls):
        from psycopg import connect
        connection = connect(cls.dsn, autocommit=True, connect_timeout=5)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                expected = unquote(urlsplit(cls.dsn).path.lstrip("/"))
                if cursor.fetchone()[0] != expected:
                    raise RuntimeError("PostgreSQL database identity changed")
                cursor.execute("DROP SCHEMA public CASCADE")
                cursor.execute("CREATE SCHEMA public")
        finally:
            connection.close()

    def setUp(self):
        self.reset_schema()
        self.store = PostgreSQLPersistence(self.dsn, initialize=True)
        self.cases = CaseService(PostgreSQLCaseRepository(self.store))
        self.jobs = PostgreSQLInvestigationJobRepository(self.store)
        self.outbox = PostgreSQLInvestigationOutboxRepository(self.store)
        self.control = PostgreSQLInvestigationControlRepository(self.store)
        self.customer = self.cases.create_customer(CustomerCreate(name="Outbox customer"))
        self.order = self.cases.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "OUTBOX-1", "name": "Outbox item", "quantity": 1}],
        ))

    def tearDown(self):
        self.reset_schema()

    def new_case(self):
        now = datetime.now(timezone.utc)
        return SupportCase(
            id=uuid4(), customer_id=self.customer.id, order_id=self.order.id,
            subject="Outbox case", description="Transactional outbox fixture",
            status=CaseStatus.OPEN, created_at=now, updated_at=now,
        )

    def pending_outbox(self):
        job = self.jobs.submit(
            self.new_case(), owner_user_id=uuid4(), idempotency_key=uuid4(),
        )[1]
        self.jobs.register_manual_start(
            job.id, admin_user_id=uuid4(), idempotency_key=uuid4(),
        )
        return self.outbox.get_by_job(job.id)

    def test_auto_customer_job_and_outbox_commit_once(self):
        admin = uuid4()
        self.control.set_mode(InvestigationMode.AUTO, expected_version=1,
                              admin_user_id=admin, idempotency_key=uuid4())
        case, job, created = self.jobs.submit(
            self.new_case(), owner_user_id=uuid4(), idempotency_key=uuid4(),
        )
        self.assertTrue(created)
        event = self.outbox.get_by_job(job.id)
        self.assertEqual(event.status, "PENDING")
        self.assertEqual(event.job_id, job.id)
        self.assertEqual(event.case_id, case.id)
        self.assertEqual(self.jobs.submit(
            case, owner_user_id=job.owner_user_id, idempotency_key=job.idempotency_key,
        )[2], False)
        with self.store.transaction() as connection:
            count = connection.execute(
                "SELECT count(*) FROM investigation_outbox WHERE job_id = ?",
                (str(job.id),),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_manual_start_and_outbox_are_atomic_and_idempotent(self):
        job = self.jobs.submit(
            self.new_case(), owner_user_id=uuid4(), idempotency_key=uuid4(),
        )[1]
        admin = uuid4()
        key = uuid4()
        started, created = self.jobs.register_manual_start(
            job.id, admin_user_id=admin, idempotency_key=key,
        )
        self.assertTrue(created)
        self.assertEqual(started.id, job.id)
        self.assertEqual(self.jobs.register_manual_start(
            job.id, admin_user_id=admin, idempotency_key=key,
        )[1], False)
        self.assertEqual(self.outbox.get_by_job(job.id).idempotency_key, key)

        # A fresh persistence/repository pair must observe the same intent;
        # this is the restart-durability boundary for Phase 1.
        restarted = PostgreSQLInvestigationOutboxRepository(
            PostgreSQLPersistence(self.dsn, initialize=False),
        )
        self.assertEqual(restarted.get_by_job(job.id), self.outbox.get_by_job(job.id))

    def test_manual_start_conflicting_key_rolls_back_without_second_outbox(self):
        job = self.jobs.submit(
            self.new_case(), owner_user_id=uuid4(), idempotency_key=uuid4(),
        )[1]
        admin = uuid4()
        key = uuid4()
        self.jobs.register_manual_start(job.id, admin_user_id=admin, idempotency_key=key)
        with self.assertRaises(ConflictError):
            self.jobs.register_manual_start(job.id, admin_user_id=admin, idempotency_key=uuid4())
        with self.store.transaction() as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM investigation_start_requests WHERE job_id = ?",
                (str(job.id),),
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM investigation_outbox WHERE job_id = ?",
                (str(job.id),),
            ).fetchone()[0], 1)

    def test_business_and_outbox_rows_rollback_together(self):
        case = self.new_case()
        job_id = uuid4()
        owner_user_id = uuid4()
        key = uuid4()
        fingerprint = "c" * 64
        with self.assertRaises(PersistenceError):
            with self.store.transaction() as connection:
                connection.execute(
                    """INSERT INTO support_cases
                       (case_id, customer_id, order_id, subject, description, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(case.id), str(case.customer_id), str(case.order_id), case.subject,
                     case.description, case.status.value, case.created_at.isoformat(), case.updated_at.isoformat()),
                )
                connection.execute(
                    """INSERT INTO investigation_jobs
                       (job_id, case_id, customer_id, owner_user_id, idempotency_key,
                        request_fingerprint, status, claim_id, claimed_by, claimed_at,
                        lease_expires_at, recovery_reason, created_at, updated_at,
                        submission_mode, mode_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(job_id), str(case.id), str(case.customer_id), str(owner_user_id),
                     str(key), fingerprint, "PENDING", None, None, None, None, None,
                     case.created_at.isoformat(), case.updated_at.isoformat(), "MANUAL", 1),
                )
                PostgreSQLInvestigationOutboxRepository.enqueue_in_transaction(
                    connection, job_id=job_id, case_id=case.id,
                    idempotency_key=key, request_fingerprint=fingerprint,
                    created_at=case.created_at,
                )
                raise RuntimeError("intentional rollback")
        with self.store.transaction() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM support_cases WHERE case_id = ?", (str(case.id),)
            ).fetchone())
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM investigation_jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone())
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM investigation_outbox"
            ).fetchone()[0], 0)

    def test_concurrent_publishers_have_one_winner(self):
        event = self.pending_outbox()
        observed = datetime.now(timezone.utc)

        def claim():
            repository = PostgreSQLInvestigationOutboxRepository(self.store)
            return repository.claim_pending(
                publisher_id=uuid4(), lease_seconds=30, max_attempts=5,
                now=observed,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = [future.result(timeout=10) for future in (
                pool.submit(claim), pool.submit(claim),
            )]
        self.assertEqual(sum(item is not None for item in claims), 1)
        self.assertEqual(next(item for item in claims if item is not None).outbox_id, event.outbox_id)

    def test_crash_after_send_window_reclaims_after_lease_expiry(self):
        event = self.pending_outbox()
        first_time = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        first = self.outbox.claim_pending(
            publisher_id=uuid4(), lease_seconds=1, max_attempts=3, now=first_time,
        )
        self.assertEqual(first.outbox_id, event.outbox_id)
        # Simulate a process crash after SQS accepted the message but before
        # mark_published committed.  No AWS call is made by this test.
        second = self.outbox.claim_pending(
            publisher_id=uuid4(), lease_seconds=1, max_attempts=3,
            now=first_time.replace(second=2),
        )
        self.assertEqual(second.outbox_id, event.outbox_id)
        self.assertEqual(second.publication_attempts, 2)

    def test_publish_failure_exhaustion_is_durable_and_not_claimable(self):
        event = self.pending_outbox()
        now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        lease = self.outbox.claim_pending(
            publisher_id=uuid4(), lease_seconds=30, max_attempts=1, now=now,
        )
        exhausted = self.outbox.record_publish_failure(
            outbox_id=lease.outbox_id, publisher_id=lease.leased_by,
            lease_id=lease.lease_id, error_category="SQS_PUBLISH_FAILED",
            max_attempts=1, now=now,
        )
        self.assertTrue(exhausted.retry_exhausted)
        self.assertIsNone(self.outbox.claim_pending(
            publisher_id=uuid4(), lease_seconds=30, max_attempts=5,
            now=now.replace(second=40),
        ))
