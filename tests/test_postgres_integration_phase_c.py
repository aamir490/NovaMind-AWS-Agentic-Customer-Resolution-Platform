"""Opt-in real-PostgreSQL verification for the Phase A persistence adapter.

The suite never falls back to SQLite and never chooses a database itself.  It
runs only when ``NOVAMIND_POSTGRES_TEST_DSN`` is supplied, and refuses every
database name except the disposable ``novamind_phase_c_test`` family before it
opens a connection.  Each test replaces that database's ``public`` schema.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import re
from threading import Barrier
from urllib.parse import unquote, urlsplit
import unittest
from uuid import UUID, uuid4

from langgraph.checkpoint.base import empty_checkpoint

from backend.app.agent import AgentResult
from backend.app.domain import CaseStatus, SupportCase
from backend.app.hitl import WorkflowResult
from backend.app.langgraph_checkpoints import SQLiteCheckpointSaver
from backend.app.operations import BusinessOperations
from backend.app.persistence import PersistenceError
from backend.app.postgres_persistence import (
    POSTGRES_MIGRATIONS, PostgreSQLCaseRepository,
    PostgreSQLCheckpointSaver, PostgreSQLCognitoCustomerBindingRepository,
    PostgreSQLInvestigationJobRepository, PostgreSQLInvestigationResultRepository,
    PostgreSQLPersistence,
    PostgreSQLProposalRepository, PostgreSQLWorkflowMetadataRepository,
)
from backend.app.proposals import HumanReview, ProposalCreate, ProposalService, ProposalStatus
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.security import CognitoCustomerBinding
from backend.app.service import CaseService, ConflictError, NotFoundError
from backend.app.workflow_metadata import WorkflowRunReservation


_DISPOSABLE_DATABASE = re.compile(r"novamind_phase_c_test(?:_[a-z0-9_]{1,48})?$")


@unittest.skipUnless(
    os.environ.get("NOVAMIND_POSTGRES_TEST_DSN"),
    "NOVAMIND_POSTGRES_TEST_DSN is not configured",
)
class PostgreSQLPhaseCIntegrationTests(unittest.TestCase):
    """Live-database coverage; skipped unless an explicitly disposable DSN is set."""

    @classmethod
    def setUpClass(cls):
        dsn = os.environ.get("NOVAMIND_POSTGRES_TEST_DSN")
        if not dsn:
            raise unittest.SkipTest("NOVAMIND_POSTGRES_TEST_DSN is not configured")
        parsed = urlsplit(dsn)
        database = unquote(parsed.path.lstrip("/"))
        if (parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname
                or not _DISPOSABLE_DATABASE.fullmatch(database)):
            raise RuntimeError(
                "NOVAMIND_POSTGRES_TEST_DSN must target a novamind_phase_c_test disposable database"
            )
        cls.dsn = dsn
        cls._assert_connected_database(database)
        cls._reset_public_schema()

    @classmethod
    def _raw_connection(cls, *, autocommit=True):
        from psycopg import connect
        return connect(cls.dsn, autocommit=autocommit, connect_timeout=5)

    @classmethod
    def _assert_connected_database(cls, expected_database):
        connection = cls._raw_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                if cursor.fetchone()[0] != expected_database:
                    raise RuntimeError("PostgreSQL test database identity could not be verified")
        finally:
            connection.close()

    @classmethod
    def _reset_public_schema(cls):
        """Erase only the already-verified disposable database between tests."""
        connection = cls._raw_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA public CASCADE")
                cursor.execute("CREATE SCHEMA public")
        finally:
            connection.close()

    def setUp(self):
        self._reset_public_schema()
        self.store = PostgreSQLPersistence(self.dsn, initialize=True)
        self.cases = CaseService(PostgreSQLCaseRepository(self.store))
        self.jobs = PostgreSQLInvestigationJobRepository(self.store)
        self.bindings = PostgreSQLCognitoCustomerBindingRepository(self.store)
        self.metadata = PostgreSQLWorkflowMetadataRepository(self.store)
        self.proposal_repository = PostgreSQLProposalRepository(self.store)
        self.proposals = ProposalService(
            self.cases, BusinessOperations(self.cases), self.proposal_repository,
        )
        self.customer = self.cases.create_customer(CustomerCreate(name="Phase C customer"))
        self.order = self.cases.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "PG-C-1", "name": "PostgreSQL test item", "quantity": 1}],
        ))
        self.case = self.cases.create_case(CaseCreate(
            customer_id=self.customer.id, order_id=self.order.id,
            subject="PostgreSQL integration fixture",
            description="A disposable real-PostgreSQL Phase C fixture.",
        ))

    def tearDown(self):
        self._reset_public_schema()

    def _pending_case(self):
        now = datetime.now(timezone.utc)
        return SupportCase(
            id=uuid4(), customer_id=self.customer.id, order_id=self.order.id,
            subject="Durable pending investigation",
            description="Verify PostgreSQL claim and HITL safeguards.",
            status=CaseStatus.OPEN, created_at=now, updated_at=now,
        )

    def _submit_job(self):
        return self.jobs.submit(
            self._pending_case(), owner_user_id=uuid4(), idempotency_key=uuid4(),
        )[1]

    def _reserve_checkpoint_workflow(self):
        workflow_id = uuid4()
        self.metadata.reserve(WorkflowRunReservation(
            workflow_id=workflow_id, run_id=uuid4(), case_id=self.case.id,
            owner_user_id=uuid4(), request_id=uuid4(),
            request_fingerprint=sha256(str(workflow_id).encode("ascii")).hexdigest(),
            provider="local_scripted",
        ))
        return workflow_id

    @staticmethod
    def _checkpoint(value):
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"state": value}
        checkpoint["channel_versions"] = {"state": "1"}
        checkpoint["versions_seen"] = {}
        return checkpoint

    @staticmethod
    def _checkpoint_config(workflow_id, checkpoint_id=None):
        configurable = {"thread_id": str(workflow_id), "checkpoint_ns": ""}
        if checkpoint_id is not None:
            configurable["checkpoint_id"] = checkpoint_id
        return {"configurable": configurable}

    def test_migrations_create_current_postgres_schema(self):
        with self.store.transaction() as connection:
            versions = [row["version"] for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()]
            self.assertEqual(versions, [migration.version for migration in POSTGRES_MIGRATIONS])
            for table in (
                "customers", "support_cases", "workflow_runs", "workflow_audit_events",
                "investigation_jobs", "langgraph_checkpoints", "langgraph_checkpoint_writes",
                "cognito_customer_bindings", "investigation_results",
            ):
                self.assertIsNotNone(connection.execute(f"SELECT 1 FROM {table} LIMIT 0"))
            trigger_rows = connection.execute(
                """SELECT tgname FROM pg_trigger
                   WHERE tgrelid = 'investigation_jobs'::regclass AND NOT tgisinternal"""
            ).fetchall()
        self.assertIn("job_guard", {row["tgname"] for row in trigger_rows})

    def _claim_and_attempt(self, *, lease_seconds=60, now=None):
        job = self._submit_job()
        worker_id = uuid4()
        claim = self.jobs.claim_next(
            worker_id=worker_id, lease_seconds=lease_seconds, now=now,
        )
        self.assertIsNotNone(claim)
        reservation = self.jobs.bind_claim(
            job.id, worker_id=worker_id, claim_id=claim.claim_id,
            provider="bedrock", now=now,
        )
        attempt = self.jobs.mark_execution_attempt(
            job.id, worker_id=worker_id, claim_id=claim.claim_id, now=now,
        )
        return job, worker_id, claim, reservation, attempt

    @staticmethod
    def _completed_result(workflow_id, *, message="Safe informational result"):
        return WorkflowResult(
            workflow_id=workflow_id, status="COMPLETED", message=message,
            agent_result=AgentResult(
                status="INFORMATIONAL", customer_response=message,
                steps_used=1, history=(), actions_executed=False,
            ),
            actions_executed=False,
        )

    def test_migration_11_creates_result_schema_with_actions_guard(self):
        with self.store.transaction() as connection:
            columns = {
                row["column_name"]: row for row in connection.execute(
                    """SELECT column_name, is_nullable
                       FROM information_schema.columns
                       WHERE table_schema = 'public' AND table_name = 'investigation_results'"""
                ).fetchall()
            }
        self.assertEqual(set(columns), {
            "result_id", "job_id", "workflow_id", "execution_attempt_id", "case_id",
            "status", "result_json", "result_hash", "actions_executed", "created_at",
        })
        self.assertEqual(columns["actions_executed"]["is_nullable"], "NO")

    def test_fenced_result_persists_duplicate_is_idempotent_and_survives_restart(self):
        job, worker_id, claim, reservation, attempt = self._claim_and_attempt()
        results = PostgreSQLInvestigationResultRepository(self.store)
        result = self._completed_result(reservation.run.workflow_id)
        persisted = results.record_completed(
            job.id, worker_id=worker_id, claim_id=claim.claim_id,
            attempt_id=attempt.attempt_id, result=result,
        )
        self.assertEqual(results.record_completed(
            job.id, worker_id=worker_id, claim_id=claim.claim_id,
            attempt_id=attempt.attempt_id, result=result,
        ), persisted)
        self.assertFalse(persisted["actions_executed"])
        restarted = PostgreSQLInvestigationResultRepository(
            PostgreSQLPersistence(self.dsn)
        )
        self.assertEqual(restarted.get_by_job(job.id), persisted)
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT status, actions_executed FROM investigation_results WHERE job_id = ?",
                (str(job.id),),
            ).fetchone()
        self.assertEqual(row["status"], "COMPLETED")
        self.assertFalse(row["actions_executed"])

    def test_conflicting_result_is_rejected_without_overwrite(self):
        job, worker_id, claim, reservation, attempt = self._claim_and_attempt()
        results = PostgreSQLInvestigationResultRepository(self.store)
        result = self._completed_result(reservation.run.workflow_id)
        persisted = results.record_completed(
            job.id, worker_id=worker_id, claim_id=claim.claim_id,
            attempt_id=attempt.attempt_id, result=result,
        )
        with self.assertRaises(ConflictError):
            results.record_completed(
                job.id, worker_id=worker_id, claim_id=claim.claim_id,
                attempt_id=attempt.attempt_id,
                result=self._completed_result(
                    reservation.run.workflow_id, message="Conflicting result",
                ),
            )
        self.assertEqual(results.get_by_job(job.id), persisted)

    def test_expired_lease_cannot_persist_result_and_restart_sees_no_result(self):
        claimed_at = datetime.now(timezone.utc)
        job, worker_id, claim, reservation, attempt = self._claim_and_attempt(
            lease_seconds=1, now=claimed_at,
        )
        results = PostgreSQLInvestigationResultRepository(self.store)
        with self.assertRaises(ConflictError):
            results.record_completed(
                job.id, worker_id=worker_id, claim_id=claim.claim_id,
                attempt_id=attempt.attempt_id,
                result=self._completed_result(reservation.run.workflow_id),
                now=claimed_at + timedelta(seconds=2),
            )
        restarted = PostgreSQLInvestigationResultRepository(
            PostgreSQLPersistence(self.dsn)
        )
        self.assertIsNone(restarted.get_by_job(job.id))

    def test_result_transaction_rolls_back_and_completion_does_not_resolve_case(self):
        job, worker_id, claim, reservation, attempt = self._claim_and_attempt()
        result = self._completed_result(reservation.run.workflow_id)
        serialized = json.dumps(
            result.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        )
        with self.assertRaisesRegex(PersistenceError, "^PERSISTENCE_UNAVAILABLE$"):
            with self.store.transaction() as connection:
                connection.execute(
                    """INSERT INTO investigation_results
                       (result_id, job_id, workflow_id, execution_attempt_id, case_id,
                        status, result_json, result_hash, actions_executed, created_at)
                       VALUES (?, ?, ?, ?, ?, 'COMPLETED', ?, ?, FALSE, ?)""",
                    (str(uuid4()), str(job.id), str(reservation.run.workflow_id),
                     str(attempt.attempt_id), str(job.case_id), serialized,
                     "f" * 64, datetime.now(timezone.utc).isoformat()),
                )
                raise RuntimeError("intentional result transaction rollback")
        with self.store.transaction() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM investigation_results WHERE job_id = ?", (str(job.id),)
            ).fetchone())
        PostgreSQLInvestigationResultRepository(self.store).record_completed(
            job.id, worker_id=worker_id, claim_id=claim.claim_id,
            attempt_id=attempt.attempt_id, result=result,
        )
        self.assertEqual(self.metadata.get(job.id).status.value, "COMPLETED")
        self.assertNotEqual(self.cases.get_case(job.case_id).status, CaseStatus.RESOLVED)

    def test_failed_transaction_rolls_back_domain_data(self):
        customer_id = uuid4()
        with self.assertRaises(PersistenceError):
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO customers(customer_id, name) VALUES (?, ?)",
                    (str(customer_id), "Rolled back customer"),
                )
                raise RuntimeError("intentional transaction rollback")
        with self.assertRaises(NotFoundError):
            self.cases.get_customer(customer_id)

    def test_customer_binding_is_immutable_and_survives_restart(self):
        binding = CognitoCustomerBinding(subject_id=uuid4(), customer_id=self.customer.id)
        self.assertEqual(self.bindings.bind(binding), binding)
        reopened = PostgreSQLCognitoCustomerBindingRepository(PostgreSQLPersistence(self.dsn))
        self.assertEqual(reopened.customer_id_for_subject(binding.subject_id), self.customer.id)
        with self.assertRaises(ConflictError):
            reopened.bind(CognitoCustomerBinding(subject_id=uuid4(), customer_id=self.customer.id))
        with self.assertRaises(PersistenceError):
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE cognito_customer_bindings SET customer_id = ? WHERE subject_id = ?",
                    (str(uuid4()), str(binding.subject_id)),
                )
        self.assertEqual(reopened.customer_id_for_subject(binding.subject_id), self.customer.id)

    def test_langgraph_checkpoint_and_pending_write_survive_restart(self):
        self.assertTrue(issubclass(PostgreSQLCheckpointSaver, SQLiteCheckpointSaver))
        workflow_id = self._reserve_checkpoint_workflow()
        config = self._checkpoint_config(workflow_id)
        checkpoint = self._checkpoint({"phase": "review_required"})
        saver = PostgreSQLCheckpointSaver(self.store)
        saved = saver.put(config, checkpoint, {"source": "phase-c", "step": 1}, {"state": "1"})
        saver.put_writes(saved, (("review", {"decision": "pending"}),), "phase-c-task")

        restarted = PostgreSQLCheckpointSaver(PostgreSQLPersistence(self.dsn))
        restored = restarted.get_tuple(config)
        self.assertEqual(restored.checkpoint, checkpoint)
        self.assertEqual(restored.metadata["source"], "phase-c")
        self.assertEqual(restored.pending_writes, [
            ("phase-c-task", "review", {"decision": "pending"}),
        ])

    def test_skip_locked_claim_ignores_a_row_locked_by_another_transaction(self):
        first, second = self._submit_job(), self._submit_job()
        connection = self._raw_connection(autocommit=False)
        try:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT job_id FROM investigation_jobs
                           ORDER BY created_at, job_id LIMIT 1 FOR UPDATE"""
                    )
                    locked_job = UUID(cursor.fetchone()[0])
                    claim = PostgreSQLInvestigationJobRepository(
                        PostgreSQLPersistence(self.dsn)
                    ).claim_next(worker_id=uuid4())
                    self.assertIsNotNone(claim)
                    self.assertNotEqual(claim.job.id, locked_job)
        finally:
            connection.close()
        self.assertIn(locked_job, {first.id, second.id})

    def test_competing_claims_have_unique_winners_and_restart_durability(self):
        first, second = self._submit_job(), self._submit_job()
        workers = (uuid4(), uuid4())
        barrier = Barrier(2)

        def claim(worker_id):
            barrier.wait(timeout=5)
            repository = PostgreSQLInvestigationJobRepository(PostgreSQLPersistence(self.dsn))
            return repository.claim_next(worker_id=worker_id, lease_seconds=60)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, workers))
        self.assertTrue(all(claims))
        self.assertEqual({claim.job.id for claim in claims}, {first.id, second.id})

        restarted = PostgreSQLInvestigationJobRepository(PostgreSQLPersistence(self.dsn))
        self.assertEqual({
            restarted.get_by_case(claim.job.case_id).claim_id for claim in claims
        }, {claim.claim_id for claim in claims})

    def test_stale_lease_is_fenced_before_workflow_binding(self):
        job = self._submit_job()
        worker_id = uuid4()
        claimed_at = datetime.now(timezone.utc)
        claim = self.jobs.claim_next(worker_id=worker_id, lease_seconds=1, now=claimed_at)
        self.assertIsNotNone(claim)

        with self.assertRaises(ConflictError):
            self.jobs.bind_claim(
                job.id, worker_id=worker_id, claim_id=claim.claim_id,
                provider="bedrock", now=claimed_at + timedelta(seconds=2),
            )

        recovered = self.jobs.get_by_case(job.case_id)
        self.assertEqual(recovered.status.value, "RECOVERY_REQUIRED")
        self.assertIsNone(recovered.workflow_id)

    def test_proposal_binding_is_idempotent_and_conflicts_are_durable(self):
        proposal = self.proposals.create(ProposalCreate(
            case_id=self.case.id, action="replacement",
            rationale="Persist one deterministic review binding.",
        ))
        workflow_id, review_id = uuid4(), uuid4()
        binding = self.proposals.bind_workflow_review(
            proposal.id, workflow_id=workflow_id, case_id=self.case.id, review_id=review_id,
        )
        self.assertEqual(
            self.proposals.bind_workflow_review(
                proposal.id, workflow_id=workflow_id,
                case_id=self.case.id, review_id=review_id,
            ),
            binding,
        )
        with self.assertRaises(ConflictError):
            self.proposals.bind_workflow_review(
                proposal.id, workflow_id=uuid4(), case_id=self.case.id, review_id=uuid4(),
            )

        reopened = ProposalService(
            self.cases, BusinessOperations(self.cases),
            PostgreSQLProposalRepository(PostgreSQLPersistence(self.dsn)),
        )
        persisted = reopened.get(proposal.id)
        self.assertEqual(persisted.status, ProposalStatus.PENDING_REVIEW)
        events = self.proposal_repository.audit_events(workflow_id)
        self.assertEqual([event["kind"] for event in events], ["REVIEW_REQUIRED"])
        self.assertTrue(all(event["actions_executed"] is False for event in events))

    def test_hitl_review_requires_the_exact_persisted_pause_and_is_terminal(self):
        proposal = self.proposals.create(ProposalCreate(
            case_id=self.case.id, action="replacement",
            rationale="A human reviewer must decide this PostgreSQL proposal.",
        ))
        workflow_id, review_id = uuid4(), uuid4()
        self.proposals.bind_workflow_review(
            proposal.id, workflow_id=workflow_id, case_id=self.case.id, review_id=review_id,
        )
        review = HumanReview(reviewer_name=str(uuid4()), note="Authorized human review.")
        with self.assertRaises(ConflictError):
            self.proposals.review(proposal.id, ProposalStatus.APPROVED, review)
        with self.assertRaises(ConflictError):
            self.proposals.review(
                proposal.id, ProposalStatus.APPROVED, review,
                workflow_id=workflow_id, review_id=uuid4(),
            )
        reviewed = self.proposals.review(
            proposal.id, ProposalStatus.APPROVED, review,
            workflow_id=workflow_id, review_id=review_id,
        )
        self.assertEqual(reviewed.status, ProposalStatus.APPROVED)
        self.assertFalse(self.proposal_repository.audit_events(workflow_id)[-1]["actions_executed"])
        restarted = PostgreSQLProposalRepository(PostgreSQLPersistence(self.dsn))
        self.assertEqual(restarted.get(proposal.id).status, ProposalStatus.APPROVED)
        with self.assertRaises(ConflictError):
            self.proposals.review(
                proposal.id, ProposalStatus.REJECTED, review,
                workflow_id=workflow_id, review_id=review_id,
            )
        with self.assertRaises(ConflictError):
            self.proposals.review(
                proposal.id, ProposalStatus.APPROVED, review,
                workflow_id=workflow_id, review_id=review_id,
            )
        events = self.proposal_repository.audit_events(workflow_id)
        self.assertEqual([event["kind"] for event in events], ["REVIEW_REQUIRED", "HUMAN_REVIEW"])
        self.assertEqual(events[-1]["proposal_status"], ProposalStatus.APPROVED.value)
        self.assertFalse(events[-1]["actions_executed"])


if __name__ == "__main__":
    unittest.main()
