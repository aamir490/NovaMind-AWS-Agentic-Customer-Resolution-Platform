"""Offline regression tests for Bedrock provider selection and labeling."""

import json
import os
import sys
import types
import unittest
from unittest.mock import Mock, patch

from backend.app.bedrock import BedrockProvider
from backend.app.frontend_contracts import IdentityResponse, RunSnapshot, TelemetryEvent
from backend.app.frontend_runtime import FrontendRuntime
from backend.app.llm import LLMMessage, LLMRequest, LLMResponse, StructuredLLM
from backend.app.main import create_app
from backend.container.bootstrap import _llm_provider


BEDROCK_CONFIG = {
    "NOVAMIND_LLM_PROVIDER": "bedrock",
    "AWS_REGION": "us-east-1",
    "BEDROCK_MODEL_ID": "amazon.nova-lite-v1:0",
}


def _mock_boto3():
    """Inject a minimal fake boto3 so no AWS SDK is needed."""
    client = Mock()
    client.converse.return_value = {
        "stopReason": "end_turn",
        "output": {"message": {"role": "assistant",
                               "content": [{"text": '{"decision":{"kind":"final"}}'}]}},
        "usage": {"inputTokens": 5, "outputTokens": 3, "totalTokens": 8},
    }
    boto3 = types.ModuleType("boto3")
    boto3.client = Mock(return_value=client)
    botocore = types.ModuleType("botocore")
    botocore.__path__ = []
    config = types.ModuleType("botocore.config")
    config.Config = Mock(return_value=object())
    return client, boto3, botocore, config


class BedrockSelectionSafetyTests(unittest.TestCase):
    """Only explicit, valid Bedrock configuration creates a cloud provider."""

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_unset_selector_leaves_cloud_provider_unavailable(self):
        self.assertIsNone(_llm_provider())

    def test_unknown_selector_is_rejected(self):
        os.environ["NOVAMIND_LLM_PROVIDER"] = "unsupported"
        with self.assertRaisesRegex(RuntimeError, "^Container LLM provider selection is invalid$"):
            _llm_provider()

    def test_bedrock_requires_region_and_model_id(self):
        os.environ["NOVAMIND_LLM_PROVIDER"] = "bedrock"
        with self.assertRaisesRegex(RuntimeError, "^Container Bedrock configuration is invalid$"):
            _llm_provider()

    def test_bedrock_rejects_invalid_region(self):
        os.environ.update({**BEDROCK_CONFIG, "AWS_REGION": "not-a-region"})
        with self.assertRaisesRegex(RuntimeError, "^Container Bedrock configuration is invalid$"):
            _llm_provider()


class BedrockWithProfileTests(unittest.TestCase):
    """AWS_PROFILE is picked up by boto3's credential chain without blocking bootstrap."""

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        client, boto3, botocore, config = _mock_boto3()
        self.boto3_client_mock = boto3.client
        self.enterContext(patch.dict(sys.modules, {
            "boto3": boto3, "botocore": botocore, "botocore.config": config}))

    def test_aws_profile_is_not_blocked_by_bedrock_selector(self):
        os.environ.update({**BEDROCK_CONFIG, "AWS_PROFILE": "novamind"})
        provider = _llm_provider()
        self.assertIsInstance(provider, BedrockProvider)
        provider.close()

    def test_boto3_client_receives_region(self):
        os.environ.update({**BEDROCK_CONFIG, "AWS_PROFILE": "novamind"})
        with _llm_provider() as provider:
            self.assertIsInstance(provider, BedrockProvider)
        self.boto3_client_mock.assert_called_once()
        _, kwargs = self.boto3_client_mock.call_args
        self.assertEqual(kwargs["region_name"], "us-east-1")

    def test_nova_lite_model_id_is_accepted(self):
        os.environ.update({**BEDROCK_CONFIG})
        with _llm_provider() as provider:
            self.assertEqual(provider.model_id, "amazon.nova-lite-v1:0")

    def test_nova_pro_model_id_is_accepted(self):
        os.environ.update({**BEDROCK_CONFIG, "BEDROCK_MODEL_ID": "amazon.nova-pro-v1:0"})
        with _llm_provider() as provider:
            self.assertEqual(provider.model_id, "amazon.nova-pro-v1:0")

    def test_cross_region_inference_profile_arn_is_accepted(self):
        arn = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.amazon.nova-lite-v1:0"
        os.environ.update({**BEDROCK_CONFIG, "BEDROCK_MODEL_ID": arn})
        with _llm_provider() as provider:
            self.assertEqual(provider.model_id, arn)


