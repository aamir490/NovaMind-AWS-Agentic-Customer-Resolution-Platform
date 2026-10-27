"""Offline Phase A PostgreSQL persistence contract coverage.

These tests deliberately use a small DB-API fake.  They verify migrations and
PostgreSQL-specific locking/query adaptation without provisioning a database or
opening a network connection.
"""

from contextlib import contextmanager
import unittest

from backend.app.main import create_app
from backend.app.domain import InvestigationControl, InvestigationModeAuditEvent
from backend.app.persistence import (
    PersistenceError, SQLiteCaseRepository, SQLiteCognitoCustomerBindingRepository,
    SQLiteInvestigationControlRepository, SQLiteInvestigationJobRepository,
    SQLiteProposalRepository, SQLiteRefundEvidenceRepository,
    SQLiteWorkflowMetadataRepository,
)
from backend.app.postgres_persistence import (
    POSTGRES_MIGRATIONS, PostgreSQLCheckpointSaver,
    PostgreSQLCaseRepository, PostgreSQLCognitoCustomerBindingRepository,
    PostgreSQLHITLReviewService,
    PostgreSQLInvestigationControlRepository,
    PostgreSQLInvestigationJobRepository, PostgreSQLPersistence,
    PostgreSQLProposalRepository, PostgreSQLRefundEvidenceRepository,
    PostgreSQLWorkflowMetadataRepository,
    _PostgreSQLCursor, _PostgreSQLRow, _qmark_to_pyformat,
)
from backend.app.langgraph_checkpoints import SQLiteCheckpointSaver
from backend.app.proposals import HumanReview, ProposalStatus
from backend.app.service import ConflictError, NotFoundError


class _FakeCursor:
    def __init__(self, database):
        self.database = database
        self.rows = []
        self.rowcount = -1

    def execute(self, statement, parameters=None):
        self.database.statements.append((statement, parameters))
        self.rows = []
        self.rowcount = -1
        if statement.startswith("SELECT version FROM schema_migrations"):
            self.rows = [{"version": version} for version in sorted(self.database.versions)]
        elif statement.startswith("INSERT INTO schema_migrations"):
            self.database.versions.add(parameters[0])
            self.rowcount = 1
        elif "FROM investigation_jobs" in statement and statement.lstrip().startswith("SELECT"):
            self.rows = []
        return self

    def executemany(self, statement, parameters):
        self.database.statements.append((statement, parameters))
        self.rowcount = len(parameters)
        return self

    def fetchone(self):
        return None if not self.rows else self.rows[0]

    def fetchall(self):
        return list(self.rows)

    def close(self):
        return None


class _FakeConnection:
    def __init__(self, database):
        self.database = database

    def cursor(self, **_kwargs):
        return _FakeCursor(self.database)

    @contextmanager
    def transaction(self):
        yield self

    def close(self):
        return None


class _FakeDatabase:
    def __init__(self):
        self.versions = set()
        self.statements = []

    def connect(self, dsn):
        self.dsn = dsn
        return _FakeConnection(self)


class _OrderItemCursor:
    def __init__(self):
        self.statements = []

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        return self

    def fetchall(self):
        return [
            {"sku": "SKU-2", "name": "Second inserted", "quantity": 1},
            {"sku": "SKU-1", "name": "First alphabetically", "quantity": 2},
        ]


