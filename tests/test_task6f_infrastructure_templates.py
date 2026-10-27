"""Offline static contracts for Task 6F infrastructure assets."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "infrastructure" / "novamind-crp-investigation-messaging.template.json"


class Task6FInfrastructureTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        cls.raw = json.dumps(cls.document, sort_keys=True)
        cls.resources = cls.document["Resources"]

    def test_json_and_project_environment_contract(self):
        self.assertEqual(self.document["Parameters"]["ProjectPrefix"]["Default"], "novamind-crp")
        self.assertEqual(self.document["Parameters"]["Environment"]["Default"], "dev")
        self.assertNotIn("novamind/groq-api-key", self.raw)
        self.assertNotIn("novamind/auth-credentials", self.raw)

    def test_queue_dlq_encryption_redrive_retention_and_visibility(self):
        queue = self.resources["InvestigationQueue"]["Properties"]
        dlq = self.resources["InvestigationDeadLetterQueue"]["Properties"]
        self.assertTrue(queue["SqsManagedSseEnabled"])
        self.assertTrue(dlq["SqsManagedSseEnabled"])
        self.assertEqual(queue["ReceiveMessageWaitTimeSeconds"], 20)
        self.assertIn("RedrivePolicy", queue)
        self.assertEqual(queue["VisibilityTimeout"]["Ref"], "QueueVisibilityTimeoutSeconds")
        self.assertEqual(
            self.document["Parameters"]["QueueVisibilityTimeoutSeconds"]["Default"], 300,
        )
        self.assertEqual(
            self.document["Parameters"]["QueueVisibilityTimeoutSeconds"]["MinValue"], 90,
        )
        self.assertEqual(
            self.document["Parameters"]["QueueRetentionSeconds"]["Default"], 1209600,
        )

    def test_roles_are_separate_and_least_privilege(self):
        roles = {
            name: json.dumps(self.resources[name]["Properties"]["Policies"])
            for name in ("ApiTaskRole", "PublisherTaskRole", "StrandsWorkerTaskRole")
        }
        self.assertNotIn("sqs:ReceiveMessage", roles["ApiTaskRole"])
        self.assertNotIn("sqs:DeleteMessage", roles["ApiTaskRole"])
        self.assertNotIn("bedrock:InvokeModel", roles["ApiTaskRole"])
        self.assertIn("sqs:SendMessage", roles["PublisherTaskRole"])
        self.assertNotIn("sqs:ReceiveMessage", roles["PublisherTaskRole"])
        self.assertIn("sqs:ReceiveMessage", roles["StrandsWorkerTaskRole"])
        self.assertIn("sqs:DeleteMessage", roles["StrandsWorkerTaskRole"])
        self.assertIn("sqs:ChangeMessageVisibility", roles["StrandsWorkerTaskRole"])
        self.assertIn("bedrock:InvokeModel", roles["StrandsWorkerTaskRole"])
        self.assertNotIn("AmazonSQSFullAccess", self.raw)
        self.assertNotIn("AdministratorAccess", self.raw)

    def test_network_has_no_ingress_and_logs_are_retained(self):
        sg = self.resources["WorkloadSecurityGroup"]["Properties"]
        self.assertNotIn("SecurityGroupIngress", sg)
        egress = json.dumps(sg["SecurityGroupEgress"])
        self.assertIn("5432", egress)
        self.assertIn("443", egress)
        for name in ("ApiLogGroup", "PublisherLogGroup", "StrandsWorkerLogGroup"):
            resource = self.resources[name]
            self.assertEqual(resource["DeletionPolicy"], "Retain")
            self.assertEqual(resource["UpdateReplacePolicy"], "Retain")

    def test_task_definitions_and_services_are_intentionally_absent(self):
        types = {resource["Type"] for resource in self.resources.values()}
        self.assertNotIn("AWS::ECS::TaskDefinition", types)
        self.assertNotIn("AWS::ECS::Service", types)

    def test_resources_have_required_tags(self):
        required = {
            ("Project", "novamind-crp"),
            ("Environment", "dev"),
            ("ManagedBy", "CloudFormation"),
        }
        for name, resource in self.resources.items():
            properties = resource.get("Properties", {})
            if "Tags" not in properties:
                continue
            actual = {(item["Key"], item["Value"]) for item in properties["Tags"]}
            self.assertTrue(required <= actual, name)

    def test_6g2_consumable_outputs_are_exported(self):
        expected = {
            "InvestigationQueueUrl", "ApiTaskRoleArn", "PublisherTaskRoleArn",
            "StrandsWorkerTaskRoleArn", "WorkloadSecurityGroupId", "ApiLogGroupName",
            "PublisherLogGroupName", "StrandsWorkerLogGroupName",
        }
        for name in expected:
            self.assertIn("Export", self.document["Outputs"][name])
            self.assertEqual(
                self.document["Outputs"][name]["Export"]["Name"],
                {"Fn::Sub": "${ProjectPrefix}-${Environment}-" + name},
            )


if __name__ == "__main__":
    unittest.main()
