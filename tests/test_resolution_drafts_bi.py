"""Focused offline BI resolution-draft coverage; no provider or network calls."""

import unittest
from datetime import datetime, timezone
import json
from uuid import uuid4

from backend.app.agent import AgentRequest
from backend.app.domain import Customer
from backend.app.graph_agent import GraphResolutionAgent
from backend.app.hitl import HITLWorkflow
from backend.app.llm import FakeLLMProvider, LLMResponse, ProviderFailure, StructuredLLM
from backend.app.main import create_app
from backend.app.operations import AuthoritativeDeliveryFact, TrustedDeliverySource
from backend.app.resolution_drafts import IssueType, RecommendedDecision
from backend.app.schemas import CustomerCreate, CaseCreate, OrderCreate
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role, authenticated


class ResolutionDraftTests(unittest.TestCase):
    def setUp(self):
        self.source = TrustedDeliverySource(id="bi-test-fulfillment")
        self.app = create_app(trusted_delivery_sources=(self.source,))
        self.service = self.app.state.case_service
        self.customer = self.service.create_customer(CustomerCreate(name="BI customer"))
        self.order = self.service.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))
        self.token = "bi-resolution-draft-staff-token"
        self.auth = LocalAuthenticationProvider((LocalCredential(
            token=self.token,
            identity=AuthenticatedIdentity(user_id=uuid4(), role=Role.REVIEWER),
        ),))

    def case(self, subject, description):
        return self.service.create_case(CaseCreate(
            customer_id=self.customer.id, order_id=self.order.id,
            subject=subject, description=description,
        ))

    def draft(self, support_case, message="Please investigate this request."):
        with authenticated(self.auth, self.token):
            return self.app.state.local_tools.build_resolution_draft(support_case.id, message)

    class WordingProvider:
        """Offline provider that derives its response only from the graph prompt."""

        def __init__(self, mutate=None):
            self.calls = 0
            self.mutate = mutate

        def generate(self, request, *, response_schema):
            self.calls += 1
            prompt = json.loads(request.messages[-1].content)
            draft = prompt["grounded_resolution_draft"]
            response = {
                "issue_type": draft["issue_type"],
                "recommended_decision": draft["recommended_decision"],
                "verified_facts": draft["verified_facts"],
                "missing_evidence": draft["missing_evidence"],
                "policy_passages": prompt["grounded_policy_evidence"]["policy_passages"],
                "decision_reason": (
                    "The verified records still require staff review of the listed evidence. "
                    "No refund, replacement, return, or payment action has been executed."
                ),
                "customer_facing_response": (
                    "Please provide the requested evidence so our staff can review your case. "
                    "No refund, replacement, return, or payment action has been executed."
                ),
                "actions_executed": False,
            }
            if self.mutate is not None:
                self.mutate(response)
            return LLMResponse(text=json.dumps({
                "decision": {"kind": "final"}, "grounded_response": response,
            }), finish_reason="stop")

    def test_all_supported_issue_types_are_classified_with_no_shipping_or_payment_invention(self):
        cases = {
            IssueType.DAMAGED: ("Damaged package", "The product arrived damaged."),
            IssueType.WRONG_ITEM: ("Wrong item", "I received a different item."),
            IssueType.MISSING_ITEM: ("Missing item", "An item is missing from the box."),
            IssueType.DELAYED_DELIVERY: ("Delivery delayed", "My delivery is late."),
            IssueType.MALFUNCTION: ("Product malfunction", "The laptop stopped working."),
            IssueType.RETURN: ("Return request", "I want to return the laptop."),
            IssueType.REFUND_NOT_RECEIVED: ("Refund not received", "My refund has not arrived."),
        }
        for issue, (subject, description) in cases.items():
            with self.subTest(issue=issue):
                draft = self.draft(self.case(subject, description))
                self.assertEqual(draft.issue_type, issue)
                self.assertIn(f"order_id:{self.order.id}", draft.verified_facts)
                self.assertFalse(draft.actions_executed)
                self.assertIn("No refund, replacement, return, or payment action has been executed.",
                              draft.customer_facing_response)
                if issue is IssueType.REFUND_NOT_RECEIVED:
                    self.assertEqual(draft.recommended_decision, RecommendedDecision.ESCALATE)
                    self.assertIn("trusted payment/refund settlement record", draft.missing_evidence)
                elif issue is IssueType.DELAYED_DELIVERY:
                    self.assertEqual(draft.recommended_decision, RecommendedDecision.ESCALATE)
                    self.assertIn("trusted shipment tracking status", draft.missing_evidence)
                elif issue is IssueType.MALFUNCTION:
                    self.assertEqual(draft.recommended_decision, RecommendedDecision.REQUEST_EVIDENCE)
                    self.assertIn("reported symptom, when it occurs, and steps already tried", draft.missing_evidence)
                    self.assertNotIn("trusted shipping or delivery evidence", draft.missing_evidence)
                else:
                    self.assertEqual(draft.recommended_decision, RecommendedDecision.REQUEST_EVIDENCE)
                    self.assertIn("trusted shipping or delivery evidence", draft.missing_evidence)

    def test_technical_draft_requests_diagnostics_not_delivery_evidence(self):
        support_case = self.case(
            "NovaControl Input 1 stops responding",
            "The input device has an intermittent connection during normal use.",
        )
        draft = self.draft(support_case, "Please investigate this technical issue.")
        self.assertEqual(draft.issue_type, IssueType.MALFUNCTION)
        self.assertEqual(draft.recommended_decision, RecommendedDecision.REQUEST_EVIDENCE)
        self.assertEqual(draft.missing_evidence, (
            "reported symptom, when it occurs, and steps already tried",
            "product and connection/setup details, including any visible error indicator",
        ))
        self.assertNotIn("delivery", " ".join(draft.missing_evidence).casefold())
        self.assertIn("diagnostic details", draft.customer_facing_response)
        self.assertFalse(draft.actions_executed)

    def test_return_and_refund_drafts_keep_their_nontechnical_evidence_requirements(self):
        return_draft = self.draft(self.case("Return request", "I want to return the laptop."))
        refund_draft = self.draft(self.case("Refund not received", "My refund has not arrived."))
        self.assertEqual(return_draft.issue_type, IssueType.RETURN)
        self.assertIn("trusted shipping or delivery evidence", return_draft.missing_evidence)
        self.assertEqual(refund_draft.issue_type, IssueType.REFUND_NOT_RECEIVED)
        self.assertIn("trusted payment/refund settlement record", refund_draft.missing_evidence)
        self.assertNotIn("diagnostic details", return_draft.customer_facing_response)
        self.assertNotIn("diagnostic details", refund_draft.customer_facing_response)

    def test_trusted_delivery_evidence_changes_only_the_draft_escalation_not_eligibility(self):
        support_case = self.case("Damaged laptop", "The laptop arrived damaged.")
        self.app.state.delivery_fact_ingestion.ingest(self.source.id, AuthoritativeDeliveryFact(
            id=uuid4(), case_id=support_case.id, order_id=self.order.id,
            customer_id=self.customer.id, sku="LAP-1", quantity=1,
            delivered_at=datetime.now(timezone.utc),
            reason="damaged", condition="damaged", source_id=self.source.id,
            source_reference="bi-delivery-1",
            verified_at=datetime.now(timezone.utc),
        ))
        draft = self.draft(support_case)
        self.assertEqual(draft.recommended_decision, RecommendedDecision.ESCALATE)
        self.assertIn("trusted_delivery_evidence:present", draft.verified_facts)
        self.assertNotIn("eligible", draft.decision_reason.casefold())

    def test_langgraph_result_contains_the_grounded_structured_draft(self):
        support_case = self.case("Delayed order", "The delivery is overdue.")
        agent = GraphResolutionAgent(StructuredLLM(FakeLLMProvider(LLMResponse(
            text='{"decision":{"kind":"final"}}', finish_reason="stop",
        ))), self.app.state.local_tools)
        with authenticated(self.auth, self.token):
            result = agent.run(AgentRequest(case_id=support_case.id, message="My shipment is late."))
        self.assertEqual(result.resolution_draft.issue_type, IssueType.DELAYED_DELIVERY)
        self.assertEqual(result.resolution_draft.recommended_decision, RecommendedDecision.ESCALATE)
        self.assertEqual(result.pending_proposal_ids, ())
        self.assertFalse(result.actions_executed)

    def test_technical_graph_uses_the_deterministic_safe_response_without_case_mutation(self):
        support_case = self.case(
            "NovaControl Input 1 stops responding",
            "The input device has an intermittent connection during normal use.",
        )
        deterministic = self.draft(support_case, "Please investigate this technical issue.")
        agent = GraphResolutionAgent(StructuredLLM(FakeLLMProvider(LLMResponse(
            text='{"decision":{"kind":"final"}}', finish_reason="stop",
        ))), self.app.state.local_tools)
        with authenticated(self.auth, self.token):
            result = agent.run(AgentRequest(case_id=support_case.id, message="Please investigate this technical issue."))
        self.assertEqual(result.status, "INFORMATIONAL")
        self.assertEqual(result.customer_response, deterministic.customer_facing_response)
        self.assertEqual(result.resolution_draft, deterministic)
        self.assertEqual(self.service.get_case(support_case.id), support_case)
        self.assertEqual(self.app.state.proposal_service.list_for_case(support_case.id), [])
        self.assertFalse(result.actions_executed)

    def test_model_cannot_override_the_grounded_draft(self):
        support_case = self.case("Damaged item", "The item arrived damaged.")
        provider = FakeLLMProvider(LLMResponse(text='''{
            "decision":{"kind":"final"},
            "resolution_draft":{
                "issue_type":"refund_not_received",
                "recommended_decision":"ESCALATE",
                "verified_facts":["invented payment record"],
                "missing_evidence":["none"],
                "decision_reason":"Refund approved.",
                "customer_facing_response":"Your refund was sent.",
                "actions_executed":false
            }
        }''', finish_reason="stop"))
        with authenticated(self.auth, self.token):
            result = GraphResolutionAgent(StructuredLLM(provider), self.app.state.local_tools).run(
                AgentRequest(case_id=support_case.id, message="It arrived damaged."),
            )
        self.assertEqual(result.resolution_draft.issue_type, IssueType.DAMAGED)
        self.assertEqual(result.resolution_draft.recommended_decision, RecommendedDecision.REQUEST_EVIDENCE)
        self.assertNotIn("invented payment record", result.resolution_draft.verified_facts)

    def test_grounded_model_wording_is_accepted_without_changing_authoritative_fields(self):
        support_case = self.case("Delayed order", "The delivery is overdue.")
        provider = self.WordingProvider()
        with authenticated(self.auth, self.token):
            result = GraphResolutionAgent(StructuredLLM(provider), self.app.state.local_tools).run(
                AgentRequest(case_id=support_case.id, message="My shipment is late."),
            )
        deterministic = self.draft(support_case, "My shipment is late.")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result.resolution_draft.issue_type, deterministic.issue_type)
        self.assertEqual(result.resolution_draft.recommended_decision, deterministic.recommended_decision)
        self.assertEqual(result.resolution_draft.verified_facts, deterministic.verified_facts)
        self.assertEqual(result.resolution_draft.missing_evidence, deterministic.missing_evidence)
        self.assertNotEqual(result.resolution_draft.decision_reason, deterministic.decision_reason)
        self.assertIn("No refund, replacement, return, or payment action has been executed.",
                      result.resolution_draft.customer_facing_response)

    def test_validated_technical_model_wording_is_used_as_the_customer_response(self):
        support_case = self.case(
            "NovaControl Input 1 stops responding",
            "The input device has an intermittent connection during normal use.",
        )
        provider = self.WordingProvider()
        with authenticated(self.auth, self.token):
            result = GraphResolutionAgent(StructuredLLM(provider), self.app.state.local_tools).run(
                AgentRequest(case_id=support_case.id, message="Please investigate this technical issue."),
            )
        deterministic = self.draft(support_case, "Please investigate this technical issue.")
        self.assertEqual(provider.calls, 1)
        self.assertNotEqual(result.resolution_draft.customer_facing_response,
                            deterministic.customer_facing_response)
        self.assertEqual(result.customer_response, result.resolution_draft.customer_facing_response)
        self.assertEqual(self.service.get_case(support_case.id), support_case)
        self.assertEqual(self.app.state.proposal_service.list_for_case(support_case.id), [])
        self.assertFalse(result.actions_executed)

    def test_product_specific_steps_require_the_case_bound_sku_and_exact_retrieved_text(self):
        order = self.service.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "INP-1", "name": "NovaControl Input 1", "quantity": 1}],
        ))
        support_case = self.service.create_case(CaseCreate(
            customer_id=self.customer.id, order_id=order.id,
            subject="NovaControl Input 1 stops responding",
            description="The input device has an intermittent connection.",
        ))

        class ProductGuidanceProvider:
            def generate(self, request, *, response_schema):
                prompt = json.loads(request.messages[-1].content)
                draft = prompt["grounded_resolution_draft"]
                steps = (
                    "Disconnect the NovaControl Input 1 from the current port.",
                    "Connect the NovaControl Input 1 directly to a different compatible port.",
                    "Restart the host device and report whether the symptom continues.",
                )
                return LLMResponse(text=json.dumps({"decision": {"kind": "final"}, "grounded_response": {
                    "issue_type": draft["issue_type"],
                    "recommended_decision": draft["recommended_decision"],
                    "verified_facts": draft["verified_facts"],
                    "missing_evidence": draft["missing_evidence"],
                    "policy_passages": prompt["grounded_policy_evidence"]["policy_passages"],
                    "decision_reason": "The verified records require diagnostic evidence for support review.",
                    "customer_facing_response": (
                        "Please try these steps:\n1. " + steps[0] + "\n2. " + steps[1]
                        + "\n3. " + steps[2]
                        + "\nNo refund, replacement, return, or payment action has been executed."
                    ),
                    "troubleshooting_steps": steps,
                }}), finish_reason="stop")

        with authenticated(self.auth, self.token):
            result = GraphResolutionAgent(StructuredLLM(ProductGuidanceProvider()), self.app.state.local_tools).run(
                AgentRequest(case_id=support_case.id, message="Please investigate this technical issue."),
            )
        self.assertEqual(result.resolution_draft.response_source, "BEDROCK_VALIDATED")
        self.assertEqual(result.resolution_draft.troubleshooting_steps, (
            "Disconnect the NovaControl Input 1 from the current port.",
            "Connect the NovaControl Input 1 directly to a different compatible port.",
            "Restart the host device and report whether the symptom continues.",
        ))
        self.assertIn("1. Disconnect the NovaControl Input 1", result.customer_response)
        self.assertIn("novacontrol-inp-1-guide", {citation.document_id for citation in result.resolution_draft.citations})
        self.assertFalse(result.actions_executed)

    def test_unapproved_product_step_uses_the_deterministic_fallback(self):
        order = self.service.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "INP-1", "name": "NovaControl Input 1", "quantity": 1}],
        ))
        support_case = self.service.create_case(CaseCreate(
            customer_id=self.customer.id, order_id=order.id,
            subject="NovaControl Input 1 stops responding", description="The input device is intermittent.",
        ))

        class UnsafeStepProvider:
            def generate(self, request, *, response_schema):
                prompt = json.loads(request.messages[-1].content)
                draft = prompt["grounded_resolution_draft"]
                return LLMResponse(text=json.dumps({"decision": {"kind": "final"}, "grounded_response": {
                    "issue_type": draft["issue_type"], "recommended_decision": draft["recommended_decision"],
                    "verified_facts": draft["verified_facts"], "missing_evidence": draft["missing_evidence"],
                    "policy_passages": prompt["grounded_policy_evidence"]["policy_passages"],
                    "decision_reason": "Support review needs evidence.",
                    "customer_facing_response": "1. Open the device casing. No refund, replacement, return, or payment action has been executed.",
                    "troubleshooting_steps": ["Open the device casing."],
                }}), finish_reason="stop")

        with authenticated(self.auth, self.token):
            result = GraphResolutionAgent(StructuredLLM(UnsafeStepProvider()), self.app.state.local_tools).run(
                AgentRequest(case_id=support_case.id, message="Please investigate this technical issue."),
            )
        self.assertEqual(result.resolution_draft.response_source, "DETERMINISTIC_FALLBACK")
        self.assertEqual(result.resolution_draft.troubleshooting_steps, ())
        self.assertFalse(result.actions_executed)

    def test_mismatched_model_citation_uses_the_safe_deterministic_draft(self):
        support_case = self.case("Delayed order", "The delivery is overdue.")

        def mutate(response):
            response["policy_passages"][0]["document_id"] = "invented-policy"

        provider = self.WordingProvider(mutate)
        with authenticated(self.auth, self.token):
            result = GraphResolutionAgent(StructuredLLM(provider), self.app.state.local_tools).run(
                AgentRequest(case_id=support_case.id, message="My shipment is late."),
            )
        deterministic = self.draft(support_case, "My shipment is late.")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result.resolution_draft, deterministic)

    def test_failed_generation_retains_the_safe_deterministic_draft(self):
        support_case = self.case("Delayed order", "The delivery is overdue.")
        with authenticated(self.auth, self.token):
            result = GraphResolutionAgent(StructuredLLM(FakeLLMProvider(ProviderFailure("UNAVAILABLE"))),
                                          self.app.state.local_tools).run(
                AgentRequest(case_id=support_case.id, message="My shipment is late."),
            )
        self.assertEqual(result.error, "UNAVAILABLE")
        self.assertEqual(result.resolution_draft, self.draft(support_case, "My shipment is late."))

    def test_hitl_result_retains_the_grounded_draft(self):
        support_case = self.case("Delayed order", "The delivery is late.")
        workflow = HITLWorkflow(StructuredLLM(FakeLLMProvider(LLMResponse(
            text='{"decision":{"kind":"final"}}', finish_reason="stop",
        ))), self.app.state.local_tools, self.app.state.proposal_service)
        with authenticated(self.auth, self.token):
            result = workflow.start(AgentRequest(case_id=support_case.id, message="Where is the shipment?"))
        self.assertEqual(result.resolution_draft.issue_type, IssueType.DELAYED_DELIVERY)
        self.assertEqual(result.resolution_draft.recommended_decision, RecommendedDecision.ESCALATE)


if __name__ == "__main__":
    unittest.main()
