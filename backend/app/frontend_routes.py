"""Phase 17A.1 HTTP contracts. Credentials/configuration/state never come from bodies."""

from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from .conversations import ConversationError
from .hitl import WorkflowResult
from .frontend_contracts import (
    AuditPage, ConversationPage, DiagnosticsResponse, EventPage, IdentityResponse, MessagePage,
    OperationMetrics, PendingReviewPage, RunDecision, RunPage, RunSnapshot, RunStart, TelemetryEvent,
    CaseInvestigationSnapshot, InvestigationControlSnapshot, InvestigationControlUpdate,
    InvestigationModeAuditItem, InvestigationModeAuditPage, DurableInvestigationItem,
    DurableInvestigationPage, DiagnosticsEventPage, AnalyticsResponse,
)
from .frontend_runtime import FrontendError, FrontendRuntime, now, safe_error
from .observability import OPERATIONS
from .security import Authorization, Role, current_identity, require_roles
from .domain import InvestigationJobStatus, InvestigationMode
from .service import NotFoundError
from .workflow_metadata import WorkflowMetadataStatus


class ContractRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # FastAPI's default validation response echoes rejected input.
                raise FrontendError(422, "INVALID_INPUT") from None
        return validated


router = APIRouter(prefix="/api", tags=["frontend-contracts"], route_class=ContractRoute)
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[int, Query(ge=0, le=1000000)]


def get_runtime(request: Request) -> FrontendRuntime:
    return request.app.state.frontend_runtime


Runtime = Annotated[FrontendRuntime, Depends(get_runtime)]


def _control_repository(request: Request):
    repository = request.app.state.investigation_control
    if repository is None:
        raise FrontendError(503, "INVESTIGATION_CONTROL_UNAVAILABLE")
    return repository


def _control_snapshot(request: Request) -> InvestigationControlSnapshot:
    repository = _control_repository(request)
    control = repository.get()
    available = request.app.state.investigation_dispatcher is not None
    effective = control.mode if (control.mode is InvestigationMode.MANUAL or available) else InvestigationMode.MANUAL
    return InvestigationControlSnapshot(
        configured_mode=control.mode, effective_mode=effective,
        automatic_capability_available=available, version=control.version,
        updated_by=control.updated_by, updated_at=control.updated_at,
        counts=repository.status_counts(),
    )


