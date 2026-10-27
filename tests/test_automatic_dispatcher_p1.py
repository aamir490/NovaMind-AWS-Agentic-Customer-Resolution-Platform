"""Focused P1-1C3 automatic-dispatch tests using only mocked providers."""

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import time
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from backend.app.domain import (
    Customer, InvestigationJobStatus, InvestigationMode, InvestigationRecoveryReason,
)
from backend.app.llm import FakeLLMProvider, LLMResponse, ProviderFailure
from backend.app.main import create_app
from backend.app.persistence import SQLitePersistence, SQLiteWorkflowMetadataRepository
from backend.app.schemas import OrderCreate
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role


CUSTOMER_ID = UUID("00000000-0000-4000-8000-0000000000c3")
CUSTOMER_USER_ID = UUID("00000000-0000-4000-8000-0000000000c4")
DISPATCHER_ID = UUID("00000000-0000-4000-8000-0000000000c5")
ADMIN_ID = UUID("00000000-0000-4000-8000-0000000000c6")
CUSTOMER_TOKEN = "p1-c3-customer-token"
ADMIN_TOKEN = "p1-c3-admin-token"


class AutomaticDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "novamind.sqlite"
        self.auth = LocalAuthenticationProvider((
            LocalCredential(token=CUSTOMER_TOKEN, identity=AuthenticatedIdentity(
                user_id=CUSTOMER_USER_ID, role=Role.CUSTOMER, customer_id=CUSTOMER_ID)),
            LocalCredential(token=ADMIN_TOKEN, identity=AuthenticatedIdentity(
                user_id=ADMIN_ID, role=Role.ADMIN)),
        ))
        self.providers = []

    def app(self, *, enabled=False, failure=False, review=False, sku="LAP-1", name="Laptop"):
        def factory():
            if review:
                provider = _ReviewProvider()
            else:
                provider = FakeLLMProvider(
                    ProviderFailure("UNAVAILABLE") if failure else LLMResponse(
                        text='{"decision":{"kind":"final"}}', finish_reason="stop",
                    )
                )
            self.providers.append(provider)
            return provider

        kwargs = {}
        if enabled:
            kwargs = {
                "automatic_dispatcher": True,
                "dispatcher_identity": AuthenticatedIdentity(
                    user_id=DISPATCHER_ID, role=Role.REVIEWER,
                ),
            }
        app = create_app(
            auth_provider=self.auth, sqlite_path=self.path,
            startup_customers=(Customer(id=CUSTOMER_ID, name="Dispatcher customer"),),
            local_provider_factory=factory, **kwargs,
        )
        if enabled:
            app.state.investigation_control.set_mode(
                InvestigationMode.AUTO, expected_version=1,
                admin_user_id=ADMIN_ID, idempotency_key=uuid4(),
            )
        order = app.state.case_service.create_order(OrderCreate(
            customer_id=CUSTOMER_ID,
            items=[{"sku": sku, "name": name, "quantity": 1}],
        ))
        return app, order

    @staticmethod
    def submit(client, order_id, key, *, subject="Damaged item", description="The item arrived damaged."):
        return client.post(
            "/api/my/cases",
            headers={"Authorization": f"Bearer {CUSTOMER_TOKEN}", "Idempotency-Key": str(key)},
            json={
                "order_id": str(order_id), "subject": subject, "description": description,
            },
        )

    def test_default_off_keeps_customer_complaint_inert(self):
        app, order = self.app()
        with TestClient(app) as client:
            response = self.submit(client, order.id, uuid4())
        self.assertEqual(response.status_code, 201, response.text)
        job = app.state.investigation_jobs.get_by_case(UUID(response.json()["id"]))
        self.assertEqual(job.status, InvestigationJobStatus.PENDING)
        self.assertIsNone(job.execution_attempt_id)
        self.assertEqual(self.providers, [])

    def test_enabled_dispatcher_uses_configured_staff_and_never_replays_a_retry(self):
        app, order = self.app(enabled=True)
        key = uuid4()
        with TestClient(app) as client:
            first = self.submit(client, order.id, key)
            repeated = self.submit(client, order.id, key)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(repeated.status_code, 201, repeated.text)
        self.assertEqual(first.json()["id"], repeated.json()["id"])
        job = self.wait_for_attempt(app, UUID(first.json()["id"]))
        self.assertEqual(job.claimed_by, DISPATCHER_ID)
        self.assertEqual(job.submission_mode, InvestigationMode.AUTO)
        self.assertIsNotNone(job.execution_attempt_id)
        self.assertEqual(len(self.providers), 1)
        metadata = SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(job.id)
        self.assertEqual(metadata.status.value, "COMPLETED")
        self.assertIsNone(metadata.failure_category)
        self.assertIsNone(metadata.failure_stage)

    def test_provider_failure_requires_recovery_without_a_second_attempt(self):
        app, order = self.app(enabled=True, failure=True)
        with TestClient(app) as client:
            response = self.submit(client, order.id, uuid4())
        self.assertEqual(response.status_code, 201, response.text)
        case_id = UUID(response.json()["id"])
        job = self.wait_for_attempt(app, case_id)
        self.assertEqual(job.status, InvestigationJobStatus.RECOVERY_REQUIRED)
        self.assertEqual(job.recovery_reason, InvestigationRecoveryReason.CLAIM_AMBIGUOUS)
        self.assertIsNotNone(job.execution_attempt_id)
        self.assertEqual(len(self.providers), 1)
        metadata = SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(job.id)
        self.assertEqual((metadata.status.value, metadata.failure_category, metadata.failure_stage),
                         ("FAILED", "UNAVAILABLE", "AGENT"))

        with TestClient(app) as client:
            investigation = client.get(
                f"/api/cases/{case_id}/investigation",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )
        self.assertEqual(investigation.status_code, 200, investigation.text)
        self.assertEqual(investigation.json()["state"], "RECOVERY_REQUIRED")
        self.assertEqual(investigation.json()["workflow_status"], "FAILED")
        self.assertEqual(investigation.json()["error"], "UNAVAILABLE")
        self.assertEqual(investigation.json()["resolution_draft"]["response_source"],
                         "DETERMINISTIC_FALLBACK")
        self.assertFalse(investigation.json()["resolution_draft"]["actions_executed"])

    def test_technical_case_completes_without_creating_a_fulfillment_proposal(self):
        app, order = self.app(enabled=True, sku="INP-1", name="NovaControl Input 1")
        with TestClient(app) as client:
            response = self.submit(
                client, order.id, uuid4(), subject="NovaControl Input 1 stops responding",
                description="The input device has an intermittent connection during normal use.",
            )
        self.assertEqual(response.status_code, 201, response.text)
        case_id = UUID(response.json()["id"])
        job = self.wait_for_attempt(app, case_id)
        self.assertEqual(job.claimed_by, DISPATCHER_ID)
        metadata = SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(job.id)
        for _ in range(100):
            if metadata.status.value == "COMPLETED":
                break
            time.sleep(0.01)
            metadata = SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path)).get(job.id)
        self.assertEqual(metadata.status.value, "COMPLETED")
        self.assertEqual(app.state.proposal_service.list_for_case(case_id), [])
        self.assertEqual(len(self.providers), 1)

    def test_proposal_path_pauses_for_staff_review(self):
        app, order = self.app(enabled=True, review=True)
        with TestClient(app) as client:
            response = self.submit(client, order.id, uuid4())
        self.assertEqual(response.status_code, 201, response.text)
        job = self.wait_for_attempt(app, UUID(response.json()["id"]))
        metadata = SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path))
        for _ in range(100):
            if metadata.get(job.id).status.value == "REVIEW_REQUIRED":
                break
            time.sleep(0.01)
        self.assertEqual(metadata.get(job.id).status.value, "REVIEW_REQUIRED")
        self.assertEqual(len(self.providers), 1)

    @staticmethod
    def wait_for_attempt(app, case_id):
        for _ in range(100):
            job = app.state.investigation_jobs.get_by_case(case_id)
            if job.execution_attempt_id is not None:
                return job
            time.sleep(0.01)
        raise AssertionError("Dispatcher did not mark an execution attempt")


if __name__ == "__main__":
    unittest.main()


class _ReviewProvider:
    """Creates one bounded proposal from the request case ID in model context."""

    def generate(self, request, *, response_schema):
        payload = json.loads(request.messages[-1].content)
        return LLMResponse(text=json.dumps({"decision": {"kind": "tool",
            "name": "create_resolution_proposal", "arguments": {
                "case_id": payload["request"]["case_id"], "action": "replacement",
                "rationale": "A staff reviewer must decide.",
            }}}), finish_reason="stop")
