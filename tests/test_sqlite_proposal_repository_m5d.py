"""Focused M5D proposal/review persistence tests; HITL checkpoints stay in memory."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.app.operations import BusinessOperations
from backend.app.persistence import (
    PersistenceError, SQLiteCaseRepository, SQLitePersistence,
    SQLiteProposalRepository,
)
from backend.app.proposals import (
    HumanReview, ProposalCreate, ProposalService, ProposalStatus,
)
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.service import CaseService, ConflictError


class SQLiteProposalRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "novamind.sqlite"
        self.cases, self.service, self.repository = self.stack(initialize=True)
        self.customer = self.cases.create_customer(CustomerCreate(name="Durable reviewer customer"))
        self.order = self.cases.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))
        self.case = self.create_case(self.cases, "Primary review")
        self.review = HumanReview(reviewer_name=str(uuid4()), note="Reviewed by authorized staff.")

    def stack(self, *, initialize=False):
        persistence = SQLitePersistence(self.path, initialize=initialize)
        cases = CaseService(SQLiteCaseRepository(persistence))
        repository = SQLiteProposalRepository(persistence)
        service = ProposalService(cases, BusinessOperations(cases), repository)
        return cases, service, repository

    def create_case(self, cases, subject):
        return cases.create_case(CaseCreate(
            customer_id=self.customer.id, order_id=self.order.id,
            subject=subject, description="Persistent proposal review fixture.",
        ))

    def proposal(self, service=None, case_id=None):
        return (service or self.service).create(ProposalCreate(
            case_id=case_id or self.case.id, action="replacement",
            rationale="Review a replacement request.",
        ))

    def bind(self, proposal, service=None):
        workflow_id, review_id = uuid4(), uuid4()
        binding = (service or self.service).bind_workflow_review(
            proposal.id, workflow_id=workflow_id,
            case_id=proposal.case_id, review_id=review_id,
        )
        return binding

    def test_proposal_binding_decision_and_audit_survive_recreation(self):
        proposal = self.proposal()
        binding = self.bind(proposal)

        _, recreated, repository = self.stack()
        self.assertEqual(recreated.get(proposal.id), proposal)
        self.assertTrue(recreated.matches_workflow_review(
            proposal.id, workflow_id=binding.workflow_id,
            case_id=binding.case_id, review_id=binding.review_id,
        ))
        reviewed = recreated.review(
            proposal.id, ProposalStatus.APPROVED, self.review,
            workflow_id=binding.workflow_id, review_id=binding.review_id,
        )

        _, final, final_repository = self.stack()
        self.assertEqual(final.get(proposal.id), reviewed)
        self.assertEqual(final.list_for_case(self.case.id), [reviewed])
        events = final_repository.audit_events(binding.workflow_id)
        self.assertEqual([event["kind"] for event in events], ["REVIEW_REQUIRED", "HUMAN_REVIEW"])
        self.assertEqual(events[-1]["reviewer_user_id"], self.review.reviewer_name)
        self.assertEqual(events[-1]["proposal_status"], "APPROVED")

    def test_refund_investigation_without_evidence_is_durable_and_distinct(self):
        proposal = self.service.create(ProposalCreate(
            case_id=self.case.id, action="refund_investigation",
            rationale="Trusted evidence is missing; investigate without approving a refund.",
        ))

        _, recreated, _ = self.stack()
        self.assertEqual(recreated.get(proposal.id), proposal)
        self.assertEqual(proposal.action, "refund_investigation")
        self.assertIsNone(proposal.eligibility_evidence_id)
        with self.assertRaises(ConflictError):
            recreated.create(ProposalCreate(
                case_id=self.case.id, action="refund",
                rationale="Evidence-free refund approval must remain impossible.",
            ))

    def test_approval_and_rejection_are_terminal_and_preserve_proposal_fields(self):
        for decision in (ProposalStatus.APPROVED, ProposalStatus.REJECTED):
            with self.subTest(decision=decision):
                original = self.proposal()
                reviewed = self.service.review(original.id, decision, self.review)
                self.assertEqual(reviewed.status, decision)
                self.assertEqual(reviewed.case_id, original.case_id)
                self.assertEqual(reviewed.action, original.action)
                self.assertEqual(reviewed.rationale, original.rationale)
                self.assertEqual(reviewed.created_at, original.created_at)
                self.assertEqual(reviewed.reviewed_at, reviewed.updated_at)
                with self.assertRaisesRegex(ConflictError, "already been reviewed"):
                    self.service.review(original.id, decision, self.review)

    def test_direct_review_is_monotonic_when_anchor_and_decision_share_a_clock_tick(self):
        instant = datetime(2026, 1, 1, tzinfo=timezone.utc)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant if tz is not None else instant.replace(tzinfo=None)

        with patch("backend.app.proposals.datetime", FixedDateTime), \
                patch("backend.app.persistence.datetime", FixedDateTime):
            proposal = self.proposal()
            reviewed = self.service.review(
                proposal.id, ProposalStatus.APPROVED, self.review,
            )

        self.assertEqual(reviewed.updated_at, instant.replace(microsecond=1))
        self.assertEqual(self.service.get(proposal.id), reviewed)

    def test_audit_failure_rolls_back_proposal_workflow_and_decision(self):
        proposal = self.proposal()
        binding = self.bind(proposal)
        store = SQLitePersistence(self.path)
        with store.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER reject_m5d_human_review_audit
                   BEFORE INSERT ON workflow_audit_events
                   WHEN NEW.kind = 'HUMAN_REVIEW'
                   BEGIN SELECT RAISE(ABORT, 'forced audit failure'); END"""
            )

        with self.assertRaises(PersistenceError):
            self.service.review(
                proposal.id, ProposalStatus.APPROVED, self.review,
                workflow_id=binding.workflow_id, review_id=binding.review_id,
            )
        self.assertEqual(self.service.get(proposal.id), proposal)
        with store.transaction() as connection:
            run = connection.execute(
                "SELECT status FROM workflow_runs WHERE workflow_id = ?",
                (str(binding.workflow_id),),
            ).fetchone()
            events = connection.execute(
                "SELECT kind FROM workflow_audit_events WHERE workflow_id = ? ORDER BY sequence",
                (str(binding.workflow_id),),
            ).fetchall()
        self.assertEqual(run["status"], "REVIEW_REQUIRED")
        self.assertEqual([event["kind"] for event in events], ["REVIEW_REQUIRED"])

    def test_concurrent_reviews_across_repository_instances_have_one_winner(self):
        proposal = self.proposal()
        _, first, _ = self.stack()
        _, second, _ = self.stack()
        barrier = threading.Barrier(2)

        def decide(arguments):
            service, decision = arguments
            barrier.wait(timeout=5)
            try:
                return service.review(proposal.id, decision, self.review).status
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, (
                (first, ProposalStatus.APPROVED),
                (second, ProposalStatus.REJECTED),
            )))
        self.assertEqual(results.count("conflict"), 1)
        winner = next(result for result in results if result != "conflict")
        self.assertEqual(self.service.get(proposal.id).status, winner)

    def test_cross_workflow_stale_and_reused_bindings_fail_closed(self):
        proposal = self.proposal()
        binding = self.bind(proposal)
        other = self.proposal()
        other_case = self.create_case(self.cases, "Other review")

        with self.assertRaisesRegex(ConflictError, "active workflow"):
            self.service.review(proposal.id, ProposalStatus.APPROVED, self.review)
        with self.assertRaisesRegex(ConflictError, "active workflow"):
            self.service.review(
                proposal.id, ProposalStatus.APPROVED, self.review,
                workflow_id=uuid4(), review_id=binding.review_id,
            )
        with self.assertRaisesRegex(ConflictError, "active workflow"):
            self.service.review(
                proposal.id, ProposalStatus.APPROVED, self.review,
                workflow_id=binding.workflow_id, review_id=uuid4(),
            )
        with self.assertRaisesRegex(ConflictError, "cannot be bound"):
            self.service.bind_workflow_review(
                other.id, workflow_id=uuid4(), case_id=other_case.id, review_id=uuid4(),
            )
        with self.assertRaisesRegex(ConflictError, "already in use"):
            self.service.bind_workflow_review(
                other.id, workflow_id=binding.workflow_id,
                case_id=other.case_id, review_id=uuid4(),
            )
        store = SQLitePersistence(self.path)
        with store.transaction() as connection:
            connection.execute(
                """UPDATE workflow_runs
                   SET status = 'RECOVERY_REQUIRED', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+1 second')
                   WHERE workflow_id = ?""",
                (str(binding.workflow_id),),
            )
        with self.assertRaisesRegex(ConflictError, "stale or invalid"):
            self.service.review(
                proposal.id, ProposalStatus.APPROVED, self.review,
                workflow_id=binding.workflow_id, review_id=binding.review_id,
            )
        self.assertEqual(self.service.get(proposal.id), proposal)
        self.assertEqual(self.service.get(other.id), other)

    def test_proposals_bindings_and_audit_events_are_immutable(self):
        proposal = self.proposal()
        binding = self.bind(proposal)
        store = SQLitePersistence(self.path)
        mutations = (
            ("UPDATE resolution_proposals SET rationale = ? WHERE proposal_id = ?",
             ("forged", str(proposal.id))),
            ("UPDATE workflow_review_bindings SET review_id = ? WHERE proposal_id = ?",
             (str(uuid4()), str(proposal.id))),
            ("DELETE FROM workflow_audit_events WHERE workflow_id = ?",
             (str(binding.workflow_id),)),
        )
        for statement, parameters in mutations:
            with self.subTest(statement=statement), self.assertRaises(PersistenceError):
                with store.transaction() as connection:
                    connection.execute(statement, parameters)

    def test_default_proposal_service_remains_process_local(self):
        memory_cases = CaseService()
        memory_operations = BusinessOperations(memory_cases)
        service = ProposalService(memory_cases, memory_operations)
        self.assertIsNone(service._repository)


if __name__ == "__main__":
    unittest.main()
