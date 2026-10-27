"""Offline P0-3B FastAPI/FrontendRuntime durable LangGraph wiring tests."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

import test_agent as baseline
from backend.app.domain import Customer
from backend.app.frontend_contracts import RunDecision
from backend.app.llm import ProviderFailure
from backend.app.main import create_app
from backend.app.persistence import SQLitePersistence, SQLiteWorkflowMetadataRepository
from backend.app.schemas import CaseCreate, OrderCreate
from backend.app.security import (
    AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role, authenticated,
)
from backend.app.workflow_metadata import WorkflowMetadataStatus, WorkflowRunReservation


ADMIN_ID = UUID("00000000-0000-4000-8000-000000000061")
CUSTOMER_ID = UUID("00000000-0000-4000-8000-000000000062")
CUSTOMER_USER_ID = UUID("00000000-0000-4000-8000-000000000063")
OTHER_CUSTOMER_ID = UUID("00000000-0000-4000-8000-000000000064")
OTHER_CUSTOMER_USER_ID = UUID("00000000-0000-4000-8000-000000000065")
ADMIN_TOKEN = "sqlite-workflow-admin-token"
CUSTOMER_TOKEN = "sqlite-workflow-customer-token"
OTHER_CUSTOMER_TOKEN = "sqlite-workflow-other-customer-token"


class SQLiteFrontendWorkflowWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "novamind.sqlite"
        self.customer = Customer(id=CUSTOMER_ID, name="Durable workflow customer")
        self.auth = LocalAuthenticationProvider((
            LocalCredential(token=ADMIN_TOKEN,
                            identity=AuthenticatedIdentity(user_id=ADMIN_ID, role=Role.ADMIN)),
            LocalCredential(token=CUSTOMER_TOKEN, identity=AuthenticatedIdentity(
                user_id=CUSTOMER_USER_ID, role=Role.CUSTOMER, customer_id=CUSTOMER_ID)),
            LocalCredential(token=OTHER_CUSTOMER_TOKEN, identity=AuthenticatedIdentity(
                user_id=OTHER_CUSTOMER_USER_ID, role=Role.CUSTOMER, customer_id=OTHER_CUSTOMER_ID)),
        ))
        self.providers = []
        self.script = {
            "decision": {"kind": "tool", "name": "create_resolution_proposal", "arguments": {
                "case_id": None, "action": "replacement", "rationale": "A staff reviewer must decide.",
            }},
        }

    def app(self):
        def factory():
            script = self.script
            if isinstance(script, dict):
                script = {**script, "decision": {**script["decision"], "arguments": {
                    **script["decision"]["arguments"], "case_id": str(self.case.id),
                }}}
            provider = baseline.ScriptedProvider((script,))
            self.providers.append(provider)
            return provider

        return create_app(auth_provider=self.auth, sqlite_path=self.path,
                          startup_customers=(self.customer,), local_provider_factory=factory)

    @staticmethod
    def wait_for(client, run_id, headers, status):
        for _ in range(100):
            response = client.get(f"/api/runs/{run_id}", headers=headers)
            if response.status_code == 200 and response.json()["status"] == status:
                return response.json()
            time.sleep(0.01)
        raise AssertionError(f"Run {run_id} did not reach {status}")

    def seed_case(self, app):
        service = app.state.case_service
        order = service.create_order(OrderCreate(
            customer_id=CUSTOMER_ID,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))
        return service.create_case(CaseCreate(
            customer_id=CUSTOMER_ID, order_id=order.id,
            subject="Durable replacement", description="Please review the damaged product.",
        ))

    def start_paused_run(self):
        first = self.app()
        self.case = self.seed_case(first)
        customer_headers = {"Authorization": f"Bearer {CUSTOMER_TOKEN}"}
        admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        with TestClient(first) as client:
            identity = client.get("/api/identity", headers=admin_headers).json()
            payload = {
                "instance_id": identity["instance_id"], "request_id": str(uuid4()),
                "case_id": str(self.case.id), "message": "Please replace this damaged item.",
            }
            denied = client.post("/api/runs", headers=customer_headers, json=payload)
            self.assertEqual(denied.status_code, 403, denied.text)
            started = client.post("/api/runs", headers=admin_headers, json=payload)
            self.assertEqual(started.status_code, 202, started.text)
            paused = self.wait_for(client, started.json()["run_id"], admin_headers, "REVIEW_REQUIRED")
        return paused

    @staticmethod
    def decision(instance_id, review, *, decision="APPROVE"):
        return {
            "instance_id": instance_id, "workflow_id": review["workflow_id"],
            "case_id": review["case_id"], "proposal_id": review["proposal_id"],
            "review_id": review["review_id"], "decision": decision,
            "note": "A reviewer checked the submitted evidence.",
        }

    def test_restart_restores_pending_review_audit_and_exactly_once_completion(self):
        paused = self.start_paused_run()
        provider_calls = len(self.providers)
        restarted = self.app()
        admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        customer_headers = {"Authorization": f"Bearer {CUSTOMER_TOKEN}"}
        with TestClient(restarted) as client:
            identity = client.get("/api/identity", headers=admin_headers).json()
            reviews = client.get("/api/reviews", headers=admin_headers)
            self.assertEqual(reviews.status_code, 200, reviews.text)
            self.assertEqual([item["run_id"] for item in reviews.json()["items"]], [paused["run_id"]])
            review = reviews.json()["items"][0]["review"]
            customer_case = client.get(f"/api/cases/{self.case.id}", headers=customer_headers)
            self.assertEqual(customer_case.status_code, 200, customer_case.text)
            customer_run = client.get(f"/api/runs/{paused['run_id']}", headers=customer_headers)
            self.assertEqual(customer_run.status_code, 403, customer_run.text)
            audit = client.get(f"/api/runs/{paused['run_id']}/audit", headers=admin_headers)
            self.assertEqual(audit.status_code, 200, audit.text)
            self.assertTrue(audit.json()["events"])

            response = client.post(f"/api/runs/{paused['run_id']}/decision", headers=admin_headers,
                json=self.decision(identity["instance_id"], review))
            self.assertEqual(response.status_code, 202, response.text)
            completed = self.wait_for(client, paused["run_id"], admin_headers, "REVIEWED")
            duplicate = client.post(f"/api/runs/{paused['run_id']}/decision", headers=admin_headers,
                json=self.decision(identity["instance_id"], review))
            self.assertEqual(duplicate.status_code, 409, duplicate.text)
            self.assertEqual(completed["reviewed_status"], "APPROVED")

        # Restoring and reviewing a paused run never instantiates or calls a model.
        self.assertEqual(len(self.providers), provider_calls)

    def test_concurrent_restart_reviews_have_one_winner(self):
        paused = self.start_paused_run()
        restarted = self.app()
        with TestClient(restarted) as client:
            identity = client.get("/api/identity", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}).json()
            review = client.get("/api/reviews", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}).json()["items"][0]["review"]
            approve = RunDecision.model_validate(self.decision(identity["instance_id"], review))
            reject = RunDecision.model_validate(self.decision(identity["instance_id"], review, decision="REJECT"))
            runtime = restarted.state.frontend_runtime

            def submit(payload):
                with authenticated(self.auth, ADMIN_TOKEN):
                    try:
                        return runtime.decide(UUID(paused["run_id"]), payload)
                    except Exception as error:
                        return error

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(submit, (approve, reject)))
            self.assertEqual(sum(getattr(item, "status", None) == "RESUMING" for item in outcomes), 1)
            self.assertEqual(sum(getattr(item, "code", None) == "NOT_PAUSED" for item in outcomes), 1)
            self.wait_for(client, paused["run_id"], {"Authorization": f"Bearer {ADMIN_TOKEN}"}, "REVIEWED")

    def test_restart_lists_rejected_review_and_loads_durable_audit_without_provider_call(self):
        paused = self.start_paused_run()
        restarted = self.app()
        admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        with TestClient(restarted) as client:
            identity = client.get("/api/identity", headers=admin_headers).json()
            review = client.get("/api/reviews", headers=admin_headers).json()["items"][0]["review"]
            response = client.post(f"/api/runs/{paused['run_id']}/decision", headers=admin_headers,
                json=self.decision(identity["instance_id"], review, decision="REJECT"))
            self.assertEqual(response.status_code, 202, response.text)
            reviewed = self.wait_for(client, paused["run_id"], admin_headers, "REVIEWED")
            self.assertEqual(reviewed["reviewed_status"], "REJECTED")
        provider_calls = len(self.providers)

        historical = self.app()
        with TestClient(historical) as client:
            listed = client.get(f"/api/cases/{self.case.id}/runs", headers=admin_headers)
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(listed.json()["scope"], "case_runs_with_durable_metadata")
            self.assertEqual([(item["run_id"], item["status"]) for item in listed.json()["items"]], [
                (paused["run_id"], "REVIEWED"),
            ])

            owner = client.get(f"/api/cases/{self.case.id}/runs", headers={"Authorization": f"Bearer {CUSTOMER_TOKEN}"})
            self.assertEqual(owner.status_code, 200, owner.text)
            self.assertEqual(owner.json()["items"], [])
            denied = client.get(f"/api/cases/{self.case.id}/runs",
                                headers={"Authorization": f"Bearer {OTHER_CUSTOMER_TOKEN}"})
            self.assertEqual(denied.status_code, 403)

            audit = client.get(f"/api/runs/{paused['run_id']}/audit", headers=admin_headers)
            self.assertEqual(audit.status_code, 200, audit.text)
            self.assertEqual(audit.json()["scope"], "durable_workflow_audit")
            self.assertIn(("HUMAN_REVIEW", "REJECTED"), {
                (event["kind"], event["proposal_status"]) for event in audit.json()["events"]
            })
        self.assertEqual(len(self.providers), provider_calls)

    def test_restart_marks_inflight_run_recovery_required_without_provider_call(self):
        first = self.app()
        self.case = self.seed_case(first)
        metadata = SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path))
        workflow_id = uuid4()
        metadata.reserve(WorkflowRunReservation(
            workflow_id=workflow_id, run_id=uuid4(), case_id=self.case.id,
            owner_user_id=CUSTOMER_USER_ID, request_id=uuid4(),
            request_fingerprint=sha256(b"inflight run").hexdigest(), provider="local_scripted",
        ))
        first.state.frontend_runtime.close()

        restarted = self.app()
        self.assertEqual(
            SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(workflow_id).status,
            WorkflowMetadataStatus.RECOVERY_REQUIRED,
        )
        self.assertEqual(self.providers, [])
        restarted.state.frontend_runtime.close()

    def test_failed_run_is_not_replayed_during_restart_recovery(self):
        self.script = ProviderFailure("UNAVAILABLE")
        first = self.app()
        self.case = self.seed_case(first)
        customer_headers = {"Authorization": f"Bearer {CUSTOMER_TOKEN}"}
        admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        with TestClient(first) as client:
            identity = client.get("/api/identity", headers=admin_headers).json()
            payload = {
                "instance_id": identity["instance_id"], "request_id": str(uuid4()),
                "case_id": str(self.case.id), "message": "Please investigate this case.",
            }
            denied = client.post("/api/runs", headers=customer_headers, json=payload)
            self.assertEqual(denied.status_code, 403, denied.text)
            started = client.post("/api/runs", headers=admin_headers, json=payload)
            self.assertEqual(started.status_code, 202, started.text)
            failed = self.wait_for(client, started.json()["run_id"], admin_headers, "FAILED")
        provider_calls = len(self.providers)

        restarted = self.app()
        with TestClient(restarted) as client:
            restored = client.get(f"/api/runs/{failed['run_id']}", headers=admin_headers)
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(restored.json()["status"], "FAILED")
        self.assertEqual(len(self.providers), provider_calls)
