"""Offline structured-result coverage for the bounded Strands investigation path."""

import json
import sys
import time
import types
import unittest
from threading import Event
from unittest.mock import Mock, patch
from uuid import uuid4

from backend.app.agent import AgentConfig, AgentRequest
from backend.app.operations import BusinessOperations
from backend.app.proposals import ProposalService
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, authenticated
from backend.app.service import CaseService
from backend.app.strands_agent import (
    READ_ONLY_TOOLS,
    StrandsWorkflow,
    _BoundedModel,
    _ModelCallLimitExceeded,
)
from backend.app.tools import LocalTools


class _Result:
    def __init__(self, text):
        self.text = text
        self.metrics = types.SimpleNamespace(tool_count=0)

    def __str__(self):
        return self.text


class _Agent:
    result = _Result("not-json")

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __call__(self, prompt):
        if callable(self.result):
            return self.result(prompt)
        return self.result


class StrandsStructuredInvestigationTests(unittest.TestCase):
    def setUp(self):
        fake_strands = types.ModuleType("strands")
        fake_strands.Agent = _Agent
        fake_strands.tool = Mock(side_effect=lambda function, **kwargs: function)
        self.strands_patch = patch.dict(sys.modules, {"strands": fake_strands})
        self.strands_patch.start()
        self.addCleanup(self.strands_patch.stop)

        self.cases = CaseService()
        self.operations = BusinessOperations(self.cases)
        self.proposals = ProposalService(self.cases, self.operations)
        self.tools = LocalTools(self.cases, self.operations, self.proposals)
        customer = self.cases.create_customer(CustomerCreate(name="Structured Strands customer"))
        self.order = self.cases.create_order(OrderCreate(
            customer_id=customer.id,
            items=[{"sku": "STRANDS-1", "name": "Test device", "quantity": 1}],
        ))
        self.identity = AuthenticatedIdentity(user_id=uuid4(), role="ADMIN")
        self.auth = LocalAuthenticationProvider((LocalCredential(
            token="structured-strands-admin", identity=self.identity,
        ),))
        self.case = None

    def make_case(self, subject, description):
        self.case = self.cases.create_case(CaseCreate(
            customer_id=self.order.customer_id, order_id=self.order.id,
            subject=subject, description=description,
        ))
        return self.case

    def workflow(self, **kwargs):
        model_factory = kwargs.pop("model_factory", lambda: Mock())
        return StrandsWorkflow(
            self.tools, model_factory=model_factory,
            config=kwargs.pop("config", AgentConfig(max_steps=4)), **kwargs,
        )

    def run_workflow(self, message, **kwargs):
        workflow = self.workflow(**kwargs)
        with authenticated(self.auth, "structured-strands-admin"):
            return workflow.start(AgentRequest(case_id=self.case.id, message=message))

    def test_all_seven_issue_categories_produce_valid_deterministic_results(self):
        cases = {
            "damaged": ("Damaged item", "The item arrived damaged.", "It was cracked on arrival."),
            "wrong_item": ("Wrong item", "A different item was delivered.", "I received the wrong product."),
            "missing_item": ("Missing item", "An item is missing from the package.", "The part is not in the box."),
            "delayed_delivery": ("Delayed delivery", "The delivery is late.", "The shipment is overdue."),
            "malfunction": ("Product malfunction", "The device stopped working.", "It is not responding."),
            "return": ("Return request", "I want to return the product.", "Please send it back."),
            "refund_not_received": ("Refund not received", "The refund has not arrived.", "My payment is missing."),
        }
        for expected, (subject, description, message) in cases.items():
            with self.subTest(issue=expected):
                self.make_case(subject, description)
                _Agent.result = _Result("not-json")
                result = self.run_workflow(message)
                self.assertEqual(result.status, "COMPLETED")
                self.assertEqual(result.agent_result.resolution_draft.issue_type.value, expected)
                self.assertFalse(result.agent_result.actions_executed)
                self.assertFalse(result.agent_result.resolution_draft.actions_executed)
                self.assertTrue(result.agent_result.investigation_plan.allowed_tools)
                self.assertTrue(set(result.agent_result.investigation_plan.allowed_tools) <= READ_ONLY_TOOLS)

    def test_invalid_model_output_falls_back_to_deterministic_grounding(self):
        self.make_case("Return request", "I want to return the product.")
        _Agent.result = _Result(json.dumps({"grounded_response": {"issue_type": "damaged"}}))
        result = self.run_workflow("Please return it")
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.agent_result.resolution_draft.issue_type.value, "return")
        self.assertEqual(result.agent_result.resolution_draft.response_source.value, "DETERMINISTIC_FALLBACK")
        self.assertEqual(result.investigation_outcome.response_source.value, "DETERMINISTIC_FALLBACK")
        self.assertEqual(result.investigation_outcome.validation_result, "DETERMINISTIC_FALLBACK")
        self.assertEqual(result.investigation_outcome.fallback_reason.value, "MODEL_OUTPUT_INVALID")
        self.assertEqual(result.investigation_outcome.provider, "bedrock")
        self.assertEqual(result.investigation_outcome.evidence_references,
                         result.agent_result.resolution_draft.verified_facts)
        self.assertEqual(result.investigation_outcome.missing_evidence,
                         result.agent_result.resolution_draft.missing_evidence)

    def test_validated_bedrock_output_has_explicit_outcome_attribution(self):
        self.make_case("Return request", "I want to return the product.")

        def validated(prompt):
            context = json.loads(prompt)
            draft = context["grounded_resolution_draft"]
            grounding = context["grounding"]
            return _Result(json.dumps({"grounded_response": {
                "issue_type": draft["issue_type"],
                "recommended_decision": draft["recommended_decision"],
                "verified_facts": draft["verified_facts"],
                "missing_evidence": draft["missing_evidence"],
                "policy_passages": grounding["policy_passages"],
                "decision_reason": "Verified records still require the requested evidence for support review.",
                "customer_facing_response": (
                    "Please send the requested evidence so support can review your report. "
                    "No refund, replacement, return, or payment action has been executed."
                ),
            }}))

        _Agent.result = staticmethod(validated)
        result = self.run_workflow(
            "Please return it", model_factory=lambda: types.SimpleNamespace(
                model_id="amazon.nova-lite-v1:0",
            ),
        )
        self.assertEqual(result.status, "COMPLETED", result.error)
        outcome = result.investigation_outcome
        self.assertEqual(result.agent_result.resolution_draft.response_source.value, "BEDROCK_VALIDATED")
        self.assertEqual(outcome.response_source.value, "BEDROCK_VALIDATED")
        self.assertEqual(outcome.validation_result, "VALIDATED")
        self.assertIsNone(outcome.fallback_reason)
        self.assertEqual(outcome.provider, "bedrock")
        self.assertEqual(outcome.model_id, "amazon.nova-lite-v1:0")
        self.assertEqual(outcome.evidence_references,
                         result.agent_result.resolution_draft.verified_facts)
        self.assertTrue(outcome.citations)
        self.assertFalse(outcome.actions_executed)

    def test_model_timeout_is_a_safe_closed_error(self):
        self.make_case("Return request", "I want to return the product.")

        original = _Agent.__call__
        try:
            _Agent.__call__ = lambda self, prompt: (time.sleep(0.15), _Result("late"))[1]
            result = self.run_workflow("Please return it", model_timeout_seconds=0.02)
        finally:
            _Agent.__call__ = original
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error, "TIMEOUT")
        self.assertNotIn("late", result.message)

    def test_model_timeout_signals_cancellation_and_discards_late_result(self):
        started, release = Event(), Event()

        def late_model_call():
            started.set()
            release.wait(timeout=1)
            return "late-model-result"

        cancellation = Event()
        with self.assertRaises(TimeoutError):
            StrandsWorkflow._bounded_call(
                late_model_call, timeout_seconds=0.02, cancellation=cancellation,
            )
        self.assertTrue(started.is_set())
        self.assertTrue(cancellation.is_set())
        release.set()

    def test_model_proxy_allows_three_provider_calls_and_fails_before_fourth(self):
        provider = Mock()
        bounded = _BoundedModel(provider)

        for _ in range(3):
            bounded.stream([], tool_specs=[])
        with self.assertRaises(_ModelCallLimitExceeded):
            bounded.stream([], tool_specs=[])

        self.assertEqual(provider.stream.call_count, 3)
        self.assertEqual(bounded.used, 3)

        structured = _BoundedModel(provider)
        for _ in range(3):
            structured.structured_output(object, [])
        with self.assertRaises(_ModelCallLimitExceeded):
            structured.structured_output(object, [])
        self.assertEqual(provider.structured_output.call_count, 3)

    def test_workflow_fails_closed_when_agent_requests_a_fourth_model_call(self):
        self.make_case("Return request", "I want to return the product.")
        provider = Mock()

        class _FourCallAgent:
            def __init__(self, **kwargs):
                self.model = kwargs["model"]

            def __call__(self, prompt):
                for _ in range(4):
                    self.model.stream([], tool_specs=[])

        strands_module = sys.modules["strands"]
        with patch.object(strands_module, "Agent", _FourCallAgent):
            workflow = StrandsWorkflow(self.tools, model_factory=lambda: provider)
            with authenticated(self.auth, "structured-strands-admin"):
                result = workflow.start(AgentRequest(case_id=self.case.id, message="Please return it"))

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error, "STEP_LIMIT")
        self.assertEqual(provider.stream.call_count, 3)
        self.assertFalse(result.agent_result)

    def test_cancelled_read_only_tool_is_not_invoked(self):
        self.make_case("Return request", "I want to return the product.")
        workflow = self.workflow()
        cancellation = Event()
        cancellation.set()
        invoked = Mock(side_effect=AssertionError("cancelled tool invoked"))
        workflow._tools.invoke = invoked
        with authenticated(self.auth, "structured-strands-admin"):
            tool = next(tool for tool in workflow._make_strands_tools(cancellation=cancellation)
                        if getattr(tool, "__name__", "") == "get_case")
            result = tool(case_id=str(self.case.id))
        self.assertEqual(result["error"], "TIMEOUT")
        invoked.assert_not_called()

    def test_tool_budget_is_shared_and_mutable_bindings_are_not_exposed(self):
        self.make_case("Return request", "I want to return the product.")
        workflow = self.workflow(config=AgentConfig(max_steps=1))
        with authenticated(self.auth, "structured-strands-admin"):
            tools = workflow._make_strands_tools()
            by_name = {
                getattr(tool, "tool_name", getattr(tool, "__name__", "")): tool
                for tool in tools
            }
            first = by_name["get_case"](case_id=str(self.case.id))
            second = by_name["get_order"](order_id=str(self.order.id))
        self.assertEqual(first["id"], str(self.case.id))
        self.assertEqual(second["error"], "STEP_LIMIT")
        self.assertNotIn("create_resolution_proposal", by_name)
        self.assertNotIn("assess_refund_eligibility", by_name)

    def test_read_only_tool_timeout_returns_safe_error(self):
        self.make_case("Return request", "I want to return the product.")
        original = self.tools.invoke

        def slow_invoke(*args, **kwargs):
            time.sleep(0.15)
            return original(*args, **kwargs)

        self.tools.invoke = slow_invoke
        workflow = self.workflow(tool_timeout_seconds=0.02)
        with authenticated(self.auth, "structured-strands-admin"):
            tool = next(tool for tool in workflow._make_strands_tools()
                        if getattr(tool, "__name__", "") == "get_case")
            result = tool(case_id=str(self.case.id))
        self.assertEqual(result["error"], "TIMEOUT")

    def test_policy_override_cannot_change_application_owned_plan_or_draft(self):
        self.make_case("Refund not received", "The refund has not arrived.")
        _Agent.result = _Result(json.dumps({
            "grounded_response": {
                "issue_type": "return", "recommended_decision": "REQUEST_EVIDENCE",
                "verified_facts": ["fabricated"], "missing_evidence": ["none"],
                "policy_passages": [], "decision_reason": "approved",
                "customer_facing_response": "Refund has been issued.",
            }
        }))
        result = self.run_workflow("My refund is missing")
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.agent_result.resolution_draft.issue_type.value, "refund_not_received")
        self.assertNotIn("approved", result.agent_result.resolution_draft.decision_reason.casefold())
        self.assertFalse(result.agent_result.actions_executed)

    def test_no_resume_or_action_execution_path(self):
        self.make_case("Return request", "I want to return the product.")
        workflow = self.workflow()
        self.assertEqual(workflow.resume(Mock()).error, "NOT_PAUSED")
        with authenticated(self.auth, "structured-strands-admin"):
            result = workflow.start(AgentRequest(case_id=self.case.id, message="Please return it"))
        self.assertEqual(result.agent_result.pending_proposal_ids, ())
        self.assertFalse(result.agent_result.actions_executed)


if __name__ == "__main__":
    unittest.main()
