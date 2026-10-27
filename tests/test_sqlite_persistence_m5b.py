"""Focused M5B SQLite schema and transaction tests; no service wiring."""

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from backend.app.persistence import MIGRATIONS, PersistenceError, SQLitePersistence


class SQLitePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "novamind.sqlite"

    def store(self):
        return SQLitePersistence(self.path, initialize=True)

    def create_database_through(self, version):
        """Build a real historical database without using the current migrator."""
        self.path.touch()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            for migration in MIGRATIONS:
                if migration.version > version:
                    break
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration.version, "2026-01-01T00:00:00Z"),
                )
            connection.commit()
        finally:
            connection.close()

    def create_v6_database(self):
        self.create_database_through(6)

    def insert_case(self, store):
        with store.transaction() as connection:
            connection.execute("INSERT INTO customers VALUES (?, ?)", ("customer-1", "Customer"))
            connection.execute("INSERT INTO orders VALUES (?, ?, ?)", ("order-1", "customer-1", "2026-01-01T00:00:00Z"))
            connection.execute("INSERT INTO order_items VALUES (?, ?, ?, ?)", ("order-1", "LAP-1", "Laptop", 1))
            connection.execute(
                "INSERT INTO support_cases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("case-1", "customer-1", "order-1", "Issue", "Description", "open",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )

    def test_initialization_applies_all_versioned_migrations(self):
        store = self.store()
        with store.transaction() as connection:
            versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(versions, [migration.version for migration in MIGRATIONS])
        self.assertTrue({"customers", "delivery_facts", "eligibility_evidence", "resolution_proposals",
                         "workflow_review_bindings", "workflow_audit_events", "workflow_runs",
                         "case_customer_communications", "investigation_control",
                         "investigation_mode_audit", "investigation_start_requests"} <= tables)

    def test_migrations_are_repeatable_without_schema_changes(self):
        self.store()
        reopened = SQLitePersistence(self.path)
        with reopened.transaction() as connection:
            versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        self.assertEqual(versions, [migration.version for migration in MIGRATIONS])

    def test_v6_database_upgrades_case_constraint_without_losing_references(self):
        self.create_v6_database()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("INSERT INTO customers VALUES (?, ?)", ("customer-1", "Customer"))
            connection.execute(
                "INSERT INTO orders VALUES (?, ?, ?)",
                ("order-1", "customer-1", "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                "INSERT INTO order_items VALUES (?, ?, ?, ?)",
                ("order-1", "LAP-1", "Laptop", 1),
            )
            connection.execute(
                "INSERT INTO support_cases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("case-1", "customer-1", "order-1", "Issue", "Description", "in_review",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                """INSERT INTO resolution_proposals
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("proposal-1", "case-1", "replacement", "Replacement proposal", None,
                 "PENDING_REVIEW", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
                 None, None, None),
            )
            connection.commit()
        finally:
            connection.close()

        store = SQLitePersistence(self.path)
        with store.transaction() as migrated:
            versions = [row[0] for row in migrated.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )]
            migrated.execute(
                "UPDATE support_cases SET status = 'resolved', updated_at = ? WHERE case_id = ?",
                ("2026-01-02T00:00:00Z", "case-1"),
            )
            self.assertEqual(migrated.execute(
                "SELECT status FROM support_cases WHERE case_id = ?", ("case-1",)
            ).fetchone()[0], "resolved")
            self.assertEqual(migrated.execute(
                "SELECT case_id FROM resolution_proposals WHERE proposal_id = ?", ("proposal-1",)
            ).fetchone()[0], "case-1")
            self.assertIsNone(migrated.execute("PRAGMA foreign_key_check").fetchone())
        self.assertEqual(versions, [migration.version for migration in MIGRATIONS])

    def test_v7_database_upgrades_to_add_decision_history_schema(self):
        self.create_database_through(7)

        store = SQLitePersistence(self.path)
        with store.transaction() as migrated:
            versions = [row[0] for row in migrated.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )]
            tables = {row[0] for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            self.assertIn("case_resolution_decisions", tables)
        self.assertEqual(versions, [migration.version for migration in MIGRATIONS])

    def test_v16_database_adds_customer_communications_without_rebuilding_cases(self):
        self.create_database_through(16)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("INSERT INTO customers VALUES (?, ?)", ("customer-1", "Customer"))
            connection.execute(
                "INSERT INTO orders VALUES (?, ?, ?)",
                ("order-1", "customer-1", "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                "INSERT INTO support_cases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("case-1", "customer-1", "order-1", "Issue", "Description", "open",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            connection.commit()
        finally:
            connection.close()

        store = SQLitePersistence(self.path)
        with store.transaction() as migrated:
            tables = {row[0] for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            self.assertIn("case_customer_communications", tables)
            self.assertEqual(migrated.execute(
                "SELECT status FROM support_cases WHERE case_id = 'case-1'"
            ).fetchone()[0], "open")

    def test_v17_database_adds_default_manual_investigation_control(self):
        self.create_database_through(17)
        store = SQLitePersistence(self.path)
        with store.transaction() as migrated:
            control = migrated.execute(
                "SELECT mode, version, updated_by FROM investigation_control WHERE singleton = 1",
            ).fetchone()
            audit = migrated.execute(
                "SELECT previous_mode, mode, version, admin_user_id FROM investigation_mode_audit",
            ).fetchall()
            columns = {row[1] for row in migrated.execute("PRAGMA table_info(investigation_jobs)")}
        self.assertEqual(tuple(control), ("MANUAL", 1, None))
        self.assertEqual([tuple(row) for row in audit], [(None, "MANUAL", 1, None)])
        self.assertTrue({"submission_mode", "mode_version"} <= columns)

    def test_foreign_key_and_unique_constraints_reject_invalid_evidence(self):
        store = self.store()
        self.insert_case(store)
        with self.assertRaises(PersistenceError):
            with store.transaction() as connection:
                connection.execute(
                    "INSERT INTO delivery_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("fact-1", "case-1", "order-1", "customer-1", "LAP-1", 1,
                     "2026-01-01T00:00:00Z", "wrong_item", "unused", "unknown-source", "ref-1",
                     "2026-01-01T00:00:00Z"),
                )
        with store.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM delivery_facts").fetchone()[0], 0)

    def test_delivery_provenance_reference_is_unique(self):
        store = self.store()
        self.insert_case(store)
        with store.transaction() as connection:
            connection.execute("INSERT INTO trusted_delivery_sources VALUES (?)", ("fulfillment",))
            connection.execute(
                "INSERT INTO delivery_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("fact-1", "case-1", "order-1", "customer-1", "LAP-1", 1,
                 "2026-01-01T00:00:00Z", "wrong_item", "unused", "fulfillment", "source-ref",
                 "2026-01-01T00:00:00Z"),
            )
        with self.assertRaises(PersistenceError):
            with store.transaction() as connection:
                connection.execute(
                    "INSERT INTO support_cases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("case-2", "customer-1", "order-1", "Second issue", "Description", "open",
                     "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                )
                connection.execute(
                    "INSERT INTO delivery_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("fact-2", "case-2", "order-1", "customer-1", "LAP-1", 1,
                     "2026-01-01T00:00:00Z", "wrong_item", "unused", "fulfillment", "source-ref",
                     "2026-01-01T00:00:00Z"),
                )
        with store.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM delivery_facts").fetchone()[0], 1)

    def test_failed_transaction_rolls_back_prior_writes(self):
        store = self.store()
        with self.assertRaises(PersistenceError):
            with store.transaction() as connection:
                connection.execute("INSERT INTO customers VALUES (?, ?)", ("customer-rollback", "Rollback"))
                connection.execute("INSERT INTO orders VALUES (?, ?, ?)",
                                   ("order-invalid", "missing-customer", "2026-01-01T00:00:00Z"))
        with store.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM customers").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
