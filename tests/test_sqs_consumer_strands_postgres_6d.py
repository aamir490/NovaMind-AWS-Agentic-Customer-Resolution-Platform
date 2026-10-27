"""Task 6D end-to-end PostgreSQL-fenced Strands consumer coverage.

The only model boundary is a fake ``strands.Agent``.  SQS is also injected;
this suite never creates or contacts AWS resources.
"""

from datetime import datetime, timezone
import json
import os
import re
import sys
import types
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit
import unittest
from uuid import UUID, uuid4

from backend.app.domain import CaseStatus, SupportCase
from backend.app.investigation_sqs_consumer import (
    InvestigationSQSConsumer, SQSReceivedMessage,
)
from backend.app.main import create_app
from backend.app.postgres_persistence import (
    PostgreSQLCaseRepository, PostgreSQLInvestigationJobRepository,
    PostgreSQLInvestigationOutboxRepository, PostgreSQLInvestigationResultRepository,
    PostgreSQLPersistence,
)
from backend.app.schemas import CustomerCreate, OrderCreate
from backend.app.service import CaseService
from backend.app.strands_worker import PostgreSQLStrandsWorker


_EXPECTED_DATABASE = "novamind_phase_c_test_20261026"
_DISPOSABLE_DATABASE = re.compile(r"novamind_phase_c_test_20261026$")


class _MockBedrockResult:
    metrics = types.SimpleNamespace(tool_count=0)

    def __str__(self):
        # Invalid optional model wording intentionally exercises the safe,
        # deterministic grounding fallback while still using real StrandsWorkflow.
        return "not-json"


class _MockStrandsAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __call__(self, prompt):
        return _MockBedrockResult()


class _SQSDelivery:
    def __init__(self, message):
        self.message = message
        self.deleted = []
        self.visibility_changes = []

    def receive_messages(self, *, max_messages, visibility_timeout):
        return [self.message]

    def delete_message(self, *, receipt_handle):
        self.deleted.append(receipt_handle)

    def change_message_visibility(self, *, receipt_handle, visibility_timeout):
        self.visibility_changes.append((receipt_handle, visibility_timeout))


class _FailingResultRepository:
    def __init__(self, real):
        self.real = real

    def get_by_job(self, job_id):
        return self.real.get_by_job(job_id)

    def record_completed(self, *args, **kwargs):
        raise RuntimeError("intentional persistence failure")


