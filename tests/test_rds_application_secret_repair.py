import json
import unittest
from unittest.mock import Mock

from scripts.repair_rds_application_secret import (
    EXPECTED_CA_PATH,
    EXPECTED_ROLE,
    SECRET_NAME,
    repair_secret,
)


class RdsApplicationSecretRepairTests(unittest.TestCase):
    def client(self, password="bad\nvalue"):
        client = Mock()
        client.describe_secret.return_value = {"Name": SECRET_NAME, "ARN": "arn:aws:secretsmanager:test"}
        client.get_secret_value.return_value = {"SecretString": json.dumps({
            "username": EXPECTED_ROLE, "password": password, "rds_ca_path": EXPECTED_CA_PATH,
        })}
        client.get_random_password.return_value = {"RandomPassword": "A" * 20 + "a" * 20 + "1" * 8}
        client.put_secret_value.return_value = {"VersionId": "version"}
        return client

    def test_repairs_only_exact_seed_shape_and_preserves_nonsecret_fields(self):
        client = self.client()
        result = repair_secret(client)
        self.assertEqual(result["password_length"], 48)
        payload = json.loads(client.put_secret_value.call_args.kwargs["SecretString"])
        self.assertEqual(set(payload), {"username", "password", "rds_ca_path"})
        self.assertEqual(payload["username"], EXPECTED_ROLE)
        self.assertEqual(payload["rds_ca_path"], EXPECTED_CA_PATH)
        self.assertEqual(len(payload["password"]), 48)

    def test_rejects_unexpected_seed_fields_without_update(self):
        client = self.client()
        client.get_secret_value.return_value = {"SecretString": json.dumps({
            "username": EXPECTED_ROLE, "password": "bad", "rds_ca_path": EXPECTED_CA_PATH,
            "extra": "no",
        })}
        with self.assertRaisesRegex(RuntimeError, "fields"):
            repair_secret(client)
        client.put_secret_value.assert_not_called()

    def test_rejects_invalid_generated_password_without_update(self):
        client = self.client()
        client.get_random_password.return_value = {"RandomPassword": "A" * 47 + "\n"}
        with self.assertRaisesRegex(RuntimeError, "outside"):
            repair_secret(client)
        client.put_secret_value.assert_not_called()

    def test_rejects_unapproved_secret_name(self):
        with self.assertRaisesRegex(RuntimeError, "unapproved"):
            repair_secret(self.client(), secret_name="another-secret")


if __name__ == "__main__":
    unittest.main()