def _case_investigation_snapshot(request: Request, case_id: UUID) -> CaseInvestigationSnapshot:
    Authorization(request.app.state.case_service).case(case_id)
    jobs = request.app.state.investigation_jobs
    if jobs is None:
        raise FrontendError(503, "INVESTIGATION_CONTROL_UNAVAILABLE")
    try:
        job = jobs.get_by_case(case_id)
    except NotFoundError:
        return CaseInvestigationSnapshot(
            case_id=case_id, state="NOT_AVAILABLE", execution_started=False,
            can_start=False,
        )
    workflow = None
    message = None
    error = None
    checkpoint_error = None
    resolution_draft = None
    investigation_outcome = None
    valid = False
    if job.workflow_id is not None:
        metadata = getattr(request.app.state, "investigation_workflow_metadata", None)
        if metadata is None:
            metadata = request.app.state.workflow_metadata
        if metadata is None:
            raise FrontendError(503, "INVESTIGATION_STATUS_UNAVAILABLE")
        try:
            workflow = metadata.get(job.workflow_id)
        except NotFoundError:
            raise FrontendError(503, "INVESTIGATION_STATUS_UNAVAILABLE") from None
        if request.app.state.frontend_runtime.agent_engine == "strands":
            result_repository = getattr(request.app.state, "investigation_results", None)
            payload = result_repository.get_by_job(job.id) if result_repository is not None else None
            if payload is not None:
                try:
                    result = WorkflowResult.model_validate(payload)
                    agent_result = result.agent_result
                    resolution_draft = None if agent_result is None else agent_result.resolution_draft
                    investigation_outcome = result.investigation_outcome
                    message = result.message
                    checkpoint_error = result.error
                    valid = (result.status == "COMPLETED" and agent_result is not None
                             and agent_result.error is None and bool(agent_result.customer_response))
                    if (valid and investigation_outcome is not None and resolution_draft is not None
                            and investigation_outcome.response_source != resolution_draft.response_source):
                        valid = False
                        checkpoint_error = "WORKFLOW_ERROR"
                except Exception:
                    checkpoint_error = "WORKFLOW_ERROR"
            else:
                checkpoint_error = None
        else:
            resolution_draft, message, checkpoint_error, valid = request.app.state.frontend_runtime._checkpoint_projection(
                job.workflow_id)
        # Durable metadata is authoritative for a failed/recovery-required
        # workflow. The checkpoint can still contain the deterministic draft
        # seeded during load_case even when the graph failed later, so do not
        # let that draft hide the persisted failure category.
        error = (safe_error(workflow.failure_category) if workflow.failure_category
                 else safe_error(checkpoint_error) if checkpoint_error else None)
    if job.status is InvestigationJobStatus.PENDING:
        state = "PENDING"
    elif (job.status is InvestigationJobStatus.RECOVERY_REQUIRED
          or workflow is not None and workflow.status is WorkflowMetadataStatus.RECOVERY_REQUIRED):
        state = "RECOVERY_REQUIRED"
    elif workflow is not None and workflow.status is WorkflowMetadataStatus.REVIEW_REQUIRED:
        state = "REVIEW_REQUIRED"
    elif workflow is not None and workflow.status in (
        WorkflowMetadataStatus.COMPLETED, WorkflowMetadataStatus.REVIEWED,
    ):
        state = "COMPLETED" if valid else "FAILED"
        if state == "FAILED":
            error = "WORKFLOW_ERROR"
    elif workflow is not None and workflow.status is WorkflowMetadataStatus.FAILED:
        state = "FAILED"
    else:
        state = "RUNNING"
    identity = current_identity()
    manual_mode = _control_repository(request).get().mode is InvestigationMode.MANUAL
    return CaseInvestigationSnapshot(
        case_id=case_id, job_id=job.id, state=state, job_status=job.status,
        workflow_id=job.workflow_id,
        workflow_status=workflow.status if workflow is not None else None,
        provider=workflow.provider if workflow is not None else None,
        submission_mode=job.submission_mode, mode_version=job.mode_version,
        submitted_at=job.created_at,
        execution_started=job.execution_attempt_id is not None,
        message=message, error=error, resolution_draft=resolution_draft,
        investigation_outcome=investigation_outcome,
        can_start=(identity.role is Role.ADMIN
                   and job.status is InvestigationJobStatus.PENDING
                   and manual_mode
                   and (request.app.state.investigation_executor is not None
                        or getattr(request.app.state, "investigation_outbox", None) is not None)),
    )


@router.get("/identity", response_model=IdentityResponse)
def identity(runtime: Runtime):
    subject = current_identity()
    privileged = subject.role in (Role.REVIEWER, Role.ADMIN)
    return IdentityResponse(identity=subject, can_review=privileged, can_view_diagnostics=privileged,
        instance_id=runtime.instance_id, provider=runtime.provider_label if (runtime.provider_factory or runtime.strands_model_factory) else "unavailable",
        memory_available=runtime.memory is not None)


@router.get("/admin/investigation-control", response_model=InvestigationControlSnapshot)
def investigation_control(request: Request):
    require_roles(Role.REVIEWER, Role.ADMIN)
    return _control_snapshot(request)


@router.patch("/admin/investigation-control", response_model=InvestigationControlSnapshot)
def update_investigation_control(
        body: InvestigationControlUpdate, request: Request,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
):
    identity = require_roles(Role.ADMIN)
    if body.mode is InvestigationMode.AUTO and request.app.state.investigation_dispatcher is None:
        raise FrontendError(409, "AUTO_INVESTIGATION_UNAVAILABLE")
    _control_repository(request).set_mode(
        body.mode, expected_version=body.expected_version,
        admin_user_id=identity.user_id, idempotency_key=idempotency_key,
    )
    # A mode change is control-plane only. It deliberately does not enumerate,
    # claim, submit, or replay any existing investigation job.
    return _control_snapshot(request)


