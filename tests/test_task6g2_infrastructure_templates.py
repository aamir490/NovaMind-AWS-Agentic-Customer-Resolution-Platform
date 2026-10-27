"""Offline CloudFormation contracts for the Task 6G2 ECS service split."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infrastructure" / "novamind-crp-investigation-services.template.json"


class Task6G2InfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        cls.resources = cls.document["Resources"]
        cls.raw = json.dumps(cls.document, sort_keys=True)

    def test_template_is_project_scoped_and_excludes_legacy_runtime_and_secrets(self):
        self.assertEqual(self.document["Parameters"]["ProjectPrefix"]["Default"], "novamind-crp")
        self.assertEqual(self.document["Parameters"]["Environment"]["Default"], "dev")
        for forbidden in ("LangGraph", "langgraph", "SQLite", "sqlite", "novamind/groq-api-key", "novamind/auth-credentials"):
            self.assertNotIn(forbidden, self.raw)
        for resource in self.resources.values():
            tags = resource.get("Properties", {}).get("Tags", [])
            if tags:
                values = {(item["Key"], item["Value"]) for item in tags}
                self.assertTrue({("Project", "novamind-crp"), ("Environment", "dev"), ("ManagedBy", "CloudFormation")} <= values)

    def test_three_task_definitions_and_services_use_6g1_roles(self):
        task_defs = {name: self.resources[name] for name in ("ApiTaskDefinition", "PublisherTaskDefinition", "WorkerTaskDefinition")}
        services = {name: self.resources[name] for name in ("ApiService", "PublisherService", "WorkerService")}
        self.assertEqual({item["Type"] for item in task_defs.values()}, {"AWS::ECS::TaskDefinition"})
        self.assertEqual({item["Type"] for item in services.values()}, {"AWS::ECS::Service"})
        self.assertEqual(services["ApiService"]["Properties"]["LoadBalancers"][0]["ContainerName"], "backend")
        for name, resource in task_defs.items():
            properties = resource["Properties"]
            self.assertEqual(properties["NetworkMode"], "awsvpc")
            self.assertEqual(properties["RequiresCompatibilities"], ["FARGATE"])
            container = properties["ContainerDefinitions"][0]
            self.assertEqual(container["Command"], ["python", "-m", "backend.container.ecs_entrypoint"])
            self.assertTrue(container["ReadonlyRootFilesystem"])
            self.assertEqual(container["StopTimeout"], 120)
        for name, export in (("ApiTaskDefinition", "ApiTaskRoleArn"), ("PublisherTaskDefinition", "PublisherTaskRoleArn"), ("WorkerTaskDefinition", "StrandsWorkerTaskRoleArn")):
            self.assertEqual(task_defs[name]["Properties"]["TaskRoleArn"], {"Fn::ImportValue": {"Fn::Sub": "${ProjectPrefix}-${Environment}-" + export}})

    def test_process_modes_and_strands_only_worker_are_explicit(self):
        env_by_name = {}
        for name in ("ApiTaskDefinition", "PublisherTaskDefinition", "WorkerTaskDefinition"):
            env_by_name[name] = {item["Name"]: item["Value"] for item in self.resources[name]["Properties"]["ContainerDefinitions"][0]["Environment"]}
        self.assertEqual(env_by_name["ApiTaskDefinition"]["NOVAMIND_PROCESS_ROLE"], "api")
        self.assertEqual(env_by_name["PublisherTaskDefinition"]["NOVAMIND_PROCESS_ROLE"], "publisher")
        self.assertEqual(env_by_name["WorkerTaskDefinition"]["NOVAMIND_PROCESS_ROLE"], "worker")
        self.assertEqual(env_by_name["ApiTaskDefinition"]["NOVAMIND_AGENT_ENGINE"], "strands")
        self.assertEqual(env_by_name["WorkerTaskDefinition"]["NOVAMIND_AGENT_ENGINE"], "strands")
        self.assertEqual(env_by_name["PublisherTaskDefinition"]["NOVAMIND_AGENT_ENGINE"], "strands")
        self.assertEqual(env_by_name["PublisherTaskDefinition"]["NOVAMIND_LLM_PROVIDER"], "bedrock")
        self.assertNotIn("NOVAMIND_OFFLINE_TESTING", self.raw)

    def test_secret_injection_is_project_scoped_and_publisher_is_minimal(self):
        parameters = self.document["Parameters"]
        self.assertIn("novamind-crp-", parameters["PostgresApplicationSecretArn"]["AllowedPattern"])
        self.assertIn("novamind-crp-", parameters["CognitoConfigurationSecretArn"]["AllowedPattern"])
        api_secrets = {item["Name"]: item["ValueFrom"] for item in self.resources["ApiTaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Secrets"]}
        self.assertIn("NOVAMIND_POSTGRES_SECRET", api_secrets)
        self.assertIn("NOVAMIND_COGNITO_ISSUER", api_secrets)
        worker_secrets = {item["Name"]: item["ValueFrom"] for item in self.resources["WorkerTaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Secrets"]}
        self.assertEqual(list(worker_secrets), ["NOVAMIND_POSTGRES_SECRET"])
        for name in ("ApiTaskDefinition",):
            secrets = {item["Name"]: item["ValueFrom"] for item in self.resources[name]["Properties"]["ContainerDefinitions"][0]["Secrets"]}
            self.assertIn("NOVAMIND_POSTGRES_SECRET", secrets)
            self.assertIn("NOVAMIND_COGNITO_ISSUER", secrets)
        publisher_secrets = self.resources["PublisherTaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Secrets"]
        self.assertEqual([item["Name"] for item in publisher_secrets], ["NOVAMIND_POSTGRES_SECRET"])
        self.assertNotIn("NOVAMIND_COGNITO_REVIEWER_GROUP", api_secrets)

    def test_cognito_is_admin_customer_only_and_log_permissions_match_imported_groups(self):
        raw = self.raw
        self.assertNotIn("reviewer_group", raw)
        self.assertNotIn("NOVAMIND_COGNITO_REVIEWER_GROUP", raw)
        expected_logs = {
            "ApiExecutionRole": "/ecs/${ProjectPrefix}/api/${Environment}:*",
            "PublisherExecutionRole": "/ecs/${ProjectPrefix}/publisher/${Environment}:*",
            "WorkerExecutionRole": "/ecs/${ProjectPrefix}/strands-worker/${Environment}:*",
        }
        for role, suffix in expected_logs.items():
            policy = json.dumps(self.resources[role]["Properties"]["Policies"])
            self.assertIn("log-group:" + suffix, policy)

    def test_bedrock_model_parameter_is_explicit_in_deployment_parameters(self):
        parameters = json.loads((ROOT / "infrastructure" / "novamind-crp-investigation-services.parameters.json").read_text(encoding="utf-8"))
        values = {item["ParameterKey"]: item["ParameterValue"] for item in parameters}
        self.assertEqual(values["BedrockModelId"], "us.amazon.nova-lite-v1:0")
        self.assertIn("@sha256:f740cccc19d02688af249e896785be2c3e2c00ad8be2369d60391e3e82d06d3a", values["ImageUri"])
        self.assertEqual(values["PrivateSubnetIds"], "subnet-0ef827a350fddfba6,subnet-05963f195c4bdbbe5")

    def test_6f_resources_are_imported_and_queue_arn_is_not_an_unused_parameter(self):
        self.assertNotIn("InvestigationQueueArn", self.document["Parameters"])
        raw = self.raw
        for export in ("InvestigationQueueUrl", "ApiTaskRoleArn", "PublisherTaskRoleArn", "StrandsWorkerTaskRoleArn", "WorkloadSecurityGroupId", "ApiLogGroupName", "PublisherLogGroupName", "StrandsWorkerLogGroupName"):
            self.assertIn("${ProjectPrefix}-${Environment}-" + export, raw)
        for name in ("PublisherService", "WorkerService"):
            security_groups = self.resources[name]["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]["SecurityGroups"]
            self.assertEqual(security_groups[0], {"Fn::ImportValue": {"Fn::Sub": "${ProjectPrefix}-${Environment}-WorkloadSecurityGroupId"}})

    def test_api_health_check_and_private_networking_are_configured(self):
        api_container = self.resources["ApiTaskDefinition"]["Properties"]["ContainerDefinitions"][0]
        health = api_container["HealthCheck"]
        self.assertEqual(health["Command"], ["CMD-SHELL", "python -m backend.container.healthcheck"])
        self.assertEqual(self.resources["ApiService"]["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"], "DISABLED")
        for name in ("ApiService", "PublisherService", "WorkerService"):
            network = self.resources[name]["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
            self.assertEqual(network["AssignPublicIp"], "DISABLED")
            self.assertEqual(network["Subnets"], {"Ref": "PrivateSubnetIds"})
            self.assertFalse(self.resources[name]["Properties"]["EnableExecuteCommand"])

    def test_execution_roles_scope_logs_and_secrets_without_bedrock(self):
        api_policy = json.dumps(self.resources["ApiExecutionRole"]["Properties"]["Policies"])
        publisher_policy = json.dumps(self.resources["PublisherExecutionRole"]["Properties"]["Policies"])
        worker_policy = json.dumps(self.resources["WorkerExecutionRole"]["Properties"]["Policies"])
        self.assertIn("secretsmanager:GetSecretValue", api_policy)
        self.assertIn("secretsmanager:GetSecretValue", publisher_policy)
        self.assertIn("secretsmanager:GetSecretValue", worker_policy)
        self.assertNotIn("CognitoConfigurationSecretArn", worker_policy)
        self.assertNotIn("bedrock:InvokeModel", api_policy)
        self.assertNotIn("bedrock:InvokeModel", publisher_policy)
        self.assertNotIn("sqs:ReceiveMessage", api_policy + publisher_policy)
        self.assertNotIn("AdministratorAccess", self.raw)
        self.assertNotIn("AmazonECS_FullAccess", self.raw)

    def test_all_execution_roles_can_pull_only_the_project_backend_image(self):
        expected_actions = {
            "ecr:BatchCheckLayerAvailability",
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchGetImage",
        }
        roles = ("ApiExecutionRole", "PublisherExecutionRole", "WorkerExecutionRole")
        for role_name in roles:
            statements = self.resources[role_name]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
            token = next(statement for statement in statements if statement["Sid"] == "PullBackendImage")
            self.assertEqual(token["Action"], "ecr:GetAuthorizationToken")
            self.assertEqual(token["Resource"], "*")
            image = next(statement for statement in statements if statement["Sid"] == "ReadBackendImage")
            self.assertEqual(set(image["Action"]), expected_actions)
            self.assertEqual(
                image["Resource"],
                {"Fn::Sub": "arn:${AWS::Partition}:ecr:${AWS::Region}:${AWS::AccountId}:repository/novamind-crp-backend-dev"},
            )
            for statement in statements:
                if any(action.startswith("ecr:") for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])):
                    self.assertIn(statement["Sid"], {"PullBackendImage", "ReadBackendImage"})


if __name__ == "__main__":
    unittest.main()
