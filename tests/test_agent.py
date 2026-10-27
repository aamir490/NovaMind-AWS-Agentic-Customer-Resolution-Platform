"""Focused Phase 8 orchestration tests; no network or real model calls."""

import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from backend.app.agent import AgentConfig, AgentDecision, AgentRequest, AgentResult, ResolutionAgent
from backend.app.conversations import MEMORY_INSTRUCTIONS
from backend.app.guardrails import SAFETY_INSTRUCTIONS
from backend.app.llm import LLMResponse, ProviderFailure, StructuredLLM
from backend.app.main import create_app
from backend.app.operations import TrustedDeliverySource
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from security_fixtures import admin_scope


def call(name, **arguments):
    return {"decision": {"kind": "tool", "name": name, "arguments": arguments}}


FINAL = {"decision": {"kind": "final"}}


class ScriptedProvider:
    def __init__(self, script):
        self.script = iter(script)
        self.requests = []
        self.schemas = []

    def generate(self, request, *, response_schema):
        self.requests.append(request)
        self.schemas.append(response_schema)
        value = next(self.script)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(request)
        return LLMResponse(text=value if isinstance(value, str) else json.dumps(value), finish_reason="stop")


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(admin_scope())
        self.delivery_source = TrustedDeliverySource(id="test-fulfillment")
        self.app = create_app(trusted_delivery_sources=(self.delivery_source,))
        self.cases = self.app.state.case_service
        self.tools = self.app.state.local_tools
        self.delivery_ingestion = self.app.state.delivery_fact_ingestion
        self.proposals = self.app.state.proposal_service
        customer = self.cases.create_customer(CustomerCreate(name="Demo"))
        self.order = self.cases.create_order(OrderCreate(customer_id=customer.id, items=[
            {"sku": "LAP-1", "name": "Laptop", "quantity": 1},
        ]))
        self.case = self.cases.create_case(CaseCreate(customer_id=customer.id, order_id=self.order.id,
                                                    subject="Wrong item", description="Please review"))
        self.request = AgentRequest(case_id=self.case.id, message="Please review my case")

    def run_script(self, script, max_steps=8):
        self.provider = ScriptedProvider(script)
        self.agent = ResolutionAgent(StructuredLLM(self.provider), self.tools, AgentConfig(max_steps=max_steps))
        return self.agent.run(self.request)

    def proposal(self, **overrides):
        return call("create_resolution_proposal", **{
            "case_id": str(self.case.id), "action": "replacement", "rationale": "For human review", **overrides,
        })

    def test_lookup_loop_passes_results_back_and_keeps_ordered_audit(self):
        result = self.run_script([call("get_order", order_id=str(self.order.id)),
                                  call("get_inventory", sku="LAP-1"), FINAL])
        self.assertEqual(result.status, "INFORMATIONAL")
        self.assertEqual(result.steps_used, 3)
        self.assertEqual([event.step for event in result.history], [0, 1, 2, 3])
        observed = json.loads(self.provider.requests[1].messages[-1].content)["tool_result"]
        self.assertEqual(observed["data"]["id"], str(self.order.id))
        self.assertEqual(self.provider.requests[1].messages[-2].role, "assistant")
        self.assertEqual(AgentResult.model_validate_json(result.model_dump_json()), result)
        self.assertFalse(result.actions_executed)

    def test_decision_schema_instructions_are_explicit_and_retained_between_steps(self):
        result = self.run_script([call("get_inventory", sku="LAP-1"), FINAL])
        self.assertEqual(result.status, "INFORMATIONAL")
        self.assertFalse(result.actions_executed)
        self.assertEqual(len(self.provider.requests), 2)
        instructions = self.provider.requests[0].messages[0].content
        for request in self.provider.requests:
            self.assertEqual(request.messages[0].role, "system")
            self.assertEqual(request.messages[0].content, instructions)
        for text in ("Every response must be a JSON object matching the provided schema",
                     "top-level decision object", 'decision.kind must be exactly "tool" for a tool call or "final" for completion',
                     "Tool decisions require name", "arguments (a JSON object", '{"decision":{"kind":"final"}}',
                     "Do not use other kind values, extra fields, Markdown, or prose",
                     MEMORY_INSTRUCTIONS, SAFETY_INSTRUCTIONS, "Eligibility is not authorization",
                     "At most one proposal may be created per run", "only for the supplied case"):
            self.assertIn(text, instructions)
        expected_descriptions = self.tools.describe()
        investigation_plan = getattr(getattr(self, "state", None), "investigation_plan", None)
        if investigation_plan is not None:
            expected_descriptions = [
                item
                for item in expected_descriptions
                if item.name in investigation_plan.allowed_tools
            ]
        self.assertEqual(json.loads(instructions.split("Tools: ", 1)[1]),
                         [item.model_dump(mode="json") for item in expected_descriptions])
        self.assertEqual(self.provider.schemas, [AgentDecision.model_json_schema()] * 2)

    def test_decision_schema_remains_strict_without_normalizing_tags(self):
        invalid = [{"decision": {"kind": tag}} for tag in ("tool_call", "complete", "Final", " final ", 1, None)]
        invalid.extend(({"decision": {"name": "get_inventory", "arguments": {"sku": "LAP-1"}}},
                        {"decision": {"kind": "tool", "name": "get_inventory"}},
                        {"decision": {"kind": "tool", "arguments": {}}},
                        {"decision": {"kind": "tool", "name": 123, "arguments": {}}},
                        {"decision": {"kind": "tool", "name": "get_inventory", "arguments": []}},
                        {"decision": {"kind": "final", "message": "private-invalid-output"}},
                        {**FINAL, "message": "private-invalid-output"}))
        for value in invalid:
            with self.subTest(value=value), patch.object(self.tools, "invoke", wraps=self.tools.invoke) as invoke:
                with self.assertRaises(ValidationError):
                    AgentDecision.model_validate_json(json.dumps(value), strict=True)
                result = self.run_script([value])
                self.assertEqual((result.status, result.error), ("FAILED", "INVALID_RESPONSE"))
                self.assertEqual(len(self.provider.requests), 1)
                self.assertEqual(invoke.call_count, 1)  # Only the existing case preflight.
                self.assertEqual(result.history[-1].llm_failure.code, "INVALID_RESPONSE")
                self.assertFalse(result.actions_executed)
                self.assertNotIn("private-invalid-output", result.model_dump_json())
                self.assertEqual(self.proposals.list_for_case(self.case.id), [])

    def test_all_read_capabilities_delegate_including_denial_reasons(self):
        result = self.run_script([
            call("get_customer", customer_id=str(self.case.customer_id)),
            call("get_case", case_id=str(self.case.id)),
            call("get_policy", policy_id="standard-return"),
            call("assess_eligibility", order_id=str(self.order.id), customer_id=str(self.case.customer_id),
                 sku="LAP-1", quantity=1, days_since_delivery=31, reason="wrong_item", condition="unused"),
            FINAL,
        ])
        self.assertEqual(result.status, "INFORMATIONAL")
        self.assertEqual(result.history[-2].result.data.denial_reasons, ("outside_return_window",))
        self.assertFalse(result.history[-2].result.data.authorization_granted)

    def test_proposal_then_status_then_final_requires_review(self):
        def status(request):
            proposal = json.loads(request.messages[-1].content)["tool_result"]["data"]
            return call("get_proposal_status", proposal_id=proposal["id"])
        result = self.run_script([self.proposal(), status, FINAL])
        self.assertEqual(result.status, "HUMAN_REVIEW_REQUIRED")
        proposal = self.proposals.get(result.pending_proposal_ids[0])
        self.assertEqual(proposal.status, "PENDING_REVIEW")
        self.assertIsNone(proposal.reviewed_at)
        self.assertIn("human review", result.customer_response)
        self.assertEqual(self.cases.get_case(self.case.id), self.case)
        self.assertEqual(self.cases.get_order(self.order.id), self.order)
        self.assertEqual(self.app.state.business_operations.get_inventory("LAP-1").available_quantity, 5)

    def test_forbidden_names_never_reach_dispatcher(self):
        for name in ("approve", "reject", "create_refund", "execute_return", "create_replacement",
                     "update_inventory", "__import__", "proposal_service.review"):
            with self.subTest(name=name), patch.object(self.tools, "invoke", wraps=self.tools.invoke) as invoke:
                result = self.run_script([call(name)])
                self.assertEqual(result.error, "UNKNOWN_TOOL")
                self.assertEqual(invoke.call_count, 1)  # Case preflight only.
                self.assertEqual(len(self.provider.requests), 1)

    def test_malformed_decisions_never_reach_dispatcher(self):
        for value in ("not JSON", {"decision": {"kind": "tool", "name": "get_case"}},
                      {"decision": {"kind": "tool", "name": "get_case", "arguments": []}},
                      {"decision": {"kind": "final", "message": "Refund executed"}},
                      {"decision": {"kind": "execute"}}, {"decision": [FINAL]}):
            with self.subTest(value=value), patch.object(self.tools, "invoke", wraps=self.tools.invoke) as invoke:
                result = self.run_script([value])
                self.assertEqual(result.error, "INVALID_RESPONSE")
                self.assertEqual(invoke.call_count, 1)
                self.assertIsNotNone(result.history[-1].llm_failure)

    def test_tool_validation_rejects_injected_status_before_service_write(self):
        with patch.object(self.proposals, "create", wraps=self.proposals.create) as create:
            result = self.run_script([self.proposal(status="APPROVED")])
            self.assertEqual(result.error, "INVALID_INPUT")
            self.assertFalse(result.history[-1].result.ok)
            create.assert_not_called()
        self.assertEqual(self.proposals.list_for_case(self.case.id), [])

    def test_invalid_lookup_arguments_and_missing_records_stop(self):
        for arguments, code in (({"order_id": "bad"}, "INVALID_INPUT"),
                                ({"order_id": str(uuid4())}, "NOT_FOUND")):
            with self.subTest(code=code):
                self.assertEqual(self.run_script([call("get_order", **arguments)]).error, code)

    def test_case_mismatch_prevents_proposal_creation(self):
        result = self.run_script([self.proposal(case_id=str(uuid4()))])
        self.assertEqual(result.error, "CASE_MISMATCH")
        self.assertEqual(self.proposals.list_for_case(self.case.id), [])

    def test_second_proposal_is_rejected_without_retrying_write(self):
        result = self.run_script([self.proposal(), self.proposal()])
        self.assertEqual(result.error, "PROPOSAL_LIMIT")
        self.assertEqual(len(self.proposals.list_for_case(self.case.id)), 1)
        self.assertEqual(len(result.pending_proposal_ids), 1)
        self.assertIn("human review", result.customer_response)

    def test_step_limit_stops_without_extra_llm_or_tool_call(self):
        result = self.run_script([call("get_inventory", sku="LAP-1")] * 3, max_steps=2)
        self.assertEqual(result.error, "STEP_LIMIT")
        self.assertEqual(result.steps_used, 2)
        self.assertEqual(len(self.provider.requests), 2)
        self.assertEqual(len(result.history), 3)

    def test_limit_after_write_preserves_pending_record_and_history(self):
        result = self.run_script([self.proposal()], max_steps=1)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error, "STEP_LIMIT")
        self.assertEqual(len(result.pending_proposal_ids), 1)
        self.assertEqual(result.history[-1].result.data.status, "PENDING_REVIEW")

    def test_provider_failures_preserve_prior_results(self):
        for failure, code in ((ProviderFailure("TIMEOUT"), "TIMEOUT"), (RuntimeError("secret"), "LLM_ERROR")):
            with self.subTest(code=code):
                result = self.run_script([call("get_inventory", sku="LAP-1"), failure])
                self.assertEqual(result.error, code)
                self.assertEqual(result.history[1].result.data.sku, "LAP-1")
                self.assertNotIn("secret", result.model_dump_json())

    def test_unexpected_tool_failure_is_closed_and_audited(self):
        original = self.tools.invoke
        def invoke(name, arguments):
            if name == "get_inventory":
                raise RuntimeError("secret")
            return original(name, arguments)
        with patch.object(self.tools, "invoke", side_effect=invoke):
            result = self.run_script([call("get_inventory", sku="LAP-1")])
        self.assertEqual(result.error, "TOOL_ERROR")
        self.assertEqual(result.history[-1].decision.name, "get_inventory")
        self.assertNotIn("secret", result.model_dump_json())

    def test_missing_case_never_calls_llm(self):
        self.request = AgentRequest(case_id=uuid4(), message="Review")
        result = self.run_script([])
        self.assertEqual(result.error, "NOT_FOUND")
        self.assertEqual(self.provider.requests, [])

    def test_contracts_and_run_state_are_bounded_and_isolated(self):
        for value in (0, 33, True, "2"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AgentConfig(max_steps=value)
        with self.assertRaises(ValidationError):
            AgentRequest(case_id=self.case.id, message=" ")
        self.run_script([self.proposal(), FINAL, FINAL])
        second = self.agent.run(self.request)
        self.assertEqual(second.status, "INFORMATIONAL")
        self.assertEqual(second.pending_proposal_ids, ())
        self.assertEqual(len(second.history), 2)


if __name__ == "__main__":
    unittest.main()
