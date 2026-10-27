"""V3 production migration preflight — fixture setup and SQL execution test.

Creates a disposable test database (novamind_test_v3_preflight) on the local
Docker container, runs production migrations as novamind_app with the correct
search_path=novamind, executes the actual V3 SQL, and validates all boolean
flags, column mapping, schema version and PASS/FAIL handling.

This is a test-only connection using sslmode=disable.  Production rds_config
uses sslmode=verify-full with a CA bundle and is NOT changed by this test.

Run:
    cd E:\\codex-projetc\\NovaMind-local-smoke-test
    python -m pytest tests/test_v3_preflight_fixture.py -v
"""

import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
from uuid import uuid4

# DSNs are supplied only by the local test operator (for example from a
# temporary environment populated from the disposable container).  Keeping
# credentials out of source prevents accidental reuse against production.
_ADMIN_DSN = os.environ.get("NOVAMIND_V3_ADMIN_DSN", "")
_APP_DSN = os.environ.get("NOVAMIND_V3_APP_DSN", "")
_DB_NAME = os.environ.get("NOVAMIND_V3_DB_NAME", "novamind_test_v3_preflight")
_APP_PASSWORD = os.environ.get("NOVAMIND_V3_APP_PASSWORD", "")

if not _ADMIN_DSN or not _APP_DSN:
    raise unittest.SkipTest(
        "NOVAMIND_V3_ADMIN_DSN and NOVAMIND_V3_APP_DSN are required; "
        "set them only for the disposable PostgreSQL fixture"
    )


def _validate_fixture_dsn(dsn: str) -> None:
    """Fail closed unless the DSN is the dedicated local disposable target."""
    parsed = urlsplit(dsn)
    database = parsed.path.lstrip("/")
    if (
        parsed.scheme not in {"postgresql", "postgres"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port != 58086
        or database != "novamind_test_v3_preflight"
    ):
        raise unittest.SkipTest(
            "V3 fixture DSNs must target only localhost:58086/"
            "novamind_test_v3_preflight"
        )


_validate_fixture_dsn(_ADMIN_DSN)
_validate_fixture_dsn(_APP_DSN)

# Load the hyphenated-filename script via spec_from_file_location (same as
# test_production_migration_preflight.py does — direct import can't use hyphens)
_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "Read-NovaMindProductionMigrationPreflight.py"
_spec = spec_from_file_location("novamind_preflight_module", _SCRIPT_PATH)
_preflight_module = module_from_spec(_spec)
_spec.loader.exec_module(_preflight_module)
SQL = _preflight_module.SQL
ALLOWED_VERSIONS = _preflight_module.ALLOWED_VERSIONS
REQUIRED_TABLES = _preflight_module.REQUIRED_TABLES
REQUIRED_FUNCTIONS = _preflight_module.REQUIRED_FUNCTIONS


def _admin_conn(autocommit: bool = True):
    from psycopg import connect
    return connect(_ADMIN_DSN, autocommit=autocommit, connect_timeout=5)


def _app_conn(autocommit: bool = False):
    from psycopg import connect
    return connect(_APP_DSN, autocommit=autocommit, connect_timeout=5)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _reset_fixture():
    """Drop and recreate the novamind schema in the fixture database."""
    conn = _admin_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS novamind CASCADE")
            cur.execute("CREATE SCHEMA novamind AUTHORIZATION novamind_app")
            cur.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            cur.execute("REVOKE ALL ON SCHEMA public FROM novamind_app")
            cur.execute("GRANT USAGE, CREATE ON SCHEMA novamind TO novamind_app")
    finally:
        conn.close()


def _run_migrations():
    """Run all production PostgreSQL migrations as novamind_app (search_path=novamind)."""
    from backend.app.postgres_persistence import PostgreSQLPersistence
    store = PostgreSQLPersistence(_APP_DSN, initialize=True)
    # PostgreSQLPersistence.initialize() calls _migrate() + _validate()
    return store


def _run_migrations_through(version):
    """Apply the immutable production migration prefix through ``version``."""
    from backend.app.postgres_persistence import POSTGRES_MIGRATIONS

    conn = _app_conn()
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY NOT NULL, applied_at TEXT NOT NULL)")
                for migration in POSTGRES_MIGRATIONS:
                    if migration.version > version:
                        break
                    for statement in migration.statements:
                        cur.execute(statement)
                    cur.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (%s, %s)",
                        (migration.version, datetime.now(timezone.utc).isoformat()),
                    )
    finally:
        conn.close()