@router.get("/admin/investigation-control/audit", response_model=InvestigationModeAuditPage)
def investigation_control_audit(request: Request, after: Cursor = 0, limit: Limit = 100):
    require_roles(Role.ADMIN)
    events = _control_repository(request).audit(after=after, limit=limit + 1)
    has_more = len(events) > limit
    events = events[:limit]
    items = tuple(InvestigationModeAuditItem(
        sequence=event.sequence, previous_mode=event.previous_mode, mode=event.mode,
        version=event.version, admin_user_id=event.admin_user_id,
        changed_at=event.changed_at,
    ) for event in events)
    next_after = items[-1].sequence if items else after
    return InvestigationModeAuditPage(
        items=items, next_after=next_after, has_more=has_more,
    )


@router.get("/cases/{case_id}/investigation", response_model=CaseInvestigationSnapshot)
def case_investigation(case_id: UUID, request: Request):
    require_roles(Role.REVIEWER, Role.ADMIN)
    return _case_investigation_snapshot(request, case_id)


@router.post("/cases/{case_id}/investigation", response_model=CaseInvestigationSnapshot,
             status_code=202)
def start_case_investigation(
        case_id: UUID, request: Request,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
):
    identity = require_roles(Role.ADMIN)
    Authorization(request.app.state.case_service).case(case_id)
    jobs = request.app.state.investigation_jobs
    executor = request.app.state.investigation_executor
    outbox = getattr(request.app.state, "investigation_outbox", None)
    if jobs is None or (executor is None and outbox is None):
        raise FrontendError(503, "INVESTIGATION_EXECUTION_UNAVAILABLE")
    if _control_repository(request).get().mode is not InvestigationMode.MANUAL:
        raise FrontendError(409, "MANUAL_INVESTIGATION_DISABLED")
    try:
        job = jobs.get_by_case(case_id)
    except NotFoundError:
        raise FrontendError(409, "DURABLE_INVESTIGATION_JOB_REQUIRED") from None
    registered, created = jobs.register_manual_start(
        job.id, admin_user_id=identity.user_id, idempotency_key=idempotency_key,
    )
    if created and registered.status is InvestigationJobStatus.PENDING:
        if executor is not None and not executor.submit_as(registered.id, worker_id=identity.user_id):
            raise FrontendError(503, "INVESTIGATION_EXECUTION_UNAVAILABLE")
    return _case_investigation_snapshot(request, case_id)


@router.get("/reviews", response_model=PendingReviewPage)
def pending_reviews(runtime: Runtime):
    return runtime.pending_reviews()


@router.post("/runs", response_model=RunSnapshot, status_code=202)
def start_run(body: RunStart, request: Request, runtime: Runtime):
    require_roles(Role.REVIEWER, Role.ADMIN)
    return runtime.start(body, trace_id=UUID(request.state.trace_id))


@router.get("/runs/{run_id}", response_model=RunSnapshot)
def get_run(run_id: UUID, runtime: Runtime):
    return runtime.snapshot(run_id)


@router.get("/runs/{run_id}/events", response_model=EventPage)
def get_events(run_id: UUID, runtime: Runtime, after: Cursor = 0, limit: Limit = 50):
    return runtime.events(run_id, after, limit)


@router.get("/cases/{case_id}/runs", response_model=RunPage)
def list_runs(case_id: UUID, runtime: Runtime, offset: Cursor = 0, limit: Limit = 50):
    return runtime.list_for_case(case_id, offset, limit)


@router.post("/runs/{run_id}/decision", response_model=RunSnapshot, status_code=202)
def decide(run_id: UUID, body: RunDecision, request: Request, runtime: Runtime):
    return runtime.decide(run_id, body, trace_id=UUID(request.state.trace_id))


def memory_for_case(request, runtime, case_id):
    Authorization(request.app.state.case_service).case(case_id)
    if runtime.memory is None:
        raise FrontendError(503, "CONVERSATION_UNAVAILABLE")
    return runtime.memory


@router.get("/cases/{case_id}/conversations", response_model=ConversationPage)
def conversations(case_id: UUID, request: Request, runtime: Runtime, offset: Cursor = 0, limit: Limit = 50):
    records = memory_for_case(request, runtime, case_id).list(case_id)
    return ConversationPage(items=records[offset:offset + limit],
        next_offset=offset + limit if offset + limit < len(records) else None)


