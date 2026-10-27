"""Focused M5E1 durable metadata tests; no LangGraph checkpoint recovery."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from uuid import uuid4

from backend.app.persistence import (
    PersistenceError, SQLiteCaseRepository, SQLitePersistence,
    SQLiteWorkflowMetadataRepository,
)
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.service import CaseService, ConflictError
from backend.app.workflow_metadata import (
    WorkflowMetadataStatus, WorkflowRunReservation,
)


class SQLiteWorkflowMetadataRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "novamind.sqlite"
        store = SQLitePersistence(self.path, initialize=True)
        self.cases = CaseService(SQLiteCaseRepository(store))
        customer = self.cases.create_customer(CustomerCreate(name="Workflow metadata owner"))
        order = self.cases.create_order(OrderCreate(
            customer_id=customer.id,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))
        self.case = self.cases.create_case(CaseCreate(
            customer_id=customer.id, order_id=order.id,
            subject="Durable metadata", description="Metadata without checkpoint recovery.",
        ))
        self.owner_id = uuid4()
        self.request_id = uuid4()
        self.repository = SQLiteWorkflowMetadataRepository(store)

    def reopened(self):
        return SQLiteWorkflowMetadataRepository(SQLitePersistence(self.path))

    def reservation(self, **changes):
        values = {
            "workflow_id": uuid4(), "run_id": uuid4(), "case_id": self.case.id,
            "owner_user_id": self.owner_id, "request_id": self.request_id,
            "request_fingerprint": sha256(b"stable workflow request").hexdigest(),
            "provider": "local_scripted",
        }
        values.update(changes)
        return WorkflowRunReservation(**values)

    def test_reservation_status_and_events_survive_recreation(self):
        created = self.repository.reserve(self.reservation())
        self.assertTrue(created.created)
        self.assertFalse(created.replayed)
        paused = self.repository.transition(
            created.run.workflow_id, owner_user_id=self.owner_id, case_id=self.case.id,
            expected_status=WorkflowMetadataStatus.RUNNING,
            status=WorkflowMetadataStatus.REVIEW_REQUIRED,
        )

        recreated = self.reopened()
        self.assertEqual(recreated.get(created.run.workflow_id), paused)
        self.assertEqual(recreated.get_by_run_id(created.run.run_id), paused)
        self.assertEqual(recreated.get_by_request(self.owner_id, self.request_id), paused)
        events = recreated.events(created.run.workflow_id)
        self.assertEqual([event.kind for event in events], ["CREATED", "STATUS_CHANGED"])
        self.assertEqual([event.status for event in events], [
            WorkflowMetadataStatus.RUNNING, WorkflowMetadataStatus.REVIEW_REQUIRED,
        ])

    def test_request_id_is_idempotent_but_never_replays_partial_work(self):
        first_request = self.reservation()
        first = self.repository.reserve(first_request)
        self.repository.require_recovery(
            first.run.workflow_id, owner_user_id=self.owner_id, case_id=self.case.id,
            expected_status=WorkflowMetadataStatus.RUNNING,
        )
        duplicate = self.reopened().reserve(self.reservation(
            workflow_id=uuid4(), run_id=uuid4(),
            request_fingerprint=first_request.request_fingerprint,
        ))
        self.assertFalse(duplicate.created)
        self.assertFalse(duplicate.replayed)
        self.assertEqual(duplicate.run.workflow_id, first.run.workflow_id)
        self.assertEqual(duplicate.run.status, WorkflowMetadataStatus.RECOVERY_REQUIRED)

        with self.assertRaisesRegex(ConflictError, "Request ID conflicts"):
            self.repository.reserve(self.reservation(
                request_fingerprint=sha256(b"different request").hexdigest(),
            ))
        self.assertEqual(
            self.repository.get(first.run.workflow_id).status,
            WorkflowMetadataStatus.RECOVERY_REQUIRED,
        )

    def test_agent_failure_is_sanitized_and_idempotent(self):
        reservation = self.reservation()
        first = self.repository.reserve(reservation).run
        failed = self.repository.transition(
            first.workflow_id, owner_user_id=self.owner_id, case_id=self.case.id,
            expected_status=WorkflowMetadataStatus.RUNNING, status=WorkflowMetadataStatus.FAILED,
            failure_category="TIMEOUT", failure_stage="AGENT",
        )
        self.assertEqual((failed.failure_category, failed.failure_stage), ("TIMEOUT", "AGENT"))
        duplicate = self.reopened().reserve(self.reservation(
            workflow_id=uuid4(), run_id=uuid4(), request_fingerprint=reservation.request_fingerprint,
        ))
        self.assertEqual(duplicate.run, failed)

    def test_invalid_response_details_are_durable_closed_and_atomic(self):
        valid = self.repository.reserve(self.reservation()).run
        failed = self.repository.transition(
            valid.workflow_id, owner_user_id=self.owner_id, case_id=self.case.id,
            expected_status=WorkflowMetadataStatus.RUNNING, status=WorkflowMetadataStatus.FAILED,
            failure_category="INVALID_RESPONSE", failure_stage="AGENT",
            invalid_response_stage="output_schema",
        )
        reopened = self.reopened().get(valid.workflow_id)
        self.assertEqual(reopened, failed)
        self.assertEqual(
            (reopened.invalid_response_stage, reopened.invalid_response_category),
            ("output_schema", None),
        )

        invalid_values = (
            ("TIMEOUT", "AGENT", "provider_envelope", None),
            ("INVALID_RESPONSE", "AGENT", None, "bedrock_empty_output"),
            ("INVALID_RESPONSE", "AGENT", "output_schema", "bedrock_empty_output"),
            ("INVALID_RESPONSE", "AGENT", "private-stage", None),
        )
        for index, values in enumerate(invalid_values):
            run = self.repository.reserve(self.reservation(
                workflow_id=uuid4(), run_id=uuid4(), request_id=uuid4(),
                request_fingerprint=sha256(f"invalid-{index}".encode()).hexdigest(),
            )).run
            with self.subTest(values=values), self.assertRaises(ConflictError):
                self.repository.transition(
                    run.workflow_id, owner_user_id=self.owner_id, case_id=self.case.id,
                    expected_status=WorkflowMetadataStatus.RUNNING,
                    status=WorkflowMetadataStatus.FAILED,
                    failure_category=values[0], failure_stage=values[1],
                    invalid_response_stage=values[2], invalid_response_category=values[3],
                )
            self.assertEqual(self.repository.get(run.workflow_id), run)

    def test_raw_or_invalid_failure_values_are_rejected_without_persistence(self):
        run = self.repository.reserve(self.reservation()).run
        for category, stage in (("provider password=secret", "AGENT"), ("TIMEOUT", "WORKFLOW")):
            with self.subTest(category=category, stage=stage), self.assertRaisesRegex(ConflictError, "failure metadata"):
                self.repository.transition(
                    run.workflow_id, owner_user_id=self.owner_id, case_id=self.case.id,
                    expected_status=WorkflowMetadataStatus.RUNNING, status=WorkflowMetadataStatus.FAILED,
                    failure_category=category, failure_stage=stage,
                )
        self.assertEqual(self.repository.get(run.workflow_id), run)

    def test_concurrent_duplicate_reservations_have_one_creation(self):
        fingerprint = sha256(b"concurrent request").hexdigest()
        barrier = threading.Barrier(2)

        def reserve(_):
            repository = self.reopened()
            request = self.reservation(
                workflow_id=uuid4(), run_id=uuid4(), request_fingerprint=fingerprint,
            )
            barrier.wait(timeout=5)
            return repository.reserve(request)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(result.created for result in results), 1)
        self.assertEqual(len({result.run.workflow_id for result in results}), 1)
        self.assertTrue(all(not result.replayed for result in results))
        self.assertEqual(len(self.repository.events(results[0].run.workflow_id)), 1)

    def test_concurrent_and_stale_transitions_have_one_winner(self):
        run = self.repository.reserve(self.reservation()).run
        barrier = threading.Barrier(2)

        def transition(status):
            repository = self.reopened()
            barrier.wait(timeout=5)
            try:
                return repository.transition(
                    run.workflow_id, owner_user_id=self.owner_id, case_id=self.case.id,
                    expected_status=WorkflowMetadataStatus.RUNNING, status=status,
                ).status
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(transition, (
                WorkflowMetadataStatus.COMPLETED, WorkflowMetadataStatus.FAILED,
            )))
        self.assertEqual(results.count("conflict"), 1)
        winner = next(result for result in results if result != "conflict")
        self.assertEqual(self.repository.get(run.workflow_id).status, winner)
        self.assertEqual(len(self.repository.events(run.workflow_id)), 2)

    def test_visible_case_runs_paginate_filter_owner_and_exclude_recovery_required(self):
        first = self.repository.reserve(self.reservation()).run
        self.repository.transition(
            first.workflow_id, owner_user_id=self.owner_id, case_id=self.case.id,
            expected_status=WorkflowMetadataStatus.RUNNING,
            status=WorkflowMetadataStatus.COMPLETED,
        )
        second = self.repository.reserve(self.reservation(
            workflow_id=uuid4(), run_id=uuid4(), request_id=uuid4(),
            request_fingerprint=sha256(b"second visible workflow").hexdigest(),
        )).run
        self.repository.transition(
            second.workflow_id, owner_user_id=self.owner_id, case_id=self.case.id,
            expected_status=WorkflowMetadataStatus.RUNNING,
            status=WorkflowMetadataStatus.FAILED,
        )
        hidden = self.repository.reserve(self.reservation(
            workflow_id=uuid4(), run_id=uuid4(), request_id=uuid4(),
            request_fingerprint=sha256(b"recovery workflow").hexdigest(),
        )).run
        self.repository.require_recovery(
            hidden.workflow_id, owner_user_id=self.owner_id, case_id=self.case.id,
            expected_status=WorkflowMetadataStatus.RUNNING,
        )

        first_page = self.reopened().visible_runs_for_case(self.case.id, self.owner_id, 0, 1)
        second_page = self.reopened().visible_runs_for_case(self.case.id, self.owner_id, 1, 1)
        self.assertEqual([run.run_id for run in (*first_page, *second_page)], [first.run_id, second.run_id])
        self.assertEqual(self.reopened().visible_runs_for_case(self.case.id, uuid4(), 0, 10), ())
        self.assertNotIn(hidden.run_id, {run.run_id for run in self.reopened().visible_runs_for_case(
            self.case.id, None, 0, 10,
        )})

    def test_event_failure_rolls_back_status_transition(self):
        run = self.repository.reserve(self.reservation()).run
        store = SQLitePersistence(self.path)
        with store.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER reject_m5e1_status_event
                   BEFORE INSERT ON workflow_run_events
                   WHEN NEW.kind = 'STATUS_CHANGED'
                   BEGIN SELECT RAISE(ABORT, 'forced metadata event failure'); END"""
            )
        with self.assertRaises(PersistenceError):
            self.repository.transition(
                run.workflow_id, owner_user_id=self.owner_id, case_id=self.case.id,
                expected_status=WorkflowMetadataStatus.RUNNING,
                status=WorkflowMetadataStatus.COMPLETED,
            )
        self.assertEqual(self.repository.get(run.workflow_id), run)
        self.assertEqual([event.kind for event in self.repository.events(run.workflow_id)], ["CREATED"])

    def test_owner_case_transition_and_identity_integrity_fail_closed(self):
        run = self.repository.reserve(self.reservation()).run
        for owner_id, case_id in (
            (uuid4(), self.case.id),
            (self.owner_id, uuid4()),
        ):
            with self.subTest(owner_id=owner_id, case_id=case_id):
                with self.assertRaisesRegex(ConflictError, "owner and case"):
                    self.repository.transition(
                        run.workflow_id, owner_user_id=owner_id, case_id=case_id,
                        expected_status=WorkflowMetadataStatus.RUNNING,
                        status=WorkflowMetadataStatus.COMPLETED,
                    )

        store = SQLitePersistence(self.path)
        mutations = (
            ("UPDATE workflow_runs SET case_id = ? WHERE workflow_id = ?",
             (str(uuid4()), str(run.workflow_id))),
            ("UPDATE workflow_runs SET request_fingerprint = ? WHERE workflow_id = ?",
             (sha256(b"forged").hexdigest(), str(run.workflow_id))),
            ("DELETE FROM workflow_run_events WHERE workflow_id = ?",
             (str(run.workflow_id),)),
        )
        for statement, parameters in mutations:
            with self.subTest(statement=statement), self.assertRaises(PersistenceError):
                with store.transaction() as connection:
                    connection.execute(statement, parameters)
        self.assertEqual(self.repository.get(run.workflow_id), run)

    def test_status_without_matching_event_is_detected_as_incomplete(self):
        run = self.repository.reserve(self.reservation()).run
        store = SQLitePersistence(self.path)
        with store.transaction() as connection:
            connection.execute(
                """UPDATE workflow_runs
                   SET status = 'FAILED', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+1 second')
                   WHERE workflow_id = ?""",
                (str(run.workflow_id),),
            )
        with self.assertRaises(PersistenceError):
            self.repository.events(run.workflow_id)


if __name__ == "__main__":
    unittest.main()