def _seed_case_and_workflow(cur, *, workflow_id, run_id, owner_id, request_id):
    customer_id, order_id, case_id = (str(uuid4()) for _ in range(3))
    now = datetime.now(timezone.utc).isoformat()
    cur.execute("INSERT INTO customers(customer_id, name) VALUES (%s, %s)", (customer_id, "Migration 10 fixture"))
    cur.execute("INSERT INTO orders(order_id, customer_id, created_at) VALUES (%s, %s, %s)",
                (order_id, customer_id, now))
    cur.execute(
        """INSERT INTO support_cases
           (case_id, customer_id, order_id, subject, description, status, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, 'open', %s, %s)""",
        (case_id, customer_id, order_id, "Migration 10", "Synthetic disposable PostgreSQL fixture.", now, now),
    )
    cur.execute(
        """INSERT INTO workflow_runs
           (workflow_id, run_id, case_id, owner_user_id, request_id, request_fingerprint,
            provider, status, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, 'bedrock', 'RUNNING', %s, %s)""",
        (workflow_id, run_id, case_id, owner_id, request_id, "a" * 64, now, now),
    )
    return case_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class V3PreflightFixtureSetupTests(unittest.TestCase):
    """Confirm the fixture database is reachable and the novamind schema exists."""

    def test_admin_connection(self):
        conn = _admin_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                db = cur.fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(db, _DB_NAME)

    def test_novamind_app_role_exists(self):
        conn = _admin_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT rolname FROM pg_roles WHERE rolname = 'novamind_app'")
                row = cur.fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "novamind_app role must exist in the test container")


class V3PreflightMigrationTests(unittest.TestCase):
    """Run production migrations and verify the ledger is populated."""

    @classmethod
    def setUpClass(cls):
        _reset_fixture()
        from backend.app.postgres_persistence import POSTGRES_MIGRATIONS
        cls.expected_versions = [m.version for m in POSTGRES_MIGRATIONS]
        cls.store = _run_migrations()

    def test_migration_ledger_contains_all_versions(self):
        conn = _admin_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM novamind.schema_migrations ORDER BY version")
                versions = [row[0] for row in cur.fetchall()]
        finally:
            conn.close()
        self.assertEqual(versions, self.expected_versions)

    def test_ledger_is_contiguous(self):
        # Versions must be 1..N with no gaps
        self.assertEqual(self.expected_versions, list(range(1, len(self.expected_versions) + 1)))

    def test_latest_version_in_allowed_set(self):
        from backend.app.postgres_persistence import POSTGRES_MIGRATIONS
        latest = max(self.expected_versions)
        self.assertIn(latest, ALLOWED_VERSIONS, f"Version {latest} not in ALLOWED_VERSIONS {ALLOWED_VERSIONS}")

    def test_required_tables_exist_in_novamind_schema(self):
        conn = _admin_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT relname FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'novamind' AND c.relkind IN ('r', 'p')
                """)
                existing = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()
        missing = set(REQUIRED_TABLES) - existing
        self.assertEqual(missing, set(), f"Missing tables in novamind schema: {missing}")

    def test_tables_owned_by_novamind_app(self):
        conn = _admin_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT relname, pg_get_userbyid(relowner) AS owner
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'novamind' AND c.relkind IN ('r', 'p')
                      AND pg_get_userbyid(c.relowner) != 'novamind_app'
                """)
                wrong = cur.fetchall()
        finally:
            conn.close()
        self.assertEqual(wrong, [], f"Tables not owned by novamind_app: {wrong}")

    def test_schema_owned_by_novamind_app(self):
        conn = _admin_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'novamind'")
                owner = cur.fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(owner, "novamind_app")


