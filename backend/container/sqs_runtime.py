"""ECS publisher and Strands-worker process runners.

This module owns process lifecycle only.  Business state, claims, leases, and
results remain in the PostgreSQL repositories and the injectable publisher /
consumer classes remain AWS-free.
"""

from __future__ import annotations

from threading import Event
import os
import re
import signal
import time
from typing import Callable, Mapping
from uuid import uuid4

from backend.app.investigation_outbox_publisher import InvestigationOutboxPublisher
from backend.app.investigation_sqs_consumer import (
    InvestigationSQSConsumer, SQSReceivedMessage,
)
from backend.app.postgres_persistence import (
    PostgreSQLInvestigationOutboxRepository, PostgreSQLPersistence,
)
from backend.container.bootstrap import (
    _agent_engine, _postgres_dsn, _sqlite_database_path, create_container_app,
)


_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,3}-[0-9]{1,2}$")


def _required_queue_url() -> str:
    value = os.environ.get("NOVAMIND_INVESTIGATION_QUEUE_URL", "")
    if (not value or len(value) > 2048 or any(character.isspace() for character in value)
            or not value.startswith("https://")):
        raise RuntimeError("Container SQS queue configuration is invalid")
    return value


def _required_region() -> str:
    value = os.environ.get("AWS_REGION", "")
    if not _REGION.fullmatch(value):
        raise RuntimeError("Container AWS region configuration is invalid")
    return value


def _required_dsn() -> str:
    value = _postgres_dsn()
    if value is None:
        raise RuntimeError("Container PostgreSQL configuration is required")
    return value


def _validate_durable_server_configuration(role: str) -> None:
    """Fail closed before a non-HTTP ECS process opens a database or queue."""
    if role not in ("publisher", "worker"):
        raise RuntimeError("Container durable server role is invalid")
    if _sqlite_database_path() is not None or os.environ.get("NOVAMIND_AUTH_FILE"):
        raise RuntimeError("Container durable server configuration is invalid")
    if os.environ.get("NOVAMIND_AUTH_MODE") is not None:
        raise RuntimeError("Container durable server authentication configuration is invalid")
    if (os.environ.get("NOVAMIND_LLM_PROVIDER") != "bedrock"
            or os.environ.get("NOVAMIND_AGENT_ENGINE") != "strands"):
        raise RuntimeError("Container durable server requires PostgreSQL and Strands")
    _agent_engine()


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise RuntimeError("Container SQS bounds are invalid") from None
    if not minimum <= value <= maximum:
        raise RuntimeError("Container SQS bounds are invalid")
    return value


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise RuntimeError("Container polling bounds are invalid") from None
    if not minimum <= value <= maximum:
        raise RuntimeError("Container polling bounds are invalid")
    return value


