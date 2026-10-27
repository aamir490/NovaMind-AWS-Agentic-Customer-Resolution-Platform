import base64
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infrastructure" / "novamind-crp-cognito-config-diagnostic.template.json"


class CognitoConfigDiagnosticTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        cls.container = cls.document["Resources"]["DiagnosticTaskDefinition"]["Properties"]["ContainerDefinitions"][0]
        command = cls.container["Command"]
        cls.inline_source = base64.b64decode(command[2].split("b64decode('")[1].split("')")[0]).decode()

    def test_resources_and_least_privilege_contract(self):
        resources = self.document["Resources"]
        self.assertEqual(set(resources), {"DiagnosticLogGroup", "DiagnosticExecutionRole", "DiagnosticTaskDefinition"})
        self.assertEqual(self.container["Command"][:2], ["python", "-c"])
        self.assertEqual(self.container["Secrets"].__len__(), 5)
        names = {item["Name"] for item in self.container["Secrets"]}
        self.assertEqual(names, {
            "NOVAMIND_COGNITO_ISSUER", "NOVAMIND_COGNITO_CLIENT_ID",
            "NOVAMIND_COGNITO_CUSTOMER_GROUP", "NOVAMIND_COGNITO_ADMIN_GROUP",
            "NOVAMIND_COGNITO_CUSTOMER_BINDINGS",
        })
        source = json.dumps(self.document)
        for forbidden in ("POSTGRES", "BEDROCK", "SQS", "CUSTOMER_INSPECTION", "UVICORN", "CREATE_APP"):
            self.assertNotIn(forbidden, source.upper())
        self.assertEqual(self.document["Parameters"]["LogRetentionDays"]["Default"], 1)

    def test_inline_diagnostic_outputs_sanitized_result_and_fails_closed(self):
        env = os.environ.copy()
        env.update({
            "NOVAMIND_COGNITO_ISSUER": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_SYNTHETIC",
            "NOVAMIND_COGNITO_CLIENT_ID": "synthetic-client",
            "NOVAMIND_COGNITO_CUSTOMER_GROUP": "customers",
            "NOVAMIND_COGNITO_ADMIN_GROUP": "admins",
            "NOVAMIND_COGNITO_CUSTOMER_BINDINGS": "[]",
        })
        result = subprocess.run([sys.executable, "-c", self.inline_source], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["bindings"]["binding_count"], 0)
        self.assertTrue(payload["bindings"]["valid"])
        self.assertEqual(payload["execution_mode"], "standalone")
        self.assertIn("NOVAMIND_AUTH_MODE=cognito", payload["bootstrap_auth_mode_contract"])
        for field in ("issuer", "client_id", "customer_group", "admin_group"):
            self.assertTrue(payload["fields"][field]["valid"])
        self.assertNotIn("cognito-idp", result.stdout)
        env["NOVAMIND_COGNITO_CUSTOMER_BINDINGS"] = '"[]"'
        bad = subprocess.run([sys.executable, "-c", self.inline_source], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(bad.returncode, 1)
        bad_payload = json.loads(bad.stdout)
        self.assertTrue(bad_payload["bindings"]["parse_success"])
        self.assertEqual(bad_payload["bindings"]["category"], "WRONG_JSON_TYPE")
        self.assertNotIn("cognito-idp", bad.stdout)

    def test_scalar_rules_and_duplicate_bindings_fail_without_echoing_values(self):
        env = os.environ.copy()
        env.update({
            "NOVAMIND_COGNITO_ISSUER": "http://not-cognito.example",
            "NOVAMIND_COGNITO_CLIENT_ID": "bad client",
            "NOVAMIND_COGNITO_CUSTOMER_GROUP": "same",
            "NOVAMIND_COGNITO_ADMIN_GROUP": "same",
            "NOVAMIND_COGNITO_CUSTOMER_BINDINGS": "[{\"subject_id\":\"6498e4d8-1071-7060-529e-78d45ffd368d\",\"customer_id\":\"26f2402e-a341-4cef-9abf-d057889b33ab\"},{\"subject_id\":\"6498e4d8-1071-7060-529e-78d45ffd368d\",\"customer_id\":\"26f2402e-a341-4cef-9abf-d057889b33ab\"}]",
        })
        result = subprocess.run([sys.executable, "-c", self.inline_source], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["fields"]["issuer"]["category"], "INVALID_ISSUER")
        self.assertEqual(payload["fields"]["client_id"]["category"], "INVALID_CLIENT_ID")
        self.assertEqual(payload["fields"]["customer_group"]["category"], "DUPLICATE_GROUP")
        self.assertEqual(payload["bindings"]["category"], "RECORD_VALIDATION_ERROR")
        for value in ("not-cognito", "bad client", "same", "6498e4d8", "26f2402e"):
            self.assertNotIn(value, result.stdout)

    def test_binding_failure_categories_distinguish_syntax_duplicate_and_record_errors(self):
        env = os.environ.copy()
        env.update({
            "NOVAMIND_COGNITO_ISSUER": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_SYNTHETIC",
            "NOVAMIND_COGNITO_CLIENT_ID": "synthetic-client",
            "NOVAMIND_COGNITO_CUSTOMER_GROUP": "customers",
            "NOVAMIND_COGNITO_ADMIN_GROUP": "admins",
        })
        cases = {
            "{": ("JSON_SYNTAX_ERROR", False),
            '{"a":1,"a":2}': ("DUPLICATE_JSON_KEY", False),
            "{}": ("WRONG_JSON_TYPE", True),
            "[{\"subject_id\":\"not-a-uuid\",\"customer_id\":\"not-a-uuid\"}]": ("RECORD_VALIDATION_ERROR", True),
        }
        for raw, (category, parsed) in cases.items():
            with self.subTest(category=category):
                env["NOVAMIND_COGNITO_CUSTOMER_BINDINGS"] = raw
                result = subprocess.run([sys.executable, "-c", self.inline_source], env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["bindings"]["category"], category)
                self.assertEqual(payload["bindings"]["parse_success"], parsed)
                self.assertNotIn("not-a-uuid", result.stdout)


if __name__ == "__main__":
    unittest.main()
