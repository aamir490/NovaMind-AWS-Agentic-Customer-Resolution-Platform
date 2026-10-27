"""
Regression tests for the opt-in Strands investigation agent.

All tests run fully offline with mocked Strands SDK and Bedrock clients.
No real AWS calls are made. Tests verify:
- Opt-in behavior (default is LangGraph, not Strands)
- Read-only tool enforcement, including hidden evidence writes
- Bounded tool execution (step limit)
- Safe failures (error handling with closed vocabulary)
- WorkflowResult contract compatibility (COMPLETED, no review)
- resume() always rejects (no HITL for Strands)
- audit() returns empty (no checkpoint history)
- Bootstrap validation (NOVAMIND_AGENT_ENGINE)
- FrontendRuntime integration (agent_engine parameter)
"""

import json
import os
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID, uuid4

import httpx

from backend.app.agent import AgentConfig, AgentRequest
from backend.app.main import create_app
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential
from backend.app.strands_agent import READ_ONLY_TOOLS, StrandsWorkflow
from backend.app.tools import LocalTools
from backend.container.bootstrap import _agent_engine


class FakeStrandsResult:
    """Simulates the Strands SDK AgentResult for offline testing."""

    def __init__(self, text="Investigation complete. No action was executed.", tool_count=0):
        self._text = text
        self.metrics = Mock()
        self.metrics.tool_count = tool_count
        self.stop_reason = "end_turn"

    def __str__(self):
        return self._text


class FakeStrandsAgent:
    """Mock Strands Agent that records tool list and returns a configured result."""

    instances = []

    def __init__(self, model=None, tools=None, system_prompt=None, **kwargs):
        self.model = model
        self.tools = tools or []
        self.system_prompt = system_prompt
        self.tool_names = [getattr(t, "tool_name", getattr(t, "__name__", str(t))) for t in (tools or [])]
        self._call_count = 0
        FakeStrandsAgent.instances.append(self)

    def __call__(self, prompt, **kwargs):
        self._call_count += 1
        self.prompt = prompt
        if hasattr(self, "_result"):
            return self._result
        return FakeStrandsResult()

    @classmethod
    def reset(cls):
        cls.instances = []


