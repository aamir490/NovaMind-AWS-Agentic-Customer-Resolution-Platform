"""Focused offline coverage for bounded LangGraph investigation plans."""

import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.app.agent import AgentConfig, AgentRequest
from backend.app.domain import Customer
from backend.app.graph_agent import GraphResolutionAgent
from backend.app.hitl import HITLWorkflow
from backend.app.llm import FakeLLMProvider, LLMResponse, StructuredLLM
from backend.app.main import create_app
from backend.app.resolution_drafts import CustomerIntent, IssueType
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role, authenticated


class AdvancedReasoningTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.service = self.app.state.case_service
        self.customer = self.service.create_customer(CustomerCreate(name="Reasoning customer"))
        self.order = self.service.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))
        self.token = "advanced-reasoning-reviewer"
        self.auth = LocalAuthenticationProvider((LocalCredential(
            token=self.token,
            identity=AuthenticatedIdentity(user_id=uuid4(), role=Role.REVIEWER),
        ),))

    def support_case(self, subject, description):
        return self.service.create_case(CaseCreate(
            customer_id=self.customer.id, order_id=self.order.id,
            subject=subject, description=description,
        ))

    @staticmethod
    def final_provider():
        return FakeLLMProvider(LLMResponse(text='{"decision":{"kind":"final"}}', finish_reason="stop"))

    class RecordingProvider:
        def __init__(self, responses):
            self.requests = []
            self._responses = iter(responses)

        def generate(self, request, *, response_schema):
            self.requests.append(request)
            value = next(self._responses)
            return LLMResponse(text=json.dumps(value), finish_reason="stop")

    def run_agent(self, support_case, message, provider=None, config=None):
        agent = GraphResolutionAgent(StructuredLLM(provider or self.final_provider()),
                                     self.app.state.local_tools, config)
        with authenticated(self.auth, self.token):
            return agent.run(AgentRequest(case_id=support_case.id, message=message))

    def run_state(self, support_case, message, provider):
        agent = GraphResolutionAgent(StructuredLLM(provider), self.app.state.local_tools)
        with authenticated(self.auth, self.token):
            return agent.run_state(AgentRequest(case_id=support_case.id, message=message))

    def test_all_seven_issue_types_receive_structured_bounded_plans(self):
        examples = {
            IssueType.DAMAGED: ("Damaged product", "It arrived damaged.", "Please replace it."),
            IssueType.WRONG_ITEM: ("Wrong item", "I received a different item.", "Please investigate."),
            IssueType.MISSING_ITEM: ("Missing item", "An item is missing from the box.", "Please investigate."),
            IssueType.DELAYED_DELIVERY: ("Late delivery", "The delivery is overdue.", "Where is it?"),
            IssueType.MALFUNCTION: ("Malfunction", "The laptop stopped working.", "Please investigate."),
            IssueType.RETURN: ("Return request", "I want to return this item.", "Please help with a return."),
            IssueType.REFUND_NOT_RECEIVED: ("Refund missing", "My refund has not arrived.", "Please investigate."),
        }
        for issue, (subject, description, message) in examples.items():
            with self.subTest(issue=issue):
                result = self.run_agent(self.support_case(subject, description), message)
                plan = result.investigation_plan
                self.assertEqual(plan.issue_type, issue)
                self.assertGreaterEqual(plan.max_tool_calls, 1)
                self.assertLessEqual(plan.max_tool_calls, 32)
                self.assertTrue(plan.required_evidence)
                self.assertFalse(plan.actions_executed)
                self.assertNotIn("approve", plan.allowed_tools)
                self.assertNotIn("execute_refund", plan.allowed_tools)
        damaged = self.run_agent(self.support_case("Damaged", "The laptop arrived damaged."), "Please replace it.")
        self.assertEqual(damaged.investigation_plan.customer_intent, CustomerIntent.REQUEST_REPLACEMENT)
        delayed = self.run_agent(self.support_case("Late", "Delivery delayed."), "Where is it?")
        self.assertEqual(delayed.investigation_plan.customer_intent, CustomerIntent.TRACK_DELIVERY)

    def test_out_of_plan_tool_is_stopped_before_dispatch(self):
        support_case = self.support_case("Delivery delayed", "The shipment is late.")
        provider = FakeLLMProvider(LLMResponse(text=json.dumps({
            "decision": {"kind": "tool", "name": "get_inventory", "arguments": {"sku": "LAP-1"}},
        }), finish_reason="stop"))
        with patch.object(self.app.state.local_tools, "invoke", wraps=self.app.state.local_tools.invoke) as invoke:
            result = self.run_agent(support_case, "Where is my delivery?", provider)
        self.assertEqual(result.error, "UNKNOWN_TOOL")
        self.assertEqual([call.args[0] for call in invoke.call_args_list], ["get_case"])
        self.assertEqual(result.investigation_plan.issue_type, IssueType.DELAYED_DELIVERY)

    def test_model_cannot_replace_grounded_classification_or_plan(self):
        support_case = self.support_case("Damaged item", "The product arrived damaged.")
        provider = FakeLLMProvider(LLMResponse(text=json.dumps({
            "decision": {"kind": "final"},
            "investigation_plan": {
                "issue_type": "refund_not_received", "customer_intent": "request_refund",
                "allowed_tools": ["get_order", "assess_refund_eligibility"],
                "required_evidence": ["invented payment record"], "max_tool_calls": 1,
                "actions_executed": False,
            },
        }), finish_reason="stop"))
        result = self.run_agent(support_case, "Please replace it.", provider)
        self.assertEqual(result.investigation_plan.issue_type, IssueType.DAMAGED)
        self.assertEqual(result.investigation_plan.customer_intent, CustomerIntent.REQUEST_REPLACEMENT)
        self.assertNotIn("invented payment record", result.investigation_plan.required_evidence)

    def test_tool_schemas_exclude_unsupported_issue_reasons(self):
        scenarios = (
            ("Damaged item", "It arrived damaged.", "damaged", True),
            ("Wrong item", "I received a different item.", "wrong", True),
            ("Missing item", "An item is missing from the box.", "missing", False),
            ("Malfunction", "The laptop stopped working.", "malfunction", False),
        )
        for subject, description, message, allows_eligibility in scenarios:
            with self.subTest(subject=subject):
                provider = self.RecordingProvider([{"decision": {"kind": "final"}}])
                state = self.run_state(self.support_case(subject, description), message, provider)
                tools = json.loads(provider.requests[0].messages[0].content.split("Tools: ", 1)[1])
                names = {tool["name"] for tool in tools}
                self.assertEqual(names, set(state.investigation_plan.allowed_tools))
                self.assertEqual("assess_eligibility" in names, allows_eligibility)

    def test_novavault_malfunction_wording_uses_the_malfunction_plan(self):
        exact_failed_complaint = (
            "Replacement Request – NovaVault Storage 1 Not Detected",
            "I recently purchased NovaVault Storage 1.\n\n"
            "After connecting the device to my computer, the storage device is not detected. "
            "I cannot access or store my files.\n\n"
            "I tried reconnecting the device and using another USB port, but the issue remains unresolved.\n\n"
            "I would like to request a replacement because the product is not functioning properly.",
            "Please investigate my order and determine the next steps.",
        )
        paraphrases = (
            ("Device issue", "The storage device does not work after reconnecting it.", "Please investigate."),
            ("Device issue", "My product is unresponsive and fails to start.", "Please investigate."),
            ("Device issue", "The device cannot be detected by my computer.", "Please investigate."),
            ("Input issue", "NovaControl Input 1 stops responding during normal use.", "Please investigate."),
            ("Input issue", "NovaControl Input 1 has an intermittent connection.", "Please investigate."),
        )
        for subject, description, message in (exact_failed_complaint, *paraphrases):
            with self.subTest(description=description):
                provider = self.RecordingProvider([{"decision": {"kind": "final"}}])
                state = self.run_state(self.support_case(subject, description), message, provider)
                names = {
                    tool["name"]
                    for tool in json.loads(provider.requests[0].messages[0].content.split("Tools: ", 1)[1])
                }
                self.assertEqual(state.investigation_plan.issue_type, IssueType.MALFUNCTION)
                self.assertNotIn("get_policy", names)
                self.assertNotIn("assess_eligibility", names)
                self.assertNotIn("create_resolution_proposal", names)

    def test_unknown_support_text_uses_the_safe_technical_path_not_return(self):
        result = self.run_agent(
            self.support_case("NovaControl Input 1", "Please help with this unexpected input-device issue."),
            "I need technical support.",
        )
        self.assertEqual(result.investigation_plan.issue_type, IssueType.MALFUNCTION)
        self.assertEqual(result.investigation_plan.customer_intent, CustomerIntent.REPORT_ISSUE)
        self.assertEqual(result.resolution_draft.recommended_decision, "REQUEST_EVIDENCE")
        self.assertNotIn("create_resolution_proposal", result.investigation_plan.allowed_tools)
        self.assertEqual(result.pending_proposal_ids, ())
        self.assertFalse(result.actions_executed)

    def test_technical_plan_rejects_financial_and_fulfillment_proposals_before_dispatch(self):
        for action in ("refund", "return", "replacement"):
            with self.subTest(action=action):
                support_case = self.support_case(
                    "NovaControl Input 1 stops responding",
                    "The controller has an intermittent connection.",
                )
                provider = self.RecordingProvider((
                    {"decision": {"kind": "tool", "name": "create_resolution_proposal", "arguments": {
                        "case_id": str(support_case.id), "action": action, "rationale": "Model requested action.",
                    }}},
                ))
                with patch.object(self.app.state.local_tools, "invoke", wraps=self.app.state.local_tools.invoke) as invoke:
                    result = self.run_agent(support_case, "Please investigate the technical issue.", provider)
                self.assertEqual(result.error, "UNKNOWN_TOOL")
                self.assertEqual([call.args[0] for call in invoke.call_args_list], ["get_case"])
                self.assertEqual(self.app.state.proposal_service.list_for_case(support_case.id), [])
                self.assertFalse(result.actions_executed)

    def test_genuine_return_still_uses_the_return_plan(self):
        provider = self.RecordingProvider([{"decision": {"kind": "final"}}])
        state = self.run_state(
            self.support_case("Return request", "I want to return this unused item because I changed my mind."),
            "Please help me start a return.", provider,
        )
        names = {
            tool["name"]
            for tool in json.loads(provider.requests[0].messages[0].content.split("Tools: ", 1)[1])
        }
        self.assertEqual(state.investigation_plan.issue_type, IssueType.RETURN)
        self.assertIn("get_policy", names)
        self.assertNotIn("assess_eligibility", names)

    def test_proposal_case_id_is_bound_before_tool_validation_and_hitl(self):
        for supplied_case_id in ("hybrid", str(uuid4()), "not-a-uuid"):
            with self.subTest(supplied_case_id=supplied_case_id):
                support_case = self.support_case(
                    "Wrong item – NovaSound Audio 10",
                    "I received a different item than the one I ordered.",
                )
                hybrid_case_id = str(support_case.id)[:14] + str(self.order.id)[14:]
                model_case_id = hybrid_case_id if supplied_case_id == "hybrid" else supplied_case_id
                provider = self.RecordingProvider((
                    {"decision": {"kind": "tool", "name": "get_case", "arguments": {
                        "case_id": str(support_case.id),
                    }}},
                    {"decision": {"kind": "tool", "name": "get_order", "arguments": {
                        "order_id": str(self.order.id),
                    }}},
                    {"decision": {"kind": "tool", "name": "search_knowledge", "arguments": {
                        "query": "wrong item replacement policy", "top_k": 3,
                    }}},
                    {"decision": {"kind": "tool", "name": "create_resolution_proposal", "arguments": {
                        "case_id": model_case_id, "action": "replacement",
                        "rationale": "The wrong-item report requires staff review before any remedy.",
                    }}},
                ))
                workflow = HITLWorkflow(
                    StructuredLLM(provider), self.app.state.local_tools, self.app.state.proposal_service,
                )
                case_before = self.service.get_case(support_case.id)
                order_before = self.service.get_order(self.order.id)
                inventory_before = self.app.state.business_operations.get_inventory("LAP-1")
                with patch.object(self.app.state.local_tools, "invoke", wraps=self.app.state.local_tools.invoke) as invoke:
                    with authenticated(self.auth, self.token):
                        result = workflow.start(AgentRequest(
                            case_id=support_case.id, message="Please investigate the wrong item.",
                        ))
                        audit = workflow.audit(result.workflow_id)
                proposal = self.app.state.proposal_service.get(result.review.proposal_id)
                self.assertEqual(result.status, "REVIEW_REQUIRED")
                self.assertFalse(result.actions_executed)
                self.assertEqual(proposal.case_id, support_case.id)
                self.assertEqual(len(self.app.state.proposal_service.list_for_case(support_case.id)), 1)
                self.assertEqual(self.service.get_case(support_case.id), case_before)
                self.assertEqual(self.service.get_order(self.order.id), order_before)
                self.assertEqual(self.app.state.business_operations.get_inventory("LAP-1"), inventory_before)
                self.assertEqual([call.args[0] for call in invoke.call_args_list], [
                    "get_case", "get_case", "get_order", "search_knowledge", "create_resolution_proposal",
                ])
                self.assertEqual(audit[-2].agent_event.decision.arguments["case_id"], str(support_case.id))
                self.assertEqual([event.kind for event in audit], [
                    "AGENT", "AGENT", "AGENT", "AGENT", "AGENT", "REVIEW_REQUIRED",
                ])

    def test_supported_damaged_and_wrong_item_eligibility_calls_still_succeed(self):
        for subject, description, reason in (
            ("Damaged item", "It arrived damaged.", "damaged"),
            ("Wrong item", "I received a different item.", "wrong_item"),
        ):
            with self.subTest(reason=reason):
                support_case = self.support_case(subject, description)
                provider = self.RecordingProvider((
                    {"decision": {"kind": "tool", "name": "assess_eligibility", "arguments": {
                        "order_id": str(self.order.id), "case_id": str(support_case.id),
                        "customer_id": str(self.customer.id), "sku": "LAP-1", "quantity": 1,
                        "days_since_delivery": 1, "reason": reason, "condition": "damaged",
                    }}},
                    {"decision": {"kind": "final"}},
                ))
                result = self.run_agent(support_case, "Please investigate.", provider)
                self.assertIsNone(result.error)
                self.assertTrue(result.history[1].result.ok)
                self.assertEqual(result.history[1].decision.name, "assess_eligibility")


if __name__ == "__main__":
    unittest.main(verbosity=2)
