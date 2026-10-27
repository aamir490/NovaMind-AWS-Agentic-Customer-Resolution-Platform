"""Offline safety contracts for the opt-in dev API security group stack."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def template() -> dict:
    return json.loads((ROOT / "infrastructure" / "novamind-crp-dev-api-security-group.template.json").read_text(encoding="utf-8"))


class DevApiSecurityGroupTemplateTests(unittest.TestCase):
    def test_identity_and_creation_guards_are_fail_closed(self):
        document = template()
        parameters = document["Parameters"]
        self.assertEqual(parameters["VpcId"]["Default"], "vpc-0baf042257d729f7d")
        for name in ("CreateApiSecurityGroup", "VpcIdentityConfirmed", "RdsExportConfirmed", "DnsCidrConfirmed"):
            self.assertEqual(parameters[name]["Default"], "false")
        self.assertIn("vpc-0baf042257d729f7d", json.dumps(document["Rules"]))

    def test_creates_only_one_api_security_group_with_no_ingress(self):
        document = template()
        resources = document["Resources"]
        self.assertEqual(set(resources), {"ApiSecurityGroup"})
        group = resources["ApiSecurityGroup"]
        self.assertEqual(group["Type"], "AWS::EC2::SecurityGroup")
        self.assertEqual(group["Condition"], "CreateApiSecurityGroupCondition")
        self.assertNotIn("SecurityGroupIngress", group["Properties"])
        self.assertNotIn("AWS::EC2::SecurityGroupIngress", json.dumps(document))

    def test_egress_is_exactly_postgres_dns_and_approved_https(self):
        group = template()["Resources"]["ApiSecurityGroup"]["Properties"]
        egress = group["SecurityGroupEgress"]
        self.assertEqual(len(egress), 4)
        postgres = next(rule for rule in egress if rule["FromPort"] == 5432)
        self.assertEqual(postgres["IpProtocol"], "tcp")
        self.assertEqual(postgres["DestinationSecurityGroupId"], {"Fn::ImportValue": {"Fn::Sub": "${ProjectPrefix}-${Environment}-RdsSecurityGroupId"}})
        dns = {(rule["IpProtocol"], rule["FromPort"], rule["ToPort"], rule["CidrIp"]) for rule in egress if rule["FromPort"] == 53}
        self.assertEqual(dns, {("tcp", 53, 53, "172.31.0.0/16"), ("udp", 53, 53, "172.31.0.0/16")})
        https = next(rule for rule in egress if rule["FromPort"] == 443)
        self.assertEqual((https["IpProtocol"], https["ToPort"], https["CidrIp"]), ("tcp", 443, "0.0.0.0/0"))
        self.assertIn("NAT", https["Description"])
        serialized = json.dumps(template())
        self.assertNotIn("RdsIngress", serialized)

    def test_exports_only_api_security_group_and_preserves_legacy_secret_isolation(self):
        document = template()
        output = document["Outputs"]["ApiSecurityGroupId"]
        self.assertEqual(output["Export"]["Name"], {"Fn::Sub": "${ProjectPrefix}-${Environment}-ApiSecurityGroupId"})
        serialized = json.dumps(document)
        self.assertNotIn("AWS::RDS::DBInstance", serialized)
        self.assertNotIn("AWS::SecretsManager::Secret", serialized)
        self.assertNotIn("novamind/groq-api-key", serialized)
        self.assertNotIn("novamind/auth-credentials", serialized)


if __name__ == "__main__":
    unittest.main()
