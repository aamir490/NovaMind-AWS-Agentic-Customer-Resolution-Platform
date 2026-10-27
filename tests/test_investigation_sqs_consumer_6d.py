"""Task 6D consumer tests with no AWS calls."""

from datetime import datetime, timezone
import json
import os
import re
from urllib.parse import unquote, urlsplit
import unittest
from uuid import uuid4

from backend.app.investigation_sqs_consumer import (
    InvestigationSQSConsumer, SQSReceivedMessage,
)
from backend.app.postgres_persistence import InvestigationOutboxEvent


def _event(status="PUBLISHED"):
    return InvestigationOutboxEvent(
        outbox_id=uuid4(), job_id=uuid4(), case_id=uuid4(),
        event_type="INVESTIGATION_DISPATCH_REQUESTED", event_version=1,
        idempotency_key=uuid4(), request_fingerprint="a" * 64,
        payload_json="{}", status=status, publication_attempts=1,
        last_error_category=None, message_id="sqs-1",
        created_at="2026-10-26T00:00:00+00:00",
        published_at="2026-10-26T00:00:01+00:00",
        lease_id=None, leased_by=None, lease_expires_at=None,
        next_attempt_at=None, retry_exhausted=False,
    )


def _body(event, **overrides):
    payload = {
        "job_id": str(event.job_id), "outbox_id": str(event.outbox_id),
        "event_type": event.event_type, "schema_version": event.event_version,
    }
    payload.update(overrides)
    return json.dumps(payload)


class _FakeSQS:
    def __init__(self):
        self.deleted = []
        self.visibility = []
        self.received = []

    def receive_messages(self, *, max_messages, visibility_timeout):
        self.received.append((max_messages, visibility_timeout))
        return []

    def delete_message(self, *, receipt_handle):
        self.deleted.append(receipt_handle)

    def change_message_visibility(self, *, receipt_handle, visibility_timeout):
        self.visibility.append((receipt_handle, visibility_timeout))


class _FakeOutbox:
    def __init__(self, event):
        self.event = event

    def get_by_job(self, job_id):
        if job_id != self.event.job_id:
            raise LookupError("missing")
        return self.event


class _FakeResults:
    def __init__(self, completed=False):
        self.completed = completed

    def get_by_job(self, job_id):
        if not self.completed:
            raise LookupError("not completed")
        return type("Result", (), {"status": "COMPLETED"})()


class _FakeWorker:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def run_once(self, job_id, *, worker_id):
        self.calls.append((job_id, worker_id))
        if self.error:
            raise self.error
        return self.result


class _RecoverySink:
    def __init__(self):
        self.calls = []

    def require_review(self, *, message, error_category):
        self.calls.append((message.receipt_handle, error_category))


