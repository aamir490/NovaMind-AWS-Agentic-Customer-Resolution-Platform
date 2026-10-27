"""Focused M3A/M3B coverage for deterministic LangGraph refund routing."""

from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

import test_agent as baseline
from pydantic import ValidationError

from backend.app.agent import AgentRequest
from backend.app.graph_agent import GraphResolutionAgent
from backend.app.hitl import HITLWorkflow
from backend.app.llm import LLMResponse, ProviderFailure, StructuredLLM
from backend.app.operations import AuthoritativeDeliveryFact
from backend.app.proposals import ProposalStatus
from backend.app.resolution_drafts import RefundRecommendationOutcome
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.security import (AuthenticatedIdentity, LocalAuthenticationProvider,
                                  LocalCredential, SecurityError, authenticated)


class RefundOrchestrationTests(baseline.AgentTests):
    def refund_request(self):
        return AgentRequest(case_id=self.case.id, message="Please issue a refund for this order.",
                            resolution_intent="refund", refund_sku="LAP-1")

    def ingest_delivery_fact(self, *, delivered_days_ago=10):
        now = datetime.now(timezone.utc)
        self.delivery_ingestion.ingest(self.delivery_source.id, AuthoritativeDeliveryFact(
            id=uuid4(), case_id=self.case.id, order_id=self.order.id,
            customer_id=self.case.customer_id, sku="LAP-1", quantity=1,
            delivered_at=now - timedelta(days=delivered_days_ago),
            reason="wrong_item", condition="unused", source_id=self.delivery_source.id,
            source_reference=f"m3a-delivery-{delivered_days_ago}", verified_at=now,
        ))

    def test_refund_intent_and_sku_must_be_supplied_together(self):
        for values in (
            {"resolution_intent": "refund"},
            {"refund_sku": "LAP-1"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                AgentRequest(case_id=self.case.id, message="Refund request.", **values)

    def test_eligible_refund_creates_one_pending_proposal_and_pauses_for_review(self):
        self.ingest_delivery_fact()
        provider = baseline.ScriptedProvider([baseline.FINAL])
        workflow = HITLWorkflow(StructuredLLM(provider), self.tools, self.proposals)

        result = workflow.start(self.refund_request())

        self.assertEqual(result.status, "REVIEW_REQUIRED")
        self.assertEqual(result.review.action, "refund")
        self.assertEqual(len(self.proposals.list_for_case(self.case.id)), 1)
        self.assertEqual(self.proposals.list_for_case(self.case.id)[0].status, "PENDING_REVIEW")
        self.assertEqual(provider.requests, [])
        self.assertFalse(result.actions_executed)

    def test_ineligible_refund_creates_investigation_review_not_refund_approval(self):
        self.ingest_delivery_fact(delivered_days_ago=31)
        provider = baseline.ScriptedProvider([baseline.FINAL])
        result = HITLWorkflow(StructuredLLM(provider), self.tools, self.proposals).start(
            self.refund_request())

        self.assertEqual(result.status, "REVIEW_REQUIRED")
        self.assertEqual(result.review.action, "refund_investigation")
        proposal = self.proposals.get(result.review.proposal_id)
        self.assertIsNone(proposal.eligibility_evidence_id)
        self.assertIn("outside_return_window", proposal.rationale)
        self.assertEqual(provider.requests, [])

    def test_missing_evidence_creates_investigation_review_not_refund_approval(self):
        provider = baseline.ScriptedProvider([baseline.FINAL])
        result = HITLWorkflow(StructuredLLM(provider), self.tools, self.proposals).start(
            self.refund_request())

        self.assertEqual(result.status, "REVIEW_REQUIRED")
        self.assertEqual(result.review.action, "refund_investigation")
        proposal = self.proposals.get(result.review.proposal_id)
        self.assertIsNone(proposal.eligibility_evidence_id)
        self.assertIn("not refund approval", proposal.rationale)
        self.assertEqual(provider.requests, [])

    def test_premature_model_final_cannot_resolve_an_eligible_refund(self):
        self.ingest_delivery_fact()
        provider = baseline.ScriptedProvider([baseline.FINAL])

        result = GraphResolutionAgent(StructuredLLM(provider), self.tools).run(self.refund_request())

        self.assertEqual(result.status, "HUMAN_REVIEW_REQUIRED")
        self.assertEqual(len(result.pending_proposal_ids), 1)
        self.assertEqual(self.proposals.get(result.pending_proposal_ids[0]).action, "refund")
        self.assertEqual(provider.requests, [])

    def test_provider_failure_cannot_bypass_refund_evidence_evaluation(self):
        provider = baseline.ScriptedProvider([ProviderFailure("UNAVAILABLE")])
        result = HITLWorkflow(StructuredLLM(provider), self.tools, self.proposals).start(
            self.refund_request())

        self.assertEqual(result.status, "REVIEW_REQUIRED")
        self.assertEqual(result.review.action, "refund_investigation")
        self.assertEqual(len(self.proposals.list_for_case(self.case.id)), 1)
        self.assertEqual(provider.requests, [])

    def test_text_mention_is_not_refund_intent_without_validated_selection(self):
        self.ingest_delivery_fact()
        provider = baseline.ScriptedProvider([baseline.FINAL])
        text_only = AgentRequest(case_id=self.case.id, message="Please refund this order.")

        result = GraphResolutionAgent(StructuredLLM(provider), self.tools).run(text_only)

        self.assertEqual(result.status, "INFORMATIONAL")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(self.proposals.list_for_case(self.case.id), [])

    def test_model_cannot_create_refund_without_explicit_intent(self):
        self.ingest_delivery_fact()
        evidence = self.tools.invoke("assess_refund_eligibility", {
            "case_id": str(self.case.id), "sku": "LAP-1",
        }).data
        provider = baseline.ScriptedProvider([baseline.call(
            "create_resolution_proposal", case_id=str(self.case.id), action="refund",
            rationale="Attempt to infer a refund.", eligibility_evidence_id=str(evidence.evidence_id),
        )])
        text_only = AgentRequest(case_id=self.case.id, message="Please refund this order.")

        result = GraphResolutionAgent(StructuredLLM(provider), self.tools).run(text_only)

        self.assertEqual(result.error, "REFUND_INTENT_REQUIRED")
        self.assertEqual(self.proposals.list_for_case(self.case.id), [])

    def test_exact_selected_sku_is_used_for_multi_item_order(self):
        customer = self.cases.create_customer(CustomerCreate(name="Multi-item customer"))
        order = self.cases.create_order(OrderCreate(customer_id=customer.id, items=[
            {"sku": "LAP-1", "name": "Laptop", "quantity": 1},
            {"sku": "ACC-1", "name": "Dock", "quantity": 1},
        ]))
        case = self.cases.create_case(CaseCreate(customer_id=customer.id, order_id=order.id,
                                                  subject="Order issue", description="Select one item."))
        now = datetime.now(timezone.utc)
        self.delivery_ingestion.ingest(self.delivery_source.id, AuthoritativeDeliveryFact(
            id=uuid4(), case_id=case.id, order_id=order.id, customer_id=customer.id,
            sku="ACC-1", quantity=1, delivered_at=now - timedelta(days=10),
            reason="wrong_item", condition="unused", source_id=self.delivery_source.id,
            source_reference="m3b-multi-item-acc-1", verified_at=now,
        ))
        request = AgentRequest(case_id=case.id, message="Review the selected item.",
                               resolution_intent="refund", refund_sku="ACC-1")

        result = GraphResolutionAgent(StructuredLLM(baseline.ScriptedProvider([baseline.FINAL])),
                                      self.tools).run(request)

        self.assertEqual(result.status, "HUMAN_REVIEW_REQUIRED")
        proposal = self.proposals.get(result.pending_proposal_ids[0])
        evidence = self.app.state.business_operations.get_eligibility_evidence(proposal.eligibility_evidence_id)
        self.assertEqual(evidence.sku, "ACC-1")

    def test_lap10_and_any_other_purchased_sku_use_the_same_review_routing(self):
        customer = self.cases.create_customer(CustomerCreate(name="Universal refund customer"))
        order = self.cases.create_order(OrderCreate(customer_id=customer.id, items=[
            {"sku": "LAP-10", "name": "Laptop 10", "quantity": 1},
            {"sku": "ACC-9", "name": "Adapter", "quantity": 1},
        ]))
        for sku in ("LAP-10", "ACC-9"):
            with self.subTest(sku=sku):
                case = self.cases.create_case(CaseCreate(
                    customer_id=customer.id, order_id=order.id,
                    subject=f"Refund {sku}", description="Human review requested.",
                ))
                provider = baseline.ScriptedProvider([baseline.FINAL])
                result = HITLWorkflow(StructuredLLM(provider), self.tools, self.proposals).start(
                    AgentRequest(case_id=case.id, message="Review refund.",
                                 resolution_intent="refund", refund_sku=sku))
                self.assertEqual(result.status, "REVIEW_REQUIRED")
                self.assertEqual(result.review.action, "refund_investigation")
                self.assertEqual(provider.requests, [])

    def test_trusted_fact_for_purchased_sku_without_policy_enters_investigation(self):
        customer = self.cases.create_customer(CustomerCreate(name="Non-catalog item customer"))
        order = self.cases.create_order(OrderCreate(customer_id=customer.id, items=[
            {"sku": "CUSTOM-42", "name": "Custom purchased item", "quantity": 1},
        ]))
        case = self.cases.create_case(CaseCreate(
            customer_id=customer.id, order_id=order.id,
            subject="Custom item refund", description="Review purchased item.",
        ))
        now = datetime.now(timezone.utc)
        self.delivery_ingestion.ingest(self.delivery_source.id, AuthoritativeDeliveryFact(
            id=uuid4(), case_id=case.id, order_id=order.id, customer_id=customer.id,
            sku="CUSTOM-42", quantity=1, delivered_at=now - timedelta(days=5),
            reason="wrong_item", condition="unused", source_id=self.delivery_source.id,
            source_reference="custom-42-delivery", verified_at=now,
        ))
        provider = baseline.ScriptedProvider([baseline.FINAL])
        result = HITLWorkflow(StructuredLLM(provider), self.tools, self.proposals).start(
            AgentRequest(case_id=case.id, message="Review refund.",
                         resolution_intent="refund", refund_sku="CUSTOM-42"))

        self.assertEqual(result.status, "REVIEW_REQUIRED")
        self.assertEqual(result.review.action, "refund_investigation")
        self.assertIsNone(self.proposals.get(result.review.proposal_id).eligibility_evidence_id)
        self.assertEqual(provider.requests, [])

    def test_investigation_review_decisions_are_atomic_and_not_refund_approval(self):
        def pause():
            workflow = HITLWorkflow(StructuredLLM(baseline.ScriptedProvider([baseline.FINAL])),
                                    self.tools, self.proposals)
            paused = workflow.start(self.refund_request())
            payload = {"workflow_id": paused.review.workflow_id,
                       "case_id": paused.review.case_id,
                       "proposal_id": paused.review.proposal_id,
                       "review_id": paused.review.review_id}
            return workflow, paused, payload

        approved_workflow, approved_pause, approved = pause()
        approved_result = approved_workflow.resume({**approved, "decision": "APPROVE"})
        self.assertEqual(approved_result.reviewed_status, "APPROVED")
        approved_proposal = self.proposals.get(approved_pause.review.proposal_id)
        self.assertEqual(approved_proposal.action, "refund_investigation")
        self.assertIsNone(approved_proposal.eligibility_evidence_id)

        rejected_workflow, rejected_pause, rejected = pause()
        rejected_result = rejected_workflow.resume({**rejected, "decision": "REJECT"})
        self.assertEqual(rejected_result.reviewed_status, "REJECTED")
        self.assertEqual(self.proposals.get(rejected_pause.review.proposal_id).status,
                         ProposalStatus.REJECTED)

    def test_customer_cannot_decide_investigation_review(self):
        workflow = HITLWorkflow(StructuredLLM(baseline.ScriptedProvider([baseline.FINAL])),
                                self.tools, self.proposals)
        paused = workflow.start(self.refund_request())
        payload = {"workflow_id": paused.review.workflow_id, "case_id": paused.review.case_id,
                   "proposal_id": paused.review.proposal_id, "review_id": paused.review.review_id,
                   "decision": "APPROVE"}
        customer_provider = LocalAuthenticationProvider((LocalCredential(
            token="refund-customer-test-token",
            identity=AuthenticatedIdentity(user_id=UUID(int=901), role="CUSTOMER",
                                           customer_id=self.case.customer_id)),))
        with authenticated(customer_provider, "refund-customer-test-token"):
            with self.assertRaises(SecurityError):
                workflow.resume(payload)
        self.assertEqual(self.proposals.get(paused.review.proposal_id).status,
                         ProposalStatus.PENDING_REVIEW)

    def test_sku_outside_case_bound_order_creates_no_proposal(self):
        request = AgentRequest(case_id=self.case.id, message="Refund selected item.",
                               resolution_intent="refund", refund_sku="ACC-1")
        provider = baseline.ScriptedProvider([baseline.FINAL])

        result = GraphResolutionAgent(StructuredLLM(provider), self.tools).run(request)

        self.assertEqual(result.status, "INFORMATIONAL")
        self.assertIn("selected SKU is not in the case-bound order", result.customer_response)
        self.assertEqual(self.proposals.list_for_case(self.case.id), [])
        self.assertEqual(provider.requests, [])

    def test_refund_recommendation_is_python_owned_and_bound_to_trusted_evidence(self):
        self.ingest_delivery_fact()
        result = HITLWorkflow(StructuredLLM(baseline.ScriptedProvider([baseline.FINAL])),
                              self.tools, self.proposals).start(self.refund_request())

        recommendation = result.resolution_draft.refund_recommendation
        self.assertEqual(recommendation.outcome, RefundRecommendationOutcome.ELIGIBLE)
        self.assertEqual(recommendation.sku, "LAP-1")
        self.assertFalse(recommendation.authorization_granted)
        self.assertFalse(recommendation.actions_executed)
        self.assertIn("eligibility_evidence_id:", " ".join(recommendation.verified_evidence))
        self.assertEqual(result.review.action, "refund")

    def test_missing_trusted_evidence_is_never_eligible(self):
        result = HITLWorkflow(StructuredLLM(baseline.ScriptedProvider([baseline.FINAL])),
                              self.tools, self.proposals).start(self.refund_request())

        recommendation = result.resolution_draft.refund_recommendation
        self.assertEqual(recommendation.outcome, RefundRecommendationOutcome.NEED_MORE_EVIDENCE)
        self.assertTrue(recommendation.missing_evidence)
        self.assertEqual(result.review.action, "refund_investigation")

    def test_policy_denial_is_not_eligible_and_preserves_non_execution_boundary(self):
        self.ingest_delivery_fact(delivered_days_ago=31)
        result = HITLWorkflow(StructuredLLM(baseline.ScriptedProvider([baseline.FINAL])),
                              self.tools, self.proposals).start(self.refund_request())

        recommendation = result.resolution_draft.refund_recommendation
        self.assertEqual(recommendation.outcome, RefundRecommendationOutcome.NOT_ELIGIBLE)
        self.assertIn("outside_return_window", recommendation.decision_reason)
        self.assertFalse(result.actions_executed)
        self.assertEqual(result.review.action, "refund_investigation")

    def test_bedrock_wording_cannot_change_the_python_refund_recommendation(self):
        self.ingest_delivery_fact()

        class GroundedProvider:
            def generate(self, request, *, response_schema):
                context = json.loads(request.messages[-1].content)
                draft = context["grounded_resolution_draft"]
                recommendation = draft["refund_recommendation"]
                return LLMResponse(text=json.dumps({"decision": {"kind": "final"}, "grounded_response": {
                    "issue_type": draft["issue_type"], "recommended_decision": draft["recommended_decision"],
                    "verified_facts": draft["verified_facts"], "missing_evidence": draft["missing_evidence"],
                    "policy_passages": context["grounded_policy_evidence"]["policy_passages"],
                    "refund_outcome": recommendation["outcome"], "refund_sku": recommendation["sku"],
                    "decision_reason": "Trusted evidence supports this staff-only recommendation. No refund, replacement, return, or payment action has been executed.",
                    "customer_facing_response": "Our staff will review the verified evidence. No refund, replacement, return, or payment action has been executed.",
                }}), finish_reason="stop")

        result = HITLWorkflow(StructuredLLM(GroundedProvider()), self.tools, self.proposals,
                              provider_label="bedrock").start(self.refund_request())
        recommendation = result.resolution_draft.refund_recommendation
        self.assertEqual(recommendation.outcome, RefundRecommendationOutcome.ELIGIBLE)
        self.assertEqual(recommendation.sku, "LAP-1")
        self.assertEqual(result.resolution_draft.response_source, "BEDROCK_VALIDATED")
        self.assertEqual(result.review.action, "refund")

    def test_bedrock_cannot_downgrade_or_upgrade_the_python_refund_outcome(self):
        self.ingest_delivery_fact()

        class AlteringProvider:
            def generate(self, request, *, response_schema):
                context = json.loads(request.messages[-1].content)
                draft = context["grounded_resolution_draft"]
                return LLMResponse(text=json.dumps({"decision": {"kind": "final"}, "grounded_response": {
                    "issue_type": draft["issue_type"], "recommended_decision": draft["recommended_decision"],
                    "verified_facts": draft["verified_facts"], "missing_evidence": draft["missing_evidence"],
                    "policy_passages": context["grounded_policy_evidence"]["policy_passages"],
                    "refund_outcome": "NOT_ELIGIBLE", "refund_sku": "LAP-1",
                    "decision_reason": "Refund denied. No refund, replacement, return, or payment action has been executed.",
                    "customer_facing_response": "No action has been taken. No refund, replacement, return, or payment action has been executed.",
                }}), finish_reason="stop")

        result = HITLWorkflow(StructuredLLM(AlteringProvider()), self.tools, self.proposals,
                              provider_label="bedrock").start(self.refund_request())
        self.assertEqual(result.resolution_draft.refund_recommendation.outcome,
                         RefundRecommendationOutcome.ELIGIBLE)
        self.assertEqual(result.resolution_draft.response_source, "DETERMINISTIC_FALLBACK")
