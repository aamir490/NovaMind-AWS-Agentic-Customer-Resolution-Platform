import json
import os
import sys
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest
from unittest.mock import patch


class ProductionMigrationPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parents[1] / "scripts" / "Read-NovaMindProductionMigrationPreflight.py"
        spec = spec_from_file_location("novamind_production_migration_preflight", path)
        module = module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        cls.module = module

    def test_sql_is_read_only_and_identity_scoped(self):
        sql = self.module.SQL.upper()
        sql_file = (Path(__file__).parents[1] / "scripts" / "Read-NovaMindProductionMigrationPreflight.sql").read_text(encoding="utf-8").upper()
        self.assertIn("CURRENT_DATABASE()", sql)
        self.assertIn("HAS_SCHEMA_PRIVILEGE", sql)
        self.assertIn("PG_GET_USERBYID", sql)
        self.assertIn("SCHEMA_OWNED", sql)
        self.assertIn("LEDGER_CONTIGUOUS", sql)
        self.assertIn("REQUIRED_TABLES_OWNED", sql)
        self.assertNotRegex(sql, r"\b(INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|ALTER\s+|DROP\s+|TRUNCATE\s+|GRANT\s+|REVOKE\s+)\b")
        self.assertIn("NOVAMIND.SCHEMA_MIGRATIONS", sql)
        self.assertIn("NOVAMIND.SCHEMA_MIGRATIONS", sql_file)
        self.assertIn("VERSION BETWEEN 1 AND 13", sql)
        self.assertIn("VERSION BETWEEN 1 AND 13", sql_file)
        self.assertEqual(self.module.REQUIRED_SCHEMA_VERSION, 13)
        self.assertEqual(self.module.EXPECTED_SCHEMA_VERSIONS[-1], 13)
        self.assertEqual(self.module.ALLOWED_VERSIONS, {13})
        self.assertEqual(self.module.REQUIRED_FUNCTIONS, ("novamind_workflow_guard",))
        self.assertIn("NOVAMIND_WORKFLOW_GUARD", sql_file)

    def test_missing_secret_is_sanitized(self):
        with patch.dict(os.environ, {}, clear=True), patch("builtins.print") as output:
            self.assertEqual(self.module.run(), 1)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload, {"phase": "SECRET_ENVIRONMENT_MISSING", "status": "FAIL"})

    def test_invalid_secret_is_sanitized(self):
        with patch.dict(os.environ, {"NOVAMIND_POSTGRES_SECRET": "not-a-secret"}, clear=True), patch("builtins.print") as output:
            self.assertEqual(self.module.run(), 1)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload, {"phase": "SECRET_PARSE_INVALID", "status": "FAIL"})

    def test_database_failures_are_sanitized(self):
        with patch.dict(os.environ, {"NOVAMIND_POSTGRES_SECRET": "{}"}, clear=True), patch("builtins.print") as output:
            self.assertEqual(self.module.run(), 1)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["phase"], "SECRET_PARSE_INVALID")
        self.assertNotIn("password", output.call_args.args[0].lower())

    def test_valid_result_is_bounded_and_identity_mismatch_fails_closed(self):
        class Column:
            def __init__(self, name):
                self.name = name

        class Cursor:
            column_names = (
                "database_name", "session_schema", "session_role", "login_role",
                "schema_owned", "connect_privilege", "usage_privilege", "create_privilege",
                "required_tables_owned", "required_table_count", "required_functions_owned",
                "required_function_count", "migration_count", "current_version",
                "ledger_versions_bounded", "ledger_contiguous",
            )

            def __init__(self):
                self.description = [Column(name) for name in self.column_names]

            def __enter__(self):
                return self

            def __exit__(self, *_):
                # Psycopg cursors may clear metadata when closed; the reader
                # must have captured it while the cursor was open.
                self.description = None
                return False

            def execute(self, _sql):
                return None

            def fetchone(self):
                return (
                    "novamindcrp", "novamind", "novamind_app", "novamind_app",
                    True, True, True, True, True, 26, True, 0, 26, 13, True, True,
                )

        class Transaction:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        class Connection:
            def __init__(self):
                self.read_only = False

            def transaction(self):
                return Transaction()

            def cursor(self):
                return Cursor()

            def close(self):
                return None

        fake_psycopg = types.SimpleNamespace(connect=lambda _dsn, connect_timeout: Connection())
        with patch.dict(os.environ, {"NOVAMIND_POSTGRES_SECRET": "valid"}, clear=True), \
                patch.object(self.module, "parse_application_secret", return_value=types.SimpleNamespace(rds_ca_pem="CA")), \
                patch.object(self.module, "load_trusted_rds_ca_bundle", return_value="CA"), \
                patch.object(self.module, "materialize_rds_ca_bundle", return_value="/tmp/ca"), \
                patch.object(self.module, "application_dsn", return_value="postgresql://redacted"), \
                patch.dict(sys.modules, {"psycopg": fake_psycopg}), \
                patch("builtins.print") as output:
            self.assertEqual(self.module.run(), 0)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["current_schema_version"], 13)
        self.assertTrue(all(payload["checks"].values()))
        self.assertNotIn("postgresql", output.call_args.args[0])

        class WrongIdentity(Cursor):
            def fetchone(self):
                row = list(super().fetchone())
                row[2] = "postgres"
                return tuple(row)

        with patch.dict(os.environ, {"NOVAMIND_POSTGRES_SECRET": "valid"}, clear=True), \
                patch.object(self.module, "parse_application_secret", return_value=types.SimpleNamespace(rds_ca_pem="CA")), \
                patch.object(self.module, "load_trusted_rds_ca_bundle", return_value="CA"), \
                patch.object(self.module, "materialize_rds_ca_bundle", return_value="/tmp/ca"), \
                patch.object(self.module, "application_dsn", return_value="postgresql://redacted"), \
                patch.dict(sys.modules, {"psycopg": types.SimpleNamespace(connect=lambda _dsn, connect_timeout: types.SimpleNamespace(
                    read_only=False, transaction=lambda: Transaction(), cursor=lambda: WrongIdentity(), close=lambda: None))}), \
                patch("builtins.print") as output:
            self.assertEqual(self.module.run(), 1)
        self.assertEqual(json.loads(output.call_args.args[0])["phase"], "RESULT_VALIDATION")

    def test_malformed_result_emits_only_allowlisted_flags(self):
        class Column:
            def __init__(self, name):
                self.name = name

        class Cursor:
            description = [Column("database_name")]
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, _sql): return None
            def fetchone(self): return ("novamindcrp",)

        class Tx:
            def __enter__(self): return self
            def __exit__(self, *_): return False

        connection = types.SimpleNamespace(
            read_only=False, transaction=lambda: Tx(), cursor=lambda: Cursor(), close=lambda: None,
        )
        with patch.dict(os.environ, {"NOVAMIND_POSTGRES_SECRET": "valid"}, clear=True), \
                patch.object(self.module, "parse_application_secret", return_value=types.SimpleNamespace(rds_ca_pem="CA")), \
                patch.object(self.module, "load_trusted_rds_ca_bundle", return_value="CA"), \
                patch.object(self.module, "materialize_rds_ca_bundle", return_value="/tmp/ca"), \
                patch.object(self.module, "application_dsn", return_value="postgresql://redacted"), \
                patch.dict(sys.modules, {"psycopg": types.SimpleNamespace(connect=lambda *_args, **_kwargs: connection)}), \
                patch("builtins.print") as output:
            self.assertEqual(self.module.run(), 1)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["phase"], "RESULT_VALIDATION")
        self.assertFalse(payload["checks"]["login_role"])
        self.assertFalse(payload["checks"]["migration_ledger_contiguous"])


if __name__ == "__main__":
    unittest.main()
