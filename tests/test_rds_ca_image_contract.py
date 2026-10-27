"""Offline contract tests for the verified RDS CA image/runtime path."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CA_PATH = "/etc/novamind/rds-ca-bundle.pem"


class RdsCaImageContractTests(unittest.TestCase):
    def test_backend_image_pins_and_verifies_the_official_bundle(self):
        dockerfile = (ROOT / "backend/Dockerfile").read_text()
        installer = (ROOT / "backend/container/install_rds_ca_bundle.py").read_text()
        dockerignore = (ROOT / "backend/.dockerignore").read_text()
        self.assertIn("ARG RDS_CA_BUNDLE_SHA256", dockerfile)
        self.assertIn("RDS_CA_BUNDLE_SHA256=\"$RDS_CA_BUNDLE_SHA256\"", dockerfile)
        self.assertIn("--output /etc/novamind/rds-ca-bundle.pem", dockerfile)
        self.assertIn("https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem", installer)
        self.assertIn("hashlib.sha256", installer)
        self.assertIn("ssl.create_default_context", installer)
        self.assertIn("RDS_CA_BUNDLE_SHA256", installer)
        self.assertIn("!container/", dockerignore)
        self.assertIn("!container/*.py", dockerignore)
        self.assertNotIn("--no-check-certificate", installer)

    def test_all_four_task_containers_pin_the_same_ca_path(self):
        bootstrap = json.loads(
            (ROOT / "infrastructure/novamind-rds-bootstrap-task-definition.template.json").read_text()
        )
        services = json.loads(
            (ROOT / "infrastructure/novamind-crp-investigation-services.template.json").read_text()
        )
        bootstrap_env = bootstrap["containerDefinitions"][0]["environment"]
        self.assertIn({"name": "NOVAMIND_RDS_CA_BUNDLE_PATH", "value": CA_PATH}, bootstrap_env)
        for logical_id in ("ApiTaskDefinition", "PublisherTaskDefinition", "WorkerTaskDefinition"):
            environment = services["Resources"][logical_id]["Properties"]["ContainerDefinitions"][0]["Environment"]
            self.assertIn({"Name": "NOVAMIND_RDS_CA_BUNDLE_PATH", "Value": CA_PATH}, environment)

    def test_runtime_loader_remains_fail_closed_and_tls_verified(self):
        source = (ROOT / "backend/container/rds_config.py").read_text()
        self.assertIn("stat.S_ISLNK", source)
        self.assertIn("metadata.st_uid", source)
        self.assertIn("mode & 0o022", source)
        self.assertIn("ssl.create_default_context(cadata=certificate)", source)
        self.assertIn("sslmode=verify-full", source)

    def test_documented_digest_review_is_reproducible_and_not_hardcoded(self):
        guide = (ROOT / "docs/aws-rds-postgres-initialization.md").read_text()
        self.assertIn("Invoke-WebRequest -Uri $caUrl -OutFile $caFile", guide)
        self.assertIn("Get-FileHash -LiteralPath $caFile -Algorithm SHA256", guide)
        self.assertIn("CreateFromPem", guide)
        self.assertIn('docker build --file backend/Dockerfile --build-arg "RDS_CA_BUNDLE_SHA256=$sha256" backend', guide)
        self.assertNotRegex(guide, r"RDS_CA_BUNDLE_SHA256=[0-9a-f]{64}")


if __name__ == "__main__":
    unittest.main()
