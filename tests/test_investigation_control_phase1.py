"""Phase 1 ADMIN AUTO/MANUAL investigation controls; offline providers only."""

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from backend.app.domain import Customer, InvestigationJobStatus, InvestigationMode
from backend.app.llm import FakeLLMProvider, LLMResponse
from backend.app.main import create_app
from backend.app.persistence import PersistenceError, SQLitePersistence
from backend.app.schemas import OrderCreate
from backend.app.security import (
    AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role,
)


CUSTOMER_ID = UUID("00000000-0000-4000-8000-000000000181")
CUSTOMER_USER_ID = UUID("00000000-0000-4000-8000-000000000182")
ADMIN_ID = UUID("00000000-0000-4000-8000-000000000183")
REVIEWER_ID = UUID("00000000-0000-4000-8000-000000000184")
DISPATCHER_ID = UUID("00000000-0000-4000-8000-000000000185")
CUSTOMER_TOKEN = "phase1-customer-token"
ADMIN_TOKEN = "phase1-admin-token"
REVIEWER_TOKEN = "phase1-reviewer-token"


class InvestigationControlPhase1Tests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.directory / "phase1.sqlite"
        self.providers = []
        self.auth = LocalAuthenticationProvider((
            LocalCredential(token=CUSTOMER_TOKEN, identity=AuthenticatedIdentity(
                user_id=CUSTOMER_USER_ID, role=Role.CUSTOMER, customer_id=CUSTOMER_ID)),
            LocalCredential(token=ADMIN_TOKEN, identity=AuthenticatedIdentity(
                user_id=ADMIN_ID, role=Role.ADMIN)),
            LocalCredential(token=REVIEWER_TOKEN, identity=AuthenticatedIdentity(
                user_id=REVIEWER_ID, role=Role.REVIEWER)),
        ))

    def app(self, *, automatic_capability=False):
        def factory():
            provider = FakeLLMProvider(LLMResponse(
                text='{"decision":{"kind":"final"}}', finish_reason="stop",
            ))
            self.providers.append(provider)
            return provider

        kwargs = {}
        if automatic_capability:
            kwargs = {
                "automatic_dispatcher": True,
                "dispatcher_identity": AuthenticatedIdentity(
                    user_id=DISPATCHER_ID, role=Role.REVIEWER,
                ),
            }
        app = create_app(
            auth_provider=self.auth, sqlite_path=self.path,
            startup_customers=(Customer(id=CUSTOMER_ID, name="Phase 1 customer"),),
            local_provider_factory=factory, **kwargs,
        )
        return app

    @staticmethod
    def headers(token, *, key=None):
        values = {"Authorization": f"Bearer {token}"}
        if key is not None:
            values["Idempotency-Key"] = str(key)
        return values

    @staticmethod
    def create_order(app):
        return app.state.case_service.create_order(OrderCreate(
            customer_id=CUSTOMER_ID,
            items=[{"sku": "INP-1", "name": "NovaControl Input 1", "quantity": 1}],
        ))

    def submit_case(self, client, order_id, *, key=None, subject="Intermittent input"):
        return client.post(
            "/api/my/cases", headers=self.headers(CUSTOMER_TOKEN, key=key or uuid4()),
            json={"order_id": str(order_id), "subject": subject,
                  "description": "The device intermittently stops responding."},
        )

    @staticmethod
    def wait_for_attempt(app, case_id):
        for _ in range(200):
            job = app.state.investigation_jobs.get_by_case(case_id)
            if job.execution_attempt_id is not None:
                return job
            time.sleep(0.01)
        raise AssertionError("Investigation did not start")

    def test_default_manual_mode_is_persistent_audited_and_role_protected(self):
        app = self.app(automatic_capability=True)
        with TestClient(app) as client:
            customer = client.get("/api/admin/investigation-control",
                                  headers=self.headers(CUSTOMER_TOKEN))
            reviewer = client.get("/api/admin/investigation-control",
                                  headers=self.headers(REVIEWER_TOKEN))
            forbidden_patch = client.patch(
                "/api/admin/investigation-control", headers=self.headers(REVIEWER_TOKEN, key=uuid4()),
                json={"mode": "AUTO", "expected_version": 1},
            )
            audit_denied = client.get(
                "/api/admin/investigation-control/audit", headers=self.headers(REVIEWER_TOKEN),
            )
            audit_allowed = client.get(
                "/api/admin/investigation-control/audit", headers=self.headers(ADMIN_TOKEN),
            )
        self.assertEqual(customer.status_code, 403)
        self.assertEqual(reviewer.status_code, 200, reviewer.text)
        self.assertEqual(reviewer.json()["configured_mode"], "MANUAL")
        self.assertEqual(reviewer.json()["effective_mode"], "MANUAL")
        self.assertTrue(reviewer.json()["automatic_capability_available"])
        self.assertEqual(forbidden_patch.status_code, 403)
        self.assertEqual(audit_denied.status_code, 403)
        self.assertEqual(audit_allowed.status_code, 200, audit_allowed.text)
        self.assertEqual(audit_allowed.json()["items"][0]["mode"], "MANUAL")
        self.assertNotIn("request_fingerprint", audit_allowed.text)
        self.assertNotIn("idempotency_key", audit_allowed.text)
        audit = app.state.investigation_control.audit()
        self.assertEqual([(event.previous_mode, event.mode, event.admin_user_id) for event in audit], [
            (None, InvestigationMode.MANUAL, None),
        ])

        reopened = self.app(automatic_capability=True)
        try:
            self.assertEqual(reopened.state.investigation_control.get().mode, InvestigationMode.MANUAL)
        finally:
            reopened.state.investigation_executor.close()
            reopened.state.frontend_runtime.close()

    def test_auto_switch_never_dispatches_backlog_and_only_new_intake_uses_server_worker(self):
        app = self.app(automatic_capability=True)
        order = self.create_order(app)
        mode_key = uuid4()
        with TestClient(app) as client:
            old = self.submit_case(client, order.id, subject="Manual backlog")
            self.assertEqual(old.status_code, 201, old.text)
            old_case_id = UUID(old.json()["id"])
            enabled = client.patch(
                "/api/admin/investigation-control",
                headers=self.headers(ADMIN_TOKEN, key=mode_key),
                json={"mode": "AUTO", "expected_version": 1},
            )
            retry = client.patch(
                "/api/admin/investigation-control",
                headers=self.headers(ADMIN_TOKEN, key=mode_key),
                json={"mode": "AUTO", "expected_version": 1},
            )
            self.assertEqual(enabled.status_code, 200, enabled.text)
            self.assertEqual(retry.status_code, 200, retry.text)
            conflicting_retry = client.patch(
                "/api/admin/investigation-control",
                headers=self.headers(ADMIN_TOKEN, key=mode_key),
                json={"mode": "MANUAL", "expected_version": 2},
            )
            self.assertEqual(conflicting_retry.status_code, 409, conflicting_retry.text)
            self.assertEqual(app.state.investigation_jobs.get_by_case(old_case_id).status,
                             InvestigationJobStatus.PENDING)
            self.assertEqual(self.providers, [])
            manual_while_auto = client.post(
                f"/api/cases/{old_case_id}/investigation",
                headers=self.headers(ADMIN_TOKEN, key=uuid4()),
            )
            self.assertEqual(manual_while_auto.status_code, 409, manual_while_auto.text)
            self.assertEqual(manual_while_auto.json()["detail"], "MANUAL_INVESTIGATION_DISABLED")

            fresh = self.submit_case(client, order.id, subject="Fresh automatic case")
            self.assertEqual(fresh.status_code, 201, fresh.text)
            fresh_case_id = UUID(fresh.json()["id"])
            disabled = client.patch(
                "/api/admin/investigation-control",
                headers=self.headers(ADMIN_TOKEN, key=uuid4()),
                json={"mode": "MANUAL", "expected_version": 2},
            )
            self.assertEqual(disabled.status_code, 200, disabled.text)
            old_status = client.get(
                f"/api/cases/{old_case_id}/investigation",
                headers=self.headers(ADMIN_TOKEN),
            )
            self.assertEqual(old_status.json()["state"], "PENDING")
            self.assertTrue(old_status.json()["can_start"])
        fresh_job = self.wait_for_attempt(app, fresh_case_id)
        self.assertEqual(fresh_job.submission_mode, InvestigationMode.AUTO)
        self.assertEqual(fresh_job.mode_version, 2)
        self.assertEqual(fresh_job.claimed_by, DISPATCHER_ID)
        self.assertEqual(app.state.investigation_jobs.get_by_case(old_case_id).status,
                         InvestigationJobStatus.PENDING)
        self.assertEqual(len(self.providers), 1)
        audit = app.state.investigation_control.audit()
        self.assertEqual(len(audit), 3)
        self.assertEqual(audit[-1].admin_user_id, ADMIN_ID)
        self.assertEqual(audit[-1].mode, InvestigationMode.MANUAL)
        restarted = self.app(automatic_capability=True)
        try:
            self.assertEqual(restarted.state.investigation_jobs.get_by_case(old_case_id).status,
                             InvestigationJobStatus.PENDING)
            self.assertEqual(len(self.providers), 1)
        finally:
            restarted.state.investigation_executor.close()
            restarted.state.frontend_runtime.close()

    def test_manual_admin_start_is_idempotent_and_uses_the_same_once_only_job(self):
        app = self.app()
        order = self.create_order(app)
        start_key = uuid4()
        with TestClient(app) as client:
            created = self.submit_case(client, order.id)
            self.assertEqual(created.status_code, 201, created.text)
            case_id = UUID(created.json()["id"])
            before = client.get(
                f"/api/cases/{case_id}/investigation", headers=self.headers(ADMIN_TOKEN),
            )
            self.assertEqual(before.status_code, 200, before.text)
            self.assertEqual(before.json()["state"], "PENDING")
            self.assertTrue(before.json()["can_start"])
            for token in (CUSTOMER_TOKEN, REVIEWER_TOKEN):
                denied = client.post(
                    f"/api/cases/{case_id}/investigation",
                    headers=self.headers(token, key=uuid4()),
                )
                self.assertEqual(denied.status_code, 403)
            first = client.post(
                f"/api/cases/{case_id}/investigation",
                headers=self.headers(ADMIN_TOKEN, key=start_key),
            )
            self.assertEqual(first.status_code, 202, first.text)
            job = self.wait_for_attempt(app, case_id)
            repeated = client.post(
                f"/api/cases/{case_id}/investigation",
                headers=self.headers(ADMIN_TOKEN, key=start_key),
            )
            self.assertEqual(repeated.status_code, 202, repeated.text)
        self.assertEqual(job.claimed_by, ADMIN_ID)
        self.assertEqual(job.submission_mode, InvestigationMode.MANUAL)
        self.assertEqual(len(self.providers), 1)
        current = app.state.investigation_jobs.get_by_case(case_id)
        self.assertEqual(current.execution_attempt_id, job.execution_attempt_id)

    def test_runs_route_cannot_bypass_a_durable_case_job(self):
        app = self.app()
        order = self.create_order(app)
        with TestClient(app) as client:
            created = self.submit_case(client, order.id)
            case_id = created.json()["id"]
            response = client.post("/api/runs", headers=self.headers(ADMIN_TOKEN), json={
                "instance_id": str(app.state.frontend_runtime.instance_id),
                "request_id": str(uuid4()), "case_id": case_id,
                "message": "Try to bypass the durable job path",
            })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "CASE_INVESTIGATION_ALREADY_EXISTS")
        self.assertEqual(self.providers, [])
        self.assertEqual(app.state.investigation_jobs.get_by_case(UUID(case_id)).status,
                         InvestigationJobStatus.PENDING)

    def test_auto_enable_fails_closed_without_server_capability(self):
        app = self.app(automatic_capability=False)
        with TestClient(app) as client:
            response = client.patch(
                "/api/admin/investigation-control",
                headers=self.headers(ADMIN_TOKEN, key=uuid4()),
                json={"mode": "AUTO", "expected_version": 1},
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "AUTO_INVESTIGATION_UNAVAILABLE")
        self.assertEqual(app.state.investigation_control.get().mode, InvestigationMode.MANUAL)
        self.assertEqual(len(app.state.investigation_control.audit()), 1)

    def test_persisted_auto_mode_degrades_without_capability_and_does_not_dispatch(self):
        capable = self.app(automatic_capability=True)
        with TestClient(capable) as client:
            enabled = client.patch(
                "/api/admin/investigation-control",
                headers=self.headers(ADMIN_TOKEN, key=uuid4()),
                json={"mode": "AUTO", "expected_version": 1},
            )
            self.assertEqual(enabled.status_code, 200, enabled.text)
        degraded = self.app(automatic_capability=False)
        order = self.create_order(degraded)
        with TestClient(degraded) as client:
            control = client.get(
                "/api/admin/investigation-control", headers=self.headers(ADMIN_TOKEN),
            )
            created = self.submit_case(client, order.id)
        self.assertEqual(control.status_code, 200, control.text)
        self.assertEqual(control.json()["configured_mode"], "AUTO")
        self.assertEqual(control.json()["effective_mode"], "MANUAL")
        self.assertFalse(control.json()["automatic_capability_available"])
        job = degraded.state.investigation_jobs.get_by_case(UUID(created.json()["id"]))
        self.assertEqual(job.submission_mode, InvestigationMode.AUTO)
        self.assertEqual(job.status, InvestigationJobStatus.PENDING)
        self.assertEqual(self.providers, [])

    def test_mode_audit_and_job_mode_snapshot_are_immutable(self):
        app = self.app(automatic_capability=True)
        order = self.create_order(app)
        with TestClient(app) as client:
            created = self.submit_case(client, order.id)
        job = app.state.investigation_jobs.get_by_case(UUID(created.json()["id"]))
        store = SQLitePersistence(self.path)
        with self.assertRaises(PersistenceError):
            with store.transaction() as connection:
                connection.execute("UPDATE investigation_mode_audit SET mode = 'AUTO' WHERE sequence = 1")
        with self.assertRaises(PersistenceError):
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE investigation_jobs SET submission_mode = 'AUTO' WHERE job_id = ?",
                    (str(job.id),),
                )


if __name__ == "__main__":
    unittest.main()
