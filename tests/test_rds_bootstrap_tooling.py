"""Offline contracts for the one-time RDS bootstrap and runtime secret path."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from backend.container.bootstrap import _postgres_dsn
from backend.container.rds_bootstrap import (
    RdsBootstrapError, RdsMasterSecret, _master_secret, _run_privileged_setup,
    _updated_application_secret,
)
from backend.container.rds_config import (
    RdsConfigurationError, application_dsn, parse_application_secret, parse_seed_application_secret,
)


ROOT = Path(__file__).resolve().parents[1]


def _certificate() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "novamind-rds-test-root")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


class _Cursor:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.statements = []

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))

    def fetchone(self):
        return next(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Connection:
    def __init__(self, rows):
        self.cursor_instance = _Cursor(rows)

    @contextmanager
    def transaction(self):
        yield self

    def cursor(self):
        return self.cursor_instance


class RdsBootstrapToolingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certificate = _certificate()
        cls.ca_directory = tempfile.TemporaryDirectory()
        cls.ca_path = Path(cls.ca_directory.name) / "rds-ca-bundle.pem"
        cls.ca_path.write_text(cls.certificate, encoding="utf-8")
        os.chmod(cls.ca_path, 0o600)
        cls.previous_ca_path = os.environ.get("NOVAMIND_RDS_CA_BUNDLE_PATH")
        os.environ["NOVAMIND_RDS_CA_BUNDLE_PATH"] = str(cls.ca_path)

    @classmethod
    def tearDownClass(cls):
        if cls.previous_ca_path is None:
            os.environ.pop("NOVAMIND_RDS_CA_BUNDLE_PATH", None)
        else:
            os.environ["NOVAMIND_RDS_CA_BUNDLE_PATH"] = cls.previous_ca_path
        cls.ca_directory.cleanup()

    def application_secret(self, **overrides):
        secret = {
            "username": "novamind_app",
            "password": "Test:Password!42",
            "host": "novamind-crp-postgres-dev.example.rds.amazonaws.com",
            "port": 5432,
            "dbname": "novamindcrp",
            "schema": "novamind",
            "rds_ca_path": str(self.ca_path),
        }
        secret.update(overrides)
        return json.dumps(secret)

    def test_runtime_secret_requires_fixed_fields_and_builds_verify_full_dsn(self):
        parsed = parse_application_secret(self.application_secret())
        dsn = application_dsn(parsed, certificate_path="/dev/shm/novamind/rds-ca.pem")
        self.assertIn("sslmode=verify-full", dsn)
        self.assertIn("sslrootcert=%2Fdev%2Fshm%2Fnovamind%2Frds-ca.pem", dsn)
        self.assertIn("options=-c%20search_path%3Dnovamind%2Cpg_catalog", dsn)
        self.assertIn("Test%3APassword%2142", dsn)
        with self.assertRaises(RdsConfigurationError):
            parse_application_secret(self.application_secret(extra="unexpected"))
        with self.assertRaises(RdsConfigurationError):
            parse_application_secret(self.application_secret(rds_ca_path="/not/the/trusted/bundle.pem"))

    def test_container_rds_secret_is_opt_in_and_does_not_change_direct_dsn_support(self):
        direct = "postgresql://local:password@localhost:5432/novamind"
        with patch.dict(os.environ, {"NOVAMIND_POSTGRES_DSN": direct}, clear=True):
            self.assertEqual(_postgres_dsn(), direct)
        with patch.dict(os.environ, {"NOVAMIND_POSTGRES_SECRET": self.application_secret(),
                                     "NOVAMIND_RDS_CA_BUNDLE_PATH": str(self.ca_path)}, clear=True), \
                patch("backend.container.bootstrap.materialize_rds_ca_bundle", return_value="/dev/shm/rds-ca.pem"):
            self.assertIn("sslmode=verify-full", _postgres_dsn())
        with patch.dict(os.environ, {
            "NOVAMIND_POSTGRES_DSN": direct,
            "NOVAMIND_POSTGRES_SECRET": self.application_secret(),
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "configuration is invalid") as caught:
                _postgres_dsn()
            self.assertNotIn("Test:Password!42", str(caught.exception))

    def test_seed_secret_cannot_be_used_as_a_runtime_connection_secret(self):
        seed = json.dumps({
            "username": "novamind_app", "password": "Test:Password!42", "rds_ca_path": str(self.ca_path),
        })
        self.assertEqual(parse_seed_application_secret(seed)[:2], ("novamind_app", "Test:Password!42"))
        with self.assertRaises(RdsConfigurationError):
            parse_application_secret(seed)
        with self.assertRaises(RdsConfigurationError):
            parse_seed_application_secret(seed, expected_role="not valid")

    def test_official_scale_ca_bundle_uses_file_limit_not_secret_limit(self):
        large_bundle = "-----BEGIN CERTIFICATE-----\n" + ("x" * 174000) + "\n-----END CERTIFICATE-----"
        with patch("backend.container.rds_config.ssl.create_default_context"):
            from backend.container.rds_config import _validate_ca_bundle
            self.assertEqual(_validate_ca_bundle(large_bundle), large_bundle)

    def test_master_secret_is_database_bound_and_endpoint_safe(self):
        master = _master_secret(json.dumps({
            "username": "postgres", "password": "master-password", "host": "db.example.rds.amazonaws.com",
            "port": 5432, "dbname": "novamindcrp",
        }), target_database="novamindcrp", rds_endpoint="db.example.rds.amazonaws.com")
        self.assertEqual((master.host, master.port, master.dbname), ("db.example.rds.amazonaws.com", 5432, "novamindcrp"))
        for bad in (
            {"username": "postgres", "password": "x", "host": "db@example", "port": 5432, "dbname": "novamindcrp"},
        ):
            with self.assertRaisesRegex(RdsBootstrapError, "MASTER_SECRET_INVALID"):
                _master_secret(json.dumps(bad), target_database="novamindcrp", rds_endpoint="db.example.rds.amazonaws.com")

    def test_unexpected_schema_owner_stops_before_role_mutation(self):
        connection = _Connection([
            ("novamindcrp",),  # current database
            ("unrelated_owner",),
        ])
        with self.assertRaisesRegex(RdsBootstrapError, "APPLICATION_SCHEMA_OWNER_MISMATCH"):
            _run_privileged_setup(
                connection, database="novamindcrp", role="novamind_app", schema="novamind",
                password="Test:Password!42",
            )
        statements = "\n".join(str(statement) for statement, _ in connection.cursor_instance.statements)
        self.assertNotIn("CREATE ROLE", statements)
        self.assertNotIn("GRANT CREATE", statements)

    def test_missing_schema_uses_temporary_database_create_without_role_membership(self):
        connection = _Connection([
            ("novamindcrp",),  # current database
            None,              # schema does not exist
            None,              # application role does not exist
            (False,),          # application role has no CREATE privilege
        ])
        provision = _run_privileged_setup(
            connection, database="novamindcrp", role="novamind_app", schema="novamind",
            password="Test:Password!42",
        )
        statements = "\n".join(str(statement) for statement, _ in connection.cursor_instance.statements)
        self.assertIn("CREATE ROLE", statements)
        self.assertIn("GRANT CREATE ON DATABASE", statements)
        self.assertNotIn("SET TRUE", statements)
        self.assertNotIn("CREATE SCHEMA", statements)
        self.assertEqual((provision.schema_missing, provision.revoke_database_create), (True, True))

    def test_existing_role_without_membership_uses_temporary_database_create(self):
        connection = _Connection([
            ("novamindcrp",),
            None,
            ("novamind_app",),
            (False,),
        ])
        provision = _run_privileged_setup(
            connection, database="novamindcrp", role="novamind_app", schema="novamind",
            password="Test:Password!42",
        )
        statements = "\n".join(str(statement) for statement, _ in connection.cursor_instance.statements)
        self.assertIn("GRANT CREATE ON DATABASE", statements)
        self.assertNotIn("ALTER ROLE", statements)
        self.assertNotIn("SET TRUE", statements)
        self.assertTrue(provision.revoke_database_create)

    def test_repeated_bootstrap_preserves_existing_schema_owner_without_membership_changes(self):
        connection = _Connection([
            ("novamindcrp",),  # current database
            ("novamind_app",), # schema already owned by the application role
            ("novamind_app",), # application role already exists
        ])
        provision = _run_privileged_setup(
            connection, database="novamindcrp", role="novamind_app", schema="novamind",
            password="Test:Password!42",
        )
        statements = "\n".join(str(statement) for statement, _ in connection.cursor_instance.statements)
        self.assertIn("GRANT CONNECT", statements)
        self.assertNotIn("CREATE SCHEMA", statements)
        self.assertNotIn("GRANT CREATE", statements)
        self.assertFalse(provision.schema_missing)
        self.assertFalse(provision.revoke_database_create)

    def test_preexisting_database_create_grant_fails_closed(self):
        connection = _Connection([
            ("novamindcrp",),
            None,
            ("novamind_app",),
            (True,),
        ])
        with self.assertRaisesRegex(RdsBootstrapError, "APPLICATION_DATABASE_CREATE_PREEXISTING"):
            _run_privileged_setup(
                connection, database="novamindcrp", role="novamind_app", schema="novamind",
                password="Test:Password!42",
            )
        statements = "\n".join(str(statement) for statement, _ in connection.cursor_instance.statements)
        self.assertNotIn("GRANT CREATE", statements)

    def test_final_secret_never_copies_master_login(self):
        result = json.loads(_updated_application_secret(
            username="novamind_app", password="application-password", certificate=("x" * (1024 * 1024)),
            master=RdsMasterSecret(
                username="master-user", password="master-password", host="db.example.rds.amazonaws.com",
                port=5432, dbname="novamindcrp",
            ), schema="novamind",
        ))
        self.assertEqual(result["username"], "novamind_app")
        self.assertEqual(result["rds_ca_path"], "/etc/novamind/rds-ca-bundle.pem")
        self.assertNotIn("rds_ca_pem", result)
        self.assertLess(len(json.dumps(result)), 65536)
        self.assertNotIn("master-user", result.values())
        self.assertNotIn("master-password", result.values())

    def test_task_definitions_pin_the_separate_trusted_ca_file(self):
        bootstrap = json.loads(
            (ROOT / "infrastructure/novamind-rds-bootstrap-task-definition.template.json").read_text()
        )
        bootstrap_environment = bootstrap["containerDefinitions"][0]["environment"]
        self.assertIn(
            {"name": "NOVAMIND_RDS_CA_BUNDLE_PATH", "value": "/etc/novamind/rds-ca-bundle.pem"},
            bootstrap_environment,
        )
        services = (ROOT / "infrastructure/novamind-crp-investigation-services.template.json").read_text()
        self.assertEqual(services.count("NOVAMIND_RDS_CA_BUNDLE_PATH"), 3)
        self.assertEqual(services.count("/etc/novamind/rds-ca-bundle.pem"), 3)

    def test_iam_and_ecs_templates_keep_master_access_out_of_runtime_backend(self):
        bootstrap = (ROOT / "infrastructure/novamind-rds-bootstrap-task-role-policy.template.json").read_text()
        backend = (ROOT / "infrastructure/novamind-rds-backend-execution-role-policy.template.json").read_text()
        fragment = (ROOT / "infrastructure/novamind-rds-backend-container-fragment.template.json").read_text()
        self.assertIn("__RDS_MASTER_SECRET_ARN__", bootstrap)
        self.assertNotIn("MASTER", backend)
        self.assertNotIn("MASTER", fragment)
        self.assertIn("NOVAMIND_POSTGRES_SECRET", fragment)

    def test_bootstrap_security_group_is_opt_in_zero_ingress_and_port_limited(self):
        document = json.loads(
            (ROOT / "infrastructure/novamind-rds-bootstrap-support.template.json").read_text()
        )
        self.assertEqual(document["Parameters"]["CreateBootstrapSecurityGroup"]["Default"], "false")
        group = document["Resources"]["BootstrapSecurityGroup"]["Properties"]
        self.assertEqual(group["GroupName"]["Ref"], "BootstrapSecurityGroupName")
        self.assertNotIn("SecurityGroupIngress", group)
        self.assertEqual(
            document["Resources"]["RdsIngressFromBootstrapSecurityGroup"]["Properties"]
            ["SourceSecurityGroupId"]["Ref"],
            "BootstrapSecurityGroup",
        )
        rules = group["SecurityGroupEgress"]
        serialized = json.dumps(rules)
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["DestinationSecurityGroupId"]["Ref"], "RdsSecurityGroupId")
        self.assertEqual((rules[0]["IpProtocol"], rules[0]["FromPort"], rules[0]["ToPort"]), ("tcp", 5432, 5432))
        self.assertEqual((rules[1]["CidrIp"], rules[1]["IpProtocol"], rules[1]["FromPort"], rules[1]["ToPort"]), ("0.0.0.0/0", "tcp", 443, 443))
        self.assertEqual(document["Parameters"]["BootstrapPublicInternetEgressApproved"]["Default"], "false")
        self.assertEqual(
            {tag["Key"]: tag["Value"] for tag in group["Tags"]},
            {"Project": "novamind-crp", "Environment": "dev", "ManagedBy": "CloudFormation"},
        )

    def test_bootstrap_execution_role_log_arn_matches_leading_slash_log_group(self):
        document = json.loads(
            (ROOT / "infrastructure/novamind-rds-bootstrap-support.template.json").read_text()
        )
        policy = document["Resources"]["BootstrapExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]
        statement = next(item for item in policy["Statement"] if item["Sid"] == "WriteBootstrapLogs")
        self.assertEqual(
            statement["Resource"]["Fn::Sub"],
            "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:${BootstrapLogGroupName}:*",
        )
        self.assertNotIn(":log-group//${BootstrapLogGroupName}", statement["Resource"]["Fn::Sub"])

    def test_bootstrap_network_template_is_secret_free_and_additive(self):
        document = json.loads(
            (ROOT / "infrastructure/novamind-rds-bootstrap-network.template.json").read_text()
        )
        serialized = json.dumps(document, sort_keys=True)
        self.assertNotIn("Secret", serialized)
        self.assertNotIn("AWS::ECS::", serialized)
        self.assertNotIn("AWS::EC2::NatGateway", serialized)
        group = document["Resources"]["BootstrapSecurityGroup"]["Properties"]
        self.assertNotIn("SecurityGroupIngress", group)
        self.assertEqual(len(group["SecurityGroupEgress"]), 2)
        self.assertEqual(group["SecurityGroupEgress"][0]["DestinationSecurityGroupId"]["Ref"], "RdsSecurityGroupId")
        self.assertEqual(group["SecurityGroupEgress"][1]["CidrIp"], "0.0.0.0/0")
        self.assertEqual(group["SecurityGroupEgress"][1]["FromPort"], 443)


if __name__ == "__main__":
    unittest.main()
