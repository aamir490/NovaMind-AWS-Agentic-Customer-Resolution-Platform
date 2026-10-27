"""Offline contracts for private S3 + CloudFront frontend delivery."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infrastructure" / "novamind-crp-frontend-delivery.template.json"
PARAMETERS = ROOT / "infrastructure" / "novamind-crp-frontend-delivery.parameters.json"


class FrontendDeliveryTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        cls.resources = cls.document["Resources"]

    def test_project_scoping_and_existing_origin_guards(self):
        self.assertEqual(self.document["Parameters"]["ProjectPrefix"]["AllowedValues"], ["novamind-crp"])
        self.assertEqual(self.document["Parameters"]["Environment"]["AllowedValues"], ["dev"])
        self.assertEqual(self.document["Parameters"]["ExistingCloudFrontVpcOriginId"]["AllowedValues"], ["vo_JfIJZ1vwgbqBqUZmbvDyu3"])
        parameters = {item["ParameterKey"]: item["ParameterValue"] for item in json.loads(PARAMETERS.read_text(encoding="utf-8"))}
        self.assertEqual(parameters["ExistingCloudFrontVpcOriginId"], "vo_JfIJZ1vwgbqBqUZmbvDyu3")
        self.assertIn("novamind-crp-frontend-repair-dev", self.document["Parameters"]["FrontendBucketName"]["AllowedValues"])

    def test_bucket_is_private_encrypted_retained_and_oac_scoped(self):
        bucket = self.resources["FrontendBucket"]
        props = bucket["Properties"]
        self.assertEqual(bucket["DeletionPolicy"], "Retain")
        self.assertEqual(bucket["UpdateReplacePolicy"], "Retain")
        self.assertEqual(props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"], "AES256")
        self.assertTrue(all(props["PublicAccessBlockConfiguration"].values()))
        self.assertEqual(props["OwnershipControls"]["Rules"][0]["ObjectOwnership"], "BucketOwnerEnforced")
        policy = self.resources["FrontendBucketPolicy"]["Properties"]["PolicyDocument"]
        statement = policy["Statement"][0]
        self.assertEqual(statement["Principal"], {"Service": "cloudfront.amazonaws.com"})
        self.assertEqual(statement["Action"], "s3:GetObject")
        self.assertIn("FrontendDistribution", json.dumps(statement["Condition"]))

    def test_spa_default_and_uncached_api_behavior_are_separate(self):
        distribution = self.resources["FrontendDistribution"]["Properties"]["DistributionConfig"]
        self.assertEqual(distribution["DefaultRootObject"], "index.html")
        default = distribution["DefaultCacheBehavior"]
        self.assertEqual(default["TargetOriginId"], "frontend-s3")
        self.assertEqual(default["ViewerProtocolPolicy"], "redirect-to-https")
        self.assertEqual(default["CachePolicyId"], "658327ea-f89d-4fab-a63d-7e88639e58f6")
        self.assertEqual(default["FunctionAssociations"][0]["EventType"], "viewer-request")
        api = distribution["CacheBehaviors"][0]
        self.assertEqual(api["PathPattern"], "/api/*")
        self.assertEqual(api["TargetOriginId"], "api-vpc-origin")
        self.assertEqual(distribution["Origins"][1]["VpcOriginConfig"]["OwnerAccountId"], {"Ref": "AWS::AccountId"})
        self.assertEqual(api["CachePolicyId"], "4135ea2d-6df8-44a3-9df3-4b5a84be39ad")
        self.assertEqual(api["OriginRequestPolicyId"], "b689b0a8-53d0-40ab-baf2-68738e2966ac")
        self.assertEqual(default["ResponseHeadersPolicyId"], {"Ref": "FrontendResponseHeadersPolicy"})
        self.assertEqual(api["ResponseHeadersPolicyId"], {"Ref": "FrontendResponseHeadersPolicy"})
        self.assertEqual(set(api["AllowedMethods"]), {"GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"})
        self.assertNotIn("CustomErrorResponses", distribution)

    def test_security_headers_are_strict_and_cognito_scoped(self):
        config = self.resources["FrontendResponseHeadersPolicy"]["Properties"]["ResponseHeadersPolicyConfig"]
        security = config["SecurityHeadersConfig"]
        self.assertEqual(security["StrictTransportSecurity"]["AccessControlMaxAgeSec"], 31536000)
        self.assertTrue(security["StrictTransportSecurity"]["IncludeSubdomains"])
        self.assertTrue(security["ContentTypeOptions"]["Override"])
        self.assertEqual(security["FrameOptions"]["FrameOption"], "DENY")
        self.assertEqual(security["ReferrerPolicy"]["ReferrerPolicy"], "strict-origin-when-cross-origin")
        csp = security["ContentSecurityPolicy"]["ContentSecurityPolicy"]["Fn::Sub"]
        self.assertIn("connect-src 'self' ${CognitoHostedUiDomain}", csp)
        self.assertIn("form-action 'self' ${CognitoHostedUiDomain}", csp)
        self.assertIn("script-src 'self' 'sha256-", csp)
        self.assertIn("style-src-attr 'unsafe-inline'", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("connect-src *", csp)

    def test_cognito_domain_is_exactly_scoped(self):
        parameter = self.document["Parameters"]["CognitoHostedUiDomain"]
        self.assertEqual(parameter["AllowedValues"], ["https://novamind-crp-dev-357001292388.auth.us-east-1.amazoncognito.com"])
        parameters = {item["ParameterKey"]: item["ParameterValue"] for item in json.loads(PARAMETERS.read_text(encoding="utf-8"))}
        self.assertEqual(parameters["CognitoHostedUiDomain"], parameter["AllowedValues"][0])

    def test_spa_function_never_rewrites_api_paths(self):
        code = self.resources["SpaRewriteFunction"]["Properties"]["FunctionCode"]
        self.assertIn("uri.indexOf('/api/') === 0", code)
        self.assertIn("request.uri = '/index.html'", code)

    def test_no_public_cloudfront_or_legacy_secret_resources(self):
        raw = json.dumps(self.document)
        self.assertNotIn("WebsiteConfiguration", raw)
        self.assertNotIn("novamind/groq-api-key", raw)
        self.assertNotIn("novamind/auth-credentials", raw)
        self.assertNotIn("AWS::SecretsManager::Secret", raw)


if __name__ == "__main__":
    unittest.main()
