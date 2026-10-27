"""Local API entry point; optional trusted-host workflow and conversation composition."""

from contextlib import asynccontextmanager
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Callable, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .routes import router
from .operation_routes import router as operation_router
from .operations import BusinessOperations, DeliveryFactIngestion, TrustedDeliverySource
from .proposal_routes import router as proposal_router
from .proposals import ProposalService
from .service import CaseService, ConflictError, NotFoundError
from .tools import LocalTools
from .security import (
    AuthenticatedIdentity, AuthenticationProvider, CognitoAuthenticationProvider,
    CognitoConfiguration, CognitoCustomerBinding, LocalAuthenticationProvider,
    Role, SecurityError, authenticated,
)
from .observability import LocalObserver, observing, span
from .conversations import ConversationError, ConversationService, ConversationStore
from .frontend_routes import router as frontend_router, conversation_status
from .frontend_runtime import FrontendError, FrontendRuntime
from .llm import LLMProvider
from .catalog import CatalogService
from .catalog_routes import router as catalog_router
from .customer_orders import CustomerOrderService
from .customer_order_routes import router as customer_order_router
from .customer_case_routes import router as customer_case_router
from .domain import Customer
from .langgraph_checkpoints import SQLiteCheckpointSaver
from .dispatcher import AutomaticInvestigationDispatcher
from .analytics import DurableAnalyticsRepository
from .persistence import (
    SQLiteCaseRepository, SQLitePersistence, SQLiteProposalRepository,
    SQLiteCognitoCustomerBindingRepository,
    SQLiteInvestigationControlRepository, SQLiteInvestigationJobRepository,
    SQLiteWorkflowMetadataRepository,
)
from .postgres_persistence import (
    PostgreSQLCaseRepository, PostgreSQLCheckpointSaver,
    PostgreSQLCognitoCustomerBindingRepository,
    PostgreSQLInvestigationControlRepository, PostgreSQLInvestigationJobRepository,
    PostgreSQLInvestigationOutboxRepository,
    PostgreSQLInvestigationResultRepository,
    PostgreSQLHITLReviewService, PostgreSQLPersistence, PostgreSQLProposalRepository,
    PostgreSQLRefundEvidenceRepository,
    PostgreSQLWorkflowMetadataRepository,
)
from .strands_worker import PostgreSQLStrandsWorker


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["novamind-api"] = "novamind-api"


def health() -> HealthResponse:
    """Confirm the API process responds; this does not check AWS dependencies."""
    return HealthResponse()


