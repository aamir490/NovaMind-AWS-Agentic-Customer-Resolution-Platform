import inspect
import sys
import types
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from scripts.strands_bedrock_smoke import main, run_smoke
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, authenticated
from backend.app.strands_agent import StrandsWorkflow


class StrandsBedrockSmokeHarnessTests(unittest.TestCase):
    def test_default_mode_is_offline_and_makes_zero_aws_calls(self):
        fake_boto3 = Mock()
        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            result = run_smoke()
        self.assertEqual(result["mode"], "offline")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["aws_calls"], 0)
        self.assertEqual(result["tool_calls"], 0)
        self.assertEqual(result["model_call_limit"], 3)
        self.assertFalse(result["actions_executed"])
        fake_boto3.client.assert_not_called()

    def test_live_mode_requires_novamind_profile(self):
        with self.assertRaises(SystemExit):
            main(["--live", "--profile", "other"])

    def test_strands_tool_adapter_uses_json_schema_and_named_arguments(self):
        from scripts.strands_bedrock_smoke import SyntheticReadOnlyTools

        tools = SyntheticReadOnlyTools()
        identity = AuthenticatedIdentity(user_id=uuid4(), role="ADMIN")
        auth = LocalAuthenticationProvider((LocalCredential(
            token="strands-schema-test-token", identity=identity,
        ),))
        workflow = StrandsWorkflow(tools, model_factory=lambda: Mock())
        fake_strands = types.ModuleType("strands")

        def fake_tool(function, **kwargs):
            function.tool_name = kwargs["name"]
            function.tool_spec = {"inputSchema": kwargs["inputSchema"]}
            return function

        fake_strands.tool = fake_tool
        with patch.dict(sys.modules, {"strands": fake_strands}), \
                authenticated(auth, "strands-schema-test-token"):
            wrapped = workflow._make_strands_tools()
        get_case = next(item for item in wrapped if item.tool_name == "get_case")
        self.assertIn("json", get_case.tool_spec["inputSchema"])
        self.assertIn("case_id", get_case.tool_spec["inputSchema"]["json"]["properties"])
        self.assertIn("case_id", str(inspect.signature(get_case)))
        get_case(case_id=str(tools.case.id))
        self.assertEqual(tools.invocations[0][0], "get_case")
        self.assertEqual(tools.invocations[0][1]["case_id"], str(tools.case.id))

    def test_metrics_report_sdk_tool_attempts_not_only_successful_invocations(self):
        from scripts.strands_bedrock_smoke import _reported_tool_calls
        metric = type("Metric", (), {"call_count": 12})()
        result = type("Result", (), {
            "metrics": type("Metrics", (), {"tool_metrics": {"get_case": metric}})(),
        })()
        self.assertEqual(_reported_tool_calls(result, 1), 12)


if __name__ == "__main__":
    unittest.main()