class PostgreSQLMigration10UpgradeTests(unittest.TestCase):
    """Exercise the exact production 9 -> 10 path on disposable PostgreSQL 16."""

    def test_version_9_upgrades_atomically_and_remains_compatible(self):
        from backend.app.postgres_persistence import POSTGRES_MIGRATIONS, PostgreSQLPersistence

        _reset_fixture()
        _run_migrations_through(9)
        ids = [(str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())) for _ in range(3)]
        conn = _app_conn()
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    [
                        _seed_case_and_workflow(
                            cur, workflow_id=workflow_id, run_id=run_id,
                            owner_id=owner_id, request_id=request_id,
                        )
                        for workflow_id, run_id, owner_id, request_id in ids
                    ]
            migration = next(item for item in POSTGRES_MIGRATIONS if item.version == 10)
            with conn.transaction():
                with conn.cursor() as cur:
                    for statement in migration.statements:
                        cur.execute(statement)
                    cur.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (%s, %s)",
                        (10, datetime.now(timezone.utc).isoformat()),
                    )

            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT invalid_response_stage, invalid_response_category
                           FROM workflow_runs WHERE workflow_id = %s""",
                        (ids[0][0],),
                    )
                    self.assertEqual(cur.fetchone(), (None, None))
                    cur.execute(
                        """UPDATE workflow_runs
                           SET status = 'FAILED', updated_at = %s,
                               failure_category = 'INVALID_RESPONSE', failure_stage = 'AGENT',
                               invalid_response_stage = 'json_guardrail',
                               invalid_response_category = 'bedrock_empty_output'
                           WHERE workflow_id = %s""",
                        (datetime.now(timezone.utc).isoformat(), ids[0][0]),
                    )
                    cur.execute(
                        """UPDATE workflow_runs SET status = 'FAILED', updated_at = %s
                           WHERE workflow_id = %s""",
                        (datetime.now(timezone.utc).isoformat(), ids[1][0]),
                    )

            with self.assertRaises(Exception):
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """UPDATE workflow_runs
                               SET status = 'FAILED', updated_at = %s,
                                   failure_category = 'INVALID_RESPONSE', failure_stage = 'AGENT',
                                   invalid_response_stage = 'private_provider_detail'
                               WHERE workflow_id = %s""",
                            (datetime.now(timezone.utc).isoformat(), ids[2][0]),
                        )

            with self.assertRaises(Exception):
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """UPDATE workflow_runs
                               SET status = 'FAILED', updated_at = %s,
                                   failure_category = 'INVALID_RESPONSE', failure_stage = 'AGENT',
                                   invalid_response_stage = 'output_schema',
                                   invalid_response_category = 'bedrock_empty_output'
                               WHERE workflow_id = %s""",
                            (datetime.now(timezone.utc).isoformat(), ids[2][0]),
                        )

            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT workflow_id, status, failure_category, failure_stage,
                                  invalid_response_stage, invalid_response_category
                           FROM workflow_runs ORDER BY workflow_id"""
                    )
                    rows = {row[0]: row[1:] for row in cur.fetchall()}
                    cur.execute("SELECT version FROM schema_migrations ORDER BY version")
                    self.assertEqual([row[0] for row in cur.fetchall()], list(range(1, 11)))
                    cur.execute(
                        """SELECT column_name FROM information_schema.columns
                           WHERE table_schema = 'novamind' AND table_name = 'workflow_runs'
                             AND column_name IN ('invalid_response_stage', 'invalid_response_category')"""
                    )
                    self.assertEqual({row[0] for row in cur.fetchall()}, {
                        "invalid_response_stage", "invalid_response_category",
                    })

            self.assertEqual(rows[ids[0][0]], (
                "FAILED", "INVALID_RESPONSE", "AGENT", "json_guardrail", "bedrock_empty_output",
            ))
            self.assertEqual(rows[ids[1][0]], ("FAILED", "WORKFLOW_ERROR", "WORKFLOW", None, None))
            self.assertEqual(rows[ids[2][0]], ("RUNNING", None, None, None, None))
        finally:
            conn.close()

        # A normal production initialize is a no-op at the current ledger version 13 and
        # still validates the complete schema.
        PostgreSQLPersistence(_APP_DSN, initialize=True)


class V3PreflightSQLExecutionTests(unittest.TestCase):
    """Execute the actual V3 SQL as novamind_app and validate all flags."""

    @classmethod
    def setUpClass(cls):
        _reset_fixture()
        _run_migrations()

    def _run_sql(self):
        """Execute the V3 preflight SQL as novamind_app (no SSL) and return the row dict."""
        conn = _app_conn()
        try:
            conn.read_only = True
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(SQL)
                    row = cur.fetchone()
                    columns = tuple(col.name for col in cur.description)
            result = dict(zip(columns, row))
        finally:
            conn.close()
        return result

    def test_sql_returns_a_row(self):
        result = self._run_sql()
        self.assertIsNotNone(result)
        self.assertIsInstance(result, dict)

    def test_column_mapping_complete(self):
        """All expected columns from the preflight parser are present."""
        expected_columns = {
            "database_name", "session_schema", "session_role", "login_role",
            "schema_owned", "connect_privilege", "usage_privilege", "create_privilege",
            "required_tables_owned", "required_table_count", "required_functions_owned",
            "required_function_count", "migration_count", "current_version",
            "ledger_versions_bounded", "ledger_contiguous",
        }
        result = self._run_sql()
        self.assertEqual(set(result.keys()), expected_columns)

    def test_database_name(self):
        result = self._run_sql()
        self.assertEqual(result["database_name"], _DB_NAME)

    def test_session_and_login_role(self):
        result = self._run_sql()
        self.assertEqual(result["session_role"], "novamind_app")
        self.assertEqual(result["login_role"], "novamind_app")

    def test_session_schema(self):
        result = self._run_sql()
        self.assertEqual(result["session_schema"], "novamind")

    def test_boolean_flags_all_true(self):
        result = self._run_sql()
        bool_flags = {
            "schema_owned", "connect_privilege", "usage_privilege", "create_privilege",
            "required_tables_owned", "required_functions_owned",
            "ledger_versions_bounded", "ledger_contiguous",
        }
        for flag in bool_flags:
            self.assertIs(result[flag], True, f"Flag {flag!r} should be True, got {result[flag]!r}")

    def test_migration_counts_match_migrations_list(self):
        from backend.app.postgres_persistence import POSTGRES_MIGRATIONS
        result = self._run_sql()
        expected_count = len(POSTGRES_MIGRATIONS)
        self.assertEqual(result["migration_count"], expected_count)
        self.assertEqual(result["current_version"], max(m.version for m in POSTGRES_MIGRATIONS))

    def test_required_table_count(self):
        result = self._run_sql()
        self.assertEqual(result["required_table_count"], len(REQUIRED_TABLES))

    def test_required_function_count(self):
        result = self._run_sql()
        self.assertEqual(result["required_function_count"], len(REQUIRED_FUNCTIONS))


