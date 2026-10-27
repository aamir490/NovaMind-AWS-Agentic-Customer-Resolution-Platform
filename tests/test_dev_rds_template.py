"""Offline safety contracts for the opt-in dev RDS stack."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def template() -> dict:
    return json.loads((ROOT / "infrastructure" / "novamind-crp-dev-rds.template.json").read_text(encoding="utf-8"))


class DevRdsTemplateTests(unittest.TestCase):
    def test_identity_and_subnet_parameters_are_fail_closed(self):
        document = template()
        parameters = document["Parameters"]
        self.assertEqual(parameters["VpcId"]["Default"], "vpc-0baf042257d729f7d")
        self.assertEqual(parameters["PrivateSubnetAId"]["Default"], "subnet-0ef827a350fddfba6")
        self.assertEqual(parameters["PrivateSubnetBId"]["Default"], "subnet-05963f195c4bdbbe5")
        for name in ("SubnetIdentityConfirmed", "SubnetAvailabilityZonesConfirmed", "DatabaseIdentityConfirmed"):
            self.assertEqual(parameters[name]["Default"], "false")
            self.assertIn(name, json.dumps(document["Rules"]))

    def test_creates_only_private_single_az_encrypted_postgres(self):
        resources = template()["Resources"]
        self.assertEqual(resources["RdsPostgres"]["Type"], "AWS::RDS::DBInstance")
        properties = resources["RdsPostgres"]["Properties"]
        self.assertEqual(properties["Engine"], "postgres")
        self.assertEqual(properties["EngineVersion"], "17.11")
        self.assertEqual(properties["DBName"], "novamindcrp")
        self.assertEqual(properties["DBInstanceClass"], "db.t4g.micro")
        self.assertEqual(properties["AllocatedStorage"], "20")
        self.assertEqual(properties["StorageType"], "gp3")
        self.assertTrue(properties["StorageEncrypted"])
        self.assertTrue(properties["ManageMasterUserPassword"])
        self.assertFalse(properties["PubliclyAccessible"])
        self.assertFalse(properties["MultiAZ"])
        self.assertEqual(properties["Port"], 5432)

    def test_db_subnet_group_uses_only_reviewed_private_subnets(self):
        resources = template()["Resources"]
        subnet_group = resources["RdsDbSubnetGroup"]["Properties"]
        self.assertEqual(subnet_group["SubnetIds"], [{"Ref": "PrivateSubnetAId"}, {"Ref": "PrivateSubnetBId"}])
        self.assertNotIn("OriginPrivateSubnet", json.dumps(subnet_group))

    def test_rds_security_group_has_no_ingress_or_broad_5432_rule(self):
        group = template()["Resources"]["RdsSecurityGroup"]["Properties"]
        self.assertNotIn("SecurityGroupIngress", group)
        self.assertEqual(group["SecurityGroupEgress"], [])
        serialized = json.dumps(template()["Resources"])
        self.assertNotIn("0.0.0.0/0", serialized)
        self.assertNotIn("SourceSecurityGroupId", serialized)

    def test_master_secret_is_rds_managed_and_no_legacy_secret_is_referenced(self):
        document = template()
        properties = document["Resources"]["RdsPostgres"]["Properties"]
        self.assertTrue(properties["ManageMasterUserPassword"])
        self.assertNotIn("MasterUserPassword", properties)
        self.assertIn("MasterUserSecret.SecretArn", json.dumps(document["Outputs"]))
        serialized = json.dumps(document)
        self.assertNotIn("novamind/groq-api-key", serialized)
        self.assertNotIn("novamind/auth-credentials", serialized)


if __name__ == "__main__":
    unittest.main()
