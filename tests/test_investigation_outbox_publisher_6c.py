"""Offline publisher contract tests for Task 6C."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest
from uuid import uuid4

from backend.app.investigation_outbox_publisher import InvestigationOutboxPublisher
from backend.app.postgres_persistence import InvestigationOutboxEvent


def _event(*, attempts=0, exhausted=False):
    return InvestigationOutboxEvent(
        outbox_id=uuid4(), job_id=uuid4(), case_id=uuid4(),
        event_type="INVESTIGATION_DISPATCH_REQUESTED", event_version=1,
        idempotency_key=uuid4(), request_fingerprint="a" * 64,
        payload_json="{}", status="PENDING", publication_attempts=attempts,
        last_error_category=None, message_id=None,
        created_at="2026-10-26T00:00:00+00:00", published_at=None,
        lease_id=uuid4(), leased_by=uuid4(),
        lease_expires_at="2026-10-26T00:01:00+00:00",
        next_attempt_at=None, retry_exhausted=exhausted,
    )


class _FakeSQS:
    def __init__(self, result="sqs-message-1", error=None):
        self.result = result
        self.error = error
        self.calls = []

    def send_message(self, *, body, message_attributes):
        self.calls.append((body, message_attributes))
        if self.error is not None:
            raise self.error
        return self.result


class _FakeRepository:
    def __init__(self, event):
        self.event = event
        self.calls = []

    def claim_pending(self, **kwargs):
        self.calls.append(("claim", kwargs))
        return self.event

    def mark_published(self, **kwargs):
        self.calls.append(("published", kwargs))
        self.event = replace(self.event, status="PUBLISHED", message_id=kwargs["message_id"],
                             published_at="2026-10-26T00:00:00+00:00",
                             lease_id=None, leased_by=None, lease_expires_at=None)
        return self.event

    def record_publish_failure(self, **kwargs):
        self.calls.append(("failed", kwargs))
        exhausted = self.event.publication_attempts >= kwargs["max_attempts"]
        self.event = replace(self.event, last_error_category=kwargs["error_category"],
                             retry_exhausted=exhausted, lease_id=None,
                             leased_by=None, lease_expires_at=None)
        return self.event


class InvestigationOutboxPublisherTests(unittest.TestCase):
    NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)

    def test_success_sends_only_versioned_references_and_confirms_after_send(self):
        repository = _FakeRepository(_event())
        sqs = _FakeSQS()
        result = InvestigationOutboxPublisher(
            repository, sqs, publisher_id=uuid4(),
        ).publish_one(now=self.NOW)
        self.assertEqual(result.status, "PUBLISHED")
        body, attributes = sqs.calls[0]
        payload = json.loads(body)
        self.assertEqual(set(payload), {"job_id", "outbox_id", "event_type", "schema_version"})
        self.assertEqual(attributes["schema_version"], "1")
        self.assertEqual([call[0] for call in repository.calls], ["claim", "published"])

    def test_publish_failure_is_bounded_and_retryable(self):
        repository = _FakeRepository(_event(attempts=1))
        sqs = _FakeSQS(error=TimeoutError("mock timeout"))
        result = InvestigationOutboxPublisher(
            repository, sqs, publisher_id=uuid4(), max_attempts=3,
        ).publish_one(now=self.NOW)
        self.assertEqual(result.status, "RETRYABLE_FAILURE")
        self.assertEqual(repository.calls[-1][0], "failed")
        self.assertEqual(repository.calls[-1][1]["error_category"], "SQS_PUBLISH_FAILED")

    def test_max_attempts_becomes_exhausted_without_claiming_again(self):
        repository = _FakeRepository(_event(attempts=1))
        sqs = _FakeSQS(error=RuntimeError("mock failure"))
        result = InvestigationOutboxPublisher(
            repository, sqs, publisher_id=uuid4(), max_attempts=1,
        ).publish_one(now=self.NOW)
        self.assertEqual(result.status, "RETRY_EXHAUSTED")
        self.assertTrue(repository.event.retry_exhausted)

    def test_empty_publish_confirmation_is_not_marked_published(self):
        repository = _FakeRepository(_event())
        sqs = _FakeSQS(result="")
        result = InvestigationOutboxPublisher(
            repository, sqs, publisher_id=uuid4(),
        ).publish_one(now=self.NOW)
        self.assertEqual(result.status, "RETRYABLE_FAILURE")
        self.assertEqual([call[0] for call in repository.calls], ["claim", "failed"])