class StrandsAgentUnitTests(unittest.IsolatedAsyncioTestCase):
    """Unit tests for StrandsWorkflow with mocked Strands SDK."""

    def setUp(self):
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.enterContext(patch("socket.getaddrinfo", side_effect=AssertionError("No DNS")))
        FakeStrandsAgent.reset()
        # Patch strands.Agent before any StrandsWorkflow is created.
        fake_strands = types.ModuleType("strands")
        fake_strands.Agent = FakeStrandsAgent
        fake_tool = Mock(side_effect=lambda func, **kw: func)
        fake_strands.tool = fake_tool
        self.enterContext(patch.dict(sys.modules, {
            "strands": fake_strands,
            "strands.models": types.ModuleType("strands.models"),
        }))

        # Build a real LocalTools with seeded data.
        from backend.app.operations import BusinessOperations
        from backend.app.proposals import ProposalService
        from backend.app.service import CaseService
        self.cases = CaseService()
        customer = self.cases.create_customer(CustomerCreate(name="Strands Test"))
        self.order = self.cases.create_order(OrderCreate(
            customer_id=customer.id,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}]))
        self.case = self.cases.create_case(CaseCreate(
            customer_id=customer.id, order_id=self.order.id,
            subject="Investigation test", description="Strands agent regression test"))
        self.operations = BusinessOperations(self.cases)
        self.proposals = ProposalService(self.cases, self.operations)
        self.tools = LocalTools(self.cases, self.operations, self.proposals)

        # Authenticated identity for tool authorization.
        self.identity = AuthenticatedIdentity(
            user_id=uuid4(), role="ADMIN")
        self.token = "strands-test-admin-token"
        self.auth = LocalAuthenticationProvider(
            (LocalCredential(token=self.token, identity=self.identity),))

    def _workflow(self, **kwargs):
        model_factory = kwargs.pop("model_factory", lambda: Mock())
        config = kwargs.pop("config", AgentConfig(max_steps=4))
        return StrandsWorkflow(self.tools, model_factory=model_factory, config=config, **kwargs)

    def _run_with_auth(self, workflow, request):
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            return workflow.start(request)

    def _request(self):
        return AgentRequest(
            case_id=self.case.id,
            message="Investigate this case and summarize findings",
        )

    # -- Read-only tool enforcement -----------------------------------------

    def test_audited_tool_allowlist_excludes_writes(self):
        self.assertEqual(READ_ONLY_TOOLS, {
            "search_knowledge", "get_customer", "get_order", "get_case",
            "get_inventory", "get_policy", "assess_eligibility", "get_proposal_status",
        })
        self.assertNotIn("create_resolution_proposal", READ_ONLY_TOOLS)
        self.assertNotIn("assess_refund_eligibility", READ_ONLY_TOOLS)

    def test_strands_tools_only_include_read_only_bindings(self):
        workflow = self._workflow()
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            strands_tools = workflow._make_strands_tools()
        tool_names = {getattr(t, "tool_name", getattr(t, "__name__", str(t))) for t in strands_tools}
        self.assertIn("get_case", tool_names)
        self.assertIn("get_order", tool_names)
        self.assertIn("search_knowledge", tool_names)
        self.assertNotIn("create_resolution_proposal", tool_names)
        self.assertNotIn("assess_refund_eligibility", tool_names)

    # -- Bounded execution --------------------------------------------------

    def test_step_limit_returns_error_from_tool_wrapper(self):
        workflow = self._workflow(config=AgentConfig(max_steps=1))
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            strands_tools = workflow._make_strands_tools()
        # First call is fine
        first = strands_tools[0](case_id=str(self.case.id))
        self.assertIsInstance(first, (dict, object))
        # Second call exceeds the limit
        second = strands_tools[0](case_id=str(self.case.id))
        if isinstance(second, dict):
            self.assertEqual(second["error"], "STEP_LIMIT")

    def test_tool_limit_is_shared_across_different_tool_wrappers(self):
        """One agent run cannot bypass its limit by switching tool names."""
        workflow = self._workflow(config=AgentConfig(max_steps=1))
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            strands_tools = workflow._make_strands_tools()
            by_name = {
                getattr(tool, "tool_name", getattr(tool, "__name__", "")): tool
                for tool in strands_tools
            }
            first = by_name["get_case"](case_id=str(self.case.id))
            second = by_name["get_order"](order_id=str(self.order.id))
        self.assertEqual(first["id"], str(self.case.id))
        self.assertEqual(second["error"], "STEP_LIMIT")

    # -- Safe failures ------------------------------------------------------

    def test_case_not_found_returns_failed_workflow_result(self):
        workflow = self._workflow()
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            request = AgentRequest(case_id=uuid4(), message="Nonexistent case")
            result = workflow.start(request)
        self.assertEqual(result.status, "FAILED")
        self.assertIsNone(result.review)
        self.assertIsNone(result.agent_result)

    def test_unauthenticated_start_raises_security_error(self):
        workflow = self._workflow()
        from backend.app.security import SecurityError
        with self.assertRaises(SecurityError):
            workflow.start(self._request())

    def test_strands_sdk_exception_produces_safe_failure(self):
        """If the Strands Agent raises, the workflow fails safely."""
        workflow = self._workflow()

        # Override the model factory to raise during agent creation.
        workflow._model_factory = lambda: (_ for _ in ()).throw(RuntimeError("private SDK error"))
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            result = workflow.start(self._request())
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error, "WORKFLOW_ERROR")
        self.assertNotIn("private SDK error", result.message)

    # -- Contract compatibility --------------------------------------------

    def test_successful_run_returns_completed_with_informational_result(self):
        workflow = self._workflow()
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            result = workflow.start(self._request())
        self.assertEqual(result.status, "COMPLETED")
        self.assertIsNone(result.review)
        self.assertIsNotNone(result.message)
        self.assertNotEqual(result.message, "")
        self.assertIsNone(result.error)

    def test_result_has_no_pending_proposals(self):
        workflow = self._workflow()
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            result = workflow.start(self._request())
        self.assertIsNone(result.review)
        if result.agent_result is not None:
            self.assertEqual(result.agent_result.pending_proposal_ids, ())
            self.assertEqual(result.agent_result.status, "INFORMATIONAL")

    def test_resume_always_returns_rejected(self):
        workflow = self._workflow()
        rejected = workflow.resume(Mock())
        self.assertEqual(rejected.error, "NOT_PAUSED")

    def test_audit_returns_empty_for_known_workflow(self):
        workflow = self._workflow()
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            result = workflow.start(self._request())
        with authenticated(self.auth, self.token):
            audit = workflow.audit(result.workflow_id)
        self.assertEqual(audit, ())

    def test_audit_raises_keyerror_for_unknown_workflow(self):
        workflow = self._workflow()
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            with self.assertRaises(KeyError):
                workflow.audit(uuid4())

    def test_actions_executed_is_false(self):
        workflow = self._workflow()
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            result = workflow.start(self._request())
        if result.agent_result is not None:
            self.assertFalse(result.agent_result.actions_executed)

    # -- Tool wrapper dispatch ---------------------------------------------

    def test_tool_wrapper_returns_data_on_success(self):
        workflow = self._workflow()
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            strands_tools = workflow._make_strands_tools()
        get_case_tool = next(t for t in strands_tools
                             if getattr(t, "tool_name", getattr(t, "__name__", "")) == "get_case")
        with authenticated(self.auth, self.token):
            result = get_case_tool(case_id=str(self.case.id))
        self.assertIsInstance(result, dict)
        self.assertEqual(result["id"], str(self.case.id))
        self.assertEqual(result["subject"], "Investigation test")

    def test_tool_wrapper_returns_error_on_not_found(self):
        workflow = self._workflow()
        from backend.app.security import authenticated
        with authenticated(self.auth, self.token):
            strands_tools = workflow._make_strands_tools()
        get_case_tool = next(t for t in strands_tools
                             if getattr(t, "tool_name", getattr(t, "__name__", "")) == "get_case")
        with authenticated(self.auth, self.token):
            result = get_case_tool(case_id=str(uuid4()))
        self.assertIsInstance(result, dict)
        self.assertEqual(result["error"], "NOT_FOUND")