@router.get("/cases/{case_id}/conversations/{conversation_id}", response_model=MessagePage)
def history(case_id: UUID, conversation_id: UUID, request: Request, runtime: Runtime,
            after: Cursor = 0, limit: Limit = 50):
    saved = memory_for_case(request, runtime, case_id).load(conversation_id, case_id)
    messages = tuple(message for message in saved.messages if message.sequence > after)[:limit]
    cursor = messages[-1].sequence if messages else after
    return MessagePage(conversation=saved.conversation, messages=messages, next_after=cursor,
                       has_more=cursor < saved.conversation.message_count)


@router.get("/runs/{run_id}/audit", response_model=AuditPage)
def audit(run_id: UUID, runtime: Runtime, after: Cursor = 0, limit: Limit = 50):
    return runtime.audit(run_id, after, limit)


@router.get("/diagnostics", response_model=DiagnosticsResponse)
def diagnostics(request: Request, limit: Limit = 50):
    require_roles(Role.REVIEWER, Role.ADMIN)
    snapshot = request.app.state.observer.snapshot()
    # Observer already supplies a closed, redacted metadata schema. Explicitly
    # select fields again at the HTTP boundary; no internal object serialization.
    events = [{key: value for key, value in event.items() if key in TelemetryEvent.model_fields}
              for event in snapshot["events"][-limit:]]
    return {"snapshot_at": now(), "scope": "process_metadata_only", "events": events,
            "metrics": {operation: {key: value for key, value in metric.items() if key in OperationMetrics.model_fields}
                        for operation, metric in snapshot["metrics"].items() if operation in OPERATIONS},
            "evicted_events": snapshot["evicted_events"],
            "omitted_events": max(0, len(snapshot["events"]) - limit),
            "logging_failures": snapshot["logging_failures"]}


def _diagnostic_event_category(operation: str) -> str:
    if operation == "http":
        return "http"
    if operation == "llm":
        return "model"
    if operation == "tool":
        return "tools"
    return "workflow"


@router.get("/admin/diagnostics/investigations", response_model=DurableInvestigationPage)
def diagnostic_investigations(request: Request, offset: Cursor = 0, limit: Limit = 25,
                              case_id: UUID | None = None, run_id: UUID | None = None):
    require_roles(Role.ADMIN)
    # LangGraph exposes its durable metadata through ``workflow_metadata``.
    # PostgreSQL + Strands uses the investigation-specific repository because
    # it has no LangGraph checkpoint/runtime.  Prefer that repository while
    # retaining the existing local LangGraph fallback.
    metadata = getattr(request.app.state, "investigation_workflow_metadata", None)
    if metadata is None:
        metadata = getattr(request.app.state, "workflow_metadata", None)
    jobs = request.app.state.investigation_jobs
    if metadata is None or jobs is None:
        raise FrontendError(503, "INVESTIGATION_STATUS_UNAVAILABLE")
    records = metadata.recent_runs(offset=offset, limit=limit + 1, case_id=case_id, run_id=run_id)
    has_more = len(records) > limit
    records = records[:limit]
    items = []
    for record in records:
        mode = None
        mode_version = None
        job = None
        try:
            job = jobs.get_by_case(record.case_id)
            if job.workflow_id == record.workflow_id:
                mode, mode_version = job.submission_mode, job.mode_version
        except NotFoundError:
            pass
        response_source = None
        try:
            runtime = request.app.state.frontend_runtime
            if runtime.agent_engine == "strands":
                result_repository = getattr(request.app.state, "investigation_results", None)
                if result_repository is not None and job is not None and job.workflow_id == record.workflow_id:
                    payload = result_repository.get_by_job(job.id)
                    if payload is not None:
                        result = WorkflowResult.model_validate(payload)
                        outcome = result.investigation_outcome
                        draft = None if result.agent_result is None else result.agent_result.resolution_draft
                        if (outcome is not None and draft is not None
                                and outcome.response_source != draft.response_source):
                            raise ValueError("inconsistent investigation outcome")
                        response_source = (outcome.response_source if outcome is not None
                                           else draft.response_source if draft is not None else None)
            else:
                draft, _, _, _ = runtime._checkpoint_projection(record.workflow_id)
                response_source = draft.response_source if draft is not None else None
        except Exception:
            # Checkpoint projection is supplementary diagnostic attribution;
            # never fail or expose checkpoint details from this Admin page.
            pass
        items.append(DurableInvestigationItem(
            case_id=record.case_id, workflow_id=record.workflow_id, run_id=record.run_id,
            workflow_status=record.status, provider=record.provider,
            submission_mode=mode, mode_version=mode_version,
            created_at=record.created_at, updated_at=record.updated_at,
            failure_category=record.failure_category, failure_stage=record.failure_stage,
            error_source=("workflow_metadata" if record.failure_category else None),
            response_source=response_source,
        ))
    next_offset = offset + limit if has_more else None
    return DurableInvestigationPage(items=tuple(items), next_offset=next_offset, has_more=has_more)


