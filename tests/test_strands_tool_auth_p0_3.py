"""P0.3 offline reproduction for Strands tool authorization boundaries."""

import sys
import types
import unittest
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from backend.app.agent import AgentConfig, AgentRequest
from backend.app.domain import InvestigationExecutionContext
from backend.app.operations import BusinessOperations
from backend.app.proposals import ProposalService
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.security import trusted_investigation_execution
from backend.app.service import CaseService
from backend.app.strands_agent import StrandsWorkflow
from backend.app.tools import LocalTools


WORKER_ID = UUID("00000000-0000-4000-8000-0000000003f1")


class _FakeResult:
    def __init__(self):
        self.metrics = Mock(tool_count=1)

    def __str__(self):
        return "not-json"


class _CallbackAgent:
    instances = []

    def __init__(self, *, tools=None, **kwargs):
        self.tools = tools or []
        self.selected_result = None
        type(self).instances.append(self)

    def __call__(self, prompt, **kwargs):
        request = json.loads(prompt)["request"]
        case_id = request["case_id"]
        by_name = {getattr(tool, "__name__", ""): tool for tool in self.tools}

        def select_tool_from_sdk_callback():
            # Simulate an SDK callback thread that does not inherit the
            # worker's ContextVar context.
            case = by_name["get_case"](case_id=case_id)
            self.selected_result = {"case": case}
            if "error" not in case:
                self.selected_result["order"] = by_name["get_order"](order_id=case["order_id"])
                self.selected_result["customer"] = by_name["get_customer"](customer_id=case["customer_id"])

        callback = Thread(target=select_tool_from_sdk_callback)
        callback.start()
        callback.join()
        return _FakeResult()


class StrandsToolAuthBoundaryTests(unittest.TestCase):
    def setUp(self):
        _CallbackAgent.instances = []
        self.strands = types.ModuleType("strands")
        self.strands.Agent = _CallbackAgent
        self.strands.tool = lambda function, **kwargs: function
        self.enterContext(patch.dict(sys.modules, {"strands": self.strands}))

        self.cases = CaseService()
        customer = self.cases.create_customer(CustomerCreate(name="P0.3 customer"))
        self.customer_id = customer.id
        self.order = self.cases.create_order(OrderCreate(
            customer_id=customer.id,
            items=[{"sku": "P03-1", "name": "P0.3 item", "quantity": 1}],
        ))
        self.case = self.cases.create_case(CaseCreate(
            customer_id=customer.id, order_id=self.order.id,
            subject="P0.3 auth boundary", description="Tool authorization boundary test",
        ))
        other_customer = self.cases.create_customer(CustomerCreate(name="P0.3 other customer"))
        self.other_customer_id = other_customer.id
        self.other_order = self.cases.create_order(OrderCreate(
            customer_id=other_customer.id,
            items=[{"sku": "P03-2", "name": "Other item", "quantity": 1}],
        ))
        self.other_case = self.cases.create_case(CaseCreate(
            customer_id=other_customer.id, order_id=self.other_order.id,
            subject="Other P0.3 case", description="Other customer record",
        ))
        operations = BusinessOperations(self.cases)
        proposals = ProposalService(self.cases, operations)
        self.tools = LocalTools(self.cases, operations, proposals)
        self.context = InvestigationExecutionContext(
            job_id=uuid4(), execution_id=uuid4(), execution_attempt_id=uuid4(),
            workflow_id=uuid4(), worker_id=WORKER_ID, customer_id=self.customer_id,
            case_id=self.case.id, order_id=self.order.id,
            agent_request_json="{}", agent_request_fingerprint="0" * 64,
        )

    @staticmethod
    def context_for(customer_id, case_id, order_id):
        return InvestigationExecutionContext(
            job_id=uuid4(), execution_id=uuid4(), execution_attempt_id=uuid4(),
            workflow_id=uuid4(), worker_id=WORKER_ID, customer_id=customer_id,
            case_id=case_id, order_id=order_id,
            agent_request_json="{}", agent_request_fingerprint="0" * 64,
        )

    def workflow(self):
        return StrandsWorkflow(
            self.tools, model_factory=lambda: Mock(),
            config=AgentConfig(max_steps=4),
        )

    def request(self):
        return AgentRequest(case_id=self.case.id, message="Investigate this case")

    def test_model_selected_callback_preserves_worker_identity_and_bound_access(self):
        with trusted_investigation_execution(self.context):
            result = self.workflow().run_durable(self.request(), workflow_id=self.context.workflow_id)

        self.assertEqual(result.status, "COMPLETED")
        selected = _CallbackAgent.instances[-1].selected_result
        self.assertEqual(selected["case"]["id"], str(self.case.id))
        self.assertEqual(selected["order"]["id"], str(self.order.id))
        self.assertEqual(selected["customer"]["id"], str(self.customer_id))

    def test_missing_identity_and_wrong_bound_ids_remain_denied(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            unauthenticated = pool.submit(
                self.tools.invoke, "get_case", {"case_id": str(self.case.id)},
            ).result()
        self.assertEqual(unauthenticated.error.code, "UNAUTHENTICATED")

        with trusted_investigation_execution(self.context):
            tools = self.workflow()._make_strands_tools()
        by_name = {getattr(tool, "__name__", ""): tool for tool in tools}

        def invoke_wrong_ids():
            return {
                "case": by_name["get_case"](case_id=str(self.other_case.id)),
                "order": by_name["get_order"](order_id=str(self.other_order.id)),
                "customer": by_name["get_customer"](customer_id=str(self.other_customer_id)),
            }

        with ThreadPoolExecutor(max_workers=1) as pool:
            denied = pool.submit(invoke_wrong_ids).result()
        self.assertEqual(denied["case"]["error"], "FORBIDDEN")
        self.assertEqual(denied["order"]["error"], "FORBIDDEN")
        self.assertEqual(denied["customer"]["error"], "FORBIDDEN")

    def test_concurrent_investigations_do_not_leak_identity_or_case_scope(self):
        second_context = self.context_for(
            self.other_customer_id, self.other_case.id, self.other_order.id,
        )
        cases = ((self.case, self.context), (self.other_case, second_context))

        def run(item):
            support_case, context = item
            with trusted_investigation_execution(context):
                return self.workflow().run_durable(
                    AgentRequest(case_id=support_case.id, message="Concurrent investigation"),
                    workflow_id=context.workflow_id,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, cases))
        self.assertTrue(all(result.status == "COMPLETED" for result in results))

        selected_by_case = {
            agent.selected_result["case"]["id"]: agent.selected_result
            for agent in _CallbackAgent.instances
            if agent.selected_result is not None and "error" not in agent.selected_result["case"]
        }
        self.assertEqual(set(selected_by_case), {str(self.case.id), str(self.other_case.id)})
        self.assertEqual(selected_by_case[str(self.case.id)]["customer"]["id"], str(self.customer_id))
        self.assertEqual(selected_by_case[str(self.other_case.id)]["customer"]["id"], str(self.other_customer_id))


if __name__ == "__main__":
    unittest.main(verbosity=2)
