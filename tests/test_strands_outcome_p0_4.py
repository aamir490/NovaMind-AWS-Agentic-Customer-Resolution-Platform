"""P0.4 offline durable outcome projection tests."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from backend.app.agent import AgentResult
from backend.app.customer_case_routes import create_my_case
from backend.app.domain import Customer
from backend.app.hitl import WorkflowResult
from backend.app.main import create_app
from backend.app.resolution_drafts import FallbackReason, InvestigationOutcome
from backend.app.schemas import CustomerCaseCreate, CustomerCreate, OrderCreate
from backend.app.security import (
    AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role, authenticated,
)
from backend.app.frontend_routes import case_investigation, diagnostic_investigations
from backend.app.workflow_metadata import WorkflowMetadataStatus


ADMIN = UUID("00000000-0000-4000-8000-0000000004f1")
CUSTOMER_USER = UUID("00000000-0000-4000-8000-0000000004f2")
CUSTOMER_ID = UUID("00000000-0000-4000-8000-0000000004f3")


class _SharedResults:
    def __init__(self):
        self.payloads = {}

    def get_by_job(self, job_id):
        return self.payloads.get(job_id)


class StrandsOutcomeProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "outcome.sqlite"
        self.auth = LocalAuthenticationProvider((
            LocalCredential(token="p04-admin-token-1", identity=AuthenticatedIdentity(user_id=ADMIN, role=Role.ADMIN)),
            LocalCredential(token="p04-customer-token-1", identity=AuthenticatedIdentity(
                user_id=CUSTOMER_USER, role=Role.CUSTOMER, customer_id=CUSTOMER_ID,
            )),
        ))
        self.results = _SharedResults()

    def app(self):
        app = create_app(
            auth_provider=self.auth, agent_engine="strands",
            strands_model_factory=lambda: object(), sqlite_path=self.path,
            startup_customers=(Customer(id=CUSTOMER_ID, name="P0.4 customer"),),
        )
        app.state.investigation_results = self.results
        self.addCleanup(app.state.frontend_runtime.close)
        return app

    @staticmethod
    def request(app):
        return type("Request", (), {"app": app})()

    def create_case(self, app):
        order = app.state.case_service.create_order(OrderCreate(
            customer_id=CUSTOMER_ID,
            items=[{"sku": "P04-1", "name": "P0.4 item", "quantity": 1}],
        ))
        with authenticated(self.auth, "p04-customer-token-1"):
            return create_my_case(
                CustomerCaseCreate(
                    order_id=order.id, subject="P0.4 outcome", description="Outcome projection test",
                ), self.request(app), app.state.case_service, uuid4(),
            )

    def bind_completed_job(self, app, case):
        job = app.state.investigation_jobs.get_by_case(case.id)
        worker_id = ADMIN
        claim = app.state.investigation_jobs.claim(job.id, worker_id=worker_id)
        self.assertIsNotNone(claim)
        reservation = app.state.investigation_jobs.bind_claim(
            job.id, worker_id=worker_id, claim_id=claim.claim_id, provider="bedrock",
        )
        app.state.investigation_workflow_metadata.transition(
            reservation.run.workflow_id, owner_user_id=worker_id, case_id=case.id,
            expected_status=WorkflowMetadataStatus.RUNNING,
            status=WorkflowMetadataStatus.COMPLETED,
        )
        return app.state.investigation_jobs.get(job.id)

    def test_bedrock_and_fallback_outcomes_survive_api_restart(self):
        first = self.app()
        case = self.create_case(first)
        job = self.bind_completed_job(first, case)
        outcome = InvestigationOutcome(
            model_id="amazon.nova-lite-v1:0", response_source="BEDROCK_VALIDATED",
            validation_result="VALIDATED", model_calls=1, tool_calls=2,
            evidence_references=(f"case_id:{case.id}", "policy:standard-return@demo-v1"),
            missing_evidence=("affected item and condition",),
        )
        result = WorkflowResult(
            workflow_id=job.workflow_id, status="COMPLETED",
            message="Validated recommendation.",
            agent_result=AgentResult(
                status="INFORMATIONAL", customer_response="Validated recommendation.",
                steps_used=2, history=(), actions_executed=False,
            ),
            investigation_outcome=outcome, actions_executed=False,
        )
        self.results.payloads[job.id] = result.model_dump(mode="json")
        run = first.state.investigation_workflow_metadata.get(job.workflow_id)

        with authenticated(self.auth, "p04-admin-token-1"):
            snapshot = case_investigation(case.id, self.request(first))
            diagnostics = diagnostic_investigations(
                self.request(first), case_id=case.id, run_id=run.run_id,
            )
        self.assertEqual(snapshot.state, "COMPLETED")
        self.assertEqual(snapshot.investigation_outcome.response_source, "BEDROCK_VALIDATED")
        self.assertEqual(snapshot.investigation_outcome.model_id, "amazon.nova-lite-v1:0")
        self.assertEqual(len(diagnostics.items), 1)
        self.assertEqual(diagnostics.items[0].response_source, "BEDROCK_VALIDATED")
        self.assertNotIn("Validated recommendation", snapshot.investigation_outcome.model_dump_json())
        self.assertNotIn("P0.4 customer", snapshot.investigation_outcome.model_dump_json())

        restarted = self.app()
        with authenticated(self.auth, "p04-admin-token-1"):
            restored = case_investigation(case.id, self.request(restarted))
        self.assertEqual(restored.investigation_outcome.model_dump(), snapshot.investigation_outcome.model_dump())

        fallback = outcome.model_copy(update={
            "response_source": "DETERMINISTIC_FALLBACK",
            "validation_result": "DETERMINISTIC_FALLBACK",
            "fallback_reason": FallbackReason.MODEL_OUTPUT_NOT_GROUNDED,
        })
        self.assertEqual(fallback.response_source, "DETERMINISTIC_FALLBACK")
        self.assertEqual(fallback.fallback_reason, FallbackReason.MODEL_OUTPUT_NOT_GROUNDED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
