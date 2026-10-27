"""Server-owned dispatch of an explicitly selected durable investigation job.

This is intentionally a one-shot hand-off, not a queue worker or recovery
loop.  A durable attempt marker is written before LangGraph is invoked, and a
later process start never calls this code for an existing job.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import RLock
from uuid import UUID

from .agent import AgentRequest
from .hitl import HITLWorkflow, WorkflowResult
from .langgraph_checkpoints import SQLiteCheckpointSaver
from .llm import StructuredLLM
from .persistence import SQLiteInvestigationJobRepository, SQLiteWorkflowMetadataRepository
from .observability import LocalObserver, observing


class AutomaticInvestigationDispatcher:
    """Dispatch one job through the shared once-only path under a staff identity."""

    def __init__(self, jobs: SQLiteInvestigationJobRepository, *, worker_id: UUID | None,
                 provider_factory, provider_label: str, tools, proposals,
                 checkpointer: SQLiteCheckpointSaver,
                 workflow_metadata: SQLiteWorkflowMetadataRepository,
                 lease_seconds: int = 60, observer: LocalObserver | None = None) -> None:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError("Dispatcher lease_seconds must be a positive integer")
        if provider_label not in ("local_scripted", "bedrock"):
            raise ValueError("Invalid dispatcher provider label")
        self._jobs = jobs
        self._worker_id = UUID(str(worker_id)) if worker_id is not None else None
        self._provider_factory = provider_factory
        self._provider_label = provider_label
        self._tools = tools
        self._proposals = proposals
        self._checkpointer = checkpointer
        self._workflow_metadata = workflow_metadata
        self._lease_seconds = lease_seconds
        self._observer = observer
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="novamind-dispatch")
        self._lock = RLock()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True)

    def submit(self, job_id: UUID) -> bool:
        """Queue a newly-created job outside the customer request context.

        Context variables holding the authenticated customer are intentionally
        not copied into this server worker.  The only identity used by the
        execution path is the configured staff identity below.
        """
        if self._worker_id is None:
            return False
        return self.submit_as(job_id, worker_id=self._worker_id)

    def submit_as(self, job_id: UUID, *, worker_id: UUID) -> bool:
        """Queue an explicitly authorized ADMIN start under that server identity."""
        worker_id = UUID(str(worker_id))
        with self._lock:
            if self._closed:
                return False
            try:
                self._executor.submit(self.dispatch, job_id, worker_id=worker_id)
            except RuntimeError:
                return False
        return True

    def dispatch(self, job_id: UUID, *, worker_id: UUID | None = None) -> WorkflowResult | None:
        """Attempt the one permitted dispatch for ``job_id``.

        Failures after a claim become recovery-required.  They are never
        retried here or on startup; a ``None`` result intentionally leaves the
        customer intake response independent of internal execution details.
        """
        execution_worker = UUID(str(worker_id)) if worker_id is not None else self._worker_id
        if execution_worker is None:
            raise ValueError("Dispatcher requires a server-authorized worker identity")
        observation = observing(self._observer) if self._observer is not None else nullcontext()
        with observation:
            return self._dispatch_claimed(job_id, execution_worker)

    def _dispatch_claimed(self, job_id: UUID, execution_worker: UUID) -> WorkflowResult | None:
        claim = self._jobs.claim(
            job_id, worker_id=execution_worker, lease_seconds=self._lease_seconds,
        )
        if claim is None:
            return None
        try:
            self._jobs.bind_claim(
                claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
                provider=self._provider_label,
            )
            # Read and validate the immutable descriptor before reserving the
            # sole attempt.  It is revalidated from SQLite again in the scope.
            self._jobs.execution_request(
                claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
            )
            attempt = self._jobs.mark_execution_attempt(
                claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
            )
            with self._jobs.execution_context(
                claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
                attempt_id=attempt.attempt_id,
            ) as execution:
                request = AgentRequest.model_validate_json(execution.agent_request_json)
                workflow = HITLWorkflow(
                    StructuredLLM(self._provider_factory()), self._tools, self._proposals,
                    checkpointer=self._checkpointer,
                    workflow_metadata=self._workflow_metadata,
                    provider_label=self._provider_label,
                )
                result = workflow.start(
                    request, workflow_id=execution.workflow_id,
                    run_id=execution.execution_id, request_id=execution.execution_id,
                )
            if result.status == "FAILED":
                self._jobs.require_recovery(
                    claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
                )
            return result
        except Exception:
            # The first durable claim is sufficient to identify this as an
            # ambiguous operation.  Never let a later automatic dispatch turn
            # it back into pending work.
            try:
                self._jobs.require_recovery(
                    claim.job.id, worker_id=execution_worker, claim_id=claim.claim_id,
                )
            except Exception:
                pass
            return None
