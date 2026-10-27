"""Task 6G1 process-role, lifecycle, and AWS-adapter contracts."""

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from threading import Event
from unittest.mock import MagicMock, patch
from uuid import UUID

from backend.app.main import create_app
from backend.app.investigation_sqs_consumer import SQSReceivedMessage
from backend.container.bootstrap import _process_role
from backend.container.sqs_runtime import (
    BotoSQSAdapter, _validate_durable_server_configuration, run_bounded_loop,
)


class Task6G1OfflineTests(unittest.TestCase):
    def test_durable_container_configuration_is_fail_closed_and_worker_is_auth_free(self):
        import backend.container.bootstrap as bootstrap

        common = {
            "NOVAMIND_PROCESS_ROLE": "worker",
            "NOVAMIND_LLM_PROVIDER": "bedrock",
            "NOVAMIND_AGENT_ENGINE": "strands",
        }
        with patch.dict(os.environ, {**common, "NOVAMIND_AUTH_MODE": "local"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "authentication"):
                bootstrap.create_container_app()

        with patch.dict(os.environ, common, clear=True), \
             patch.object(bootstrap, "_authentication_provider", return_value=(None, ())), \
             patch.object(bootstrap, "_cognito_authentication_configuration", side_effect=AssertionError("worker must not read Cognito")), \
             patch.object(bootstrap, "_llm_provider", return_value=None), \
             patch.object(bootstrap, "_agent_engine", return_value="strands"), \
             patch.object(bootstrap, "_postgres_dsn", return_value="postgresql://worker/db"), \
             patch.object(bootstrap, "_sqlite_database_path", return_value=None), \
             patch.object(bootstrap, "_automatic_dispatcher_configuration", return_value=(False, None)), \
             patch.object(bootstrap, "create_app") as create_app:
            create_app.return_value = MagicMock()
            bootstrap.create_container_app()
            kwargs = create_app.call_args.kwargs
            self.assertIsNone(kwargs["cognito_configuration"])
            self.assertEqual(kwargs["process_role"], "worker")

        with patch.dict(os.environ, {"NOVAMIND_PROCESS_ROLE": "api", "NOVAMIND_AUTH_MODE": "local"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "API authentication"):
                bootstrap.create_container_app()

        with patch.dict(os.environ, {"NOVAMIND_PROCESS_ROLE": "api", "NOVAMIND_AUTH_MODE": "cognito"}, clear=True), \
             patch.object(bootstrap, "_authentication_provider", return_value=(None, ())), \
             patch.object(bootstrap, "_cognito_authentication_configuration", return_value=(None, ())), \
             patch.object(bootstrap, "_llm_provider", return_value=None), \
             patch.object(bootstrap, "_agent_engine", return_value="strands"), \
             patch.object(bootstrap, "_postgres_dsn", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "PostgreSQL and Strands"):
                bootstrap.create_container_app()

    def test_durable_default_is_api_and_explicit_roles_are_validated(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_process_role(), "local")
        with patch.dict(os.environ, {"NOVAMIND_POSTGRES_DSN": "postgresql://redacted"}, clear=True):
            self.assertEqual(_process_role(), "api")
        with patch.dict(os.environ, {"NOVAMIND_PROCESS_ROLE": "publisher"}, clear=True):
            self.assertEqual(_process_role(), "publisher")
        with patch.dict(os.environ, {"NOVAMIND_PROCESS_ROLE": "unexpected"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                _process_role()

    def test_publisher_entrypoint_requires_strands_postgres_configuration(self):
        with patch.dict(os.environ, {
            "NOVAMIND_PROCESS_ROLE": "publisher",
            "NOVAMIND_LLM_PROVIDER": "bedrock",
            "NOVAMIND_AGENT_ENGINE": "langgraph",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "PostgreSQL and Strands"):
                _validate_durable_server_configuration("publisher")
        with patch.dict(os.environ, {
            "NOVAMIND_PROCESS_ROLE": "publisher",
            "NOVAMIND_LLM_PROVIDER": "strands",
            "NOVAMIND_AGENT_ENGINE": "strands",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "PostgreSQL and Strands"):
                _validate_durable_server_configuration("publisher")
        with patch.dict(os.environ, {
            "NOVAMIND_PROCESS_ROLE": "publisher",
            "NOVAMIND_LLM_PROVIDER": "bedrock",
            "NOVAMIND_AGENT_ENGINE": "strands",
        }, clear=True), patch("backend.container.sqs_runtime._agent_engine", return_value="strands"):
            _validate_durable_server_configuration("publisher")

    def test_api_role_has_no_inline_executor_or_strands_model_factory_requirement(self):
        with TemporaryDirectory() as directory:
            app = create_app(
                sqlite_path=str(Path(directory) / "state.sqlite3"),
                agent_engine="strands", process_role="api",
            )
            self.assertEqual(app.state.process_role, "api")
            self.assertIsNone(app.state.investigation_executor)
            self.assertFalse(app.state.frontend_runtime.inline_execution_enabled)
            app.state.frontend_runtime.close()

    def test_bounded_loop_honors_stop_and_test_iteration_limit(self):
        calls = []
        stop = Event()
        run_bounded_loop(lambda: calls.append(True), stop_event=stop,
                         interval_seconds=0, max_iterations=3)
        self.assertEqual(len(calls), 3)
        stop.set()
        run_bounded_loop(lambda: calls.append(True), stop_event=stop,
                         interval_seconds=0, max_iterations=3)
        self.assertEqual(len(calls), 3)

    def test_boto_adapter_maps_reference_only_sqs_contract(self):
        client = MagicMock()
        client.send_message.return_value = {"MessageId": "message-1"}
        client.receive_message.return_value = {
            "Messages": [{
                "Body": "{}", "ReceiptHandle": "receipt-1",
                "Attributes": {"ApproximateReceiveCount": "2"},
            }]
        }
        fake_boto3 = MagicMock()
        fake_boto3.client.return_value = client
        with patch.dict(sys.modules, {"boto3": fake_boto3}), \
             patch.dict(os.environ, {"NOVAMIND_SQS_LONG_POLL_SECONDS": "0"}, clear=False):
            adapter = BotoSQSAdapter(
                queue_url="https://sqs.us-east-1.amazonaws.com/123/queue",
                region="us-east-1",
            )
            self.assertEqual(adapter.send_message(
                body="{}", message_attributes={"schema_version": "1"}), "message-1")
            received = adapter.receive_messages(max_messages=1, visibility_timeout=30)
            self.assertEqual(received, (SQSReceivedMessage("{}", "receipt-1", 2),))
            adapter.delete_message(receipt_handle="receipt-1")
            adapter.change_message_visibility(receipt_handle="receipt-1", visibility_timeout=60)
        client.delete_message.assert_called_once()
        client.change_message_visibility.assert_called_once()

    def test_ecs_entrypoint_dispatches_roles_without_starting_http_for_worker(self):
        import backend.container.ecs_entrypoint as entrypoint

        with patch.dict(os.environ, {"NOVAMIND_PROCESS_ROLE": "publisher"}, clear=True), \
             patch("backend.container.sqs_runtime.run_publisher") as publisher:
            entrypoint.main()
            publisher.assert_called_once_with()
        with patch.dict(os.environ, {"NOVAMIND_PROCESS_ROLE": "worker"}, clear=True), \
             patch("backend.container.sqs_runtime.run_worker") as worker:
            entrypoint.main()
            worker.assert_called_once_with()


@unittest.skipUnless(
    os.environ.get("NOVAMIND_POSTGRES_TEST_DSN"),
    "NOVAMIND_POSTGRES_TEST_DSN is not configured",
)
class Task6G1PostgresGatedTests(unittest.TestCase):
    def test_api_role_uses_verified_disposable_database_without_executor(self):
        dsn = os.environ["NOVAMIND_POSTGRES_TEST_DSN"]
        try:
            import psycopg
        except ImportError:
            self.skipTest("psycopg is unavailable")
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                database = cursor.fetchone()[0]
        if database != "novamind_phase_c_test_20261026":
            self.skipTest("DSN is not the dedicated disposable database")
        app = create_app(
            postgres_dsn=dsn, agent_engine="strands", process_role="api",
        )
        try:
            self.assertIsNone(app.state.investigation_executor)
            self.assertFalse(app.state.frontend_runtime.inline_execution_enabled)
            self.assertIsNotNone(app.state.investigation_outbox)
        finally:
            app.state.frontend_runtime.close()


if __name__ == "__main__":
    unittest.main()
