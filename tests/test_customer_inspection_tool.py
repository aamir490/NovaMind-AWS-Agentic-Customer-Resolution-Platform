import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_module():
    import backend.container.customer_inspection as module
    return module


class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Cursor:
    description = None

    def __init__(self):
        self.results = [
            [("customer-1", "Ada")],
            [("subject-1", "customer-1")],
        ]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.query = query
        self.params = params

    def fetchall(self):
        return self.results.pop(0)


class _Connection:
    def __init__(self):
        self.read_only = False
        self.cursor_instance = _Cursor()

    def transaction(self):
        return _Transaction()

    def cursor(self):
        return self.cursor_instance

    def close(self):
        pass


class CustomerInspectionTests(unittest.TestCase):
    def test_queries_are_bounded_and_output_contains_only_allowed_records(self):
        module = load_module()
        connection = _Connection()
        fake_psycopg = types.SimpleNamespace(
            connect=lambda _dsn, connect_timeout: connection
        )
        output = []
        with patch.dict(sys.modules, {"psycopg": fake_psycopg}), \
             patch.dict(
                 module.os.environ,
                 {"NOVAMIND_POSTGRES_SECRET": "opaque", "NOVAMIND_RDS_CA_BUNDLE_PATH": "/trusted/ca"},
                 clear=True,
             ), \
             patch.object(module, "parse_application_secret", return_value=types.SimpleNamespace()), \
             patch.object(module, "load_trusted_rds_ca_bundle", return_value="CA"), \
             patch.object(module, "materialize_rds_ca_bundle", return_value="/tmp/ca"), \
             patch.object(module, "application_dsn", return_value="postgresql://redacted"), \
             patch.object(module, "_emit", side_effect=lambda **values: output.append(values)):
            self.assertEqual(module.run(), 0)

        self.assertTrue(connection.read_only)
        self.assertEqual(output[0]["customer_count"], 1)
        self.assertEqual(output[0]["binding_count"], 1)
        self.assertNotIn("password", output[0])
        self.assertNotIn("dsn", output[0])
        self.assertEqual(connection.cursor_instance.params, (module.MAX_ROWS + 1,))

    def test_source_has_no_application_startup_or_mutation_sql(self):
        source = (ROOT / "backend" / "container" / "customer_inspection.py").read_text()
        self.assertNotIn("create_app", source)
        self.assertNotIn("migrate", source.lower())
        self.assertNotIn("INSERT INTO", source)
        self.assertNotIn("UPDATE ", source)
        self.assertNotIn("DELETE FROM", source)
        self.assertIn("connection.transaction()", source)
        self.assertIn("connection.read_only = True", source)
        self.assertIn("novamind.customers", source)
        self.assertIn("novamind.cognito_customer_bindings", source)


if __name__ == "__main__":
    unittest.main()
