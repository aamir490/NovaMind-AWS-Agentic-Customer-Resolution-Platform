"""Offline admin draft-to-decision flow; no live provider, payment, or fulfillment calls."""

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import time
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from backend.app.domain import Customer
from backend.app.llm import FakeLLMProvider, LLMResponse
from backend.app.main import create_app
from backend.app.schemas import CaseCreate, OrderCreate
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role
import test_agent as baseline


ADMIN_ID = UUID("00000000-0000-4000-8000-000000000091")
CUSTOMER_ID = UUID("00000000-0000-4000-8000-000000000092")
CUSTOMER_USER_ID = UUID("00000000-0000-4000-8000-000000000093")
ADMIN_TOKEN = "admin-resolution-draft-token"
CUSTOMER_TOKEN = "customer-resolution-draft-token"


class AdminResolutionDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "novamind.sqlite"
        self.auth = LocalAuthenticationProvider((
            LocalCredential(token=ADMIN_TOKEN,
                            identity=AuthenticatedIdentity(user_id=ADMIN_ID, role=Role.ADMIN)),
            LocalCredential(token=CUSTOMER_TOKEN, identity=AuthenticatedIdentity(
                user_id=CUSTOMER_USER_ID, role=Role.CUSTOMER, customer_id=CUSTOMER_ID)),
        ))
        self.provider_calls = []

    def app(self, response=None):
        def factory():
            self.provider_calls.append(True)
            if response is not None:
                return baseline.ScriptedProvider((response,))
            return FakeLLMProvider(LLMResponse(
                text='{"decision":{"kind":"final"}}', finish_reason="stop",
            ))

        return create_app(auth_provider=self.auth, sqlite_path=self.path,
                          startup_customers=(Customer(id=CUSTOMER_ID, name="Draft customer"),),
                          local_provider_factory=factory)

    @staticmethod
    def headers(token):
        return {"Authorization": f"Bearer {token}"}

    def case(self, app, subject, description, *, sku="LAP-1", name="Laptop"):
        order = app.state.case_service.create_order(OrderCreate(
            customer_id=CUSTOMER_ID,
            items=[{"sku": sku, "name": name, "quantity": 1}],
        ))
        return app.state.case_service.create_case(CaseCreate(
            customer_id=CUSTOMER_ID, order_id=order.id, subject=subject, description=description,
        ))

    @staticmethod
    def wait_for_completion(client, run_id):
        for _ in range(100):
            snapshot = client.get(f"/api/runs/{run_id}", headers=AdminResolutionDraftTests.headers(ADMIN_TOKEN))
            if snapshot.status_code == 200 and snapshot.json()["status"] == "COMPLETED":
                return snapshot.json()
            time.sleep(0.01)
        raise AssertionError("run did not complete")

    def start(self, client, support_case, message):
        identity = client.get("/api/identity", headers=self.headers(ADMIN_TOKEN)).json()
        response = client.post("/api/runs", headers=self.headers(ADMIN_TOKEN), json={
            "instance_id": identity["instance_id"], "request_id": str(uuid4()),
            "case_id": str(support_case.id), "message": message,
        })
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait_for_completion(client, response.json()["run_id"])

    def test_admin_uses_persisted_draft_once_and_customer_never_receives_staff_reason(self):
        first = self.app()
        support_case = self.case(first, "Damaged laptop", "The product arrived damaged.")
        with TestClient(first) as client:
            self.start(client, support_case, "My laptop arrived damaged.")
            denied = client.get(f"/api/cases/{support_case.id}/resolution-draft",
                                headers=self.headers(CUSTOMER_TOKEN))
            self.assertEqual(denied.status_code, 403)
            draft_response = client.get(f"/api/cases/{support_case.id}/resolution-draft",
                                        headers=self.headers(ADMIN_TOKEN))
            self.assertEqual(draft_response.status_code, 200, draft_response.text)
            draft = draft_response.json()
            self.assertEqual(draft["recommended_decision"], "REQUEST_EVIDENCE")
            self.assertEqual(draft["response_source"], "DETERMINISTIC_FALLBACK")
            self.assertTrue(draft["citations"])
            self.assertFalse(draft["actions_executed"])

            body = {"action": "REQUEST_MORE_EVIDENCE", "reason": draft["decision_reason"],
                    "customer_message": draft["customer_facing_response"], "evidence_reviewed": False}
            recorded = client.post(f"/api/cases/{support_case.id}/resolution",
                                   headers=self.headers(ADMIN_TOKEN), json=body)
            self.assertEqual(recorded.status_code, 200, recorded.text)
            self.assertEqual(recorded.json()["case"]["status"], "in_review")
            duplicate = client.post(f"/api/cases/{support_case.id}/resolution",
                                    headers=self.headers(ADMIN_TOKEN), json=body)
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(len(recorded.json()["history"]), 1)

            customer = client.get(f"/api/cases/{support_case.id}/resolution",
                                  headers=self.headers(CUSTOMER_TOKEN))
            self.assertEqual(customer.status_code, 200, customer.text)
            self.assertNotIn("reason", customer.json()["latest"])
            self.assertNotIn("history", customer.json())
            self.assertNotIn("admin_user_id", customer.json()["latest"])
        first.state.frontend_runtime.close()

        calls_before_read = len(self.provider_calls)
        restarted = self.app()
        with TestClient(restarted) as client:
            restored = client.get(f"/api/cases/{support_case.id}/resolution-draft",
                                  headers=self.headers(ADMIN_TOKEN))
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(restored.json()["issue_type"], "damaged")
            self.assertEqual(restored.json()["recommended_decision"], "REQUEST_EVIDENCE")
            self.assertEqual(restored.json()["response_source"], "DETERMINISTIC_FALLBACK")
        self.assertEqual(len(self.provider_calls), calls_before_read)
        restarted.state.frontend_runtime.close()

    def test_validated_bedrock_wording_survives_hitl_checkpoint_and_admin_projection(self):
        def grounded_final(request):
            context = json.loads(request.messages[-1].content)
            draft = context["grounded_resolution_draft"]
            grounding = context["grounded_policy_evidence"]
            return {"decision": {"kind": "final"}, "resolution_draft": draft, "grounded_response": {
                "issue_type": draft["issue_type"],
                "recommended_decision": draft["recommended_decision"],
                "verified_facts": draft["verified_facts"],
                "missing_evidence": draft["missing_evidence"],
                "policy_passages": grounding["policy_passages"],
                "decision_reason": "Verified records still require the requested evidence for support review.",
                "customer_facing_response": (
                    "Please send the requested evidence so support can review your report. "
                    "No refund, replacement, return, or payment action has been executed."
                ),
            }}

        first = self.app(grounded_final)
        support_case = self.case(first, "Damaged laptop", "The product arrived damaged.")
        with TestClient(first) as client:
            self.start(client, support_case, "My laptop arrived damaged.")
            draft = client.get(f"/api/cases/{support_case.id}/resolution-draft",
                               headers=self.headers(ADMIN_TOKEN))
            self.assertEqual(draft.status_code, 200, draft.text)
            self.assertEqual(draft.json()["response_source"], "BEDROCK_VALIDATED")
            self.assertEqual(draft.json()["decision_reason"],
                             "Verified records still require the requested evidence for support review.")
            self.assertIn("Please send the requested evidence", draft.json()["customer_facing_response"])
            self.assertTrue(draft.json()["citations"])
        first.state.frontend_runtime.close()

        calls_before_read = len(self.provider_calls)
        restarted = self.app()
        with TestClient(restarted) as client:
            restored = client.get(f"/api/cases/{support_case.id}/resolution-draft",
                                  headers=self.headers(ADMIN_TOKEN))
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(restored.json()["response_source"], "BEDROCK_VALIDATED")
            self.assertIn("Please send the requested evidence", restored.json()["customer_facing_response"])
            self.assertTrue(restored.json()["citations"])
        self.assertEqual(len(self.provider_calls), calls_before_read)
        restarted.state.frontend_runtime.close()

    def test_product_steps_survive_the_durable_admin_draft_projection(self):
        def product_final(request):
            context = json.loads(request.messages[-1].content)
            draft = context["grounded_resolution_draft"]
            steps = (
                "Disconnect the NovaControl Input 1 from the current port.",
                "Connect the NovaControl Input 1 directly to a different compatible port.",
                "Restart the host device and report whether the symptom continues.",
            )
            return {"decision": {"kind": "final"}, "grounded_response": {
                "issue_type": draft["issue_type"], "recommended_decision": draft["recommended_decision"],
                "verified_facts": draft["verified_facts"], "missing_evidence": draft["missing_evidence"],
                "policy_passages": context["grounded_policy_evidence"]["policy_passages"],
                "decision_reason": "Verified records require the listed diagnostic evidence for support review.",
                "customer_facing_response": (
                    "Please try these steps:\n1. " + steps[0] + "\n2. " + steps[1] + "\n3. " + steps[2]
                    + "\nNo refund, replacement, return, or payment action has been executed."
                ),
                "troubleshooting_steps": steps,
            }}

        first = self.app(product_final)
        support_case = self.case(first, "NovaControl Input 1 stops responding",
                                 "The input device has an intermittent connection.",
                                 sku="INP-1", name="NovaControl Input 1")
        with TestClient(first) as client:
            self.start(client, support_case, "Please investigate this technical issue.")
            draft = client.get(f"/api/cases/{support_case.id}/resolution-draft",
                               headers=self.headers(ADMIN_TOKEN))
            self.assertEqual(draft.status_code, 200, draft.text)
            self.assertEqual(draft.json()["response_source"], "BEDROCK_VALIDATED")
            self.assertEqual(draft.json()["troubleshooting_steps"], [
                "Disconnect the NovaControl Input 1 from the current port.",
                "Connect the NovaControl Input 1 directly to a different compatible port.",
                "Restart the host device and report whether the symptom continues.",
            ])
            self.assertIn("novacontrol-inp-1-guide", {
                citation["document_id"] for citation in draft.json()["citations"]
            })
        first.state.frontend_runtime.close()

        calls_before_read = len(self.provider_calls)
        restarted = self.app()
        with TestClient(restarted) as client:
            restored = client.get(f"/api/cases/{support_case.id}/resolution-draft",
                                  headers=self.headers(ADMIN_TOKEN))
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(restored.json()["troubleshooting_steps"][0],
                             "Disconnect the NovaControl Input 1 from the current port.")
        self.assertEqual(len(self.provider_calls), calls_before_read)
        restarted.state.frontend_runtime.close()

    def test_escalation_draft_records_only_a_terminal_case_decision(self):
        app = self.app()
        support_case = self.case(app, "Delivery delayed", "The shipment has not arrived.")
        with TestClient(app) as client:
            self.start(client, support_case, "My delivery is late.")
            draft = client.get(f"/api/cases/{support_case.id}/resolution-draft",
                               headers=self.headers(ADMIN_TOKEN)).json()
            self.assertEqual(draft["recommended_decision"], "ESCALATE")
            recorded = client.post(f"/api/cases/{support_case.id}/resolution", headers=self.headers(ADMIN_TOKEN),
                                   json={"action": "ESCALATE", "reason": draft["decision_reason"],
                                         "customer_message": draft["customer_facing_response"],
                                         "evidence_reviewed": False})
            self.assertEqual(recorded.status_code, 200, recorded.text)
            self.assertEqual(recorded.json()["case"]["status"], "escalated")
            self.assertEqual(recorded.json()["latest"]["action"], "ESCALATE")
            self.assertNotIn("actions_executed", recorded.json())
        app.state.frontend_runtime.close()

    def test_refund_recommendation_survives_checkpoint_and_stays_admin_only(self):
        first = self.app()
        support_case = self.case(first, "Refund request", "Please refund my purchased laptop.")
        with TestClient(first) as client:
            identity = client.get("/api/identity", headers=self.headers(ADMIN_TOKEN)).json()
            started = client.post("/api/runs", headers=self.headers(ADMIN_TOKEN), json={
                "instance_id": identity["instance_id"], "request_id": str(uuid4()),
                "case_id": str(support_case.id), "message": "Review the selected refund.",
                "resolution_intent": "refund", "refund_sku": "LAP-1",
            })
            self.assertEqual(started.status_code, 202, started.text)
            for _ in range(100):
                snapshot = client.get(f"/api/runs/{started.json()['run_id']}",
                                      headers=self.headers(ADMIN_TOKEN)).json()
                if snapshot["status"] == "REVIEW_REQUIRED":
                    break
                time.sleep(0.01)
            else:
                self.fail("refund review did not pause")
            self.assertEqual(snapshot["review"]["action"], "refund_investigation")
            denied = client.get(f"/api/cases/{support_case.id}/resolution-draft",
                                headers=self.headers(CUSTOMER_TOKEN))
            self.assertEqual(denied.status_code, 403)
            draft = client.get(f"/api/cases/{support_case.id}/resolution-draft",
                               headers=self.headers(ADMIN_TOKEN)).json()
            recommendation = draft["refund_recommendation"]
            self.assertEqual(recommendation["outcome"], "NEED_MORE_EVIDENCE")
            self.assertEqual(recommendation["sku"], "LAP-1")
            self.assertFalse(recommendation["authorization_granted"])
            self.assertFalse(recommendation["actions_executed"])
            self.assertTrue(recommendation["citations"])
        first.state.frontend_runtime.close()

        restarted = self.app()
        with TestClient(restarted) as client:
            restored = client.get(f"/api/cases/{support_case.id}/resolution-draft",
                                  headers=self.headers(ADMIN_TOKEN))
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(restored.json()["refund_recommendation"]["outcome"], "NEED_MORE_EVIDENCE")
        restarted.state.frontend_runtime.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