class PostgreSQLPersistencePhaseATests(unittest.TestCase):
    DSN = "postgresql://novamind:password@db.internal:5432/novamind"

    def setUp(self):
        self.database = _FakeDatabase()

    def persistence(self, *, initialize=True):
        return PostgreSQLPersistence(
            self.DSN, initialize=initialize, connection_factory=self.database.connect,
        )

    def test_versioned_migrations_create_durable_domain_job_audit_and_checkpoint_schema(self):
        self.persistence()
        self.assertEqual(
            self.database.versions,
            {migration.version for migration in POSTGRES_MIGRATIONS},
        )
        statements = "\n".join(statement for statement, _ in self.database.statements)
        for table in (
            "support_cases", "workflow_audit_events", "investigation_jobs",
            "langgraph_checkpoints", "langgraph_checkpoint_writes",
        ):
            self.assertIn(table, statements)
        self.assertIn("novamind_workflow_guard", statements)
        self.assertIn("novamind_job_guard", statements)
        self.assertNotIn("sqlite", statements.lower())

    def test_control_bootstrap_timestamp_migration_preserves_strict_domain_validation(self):
        canonical_timestamp = "2026-10-23T02:30:27.892336Z"
        control = InvestigationControl.model_validate({
            "mode": "MANUAL", "version": 1, "updated_by": None,
            "updated_at": canonical_timestamp,
        })
        audit = InvestigationModeAuditEvent.model_validate({
            "sequence": 1, "previous_mode": None, "mode": "MANUAL", "version": 1,
            "admin_user_id": None, "idempotency_key": None,
            "changed_at": canonical_timestamp,
        })
        self.assertEqual(control.mode.value, "MANUAL")
        self.assertEqual(audit.mode.value, "MANUAL")
        migration = next(item for item in POSTGRES_MIGRATIONS if item.version == 7)
        statements = "\n".join(migration.statements)
        self.assertIn("UPDATE investigation_control", statements)
        self.assertIn("AT TIME ZONE 'UTC'", statements)

    def test_failure_metadata_migration_is_additive_and_closed(self):
        migration = next(item for item in POSTGRES_MIGRATIONS if item.version == 8)
        statements = "\n".join(migration.statements)
        self.assertIn("ADD COLUMN failure_category", statements)
        self.assertIn("ADD COLUMN failure_stage", statements)
        self.assertIn("workflow_failure_metadata_paired", statements)
        self.assertIn("invalid workflow status transition", statements)
        self.assertNotIn("exception_message", statements.lower())

    def test_legacy_failure_transition_compatibility_is_a_follow_up_migration(self):
        migration_8 = next(item for item in POSTGRES_MIGRATIONS if item.version == 8)
        migration_9 = next(item for item in POSTGRES_MIGRATIONS if item.version == 9)
        original = "\n".join(migration_8.statements)
        compatibility = "\n".join(migration_9.statements)
        self.assertNotIn("NEW.failure_category :=", original)
        self.assertIn("NEW.status = 'FAILED'", compatibility)
        self.assertIn("NEW.failure_category IS NULL AND NEW.failure_stage IS NULL", compatibility)
        self.assertIn("NEW.failure_category := 'WORKFLOW_ERROR'", compatibility)
        self.assertIn("NEW.failure_stage := 'WORKFLOW'", compatibility)
        self.assertIn("NEW.status <> 'FAILED'", compatibility)
        self.assertIn("invalid workflow status transition", compatibility)
        self.assertNotIn("exception_message", compatibility.lower())

    def test_invalid_response_diagnostics_are_added_by_a_closed_follow_up_migration(self):
        migration = next(item for item in POSTGRES_MIGRATIONS if item.version == 10)
        statements = "\n".join(migration.statements)
        self.assertIn("ADD COLUMN invalid_response_stage", statements)
        self.assertIn("ADD COLUMN invalid_response_category", statements)
        self.assertIn("NEW.failure_category = 'INVALID_RESPONSE'", statements)
        self.assertIn("NEW.failure_stage = 'AGENT'", statements)
        self.assertIn("provider_envelope", statements)
        self.assertIn("bedrock_empty_output", statements)
        self.assertNotIn("exception_message", statements.lower())

    def test_legacy_postgres_timestamp_is_normalized_only_at_the_strict_read_boundary(self):
        control = SQLiteInvestigationControlRepository._control({
            "mode": "MANUAL", "version": 1, "updated_by": None,
            "updated_at": "2026-10-23 02:30:27.892336+00",
        })
        event = SQLiteInvestigationControlRepository._event({
            "sequence": 1, "previous_mode": None, "mode": "MANUAL", "version": 1,
            "admin_user_id": None, "idempotency_key": None,
            "changed_at": "2026-10-23 02:30:27.892336+00",
        })
        self.assertEqual(control.updated_at.isoformat(), "2026-10-23T02:30:27.892336+00:00")
        self.assertEqual(event.changed_at.isoformat(), "2026-10-23T02:30:27.892336+00:00")
        with self.assertRaises(PersistenceError):
            SQLiteInvestigationControlRepository._control({
                "mode": "MANUAL", "version": 1, "updated_by": None,
                "updated_at": "2026-10-23 02:30:27+000",
            })

    def test_qmark_adapter_preserves_quoted_question_marks_and_sqlite_row_contract(self):
        self.assertEqual(
            _qmark_to_pyformat("SELECT '?' AS literal, value FROM sample WHERE id = ?"),
            "SELECT '?' AS literal, value FROM sample WHERE id = %s",
        )
        row = _PostgreSQLRow({"first": "a", "second": "b"})
        self.assertEqual((row["first"], row[1], tuple(row)), ("a", "b", ("a", "b")))
        self.assertEqual(dict(row), {"first": "a", "second": "b"})

    def test_cursor_preserves_batch_order_inserts_and_marks_rowid_unsupported(self):
        cursor = _FakeCursor(self.database)
        wrapped = _PostgreSQLCursor(cursor)
        wrapped.executemany(
            "INSERT INTO order_items(order_id, sku) VALUES (?, ?)",
            [("order-1", "sku-1"), ("order-1", "sku-2")],
        )
        self.assertFalse(wrapped.supports_rowid)
        self.assertEqual(
            self.database.statements[-1],
            (
                "INSERT INTO order_items(order_id, sku) VALUES (%s, %s)",
                [("order-1", "sku-1"), ("order-1", "sku-2")],
            ),
        )

    def test_order_item_read_uses_legacy_rowid_for_sqlite_and_sku_for_postgres(self):
        row = {
            "order_id": "00000000-0000-4000-8000-000000000001",
            "customer_id": "00000000-0000-4000-8000-000000000002",
        }
        sqlite_cursor = _OrderItemCursor()
        sqlite_order = SQLiteCaseRepository._order(sqlite_cursor, row)
        postgres_cursor = _OrderItemCursor()
        postgres_order = SQLiteCaseRepository._order(_PostgreSQLCursor(postgres_cursor), row)

        self.assertEqual([item.sku for item in sqlite_order.items], ["SKU-2", "SKU-1"])
        self.assertEqual(postgres_order, sqlite_order)
        self.assertIn("ORDER BY rowid", sqlite_cursor.statements[-1][0])
        self.assertIn("ORDER BY sku", postgres_cursor.statements[-1][0])

    def test_claim_next_uses_postgres_skip_locked_without_claiming_a_job(self):
        persistence = self.persistence()
        repository = PostgreSQLInvestigationJobRepository(persistence)
        self.assertIsNone(repository.claim_next(worker_id="00000000-0000-4000-8000-000000000001"))
        statements = [statement for statement, _ in self.database.statements]
        self.assertTrue(any("FOR UPDATE SKIP LOCKED" in statement for statement in statements))
        self.assertTrue(any(
            statement == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
            for statement in statements
        ))

    def test_postgres_business_repositories_are_native_and_not_sqlite_inherited(self):
        pairs = (
            (PostgreSQLCaseRepository, SQLiteCaseRepository),
            (PostgreSQLCognitoCustomerBindingRepository, SQLiteCognitoCustomerBindingRepository),
            (PostgreSQLInvestigationControlRepository, SQLiteInvestigationControlRepository),
            (PostgreSQLInvestigationJobRepository, SQLiteInvestigationJobRepository),
            (PostgreSQLRefundEvidenceRepository, SQLiteRefundEvidenceRepository),
            (PostgreSQLProposalRepository, SQLiteProposalRepository),
            (PostgreSQLWorkflowMetadataRepository, SQLiteWorkflowMetadataRepository),
        )
        for postgres_type, sqlite_type in pairs:
            self.assertNotIn(sqlite_type, postgres_type.__mro__)
            repository = postgres_type(self.persistence())
            self.assertNotIn("_delegate", repository.__dict__)
            self.assertIsInstance(repository._persistence, PostgreSQLPersistence)

        self.assertTrue(callable(getattr(
            PostgreSQLInvestigationJobRepository(self.persistence()), "claim_next", None,
        )))

    def test_postgres_hitl_service_is_repository_only_and_preserves_review_contract(self):
        calls = []

        class Repository:
            def review(self, proposal_id, decision, review, *, workflow_id, review_id):
                calls.append((proposal_id, decision, review, workflow_id, review_id))
                return "persisted-review"

        service = PostgreSQLHITLReviewService(Repository())
        result = service.review(
            "proposal-id", "APPROVED", HumanReview(reviewer_name="reviewer", note="approved"),
            workflow_id="workflow-id", review_id="review-id",
        )
        self.assertEqual(result, "persisted-review")
        self.assertEqual(calls[0][1], ProposalStatus.APPROVED)
        self.assertEqual(calls[0][2].note, "approved")
        with self.assertRaisesRegex(ConflictError, "APPROVED or REJECTED"):
            service.review(
                "proposal-id", "PENDING_REVIEW",
                HumanReview(reviewer_name="reviewer", note="invalid"),
            )

    def test_postgres_checkpoint_saver_preserves_the_custom_saver_contract(self):
        self.assertTrue(issubclass(PostgreSQLCheckpointSaver, SQLiteCheckpointSaver))

    def test_invalid_dsn_and_mixed_backend_configuration_fail_closed(self):
        with self.assertRaisesRegex(Exception, "PERSISTENCE_UNAVAILABLE"):
            PostgreSQLPersistence("postgresql://missing-database", connection_factory=self.database.connect)
        with self.assertRaisesRegex(ValueError, "only one durable persistence backend"):
            create_app(sqlite_path="C:/temporary/novamind.sqlite", postgres_dsn=self.DSN)

    def test_domain_conflicts_and_not_found_outcomes_survive_postgres_transactions(self):
        persistence = self.persistence()
        for error in (ConflictError("conflict"), NotFoundError("missing")):
            with self.assertRaises(type(error)):
                with persistence.transaction():
                    raise error
        with self.assertRaisesRegex(PersistenceError, "PERSISTENCE_UNAVAILABLE"):
            with persistence.transaction():
                raise RuntimeError("unexpected persistence body failure")


if __name__ == "__main__":
    unittest.main()
