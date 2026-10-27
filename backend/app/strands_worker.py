"""One-shot PostgreSQL-fenced Strands investigation worker."""

from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from uuid import UUID

from .postgres_persistence import (
    PostgreSQLInvestigationJobRepository,
    PostgreSQLInvestigationResultRepository,
)
from .strands_agent import StrandsWorkflow


class PostgreSQLStrandsWorker:
    """Execute exactly one claimed attempt; no checkpoint or local run authority."""

    def __init__(self, jobs: PostgreSQLInvestigationJobRepository,
                 results: PostgreSQLInvestigationResultRepository, *, tools,
                 model_factory, worker_id: UUID | None = None,
                 lease_seconds: int = 60) -> None:
        self._jobs = jobs
        self._results = results
        self._tools = tools
        self._model_factory = model_factory
        self._worker_id = None if worker_id is None else UUID(str(worker_id))
        self._lease_seconds = lease_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="novamind-strands")
        self._lock = RLock()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True)

    def submit_as(self, job_id: UUID, *, worker_id: UUID) -> bool:
        """Queue one explicitly authorized ADMIN start outside the request."""
        worker_id = UUID(str(worker_id))
        with self._lock:
            if self._closed:
                return False
            try:
                self._executor.submit(self.run_once, job_id, worker_id=worker_id)
            except RuntimeError:
                return False
        return True

    def submit(self, job_id: UUID) -> bool:
        if self._worker_id is None:
            return False
        return self.submit_as(job_id, worker_id=self._worker_id)

    def run_once(self, job_id: UUID, *, worker_id: UUID | None = None):
        execution_worker = self._worker_id if worker_id is None else UUID(str(worker_id))
        if execution_worker is None:
            raise ValueError("Strands worker requires a server-authorized worker identity")
        claim = self._jobs.claim(
            job_id, worker_id=execution_worker, lease_seconds=self._lease_seconds,
        )
        if claim is None:
            return None
        try:
            self._jobs.bind_claim(
                claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
                provider="bedrock",
            )
            request = self._jobs.execution_request(
                claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
            )
            attempt = self._jobs.mark_execution_attempt(
                claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
            )
            with self._jobs.execution_context(
                claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
                attempt_id=attempt.attempt_id,
            ) as execution:
                workflow = StrandsWorkflow(
                    self._tools, model_factory=self._model_factory,
                )
                result = workflow.run_durable(
                    request, workflow_id=execution.workflow_id,
                )
            if result.status != "COMPLETED":
                self._jobs.require_recovery(
                    claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
                )
                return result
            self._results.record_completed(
                claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
                attempt_id=attempt.attempt_id, result=result,
            )
            return result
        except Exception:
            # A timeout, stale lease, provider failure, or uncertain write is
            # never converted into a retryable pending job.  The repository
            # fences the recovery transition; no late daemon result is stored.
            try:
                self._jobs.require_recovery(
                    claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
                )
            except Exception:
                pass
            return None
