"""Admin diagnostics expose durable metadata separately from process telemetry."""

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from backend.app.domain import Customer
from backend.app.frontend_routes import admin_analytics, diagnostic_events, diagnostic_investigations
from backend.app.frontend_runtime import FrontendError
from backend.app.main import create_app
from backend.app.schemas import CaseCreate, OrderCreate
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role, SecurityError, authenticated
from backend.app.workflow_metadata import WorkflowMetadataStatus, WorkflowRunReservation


ADMIN = UUID("00000000-0000-4000-8000-000000000301")
REVIEWER = UUID("00000000-0000-4000-8000-000000000302")
CUSTOMER = UUID("00000000-0000-4000-8000-000000000303")


class AdminDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth = LocalAuthenticationProvider((
            LocalCredential(token="diag-admin-token-0001", identity=AuthenticatedIdentity(user_id=ADMIN, role=Role.ADMIN)),
            LocalCredential(token="diag-reviewer-token-0001", identity=AuthenticatedIdentity(user_id=REVIEWER, role=Role.REVIEWER)),
            LocalCredential(token="diag-customer-token-0001", identity=AuthenticatedIdentity(user_id=CUSTOMER, role=Role.CUSTOMER, customer_id=CUSTOMER)),
        ))
        self.app = create_app(auth_provider=self.auth, sqlite_path=str(Path(self.tmp.name) / "diag.sqlite"),
                              startup_customers=(Customer(id=CUSTOMER, name="Diagnostic Customer"),))
        self.addCleanup(self.app.state.frontend_runtime.close)
        if self.app.state.investigation_executor is not None:
            self.addCleanup(self.app.state.investigation_executor.close)

    @staticmethod
    def request(app):
        return type("Request", (), {"app": app})()

    def strands_app(self):
        """Create an independent Strands API instance over durable test state."""
        app = create_app(
            auth_provider=self.auth,
            agent_engine="strands",
            strands_model_factory=lambda: object(),
            sqlite_path=str(Path(self.tmp.name) / "diag-strands.sqlite"),
            startup_customers=(Customer(id=CUSTOMER, name="Diagnostic Customer"),),
        )
        self.addCleanup(app.state.frontend_runtime.close)
        return app

    @staticmethod
    def add_strands_run(app, status=WorkflowMetadataStatus.COMPLETED):
        order = app.state.case_service.create_order(OrderCreate(
            customer_id=CUSTOMER,
            items=[{"sku": "STRANDS-DIAG-1", "name": "Strands diagnostic item", "quantity": 1}],
        ))
        case_id = app.state.case_service.create_case(CaseCreate(
            customer_id=CUSTOMER, order_id=order.id, subject="Strands diagnostic case",
            description="A durable Strands diagnostic test case.",
        )).id
        reservation = WorkflowRunReservation(
            workflow_id=uuid4(), run_id=uuid4(), case_id=case_id, owner_user_id=ADMIN,
            request_id=uuid4(), request_fingerprint=sha256(uuid4().bytes).hexdigest(), provider="bedrock",
        )
        metadata = app.state.investigation_workflow_metadata
        run = metadata.reserve(reservation).run
        metadata.transition(run.workflow_id, owner_user_id=ADMIN, case_id=case_id,
                            expected_status=WorkflowMetadataStatus.RUNNING, status=status)
        return run

    def add_run(self, status, *, case_id=None):
        if case_id is None:
            order = self.app.state.case_service.create_order(OrderCreate(
                customer_id=CUSTOMER, items=[{"sku": "DIAG-1", "name": "Diagnostic item", "quantity": 1}],
            ))
            case_id = self.app.state.case_service.create_case(CaseCreate(
                customer_id=CUSTOMER, order_id=order.id, subject="Diagnostic case",
                description="A bounded diagnostic test case.",
            )).id
        reservation = WorkflowRunReservation(
            workflow_id=uuid4(), run_id=uuid4(), case_id=case_id, owner_user_id=ADMIN,
            request_id=uuid4(), request_fingerprint=sha256(uuid4().bytes).hexdigest(), provider="local_scripted",
        )
        run = self.app.state.workflow_metadata.reserve(reservation).run
        kwargs = {}
        if status is WorkflowMetadataStatus.FAILED:
            kwargs = {"failure_category": "WORKFLOW_ERROR", "failure_stage": "WORKFLOW"}
        self.app.state.workflow_metadata.transition(run.workflow_id, owner_user_id=ADMIN, case_id=case_id,
            expected_status=WorkflowMetadataStatus.RUNNING, status=status, **kwargs)
        return run

    def test_durable_runs_are_newest_first_and_survive_runtime_restart_boundary(self):
        older = self.add_run(WorkflowMetadataStatus.COMPLETED)
        newer = self.add_run(WorkflowMetadataStatus.FAILED)
        with authenticated(self.auth, "diag-admin-token-0001"):
            page = diagnostic_investigations(self.request(self.app), offset=0, limit=25)
        self.assertEqual([item.workflow_id for item in page.items[:2]], [newer.workflow_id, older.workflow_id])
        self.assertEqual(page.items[0].failure_category, "WORKFLOW_ERROR")
        self.assertEqual(page.items[0].error_source, "workflow_metadata")
        self.assertFalse(page.items[0].actions_executed)

    def test_strands_diagnostics_uses_durable_metadata_after_restart_and_keeps_ids(self):
        first = self.strands_app()
        run = self.add_strands_run(first)
        self.assertIsNone(first.state.workflow_metadata)
        self.assertIsNotNone(first.state.investigation_workflow_metadata)

        with authenticated(self.auth, "diag-admin-token-0001"):
            initial = diagnostic_investigations(self.request(first), case_id=run.case_id)
        self.assertEqual(len(initial.items), 1)
        self.assertEqual((initial.items[0].workflow_id, initial.items[0].run_id),
                         (run.workflow_id, run.run_id))
        self.assertEqual(initial.items[0].workflow_status, WorkflowMetadataStatus.COMPLETED)

        restarted = self.strands_app()
        with authenticated(self.auth, "diag-admin-token-0001"):
            restored = diagnostic_investigations(self.request(restarted), case_id=run.case_id)
        self.assertEqual((restored.items[0].workflow_id, restored.items[0].run_id),
                         (run.workflow_id, run.run_id))
        self.assertEqual(restored.items[0].workflow_status, WorkflowMetadataStatus.COMPLETED)

        events = restarted.state.investigation_workflow_metadata.events(run.workflow_id)
        self.assertEqual([event.status for event in events], [
            WorkflowMetadataStatus.RUNNING, WorkflowMetadataStatus.COMPLETED,
        ])
        self.assertEqual([event.workflow_id for event in events], [run.workflow_id, run.workflow_id])

        independent = self.strands_app()
        with authenticated(self.auth, "diag-admin-token-0001"):
            from_other_api = diagnostic_investigations(self.request(independent), run_id=run.run_id)
        self.assertEqual((from_other_api.items[0].workflow_id, from_other_api.items[0].run_id),
                         (run.workflow_id, run.run_id))
        with authenticated(self.auth, "diag-reviewer-token-0001"):
            with self.assertRaises(SecurityError):
                diagnostic_investigations(self.request(independent), run_id=run.run_id)

    def test_missing_durable_metadata_preserves_status_unavailable_contract(self):
        app = self.strands_app()
        app.state.investigation_workflow_metadata = None
        app.state.workflow_metadata = None
        with authenticated(self.auth, "diag-admin-token-0001"):
            with self.assertRaisesRegex(FrontendError, "INVESTIGATION_STATUS_UNAVAILABLE"):
                diagnostic_investigations(self.request(app))

    def test_durable_projection_is_admin_only_and_events_are_bounded_categories(self):
        self.add_run(WorkflowMetadataStatus.COMPLETED)
        with authenticated(self.auth, "diag-reviewer-token-0001"):
            with self.assertRaises(SecurityError):
                diagnostic_investigations(self.request(self.app), offset=0, limit=25)
        with authenticated(self.auth, "diag-admin-token-0001"):
            page = diagnostic_events(self.request(self.app), category="workflow", offset=0, limit=50)
        self.assertEqual(page.category, "workflow")
        self.assertEqual(page.scope, "process_metadata_only")
        self.assertLessEqual(len(page.events), 50)

    def test_timeline_source_projection_is_durable_and_closed(self):
        run = self.add_run(WorkflowMetadataStatus.FAILED)
        with authenticated(self.auth, "diag-admin-token-0001"):
            page = diagnostic_investigations(self.request(self.app), offset=0, limit=25,
                                             case_id=run.case_id, run_id=run.run_id)
        item = page.items[0]
        self.assertEqual((item.workflow_id, item.run_id, item.provider),
                         (run.workflow_id, run.run_id, "local_scripted"))
        self.assertEqual((item.failure_category, item.failure_stage, item.error_source),
                         ("WORKFLOW_ERROR", "WORKFLOW", "workflow_metadata"))
        payload = item.model_dump()
        self.assertNotIn("checkpoint", payload)
        self.assertNotIn("model_response", payload)
        self.assertFalse(item.actions_executed)

    def test_admin_analytics_is_bounded_and_reads_durable_aggregates(self):
        self.add_run(WorkflowMetadataStatus.COMPLETED)
        today = datetime.now(timezone.utc).date()
        with authenticated(self.auth, "diag-admin-token-0001"):
            result = admin_analytics(self.request(self.app), from_date=today, to_date=today)
        self.assertEqual(result.scope, "durable_analytics")
        self.assertGreaterEqual(result.workflow_count, 1)
        self.assertEqual(result.workflow_status_counts["COMPLETED"], 1)
        self.assertEqual(result.mode_counts, {"UNAVAILABLE": 1})
        self.assertEqual(result.response_source_counts["UNAVAILABLE"], 1)
        self.assertFalse(result.duration_measurement_sampled)
        with authenticated(self.auth, "diag-reviewer-token-0001"):
            with self.assertRaises(SecurityError):
                admin_analytics(self.request(self.app), from_date=today, to_date=today)
        with authenticated(self.auth, "diag-admin-token-0001"):
            with self.assertRaisesRegex(FrontendError, "INVALID_INPUT"):
                admin_analytics(self.request(self.app), from_date=today - timedelta(days=366), to_date=today)


if __name__ == "__main__":
    unittest.main(verbosity=2)