@unittest.skipUnless(
    os.environ.get("NOVAMIND_POSTGRES_TEST_DSN"),
    "NOVAMIND_POSTGRES_TEST_DSN is not configured",
)
class PostgreSQLFencedStrandsConsumerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ["NOVAMIND_POSTGRES_TEST_DSN"]
        parsed = urlsplit(dsn)
        database = unquote(parsed.path.lstrip("/"))
        if (parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname
                or not _DISPOSABLE_DATABASE.fullmatch(database)):
            raise RuntimeError(
                "NOVAMIND_POSTGRES_TEST_DSN must target exactly "
                f"{_EXPECTED_DATABASE}"
            )
        cls.dsn = dsn
        from psycopg import connect
        connection = connect(dsn, autocommit=True, connect_timeout=5)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                if cursor.fetchone()[0] != _EXPECTED_DATABASE:
                    raise RuntimeError("PostgreSQL database identity mismatch")
        finally:
            connection.close()

    @classmethod
    def _reset_schema(cls):
        from psycopg import connect
        connection = connect(cls.dsn, autocommit=True, connect_timeout=5)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                if cursor.fetchone()[0] != _EXPECTED_DATABASE:
                    raise RuntimeError("PostgreSQL database identity mismatch")
                cursor.execute("DROP SCHEMA IF EXISTS public CASCADE")
                cursor.execute("CREATE SCHEMA public")
        finally:
            connection.close()

    def setUp(self):
        self._reset_schema()
        self.model_factory = Mock(return_value=object())
        fake_strands = types.ModuleType("strands")
        fake_strands.Agent = _MockStrandsAgent
        fake_strands.tool = lambda function, **kwargs: function
        self.strands_patch = patch.dict(sys.modules, {"strands": fake_strands})
        self.strands_patch.start()

        self.app = create_app(
            postgres_dsn=self.dsn, agent_engine="strands",
            strands_model_factory=self.model_factory,
        )
        cases = self.app.state.case_service
        customer = cases.create_customer(CustomerCreate(name="6D customer"))
        order = cases.create_order(OrderCreate(
            customer_id=customer.id,
            items=[{"sku": "6D-ITEM", "name": "Task 6D item", "quantity": 1}],
        ))
        now = datetime.now(timezone.utc)
        case = SupportCase(
            id=uuid4(), customer_id=customer.id, order_id=order.id,
            subject="Task 6D investigation", description="Consumer integration fixture",
            status=CaseStatus.OPEN, created_at=now, updated_at=now,
        )
        self.jobs = self.app.state.investigation_jobs
        self.outbox = PostgreSQLInvestigationOutboxRepository(self.app.state.persistence)
        self.results = self.app.state.investigation_results
        self.admin_id = uuid4()
        self.job = self.jobs.submit(
            case, owner_user_id=uuid4(), idempotency_key=uuid4(),
        )[1]
        self.jobs.register_manual_start(
            self.job.id, admin_user_id=self.admin_id, idempotency_key=uuid4(),
        )
        publisher_id = uuid4()
        now = datetime.now(timezone.utc)
        lease = self.outbox.claim_pending(
            publisher_id=publisher_id, lease_seconds=30, max_attempts=3, now=now,
        )
        self.event = self.outbox.mark_published(
            outbox_id=lease.outbox_id, publisher_id=publisher_id,
            lease_id=lease.lease_id, message_id="mock-sqs-message", now=now,
        )
        self.server_worker_id = uuid4()
        self.message = SQSReceivedMessage(json.dumps({
            "job_id": str(self.job.id), "outbox_id": str(self.event.outbox_id),
            "event_type": self.event.event_type,
            "schema_version": self.event.event_version,
        }), "receipt-6d")

    def tearDown(self):
        executor = getattr(self.app.state, "investigation_executor", None)
        if executor is not None:
            executor.close()
        self.strands_patch.stop()
        self._reset_schema()

    def consumer(self, *, worker=None, results=None, sqs=None):
        return InvestigationSQSConsumer(
            sqs or _SQSDelivery(self.message),
            outbox=self.outbox, results=results or self.results,
            worker=worker or self.app.state.investigation_executor,
            server_worker_id=self.server_worker_id,
            max_messages=1, visibility_timeout=60,
        )

    def test_real_fenced_run_once_persists_then_deletes_after_commit(self):
        sqs = _SQSDelivery(self.message)
        outcome = self.consumer(sqs=sqs).process_message(self.message)
        self.assertEqual(outcome.status, "ACKNOWLEDGED")
        self.assertEqual(sqs.deleted, ["receipt-6d"])
        durable = self.results.get_by_job(self.job.id)
        self.assertEqual(durable["status"], "COMPLETED")
        self.assertFalse(durable["actions_executed"])
        self.assertEqual(self.model_factory.call_count, 1)

        with self.app.state.persistence.transaction() as connection:
            claimed_by = connection.execute(
                "SELECT claimed_by FROM investigation_jobs WHERE job_id = ?",
                (str(self.job.id),),
            ).fetchone()["claimed_by"]
        self.assertEqual(UUID(claimed_by), self.server_worker_id)
        self.assertNotEqual(self.server_worker_id, self.admin_id)

    def test_duplicate_delivery_rechecks_durable_result_and_acknowledges_without_second_run(self):
        consumer = self.consumer()
        first = consumer.process_message(self.message)
        self.assertEqual(first.status, "ACKNOWLEDGED")
        with patch.object(self.app.state.investigation_executor, "run_once",
                          wraps=self.app.state.investigation_executor.run_once) as run_once:
            second = consumer.process_message(self.message)
        self.assertEqual(second.status, "ACKNOWLEDGED")
        run_once.assert_not_called()

    def test_failed_result_persistence_does_not_acknowledge_delivery(self):
        failing_results = _FailingResultRepository(self.results)
        worker = PostgreSQLStrandsWorker(
            self.jobs, failing_results, tools=self.app.state.local_tools,
            model_factory=self.model_factory,
        )
        sqs = _SQSDelivery(self.message)
        try:
            outcome = self.consumer(worker=worker, results=failing_results, sqs=sqs).process_message(self.message)
        finally:
            worker.close()
        self.assertEqual(outcome.status, "RETRYABLE")
        self.assertEqual(outcome.error_category, "DURABLE_COMPLETION_UNCONFIRMED")
        self.assertEqual(sqs.deleted, [])
        self.assertTrue(sqs.visibility_changes)
        self.assertIsNone(self.results.get_by_job(self.job.id))

    def test_real_postgres_stale_claim_remains_retryable_and_unacknowledged(self):
        other_worker = uuid4()
        claim = self.jobs.claim(
            self.job.id, worker_id=other_worker, lease_seconds=60,
        )
        self.assertIsNotNone(claim)
        sqs = _SQSDelivery(self.message)
        outcome = self.consumer(sqs=sqs).process_message(self.message)
        self.assertEqual(outcome.status, "RETRYABLE")
        self.assertEqual(outcome.error_category, "DURABLE_COMPLETION_UNCONFIRMED")
        self.assertEqual(sqs.deleted, [])
        self.assertEqual(self.jobs.get(self.job.id).claimed_by, other_worker)

    def test_real_postgres_poison_reference_requests_operator_recovery_without_delete(self):
        reviews = []

        class Sink:
            def require_review(self, *, message, error_category):
                reviews.append((message.receipt_handle, error_category))

        poison = SQSReceivedMessage(json.dumps({
            "job_id": str(self.job.id), "outbox_id": str(uuid4()),
            "event_type": self.event.event_type, "schema_version": 1,
        }), "receipt-poison", receive_count=5)
        sqs = _SQSDelivery(poison)
        consumer = self.consumer(sqs=sqs)
        consumer._operator_recovery_sink = Sink()
        consumer._max_receive_count = 5
        outcome = consumer.process_message(poison)
        self.assertEqual(outcome.status, "DLQ_REVIEW_REQUIRED")
        self.assertEqual(sqs.deleted, [])
        self.assertEqual(reviews, [("receipt-poison", "OUTBOX_REFERENCE_MISMATCH")])
        self.assertEqual(sqs.visibility_changes, [("receipt-poison", 0)])
