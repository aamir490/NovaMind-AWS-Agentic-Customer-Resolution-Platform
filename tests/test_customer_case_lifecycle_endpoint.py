"""Offline customer lifecycle projections never expose workflow internals."""

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from backend.app.domain import CaseStatus, Customer, SupportCase
from backend.app.customer_case_routes import create_my_case
from backend.app.main import create_app
from backend.app.routes import get_customer_lifecycle
from backend.app.schemas import CaseCreate, CustomerCaseCreate, OrderCreate
from backend.app.security import (
    AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role,
    SecurityError, authenticated,
)
from backend.app.workflow_metadata import (
    WorkflowMetadataStatus, WorkflowRunReservation,
)


ADMIN_ID = UUID("00000000-0000-4000-8000-000000000201")
CUSTOMER_A_ID = UUID("00000000-0000-4000-8000-000000000202")
CUSTOMER_B_ID = UUID("00000000-0000-4000-8000-000000000203")
CUSTOMER_A_USER_ID = UUID("00000000-0000-4000-8000-000000000204")
CUSTOMER_B_USER_ID = UUID("00000000-0000-4000-8000-000000000205")

ADMIN_TOKEN = "lifecycle-admin-token"
CUSTOMER_A_TOKEN = "lifecycle-customer-a-token"
CUSTOMER_B_TOKEN = "lifecycle-customer-b-token"


class CustomerCaseLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "lifecycle.sqlite"
        self.auth = LocalAuthenticationProvider((
            LocalCredential(token=ADMIN_TOKEN,
                            identity=AuthenticatedIdentity(user_id=ADMIN_ID, role=Role.ADMIN)),
            LocalCredential(token=CUSTOMER_A_TOKEN, identity=AuthenticatedIdentity(
                user_id=CUSTOMER_A_USER_ID, role=Role.CUSTOMER, customer_id=CUSTOMER_A_ID)),
            LocalCredential(token=CUSTOMER_B_TOKEN, identity=AuthenticatedIdentity(
                user_id=CUSTOMER_B_USER_ID, role=Role.CUSTOMER, customer_id=CUSTOMER_B_ID)),
        ))

    def app(self):
        app = create_app(auth_provider=self.auth, sqlite_path=self.path, startup_customers=(
            Customer(id=CUSTOMER_A_ID, name="Customer A"),
            Customer(id=CUSTOMER_B_ID, name="Customer B"),
        ))
        if app.state.investigation_executor is not None:
            self.addCleanup(app.state.investigation_executor.close)
        self.addCleanup(app.state.frontend_runtime.close)
        return app

    def strands_app(self):
        """Create an independent Strands API instance over the shared durable store.

        The offline test uses SQLite only as a disposable durable adapter.  The
        lifecycle wiring is the same one used by PostgreSQL + Strands: the
        Strands metadata repository is exposed as
        ``investigation_workflow_metadata`` and the legacy LangGraph slot is
        unset.
        """
        app = create_app(
            auth_provider=self.auth, agent_engine="strands", sqlite_path=self.path,
            strands_model_factory=lambda: object(),
            startup_customers=(
                Customer(id=CUSTOMER_A_ID, name="Customer A"),
                Customer(id=CUSTOMER_B_ID, name="Customer B"),
            ),
        )
        self.addCleanup(app.state.frontend_runtime.close)
        return app

    @staticmethod
    def headers(token):
        return {"Authorization": f"Bearer {token}"}

    def create_case(self, app, *, subject="Support request", description="Please investigate this request."):
        order = app.state.case_service.create_order(OrderCreate(
            customer_id=CUSTOMER_A_ID,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))
        return app.state.case_service.create_case(CaseCreate(
            customer_id=CUSTOMER_A_ID, order_id=order.id,
            subject=subject, description=description,
        ))

    def create_strands_case(self, app):
        order = app.state.case_service.create_order(OrderCreate(
            customer_id=CUSTOMER_A_ID,
            items=[{"sku": "STRANDS-1", "name": "Strands test item", "quantity": 1}],
        ))
        request = type("Request", (), {"app": app})
        with authenticated(self.auth, CUSTOMER_A_TOKEN):
            return create_my_case(
                CustomerCaseCreate(
                    order_id=order.id, subject="Strands lifecycle",
                    description="Customer portal lifecycle regression",
                ),
                request, app.state.case_service, uuid4(),
            )

    def complete_strands_investigation(self, app, support_case):
        job = app.state.investigation_jobs.get_by_case(support_case.id)
        worker_id = UUID("00000000-0000-4000-8000-000000000299")
        claim = app.state.investigation_jobs.claim(job.id, worker_id=worker_id)
        self.assertIsNotNone(claim)
        reservation = app.state.investigation_jobs.bind_claim(
            job.id, worker_id=worker_id, claim_id=claim.claim_id, provider="bedrock",
        )
        return app.state.investigation_workflow_metadata.transition(
            reservation.run.workflow_id, owner_user_id=worker_id,
            case_id=support_case.id, expected_status=WorkflowMetadataStatus.RUNNING,
            status=WorkflowMetadataStatus.COMPLETED,
        )

    def completed_workflow(self, app, support_case):
        reservation = WorkflowRunReservation(
            workflow_id=uuid4(), run_id=uuid4(), case_id=support_case.id,
            owner_user_id=ADMIN_ID, request_id=uuid4(),
            request_fingerprint=sha256(str(uuid4()).encode()).hexdigest(),
            provider="local_scripted",
        )
        run = app.state.workflow_metadata.reserve(reservation).run
        return app.state.workflow_metadata.transition(
            run.workflow_id, owner_user_id=ADMIN_ID, case_id=support_case.id,
            expected_status=WorkflowMetadataStatus.RUNNING,
            status=WorkflowMetadataStatus.COMPLETED,
        )

    def lifecycle(self, app, case_id, token=CUSTOMER_A_TOKEN):
        # Invoke the route under the real authentication context without
        # starting a socket listener; the FastAPI route registration is checked
        # below and the route itself performs the ownership check.
        with authenticated(self.auth, token):
            return get_customer_lifecycle(case_id, type("Request", (), {"app": app})(),
                                          app.state.case_service)

    def test_completed_investigation_keeps_case_open_and_hides_internal_data(self):
        app = self.app()
        support_case = self.create_case(app)
        self.completed_workflow(app, support_case)
        response = self.lifecycle(app, support_case.id).model_dump(mode="json")
        self.assertEqual(response, {
            "case_id": str(support_case.id), "case_status": "open",
            "progress": "AI_REVIEW_COMPLETED",
            "message": "AI review completed. Your request is awaiting our support team's decision.",
            "updated_at": response["updated_at"],
        })
        self.assertNotIn("workflow", str(response).lower())
        self.assertNotIn("provider", str(response).lower())
        self.assertNotIn("draft", str(response).lower())
        self.assertEqual(app.state.case_service.get_case(support_case.id).status, CaseStatus.OPEN)

    def test_authorized_decision_updates_customer_projection_and_persists_restart(self):
        first = self.app()
        support_case = self.create_case(first)
        self.completed_workflow(first, support_case)
        approved_message = "Please upload a clear photo of the item so our support team can continue the review."
        first.state.case_service.resolve_case(
            support_case.id, action="REQUEST_MORE_EVIDENCE", reason="Evidence is incomplete.",
            customer_message=approved_message, admin_user_id=ADMIN_ID,
            evidence_reviewed=False,
        )
        response = self.lifecycle(first, support_case.id).model_dump(mode="json")
        self.assertEqual(response["case_status"], "in_review")
        self.assertEqual(response["progress"], "ADDITIONAL_EVIDENCE_REQUIRED")
        self.assertEqual(response["message"], approved_message)

        restarted = self.app()
        restored = self.lifecycle(restarted, support_case.id).model_dump(mode="json")
        self.assertEqual(restored["message"], approved_message)
        self.assertEqual(restored["case_status"], "in_review")

    def test_evidence_request_projects_the_exact_persisted_message_without_concatenation(self):
        app = self.app()
        support_case = self.create_case(app, subject="Message integrity")
        stored_message = "Please provide a clear photo of the damage. Your case has been exThank you."
        app.state.case_service.resolve_case(
            support_case.id, action="REQUEST_MORE_EVIDENCE", reason="Evidence is incomplete.",
            customer_message=stored_message, admin_user_id=ADMIN_ID,
            evidence_reviewed=False,
        )
        projected = self.lifecycle(app, support_case.id).model_dump(mode="json")
        self.assertEqual(projected["progress"], "ADDITIONAL_EVIDENCE_REQUIRED")
        # Lifecycle must not alter an immutable, already-approved decision.
        self.assertEqual(projected["message"], stored_message)
        self.assertEqual(
            app.state.case_service.resolution_history(support_case.id)[-1].customer_message,
            stored_message,
        )

    def test_failed_and_recovery_states_are_safe_and_customer_ownership_is_enforced(self):
        app = self.app()
        failed_case = self.create_case(app, subject="Failed investigation")
        reservation = WorkflowRunReservation(
            workflow_id=uuid4(), run_id=uuid4(), case_id=failed_case.id,
            owner_user_id=ADMIN_ID, request_id=uuid4(),
            request_fingerprint=sha256(str(uuid4()).encode()).hexdigest(),
            provider="local_scripted",
        )
        run = app.state.workflow_metadata.reserve(reservation).run
        app.state.workflow_metadata.transition(
            run.workflow_id, owner_user_id=ADMIN_ID, case_id=failed_case.id,
            expected_status=WorkflowMetadataStatus.RUNNING, status=WorkflowMetadataStatus.FAILED,
            failure_category="WORKFLOW_ERROR", failure_stage="WORKFLOW",
        )
        now = datetime.now(timezone.utc)
        recovery_case = SupportCase(
            id=uuid4(), customer_id=CUSTOMER_A_ID,
            order_id=failed_case.order_id, subject="Recovery investigation",
            description="The request needs a support-team review.", status=CaseStatus.OPEN,
            created_at=now, updated_at=now,
        )
        persisted, recovery_job, _ = app.state.investigation_jobs.submit(
            recovery_case, owner_user_id=CUSTOMER_A_USER_ID, idempotency_key=uuid4(),
        )
        claim = app.state.investigation_jobs.claim(recovery_job.id, worker_id=ADMIN_ID)
        self.assertIsNotNone(claim)
        app.state.investigation_jobs.require_recovery(
            recovery_job.id, worker_id=ADMIN_ID, claim_id=claim.claim_id,
        )
        failed = self.lifecycle(app, failed_case.id).model_dump(mode="json")
        recovery = self.lifecycle(app, persisted.id).model_dump(mode="json")
        with self.assertRaises(SecurityError):
            self.lifecycle(app, failed_case.id, CUSTOMER_B_TOKEN)
        for response in (failed, recovery):
            self.assertEqual(response["progress"], "AWAITING_ADMIN_DECISION")
            self.assertEqual(response["message"],
                             "Your request requires additional review from our support team.")
            self.assertNotIn("ERROR", str(response))
            self.assertNotIn("workflow", str(response).lower())

    def test_all_seven_issue_categories_receive_the_same_safe_completed_projection(self):
        app = self.app()
        categories = (
            "Damaged", "Wrong item", "Missing item", "Delayed delivery",
            "Malfunction", "Return", "Refund not received",
        )
        cases = []
        for category in categories:
            support_case = self.create_case(app, subject=category,
                description=f"Please investigate this {category.lower()} request.")
            self.completed_workflow(app, support_case)
            cases.append(support_case)
        for support_case in cases:
            response = self.lifecycle(app, support_case.id).model_dump(mode="json")
            self.assertEqual(response["progress"], "AI_REVIEW_COMPLETED")
            self.assertEqual(response["case_status"], "open")

    def test_completed_strands_investigation_projects_after_restart_and_from_another_api_instance(self):
        first = self.strands_app()
        support_case = self.create_strands_case(first)
        self.complete_strands_investigation(first, support_case)

        # The Strands worker writes durable metadata/results, not the
        # process-local LangGraph workflow slot.
        self.assertIsNone(first.state.workflow_metadata)
        self.assertIsNotNone(first.state.investigation_workflow_metadata)

        first_response = self.lifecycle(first, support_case.id).model_dump(mode="json")
        self.assertEqual(first_response["progress"], "AI_REVIEW_COMPLETED")
        self.assertEqual(first_response["case_status"], "open")

        # A fresh app instance must project the same customer-safe state from
        # the shared durable store rather than from the first process memory.
        restarted = self.strands_app()
        restarted_response = self.lifecycle(restarted, support_case.id).model_dump(mode="json")
        self.assertEqual(restarted_response["progress"], "AI_REVIEW_COMPLETED")
        self.assertEqual(restarted_response["case_status"], "open")

        independent = self.strands_app()
        independent_response = self.lifecycle(independent, support_case.id).model_dump(mode="json")
        self.assertEqual(independent_response["progress"], "AI_REVIEW_COMPLETED")
        self.assertEqual(independent_response["case_status"], "open")
        self.assertEqual(restarted_response["message"], first_response["message"])
        self.assertEqual(independent_response["message"], first_response["message"])
        with self.assertRaises(SecurityError):
            self.lifecycle(independent, support_case.id, CUSTOMER_B_TOKEN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
