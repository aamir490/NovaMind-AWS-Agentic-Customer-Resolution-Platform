"""Offline safety contracts for the additive RDS ingress stack."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def template(name: str) -> dict:
    return json.loads((ROOT / "infrastructure" / name).read_text(encoding="utf-8"))


class DevRdsIngressTemplateTests(unittest.TestCase):
    def test_identity_and_approval_guards_are_fail_closed(self):
        document = template("novamind-crp-dev-rds-ingress.template.json")
        parameters = document["Parameters"]
        self.assertEqual(parameters["VpcId"]["Default"], "vpc-0baf042257d729f7d")
        self.assertEqual(parameters["IngressSourcesConfirmed"]["Default"], "false")
        self.assertNotIn("BootstrapSecurityGroupId", parameters)
        self.assertNotIn("TemporaryBootstrapIngressApproved", parameters)
        self.assertIn("vpc-0baf042257d729f7d", json.dumps(document["Rules"]))
        self.assertIn("IngressSourcesConfirmed", json.dumps(document["Rules"]))
        self.assertNotIn("TemporaryBootstrapIngressApproved", json.dumps(document["Rules"]))

    def test_uses_verified_rds_and_workload_exports_and_explicit_other_refs(self):
        document = template("novamind-crp-dev-rds-ingress.template.json")
        resources = document["Resources"]
        for name in resources:
            props = resources[name]["Properties"]
            self.assertEqual(props["GroupId"], {"Fn::ImportValue": {"Fn::Sub": "${ProjectPrefix}-${Environment}-RdsSecurityGroupId"}})
        self.assertEqual(
            resources["RdsIngressFromWorkload"]["Properties"]["SourceSecurityGroupId"],
            {"Fn::ImportValue": {"Fn::Sub": "${ProjectPrefix}-${Environment}-WorkloadSecurityGroupId"}},
        )
        self.assertEqual(resources["RdsIngressFromApi"]["Properties"]["SourceSecurityGroupId"], {"Ref": "ApiSecurityGroupId"})

    def test_exactly_two_permanent_tcp_5432_rules_and_no_bootstrap_source(self):
        document = template("novamind-crp-dev-rds-ingress.template.json")
        resources = document["Resources"]
        self.assertEqual(set(resources), {"RdsIngressFromApi", "RdsIngressFromWorkload"})
        for resource in resources.values():
            properties = resource["Properties"]
            self.assertEqual(properties["Type"] if "Type" in properties else "tcp", "tcp")
            self.assertEqual(properties["IpProtocol"], "tcp")
            self.assertEqual(properties["FromPort"], 5432)
            self.assertEqual(properties["ToPort"], 5432)
            self.assertIn("SourceSecurityGroupId", properties)
            self.assertNotIn("CidrIp", properties)
            self.assertNotIn("SourcePrefixListId", properties)
        serialized = json.dumps(document)
        self.assertNotIn("0.0.0.0/0", serialized)
        self.assertNotIn("VpcCidr", serialized)
        self.assertNotIn("Bootstrap", serialized)

    def test_existing_stack_contracts_expose_expected_imports_or_outputs(self):
        rds = template("novamind-crp-dev-rds.template.json")
        messaging = template("novamind-crp-investigation-messaging.template.json")
        network = template("novamind-ecs-network-service.template.json")
        self.assertIn("RdsSecurityGroupId", rds["Outputs"])
        self.assertIn("WorkloadSecurityGroupId", messaging["Outputs"])
        self.assertIn("ApiSecurityGroupId", network["Outputs"])
        self.assertNotIn("Export", network["Outputs"]["ApiSecurityGroupId"])

    def test_no_secret_or_database_creation_resources_are_added(self):
        document = template("novamind-crp-dev-rds-ingress.template.json")
        resource_types = {resource["Type"] for resource in document["Resources"].values()}
        self.assertEqual(resource_types, {"AWS::EC2::SecurityGroupIngress"})
        serialized = json.dumps(document)
        self.assertNotIn("AWS::RDS::DBInstance", serialized)
        self.assertNotIn("AWS::SecretsManager::Secret", serialized)
        self.assertNotIn("novamind/groq-api-key", serialized)
        self.assertNotIn("novamind/auth-credentials", serialized)

    def test_temporary_bootstrap_ingress_has_one_canonical_owner(self):
        permanent = template("novamind-crp-dev-rds-ingress.template.json")
        bootstrap = template("novamind-rds-bootstrap-support.template.json")
        self.assertNotIn("RdsIngressFromBootstrap", permanent["Resources"])
        self.assertIn("RdsIngressFromBootstrapSecurityGroup", bootstrap["Resources"])
        resource = bootstrap["Resources"]["RdsIngressFromBootstrapSecurityGroup"]
        self.assertEqual(resource["Condition"], "CreateBootstrapSecurityGroupCondition")
        properties = resource["Properties"]
        self.assertEqual(properties["IpProtocol"], "tcp")
        self.assertEqual((properties["FromPort"], properties["ToPort"]), (5432, 5432))
        self.assertEqual(properties["SourceSecurityGroupId"], {"Ref": "BootstrapSecurityGroup"})


if __name__ == "__main__":
    unittest.main()
