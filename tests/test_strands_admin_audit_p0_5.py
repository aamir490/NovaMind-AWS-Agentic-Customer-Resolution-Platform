"""P0.5 regression coverage for durable Strands Admin Audit projections."""

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from backend.app.domain import Customer
from backend.app.frontend_routes import audit, list_runs
from backend.app.main import create_app
from backend.app.schemas import CaseCreate, OrderCreate
from backend.app.security import (
    AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role, SecurityError,
    authenticated,
)
from backend.app.workflow_metadata import WorkflowMetadataStatus, WorkflowRunReservation


ADMIN = UUID("00000000-0000-4000-8000-0000000005a1")
CUSTOMER = UUID("00000000-0000-4000-8000-0000000005a2")
OTHER_CUSTOMER = UUID("00000000-0000-4000-8000-0000000005a3")
ADMIN_TOKEN = "p05-admin-token-0001"
CUSTOMER_TOKEN = "p05-customer-token-0001"


class StrandsAdminAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "p05.sqlite"
        self.auth = LocalAuthenticationProvider((
            LocalCredential(token=ADMIN_TOKEN,
                            identity=AuthenticatedIdentity(user_id=ADMIN, role=Role.ADMIN)),
            LocalCredential(token=CUSTOMER_TOKEN,
                            identity=AuthenticatedIdentity(
                                user_id=CUSTOMER, role=Role.CUSTOMER, customer_id=CUSTOMER,
                            )),
        ))

    def app(self):
        app = create_app(
            auth_provider=self.auth,
            agent_engine="strands",
            strands_model_factory=lambda: object(),
            sqlite_path=self.path,
            startup_customers=(Customer(id=CUSTOMER, name="P0.5 customer"),
                               Customer(id=OTHER_CUSTOMER, name="Other customer")),
        )
        self.addCleanup(app.state.frontend_runtime.close)
        return app

    def seed_run(self, app):
        order = app.state.case_service.create_order(OrderCreate(
            customer_id=CUSTOMER,
            items=[{"sku": "P05-1", "name": "P0.5 item", "quantity": 1}],
        ))
        case = app.state.case_service.create_case(CaseCreate(
            customer_id=CUSTOMER,
            order_id=order.id,
            subject="Durable Strands audit",
            description="The run must remain visible after restart.",
        ))
        reservation = WorkflowRunReservation(
            workflow_id=uuid4(), run_id=uuid4(), case_id=case.id,
            owner_user_id=ADMIN, request_id=uuid4(),
            request_fingerprint=sha256(b"p0.5 durable audit").hexdigest(),
            provider="bedrock",
        )
        run = app.state.investigation_workflow_metadata.reserve(reservation).run
        run = app.state.investigation_workflow_metadata.transition(
            run.workflow_id, owner_user_id=ADMIN, case_id=case.id,
            expected_status=WorkflowMetadataStatus.RUNNING,
            status=WorkflowMetadataStatus.COMPLETED,
        )
        return case, run

    @staticmethod
    def request(app):
        return type("Request", (), {"app": app})()

    def test_strands_run_listing_and_audit_are_durable_after_restart(self):
        first = self.app()
        case, run = self.seed_run(first)
        first_request = self.request(first)

        # Before P0.5, this Strands run was absent from the process-local
        # LangGraph-backed Admin Audit view and its audit endpoint returned 404.
        with authenticated(self.auth, ADMIN_TOKEN):
            listed = list_runs(case.id, first.state.frontend_runtime)
            audit_page = audit(run.run_id, first.state.frontend_runtime)
        self.assertEqual(listed.scope, "case_runs_with_durable_metadata")
        self.assertEqual([(item.case_id, item.run_id, item.workflow_id) for item in listed.items],
                         [(case.id, run.run_id, run.workflow_id)])
        self.assertEqual(audit_page.scope, "durable_workflow_audit")
        self.assertEqual([event.sequence for event in audit_page.events], [1, 2])
        self.assertEqual([event.kind for event in audit_page.events], ["AGENT", "AGENT"])
        self.assertTrue(all(event.tool is None for event in audit_page.events))

        restarted = self.app()
        independent = self.app()
        for app in (restarted, independent):
            with authenticated(self.auth, ADMIN_TOKEN):
                restored = list_runs(case.id, app.state.frontend_runtime)
                restored_audit = audit(run.run_id, app.state.frontend_runtime)
            self.assertEqual(restored.items[0].case_id, case.id)
            self.assertEqual(restored.items[0].workflow_id, run.workflow_id)
            self.assertEqual(restored_audit.events, audit_page.events)

        with authenticated(self.auth, CUSTOMER_TOKEN):
            with self.assertRaises(SecurityError):
                audit(run.run_id, independent.state.frontend_runtime)


if __name__ == "__main__":
    unittest.main(verbosity=2)