class BotoSQSAdapter:
    """Small adapter translating boto3 responses to application contracts."""

    def __init__(self, *, queue_url: str, region: str):
        try:
            import boto3
            self._client = boto3.client("sqs", region_name=region)
        except Exception:
            raise RuntimeError("Container SQS client configuration is invalid") from None
        self._queue_url = queue_url

    def send_message(self, *, body: str, message_attributes: Mapping[str, str]) -> str:
        response = self._client.send_message(
            QueueUrl=self._queue_url,
            MessageBody=body,
            MessageAttributes={
                key: {"DataType": "String", "StringValue": value}
                for key, value in message_attributes.items()
            },
        )
        message_id = response.get("MessageId")
        if not isinstance(message_id, str) or not message_id:
            raise RuntimeError("SQS publish confirmation was invalid")
        return message_id

    def receive_messages(self, *, max_messages: int,
                         visibility_timeout: int):
        response = self._client.receive_message(
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=max_messages,
            VisibilityTimeout=visibility_timeout,
            WaitTimeSeconds=min(20, max(0, _bounded_int(
                "NOVAMIND_SQS_LONG_POLL_SECONDS", 20, 0, 20))),
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        messages = []
        for item in response.get("Messages", ()):
            count = item.get("Attributes", {}).get("ApproximateReceiveCount", "1")
            try:
                receive_count = max(1, int(count))
            except (TypeError, ValueError):
                receive_count = 1
            messages.append(SQSReceivedMessage(
                body=item.get("Body", ""),
                receipt_handle=item.get("ReceiptHandle", ""),
                receive_count=receive_count,
            ))
        return tuple(messages)

    def delete_message(self, *, receipt_handle: str) -> None:
        self._client.delete_message(
            QueueUrl=self._queue_url, ReceiptHandle=receipt_handle,
        )

    def change_message_visibility(self, *, receipt_handle: str,
                                  visibility_timeout: int) -> None:
        self._client.change_message_visibility(
            QueueUrl=self._queue_url, ReceiptHandle=receipt_handle,
            VisibilityTimeout=visibility_timeout,
        )


def run_bounded_loop(step: Callable[[], object], *, stop_event: Event,
                     interval_seconds: float, max_iterations: int | None = None) -> None:
    """Run bounded work until SIGTERM/stop or an optional test limit."""
    iterations = 0
    while not stop_event.is_set() and (max_iterations is None or iterations < max_iterations):
        step()
        iterations += 1
        if not stop_event.is_set() and interval_seconds:
            stop_event.wait(interval_seconds)


def _signal_stop(stop_event: Event):
    def handler(_signum, _frame):
        stop_event.set()
    for name in ("SIGTERM", "SIGINT"):
        value = getattr(signal, name, None)
        if value is not None:
            signal.signal(value, handler)


def run_publisher(*, stop_event: Event | None = None,
                  max_iterations: int | None = None) -> None:
    _validate_durable_server_configuration("publisher")
    queue_url = _required_queue_url()
    region = _required_region()
    dsn = _required_dsn()
    adapter = BotoSQSAdapter(queue_url=queue_url, region=region)
    repository = PostgreSQLInvestigationOutboxRepository(
        PostgreSQLPersistence(dsn, initialize=False),
    )
    publisher = InvestigationOutboxPublisher(
        repository, adapter, publisher_id=uuid4(),
        lease_seconds=_bounded_int("NOVAMIND_OUTBOX_LEASE_SECONDS", 30, 1, 300),
        max_attempts=_bounded_int("NOVAMIND_OUTBOX_MAX_ATTEMPTS", 5, 1, 100),
        retry_after_seconds=_bounded_int("NOVAMIND_OUTBOX_RETRY_SECONDS", 5, 0, 300),
    )
    event = stop_event or Event()
    _signal_stop(event)
    interval = _bounded_float("NOVAMIND_POLL_INTERVAL_SECONDS", 1.0, 0.0, 30.0)

    def step():
        try:
            publisher.publish_one()
        except Exception:
            # The outbox remains durable and retryable; process lifecycle must
            # remain available for the next bounded polling attempt.
            return None

    run_bounded_loop(step, stop_event=event, interval_seconds=interval,
                     max_iterations=max_iterations)


def run_worker(*, stop_event: Event | None = None,
               max_iterations: int | None = None) -> None:
    _validate_durable_server_configuration("worker")
    region = _required_region()
    queue_url = _required_queue_url()
    # create_container_app validates Bedrock, Cognito, PostgreSQL secret, and
    # Strands configuration before constructing the worker task.
    app = create_container_app()
    adapter = BotoSQSAdapter(queue_url=queue_url, region=region)
    consumer = InvestigationSQSConsumer(
        adapter,
        outbox=app.state.investigation_outbox,
        results=app.state.investigation_results,
        worker=app.state.investigation_executor,
        server_worker_id=uuid4(),
        max_messages=_bounded_int("NOVAMIND_SQS_MAX_MESSAGES", 1, 1, 10),
        visibility_timeout=_bounded_int("NOVAMIND_SQS_VISIBILITY_SECONDS", 300, 90, 43200),
        retry_visibility_timeout=_bounded_int("NOVAMIND_SQS_RETRY_VISIBILITY_SECONDS", 30, 1, 43200),
        max_receive_count=_bounded_int("NOVAMIND_SQS_MAX_RECEIVE_COUNT", 5, 1, 1000),
    )
    event = stop_event or Event()
    _signal_stop(event)
    interval = _bounded_float("NOVAMIND_POLL_INTERVAL_SECONDS", 1.0, 0.0, 30.0)

    def step():
        try:
            consumer.poll_once()
        except Exception:
            return None

    try:
        run_bounded_loop(step, stop_event=event, interval_seconds=interval,
                         max_iterations=max_iterations)
    finally:
        executor = getattr(app.state, "investigation_executor", None)
        if executor is not None:
            executor.close()
        app.state.frontend_runtime.close()