class InvestigationSQSConsumerTests(unittest.TestCase):
    def setUp(self):
        self.event = _event()
        self.sqs = _FakeSQS()
        self.results = _FakeResults()
        self.worker = _FakeWorker()
        self.worker_id = uuid4()
        self.consumer = InvestigationSQSConsumer(
            self.sqs, outbox=_FakeOutbox(self.event), results=self.results,
            worker=self.worker, server_worker_id=self.worker_id,
            max_messages=2, visibility_timeout=60, retry_visibility_timeout=10,
        )
        self.message = SQSReceivedMessage(_body(self.event), "receipt-1")

    def test_success_calls_fenced_worker_with_independent_identity_and_acks_durable_result(self):
        self.results.completed = True
        outcome = self.consumer.process_message(self.message)
        self.assertEqual(outcome.status, "ACKNOWLEDGED")
        self.assertEqual(self.worker.calls, [])
        self.assertEqual(self.sqs.deleted, ["receipt-1"])

        # A non-completed delivery invokes run_once with the server-owned ID.
        self.results.completed = False
        self.worker.result = object()
        outcome = self.consumer.process_message(self.message)
        self.assertEqual(outcome.status, "RETRYABLE")
        self.assertEqual(self.worker.calls[-1], (self.event.job_id, self.worker_id))
        self.assertEqual(self.sqs.deleted, ["receipt-1"])

    def test_invalid_reference_and_worker_error_remain_retryable(self):
        invalid = SQSReceivedMessage(_body(self.event, schema_version=99), "bad")
        self.assertEqual(self.consumer.process_message(invalid).status, "RETRYABLE")
        self.worker.error = RuntimeError("mock worker failure")
        self.assertEqual(self.consumer.process_message(self.message).status, "RETRYABLE")
        self.assertEqual(self.sqs.deleted, [])
        self.assertGreaterEqual(len(self.sqs.visibility), 2)

    def test_poison_delivery_requests_operator_review_and_never_deletes(self):
        sink = _RecoverySink()
        consumer = InvestigationSQSConsumer(
            self.sqs, outbox=_FakeOutbox(self.event), results=self.results,
            worker=self.worker, server_worker_id=self.worker_id,
            max_receive_count=3, operator_recovery_sink=sink,
        )
        poison = SQSReceivedMessage(
            _body(self.event, schema_version=99), "poison", receive_count=3,
        )
        outcome = consumer.process_message(poison)
        self.assertEqual(outcome.status, "DLQ_REVIEW_REQUIRED")
        self.assertEqual(self.sqs.deleted, [])
        self.assertEqual(sink.calls, [("poison", "UNSUPPORTED_SQS_REFERENCE")])
        self.assertEqual(self.sqs.visibility, [("poison", 0)])

    def test_stale_delivery_requests_review_at_receive_limit_without_ack(self):
        sink = _RecoverySink()
        consumer = InvestigationSQSConsumer(
            self.sqs, outbox=_FakeOutbox(self.event), results=self.results,
            worker=self.worker, server_worker_id=self.worker_id,
            max_receive_count=1, operator_recovery_sink=sink,
        )
        stale = SQSReceivedMessage(_body(self.event), "stale", receive_count=1)
        outcome = consumer.process_message(stale)
        self.assertEqual(outcome.status, "DLQ_REVIEW_REQUIRED")
        self.assertEqual(outcome.error_category, "DURABLE_COMPLETION_UNCONFIRMED")
        self.assertEqual(sink.calls, [("stale", "DURABLE_COMPLETION_UNCONFIRMED")])
        self.assertEqual(self.sqs.deleted, [])
        self.assertEqual(self.sqs.visibility[0], ("stale", 120))
        self.assertEqual(self.sqs.visibility[-1], ("stale", 0))

    def test_processing_visibility_extension_failure_prevents_worker_execution(self):
        class VisibilityFailSQS(_FakeSQS):
            def change_message_visibility(self, *, receipt_handle, visibility_timeout):
                raise RuntimeError("visibility unavailable")

        sqs = VisibilityFailSQS()
        consumer = InvestigationSQSConsumer(
            sqs, outbox=_FakeOutbox(self.event), results=self.results,
            worker=self.worker, server_worker_id=self.worker_id,
        )
        outcome = consumer.process_message(self.message)
        self.assertEqual(outcome.error_category, "SQS_VISIBILITY_EXTENSION_FAILED")
        self.assertEqual(self.worker.calls, [])
        self.assertEqual(sqs.deleted, [])

    def test_stale_or_active_claim_without_result_is_not_acknowledged(self):
        self.worker.result = None
        outcome = self.consumer.process_message(self.message)
        self.assertEqual(outcome.error_category, "DURABLE_COMPLETION_UNCONFIRMED")
        self.assertEqual(self.sqs.deleted, [])

    def test_delete_failure_remains_retryable_and_poll_is_bounded(self):
        class DeleteFailSQS(_FakeSQS):
            def delete_message(self, *, receipt_handle):
                raise RuntimeError("delete unavailable")

        sqs = DeleteFailSQS()
        results = _FakeResults(completed=True)
        consumer = InvestigationSQSConsumer(
            sqs, outbox=_FakeOutbox(self.event), results=results,
            worker=self.worker, server_worker_id=uuid4(), max_messages=1,
        )
        outcome = consumer.process_message(self.message)
        self.assertEqual(outcome.error_category, "SQS_DELETE_FAILED")
        self.assertEqual(consumer.poll_once(), ())
        self.assertEqual(sqs.received, [(1, 120)])


_DISPOSABLE_DATABASE = re.compile(r"novamind_phase_c_test(?:_[a-z0-9_]{1,48})?$")