def create_app(*, auth_provider: AuthenticationProvider | None = None,
               observer: LocalObserver | None = None, local_provider_factory: Callable[[], LLMProvider] | None = None,
               provider_label: Literal["local_scripted", "bedrock"] = "local_scripted",
               agent_engine: Literal["langgraph", "strands"] = "langgraph",
               strands_model_factory: Callable[[], object] | None = None,
               conversation_store: ConversationStore | None = None,
               sqlite_path: str | Path | None = None,
               postgres_dsn: str | None = None,
               startup_customers: tuple[Customer, ...] = (),
               cognito_configuration: CognitoConfiguration | None = None,
               cognito_customer_bindings: tuple[CognitoCustomerBinding, ...] = (),
               cognito_jwks_fetcher: Callable[[str], Mapping[str, Any]] | None = None,
               trusted_delivery_sources: tuple[TrustedDeliverySource, ...] = (),
               automatic_dispatcher: bool = False,
               dispatcher_identity: AuthenticatedIdentity | None = None,
               process_role: Literal["local", "api", "publisher", "worker"] = "local") -> FastAPI:
    if not isinstance(automatic_dispatcher, bool):
        raise ValueError("automatic_dispatcher must be a boolean")
    if process_role not in ("local", "api", "publisher", "worker"):
        raise ValueError("Invalid process role")
    if process_role == "worker" and (postgres_dsn is None or agent_engine != "strands"):
        raise ValueError("Strands worker requires PostgreSQL and the Strands engine")
    if process_role in ("api", "publisher") and automatic_dispatcher:
        raise ValueError("Non-worker process cannot enable an in-process dispatcher")
    if automatic_dispatcher and dispatcher_identity is None:
        raise ValueError("Automatic dispatcher requires a server-owned worker identity")
    if sqlite_path is not None and postgres_dsn is not None:
        raise ValueError("Configure only one durable persistence backend")
    if auth_provider is not None and cognito_configuration is not None:
        raise ValueError("Configure either a custom authentication provider or Cognito")
    if cognito_customer_bindings and cognito_configuration is None:
        raise ValueError("Cognito customer bindings require Cognito authentication")
    if cognito_configuration is not None and sqlite_path is None and postgres_dsn is None:
        raise ValueError("Cognito authentication requires durable persistence")
    if automatic_dispatcher and ((sqlite_path is None and postgres_dsn is None) or agent_engine != "langgraph"
                                 or local_provider_factory is None):
        raise ValueError("Automatic dispatcher requires durable persistence, LangGraph, and a provider")
    if automatic_dispatcher:
        dispatcher_identity = AuthenticatedIdentity.model_validate(dispatcher_identity)
        if dispatcher_identity.role not in (Role.REVIEWER, Role.ADMIN):
            raise ValueError("Automatic dispatcher requires a staff identity")
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            executor = app.state.investigation_executor
            if executor is not None:
                executor.close()
            app.state.frontend_runtime.close()

    app = FastAPI(
        title="NovaMind API",
        description="Local authenticated case/proposal operations. No action execution.",
        version="0.13.0",
        lifespan=lifespan,
    )
    persistence = None
    is_postgres = postgres_dsn is not None
    if sqlite_path is not None:
        database_path = Path(sqlite_path)
        if not database_path.is_absolute():
            raise ValueError("SQLite persistence path must be absolute")
        # SQLitePersistence opens short-lived connections per transaction, so
        # migrations complete before the app begins serving and no connection
        # needs to outlive this application instance.
        persistence = SQLitePersistence(database_path, initialize=True)
    elif postgres_dsn is not None:
        # The PostgreSQL adapter owns transactional migrations.  It never
        # touches the optional SQLite path and is safe for multiple task starts.
        persistence = PostgreSQLPersistence(postgres_dsn, initialize=True)
    app.state.persistence = persistence
    app.state.process_role = process_role
    app.state.analytics = DurableAnalyticsRepository(persistence) if persistence is not None else None
    # Compatibility for existing local diagnostics/tests.  New code must use
    # ``persistence``; this attribute may hold either supported backend.
    app.state.sqlite_persistence = persistence
    case_repository = PostgreSQLCaseRepository if is_postgres else SQLiteCaseRepository
    cognito_binding_repository = (
        PostgreSQLCognitoCustomerBindingRepository if is_postgres
        else SQLiteCognitoCustomerBindingRepository
    )
    proposal_repository = PostgreSQLProposalRepository if is_postgres else SQLiteProposalRepository
    control_repository = (
        PostgreSQLInvestigationControlRepository if is_postgres
        else SQLiteInvestigationControlRepository
    )
    jobs_repository = PostgreSQLInvestigationJobRepository if is_postgres else SQLiteInvestigationJobRepository
    workflow_repository = PostgreSQLWorkflowMetadataRepository if is_postgres else SQLiteWorkflowMetadataRepository
    checkpointer_type = PostgreSQLCheckpointSaver if is_postgres else SQLiteCheckpointSaver
    evidence_repository = PostgreSQLRefundEvidenceRepository(persistence) if is_postgres else None
    app.state.investigation_jobs = (
        jobs_repository(persistence) if persistence is not None else None
    )
    app.state.investigation_outbox = (
        PostgreSQLInvestigationOutboxRepository(persistence) if is_postgres else None
    )
    app.state.investigation_control = (
        control_repository(persistence) if persistence is not None else None
    )
    app.state.case_service = CaseService(
        case_repository(persistence) if persistence is not None else None
    )
    # Seed deterministic startup customers before the app begins serving requests.
    # This is the only authorised call site for seed_customer(); no HTTP route
    # may call it. A seeded record does not itself grant authentication — a valid
    # bearer token is still required for every request.
    for customer in startup_customers:
        app.state.case_service.seed_customer(customer)
    app.state.cognito_customer_bindings = None
    if cognito_configuration is not None:
        configuration = CognitoConfiguration.model_validate(cognito_configuration)
        bindings = cognito_binding_repository(persistence)
        for binding in cognito_customer_bindings:
            bindings.bind(CognitoCustomerBinding.model_validate(binding))
        app.state.cognito_customer_bindings = bindings
        app.state.auth_provider = CognitoAuthenticationProvider(
            configuration, bindings, jwks_fetcher=cognito_jwks_fetcher,
        )
    else:
        app.state.auth_provider = auth_provider if auth_provider is not None else LocalAuthenticationProvider()
    app.state.business_operations = BusinessOperations(
        app.state.case_service, evidence_repository=evidence_repository,
    )
    app.state.delivery_fact_ingestion = DeliveryFactIngestion(
        app.state.business_operations, trusted_delivery_sources,
    )
    proposal_repository_instance = proposal_repository(persistence) if persistence is not None else None
    app.state.proposal_service = ProposalService(
        app.state.case_service, app.state.business_operations,
        proposal_repository_instance,
    )
    app.state.hitl_review_service = (
        PostgreSQLHITLReviewService(proposal_repository_instance)
        if is_postgres else app.state.proposal_service
    )
    app.state.local_tools = LocalTools(
        app.state.case_service, app.state.business_operations, app.state.proposal_service,
    )
    app.state.catalog_service = CatalogService()
    app.state.customer_order_service = CustomerOrderService(
        app.state.case_service, app.state.catalog_service,
    )
    app.state.observer = observer if observer is not None else LocalObserver()
    memory = ConversationService(conversation_store, app.state.case_service) if conversation_store is not None else None
    workflow_checkpointer = (
        checkpointer_type(persistence)
        if persistence is not None and agent_engine == "langgraph" else None
    )
    durable_workflow_metadata = workflow_repository(persistence) if persistence is not None else None
    workflow_metadata = durable_workflow_metadata if agent_engine == "langgraph" else None
    app.state.workflow_metadata = workflow_metadata
    app.state.investigation_workflow_metadata = durable_workflow_metadata
    app.state.investigation_results = (
        PostgreSQLInvestigationResultRepository(persistence)
        if is_postgres and agent_engine == "strands" else None
    )
    app.state.frontend_runtime = FrontendRuntime(app.state.case_service, app.state.local_tools,
        app.state.proposal_service, local_provider_factory=local_provider_factory,
        provider_label=provider_label, memory=memory,
        agent_engine=agent_engine, strands_model_factory=strands_model_factory,
        checkpointer=workflow_checkpointer, workflow_metadata=workflow_metadata,
        investigation_workflow_metadata=(durable_workflow_metadata
                                         if agent_engine == "strands" else None),
        investigation_jobs=app.state.investigation_jobs,
        inline_execution_enabled=(process_role in ("local", "worker")))
    app.state.investigation_executor = None
    app.state.investigation_dispatcher = None
    if is_postgres and agent_engine == "strands" and process_role in ("local", "worker"):
        app.state.investigation_executor = PostgreSQLStrandsWorker(
            app.state.investigation_jobs, app.state.investigation_results,
            tools=app.state.local_tools, model_factory=strands_model_factory,
            worker_id=dispatcher_identity.user_id if dispatcher_identity is not None else None,
        )
    if (persistence is not None and agent_engine == "langgraph"
            and local_provider_factory is not None):
        # Manual and automatic starts share this one durable claim/bind/attempt
        # path. Only the automatic capability receives a fixed server identity.
        app.state.investigation_executor = AutomaticInvestigationDispatcher(
            app.state.investigation_jobs,
            worker_id=dispatcher_identity.user_id if automatic_dispatcher else None,
            provider_factory=local_provider_factory, provider_label=provider_label,
            tools=app.state.local_tools, proposals=app.state.proposal_service,
            checkpointer=workflow_checkpointer,
            workflow_metadata=workflow_metadata,
            observer=app.state.observer,
        )
        if automatic_dispatcher:
            app.state.investigation_dispatcher = app.state.investigation_executor

    @app.middleware("http")
    async def authentication(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            headers = request.headers.getlist("authorization")
            if len(headers) != 1:
                return security_response(SecurityError(401))
            parts = headers[0].split(" ")
            if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
                return security_response(SecurityError(401))
            try:
                with authenticated(app.state.auth_provider, parts[1]):
                    return await call_next(request)
            except SecurityError as error:
                return security_response(error)
        return await call_next(request)

    @app.middleware("http")
    async def request_trace(request: Request, call_next):
        # Registered last: wraps authentication, including rejected requests.
        # Incoming trace headers/paths/query strings are never trusted or recorded.
        with observing(app.state.observer), span("http") as current:
            request.state.trace_id = current.trace_id
            current.annotate(method=request.method)
            try:
                response = await call_next(request)
            except Exception:
                current.annotate(status_code=500)
                raise
            current.annotate(status_code=response.status_code)
            if response.status_code >= 400:
                current.fail({401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                              409: "CONFLICT", 422: "INVALID_INPUT"}.get(response.status_code))
            response.headers["X-Trace-ID"] = current.trace_id
            if request.url.path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            return response

    def security_response(error: SecurityError):
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail},
                            headers={"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None)

    @app.exception_handler(SecurityError)
    async def security_error(request: Request, error: SecurityError):
        return security_response(error)
    app.add_api_route("/api/health", health, response_model=HealthResponse, tags=["health"])
    app.include_router(router)
    app.include_router(operation_router)
    app.include_router(proposal_router)
    app.include_router(frontend_router)
    app.include_router(catalog_router)
    app.include_router(customer_order_router)
    app.include_router(customer_case_router)

    @app.exception_handler(FrontendError)
    async def frontend_error(request: Request, error: FrontendError):
        return JSONResponse(status_code=error.status, content={"detail": error.code})

    @app.exception_handler(ConversationError)
    async def conversation_error(request: Request, error: ConversationError):
        return JSONResponse(status_code=conversation_status(error), content={"detail": error.code})

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, error: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(ConflictError)
    async def conflict(request: Request, error: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    return app


app = create_app()
