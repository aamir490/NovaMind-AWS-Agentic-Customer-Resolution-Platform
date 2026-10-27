"""Bedrock container selection and contracts with a fully mocked SDK client."""

import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import httpx
import yaml

from backend.app.bedrock import BedrockProvider
from backend.app.proposals import ProposalStatus
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.service import ConflictError
from backend.container.bootstrap import _llm_provider, create_container_app


CONFIG = {"NOVAMIND_LLM_PROVIDER": "bedrock", "AWS_REGION": "us-east-1",
          "BEDROCK_MODEL_ID": "us.anthropic.claude-3-5-sonnet-20240620-v1:0"}
TOKEN = "container-bedrock-admin-test-token"
CUSTOMER_TOKEN = "container-bedrock-demo-customer-token"
REVIEWER_TOKEN = "container-bedrock-demo-reviewer-token"
CUSTOMER_ID = UUID(int=31)
ROOT = Path(__file__).resolve().parents[1]


class ContainerBedrockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.enterContext(patch("socket.getaddrinfo", side_effect=AssertionError("No DNS")))
        self.client = Mock()
        self.client.converse.return_value = {"stopReason": "end_turn", "output": {"message": {
            "role": "assistant", "content": [{"text": '{"decision":{"kind":"final"}}'}]}},
            "usage": {"inputTokens": 5, "outputTokens": 6, "totalTokens": 11}}
        self.constructor = Mock(return_value=self.client)
        self.config_constructor = Mock(return_value=object())
        boto3 = types.ModuleType("boto3")
        boto3.client = self.constructor
        botocore = types.ModuleType("botocore")
        botocore.__path__ = []
        config = types.ModuleType("botocore.config")
        config.Config = self.config_constructor
        self.enterContext(patch.dict(sys.modules, {"boto3": boto3, "botocore": botocore,
                                                   "botocore.config": config}))

    async def request(self, app, method, path, body=None, token=TOKEN):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://local.test") as client:
            return await client.request(method, path, json=body,
                headers={"Authorization": f"Bearer {token}"} if token else {})

    def test_unset_and_unknown_selectors_remain_default_deny(self):
        os.environ.update({key: value for key, value in CONFIG.items()
                           if key != "NOVAMIND_LLM_PROVIDER"})
        self.assertIsNone(_llm_provider())
        app = create_container_app()
        self.assertIsNone(app.state.frontend_runtime.provider_factory)
        app.state.frontend_runtime.close()
        for selector in ("", "BEDROCK", "unknown", "gemini"):
            with self.subTest(selector=selector):
                os.environ["NOVAMIND_LLM_PROVIDER"] = selector
                with self.assertRaisesRegex(RuntimeError, "^Container LLM provider selection is invalid$"):
                    create_container_app()
        self.constructor.assert_not_called()

    def test_invalid_region_and_model_fail_before_sdk_client_creation(self):
        invalid = ({"AWS_REGION": ""}, {"AWS_REGION": "us east 1"},
                   {"AWS_REGION": "https://invalid"}, {"BEDROCK_MODEL_ID": ""},
                   {"BEDROCK_MODEL_ID": "model with spaces"},
                   {"BEDROCK_MODEL_ID": "arn:aws:bedrock:us-east-1:123456789012:wrong/x"})
        for change in invalid:
            with self.subTest(change=change), patch.dict(os.environ, {**CONFIG, **change}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "^Container Bedrock configuration is invalid$") as caught:
                    create_container_app()
                self.assertTrue(caught.exception.__suppress_context__)
        self.constructor.assert_not_called()

    def test_profile_arn_and_retired_configuration(self):
        os.environ.update({**CONFIG, "BEDROCK_MODEL_ID":
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/profile.v1"})
        with _llm_provider() as provider:
            self.assertIsInstance(provider, BedrockProvider)
        self.constructor.assert_called_once()
        self.constructor.reset_mock()
        for name in ("GEMINI_API_KEY", "GEMINI_MODEL"):
            with self.subTest(name=name), patch.dict(os.environ, {**CONFIG, name: ""}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "^Container LLM provider selection is invalid$"):
                    create_container_app()
        self.constructor.assert_not_called()

    def test_sdk_construction_uses_region_standard_credentials_and_one_attempt(self):
        os.environ.update(CONFIG)
        with _llm_provider() as provider:
            self.assertIsInstance(provider, BedrockProvider)
            self.assertEqual(provider.model_id, CONFIG["BEDROCK_MODEL_ID"])
        self.config_constructor.assert_called_once_with(
            connect_timeout=30, read_timeout=30, retries={"total_max_attempts": 1})
        self.constructor.assert_called_once_with("bedrock-runtime", region_name="us-east-1",
                                                 config=self.config_constructor.return_value)
        self.client.close.assert_called_once()
        self.client.converse.assert_not_called()

    def test_authentication_precedes_client_creation_and_failed_app_closes_client(self):
        os.environ.update({**CONFIG, "NOVAMIND_AUTH_FILE": "relative-invalid.json"})
        with self.assertRaisesRegex(RuntimeError, "^Container authentication configuration is invalid$"):
            create_container_app()
        self.constructor.assert_not_called()
        del os.environ["NOVAMIND_AUTH_FILE"]
        with patch("backend.container.bootstrap.create_app", side_effect=RuntimeError("app failure")):
            with self.assertRaisesRegex(RuntimeError, "app failure"):
                create_container_app()
        self.client.close.assert_called_once()

    def test_sdk_construction_failure_is_sanitized_without_fallback(self):
        os.environ.update(CONFIG)
        self.constructor.side_effect = ValueError("private credential detail")
        with patch("backend.container.bootstrap.create_app") as app_factory:
            with self.assertRaisesRegex(RuntimeError, "^Container Bedrock configuration is invalid$") as caught:
                create_container_app()
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertNotIn("private", str(caught.exception))
            app_factory.assert_not_called()

    def test_compose_overlay_selects_only_backend_without_embedded_credentials(self):
        overlay = yaml.safe_load((ROOT / "compose.bedrock.yaml").read_text())
        self.assertEqual(set(overlay["services"]), {"backend"})
        backend = overlay["services"]["backend"]
        self.assertEqual(backend["networks"], ["application", "edge"])
        self.assertEqual(backend["environment"], {
            "NOVAMIND_LLM_PROVIDER": "bedrock",
            "AWS_REGION": "${AWS_REGION:?Set AWS_REGION in the invoking process}",
            "BEDROCK_MODEL_ID": "${BEDROCK_MODEL_ID:?Set BEDROCK_MODEL_ID in the invoking process}"})
        self.assertNotIn("AWS_ACCESS_KEY_ID", (ROOT / "compose.bedrock.yaml").read_text())
        demo_path = ROOT / "compose.bedrock.demo.yaml"
        demo = yaml.safe_load(demo_path.read_text())
        self.assertEqual(demo, {"services": {"backend": {"environment": {
            "NOVAMIND_BEDROCK_DEMO_DATA": "true",
        }}}})
        demo_text = demo_path.read_text()
        for forbidden in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "secrets:", "volumes:"):
            self.assertNotIn(forbidden, demo_text)

    async def test_opt_in_bedrock_demo_lap10_reaches_review_without_model_call(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        auth = directory / "auth.json"
        auth.write_text(json.dumps([
            {"token": CUSTOMER_TOKEN, "identity": {
                "user_id": str(UUID(int=32)), "role": "CUSTOMER",
                "customer_id": str(CUSTOMER_ID)}},
            {"token": REVIEWER_TOKEN, "identity": {
                "user_id": str(UUID(int=33)), "role": "REVIEWER"}},
        ]), encoding="utf-8")
        os.environ.update({**CONFIG, "NOVAMIND_AUTH_FILE": str(auth),
                           "NOVAMIND_BEDROCK_DEMO_DATA": "true"})
        app = create_container_app()
        service = app.state.case_service
        cases = service.list_cases(CUSTOMER_ID)
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case.subject,
                         "[SYNTHETIC BEDROCK DEMO] Eligible LAP-10 refund review")
        order = service.get_order(case.order_id)
        self.assertEqual([(item.sku, item.quantity) for item in order.items], [("LAP-10", 1)])
        inventory = app.state.business_operations.get_inventory("LAP-10")
        with self.assertRaises(ConflictError):
            app.state.business_operations.assess_refund_eligibility(case.id, "LAP-1")
        other_order = service.create_order(OrderCreate(
            customer_id=CUSTOMER_ID,
            items=[{"sku": "LAP-10", "name": "NovaBook Laptop 10", "quantity": 1}],
        ))
        other_case = service.create_case(CaseCreate(
            customer_id=CUSTOMER_ID, order_id=other_order.id,
            subject="Synthetic mismatch", description="No trusted delivery fact.",
        ))
        with self.assertRaises(ConflictError):
            app.state.business_operations.assess_refund_eligibility(other_case.id, "LAP-10")

        identity = await self.request(app, "GET", "/api/identity", token=REVIEWER_TOKEN)
        started = await self.request(app, "POST", "/api/runs", {
            "instance_id": identity.json()["instance_id"], "request_id": str(uuid4()),
            "case_id": str(case.id), "message": "Synthetic LAP-10 refund review.",
            "resolution_intent": "refund", "refund_sku": "LAP-10",
        }, token=REVIEWER_TOKEN)
        self.assertEqual(started.status_code, 202, started.text)
        run_id = started.json()["run_id"]
        for _ in range(200):
            snapshot = await self.request(app, "GET", f"/api/runs/{run_id}",
                                          token=REVIEWER_TOKEN)
            if snapshot.json()["status"] != "RUNNING":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(snapshot.json()["status"], "REVIEW_REQUIRED", snapshot.text)
        self.assertFalse(snapshot.json()["actions_executed"])
        proposal = app.state.proposal_service.get(UUID(snapshot.json()["review"]["proposal_id"]))
        self.assertEqual(proposal.status, ProposalStatus.PENDING_REVIEW)
        evidence = app.state.business_operations.get_eligibility_evidence(
            proposal.eligibility_evidence_id)
        self.assertEqual((evidence.case_id, evidence.order_id, evidence.customer_id, evidence.sku),
                         (case.id, order.id, CUSTOMER_ID, "LAP-10"))
        self.assertEqual(evidence.delivery_source_id, "bedrock-demo-fulfillment")
        self.assertEqual(service.get_order(order.id), order)
        self.assertEqual(service.get_case(case.id), case)
        self.assertEqual(app.state.business_operations.get_inventory("LAP-10"), inventory)
        self.client.converse.assert_not_called()
        app.state.frontend_runtime.close()
        app.state.frontend_runtime.provider_factory().close()

    def test_bedrock_demo_flag_is_exact_and_provider_bound(self):
        for environment in (
            {**CONFIG, "NOVAMIND_BEDROCK_DEMO_DATA": "TRUE"},
            {"NOVAMIND_LLM_PROVIDER": "local_scripted", "NOVAMIND_OFFLINE_TESTING": "true",
             "NOVAMIND_BEDROCK_DEMO_DATA": "true"},
        ):
            with self.subTest(environment=tuple(environment)):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(
                            RuntimeError, "^Container Bedrock demo configuration is invalid$"):
                        create_container_app()

    async def test_identity_runs_and_diagnostics_report_bedrock(self):
        os.environ.update(CONFIG)
        directory = Path(self.enterContext(TemporaryDirectory()))
        auth = directory / "auth.json"
        auth.write_text(json.dumps([{"token": TOKEN, "identity": {
            "user_id": str(UUID(int=17)), "role": "ADMIN"}}]), encoding="utf-8")
        os.environ["NOVAMIND_AUTH_FILE"] = str(auth)
        app = create_container_app()
        runtime = app.state.frontend_runtime
        self.assertIsInstance(runtime.provider_factory(), BedrockProvider)
        async with app.router.lifespan_context(app):
            self.assertEqual((await self.request(app, "GET", "/api/identity", token=None)).status_code, 401)
            identity = await self.request(app, "GET", "/api/identity")
            self.assertEqual(identity.json()["provider"], "bedrock")
            service = app.state.case_service
            customer = service.create_customer(CustomerCreate(name="Bedrock fixture"))
            order = service.create_order(OrderCreate(customer_id=customer.id,
                items=[{"sku": "FIXTURE", "name": "Fixture", "quantity": 1}]))
            case = service.create_case(CaseCreate(customer_id=customer.id, order_id=order.id,
                subject="Fixture", description="Mocked Bedrock runtime"))
            body = {"instance_id": str(runtime.instance_id), "request_id": str(uuid4()),
                    "case_id": str(case.id), "message": "Review fixture"}
            self.assertEqual((await self.request(app, "POST", "/api/runs", body, token=None)).status_code, 401)
            started = await self.request(app, "POST", "/api/runs", body)
            self.assertEqual(started.status_code, 202, started.text)
            self.assertEqual(started.json()["provider"], "bedrock")
        snapshot = await self.request(app, "GET", f"/api/runs/{started.json()['run_id']}")
        self.assertEqual(snapshot.json()["provider"], "bedrock")
        self.assertEqual(snapshot.json()["status"], "COMPLETED", snapshot.text)
        runs = await self.request(app, "GET", f"/api/cases/{case.id}/runs?offset=0&limit=50")
        self.assertEqual(runs.json()["items"][0]["provider"], "bedrock")
        diagnostics = await self.request(app, "GET", "/api/diagnostics?limit=50")
        self.assertEqual(diagnostics.status_code, 200, diagnostics.text)
        self.assertTrue(any(event.get("provider") == "bedrock"
                            for event in diagnostics.json()["events"] if event["operation"] == "llm"))
        self.assertTrue(runtime._closed)
        self.client.close.assert_called_once()
        self.assertGreaterEqual(self.client.converse.call_count, 1)
        for call in self.client.converse.call_args_list:
            self.assertEqual(call.kwargs["modelId"], CONFIG["BEDROCK_MODEL_ID"])
            self.assertNotIn("toolConfig", call.kwargs)


if __name__ == "__main__":
    unittest.main()
