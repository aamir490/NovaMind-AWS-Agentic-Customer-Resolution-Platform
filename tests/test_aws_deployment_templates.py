"""Offline safety contracts for opt-in NovaMind CloudFormation delivery assets."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def template(name: str) -> dict:
    return json.loads((ROOT / "infrastructure" / name).read_text(encoding="utf-8"))


class AwsDeploymentTemplateTests(unittest.TestCase):
    def test_cognito_components_are_opt_in_and_public_client_has_no_secret(self):
        document = template("novamind-cognito-components.template.json")
        for name in ("CreateUserPool", "CreateGroups", "CreateSpaClient", "CreateHostedUiDomain"):
            self.assertEqual(document["Parameters"][name]["Default"], "false")
        self.assertEqual(document["Parameters"]["ProjectName"]["Default"], "novamind-crp")
        self.assertEqual(document["Resources"]["UserPool"]["Properties"]["MfaConfiguration"], "OFF")
        self.assertNotIn("EnabledMfas", document["Resources"]["UserPool"]["Properties"])
        client = document["Resources"]["SpaClient"]["Properties"]
        self.assertFalse(client["GenerateSecret"])
        self.assertEqual(client["AllowedOAuthFlows"], ["code"])
        self.assertEqual(client["AllowedOAuthScopes"], ["openid"])
        self.assertEqual(client["SupportedIdentityProviders"], ["COGNITO"])

    def test_cognito_configuration_secret_matches_api_contract_and_is_opt_in(self):
        document = template("novamind-cognito-components.template.json")
        parameters = document["Parameters"]
        self.assertEqual(parameters["CreateConfigurationSecret"]["Default"], "false")
        self.assertEqual(parameters["CustomerBindingsJson"]["Default"], "[]")
        secret = document["Resources"]["CognitoConfigurationSecret"]
        self.assertEqual(secret["Type"], "AWS::SecretsManager::Secret")
        self.assertEqual(secret["Condition"], "CreateConfigurationSecretCondition")
        self.assertEqual(
            secret["DependsOn"],
            ["CustomerGroup", "AdminGroup", "SpaClient"],
        )
        self.assertNotIn("ReviewerGroup", document["Resources"])
        secret_string = json.dumps(secret["Properties"]["SecretString"])
        for key in ("issuer", "client_id", "customer_group", "reviewer_group", "admin_group", "customer_bindings"):
            self.assertIn(key, secret_string)
        self.assertNotIn("client_secret", secret_string)
        self.assertIn("CognitoConfigurationSecretArn", document["Outputs"])

    def test_cognito_configuration_secret_requires_pool_client_and_groups(self):
        document = template("novamind-cognito-components.template.json")
        rule = json.dumps(document["Rules"]["ConfigurationSecretRequiresComponents"])
        self.assertIn("CreateGroups", rule)
        self.assertIn("CreateSpaClient", rule)
        self.assertIn("ExistingUserPoolId", rule)

    def test_domain_free_network_template_uses_private_vpc_origin_and_no_nat(self):
        document = template("novamind-ecs-network-service.template.json")
        resource_types = {item["Type"] for item in document["Resources"].values()}
        self.assertFalse(resource_types & {
            "AWS::EC2::NatGateway", "AWS::EC2::VPCEndpoint", "AWS::EC2::Route",
            "AWS::RDS::DBInstance", "AWS::EC2::EIP", "AWS::Route53::RecordSet",
            "AWS::CertificateManager::Certificate",
        })
        service = document["Resources"]["ApiService"]["Properties"]
        self.assertIn("Fn::If", service["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"])
        self.assertEqual(document["Parameters"]["EnableApiExternalHttpsEgress"]["Default"], "false")
        alb = document["Resources"]["InternalApiLoadBalancer"]["Properties"]
        self.assertEqual(alb["Scheme"], "internal")
        listener = document["Resources"]["InternalApiHttpListener"]["Properties"]
        self.assertEqual(listener["Protocol"], "HTTP")
        vpc_origin = document["Resources"]["CloudFrontVpcOrigin"]["Properties"]["VpcOriginEndpointConfig"]
        self.assertEqual(vpc_origin["OriginProtocolPolicy"], "http-only")
        self.assertEqual(document["Parameters"]["CreateCloudFrontVpcOrigin"]["Default"], "false")
        self.assertIn("Fn::And", json.dumps(document["Rules"]["ApiSecurityGroupSource"]))
        self.assertIn("Fn::And", json.dumps(document["Rules"]["TargetGroupSource"]))

    def test_network_optional_ec2_ids_and_cidrs_have_valid_sentinels(self):
        document = template("novamind-ecs-network-service.template.json")
        parameters = document["Parameters"]
        for name in ("ExistingOriginPrivateSubnetAId", "ExistingOriginPrivateSubnetBId",
                     "ExistingLoadBalancerSecurityGroupId", "ExistingApiSecurityGroupId"):
            self.assertEqual(parameters[name]["Type"], "String")
            self.assertEqual(parameters[name]["Default"], "")
        self.assertEqual(parameters["OriginPrivateSubnetCidrA"]["Default"], "0.0.0.0/32")
        rules = json.dumps(document["Rules"])
        self.assertNotIn("subnet-00000000", json.dumps(document))
        self.assertNotIn("sg-00000000", json.dumps(document))
        self.assertIn("CreateOriginPrivateSubnets", rules)
        self.assertIn("CreateLoadBalancerSecurityGroup", rules)
        self.assertIn("CreateApiSecurityGroup", rules)
        self.assertIn("0.0.0.0/32", rules)

    def test_network_origin_resources_use_project_scoped_names_and_tags(self):
        document = template("novamind-ecs-network-service.template.json")
        resources = document["Resources"]
        self.assertEqual(resources["InternalApiLoadBalancer"]["Properties"]["Name"], "novamind-crp-api-internal")
        self.assertEqual(resources["ApiTargetGroup"]["Properties"]["Name"], "novamind-crp-api")
        self.assertEqual(document["Parameters"]["VpcOriginName"]["Default"], "novamind-crp-api-vpc-origin-dev")
        for name in ("LoadBalancerSecurityGroup", "InternalApiLoadBalancer", "ApiTargetGroup", "CloudFrontVpcOrigin"):
            tags = {(item["Key"], item["Value"]) for item in resources[name]["Properties"]["Tags"]}
            self.assertTrue({("Project", "novamind-crp"), ("Environment", "dev"), ("ManagedBy", "CloudFormation")} <= tags)

    def test_bootstrap_iam_keeps_master_secret_out_of_the_api_execution_policy(self):
        document = template("novamind-rds-bootstrap-support.template.json")
        api_policy = document["Resources"]["ApiExecutionApplicationSecretPolicy"]["Properties"]["PolicyDocument"]
        encoded = json.dumps(api_policy, sort_keys=True)
        self.assertNotIn("RdsMasterSecretArn", encoded)
        self.assertIn("ApplicationSecretArn", encoded)
        task_role = document["Resources"]["BootstrapTaskRole"]["Properties"]
        self.assertEqual(document["Parameters"]["CreateBootstrapTaskRole"]["Default"], "false")
        self.assertIn("ecs-tasks.amazonaws.com", json.dumps(task_role))
        execution_role = document["Resources"]["BootstrapExecutionRole"]["Properties"]
        self.assertEqual(
            document["Parameters"]["BootstrapExecutionRoleName"]["Default"],
            "novamind-crp-bootstrap-execution-dev",
        )
        self.assertIn("ecs-tasks.amazonaws.com", json.dumps(execution_role))
        execution_policy = json.dumps(execution_role["Policies"], sort_keys=True)
        self.assertIn("ecr:GetAuthorizationToken", execution_policy)
        self.assertIn("ecr:BatchGetImage", execution_policy)
        self.assertIn("logs:PutLogEvents", execution_policy)
        self.assertNotIn("secretsmanager", execution_policy.lower())
        self.assertNotIn("RdsMasterSecretArn", execution_policy)
        self.assertIn("BootstrapExecutionRoleArn", document["Outputs"])
        self.assertIn("Export", document["Outputs"]["BootstrapExecutionRoleArn"])

    def test_cloudfront_helper_is_etag_guarded_and_does_not_create_infrastructure(self):
        script = (ROOT / "scripts" / "Configure-NovaMindCloudFrontApiOrigin.ps1").read_text(encoding="utf-8")
        self.assertIn("--if-match $document.ETag", script)
        self.assertIn("get-vpc-origin", script)
        self.assertIn("VpcOriginConfig", script)
        self.assertIn("InternalAlbArn", script)
        self.assertIn("AddMissingApiRoute", script)
        self.assertIn("OriginRequestPolicyId", script)
        self.assertIn("CachePolicyId", script)
        self.assertIn("AllowedMethods", script)
        self.assertIn("get-origin-request-policy", script)
        self.assertNotIn("create-distribution", script.lower())
        self.assertNotIn("get-secret-value", script.lower())

    def test_api_deploy_requires_an_explicit_egress_design_and_can_create_one_service(self):
        script = (ROOT / "scripts" / "Deploy-NovaMindEcsApi.ps1").read_text(encoding="utf-8")
        self.assertIn("ValidateSet('PublicTaskEgress', 'ExistingNat', 'ApprovedEgressProxy')", script)
        self.assertIn("CreateMissingService", script)
        self.assertIn("ecs create-service", script)
        self.assertIn("Cognito JWKS and Bedrock", script)
        self.assertIn("ForwardOnlyMigrationCutover", script)
        self.assertIn("ExpectedCurrentTaskDefinition", script)
        self.assertIn("MigrationPreflightEvidencePath", script)
        self.assertIn("service creation is not permitted", script)
        self.assertIn("rollback=false", script)
        self.assertIn("TargetHealth.State", script)
        self.assertIn("HealthUrl", script)
        self.assertIn("automatic dispatcher to remain disabled", script)
        verifier = (ROOT / "scripts" / "Test-NovaMindAwsRelease.ps1").read_text(encoding="utf-8")
        self.assertIn("ExpectedInternalAlbArn", verifier)

    def test_forward_only_cutover_is_opt_in_and_normal_defaults_retain_rollback(self):
        script = (ROOT / "scripts" / "Deploy-NovaMindEcsApi.ps1").read_text(encoding="utf-8")
        self.assertIn("Assert-DeploymentCircuitBreakerSafety", script)
        self.assertIn("$CircuitBreaker.rollback -notin @($true, $false)", script)
        self.assertIn("$CircuitBreaker.rollback -ne $true", script)
        self.assertIn("$createRollback = if ($ForwardOnlyMigrationCutover) { 'false' } else { 'true' }", script)
        self.assertIn("CurrentSchemaVersion -ne $requiredPostgresSchemaVersion", script)
        self.assertIn("TargetSchemaVersion -ne $requiredPostgresSchemaVersion", script)
        self.assertIn("$requiredPostgresSchemaVersion = 13", script)
        self.assertIn("SchemaOwner -ne 'novamind_app'", script)

    def test_bedrock_task_role_policy_fragment_covers_all_required_arns(self):
        """The standalone fragment must grant both invoke actions on all 4 Nova Lite ARNs."""
        document = json.loads(
            (ROOT / "infrastructure" / "novamind-ecs-api-task-role-bedrock-policy.template.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(document["Version"], "2012-10-17")
        statements = document["Statement"]
        self.assertEqual(len(statements), 1)
        stmt = statements[0]
        self.assertIn("bedrock:InvokeModel", stmt["Action"])
        self.assertIn("bedrock:InvokeModelWithResponseStream", stmt["Action"])
        self.assertEqual(stmt["Effect"], "Allow")
        resources = stmt["Resource"]
        self.assertIsInstance(resources, list)
        self.assertEqual(len(resources), 4)
        # Inference-profile ARN must be account-scoped for us-east-1
        inference_arns = [r for r in resources if ":inference-profile/" in r]
        self.assertEqual(len(inference_arns), 1)
        self.assertIn("us-east-1", inference_arns[0])
        self.assertIn("us.amazon.nova-lite-v1:0", inference_arns[0])
        self.assertIn("357001292388", inference_arns[0])
        # Foundation-model ARNs must cover all three US cross-region regions
        fm_arns = [r for r in resources if ":foundation-model/" in r]
        self.assertEqual(len(fm_arns), 3)
        fm_regions = {arn.split(":")[3] for arn in fm_arns}
        self.assertEqual(fm_regions, {"us-east-1", "us-east-2", "us-west-2"})
        for arn in fm_arns:
            self.assertIn("amazon.nova-lite-v1:0", arn)
            # Foundation-model ARNs have an empty account field
            parts = arn.split(":")
            self.assertEqual(parts[4], "", f"Foundation-model ARN must have empty account field: {arn}")
        # No placeholder strings left
        raw = json.dumps(document)
        self.assertNotIn("__BEDROCK_MODEL_ARN__", raw)
        self.assertNotIn("__ACCOUNT_ID__", raw)

    def test_6f_strands_worker_policy_allows_nova_lite_cross_region_foundations_only(self):
        document = template("novamind-crp-investigation-messaging.template.json")
        policies = document["Resources"]["StrandsWorkerTaskRole"]["Properties"]["Policies"]
        statements = [
            statement
            for policy in policies
            for statement in policy["PolicyDocument"]["Statement"]
            if statement.get("Sid", "").startswith("InvokeNovaMindCrpNovaLite")
        ]
        self.assertEqual({statement["Sid"] for statement in statements}, {
            "InvokeNovaMindCrpNovaLiteInferenceProfile",
            "InvokeNovaMindCrpNovaLiteFoundationModels",
        })
        profile = next(statement for statement in statements if statement["Sid"].endswith("InferenceProfile"))
        foundations = next(statement for statement in statements if statement["Sid"].endswith("FoundationModels"))
        expected_actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        self.assertEqual(profile["Action"], expected_actions)
        self.assertEqual(foundations["Action"], expected_actions)
        self.assertEqual(len(profile["Resource"]), 1)
        self.assertEqual(len(foundations["Resource"]), 3)
        self.assertNotIn("Condition", profile)
        self.assertEqual(
            foundations["Condition"]["StringEquals"]["bedrock:InferenceProfileArn"],
            {"Fn::Sub": "arn:${AWS::Partition}:bedrock:us-east-1:${AWS::AccountId}:inference-profile/us.amazon.nova-lite-v1:0"},
        )
        serialized = json.dumps(profile) + json.dumps(foundations)
        self.assertIn("us.amazon.nova-lite-v1:0", serialized)
        for region in ("us-east-1", "us-east-2", "us-west-2"):
            self.assertIn(f"bedrock:{region}", json.dumps(foundations))
        self.assertEqual(
            sum(":foundation-model/amazon.nova-lite-v1:0" in json.dumps(resource) for resource in foundations["Resource"]),
            3,
        )
        self.assertNotIn("ApprovedBedrockModelArns", json.dumps(document))

    def test_bootstrap_support_bedrock_policy_uses_novamind_crp_naming_and_multi_resource(self):
        """ApiTaskBedrockPolicy in bootstrap-support must use novamind-crp naming and list 4 ARNs."""
        document = template("novamind-rds-bootstrap-support.template.json")
        policy_resource = document["Resources"]["ApiTaskBedrockPolicy"]
        policy_doc = policy_resource["Properties"]["PolicyDocument"]
        policy_name = policy_resource["Properties"]["PolicyName"]
        # Must use novamind-crp naming convention
        self.assertIn("novamind-crp", policy_name)
        self.assertIn("nova-lite", policy_name)
        statements = policy_doc["Statement"]
        self.assertEqual(len(statements), 1)
        stmt = statements[0]
        self.assertIn("bedrock:InvokeModel", stmt["Action"])
        self.assertIn("bedrock:InvokeModelWithResponseStream", stmt["Action"])
        self.assertEqual(stmt["Effect"], "Allow")
        resources = stmt["Resource"]
        self.assertIsInstance(resources, list, "Resource must be a list of 4 ARN entries")
        self.assertEqual(len(resources), 4)
        # One inference-profile entry (must use Fn::Sub for account portability)
        ip_entries = [r for r in resources
                      if isinstance(r, dict) and ":inference-profile/" in json.dumps(r)]
        self.assertEqual(len(ip_entries), 1)
        ip_str = json.dumps(ip_entries[0])
        self.assertIn("us-east-1", ip_str)
        self.assertIn("us.amazon.nova-lite-v1:0", ip_str)
        self.assertIn("AWS::AccountId", ip_str)
        # Three foundation-model entries covering all three US regions
        fm_entries = [r for r in resources
                      if isinstance(r, dict) and ":foundation-model/" in json.dumps(r)]
        self.assertEqual(len(fm_entries), 3)
        fm_text = json.dumps(fm_entries)
        for region in ("us-east-1", "us-east-2", "us-west-2"):
            self.assertIn(region, fm_text)
        for entry in fm_entries:
            self.assertIn("amazon.nova-lite-v1:0", json.dumps(entry))
        # The old single-resource form must not appear
        raw = json.dumps(policy_doc)
        self.assertNotIn('"Ref": "ApprovedBedrockModelArn"', raw)
        self.assertNotIn('"Ref":"ApprovedBedrockModelArn"', raw)

    def test_approved_bedrock_model_arn_pattern_accepts_only_nova_lite_inference_profile_in_us_east_1(self):
        """AllowedPattern must accept exactly the Nova Lite inference-profile ARN for us-east-1
        and reject every other Bedrock ARN form, partition, region, or model."""
        import re
        document = template("novamind-rds-bootstrap-support.template.json")
        raw_pattern = document["Parameters"]["ApprovedBedrockModelArn"]["AllowedPattern"]
        pattern = re.compile(raw_pattern)

        # The Rule description must reference the exact ARN shape expected
        rule_desc = document["Rules"]["ApiTaskRoleForBedrock"]["Assertions"][0]["AssertDescription"]
        self.assertIn("us-east-1", rule_desc)
        self.assertIn("inference-profile", rule_desc)
        self.assertIn("us.amazon.nova-lite-v1:0", rule_desc)

        # Accepted values
        self.assertTrue(pattern.fullmatch(""),
            "Empty string must be accepted (disabled-by-default)")
        self.assertTrue(pattern.fullmatch(
            "arn:aws:bedrock:us-east-1:357001292388:inference-profile/us.amazon.nova-lite-v1:0"),
            "Exact account ARN must be accepted")
        self.assertTrue(pattern.fullmatch(
            "arn:aws:bedrock:us-east-1:000000000001:inference-profile/us.amazon.nova-lite-v1:0"),
            "Any valid 12-digit account must be accepted")

        # Rejected: wrong region
        self.assertFalse(pattern.fullmatch(
            "arn:aws:bedrock:us-west-2:357001292388:inference-profile/us.amazon.nova-lite-v1:0"),
            "us-west-2 inference-profile must be rejected")
        self.assertFalse(pattern.fullmatch(
            "arn:aws:bedrock:eu-west-1:357001292388:inference-profile/us.amazon.nova-lite-v1:0"),
            "eu-west-1 must be rejected")

        # Rejected: foundation-model ARNs (those are in the policy Resource list, not here)
        self.assertFalse(pattern.fullmatch(
            "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0"),
            "Foundation-model ARN must be rejected")

        # Rejected: wrong partition
        self.assertFalse(pattern.fullmatch(
            "arn:aws-cn:bedrock:us-east-1:357001292388:inference-profile/us.amazon.nova-lite-v1:0"),
            "aws-cn partition must be rejected")
        self.assertFalse(pattern.fullmatch(
            "arn:aws-us-gov:bedrock:us-east-1:357001292388:inference-profile/us.amazon.nova-lite-v1:0"),
            "aws-us-gov partition must be rejected")

        # Rejected: application-inference-profile
        self.assertFalse(pattern.fullmatch(
            "arn:aws:bedrock:us-east-1:357001292388:application-inference-profile/us.amazon.nova-lite-v1:0"),
            "application-inference-profile type must be rejected")

        # Rejected: wrong model
        self.assertFalse(pattern.fullmatch(
            "arn:aws:bedrock:us-east-1:357001292388:inference-profile/us.amazon.nova-pro-v1:0"),
            "Nova Pro inference-profile must be rejected")
        self.assertFalse(pattern.fullmatch(
            "arn:aws:bedrock:us-east-1:357001292388:inference-profile/anthropic.claude-3-sonnet"),
            "Other models must be rejected")

        # Rejected: malformed account ID (not exactly 12 digits)
        self.assertFalse(pattern.fullmatch(
            "arn:aws:bedrock:us-east-1:12345:inference-profile/us.amazon.nova-lite-v1:0"),
            "Short account ID must be rejected")
        self.assertFalse(pattern.fullmatch(
            "arn:aws:bedrock:us-east-1:1234567890123:inference-profile/us.amazon.nova-lite-v1:0"),
            "13-digit account ID must be rejected")

    def test_runbook_uses_live_revision_and_keeps_auto_disabled_for_migration_cutover(self):
        runbook = (ROOT / "docs" / "aws-deployment-execution-runbook.md").read_text(encoding="utf-8")
        self.assertIn("ForwardOnlyMigrationCutover", runbook)
        self.assertIn("ExpectedCurrentTaskDefinition novamind-crp-backend-dev:5", runbook)
        self.assertNotIn("BaseTaskDefinition novamind-crp-backend-dev:3", runbook)
        self.assertNotIn("BaseTaskDefinition novamind-crp-backend-dev:4", runbook)
        deployment_command = next(line for line in runbook.splitlines()
                                  if line.startswith(".\\scripts\\Deploy-NovaMindEcsApi.ps1"))
        self.assertNotIn("-EnableAutomaticDispatcher", deployment_command)
        self.assertIn("rollback=false", runbook)
        self.assertIn("rollback to revision 5", runbook)


if __name__ == "__main__":
    unittest.main()
