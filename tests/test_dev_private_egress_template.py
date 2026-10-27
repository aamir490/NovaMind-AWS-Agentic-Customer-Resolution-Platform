"""Offline safety contracts for the opt-in dev private-egress stack."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def template() -> dict:
    return json.loads((ROOT / "infrastructure" / "novamind-crp-dev-private-egress.template.json").read_text(encoding="utf-8"))


class DevPrivateEgressTemplateTests(unittest.TestCase):
    def test_fixed_dev_vpc_cidrs_and_azs_are_guarded(self):
        document = template()
        parameters = document["Parameters"]
        self.assertEqual(parameters["VpcId"]["Default"], "vpc-0baf042257d729f7d")
        self.assertEqual(parameters["PrivateSubnetCidrA"]["Default"], "172.31.96.0/24")
        self.assertEqual(parameters["PrivateSubnetCidrB"]["Default"], "172.31.97.0/24")
        self.assertEqual(parameters["PrivateSubnetAzA"]["Default"], "us-east-1a")
        self.assertEqual(parameters["PrivateSubnetAzB"]["Default"], "us-east-1b")
        rules = json.dumps(document["Rules"])
        for value in ("172.31.96.0/24", "172.31.97.0/24", "us-east-1a", "us-east-1b"):
            self.assertIn(value, rules)
        self.assertEqual(parameters["PrivateCidrAvailabilityConfirmed"]["Default"], "false")
        self.assertIn("PrivateCidrAvailabilityConfirmed", rules)

    def test_requires_verified_existing_public_subnet_and_route_table(self):
        document = template()
        parameters = document["Parameters"]
        self.assertEqual(parameters["ExistingPublicSubnetId"]["Default"], "subnet-00000000")
        self.assertEqual(parameters["ExistingPublicRouteTableId"]["Default"], "rtb-00000000")
        self.assertEqual(parameters["ExistingPublicRouteTableId"]["Type"], "String")
        self.assertEqual(parameters["ExistingPublicRouteTableId"]["AllowedPattern"], "^rtb-[0-9a-f]+$")
        self.assertEqual(parameters["PublicSubnetInternetGatewayRouteConfirmed"]["Default"], "false")
        rules = json.dumps(document["Rules"]["ConfirmedPublicEgressPrerequisites"])
        self.assertIn("subnet-00000000", rules)
        self.assertIn("rtb-00000000", rules)
        self.assertIn("PublicSubnetInternetGatewayRouteConfirmed", rules)

    def test_creates_one_nat_and_one_s3_gateway_endpoint(self):
        resources = template()["Resources"]
        self.assertEqual(sum(resource["Type"] == "AWS::EC2::NatGateway" for resource in resources.values()), 1)
        endpoints = [resource for resource in resources.values() if resource["Type"] == "AWS::EC2::VPCEndpoint"]
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0]["Properties"]["VpcEndpointType"], "Gateway")
        self.assertEqual(endpoints[0]["Properties"]["ServiceName"], {"Fn::Sub": "com.amazonaws.${AWS::Region}.s3"})

    def test_routes_and_endpoint_are_limited_to_new_ecs_private_route_tables(self):
        resources = template()["Resources"]
        route_tables = {"PrivateRouteTableA", "PrivateRouteTableB"}
        for name in ("PrivateDefaultRouteA", "PrivateDefaultRouteB"):
            self.assertIn(resources[name]["Properties"]["RouteTableId"]["Ref"], route_tables)
            self.assertNotIn("DependsOn", resources[name])
        endpoint_routes = resources["S3GatewayEndpoint"]["Properties"]["RouteTableIds"]
        self.assertEqual({entry["Ref"] for entry in endpoint_routes}, route_tables)
        self.assertNotIn("OriginPrivateRouteTable", json.dumps(resources))

    def test_private_subnets_do_not_map_public_ips_and_are_tagged(self):
        resources = template()["Resources"]
        for name in ("PrivateSubnetA", "PrivateSubnetB"):
            subnet = resources[name]["Properties"]
            self.assertFalse(subnet["MapPublicIpOnLaunch"])
            tags = {tag["Key"]: tag["Value"] for tag in subnet["Tags"]}
            self.assertEqual(tags["Project"], "novamind-crp")
            self.assertEqual(tags["Environment"], "dev")
            self.assertEqual(tags["ManagedBy"], "CloudFormation")

    def test_template_has_no_legacy_secret_or_cloudfront_origin_resources(self):
        document = template()
        serialized = json.dumps(document)
        self.assertNotIn("novamind/groq-api-key", serialized)
        self.assertNotIn("novamind/auth-credentials", serialized)
        self.assertNotIn("OriginPrivateSubnet", json.dumps(document["Resources"]))
        self.assertFalse(any(resource["Type"] == "AWS::CloudFront::VpcOrigin" for resource in document["Resources"].values()))


if __name__ == "__main__":
    unittest.main()
