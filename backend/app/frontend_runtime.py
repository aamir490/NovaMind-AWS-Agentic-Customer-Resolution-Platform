"""Single-process, bounded HTTP adapter over the existing HITL and memory services."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from uuid import UUID, uuid4

from .agent import AgentRequest
from .graph_agent import GraphResolutionAgent, GraphState
from .frontend_contracts import (
    AuditPage, AuditSummary, EventPage, PendingReviewPage, PendingReviewRun, RunEvent, RunPage, RunSnapshot,
)
from .hitl import HITLState, HITLWorkflow, HumanDecision, ResumeRejected
from .langgraph_checkpoints import SQLiteCheckpointSaver
from .knowledge import RetrievalResult
from .llm import ProviderFailure, StructuredLLM
from .observability import ERRORS, TOOLS
from .persistence import SQLiteInvestigationJobRepository, SQLiteWorkflowMetadataRepository
from .proposals import ProposalStatus, ResolutionProposal
from .security import Authorization, Role, SecurityError, current_identity, require_roles
from .service import NotFoundError
from .workflow_metadata import WorkflowMetadataStatus


def now():
    return datetime.now(timezone.utc)


def safe_error(code):
    return code if code in ERRORS else "WORKFLOW_ERROR" if code else None


class FrontendError(Exception):
    def __init__(self, status, code):
        self.status = status
        self.code = code
        super().__init__(code)


class _UnavailableProvider:
    """Allows a restored paused review to finish without creating an LLM client."""

    def generate(self, request, *, response_schema):
        raise ProviderFailure("UNAVAILABLE")


@dataclass
class _Run:
    snapshot: RunSnapshot
    owner: UUID
    fingerprint: str
    request_id: UUID | None = None
    workflow: HITLWorkflow | None = None
    events: deque = field(default_factory=deque)


class FrontendRuntime:
    # No run eviction: deduplication keys and checkpoints live for this process.
    # Once full, refuse new starts rather than silently replaying an evicted write.
    def __init__(self, cases, tools, proposals, *, local_provider_factory=None,
                 provider_label="local_scripted", memory=None,
                 agent_engine="langgraph", strands_model_factory=None,
                 checkpointer: SQLiteCheckpointSaver | None = None,
                 workflow_metadata: SQLiteWorkflowMetadataRepository | None = None,
                 investigation_workflow_metadata=None,
                 investigation_jobs: SQLiteInvestigationJobRepository | None = None,
                 inline_execution_enabled: bool = True,
                 max_runs=128, max_active=2, event_capacity=128):
        if not (1 <= max_runs <= 1024 and 1 <= max_active <= 8 and 1 <= event_capacity <= 1024):
            raise ValueError("Invalid local runtime bounds")
        if provider_label not in ("local_scripted", "bedrock"):
            raise ValueError("Invalid provider label")
        if agent_engine not in ("langgraph", "strands"):
            raise ValueError("Invalid agent engine")
        if not isinstance(inline_execution_enabled, bool):
            raise ValueError("inline_execution_enabled must be a boolean")
        if agent_engine == "strands" and inline_execution_enabled and strands_model_factory is None:
            raise ValueError("Strands agent engine requires a model factory")
        if (checkpointer is None) != (workflow_metadata is None):
            raise ValueError("Durable checkpoints require durable workflow metadata")
        if checkpointer is not None and agent_engine != "langgraph":
            raise ValueError("Durable checkpoints are only available for LangGraph")
        self.instance_id = uuid4()
        self.provider_factory = local_provider_factory
        self.provider_label = provider_label
        self.agent_engine = agent_engine
        self.strands_model_factory = strands_model_factory
        self.memory = memory
        self._checkpointer = checkpointer
        self._workflow_metadata = workflow_metadata
        self._investigation_workflow_metadata = investigation_workflow_metadata
        self._investigation_jobs = investigation_jobs
        self.inline_execution_enabled = inline_execution_enabled
        self._cases, self._tools, self._proposals = cases, tools, proposals
        self._max_runs, self._max_active, self._event_capacity = max_runs, max_active, event_capacity
        self._runs, self._requests = {}, {}
        self._active = 0
        self._closed = False
        self._lock = RLock()
        self._executor = (
            ThreadPoolExecutor(max_workers=max_active, thread_name_prefix="novamind-run")
            if inline_execution_enabled else None
        )
        self._restore_durable_runs()

    def close(self):
        with self._lock:
            self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=True)

    def check_instance(self, instance_id):
        if instance_id != self.instance_id:
            raise FrontendError(409, "INSTANCE_CHANGED_DO_NOT_REPLAY")

    @property
    def _durable(self):
        return self._checkpointer is not None

    @property
    def _read_metadata(self):
        """Return the repository authoritative for read-only run projections."""
        return self._workflow_metadata or self._investigation_workflow_metadata

    def _workflow(self, run, *, restoring=False):
        """Build a LangGraph workflow without invoking a provider during recovery."""
        provider = (self.provider_factory() if self.provider_factory is not None and not restoring
                    else _UnavailableProvider())
        return HITLWorkflow(StructuredLLM(provider), self._tools, self._proposals,
            memory=self.memory, on_transition=lambda *args: self._transition(run, *args),
            checkpointer=self._checkpointer, workflow_metadata=self._workflow_metadata,
            provider_label=self.provider_label)

    def _require_recovery(self, metadata):
        try:
            self._workflow_metadata.require_recovery(
                metadata.workflow_id, owner_user_id=metadata.owner_user_id,
                case_id=metadata.case_id, expected_status=metadata.status,
            )
        except Exception:
            # A corrupted or already-finalized run is never retried from startup.
            pass

    def _hydrate_durable_run(self, metadata):
        """Load one stored workflow state; this is read-only and never resumes it."""
        if metadata.provider != self.provider_label or metadata.status == WorkflowMetadataStatus.RECOVERY_REQUIRED:
            return None
        try:
            holder = _Run(
                RunSnapshot(instance_id=self.instance_id, run_id=metadata.run_id,
                    workflow_id=metadata.workflow_id, case_id=metadata.case_id,
                    provider=metadata.provider, status=metadata.status.value,
                    created_at=metadata.created_at, updated_at=metadata.updated_at,
                    failure_category=metadata.failure_category, failure_stage=metadata.failure_stage,
                    invalid_response_stage=metadata.invalid_response_stage,
                    invalid_response_category=metadata.invalid_response_category),
                metadata.owner_user_id, metadata.request_fingerprint, metadata.request_id,
                events=deque(maxlen=self._event_capacity),
            )
            holder.workflow = self._workflow(holder, restoring=True)
            state = holder.workflow._graph.get_state(
                holder.workflow._runtime_config(metadata.workflow_id),
            )
            restored = HITLState.model_validate(state.values)
            review = restored.review
            if restored.workflow_id != metadata.workflow_id:
                raise ValueError("workflow mismatch")
            if metadata.status == WorkflowMetadataStatus.REVIEW_REQUIRED:
                if (review is None or state.next != ("human_review",)
                        or not any(task.interrupts for task in state.tasks)):
                    raise ValueError("missing paused review")
                proposal = self._proposals.get(review.proposal_id)
                if (proposal.status != ProposalStatus.PENDING_REVIEW
                        or proposal.case_id != metadata.case_id
                        or not self._proposals.matches_workflow_review(
                            proposal.id, workflow_id=review.workflow_id,
                            case_id=review.case_id, review_id=review.review_id)):
                    raise ValueError("review binding mismatch")
            holder.snapshot = holder.snapshot.model_copy(update={
                "conversation_id": restored.agent.request.conversation_id,
                "review": review,
                "reviewed_status": restored.reviewed_status,
                "resolution_draft": GraphResolutionAgent.resolution_draft_for_state(restored.agent),
                "message": (restored.agent.final_result.customer_response
                            if restored.agent.final_result is not None else None),
                "error": safe_error(restored.error),
            })
            self._runs[metadata.run_id] = holder
            self._requests[(metadata.owner_user_id, metadata.request_id)] = metadata.run_id
            return holder
        except Exception:
            if metadata.status in (
                WorkflowMetadataStatus.RUNNING, WorkflowMetadataStatus.RESUMING,
                WorkflowMetadataStatus.REVIEW_REQUIRED,
            ):
                self._require_recovery(metadata)
            return None

    def _restore_durable_runs(self):
        if not self._durable:
            return
        # Only REVIEW_REQUIRED runs are exposed after restart. In-flight work is
        # marked for explicit recovery instead of replaying an agent/tool call.
        for metadata in self._workflow_metadata.active_runs():
            if metadata.status == WorkflowMetadataStatus.REVIEW_REQUIRED:
                self._hydrate_durable_run(metadata)
            else:
                self._require_recovery(metadata)

    def _authorized(self, run_id):
        identity = current_identity()
        run = self._runs.get(run_id)
        metadata_repository = self._read_metadata
        if run is None and metadata_repository is not None:
            try:
                metadata = metadata_repository.get_by_run_id(run_id)
            except NotFoundError:
                metadata = None
            if metadata is not None:
                if self.agent_engine == "strands":
                    run = _Run(
                        snapshot=self._snapshot_from_metadata(metadata),
                        owner=metadata.owner_user_id,
                        fingerprint=metadata.request_fingerprint,
                        request_id=metadata.request_id,
                    )
                else:
                    run = self._hydrate_durable_run(metadata)
        if run is None:
            if identity.role == Role.CUSTOMER:
                raise SecurityError()
            raise FrontendError(404, "RUN_NOT_FOUND_OR_RESTARTED")
        Authorization(self._cases).case(run.snapshot.case_id)
        if identity.role == Role.CUSTOMER and identity.user_id != run.owner:
            raise SecurityError()
        return run

    def _publish(self, run, kind, state, **fields):
        timestamp = now()
        event = RunEvent(run_id=run.snapshot.run_id, sequence=run.snapshot.last_sequence + 1,
                         timestamp=timestamp, kind=kind, state=state, **fields)
        run.events.append(event)
        run.snapshot = run.snapshot.model_copy(update={"last_sequence": event.sequence, "updated_at": timestamp})

    def _transition(self, run, node, phase, state):
        with self._lock:
            run.snapshot = run.snapshot.model_copy(update={"workflow_id": state.workflow_id,
                "conversation_id": state.agent.request.conversation_id, "review": state.review,
                "resolution_draft": GraphResolutionAgent.resolution_draft_for_state(state.agent)})
            self._publish(run, "NODE", phase, node=node)
            if node not in ("load_case", "execute_tool"):
                return
            entry = state.agent.staged_event
            if node == "load_case" and state.agent.history:
                entry = state.agent.history[0]
            call = state.agent.decision if node == "execute_tool" else None
            name = getattr(call, "name", None) if node == "execute_tool" else "get_case"
            if name not in TOOLS:
                return
            # Publish only actual dispatcher results. A node can reject a call
            # before invoking a tool (case/proposal budgets); that is not tool activity.
            if phase == "COMPLETED" and entry is not None and entry.result is not None:
                result = entry.result
                data = result.data if result is not None and result.ok else None
                code = entry.error or (result.error.code if result is not None and not result.ok else None)
                self._publish(run, "TOOL", phase, tool=name, ok=bool(result and result.ok),
                    error=safe_error(code), proposal_id=data.id if isinstance(data, ResolutionProposal) else None,
                    evidence=data.hits if isinstance(data, RetrievalResult) else (),
                    retrieval_method=data.method if isinstance(data, RetrievalResult) else None)

    def _checkpoint_projection(self, workflow_id):
        """Read a validated durable result without constructing a provider or resuming work."""
        if not self._durable:
            return None, None, None, False
        try:
            checkpoint = self._checkpointer.get_tuple({"configurable": {
                "thread_id": str(workflow_id),
            }})
            values = checkpoint.checkpoint.get("channel_values", {}) if checkpoint else {}
            agent = GraphState.model_validate(values.get("agent"))
            draft = GraphResolutionAgent.admin_resolution_draft_for_state(agent)
            result = agent.final_result
            message = result.customer_response if result is not None else None
            # A durable COMPLETED projection is only valid when the checkpoint
            # contains the grounded draft and the agent's final customer-safe
            # result.  A draft without a final result is an incomplete write,
            # not an AI completion that the dashboard may present as complete.
            valid = (draft is not None and result is not None
                     and result.error is None and bool(result.customer_response))
            return draft, message, agent.error, valid
        except Exception:
            return None, None, "WORKFLOW_ERROR", False

    def _reserve(self):
        if self._closed or self._active >= self._max_active:
            raise FrontendError(503, "RUN_CAPACITY_UNAVAILABLE")
        self._active += 1

    def start(self, body, *, trace_id=None):
        identity = current_identity()
        Authorization(self._cases).case(body.case_id)
        if not self.inline_execution_enabled:
            raise FrontendError(503, "INLINE_INVESTIGATION_DISABLED")
        self.check_instance(body.instance_id)
        if self._investigation_jobs is not None:
            try:
                self._investigation_jobs.get_by_case(body.case_id)
            except NotFoundError:
                pass
            else:
                # Customer-intake cases must use the durable job claim path.
                # A new request_id here must never create a second workflow or
                # an additional billable provider call for the same case.
                raise FrontendError(409, "CASE_INVESTIGATION_ALREADY_EXISTS")
        request = AgentRequest.model_validate(body.model_dump(exclude={"instance_id", "request_id"}))
        fingerprint = HITLWorkflow._fingerprint(request)
        key = (identity.user_id, body.request_id)
        with self._lock:
            if key in self._requests:
                run = self._authorized(self._requests[key])
                if fingerprint != run.fingerprint:
                    raise FrontendError(409, "REQUEST_ID_CONFLICT")
                return run.snapshot
            if self._durable:
                try:
                    existing = self._workflow_metadata.get_by_request(identity.user_id, body.request_id)
                except NotFoundError:
                    existing = None
                if existing is not None:
                    if (existing.case_id != request.case_id
                            or existing.request_fingerprint != fingerprint
                            or existing.provider != self.provider_label):
                        raise FrontendError(409, "REQUEST_ID_CONFLICT")
                    run = self._hydrate_durable_run(existing)
                    if run is None:
                        raise FrontendError(409, "REQUEST_RECOVERY_REQUIRED")
                    return run.snapshot
            if self.provider_factory is None and self.agent_engine != "strands":
                raise FrontendError(503, "PROVIDER_UNAVAILABLE")
            if len(self._runs) >= self._max_runs:
                raise FrontendError(503, "RUN_RETENTION_FULL")
            if body.conversation_id is not None:
                if self.memory is None:
                    raise FrontendError(503, "CONVERSATION_UNAVAILABLE")
                self.memory.load(body.conversation_id, body.case_id)
            self._reserve()
            timestamp = now()
            run_id = uuid4()
            run = _Run(RunSnapshot(instance_id=self.instance_id, run_id=run_id, case_id=body.case_id,
                conversation_id=body.conversation_id, provider=self.provider_label,
                status="RUNNING", created_at=timestamp, updated_at=timestamp,
                trace_id=trace_id),
                identity.user_id, fingerprint, body.request_id, events=deque(maxlen=self._event_capacity))
            self._runs[run.snapshot.run_id] = run
            self._requests[key] = run.snapshot.run_id
            self._publish(run, "STATE", "RUNNING")
            self._submit(run, request, resume=False)
            return run.snapshot

    def _submit(self, run, payload, *, resume):
        if self._executor is None:
            self._active -= 1
            run.snapshot = run.snapshot.model_copy(update={"status": "FAILED", "error": "WORKFLOW_ERROR"})
            self._publish(run, "STATE", "FAILED", error="WORKFLOW_ERROR")
            return
        try:
            self._executor.submit(copy_context().run, self._execute, run, payload, resume)
        except Exception:
            self._active -= 1
            run.snapshot = run.snapshot.model_copy(update={"status": "FAILED", "error": "WORKFLOW_ERROR"})
            self._publish(run, "STATE", "FAILED", error="WORKFLOW_ERROR")

    def _execute(self, run, payload, resume):
        try:
            if not resume:
                if self.agent_engine == "strands":
                    from .strands_agent import StrandsWorkflow
                    run.workflow = StrandsWorkflow(self._tools,
                        model_factory=self.strands_model_factory,
                        memory=self.memory,
                        on_transition=lambda *args: self._transition(run, *args))
                else:
                    run.workflow = self._workflow(run) if self._durable else HITLWorkflow(
                        StructuredLLM(self.provider_factory()), self._tools, self._proposals,
                        memory=self.memory, on_transition=lambda *args: self._transition(run, *args))
            result = run.workflow.resume(payload) if resume else run.workflow.start(
                payload, workflow_id=run.snapshot.run_id, run_id=run.snapshot.run_id,
                request_id=run.request_id,
            ) if self._durable else run.workflow.start(payload)
            durable_failure = {}
            if self._durable and not isinstance(result, ResumeRejected):
                try:
                    metadata = self._workflow_metadata.get(result.workflow_id)
                    durable_failure = {
                        "failure_category": metadata.failure_category,
                        "failure_stage": metadata.failure_stage,
                        "invalid_response_stage": metadata.invalid_response_stage,
                        "invalid_response_category": metadata.invalid_response_category,
                    }
                except Exception:
                    # Failure diagnostics are passive. Their readback must not
                    # replace or replay an already completed workflow result.
                    pass
            with self._lock:
                if isinstance(result, ResumeRejected):
                    run.snapshot = run.snapshot.model_copy(update={"status": "REVIEW_REQUIRED", "error": result.error})
                else:
                    run.snapshot = run.snapshot.model_copy(update={
                        "workflow_id": result.workflow_id, "status": result.status,
                        "conversation_id": result.conversation_id or run.snapshot.conversation_id,
                        "review": result.review or run.snapshot.review, "reviewed_status": result.reviewed_status,
                        "message": result.message, "resolution_draft": result.resolution_draft,
                        "error": safe_error(result.error), **durable_failure})
                self._publish(run, "STATE", run.snapshot.status, error=run.snapshot.error)
        except Exception:
            with self._lock:
                run.snapshot = run.snapshot.model_copy(update={"status": "FAILED", "error": "WORKFLOW_ERROR",
                    "message": "Workflow stopped. Inspect current records; do not replay. No action was executed."})
                self._publish(run, "STATE", "FAILED", error="WORKFLOW_ERROR")
        finally:
            with self._lock:
                self._active -= 1

    def snapshot(self, run_id):
        with self._lock:
            return self._authorized(run_id).snapshot

    def events(self, run_id, after, limit):
        with self._lock:
            run = self._authorized(run_id)
            if after > run.snapshot.last_sequence:
                raise FrontendError(409, "EVENT_CURSOR_AHEAD")
            if not run.events:
                raise FrontendError(409, "NO_EVENTS_YET")
            first = run.events[0].sequence
            events = tuple(event for event in run.events if event.sequence > after)[:limit]
            cursor = events[-1].sequence if events else after
            return EventPage(snapshot=run.snapshot, events=events, next_after=cursor,
                has_more=cursor < run.snapshot.last_sequence, first_available_sequence=first,
                dropped_events=first - 1, gap=after < first - 1)

    def _snapshot_from_metadata(self, metadata):
        """Project durable metadata for selection; checkpoint hydration stays lazy."""
        if self.agent_engine == "strands":
            return RunSnapshot(
                instance_id=self.instance_id, run_id=metadata.run_id,
                case_id=metadata.case_id, workflow_id=metadata.workflow_id,
                provider=metadata.provider, status=metadata.status.value,
                created_at=metadata.created_at, updated_at=metadata.updated_at,
                failure_category=metadata.failure_category,
                failure_stage=metadata.failure_stage,
                invalid_response_stage=metadata.invalid_response_stage,
                invalid_response_category=metadata.invalid_response_category,
            )
        draft, message, checkpoint_error, valid = self._checkpoint_projection(metadata.workflow_id)
        status = metadata.status.value
        error = safe_error(metadata.failure_category) if metadata.failure_category else None
        if status == WorkflowMetadataStatus.COMPLETED.value and not valid:
            status, error = "FAILED", "WORKFLOW_ERROR"
        return RunSnapshot(
            instance_id=self.instance_id, run_id=metadata.run_id,
            workflow_id=metadata.workflow_id, case_id=metadata.case_id,
            provider=metadata.provider, status=status,
            created_at=metadata.created_at, updated_at=metadata.updated_at,
            message=message, resolution_draft=draft,
            error=error or safe_error(checkpoint_error),
            failure_category=metadata.failure_category, failure_stage=metadata.failure_stage,
            invalid_response_stage=metadata.invalid_response_stage,
            invalid_response_category=metadata.invalid_response_category,
        )

    def list_for_case(self, case_id, offset, limit):
        Authorization(self._cases).case(case_id)
        identity = current_identity()
        with self._lock:
            metadata_repository = self._read_metadata
            if metadata_repository is not None:
                owner_user_id = identity.user_id if identity.role == Role.CUSTOMER else None
                # Fetch one extra record to keep pagination bounded and avoid
                # instantiating historical workflows or their model providers.
                records = metadata_repository.visible_runs_for_case(
                    case_id, owner_user_id, offset, limit + 1,
                )
                items = tuple(self._snapshot_from_metadata(record) for record in records[:limit])
                return RunPage(
                    items=items,
                    next_offset=offset + limit if len(records) > limit else None,
                    scope="case_runs_with_durable_metadata",
                )
            records = [run.snapshot for run in self._runs.values() if run.snapshot.case_id == case_id
                       and (identity.role != Role.CUSTOMER or run.owner == identity.user_id)]
            return RunPage(items=tuple(records[offset:offset + limit]),
                next_offset=offset + limit if offset + limit < len(records) else None)

    def resolution_draft_for_case(self, case_id):
        """Read the newest validated graph draft without resuming a workflow."""
        require_roles(Role.ADMIN)
        Authorization(self._cases).case(case_id)
        with self._lock:
            live = [run.snapshot for run in self._runs.values()
                    if run.snapshot.case_id == case_id and run.snapshot.resolution_draft is not None]
            newest = (max(live, key=lambda snapshot: snapshot.updated_at) if live else None)
            if not self._durable:
                return newest.resolution_draft if newest else None
            # Checkpoints are append-only and this is a bounded, read-only
            # projection.  It deliberately never constructs a provider or
            # resumes a run while locating the draft for an admin decision.
            records = self._workflow_metadata.visible_runs_for_case(case_id, None, 0, 1024)
            for record in reversed(records):
                try:
                    checkpoint = self._checkpointer.get_tuple({"configurable": {
                        "thread_id": str(record.workflow_id),
                    }})
                    values = checkpoint.checkpoint.get("channel_values", {}) if checkpoint else {}
                    agent = GraphState.model_validate(values.get("agent"))
                    draft = GraphResolutionAgent.admin_resolution_draft_for_state(agent)
                    if (draft is not None and (newest is None
                            or record.updated_at >= newest.updated_at
                            # The append-only durable checkpoint is canonical
                            # for a live snapshot of this same workflow.
                            or newest.workflow_id == record.workflow_id)):
                        newest = RunSnapshot(
                            instance_id=self.instance_id, run_id=record.run_id,
                            case_id=case_id, workflow_id=record.workflow_id,
                            provider=record.provider, status=record.status.value,
                            created_at=record.created_at, updated_at=record.updated_at,
                            resolution_draft=draft,
                        )
                except Exception:
                    # A malformed historical checkpoint must not result in a
                    # synthesized decision or automatic recovery attempt.
                    continue
            return newest.resolution_draft if newest else None

    def pending_reviews(self):
        require_roles(Role.REVIEWER, Role.ADMIN)
        authorization = Authorization(self._cases, self._proposals)
        with self._lock:
            if self._durable:
                # The automatic dispatcher owns no FrontendRuntime object.  A
                # newly paused durable graph is therefore hydrated here using
                # its checkpoint only; this does not create a model client or
                # restart an in-flight workflow.
                for metadata in self._workflow_metadata.active_runs():
                    if (metadata.status == WorkflowMetadataStatus.REVIEW_REQUIRED
                            and metadata.run_id not in self._runs):
                        self._hydrate_durable_run(metadata)
            items = []
            for run in self._runs.values():
                snapshot, review = run.snapshot, run.snapshot.review
                if snapshot.status != "REVIEW_REQUIRED" or review is None:
                    continue
                support_case = authorization.case(snapshot.case_id)
                proposal = authorization.proposal(review.proposal_id)
                if (proposal.status != ProposalStatus.PENDING_REVIEW
                        or proposal.case_id != snapshot.case_id or review.case_id != snapshot.case_id
                        or review.workflow_id != snapshot.workflow_id):
                    continue
                items.append(PendingReviewRun(run_id=snapshot.run_id, case_id=snapshot.case_id,
                    case_subject=support_case.subject, workflow_id=snapshot.workflow_id,
                    updated_at=snapshot.updated_at, review=review))
            return PendingReviewPage(instance_id=self.instance_id, snapshot_at=now(), items=tuple(items))

    def decide(self, run_id, body, *, trace_id=None):
        require_roles(Role.REVIEWER, Role.ADMIN)
        self.check_instance(body.instance_id)
        with self._lock:
            run = self._authorized(run_id)
            if run.snapshot.status != "REVIEW_REQUIRED":
                raise FrontendError(409, "NOT_PAUSED")
            review = run.snapshot.review
            if review is None or any(getattr(body, key) != getattr(review, key)
                                     for key in ("workflow_id", "case_id", "proposal_id", "review_id")):
                raise FrontendError(409, "REVIEW_MISMATCH")
            proposal = Authorization(self._cases, self._proposals).proposal(body.proposal_id)
            if proposal.status != ProposalStatus.PENDING_REVIEW:
                raise FrontendError(409, "ALREADY_REVIEWED")
            if not self._proposals.matches_workflow_review(
                    proposal.id, workflow_id=body.workflow_id, case_id=body.case_id,
                    review_id=body.review_id):
                raise FrontendError(409, "REVIEW_MISMATCH")
            self._reserve()
            run.snapshot = run.snapshot.model_copy(update={"status": "RESUMING", "error": None, "trace_id": trace_id})
            self._publish(run, "STATE", "RESUMING")
            self._submit(run, HumanDecision.model_validate(body.model_dump(exclude={"instance_id"})), resume=True)
            return run.snapshot

    def audit(self, run_id, after, limit):
        require_roles(Role.REVIEWER, Role.ADMIN)
        with self._lock:
            run = self._authorized(run_id)
            if run.snapshot.status in ("RUNNING", "RESUMING"):
                raise FrontendError(409, "AUDIT_PENDING_USE_EVENTS")
            if self.agent_engine == "strands" and self._investigation_workflow_metadata is not None:
                try:
                    metadata = self._investigation_workflow_metadata.get_by_run_id(run_id)
                    if (metadata.run_id != run_id or metadata.case_id != run.snapshot.case_id
                            or metadata.workflow_id != run.snapshot.workflow_id):
                        raise FrontendError(503, "AUDIT_UNAVAILABLE")
                    records = self._investigation_workflow_metadata.events(metadata.workflow_id)
                except FrontendError:
                    raise
                except (KeyError, NotFoundError):
                    raise FrontendError(503, "AUDIT_UNAVAILABLE") from None
                selected = [event for event in records if event.sequence > after][:limit]
                summaries = []
                for event in selected:
                    if event.status in (WorkflowMetadataStatus.FAILED,
                                        WorkflowMetadataStatus.RECOVERY_REQUIRED):
                        kind = "FAILURE"
                        error = safe_error(metadata.failure_category) if event.status is WorkflowMetadataStatus.FAILED else None
                    elif event.status is WorkflowMetadataStatus.REVIEW_REQUIRED:
                        kind, error = "REVIEW_REQUIRED", None
                    else:
                        # Durable Strands metadata records lifecycle transitions
                        # only. It has no persisted tool/model event to expose.
                        kind, error = "AGENT", None
                    summaries.append(AuditSummary(
                        sequence=event.sequence, timestamp=event.created_at,
                        kind=kind, proposal_id=None, reviewer_user_id=None,
                        proposal_status=None, tool=None, error=error,
                        invalid_response_stage=(metadata.invalid_response_stage
                                                if event.status is WorkflowMetadataStatus.FAILED else None),
                        invalid_response_category=(metadata.invalid_response_category
                                                   if event.status is WorkflowMetadataStatus.FAILED else None),
                    ))
                cursor = summaries[-1].sequence if summaries else after
                return AuditPage(
                    run_id=run_id, events=tuple(summaries), next_after=cursor,
                    has_more=bool(records and cursor < records[-1].sequence),
                    scope="durable_workflow_audit",
                )
            if run.workflow is None or run.snapshot.workflow_id is None:
                raise FrontendError(503, "AUDIT_UNAVAILABLE")
            try:
                records = run.workflow.audit(run.snapshot.workflow_id)
            except KeyError:
                raise FrontendError(503, "AUDIT_UNAVAILABLE") from None
            selected = [event for event in records if event.sequence > after][:limit]
            summaries = []
            for event in selected:
                entry = event.agent_event
                tool = getattr(getattr(entry, "decision", None), "name", None)
                llm_failure = getattr(entry, "llm_failure", None)
                summaries.append(AuditSummary(sequence=event.sequence, timestamp=event.timestamp,
                    kind=event.kind, proposal_id=event.proposal_id, reviewer_user_id=event.reviewer_user_id,
                    proposal_status=event.proposal_status, tool=tool if tool in TOOLS else None,
                    error=safe_error(event.error or getattr(entry, "error", None)
                                     or getattr(llm_failure, "code", None)),
                    invalid_response_stage=getattr(llm_failure, "invalid_response_stage", None),
                    invalid_response_category=getattr(llm_failure, "invalid_response_category", None)))
            cursor = summaries[-1].sequence if summaries else after
            return AuditPage(run_id=run_id, events=tuple(summaries), next_after=cursor,
                             has_more=bool(records and cursor < records[-1].sequence),
                             scope="durable_workflow_audit" if self._durable else "in_memory_workflow_audit")
