"""Bounded, injectable SQS consumer for durable Strands investigations."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Callable, Mapping, Protocol, Sequence
from uuid import UUID, uuid4

from .postgres_persistence import (
    InvestigationOutboxEvent, PostgreSQLInvestigationOutboxRepository,
    PostgreSQLInvestigationResultRepository,
)
from .strands_worker import PostgreSQLStrandsWorker


class SQSConsumerAdapter(Protocol):
    def receive_messages(self, *, max_messages: int,
                         visibility_timeout: int) -> Sequence["SQSReceivedMessage"]:
        """Return at most the requested number of leased deliveries."""

    def delete_message(self, *, receipt_handle: str) -> None:
        """Acknowledge one delivery after durable completion."""

    def change_message_visibility(self, *, receipt_handle: str,
                                  visibility_timeout: int) -> None:
        """Keep an unacknowledged delivery retryable."""


class OperatorRecoverySink(Protocol):
    def require_review(self, *, message: "SQSReceivedMessage",
                       error_category: str) -> None:
        """Record/notify explicit operator recovery without acknowledging SQS."""


@dataclass(frozen=True)
class SQSReceivedMessage:
    body: str
    receipt_handle: str
    receive_count: int = 1


@dataclass(frozen=True)
class SQSConsumeOutcome:
    receipt_handle: str
    status: str
    job_id: UUID | None = None
    error_category: str | None = None


class InvestigationSQSConsumer:
    """Process a bounded batch without taking HTTP identity into the worker."""

    ALLOWED_KEYS = frozenset({"job_id", "outbox_id", "event_type", "schema_version"})
    EVENT_TYPE = "INVESTIGATION_DISPATCH_REQUESTED"
    SCHEMA_VERSION = 1

    def __init__(self, sqs: SQSConsumerAdapter, *, outbox,
                 results: PostgreSQLInvestigationResultRepository,
                 worker: PostgreSQLStrandsWorker,
                 server_worker_id: UUID | None = None,
                 max_messages: int = 1, visibility_timeout: int = 120,
                 retry_visibility_timeout: int = 30,
                 max_receive_count: int = 5,
                 operator_recovery_sink: OperatorRecoverySink | Callable[..., None] | None = None) -> None:
        if (max_messages < 1 or visibility_timeout < 1 or retry_visibility_timeout < 1
                or max_receive_count < 1):
            raise ValueError("invalid SQS consumer bounds")
        self._sqs = sqs
        self._outbox: PostgreSQLInvestigationOutboxRepository = outbox
        self._results = results
        self._worker = worker
        # This identity is task-owned and generated independently of any HTTP
        # request.  It is passed explicitly to the fenced worker on every run.
        self._server_worker_id = UUID(str(server_worker_id)) if server_worker_id else uuid4()
        self._max_messages = max_messages
        self._visibility_timeout = visibility_timeout
        self._retry_visibility_timeout = retry_visibility_timeout
        self._max_receive_count = max_receive_count
        self._operator_recovery_sink = operator_recovery_sink

    @property
    def server_worker_id(self) -> UUID:
        return self._server_worker_id

    @classmethod
    def _parse_reference(cls, body: str) -> tuple[UUID, UUID, str, int]:
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            raise ValueError("INVALID_SQS_JSON") from None
        if not isinstance(payload, dict) or set(payload) != cls.ALLOWED_KEYS:
            raise ValueError("INVALID_SQS_REFERENCE")
        try:
            job_id = UUID(str(payload["job_id"]))
            outbox_id = UUID(str(payload["outbox_id"]))
            event_type = payload["event_type"]
            version = payload["schema_version"]
        except (TypeError, ValueError, KeyError):
            raise ValueError("INVALID_SQS_REFERENCE") from None
        if (event_type != cls.EVENT_TYPE or version != cls.SCHEMA_VERSION):
            raise ValueError("UNSUPPORTED_SQS_REFERENCE")
        return job_id, outbox_id, event_type, version

    @staticmethod
    def _durably_completed(results, job_id: UUID) -> bool:
        try:
            result = results.get_by_job(job_id)
        except Exception:
            return False
        status = result.get("status") if isinstance(result, Mapping) else getattr(result, "status", None)
        return status == "COMPLETED"

    def _retry(self, message: SQSReceivedMessage, *, error_category: str,
               dlq_eligible: bool) -> SQSConsumeOutcome:
        dlq_review = dlq_eligible and message.receive_count >= self._max_receive_count
        if dlq_review and self._operator_recovery_sink is not None:
            try:
                sink = self._operator_recovery_sink
                if hasattr(sink, "require_review"):
                    sink.require_review(message=message, error_category=error_category)
                else:
                    sink(message=message, error_category=error_category)
            except Exception:
                # Operator notification failure must not turn an uncertain
                # delivery into an acknowledged or lost delivery.
                pass
        try:
            self._sqs.change_message_visibility(
                receipt_handle=message.receipt_handle,
                visibility_timeout=0 if dlq_review else self._retry_visibility_timeout,
            )
        except Exception:
            # The delivery remains unacknowledged even if visibility extension
            # fails; SQS can redeliver it according to the queue policy.
            pass
        return SQSConsumeOutcome(
            receipt_handle=message.receipt_handle,
            status="DLQ_REVIEW_REQUIRED" if dlq_review else "RETRYABLE",
            error_category=error_category,
        )

    def _extend_processing_visibility(self, message: SQSReceivedMessage) -> bool:
        """Extend once before bounded work; failure leaves the delivery retryable."""
        try:
            self._sqs.change_message_visibility(
                receipt_handle=message.receipt_handle,
                visibility_timeout=self._visibility_timeout,
            )
            return True
        except Exception:
            return False

    def process_message(self, message: SQSReceivedMessage,
                        *, now: datetime | None = None) -> SQSConsumeOutcome:
        try:
            job_id, outbox_id, event_type, version = self._parse_reference(message.body)
            event: InvestigationOutboxEvent = self._outbox.get_by_job(job_id)
            if (event.outbox_id != outbox_id or event.event_type != event_type
                    or event.event_version != version or event.status != "PUBLISHED"):
                raise ValueError("OUTBOX_REFERENCE_MISMATCH")
        except Exception as error:
            return self._retry(
                message, error_category=str(error) or "INVALID_SQS_REFERENCE",
                dlq_eligible=True,
            )

        # Duplicate deliveries are acknowledged only after rechecking the
        # durable result.  An active/stale claim with no result is not enough.
        if self._durably_completed(self._results, job_id):
            try:
                self._sqs.delete_message(receipt_handle=message.receipt_handle)
            except Exception:
                return self._retry(
                    message, error_category="SQS_DELETE_FAILED", dlq_eligible=True,
                )
            return SQSConsumeOutcome(message.receipt_handle, "ACKNOWLEDGED", job_id)

        if not self._extend_processing_visibility(message):
            return self._retry(
                message, error_category="SQS_VISIBILITY_EXTENSION_FAILED",
                dlq_eligible=False,
            )

        try:
            self._worker.run_once(job_id, worker_id=self._server_worker_id)
        except Exception:
            return self._retry(
                message, error_category="WORKER_EXECUTION_FAILED", dlq_eligible=True,
            )

        # run_once may return None for a stale/active claim.  Only the durable
        # result authorizes deletion; the return value is never authoritative.
        if not self._durably_completed(self._results, job_id):
            return self._retry(
                message, error_category="DURABLE_COMPLETION_UNCONFIRMED",
                dlq_eligible=True,
            )
        try:
            self._sqs.delete_message(receipt_handle=message.receipt_handle)
        except Exception:
            return self._retry(
                message, error_category="SQS_DELETE_FAILED", dlq_eligible=True,
            )
        return SQSConsumeOutcome(message.receipt_handle, "ACKNOWLEDGED", job_id)

    def poll_once(self, *, now: datetime | None = None) -> tuple[SQSConsumeOutcome, ...]:
        """Receive and process one bounded batch; never loops indefinitely."""
        messages = self._sqs.receive_messages(
            max_messages=self._max_messages,
            visibility_timeout=self._visibility_timeout,
        )
        return tuple(self.process_message(message, now=now) for message in messages)
