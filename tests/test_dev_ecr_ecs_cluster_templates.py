"""Offline contracts for additive dev ECR and ECS cluster templates."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def template(name: str) -> dict:
    return json.loads((ROOT / "infrastructure" / name).read_text(encoding="utf-8"))


class DevEcrEcsClusterTemplateTests(unittest.TestCase):
    def test_ecr_template_is_opt_in_project_scoped_and_private(self):
        document = template("novamind-crp-dev-ecr.template.json")
        parameters = document["Parameters"]
        self.assertEqual(parameters["ProjectPrefix"]["Default"], "novamind-crp")
        self.assertEqual(parameters["Environment"]["Default"], "dev")
        self.assertEqual(parameters["CreateEcrRepository"]["Default"], "false")
        self.assertEqual(parameters["RepositoryName"]["Default"], "novamind-crp-backend-dev")
        resource = document["Resources"]["BackendRepository"]
        properties = resource["Properties"]
        self.assertEqual(resource["Condition"], "CreateEcrRepositoryCondition")
        self.assertTrue(properties["ImageScanningConfiguration"]["ScanOnPush"])
        self.assertEqual(properties["ImageTagMutability"], "IMMUTABLE")
        self.assertEqual(properties["EncryptionConfiguration"]["EncryptionType"], "AES256")
        self.assertEqual(resource["DeletionPolicy"], "Retain")
        self.assertEqual(resource["UpdateReplacePolicy"], "Retain")

    def test_ecr_lifecycle_is_valid_and_tags_are_required(self):
        document = template("novamind-crp-dev-ecr.template.json")
        properties = document["Resources"]["BackendRepository"]["Properties"]
        lifecycle = json.loads(properties["LifecyclePolicy"]["LifecyclePolicyText"])
        rules = lifecycle["rules"]
        self.assertEqual([rule["rulePriority"] for rule in rules], [1, 2])
        self.assertEqual(rules[0]["selection"]["tagStatus"], "untagged")
        self.assertEqual(rules[0]["selection"]["countNumber"], 1)
        self.assertEqual(rules[1]["selection"]["tagStatus"], "any")
        self.assertEqual(rules[1]["selection"]["countNumber"], 50)
        tags = {(item["Key"], item["Value"]) for item in properties["Tags"]}
        self.assertTrue({
            ("Project", "novamind-crp"),
            ("Environment", "dev"),
            ("ManagedBy", "CloudFormation"),
        } <= tags)
        self.assertIn("BackendRepositoryArn", document["Outputs"])
        self.assertIn("BackendRepositoryUri", document["Outputs"])

    def test_ecs_cluster_is_opt_in_insights_enabled_and_tagged(self):
        document = template("novamind-crp-dev-ecs-cluster.template.json")
        parameters = document["Parameters"]
        self.assertEqual(parameters["CreateEcsCluster"]["Default"], "false")
        self.assertEqual(parameters["ClusterName"]["Default"], "novamind-crp-ecs-cluster-dev")
        resource = document["Resources"]["EcsCluster"]
        self.assertEqual(resource["Condition"], "CreateEcsClusterCondition")
        settings = resource["Properties"]["ClusterSettings"]
        self.assertIn({"Name": "containerInsights", "Value": "enabled"}, settings)
        tags = {(item["Key"], item["Value"]) for item in resource["Properties"]["Tags"]}
        self.assertTrue({
            ("Project", "novamind-crp"),
            ("Environment", "dev"),
            ("ManagedBy", "CloudFormation"),
        } <= tags)
        self.assertIn("EcsClusterArn", document["Outputs"])
        self.assertIn("EcsClusterName", document["Outputs"])

    def test_templates_are_additive_and_do_not_create_services_or_secrets(self):
        for name in ("novamind-crp-dev-ecr.template.json", "novamind-crp-dev-ecs-cluster.template.json"):
            document = template(name)
            resource_types = {resource["Type"] for resource in document["Resources"].values()}
            self.assertNotIn("AWS::ECS::Service", resource_types)
            self.assertNotIn("AWS::ECS::TaskDefinition", resource_types)
            self.assertNotIn("AWS::SecretsManager::Secret", resource_types)
            serialized = json.dumps(document, sort_keys=True)
            self.assertNotIn("novamind/groq-api-key", serialized)
            self.assertNotIn("novamind/auth-credentials", serialized)


if __name__ == "__main__":
    unittest.main()