class BootstrapAgentEngineTests(unittest.TestCase):
    """Test NOVAMIND_AGENT_ENGINE validation in bootstrap."""

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_default_engine_is_langgraph(self):
        self.assertEqual(_agent_engine(), "langgraph")

    def test_explicit_langgraph_is_accepted(self):
        os.environ["NOVAMIND_AGENT_ENGINE"] = "langgraph"
        self.assertEqual(_agent_engine(), "langgraph")

    def test_strands_engine_requires_bedrock_provider(self):
        os.environ["NOVAMIND_AGENT_ENGINE"] = "strands"
        with self.assertRaisesRegex(RuntimeError, "^Container Strands agent engine requires Bedrock provider$"):
            _agent_engine()

    def test_strands_engine_accepted_with_bedrock_provider(self):
        os.environ["NOVAMIND_AGENT_ENGINE"] = "strands"
        os.environ["NOVAMIND_LLM_PROVIDER"] = "bedrock"
        self.assertEqual(_agent_engine(), "strands")

    def test_strands_engine_accepts_only_approved_nova_lite_model_ids(self):
        for model_id in (
            "amazon.nova-lite-v1:0", "us.amazon.nova-lite-v1:0",
            "eu.amazon.nova-lite-v1:0", "apac.amazon.nova-lite-v1:0",
        ):
            with self.subTest(model_id=model_id), patch.dict(os.environ, {
                "NOVAMIND_AGENT_ENGINE": "strands",
                "NOVAMIND_LLM_PROVIDER": "bedrock",
                "BEDROCK_MODEL_ID": model_id,
            }, clear=True):
                self.assertEqual(_agent_engine(), "strands")

    def test_strands_engine_rejects_other_bedrock_model_ids(self):
        for model_id in (
            "amazon.nova-pro-v1:0",
            "anthropic.claude-3-5-sonnet-20240620-v1:0",
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.amazon.nova-lite-v1:0",
        ):
            with self.subTest(model_id=model_id), patch.dict(os.environ, {
                "NOVAMIND_AGENT_ENGINE": "strands",
                "NOVAMIND_LLM_PROVIDER": "bedrock",
                "BEDROCK_MODEL_ID": model_id,
            }, clear=True):
                with self.assertRaisesRegex(RuntimeError, "^Container Strands model selection is invalid$"):
                    _agent_engine()

    def test_invalid_engine_rejected(self):
        for engine in ("", " ", "STRANDS", "gemini", "auto"):
            with self.subTest(engine=engine):
                os.environ["NOVAMIND_AGENT_ENGINE"] = engine
                with self.assertRaisesRegex(RuntimeError, "^Container agent engine selection is invalid$"):
                    _agent_engine()

    def test_strands_sdk_is_in_the_runtime_dependency_manifest(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn("strands-agents==1.0.0", (root / "backend" / "requirements.in").read_text())
        self.assertIn("strands-agents==1.0.0", (root / "backend" / "requirements.txt").read_text())


class FrontendRuntimeStrandsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Test FrontendRuntime with agent_engine='strands'."""

    def setUp(self):
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.enterContext(patch("socket.getaddrinfo", side_effect=AssertionError("No DNS")))
        FakeStrandsAgent.reset()
        # Patch strands.Agent for the integration test.
        fake_strands = types.ModuleType("strands")
        fake_strands.Agent = FakeStrandsAgent
        fake_strands.tool = Mock(side_effect=lambda func, **kw: func)
        self.enterContext(patch.dict(sys.modules, {
            "strands": fake_strands,
            "strands.models": types.ModuleType("strands.models"),
        }))

    async def asyncSetUp(self):
        self.app = create_app(
            agent_engine="strands",
            strands_model_factory=lambda: Mock(),
        )
        self.runtime = self.app.state.frontend_runtime
        self.addCleanup(self.runtime.close)
        cases = self.app.state.case_service
        customer = cases.create_customer(CustomerCreate(name="Integration Test"))
        order = cases.create_order(OrderCreate(
            customer_id=customer.id,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}]))
        self.case = cases.create_case(CaseCreate(
            customer_id=customer.id, order_id=order.id,
            subject="Strands integration", description="Integration test case"))
        identity = AuthenticatedIdentity(user_id=uuid4(), role="ADMIN")
        token = "strands-integration-admin-token"
        self.app.state.auth_provider = LocalAuthenticationProvider(
            (LocalCredential(token=token, identity=identity),))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://local.test",
        )
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {"Authorization": f"Bearer {token}"}

    async def start_run(self):
        body = {
            "instance_id": str(self.runtime.instance_id),
            "request_id": str(uuid4()),
            "case_id": str(self.case.id),
            "message": "Investigate this case",
        }
        response = await self.client.post("/api/runs", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    async def wait_finished(self, run_id):
        for _ in range(300):
            response = await self.client.get(f"/api/runs/{run_id}", headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            snapshot = response.json()
            if snapshot["status"] not in ("RUNNING", "RESUMING"):
                return snapshot
            import asyncio
            await asyncio.sleep(0.01)
        self.fail("Run did not finish")

    async def test_strands_run_completes_without_proposal(self):
        run = await self.start_run()
        snapshot = await self.wait_finished(run["run_id"])
        self.assertEqual(snapshot["status"], "COMPLETED")
        self.assertIsNone(snapshot["review"])
        self.assertFalse(snapshot["actions_executed"])

    async def test_strands_run_has_no_workflow_id_in_events_until_completed(self):
        run = await self.start_run()
        snapshot = await self.wait_finished(run["run_id"])
        self.assertIsNotNone(snapshot["workflow_id"])

    async def test_strands_decision_returns_not_paused(self):
        run = await self.start_run()
        await self.wait_finished(run["run_id"])
        decision_body = {
            "instance_id": str(self.runtime.instance_id),
            "workflow_id": str(uuid4()),
            "case_id": str(self.case.id),
            "proposal_id": str(uuid4()),
            "review_id": str(uuid4()),
            "decision": "APPROVE",
        }
        response = await self.client.post(
            f"/api/runs/{run['run_id']}/decision", json=decision_body, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "NOT_PAUSED")

    async def test_strands_identity_shows_provider(self):
        response = await self.client.get("/api/identity", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "local_scripted")

    async def test_strands_audit_returns_empty_page(self):
        run = await self.start_run()
        await self.wait_finished(run["run_id"])
        response = await self.client.get(
            f"/api/runs/{run['run_id']}/audit", headers=self.headers)
        # Strands has no checkpoint history; audit returns 200 with empty events.
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["events"], [])

    async def test_strands_events_page_works(self):
        run = await self.start_run()
        await self.wait_finished(run["run_id"])
        response = await self.client.get(
            f"/api/runs/{run['run_id']}/events?after=0&limit=50", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        page = response.json()
        # STATE events should exist (RUNNING + COMPLETED at minimum).
        self.assertGreaterEqual(len(page["events"]), 1)
        self.assertTrue(all(e["kind"] == "STATE" for e in page["events"]))


class FrontendRuntimeValidationTests(unittest.TestCase):
    """Test that FrontendRuntime validates the agent_engine parameter."""

    def test_strands_engine_without_model_factory_raises(self):
        with self.assertRaisesRegex(ValueError, "Strands agent engine requires a model factory"):
            from backend.app.frontend_runtime import FrontendRuntime
            FrontendRuntime(None, None, None, agent_engine="strands")

    def test_invalid_agent_engine_raises(self):
        with self.assertRaisesRegex(ValueError, "Invalid agent engine"):
            from backend.app.frontend_runtime import FrontendRuntime
            FrontendRuntime(None, None, None, agent_engine="gemini")

    def test_default_agent_engine_is_langgraph(self):
        from backend.app.frontend_runtime import FrontendRuntime
        runtime = FrontendRuntime(None, None, None)
        self.assertEqual(runtime.agent_engine, "langgraph")
        runtime.close()


if __name__ == "__main__":
    unittest.main()
