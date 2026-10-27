"""Offline fencing/idempotency coverage for the PostgreSQL Strands worker."""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.app.agent import AgentRequest, AgentResult
from backend.app.hitl import WorkflowResult
from backend.app.service import ConflictError
from backend.app.strands_worker import PostgreSQLStrandsWorker


def _result(workflow_id, *, status="COMPLETED", error=None, issue_type=None):
    agent = AgentResult(
        status="INFORMATIONAL", customer_response="Safe investigation result.",
        steps_used=1, history=(), error=error,
    )
    if issue_type is not None:
        agent.investigation_plan = None
    return WorkflowResult(
        workflow_id=workflow_id, status=status, agent_result=agent,
        message="Safe investigation result.", error=error,
    )


class _FakeJobs:
    def __init__(self, *, stale=False):
        self.job_id, self.claim_id, self.attempt_id = uuid4(), uuid4(), uuid4()
        self.workflow_id, self.case_id = uuid4(), uuid4()
        self.stale = stale
        self.claim_count = 0
        self.recovery_count = 0
        self.execution = SimpleNamespace(
            workflow_id=self.workflow_id, execution_id=self.workflow_id,
            agent_request_json="", agent_request_fingerprint="a" * 64,
        )

    def claim(self, job_id, *, worker_id, lease_seconds):
        self.claim_count += 1
        if self.claim_count > 1:
            return None
        return SimpleNamespace(
            job=SimpleNamespace(id=self.job_id), claim_id=self.claim_id,
        )

    def bind_claim(self, *args, **kwargs):
        if self.stale:
            raise ConflictError("stale claim")

    def execution_request(self, *args, **kwargs):
        return AgentRequest(case_id=self.case_id, message="Investigate")

    def mark_execution_attempt(self, *args, **kwargs):
        if self.stale:
            raise ConflictError("stale attempt")
        return SimpleNamespace(attempt_id=self.attempt_id)

    @contextmanager
    def execution_context(self, *args, **kwargs):
        if self.stale:
            raise ConflictError("stale execution fence")
        yield self.execution

    def require_recovery(self, *args, **kwargs):
        self.recovery_count += 1


class _FakeResults:
    def __init__(self):
        self.records = []

    def record_completed(self, *args, **kwargs):
        self.records.append(kwargs["result"])


class StrandsWorkerTests(unittest.TestCase):
    def setUp(self):
        self.jobs = _FakeJobs()
        self.results = _FakeResults()
        self.worker = PostgreSQLStrandsWorker(
            self.jobs, self.results, tools=object(), model_factory=lambda: object(),
            worker_id=uuid4(),
        )

    def test_duplicate_execution_is_claim_idempotent_and_result_survives_restart(self):
        successful = _result(self.jobs.workflow_id)
        with patch("backend.app.strands_worker.StrandsWorkflow") as workflow_type:
            workflow_type.return_value.run_durable.return_value = successful
            first = self.worker.run_once(self.jobs.job_id)
            second = self.worker.run_once(self.jobs.job_id)
        self.assertIs(first, successful)
        self.assertIsNone(second)
        self.assertEqual(len(self.results.records), 1)
        restarted_result = self.results.records[0]
        self.assertFalse(restarted_result.agent_result.actions_executed)

    def test_stale_claim_never_invokes_or_persists_result(self):
        jobs = _FakeJobs(stale=True)
        results = _FakeResults()
        worker = PostgreSQLStrandsWorker(
            jobs, results, tools=object(), model_factory=lambda: object(), worker_id=uuid4(),
        )
        with patch("backend.app.strands_worker.StrandsWorkflow") as workflow_type:
            self.assertIsNone(worker.run_once(jobs.job_id))
        workflow_type.assert_not_called()
        self.assertEqual(results.records, [])
        self.assertGreaterEqual(jobs.recovery_count, 1)

    def test_timeout_or_invalid_execution_enters_recovery_without_late_persist(self):
        failed = _result(self.jobs.workflow_id, status="FAILED", error="TIMEOUT")
        with patch("backend.app.strands_worker.StrandsWorkflow") as workflow_type:
            workflow_type.return_value.run_durable.return_value = failed
            result = self.worker.run_once(self.jobs.job_id)
        self.assertEqual(result.error, "TIMEOUT")
        self.assertEqual(self.results.records, [])
        self.assertEqual(self.jobs.recovery_count, 1)

    def test_all_seven_issue_types_use_the_same_fenced_worker_boundary(self):
        issue_types = (
            "damaged", "wrong_item", "missing_item", "delayed_delivery",
            "malfunction", "return", "refund_not_received",
        )
        for issue_type in issue_types:
            with self.subTest(issue_type=issue_type):
                jobs = _FakeJobs()
                results = _FakeResults()
                worker = PostgreSQLStrandsWorker(
                    jobs, results, tools=object(), model_factory=lambda: object(), worker_id=uuid4(),
                )
                with patch("backend.app.strands_worker.StrandsWorkflow") as workflow_type:
                    workflow_type.return_value.run_durable.return_value = _result(jobs.workflow_id)
                    worker.run_once(jobs.job_id)
                self.assertEqual(len(results.records), 1)


if __name__ == "__main__":
    unittest.main()