@router.get("/admin/diagnostics/events", response_model=DiagnosticsEventPage)
def diagnostic_events(request: Request, category: str = Query(...), offset: Cursor = 0,
                      limit: Limit = 50):
    require_roles(Role.ADMIN)
    if category not in {"workflow", "model", "tools", "http"}:
        raise FrontendError(422, "INVALID_INPUT")
    snapshot = request.app.state.observer.snapshot()
    selected = [event for event in snapshot["events"] if _diagnostic_event_category(event["operation"]) == category]
    selected = list(reversed(selected))
    page = selected[offset:offset + limit + 1]
    has_more = len(page) > limit
    page = page[:limit]
    events = tuple({key: value for key, value in event.items() if key in TelemetryEvent.model_fields}
                   for event in page)
    return DiagnosticsEventPage(events=events, next_offset=offset + limit if has_more else None,
                                has_more=has_more, category=category)


@router.get("/admin/analytics", response_model=AnalyticsResponse)
def admin_analytics(request: Request,
                    from_date: date | None = Query(None, alias="from"),
                    to_date: date | None = Query(None, alias="to")):
    require_roles(Role.ADMIN)
    repository = request.app.state.analytics
    if repository is None:
        raise FrontendError(503, "ANALYTICS_UNAVAILABLE")
    today = datetime.now(timezone.utc).date()
    end_date = to_date or today
    start_date = from_date or (end_date - timedelta(days=29))
    if start_date > end_date or (end_date - start_date).days > 365:
        raise FrontendError(422, "INVALID_INPUT")
    start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    values = repository.summary(start, end)
    response_sources = {"BEDROCK_VALIDATED": 0, "DETERMINISTIC_FALLBACK": 0, "UNAVAILABLE": 0}
    for observation in values.pop("run_observations"):
        try:
            runtime = request.app.state.frontend_runtime
            source = None
            result_repository = getattr(request.app.state, "investigation_results", None)
            if (observation["provider"] == "bedrock" and result_repository is not None
                    and observation.get("case_id") is not None):
                job = request.app.state.investigation_jobs.get_by_case(UUID(observation["case_id"]))
                payload = result_repository.get_by_job(job.id)
                if payload is not None:
                    result = WorkflowResult.model_validate(payload)
                    outcome = result.investigation_outcome
                    draft = None if result.agent_result is None else result.agent_result.resolution_draft
                    if (outcome is not None and draft is not None
                            and outcome.response_source != draft.response_source):
                        raise ValueError("inconsistent investigation outcome")
                    source = (outcome.response_source if outcome is not None
                              else draft.response_source if draft is not None else None)
            elif runtime.agent_engine != "strands":
                draft, _, _, _ = runtime._checkpoint_projection(UUID(observation["workflow_id"]))
                source = draft.response_source if draft is not None else None
            response_sources[source if source in response_sources else "UNAVAILABLE"] += 1
        except Exception:
            response_sources["UNAVAILABLE"] += 1
    measured = sum(response_sources.values())
    if measured < values["workflow_count"]:
        response_sources["UNAVAILABLE"] += values["workflow_count"] - measured
    return AnalyticsResponse(from_date=start_date.isoformat(), to_date=end_date.isoformat(),
                             response_source_counts=response_sources, **values)


def conversation_status(error: ConversationError):
    return {"CONVERSATION_NOT_FOUND": 404, "CONVERSATION_CASE_NOT_FOUND": 404,
            "CONVERSATION_CASE_MISMATCH": 409, "CONVERSATION_FULL": 409}.get(error.code, 503)
