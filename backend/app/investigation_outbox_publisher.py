"""Transactional-outbox publisher boundary for investigation dispatch.

The adapter is intentionally injectable.  PostgreSQL lease transactions end
before ``send_message`` is called, and the confirmation is committed through a
separate fenced transaction.  A crash between those operations may publish a
duplicate reference; the durable consumer contract must therefore remain
idempotent.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Mapping, Protocol
from uuid import UUID

from .postgres_persistence import (
    InvestigationOutboxEvent, PostgreSQLInvestigationOutboxRepository,
)


class SQSAdapter(Protocol):
    def send_message(self, *, body: str,
                     message_attributes: Mapping[str, str]) -> str:
        """Return the provider confirmation/message ID or raise."""


@dataclass(frozen=True)
class OutboxPublishOutcome:
    outbox_id: UUID
    status: str
    message_id: str | None = None
    error_category: str | None = None


class InvestigationOutboxPublisher:
    """Publish at most one fenced outbox event per bounded call."""

    def __init__(self, repository: PostgreSQLInvestigationOutboxRepository,
                 sqs: SQSAdapter, *, publisher_id: UUID,
                 lease_seconds: int = 30, max_attempts: int = 5,
                 retry_after_seconds: int = 5) -> None:
        if lease_seconds < 1 or max_attempts < 1 or retry_after_seconds < 0:
            raise ValueError("invalid outbox publisher bounds")
        self._repository = repository
        self._sqs = sqs
        self._publisher_id = UUID(str(publisher_id))
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._retry_after_seconds = retry_after_seconds

    @staticmethod
    def _message(event: InvestigationOutboxEvent) -> tuple[str, dict[str, str]]:
        """Build a bounded reference-only message from durable columns."""
        body = json.dumps({
            "job_id": str(event.job_id),
            "outbox_id": str(event.outbox_id),
            "event_type": event.event_type,
            "schema_version": event.event_version,
        }, sort_keys=True, separators=(",", ":"))
        attributes = {
            "event_type": event.event_type,
            "schema_version": str(event.event_version),
        }
        return body, attributes

    def publish_one(self, *, now: datetime | None = None) -> OutboxPublishOutcome | None:
        observed = datetime.now(timezone.utc) if now is None else now
        lease = self._repository.claim_pending(
            publisher_id=self._publisher_id, lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts, now=observed,
        )
        if lease is None:
            return None

        # This call is deliberately outside the repository transaction.  A
        # process crash after send and before mark_published is safe: the lease
        # expires, the same reference can be republished, and the consumer must
        # use PostgreSQL idempotency/fencing as its authority.
        body, attributes = self._message(lease)
        try:
            message_id = self._sqs.send_message(
                body=body, message_attributes=attributes,
            )
            if not isinstance(message_id, str) or not message_id:
                raise ValueError("SQS publish confirmation was empty")
        except Exception:
            failed = self._repository.record_publish_failure(
                outbox_id=lease.outbox_id, publisher_id=self._publisher_id,
                lease_id=lease.lease_id, error_category="SQS_PUBLISH_FAILED",
                retry_after_seconds=self._retry_after_seconds,
                max_attempts=self._max_attempts, now=observed,
            )
            return OutboxPublishOutcome(
                outbox_id=failed.outbox_id,
                status="RETRY_EXHAUSTED" if failed.retry_exhausted else "RETRYABLE_FAILURE",
                error_category=failed.last_error_category,
            )

        published = self._repository.mark_published(
            outbox_id=lease.outbox_id, publisher_id=self._publisher_id,
            lease_id=lease.lease_id, message_id=message_id, now=observed,
        )
        return OutboxPublishOutcome(
            outbox_id=published.outbox_id, status="PUBLISHED",
            message_id=published.message_id,
        )
