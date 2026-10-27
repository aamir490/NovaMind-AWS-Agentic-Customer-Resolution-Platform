"""Focused Phase 5 tool-contract tests; no HTTP server or previous suites."""

import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.app.main import create_app
from backend.app.proposals import HumanReview, ProposalStatus
from backend.app.schemas import CustomerCreate, OrderCreate, CaseCreate
from backend.app.tools import LocalTools
from security_fixtures import admin_scope


class LocalToolTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(admin_scope())
        self.app = create_app()
        self.cases = self.app.state.case_service
        self.operations = self.app.state.business_operations
        self.proposals = self.app.state.proposal_service
        self.tools = self.app.state.local_tools
        self.customer = self.cases.create_customer(CustomerCreate(name="Tool demo"))
        self.order = self.cases.create_order(OrderCreate(customer_id=self.customer.id, items=[
            {"sku": "LAP-1", "name": "Laptop", "quantity": 1},
        ]))
        self.case = self.cases.create_case(CaseCreate(
            customer_id=self.customer.id, order_id=self.order.id,
            subject="Wrong item", description="Review the delivered item.",
        ))

    def proposal_input(self):
        return {"case_id": str(self.case.id), "action": "replacement", "rationale": "Request review only."}

    def assessment_input(self):
        return {"order_id": str(self.order.id), "customer_id": str(self.customer.id),
                "sku": "LAP-1", "quantity": 1, "days_since_delivery": 31,
                "reason": "wrong_item", "condition": "unused"}

    def test_exact_allowlist_and_typed_schemas(self):
        descriptions = self.tools.describe()
        self.assertEqual({d.name for d in descriptions}, {
            "search_knowledge",
            "get_customer", "get_order", "get_case", "get_inventory", "get_policy",
            "assess_eligibility", "assess_refund_eligibility",
            "create_resolution_proposal", "get_proposal_status",
        })
        for description in descriptions:
            self.assertFalse(description.input_schema["additionalProperties"])
            self.assertEqual(description.read_only, description.name != "create_resolution_proposal")
            self.assertIn("properties", description.output_schema)
            json.loads(description.model_dump_json())
        descriptions[0].input_schema.clear()
        self.assertIn("properties", self.tools.describe()[0].input_schema)

    def test_customer_order_case_inventory_policy_delegation(self):
        checks = [
            ("get_customer", {"customer_id": str(self.customer.id)}, self.customer),
            ("get_order", {"order_id": str(self.order.id)}, self.order),
            ("get_case", {"case_id": str(self.case.id)}, self.case),
            ("get_inventory", {"sku": "LAP-1"}, self.operations.get_inventory("LAP-1")),
            ("get_policy", {"policy_id": "standard-return"}, self.operations.get_policy("standard-return")),
        ]
        for name, arguments, expected in checks:
            with self.subTest(name=name):
                result = self.tools.invoke(name, arguments)
                self.assertTrue(result.ok)
                self.assertEqual(result.data, expected)
                self.assertEqual(json.loads(result.model_dump_json())["tool"], name)

    def test_lookup_errors_are_structured(self):
        for name, arguments in (
            ("get_customer", {"customer_id": str(uuid4())}),
            ("get_order", {"order_id": str(uuid4())}),
            ("get_case", {"case_id": str(uuid4())}),
            ("get_inventory", {"sku": "UNKNOWN"}),
            ("get_policy", {"policy_id": "UNKNOWN"}),
            ("get_proposal_status", {"proposal_id": str(uuid4())}),
        ):
            with self.subTest(name=name):
                result = self.tools.invoke(name, arguments)
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, "NOT_FOUND")
                self.assertNotIn("data", result.model_dump())

    def test_invalid_inputs_do_not_reach_services(self):
        with patch.object(self.cases, "get_order", wraps=self.cases.get_order) as lookup:
            for arguments in ({}, {"order_id": "bad"}, {"order_id": str(self.order.id), "approve": True}, None, []):
                with self.subTest(arguments=arguments):
                    result = self.tools.invoke("get_order", arguments)
                    self.assertEqual(result.error.code, "INVALID_INPUT")
                    self.assertTrue(result.error.issues)
            lookup.assert_not_called()

    def test_eligibility_delegates_and_preserves_ineligible_result(self):
        with patch.object(self.operations, "check_eligibility", wraps=self.operations.check_eligibility) as check:
            result = self.tools.invoke("assess_eligibility", self.assessment_input())
            check.assert_called_once()
            self.assertTrue(result.ok)  # Ineligible is a valid assessment, not a tool error.
            self.assertEqual(result.data.denial_reasons, ("outside_return_window",))
            self.assertFalse(result.data.refund_eligible)
            self.assertFalse(result.data.authorization_granted)
            self.assertEqual(check.call_args.args[0], self.order.id)
            self.assertEqual(check.call_args.args[1].days_since_delivery, 31)

    def test_eligibility_conflict_and_validation_are_not_reimplemented(self):
        other = self.cases.create_customer(CustomerCreate(name="Other"))
        conflict = self.tools.invoke("assess_eligibility", {
            **self.assessment_input(), "customer_id": str(other.id),
        })
        self.assertEqual(conflict.error.code, "CONFLICT")
        for changes in ({"quantity": True}, {"quantity": "1"}, {"days_since_delivery": -1},
                        {"reason": "other"}, {"status": "APPROVED"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.tools.invoke("assess_eligibility", {
                    **self.assessment_input(), **changes,
                }).error.code, "INVALID_INPUT")

    def test_creation_is_pending_and_shares_existing_service(self):
        with patch.object(self.proposals, "create", wraps=self.proposals.create) as create:
            tools = LocalTools(self.cases, self.operations, self.proposals)
            result = tools.invoke("create_resolution_proposal", self.proposal_input())
            create.assert_called_once()
        self.assertTrue(result.ok)
        self.assertEqual(result.data.status, ProposalStatus.PENDING_REVIEW)
        self.assertIsNone(result.data.reviewed_at)
        self.assertEqual(self.proposals.get(result.data.id), result.data)
        status = self.tools.invoke("get_proposal_status", {"proposal_id": str(result.data.id)})
        self.assertEqual(status.data, result.data)

    def test_proposal_input_cannot_inject_server_or_review_fields(self):
        for changes in ({"status": "APPROVED"}, {"reviewer_name": "Injected"},
                        {"id": str(uuid4())}, {"created_at": "2026-01-01"},
                        {"reviewed_at": "2026-01-01"}, {"action": "execute_refund"}, {"rationale": " "}):
            with self.subTest(changes=changes):
                result = self.tools.invoke("create_resolution_proposal", {**self.proposal_input(), **changes})
                self.assertEqual(result.error.code, "INVALID_INPUT")
        self.assertEqual(self.proposals.list_for_case(self.case.id), [])
        missing = self.tools.invoke("create_resolution_proposal", {**self.proposal_input(), "case_id": str(uuid4())})
        self.assertEqual(missing.error.code, "NOT_FOUND")

    def test_review_execution_and_arbitrary_service_names_are_unavailable(self):
        for name in ("approve", "reject", "review", "create_refund", "execute", "update_status",
                     "proposal_service.review", "__dict__", "describe"):
            with self.subTest(name=name):
                result = self.tools.invoke(name, {"status": "APPROVED"})
                self.assertEqual(result.error.code, "UNKNOWN_TOOL")
        self.assertEqual(self.proposals.list_for_case(self.case.id), [])

    def test_status_reflects_separate_human_review_without_review_tool(self):
        proposal = self.tools.invoke("create_resolution_proposal", self.proposal_input()).data
        self.proposals.review(proposal.id, ProposalStatus.REJECTED,
                              HumanReview(reviewer_name="Local reviewer", note="Separate human operation."))
        result = self.tools.invoke("get_proposal_status", {"proposal_id": str(proposal.id)})
        self.assertEqual(result.data.status, ProposalStatus.REJECTED)
        self.assertEqual(self.cases.get_case(self.case.id), self.case)
        self.assertEqual(self.cases.get_order(self.order.id), self.order)
        self.assertEqual(self.operations.get_inventory("LAP-1").available_quantity, 5)

    def test_app_instances_do_not_share_tool_data(self):
        isolated = create_app().state.local_tools.invoke("get_case", {"case_id": str(self.case.id)})
        self.assertEqual(isolated.error.code, "NOT_FOUND")

    def test_unexpected_service_failure_is_not_reported_as_business_result(self):
        with patch.object(self.cases, "get_case", side_effect=RuntimeError("Unexpected defect")):
            with self.assertRaises(RuntimeError):
                self.tools.invoke("get_case", {"case_id": str(self.case.id)})


if __name__ == "__main__":
    unittest.main()