class V3PreflightParserValidationTests(unittest.TestCase):
    """Run the parser's own check() path against the live SQL output.

    The preflight parser validates session_role == 'novamind_app' and
    database_name == 'novamindcrp'.  The fixture database is named
    novamind_test_v3_preflight, so database_identity must correctly FAIL.
    All other flags must PASS, confirming the parser logic is exercised.
    """

    @classmethod
    def setUpClass(cls):
        _reset_fixture()
        _run_migrations()

    def _get_live_result(self):
        conn = _app_conn()
        try:
            conn.read_only = True
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(SQL)
                    row = cur.fetchone()
                    columns = tuple(col.name for col in cur.description)
            return dict(zip(columns, row))
        finally:
            conn.close()

    def test_parser_correctly_fails_database_identity(self):
        """database_name != 'novamindcrp' → database_identity check must be False."""
        result = self._get_live_result()
        required = {
            "database_identity": result.get("database_name") == "novamindcrp",
            "login_role": (result.get("session_role") == "novamind_app"
                           and result.get("login_role") == "novamind_app"),
            "schema_owner": result.get("schema_owned") is True,
            "connect_privilege": result.get("connect_privilege") is True,
            "usage_privilege": result.get("usage_privilege") is True,
            "create_privilege": result.get("create_privilege") is True,
            "required_tables_owned": result.get("required_tables_owned") is True,
            "required_functions_owned": result.get("required_functions_owned") is True,
            "migration_ledger_contiguous": (result.get("ledger_contiguous") is True
                                            and result.get("ledger_versions_bounded") is True),
            "migration_version_allowed": (isinstance(result.get("current_version"), int)
                                          and result.get("current_version") in ALLOWED_VERSIONS),
            "session_schema": result.get("session_schema") == "novamind",
        }
        # database_identity must be False (fixture DB != novamindcrp)
        self.assertFalse(required["database_identity"],
            "database_identity must FAIL for non-production fixture DB")
        # All other checks must pass — confirms the schema, role, and ledger are correct
        other_checks = {k: v for k, v in required.items() if k != "database_identity"}
        failed = {k: v for k, v in other_checks.items() if not v}
        self.assertEqual(failed, {},
            f"Non-identity checks must all PASS. Failed: {failed}")

    def test_parser_run_produces_fail_on_database_identity(self):
        """The full run() path must emit FAIL with checks dict when database_identity is wrong."""
        # Use the module-level _preflight_module loaded at import time
        mod = _preflight_module

        # Patch parse_application_secret and DSN helpers to use our test connection
        class FakeSecret:
            rds_ca_pem = "NOTUSED"
            username = "novamind_app"
            password = _APP_PASSWORD
            host = "127.0.0.1"
            port = 58086
            dbname = _DB_NAME
            schema = "novamind"

        import psycopg as real_psycopg

        def fake_connect(dsn, connect_timeout):
            # Connect directly without SSL; ignore the injected DSN
            return real_psycopg.connect(_APP_DSN, connect_timeout=connect_timeout)

        fake_psycopg = types.SimpleNamespace(connect=fake_connect)

        with patch.dict(os.environ, {"NOVAMIND_POSTGRES_SECRET": "any"}, clear=True), \
             patch.object(mod, "parse_application_secret", return_value=FakeSecret()), \
             patch.object(mod, "materialize_rds_ca_bundle", return_value="/tmp/unused-ca"), \
             patch.object(mod, "application_dsn", return_value=_APP_DSN), \
             patch("builtins.print") as mock_print, \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            exit_code = mod.run()

        self.assertEqual(exit_code, 1, "run() must return 1 (FAIL) for wrong database_name")
        payload = json.loads(mock_print.call_args.args[0])
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["phase"], "RESULT_VALIDATION")
        self.assertFalse(payload["checks"]["database_identity"])
        # All other checks must be True
        other = {k: v for k, v in payload["checks"].items() if k != "database_identity"}
        self.assertTrue(all(other.values()), f"Non-identity checks failed: {other}")
        # Connection material must not appear in output
        self.assertNotIn("novamind_test_v3_preflight", mock_print.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
