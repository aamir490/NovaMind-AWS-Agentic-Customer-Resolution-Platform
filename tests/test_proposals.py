"""Focused Phase 4 tests only; no earlier test classes are imported or run."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import socket
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4

from pydantic import ValidationError
import uvicorn

from backend.app.main import create_app
from backend.app.operations import (
    AuthoritativeDeliveryFact, TrustedDeliverySource,
)
from backend.app.proposals import HumanReview, ProposalCreate, ProposalStatus
from backend.app.schemas import CustomerCreate, OrderCreate, CaseCreate
from backend.app.service import ConflictError, NotFoundError
from security_fixtures import ADMIN_TOKEN, admin_provider


def create_case(app):
    cases = app.state.case_service
    customer = cases.create_customer(CustomerCreate(name="Proposal test"))
    order = cases.create_order(OrderCreate(customer_id=customer.id, items=[
        {"sku": "LAP-1", "name": "Laptop", "quantity": 1},
    ]))
    return cases.create_case(CaseCreate(customer_id=customer.id, order_id=order.id,
                                      subject="Wrong item", description="Wrong laptop received."))


class ProposalServiceTests(unittest.TestCase):
    def setUp(self):
        self.delivery_source = TrustedDeliverySource(id="test-fulfillment")
        self.app = create_app(trusted_delivery_sources=(self.delivery_source,))
        self.case = create_case(self.app)
        self.service = self.app.state.proposal_service
        self.request = ProposalCreate(case_id=self.case.id, action="replacement", rationale="Review a replacement.")
        self.review = HumanReview(reviewer_name="Local reviewer", note="Reviewed the proposal.")

    def refund_request(self):
        now = datetime.now(timezone.utc)
        operations = self.app.state.business_operations
        self.app.state.delivery_fact_ingestion.ingest(self.delivery_source.id, AuthoritativeDeliveryFact(
            id=uuid4(), case_id=self.case.id, order_id=self.case.order_id,
            customer_id=self.case.customer_id, sku="LAP-1", quantity=1,
            delivered_at=now - timedelta(days=10), reason="wrong_item", condition="unused",
            source_id=self.delivery_source.id,
            source_reference="test-verified-delivery", verified_at=now,
        ))
        evidence = operations.assess_refund_eligibility(self.case.id, "LAP-1")
        return ProposalCreate(case_id=self.case.id, action="refund", rationale="Review only.",
                              eligibility_evidence_id=evidence.evidence_id)

    def test_creation_is_pending_and_server_managed(self):
        proposal = self.service.create(self.request)
        self.assertEqual(proposal.status, ProposalStatus.PENDING_REVIEW)
        self.assertEqual(proposal.created_at, proposal.updated_at)
        self.assertIsNotNone(proposal.created_at.tzinfo)
        self.assertIsNone(proposal.reviewed_at)
        self.assertIsNone(proposal.reviewer_name)
        self.assertIsNone(proposal.review_note)
        self.assertEqual(self.service.list_for_case(self.case.id), [proposal])
        self.assertNotEqual(self.service.create(self.request).id, proposal.id)
        with self.assertRaises(ValidationError):
            proposal.action = "refund"

    def test_missing_case_and_proposal(self):
        with self.assertRaises(NotFoundError):
            self.service.create(ProposalCreate(case_id=uuid4(), action="return", rationale="Review."))
        with self.assertRaises(NotFoundError):
            self.service.list_for_case(uuid4())
        with self.assertRaises(NotFoundError):
            self.service.get(uuid4())
        with self.assertRaises(NotFoundError):
            self.service.review(uuid4(), ProposalStatus.APPROVED, self.review)
        self.assertEqual(self.service.list_for_case(self.case.id), [])

    def test_both_terminal_states_and_all_repeated_reviews_rejected(self):
        for decision in (ProposalStatus.APPROVED, ProposalStatus.REJECTED):
            with self.subTest(decision=decision):
                original = self.service.create(self.request)
                reviewed = self.service.review(original.id, decision, self.review)
                self.assertEqual(reviewed.status, decision)
                self.assertEqual(reviewed.created_at, original.created_at)
                self.assertEqual(reviewed.action, original.action)
                self.assertEqual(reviewed.rationale, original.rationale)
                self.assertEqual(reviewed.reviewed_at, reviewed.updated_at)
                self.assertGreaterEqual(reviewed.updated_at, original.created_at)
                self.assertEqual(reviewed.reviewer_name, self.review.reviewer_name)
                self.assertEqual(reviewed.review_note, self.review.note)
                for next_decision in (ProposalStatus.APPROVED, ProposalStatus.REJECTED, ProposalStatus.PENDING_REVIEW):
                    with self.assertRaises(ConflictError):
                        self.service.review(original.id, next_decision, self.review)
                self.assertEqual(self.service.get(original.id), reviewed)

    def test_invalid_pending_review_does_not_change_record(self):
        proposal = self.service.create(self.request)
        with self.assertRaises(ConflictError):
            self.service.review(proposal.id, ProposalStatus.PENDING_REVIEW, self.review)
        self.assertEqual(self.service.get(proposal.id), proposal)

    def test_approval_does_not_execute_or_modify_case_order_stock(self):
        case_service = self.app.state.case_service
        order_before = case_service.get_order(self.case.order_id)
        stock_before = self.app.state.business_operations.get_inventory("LAP-1")
        for action in ("refund", "replacement", "return"):
            request = self.refund_request() if action == "refund" else ProposalCreate(
                case_id=self.case.id, action=action, rationale="Review only.",
            )
            proposal = self.service.create(request)
            self.service.review(proposal.id, ProposalStatus.APPROVED, self.review)
        self.assertEqual(case_service.get_case(self.case.id), self.case)
        self.assertEqual(case_service.get_order(self.case.order_id), order_before)
        self.assertEqual(self.app.state.business_operations.get_inventory("LAP-1"), stock_before)

    def test_concurrent_reviews_have_one_winner(self):
        proposal = self.service.create(self.request)
        barrier = threading.Barrier(2)

        def review(decision):
            barrier.wait(timeout=5)
            try:
                return self.service.review(proposal.id, decision, self.review).status
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(review, (ProposalStatus.APPROVED, ProposalStatus.REJECTED)))
        self.assertEqual(results.count("conflict"), 1)
        winner = next(value for value in results if value != "conflict")
        self.assertEqual(self.service.get(proposal.id).status, winner)

    def test_case_filter_and_application_isolation(self):
        proposal = self.service.create(self.request)
        other_case = create_case(self.app)
        self.assertEqual(self.service.list_for_case(other_case.id), [])
        with self.assertRaises(NotFoundError):
            create_app().state.proposal_service.get(proposal.id)


class ProposalHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(auth_provider=admin_provider())
        cls.case = create_case(cls.app)
        cls.listener = socket.socket()
        cls.listener.bind(("127.0.0.1", 0))
        cls.port = cls.listener.getsockname()[1]
        cls.server = uvicorn.Server(uvicorn.Config(cls.app, log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.listener]}, daemon=True)
        cls.addClassCleanup(cls.stop_server)
        cls.thread.start()
        deadline = time.monotonic() + 15
        while not cls.server.started:
            if not cls.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Proposal test server did not start")
            time.sleep(0.01)
        cls.client = build_opener(ProxyHandler({}))

    @classmethod
    def stop_server(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.listener.close()
        if cls.thread.is_alive():
            raise RuntimeError("Proposal test server did not stop")

    def request(self, method, path, body=None):
        request = Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + ADMIN_TOKEN})
        try:
            response = self.client.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def create(self):
        status, proposal = self.request("POST", "/api/proposals", {
            "case_id": str(self.case.id), "action": "replacement", "rationale": "Review a replacement request.",
        })
        self.assertEqual(status, 201)
        self.assertEqual(proposal["status"], "PENDING_REVIEW")
        return proposal

    def test_create_read_list_and_explicit_reviews(self):
        for decision in ("approve", "reject"):
            with self.subTest(decision=decision):
                proposal = self.create()
                path = f'/api/proposals/{proposal["id"]}'
                self.assertEqual(self.request("GET", path), (200, proposal))
                self.assertIn(proposal, self.request("GET", f"/api/cases/{self.case.id}/proposals")[1])
                body = {"note": "Reviewed manually."}
                status, reviewed = self.request("POST", f"{path}/{decision}", body)
                self.assertEqual(status, 200)
                self.assertEqual(reviewed["status"], "APPROVED" if decision == "approve" else "REJECTED")
                self.assertIsNotNone(reviewed["reviewed_at"])
                for repeat in ("approve", "reject"):
                    self.assertEqual(self.request("POST", f"{path}/{repeat}", body)[0], 409)
                self.assertEqual(self.request("GET", path), (200, reviewed))

    def test_create_rejects_unknown_action_and_client_managed_fields(self):
        base = {"case_id": str(self.case.id), "action": "return", "rationale": "Review."}
        for changes in ({"action": "execute_refund"}, {"rationale": " "}, {"status": "APPROVED"},
                        {"id": str(uuid4())}, {"created_at": "2026-01-01"}, {"updated_at": "2026-01-01"},
                        {"reviewed_at": "2026-01-01"}, {"reviewer_name": "Injected"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.request("POST", "/api/proposals", {**base, **changes})[0], 422)

    def test_review_requires_explicit_human_fields_and_cannot_rewrite_proposal(self):
        proposal = self.create()
        path = f'/api/proposals/{proposal["id"]}'
        base = {"note": "Review."}
        for body in ({}, {**base, "reviewer_name": " "}, {**base, "note": " "},
                     {**base, "status": "APPROVED"}, {**base, "action": "replacement"},
                     {**base, "reviewed_at": "2026-01-01"}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", f"{path}/approve", body)[0], 422)
        self.assertEqual(self.request("GET", path), (200, proposal))
        self.assertEqual(self.request("PATCH", path, {"status": "APPROVED"})[0], 405)

    def test_missing_records_and_invalid_ids(self):
        body = {"note": "Review."}
        self.assertEqual(self.request("GET", f"/api/proposals/{uuid4()}")[0], 404)
        self.assertEqual(self.request("GET", "/api/proposals/not-a-uuid")[0], 422)
        self.assertEqual(self.request("GET", f"/api/cases/{uuid4()}/proposals")[0], 404)
        self.assertEqual(self.request("POST", f"/api/proposals/{uuid4()}/reject", body)[0], 404)
        self.assertEqual(self.request("POST", "/api/proposals", {
            "case_id": str(uuid4()), "action": "refund", "rationale": "Review.",
        })[0], 404)


if __name__ == "__main__":
    unittest.main()