class ProviderLabelContractTests(unittest.TestCase):
    """Public provider contracts accept only supported runtime labels."""

    def test_identity_response_rejects_unknown_provider_label(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            IdentityResponse(
                identity={"user_id": "00000000-0000-4000-8000-000000000001",
                          "role": "ADMIN", "customer_id": None},
                can_review=True, can_view_diagnostics=True,
                instance_id="00000000-0000-4000-8000-000000000002",
                provider="unsupported",
                memory_available=False,
            )

    def test_run_snapshot_rejects_unknown_provider_label(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            RunSnapshot(
                instance_id="00000000-0000-4000-8000-000000000001",
                run_id="00000000-0000-4000-8000-000000000002",
                case_id="00000000-0000-4000-8000-000000000003",
                provider="unsupported",
                status="RUNNING",
                created_at="2026-10-15T00:00:00Z",
                updated_at="2026-10-15T00:00:00Z",
            )

    def test_frontend_runtime_rejects_unknown_provider_label(self):
        with self.assertRaises(ValueError):
            FrontendRuntime(None, None, None, provider_label="unsupported")

    def test_create_app_rejects_unknown_provider_label(self):
        from pydantic import ValidationError
        with self.assertRaises((ValueError, ValidationError)):
            create_app(provider_label="unsupported")  # type: ignore[arg-type]

    def test_telemetry_event_provider_literal_still_accepts_bedrock_and_fake(self):
        """Diagnostics vocabulary kept for historical events."""
        for label in ("bedrock", "fake", "other", None):
            event = TelemetryEvent(
                timestamp="2026-10-15T00:00:00Z",
                operation="llm",
                trace_id="00000000-0000-4000-8000-000000000001",
                span_id="00000000-0000-4000-8000-000000000002",
                parent_span_id=None,
                duration_ms=5.0,
                error=None,
                provider=label,
            )
            self.assertEqual(event.provider, label)


class StructuredLLMProviderLabelTests(unittest.TestCase):
    """StructuredLLM exposes stable diagnostic labels for Bedrock and test doubles."""

    def setUp(self):
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.enterContext(patch("socket.getaddrinfo", side_effect=AssertionError("No DNS")))
        self.client = Mock()
        self.client.converse.return_value = {
            "stopReason": "end_turn",
            "output": {"message": {"role": "assistant",
                                   "content": [{"text": '{"decision":{"kind":"final"}}'}]}},
            "usage": {"inputTokens": 4, "outputTokens": 2, "totalTokens": 6},
        }

    def _snapshot(self, observer):
        """observer.snapshot() requires REVIEWER/ADMIN identity."""
        from tests.security_fixtures import admin_scope
        with admin_scope():
            return observer.snapshot()

    def test_bedrock_provider_is_labelled_bedrock(self):
        from backend.app.agent import AgentDecision
        from backend.app.observability import LocalObserver, observing
        observer = LocalObserver(capacity=10)
        provider = BedrockProvider(model_id="amazon.nova-lite-v1:0", client=self.client)
        with observing(observer):
            StructuredLLM(provider).generate(
                LLMRequest(messages=(LLMMessage(role="user", content="Test"),)),
                AgentDecision,
            )
        events = self._snapshot(observer)["events"]
        llm_events = [e for e in events if e["operation"] == "llm"]
        self.assertEqual(len(llm_events), 1)
        self.assertEqual(llm_events[0]["provider"], "bedrock")

    def test_unknown_provider_class_falls_back_to_other(self):
        from backend.app.agent import AgentDecision
        from backend.app.observability import LocalObserver, observing

        class CustomProvider:
            def generate(self, request, *, response_schema):
                return LLMResponse(text='{"decision":{"kind":"final"}}', finish_reason="stop")

        observer = LocalObserver(capacity=10)
        with observing(observer):
            StructuredLLM(CustomProvider()).generate(
                LLMRequest(messages=(LLMMessage(role="user", content="Test"),)),
                AgentDecision,
            )
        events = self._snapshot(observer)["events"]
        llm_events = [e for e in events if e["operation"] == "llm"]
        self.assertEqual(llm_events[0]["provider"], "other")


if __name__ == "__main__":
    unittest.main()