@unittest.skipUnless(os.environ.get("NOVAMIND_POSTGRES_TEST_DSN"), "NOVAMIND_POSTGRES_TEST_DSN is not configured")
class PostgreSQLSQSConsumerIntegrationTests(unittest.TestCase):
    """Database-backed reference and duplicate checks; no AWS is used."""

    @classmethod
    def setUpClass(cls):
        dsn = os.environ["NOVAMIND_POSTGRES_TEST_DSN"]
        parsed = urlsplit(dsn)
        database = unquote(parsed.path.lstrip("/"))
        if (parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname
                or not _DISPOSABLE_DATABASE.fullmatch(database)):
            raise RuntimeError("NOVAMIND_POSTGRES_TEST_DSN must target a disposable novamind_phase_c_test database")
        cls.dsn = dsn
        from psycopg import connect
        connection = connect(dsn, autocommit=True, connect_timeout=5)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                if cursor.fetchone()[0] != database:
                    raise RuntimeError("PostgreSQL database identity could not be verified")
                cursor.execute("DROP SCHEMA public CASCADE")
                cursor.execute("CREATE SCHEMA public")
        finally:
            connection.close()

    @classmethod
    def tearDownClass(cls):
        from psycopg import connect
        connection = connect(cls.dsn, autocommit=True, connect_timeout=5)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                expected = unquote(urlsplit(cls.dsn).path.lstrip("/"))
                if cursor.fetchone()[0] != expected:
                    raise RuntimeError("PostgreSQL database identity changed")
                cursor.execute("DROP SCHEMA public CASCADE")
        finally:
            connection.close()

    def setUp(self):
        from datetime import timedelta
        from backend.app.domain import CaseStatus, SupportCase
        from backend.app.postgres_persistence import (
            PostgreSQLCaseRepository, PostgreSQLInvestigationJobRepository,
            PostgreSQLInvestigationOutboxRepository, PostgreSQLPersistence,
        )
        from backend.app.schemas import CustomerCreate, OrderCreate
        from backend.app.service import CaseService
        self.store = PostgreSQLPersistence(self.dsn, initialize=True)
        self.cases = CaseService(PostgreSQLCaseRepository(self.store))
        self.jobs = PostgreSQLInvestigationJobRepository(self.store)
        self.outbox = PostgreSQLInvestigationOutboxRepository(self.store)
        self.customer = self.cases.create_customer(CustomerCreate(name="Consumer customer"))
        self.order = self.cases.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "CONSUMER-1", "name": "Consumer item", "quantity": 1}],
        ))
        now = datetime.now(timezone.utc)
        case = SupportCase(
            id=uuid4(), customer_id=self.customer.id, order_id=self.order.id,
            subject="Consumer case", description="Consumer integration fixture",
            status=CaseStatus.OPEN, created_at=now, updated_at=now,
        )
        job = self.jobs.submit(case, owner_user_id=uuid4(), idempotency_key=uuid4())[1]
        self.jobs.register_manual_start(job.id, admin_user_id=uuid4(), idempotency_key=uuid4())
        self.event = self.outbox.get_by_job(job.id)

    def test_pending_reference_is_retryable_until_durable_completion(self):
        # The real outbox row is still PENDING, so a delivery cannot be
        # acknowledged even when a result lookup happens to report complete.
        consumer = InvestigationSQSConsumer(
            _FakeSQS(), outbox=self.outbox, results=_FakeResults(completed=True),
            worker=_FakeWorker(), server_worker_id=uuid4(),
        )
        message = SQSReceivedMessage(_body(self.event), "receipt-pending")
        outcome = consumer.process_message(message)
        self.assertEqual(outcome.status, "RETRYABLE")

    def test_duplicate_delivery_rechecks_real_outbox_and_durable_completion(self):
        publisher_id = uuid4()
        now = datetime.now(timezone.utc)
        lease = self.outbox.claim_pending(
            publisher_id=publisher_id, lease_seconds=30, max_attempts=3, now=now,
        )
        published = self.outbox.mark_published(
            outbox_id=lease.outbox_id, publisher_id=publisher_id,
            lease_id=lease.lease_id, message_id="mock-sqs-message", now=now,
        )
        sqs = _FakeSQS()
        worker = _FakeWorker()
        consumer = InvestigationSQSConsumer(
            sqs, outbox=self.outbox, results=_FakeResults(completed=True),
            worker=worker, server_worker_id=uuid4(),
        )
        outcome = consumer.process_message(
            SQSReceivedMessage(_body(published), "receipt-duplicate"),
        )
        self.assertEqual(outcome.status, "ACKNOWLEDGED")
        self.assertEqual(worker.calls, [])
        self.assertEqual(sqs.deleted, ["receipt-duplicate"])
