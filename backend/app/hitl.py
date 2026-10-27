"""Local interrupt/resume workflow. Human review is never exposed to the LLM."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from threading import RLock
from typing import Callable, Literal
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, interrupt
from langsmith import tracing_context
from pydantic import ConfigDict, ValidationError

from .agent import (
    AgentConfig, AgentRequest, AgentResult, AuditEntry, Contract,
    agent_request_fingerprint,
)
from .domain import Description
from .graph_agent import GraphResolutionAgent, GraphState
from .llm import InvalidResponseCategory, InvalidResponseStage, StructuredLLM
from .proposals import HumanReview, ProposalService, ProposalStatus, ProposedAction
from .service import ConflictError, NotFoundError
from .tools import LocalTools
from .conversations import ConversationService, ConversationError, finish_memory
from .security import Role, current_identity, require_roles
from .observability import observed
from .workflow_metadata import (
    AGENT_FAILURE_CATEGORIES, FailureCategory, FailureStage,
    WorkflowMetadataRepository, WorkflowMetadataStatus, WorkflowRunReservation,
)
from .resolution_drafts import InvestigationOutcome, ResolutionDraft


class ReviewRequired(Contract):
    workflow_id: UUID
    case_id: UUID
    proposal_id: UUID
    review_id: UUID
    action: ProposedAction
    rationale: Description
    status: Literal["PENDING_REVIEW"] = "PENDING_REVIEW"


class HumanDecision(Contract):
    model_config = ConfigDict(revalidate_instances="always")

    workflow_id: UUID
    case_id: UUID
    proposal_id: UUID
    review_id: UUID
    decision: Literal["APPROVE", "REJECT"]
    note: Description | None = None


class WorkflowEvent(Contract):
    sequence: int
    kind: Literal["AGENT", "REVIEW_REQUIRED", "HUMAN_REVIEW", "FAILURE"]
    timestamp: datetime
    agent_event: AuditEntry | None = None
    proposal_id: UUID | None = None
    human_decision: HumanDecision | None = None
    reviewer_user_id: UUID | None = None
    proposal_status: ProposalStatus | None = None
    error: str | None = None


class HITLState(Contract):
    workflow_id: UUID
    agent: GraphState
    audit: tuple[WorkflowEvent, ...] = ()
    review: ReviewRequired | None = None
    reviewed_status: Literal["APPROVED", "REJECTED"] | None = None
    error: str | None = None


class WorkflowResult(Contract):
    conversation_id: UUID | None = None
    workflow_id: UUID
    status: Literal["REVIEW_REQUIRED", "REVIEWED", "COMPLETED", "FAILED"]
    message: str
    review: ReviewRequired | None = None
    reviewed_status: Literal["APPROVED", "REJECTED"] | None = None
    agent_result: AgentResult | None = None
    resolution_draft: ResolutionDraft | None = None
    investigation_outcome: InvestigationOutcome | None = None
    error: str | None = None
    actions_executed: Literal[False] = False


class ResumeRejected(Contract):
    ok: Literal[False] = False
    error: Literal["INVALID_INPUT", "UNKNOWN_WORKFLOW", "NOT_PAUSED", "REVIEW_MISMATCH", "ALREADY_REVIEWED"]


class _ReviewAlreadyRecorded(Exception):
    """A concurrent durable review won before this graph could checkpoint."""


class HITLWorkflow:
    def __init__(self, llm: StructuredLLM, tools: LocalTools, proposals: ProposalService,
                 config: AgentConfig | None = None, *, memory: ConversationService | None = None,
                 on_transition: Callable[[str, str, HITLState], None] | None = None,
                 checkpointer=None,
                 workflow_metadata: WorkflowMetadataRepository | None = None,
                 provider_label: Literal["local_scripted", "bedrock"] = "local_scripted") -> None:
        if (checkpointer is None) != (workflow_metadata is None):
            raise ValueError("Durable checkpoints require durable workflow metadata")
        self._config = config or AgentConfig()
        self._baseline = GraphResolutionAgent(
            llm, tools, self._config, memory=memory,
            enable_refund_wording=provider_label == "bedrock",
            deterministic_fallback_on_invalid_response=provider_label == "bedrock",
        )
        self._memory = memory
        self._on_transition = on_transition
        self._proposals = proposals
        self._workflow_metadata = workflow_metadata
        self._provider_label = provider_label
        self._lock = RLock()
        self._runs: dict[UUID, str] = {}
        graph = StateGraph(HITLState)
        for name in ("load_case", "evaluate_refund", "reason", "execute_tool", "record_result", "finalize", "fail_safely"):
            graph.add_node(name, self._instrument(name, self._delegate(name)))
        graph.add_node("prepare_review", self._instrument("prepare_review", self._prepare_review))
        graph.add_node("human_review", self._instrument("human_review", self._human_review))
        graph.add_edge(START, "load_case")
        graph.add_conditional_edges("load_case", lambda s: self._baseline._after_load_case(s.agent),
                                    {n: n for n in ("fail_safely", "evaluate_refund", "reason")})
        graph.add_conditional_edges("evaluate_refund", lambda s: self._after_refund_evaluation(s.agent),
                                    {"fail_safely": "fail_safely", "proposal_created": "prepare_review",
                                     "finalize": "finalize"})
        graph.add_conditional_edges("reason", lambda s: self._baseline._after_reason(s.agent),
                                    {n: n for n in ("fail_safely", "finalize", "execute_tool")})
        graph.add_edge("execute_tool", "record_result")
        graph.add_conditional_edges("record_result", self._after_record,
                                    {n: n for n in ("prepare_review", "fail_safely", "reason")})
        graph.add_conditional_edges("prepare_review", lambda s: "end" if s.error else "human_review",
                                    {"end": END, "human_review": "human_review"})
        graph.add_edge("human_review", END)
        graph.add_edge("finalize", END)
        graph.add_edge("fail_safely", END)
        self._graph = graph.compile(checkpointer=checkpointer or InMemorySaver())

    @staticmethod
    def _fingerprint(request: AgentRequest) -> str:
        """Hash request data for durable metadata without storing customer text."""
        return agent_request_fingerprint(request)

    def _transition_metadata(self, workflow_id: UUID, target: WorkflowMetadataStatus, *,
                             failure_category: FailureCategory | None = None,
                             failure_stage: FailureStage | None = None,
                             invalid_response_stage: InvalidResponseStage | None = None,
                             invalid_response_category: InvalidResponseCategory | None = None) -> None:
        if self._workflow_metadata is None:
            return
        run = self._workflow_metadata.get(workflow_id)
        if run.status == target:
            return
        self._workflow_metadata.transition(
            workflow_id, owner_user_id=run.owner_user_id, case_id=run.case_id,
            expected_status=run.status, status=target,
            failure_category=failure_category, failure_stage=failure_stage,
            invalid_response_stage=invalid_response_stage,
            invalid_response_category=invalid_response_category,
        )

    @staticmethod
    def _failure_metadata(state: HITLState | None):
        """Return a closed, provider-safe failure record without any exception text."""

        candidate = state.agent.error if state is not None else None
        if candidate in AGENT_FAILURE_CATEGORIES:
            invalid_stage = None
            invalid_category = None
            if candidate == "INVALID_RESPONSE" and state is not None:
                history = (state.agent.final_result.history
                           if state.agent.final_result is not None else state.agent.history)
                failure = next((entry.llm_failure for entry in reversed(history)
                                if entry.llm_failure is not None
                                and entry.llm_failure.code == "INVALID_RESPONSE"), None)
                if failure is not None:
                    invalid_stage = failure.invalid_response_stage
                    invalid_category = failure.invalid_response_category
            return candidate, "AGENT", invalid_stage, invalid_category
        return "WORKFLOW_ERROR", "WORKFLOW", None, None

    def _mark_recovery_or_failure(self, workflow_id: UUID, *,
                                  state: HITLState | None = None) -> None:
        if self._workflow_metadata is None:
            return
        try:
            run = self._workflow_metadata.get(workflow_id)
            if run.status in (WorkflowMetadataStatus.RUNNING, WorkflowMetadataStatus.RESUMING):
                category, stage, invalid_stage, invalid_category = self._failure_metadata(state)
                self._transition_metadata(
                    workflow_id, WorkflowMetadataStatus.FAILED,
                    failure_category=category, failure_stage=stage,
                    invalid_response_stage=invalid_stage,
                    invalid_response_category=invalid_category,
                )
            elif run.status == WorkflowMetadataStatus.REVIEW_REQUIRED:
                self._transition_metadata(workflow_id, WorkflowMetadataStatus.RECOVERY_REQUIRED)
        except Exception:
            # Metadata failure must never cause an automatic retry of a possibly
            # committed proposal/review operation.
            pass

    def _instrument(self, name, node):
        # Trusted, passive host callback. Sink failures cannot affect graph decisions
        # or retry a node with a possibly committed business/memory write.
        def publish(phase, state, updates=None):
            if self._on_transition is not None:
                try:
                    if updates is not None:
                        state = HITLState.model_validate({**state.model_dump(), **updates})
                    self._on_transition(name, phase, state)
                except Exception:
                    pass

        def wrapped(state: HITLState):
            publish("STARTED", state)
            try:
                updates = node(state)
            except GraphInterrupt:
                publish("PAUSED", state)
                raise
            except Exception:
                publish("FAILED", state)
                raise
            publish("COMPLETED", state, updates)
            return updates
        return wrapped

    @staticmethod
    def _event(state: HITLState, kind: str, **fields) -> WorkflowEvent:
        return WorkflowEvent(sequence=len(state.audit) + 1, kind=kind,
                             timestamp=datetime.now(timezone.utc), **fields)

    @staticmethod
    def _update(state: HITLState, **changes) -> dict:
        validated = HITLState.model_validate({
            **{key: getattr(state, key) for key in HITLState.model_fields}, **changes,
        })
        # Checkpoints contain JSON-compatible values, not application class objects.
        serialized = validated.model_dump(mode="json")
        return {key: serialized[key] for key in changes}

    def _delegate(self, name):
        def node(state: HITLState):
            before = state.agent
            updates = getattr(self._baseline, "_" + name)(before)
            after = GraphState.model_validate({**before.model_dump(), **updates})
            audit = list(state.audit)
            for entry in after.history[len(before.history):]:
                audit.append(WorkflowEvent(sequence=len(audit) + 1, kind="AGENT",
                    timestamp=datetime.now(timezone.utc), agent_event=entry))
            return self._update(state, agent=after, audit=tuple(audit))
        return node

    @staticmethod
    def _after_record(state: HITLState) -> str:
        agent = state.agent
        event = agent.history[-1] if agent.history else None
        if (event and event.result and event.result.ok and event.decision
                and event.decision.name == "create_resolution_proposal"
                and agent.created and any(agent.pending.values())
                and agent.error in (None, "STEP_LIMIT")):
            # The human boundary uses no further reasoning steps, even at the LLM limit.
            return "prepare_review"
        return "fail_safely" if agent.error else "reason"

    @staticmethod
    def _after_refund_evaluation(agent: GraphState) -> str:
        return GraphResolutionAgent._after_refund_evaluation(agent)

    def _failure(self, state: HITLState, code: str) -> dict:
        return self._update(state, error=code, audit=(*state.audit,
            self._event(state, "FAILURE", error=code)))

    def _prepare_review(self, state: HITLState) -> dict:
        record = state.agent.history[-1].result.data
        try:
            live = self._proposals.get(record.id)
        except NotFoundError:
            return self._failure(state, "PROPOSAL_NOT_FOUND")
        if live != record or live.case_id != state.agent.request.case_id or live.status != ProposalStatus.PENDING_REVIEW:
            return self._failure(state, "PROPOSAL_CHANGED")
        review = ReviewRequired(workflow_id=state.workflow_id, case_id=live.case_id,
            proposal_id=live.id, review_id=uuid4(), action=live.action, rationale=live.rationale)
        try:
            if self._workflow_metadata is not None:
                run = self._workflow_metadata.get(review.workflow_id)
                if run.case_id != review.case_id:
                    return self._failure(state, "PROPOSAL_CHANGED")
                if run.status != WorkflowMetadataStatus.REVIEW_REQUIRED:
                    self._transition_metadata(review.workflow_id, WorkflowMetadataStatus.REVIEW_REQUIRED)
            self._proposals.bind_workflow_review(
                live.id, workflow_id=review.workflow_id, case_id=review.case_id,
                review_id=review.review_id,
            )
        except Exception:
            self._mark_recovery_or_failure(review.workflow_id)
            return self._failure(state, "PROPOSAL_CHANGED")
        agent = GraphState.model_validate({**state.agent.model_dump(), "error": None})
        return self._update(state, review=review, agent=agent, audit=(*state.audit,
            self._event(state, "REVIEW_REQUIRED", proposal_id=live.id)))

    @staticmethod
    def _matches(decision: HumanDecision, review: ReviewRequired) -> bool:
        return all(getattr(decision, key) == getattr(review, key)
                   for key in ("workflow_id", "case_id", "proposal_id", "review_id"))

    def _human_review(self, state: HITLState) -> dict:
        # This node restarts on resume. Everything before interrupt is read-only;
        # proposal creation and pause audit recording are in earlier checkpointed nodes.
        payload = interrupt(state.review.model_dump(mode="json"))
        identity = require_roles(Role.REVIEWER, Role.ADMIN)
        try:
            decision = HumanDecision.model_validate(payload)
        except ValidationError:
            return self._failure(state, "INVALID_INPUT")
        if not self._matches(decision, state.review):
            return self._failure(state, "REVIEW_MISMATCH")
        try:
            proposal = self._proposals.get(decision.proposal_id)
            if proposal.case_id != decision.case_id:
                return self._failure(state, "REVIEW_MISMATCH")
            status = ProposalStatus.APPROVED if decision.decision == "APPROVE" else ProposalStatus.REJECTED
            reviewed = self._proposals.review(proposal.id, status, HumanReview(
                reviewer_name=str(identity.user_id), note=decision.note or "No note supplied.",
            ), workflow_id=decision.workflow_id, review_id=decision.review_id)
        except NotFoundError:
            return self._failure(state, "REVIEW_CONFLICT")
        except ConflictError:
            # The proposal repository has already committed another review.
            # Raising prevents this losing graph from writing a stale failure
            # checkpoint that could race the durable winner's REVIEWED state.
            raise _ReviewAlreadyRecorded() from None
        return self._update(state, reviewed_status=reviewed.status, audit=(*state.audit,
            self._event(state, "HUMAN_REVIEW", proposal_id=reviewed.id,
                        human_decision=decision, reviewer_user_id=identity.user_id, proposal_status=reviewed.status)))

    def _runtime_config(self, workflow_id: UUID) -> dict:
        return {"configurable": {"thread_id": str(workflow_id)},
                "recursion_limit": 3 * self._config.max_steps + 8, "callbacks": []}

    def _drive(self, workflow_id: UUID, value) -> WorkflowResult | ResumeRejected:
        config = self._runtime_config(workflow_id)
        with tracing_context(enabled=False):
            try:
                self._graph.invoke(value, config=config)
            except _ReviewAlreadyRecorded:
                # ProposalService.review is the durable arbitration point. Do
                # not checkpoint a loser-side error over the reviewer that
                # atomically recorded the human decision.
                # This recovered workflow is no longer paused.  Match the
                # durable resume contract regardless of which writer won the
                # metadata/proposal race.
                return ResumeRejected(error="NOT_PAUSED")
            except Exception:
                # A concurrent reviewer can commit the human decision after
                # this worker loaded the paused checkpoint.  Read that durable
                # winner before attempting any recovery write; writing a
                # failure state from the stale checkpoint would conflict with,
                # or overwrite, the completed review.
                snapshot = self._graph.get_state(config)
                if snapshot.values:
                    state = HITLState.model_validate(snapshot.values)
                    if state.reviewed_status is not None:
                        self._runs[workflow_id] = "REVIEWED"
                        return ResumeRejected(error="NOT_PAUSED")
                # Never retry a possibly committed review automatically.
                self._runs[workflow_id] = "FAILED"
                self._mark_recovery_or_failure(workflow_id)
                if snapshot.values:
                    try:
                        self._graph.update_state(config, self._failure(state, "WORKFLOW_ERROR"))
                    except Exception:
                        # A concurrent durable writer remains authoritative;
                        # do not replace its checkpoint with error recovery.
                        pass
                return WorkflowResult(workflow_id=workflow_id, status="FAILED", error="WORKFLOW_ERROR",
                    message="Workflow stopped. Inspect the proposal before retrying; no action was executed.")
            snapshot = self._graph.get_state(config)
        state = HITLState.model_validate(snapshot.values)
        if state.error:
            result = WorkflowResult(workflow_id=workflow_id, status="FAILED", error=state.error,
                                    message="Workflow stopped. No action was executed.")
        elif state.reviewed_status:
            result = WorkflowResult(workflow_id=workflow_id, status="REVIEWED", reviewed_status=state.reviewed_status,
                message=f"Human review recorded: {state.reviewed_status}. No action was executed.")
        elif snapshot.next == ("human_review",) and any(task.interrupts for task in snapshot.tasks):
            result = WorkflowResult(workflow_id=workflow_id, status="REVIEW_REQUIRED", review=state.review,
                                    message="Human review is required. No action was executed.")
        else:
            agent_result = state.agent.final_result
            result = WorkflowResult(workflow_id=workflow_id,
                status="FAILED" if agent_result is None or agent_result.error else "COMPLETED",
                agent_result=agent_result, error=agent_result.error if agent_result else "WORKFLOW_ERROR",
                message=agent_result.customer_response if agent_result else "Workflow stopped. No action was executed.")
        # The checkpoint may contain validated Bedrock wording. Preserve that
        # selected draft (including during a paused HITL review) instead of
        # replacing it with the earlier deterministic seed draft.
        result = result.model_copy(update={
            "resolution_draft": GraphResolutionAgent.resolution_draft_for_state(state.agent),
        })
        if state.agent.memory_started:
            result = result.model_copy(update={"conversation_id": state.agent.request.conversation_id})
            try:
                finish_memory(self._memory, state.agent.request.conversation_id, state.agent.request.case_id,
                              result.message)
            except ConversationError as failure:
                # Never replay a proposal/review because a text write failed. A paused
                # checkpoint remains resumable; report the independent memory failure.
                result = result.model_copy(update={"error": failure.code,
                    "message": result.message + " Conversation response could not be saved; do not replay the operation."})
        self._runs[workflow_id] = result.status
        if self._workflow_metadata is not None:
            try:
                if result.status == "COMPLETED":
                    self._transition_metadata(workflow_id, WorkflowMetadataStatus.COMPLETED)
                elif result.status == "FAILED":
                    self._mark_recovery_or_failure(workflow_id, state=state)
                elif result.status == "REVIEW_REQUIRED":
                    run = self._workflow_metadata.get(workflow_id)
                    if run.status != WorkflowMetadataStatus.REVIEW_REQUIRED:
                        self._mark_recovery_or_failure(workflow_id)
                elif result.status == "REVIEWED":
                    run = self._workflow_metadata.get(workflow_id)
                    if run.status != WorkflowMetadataStatus.REVIEWED:
                        self._mark_recovery_or_failure(workflow_id)
            except Exception:
                self._mark_recovery_or_failure(workflow_id)
        return result

    @observed("hitl.start")
    def start(self, request: AgentRequest, *, workflow_id: UUID | None = None,
              run_id: UUID | None = None, request_id: UUID | None = None) -> WorkflowResult:
        """Start a workflow; durable hosts may supply their already-issued IDs."""
        identity = current_identity()
        request = AgentRequest.model_validate(request)
        with self._lock:
            workflow_id = workflow_id or uuid4()
            if self._workflow_metadata is not None:
                self._workflow_metadata.reserve(WorkflowRunReservation(
                    workflow_id=workflow_id, run_id=run_id or uuid4(), case_id=request.case_id,
                    owner_user_id=identity.user_id, request_id=request_id or uuid4(),
                    request_fingerprint=self._fingerprint(request), provider=self._provider_label,
                ))
            self._runs[workflow_id] = "RUNNING"
            return self._drive(workflow_id, {"workflow_id": str(workflow_id),
                                          "agent": GraphState(request=request).model_dump(mode="json")})

    @observed("hitl.resume")
    def resume(self, payload: HumanDecision | dict) -> WorkflowResult | ResumeRejected:
        require_roles(Role.REVIEWER, Role.ADMIN)
        try:
            decision = HumanDecision.model_validate(payload)
        except ValidationError:
            return ResumeRejected(error="INVALID_INPUT")
        with self._lock:
            if self._workflow_metadata is not None:
                try:
                    run = self._workflow_metadata.get(decision.workflow_id)
                except NotFoundError:
                    return ResumeRejected(error="UNKNOWN_WORKFLOW")
                if run.status != WorkflowMetadataStatus.REVIEW_REQUIRED:
                    return ResumeRejected(error="NOT_PAUSED")
            else:
                if decision.workflow_id not in self._runs:
                    return ResumeRejected(error="UNKNOWN_WORKFLOW")
                if self._runs[decision.workflow_id] != "REVIEW_REQUIRED":
                    return ResumeRejected(error="NOT_PAUSED")
            with tracing_context(enabled=False):
                snapshot = self._graph.get_state(self._runtime_config(decision.workflow_id))
            state = HITLState.model_validate(snapshot.values)
            if (not state.review or not self._matches(decision, state.review)
                    or snapshot.next != ("human_review",) or not any(task.interrupts for task in snapshot.tasks)):
                return ResumeRejected(error="REVIEW_MISMATCH")
            try:
                proposal = self._proposals.get(decision.proposal_id)
            except NotFoundError:
                return ResumeRejected(error="REVIEW_MISMATCH")
            if proposal.case_id != decision.case_id:
                return ResumeRejected(error="REVIEW_MISMATCH")
            if not self._proposals.matches_workflow_review(
                    proposal.id, workflow_id=decision.workflow_id, case_id=decision.case_id,
                    review_id=decision.review_id):
                return ResumeRejected(error="REVIEW_MISMATCH")
            if proposal.status != ProposalStatus.PENDING_REVIEW:
                # Recovered durable workflows expose NOT_PAUSED after a
                # competing reviewer commits, even if the proposal update is
                # observed just before its metadata transition.
                return ResumeRejected(
                    error="NOT_PAUSED"
                    if self._workflow_metadata is not None
                    else "ALREADY_REVIEWED"
                )
            if self._workflow_metadata is not None:
                try:
                    self._transition_metadata(decision.workflow_id, WorkflowMetadataStatus.RESUMING)
                except Exception:
                    return ResumeRejected(error="NOT_PAUSED")
            self._runs[decision.workflow_id] = "RESUMING"
            return self._drive(decision.workflow_id, Command(resume=decision.model_dump(mode="json")))

    def audit(self, workflow_id: UUID) -> tuple[WorkflowEvent, ...]:
        """Local diagnostic history, separate from the minimal reviewer payload."""
        require_roles(Role.REVIEWER, Role.ADMIN)
        with self._lock, tracing_context(enabled=False):
            if self._workflow_metadata is not None:
                try:
                    self._workflow_metadata.get(workflow_id)
                except NotFoundError:
                    raise KeyError("Unknown workflow") from None
            elif workflow_id not in self._runs:
                raise KeyError("Unknown workflow")
            state = HITLState.model_validate(self._graph.get_state(self._runtime_config(workflow_id)).values)
            return tuple(event.model_copy(deep=True) for event in state.audit)
