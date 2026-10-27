"""Focused Phase 10 tests; fake providers, in-memory checkpoints, no live calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import uuid4
from unittest.mock import Mock, patch

import test_agent as baseline
from backend.app.agent import AgentConfig, AgentRequest
from backend.app.bedrock import BedrockProvider
from backend.app.hitl import HITLWorkflow, HumanDecision
from backend.app.llm import StructuredLLM, ProviderFailure
from backend.app.operations import AuthoritativeDeliveryFact
from backend.app.proposals import HumanReview, ProposalStatus
from backend.app.service import ConflictError
from security_fixtures import ADMIN_ID, call_as_admin


class HITLTests(unittest.TestCase):
    setUp = baseline.AgentTests.setUp
    proposal = baseline.AgentTests.proposal

    def start(self, script=None, max_steps=8):
        self.provider = baseline.ScriptedProvider(script if script is not None else [self.proposal()])
        self.workflow = HITLWorkflow(StructuredLLM(self.provider), self.tools, self.proposals,
                                     AgentConfig(max_steps=max_steps))
        self.paused = self.workflow.start(self.request)
        return self.paused

    def decision(self, **overrides):
        review = self.paused.review
        return {"workflow_id": str(review.workflow_id), "case_id": str(review.case_id),
                "proposal_id": str(review.proposal_id), "review_id": str(review.review_id),
                "decision": "APPROVE", **overrides}

    def test_real_interrupt_and_minimal_payload(self):
        result = self.start()
        self.assertEqual(result.status, "REVIEW_REQUIRED")
        self.assertEqual(len(self.provider.requests), 1)
        self.assertIsNone(result.agent_result)
        self.assertEqual(set(result.review.model_dump()), {
            "workflow_id", "case_id", "proposal_id", "review_id", "action", "rationale", "status",
        })
        snapshot = self.workflow._graph.get_state(self.workflow._runtime_config(result.workflow_id))
        self.assertEqual(snapshot.next, ("human_review",))
        self.assertTrue(snapshot.tasks[0].interrupts)
        self.assertEqual(self.proposals.get(result.review.proposal_id).status, "PENDING_REVIEW")

    def test_nova_pro_refund_decision_pauses_for_human_review_without_execution(self):
        now = datetime.now(timezone.utc)
        self.delivery_ingestion.ingest(self.delivery_source.id, AuthoritativeDeliveryFact(
            id=uuid4(), case_id=self.case.id, order_id=self.order.id,
            customer_id=self.case.customer_id, sku="LAP-1", quantity=1,
            delivered_at=now - timedelta(days=10), reason="wrong_item", condition="unused",
            source_id=self.delivery_source.id,
            source_reference="test-verified-delivery", verified_at=now,
        ))
        client = Mock()
        workflow = HITLWorkflow(StructuredLLM(BedrockProvider(
            model_id="us.amazon.nova-pro-v1:0", client=client)), self.tools, self.proposals)
        order_before = self.cases.get_order(self.order.id)
        case_before = self.cases.get_case(self.case.id)
        inventory_before = self.app.state.business_operations.get_inventory("LAP-1")
        with patch.object(self.proposals, "review", wraps=self.proposals.review) as review:
            result = workflow.start(AgentRequest(
                case_id=self.case.id, message="Request a refund for the selected item.",
                resolution_intent="refund", refund_sku="LAP-1",
            ))
            review.assert_not_called()
        self.assertEqual(result.status, "REVIEW_REQUIRED")
        self.assertEqual(result.review.action, "refund")
        self.assertEqual(self.proposals.get(result.review.proposal_id).status, "PENDING_REVIEW")
        self.assertEqual(len(self.proposals.list_for_case(self.case.id)), 1)
        self.assertFalse(result.actions_executed)
        self.assertEqual(self.cases.get_order(self.order.id), order_before)
        self.assertEqual(self.cases.get_case(self.case.id), case_before)
        self.assertEqual(self.app.state.business_operations.get_inventory("LAP-1"), inventory_before)
        snapshot = workflow._graph.get_state(workflow._runtime_config(result.workflow_id))
        self.assertEqual(snapshot.next, ("human_review",))
        self.assertTrue(snapshot.tasks[0].interrupts)
        client.converse.assert_not_called()

    def test_nova_pro_non_refund_tool_decision_reaches_human_review(self):
        proposal = self.proposal()
        client = Mock()
        client.converse.return_value = {"stopReason": "tool_use", "output": {"message": {
            "role": "assistant", "content": [{"toolUse": {
                "toolUseId": "tooluse_offline", "name": "submit_tool_decision",
                "input": proposal}}]}}}
        workflow = HITLWorkflow(StructuredLLM(BedrockProvider(
            model_id="us.amazon.nova-pro-v1:0", client=client)), self.tools, self.proposals)

        result = workflow.start(self.request)

        self.assertEqual(result.status, "REVIEW_REQUIRED")
        self.assertEqual(result.review.action, "replacement")
        self.assertFalse(result.actions_executed)
        client.converse.assert_called_once()
        config = client.converse.call_args.kwargs["toolConfig"]
        self.assertEqual(config["toolChoice"], {"any": {}})
        self.assertEqual({item["toolSpec"]["name"] for item in config["tools"]},
                         {"submit_final_decision", "submit_tool_decision"})

    def test_approve_resume_reuses_review_service_and_never_calls_llm_again(self):
        self.start()
        with patch.object(self.proposals, "review", wraps=self.proposals.review) as review:
            result = self.workflow.resume(self.decision(note="Evidence checked"))
            review.assert_called_once()
        self.assertEqual(result.status, "REVIEWED")
        self.assertEqual(result.reviewed_status, "APPROVED")
        proposal = self.proposals.get(self.paused.review.proposal_id)
        self.assertEqual(proposal.reviewer_name, str(ADMIN_ID))
        self.assertEqual(proposal.review_note, "Evidence checked")
        self.assertEqual(len(self.provider.requests), 1)
        self.assertFalse(result.actions_executed)

    def test_reject_resume_is_final_without_model_veto(self):
        self.start([self.proposal(), baseline.call("approve")])
        result = self.workflow.resume(self.decision(decision="REJECT"))
        self.assertEqual(result.reviewed_status, "REJECTED")
        proposal = self.proposals.get(self.paused.review.proposal_id)
        self.assertEqual(proposal.review_note, "No note supplied.")
        self.assertEqual(len(self.provider.requests), 1)

    def test_invalid_resume_does_not_consume_pause(self):
        self.start()
        for changes in ({"decision": "APPROVED"}, {"decision": True}, {"reviewer_name": " "},
                        {"reviewer_name": 2}, {"note": " "}, {"status": "APPROVED"},
                        {"action": "replacement"}, {"goto": "execute_tool"}, {"note": "x" * 4001}):
            with self.subTest(changes=changes):
                self.assertEqual(self.workflow.resume(self.decision(**changes)).error, "INVALID_INPUT")
        for malformed in ({}, [], None):
            self.assertEqual(self.workflow.resume(malformed).error, "INVALID_INPUT")
        self.assertEqual(self.proposals.get(self.paused.review.proposal_id).status, "PENDING_REVIEW")
        self.assertEqual(self.workflow.resume(self.decision()).status, "REVIEWED")

    def test_wrong_workflow_case_proposal_and_review_id(self):
        self.start()
        for field in ("workflow_id", "case_id", "proposal_id", "review_id"):
            with self.subTest(field=field):
                result = self.workflow.resume(self.decision(**{field: str(uuid4())}))
                self.assertEqual(result.error, "UNKNOWN_WORKFLOW" if field == "workflow_id" else "REVIEW_MISMATCH")
        self.assertEqual(self.workflow.resume(self.decision()).status, "REVIEWED")

    def test_two_workflows_cannot_exchange_review_payloads(self):
        self.start([self.proposal(), self.proposal()])
        first = self.decision()
        other = self.workflow.start(self.request)
        self.assertEqual(self.workflow.resume({**first, "workflow_id": str(other.workflow_id)}).error, "REVIEW_MISMATCH")
        self.assertEqual(self.workflow.resume(first).status, "REVIEWED")
        self.assertEqual(self.proposals.get(other.review.proposal_id).status, "PENDING_REVIEW")

    def test_duplicate_and_changed_decision_replays_are_rejected(self):
        self.start()
        payload = self.decision()
        self.workflow.resume(payload)
        before = self.proposals.get(self.paused.review.proposal_id)
        self.assertEqual(self.workflow.resume(payload).error, "NOT_PAUSED")
        self.assertEqual(self.workflow.resume({**payload, "decision": "REJECT"}).error, "NOT_PAUSED")
        self.assertEqual(self.proposals.get(before.id), before)
        self.assertEqual(len(self.proposals.list_for_case(self.case.id)), 1)

    def test_concurrent_resumes_have_one_winner(self):
        self.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda decision: call_as_admin(self.workflow.resume, decision),
                                     [self.decision(), self.decision(decision="REJECT")]))
        self.assertEqual(sum(getattr(r, "status", None) == "REVIEWED" for r in outcomes), 1)
        self.assertEqual(sum(getattr(r, "error", None) == "NOT_PAUSED" for r in outcomes), 1)

    def test_workflow_bound_proposal_rejects_external_review(self):
        self.start()
        with self.assertRaises(ConflictError):
            self.proposals.review(self.paused.review.proposal_id, ProposalStatus.REJECTED,
                                  HumanReview(reviewer_name="Other human", note="Rejected elsewhere"))
        self.assertEqual(self.workflow.resume(self.decision(decision="REJECT")).status, "REVIEWED")
        self.assertEqual(self.proposals.get(self.paused.review.proposal_id).status, ProposalStatus.REJECTED)
        audit = self.workflow.audit(self.paused.workflow_id)
        self.assertEqual((audit[-1].kind, audit[-1].proposal_status),
                         ("HUMAN_REVIEW", ProposalStatus.REJECTED))

    def test_ordered_audit_survives_checkpoint_and_resume_without_duplicate_creation(self):
        self.start([baseline.call("get_inventory", sku="LAP-1"), self.proposal()])
        before = self.workflow.audit(self.paused.workflow_id)
        self.assertEqual([e.kind for e in before], ["AGENT", "AGENT", "AGENT", "REVIEW_REQUIRED"])
        self.workflow.resume(self.decision())
        after = self.workflow.audit(self.paused.workflow_id)
        self.assertEqual(after[:-1], before)
        self.assertEqual(after[-1].kind, "HUMAN_REVIEW")
        self.assertEqual(after[-1].reviewer_user_id, ADMIN_ID)
        self.assertEqual([e.sequence for e in after], list(range(1, len(after) + 1)))
        self.assertEqual(len(self.proposals.list_for_case(self.case.id)), 1)

    def test_llm_cannot_supply_review_identity_or_resume(self):
        for request in (self.proposal(reviewer_name="Model"),
                        baseline.call("resume", decision="APPROVE", reviewer_name="Model"),
                        baseline.call("review", reviewer_name="Model"), baseline.call("approve"),
                        {"decision": {"kind": "final", "reviewer_name": "Model"}}):
            with self.subTest(request=request):
                result = self.start([request])
                self.assertEqual(result.status, "FAILED")
        self.assertEqual(self.proposals.list_for_case(self.case.id), [])

    def test_review_never_executes_business_actions(self):
        for decision in ("APPROVE", "REJECT"):
            with self.subTest(decision=decision):
                self.start()
                inventory = self.app.state.business_operations.get_inventory("LAP-1")
                self.workflow.resume(self.decision(decision=decision))
                self.assertEqual(self.cases.get_case(self.case.id), self.case)
                self.assertEqual(self.cases.get_order(self.order.id), self.order)
                self.assertEqual(self.app.state.business_operations.get_inventory("LAP-1"), inventory)

    def test_tool_limit_and_provider_failure_still_terminate(self):
        result = self.start([baseline.call("get_inventory", sku="LAP-1")] * 2, max_steps=1)
        self.assertEqual(result.error, "STEP_LIMIT")
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(self.start([ProviderFailure("TIMEOUT")]).error, "TIMEOUT")
        self.assertEqual(self.start([baseline.call("execute_refund")]).error, "UNKNOWN_TOOL")
        self.assertEqual(self.start([baseline.call("get_order", order_id="bad")]).error, "INVALID_INPUT")

    def test_last_llm_step_proposal_can_pause_and_review_without_new_llm_call(self):
        result = self.start(max_steps=1)
        self.assertEqual(result.status, "REVIEW_REQUIRED")
        self.assertEqual(self.workflow.resume(self.decision()).status, "REVIEWED")
        self.assertEqual(len(self.provider.requests), 1)

    def test_informational_run_has_no_interrupt(self):
        result = self.start([baseline.FINAL])
        self.assertEqual(result.status, "COMPLETED")
        self.assertIsNone(result.review)
        self.assertEqual(result.agent_result.status, "INFORMATIONAL")

    def test_missing_case_never_calls_model(self):
        self.request = AgentRequest(case_id=uuid4(), message="Review")
        self.assertEqual(self.start([]).error, "NOT_FOUND")
        self.assertEqual(self.provider.requests, [])

    def test_checkpoint_does_not_cross_workflow_service_instances(self):
        self.start()
        other = HITLWorkflow(StructuredLLM(baseline.ScriptedProvider([])), self.tools, self.proposals)
        self.assertEqual(other.resume(self.decision()).error, "UNKNOWN_WORKFLOW")

    def test_review_write_exception_is_not_retried(self):
        self.start()
        original = self.proposals.review
        def uncertain(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("private detail")
        with patch.object(self.proposals, "review", side_effect=uncertain) as review:
            result = self.workflow.resume(self.decision())
            review.assert_called_once()
        self.assertEqual(result.error, "WORKFLOW_ERROR")
        self.assertNotIn("private detail", result.model_dump_json())
        self.assertEqual(self.workflow.resume(self.decision()).error, "NOT_PAUSED")
        self.assertEqual(self.workflow.audit(self.paused.workflow_id)[-1].kind, "FAILURE")

    def test_strict_checkpoint_serialization_supports_pause_and_resume(self):
        with patch.dict(os.environ, {"LANGGRAPH_STRICT_MSGPACK": "true"}):
            self.assertEqual(self.start().status, "REVIEW_REQUIRED")
            self.assertEqual(self.workflow.resume(self.decision()).status, "REVIEWED")
            self.assertEqual(self.workflow.audit(self.paused.workflow_id)[-1].kind, "HUMAN_REVIEW")

    def test_tampered_model_instance_is_revalidated(self):
        self.start()
        decision = HumanDecision.model_validate(self.decision())
        tampered = decision.model_copy(update={"decision": "EXECUTE"})
        self.assertEqual(self.workflow.resume(tampered).error, "INVALID_INPUT")
        self.assertEqual(self.workflow.resume(decision).status, "REVIEWED")
