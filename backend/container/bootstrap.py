"""Trusted runtime configuration only. No provisioning, seeding, or model calls."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from backend.app.bedrock import BedrockProvider
from backend.app.domain import Customer
from backend.app.llm import FakeLLMProvider, LLMResponse
from backend.app.main import create_app
from backend.app.operations import AuthoritativeDeliveryFact, TrustedDeliverySource
from backend.app.schemas import CaseCreate, OrderCreate
from backend.app.security import (
    AuthenticatedIdentity, CognitoConfiguration, CognitoCustomerBinding,
    LocalAuthenticationProvider, LocalCredential, Role,
)
from .rds_config import (
    RdsConfigurationError, application_dsn, materialize_rds_ca_bundle, parse_application_secret,
)


class _OfflineScriptedProvider:
    """Explicit local-test provider; contains no transport or credential support."""

    def __init__(self):
        self._fixture = FakeLLMProvider(LLMResponse(
            text='{"decision":{"kind":"final"}}', finish_reason="stop",
        ))

    def generate(self, request, *, response_schema):
        return self._fixture.generate(request, response_schema=response_schema)

    def close(self):
        return None


_OFFLINE_DEMO_SOURCE = TrustedDeliverySource(id="offline-demo-fulfillment")
_BEDROCK_DEMO_SOURCE = TrustedDeliverySource(id="bedrock-demo-fulfillment")
_STRANDS_NOVA_LITE_MODEL_IDS = frozenset({
    "amazon.nova-lite-v1:0",
    "us.amazon.nova-lite-v1:0",
    "eu.amazon.nova-lite-v1:0",
    "apac.amazon.nova-lite-v1:0",
})


def _process_role() -> str:
    """Select the ECS process role without accepting an ambiguous fallback."""
    value = os.environ.get("NOVAMIND_PROCESS_ROLE")
    if value is None:
        # Preserve local/offline test composition when no durable cloud store
        # is configured; a durable container defaults to API-only mode.
        return "local" if os.environ.get("NOVAMIND_POSTGRES_DSN") is None \
            and os.environ.get("NOVAMIND_POSTGRES_SECRET") is None else "api"
    if value not in ("api", "publisher", "worker", "local"):
        raise RuntimeError("Container process role is invalid")
    return value


def _offline_demo_enabled():
    value = os.environ.get("NOVAMIND_OFFLINE_DEMO_DATA")
    if value is None:
        return False
    if (value != "true" or os.environ.get("NOVAMIND_LLM_PROVIDER") != "local_scripted"
            or os.environ.get("NOVAMIND_OFFLINE_TESTING") != "true"):
        raise RuntimeError("Container offline demo configuration is invalid")
    return True


def _bedrock_demo_enabled():
    value = os.environ.get("NOVAMIND_BEDROCK_DEMO_DATA")
    if value is None:
        return False
    if (value != "true" or os.environ.get("NOVAMIND_LLM_PROVIDER") != "bedrock"
            or os.environ.get("NOVAMIND_OFFLINE_TESTING") is not None):
        raise RuntimeError("Container Bedrock demo configuration is invalid")
    return True


def _seed_demo(app, customers, *, source, sku, product_name, subject):
    """Seed one clearly labelled, eligible test case through trusted-host boundaries."""
    unique = {customer.id: customer for customer in customers}
    if len(unique) != 1:
        raise RuntimeError("Container offline demo requires exactly one customer identity")
    customer = next(iter(unique.values()))
    service = app.state.case_service
    order = service.create_order(OrderCreate(
        customer_id=customer.id,
        items=[{"sku": sku, "name": product_name, "quantity": 1}],
    ))
    case = service.create_case(CaseCreate(
        customer_id=customer.id, order_id=order.id,
        subject=subject,
        description=("Synthetic local-only case for demonstrating verified eligibility and "
                     "human review. No production delivery record or business action."),
    ))
    now = datetime.now(timezone.utc)
    app.state.delivery_fact_ingestion.ingest(
        source.id,
        AuthoritativeDeliveryFact(
            id=uuid4(), case_id=case.id, order_id=order.id, customer_id=customer.id,
            sku=sku, quantity=1, delivered_at=now - timedelta(days=5),
            reason="wrong_item", condition="unused", source_id=source.id,
            source_reference=f"synthetic-{case.id}", verified_at=now,
        ),
    )


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration key")
        result[key] = value
    return result


def _authentication_provider():
    if os.environ.get("NOVAMIND_AUTH_MODE") == "cognito":
        # Do not open a local credential file in Cognito mode.  Mixed modes
        # would make an operator mistake silently expose a second authenticator.
        if os.environ.get("NOVAMIND_AUTH_FILE"):
            raise RuntimeError("Container authentication configuration is invalid")
        return None, ()
    location = os.environ.get("NOVAMIND_AUTH_FILE", "")
    if not location:
        return None, ()
    try:
        path = Path(location)
        if not path.is_absolute():
            raise ValueError("Configuration path must be absolute")
        with path.open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError("Configuration is too large")
        records = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(records, list) or len(records) > 1000:
            raise ValueError("Invalid credential list")
        credentials = tuple(LocalCredential.model_validate(item) for item in records)
        provider = LocalAuthenticationProvider(credentials)
        # For every CUSTOMER credential, seed a deterministic Customer domain
        # record using the exact customer_id declared in the identity.
        # The name is a generic demo label; customer names are not part of
        # authentication configuration and must not be added to credentials.
        # A seeded record does NOT grant authentication — the bearer token is
        # still required for every request.
        startup_customers = tuple(
            Customer(id=credential.identity.customer_id, name="Demo Customer")
            for credential in credentials
            if credential.identity.role == Role.CUSTOMER
        )
    except Exception:
        # Never put credentials, file paths, or Pydantic input excerpts in logs.
        raise RuntimeError("Container authentication configuration is invalid") from None
    return provider, startup_customers


def _cognito_authentication_configuration():
    """Read the opt-in, non-secret Cognito configuration without AWS calls."""
    mode = os.environ.get("NOVAMIND_AUTH_MODE")
    names = (
        "NOVAMIND_COGNITO_ISSUER", "NOVAMIND_COGNITO_CLIENT_ID",
        "NOVAMIND_COGNITO_CUSTOMER_GROUP",
        "NOVAMIND_COGNITO_REVIEWER_GROUP", "NOVAMIND_COGNITO_ADMIN_GROUP",
        "NOVAMIND_COGNITO_CUSTOMER_BINDINGS",
    )
    has_cognito_values = any(name in os.environ for name in names)
    if mode is None or mode == "local":
        if has_cognito_values:
            raise RuntimeError("Container authentication configuration is invalid")
        return None, ()
    if mode != "cognito" or os.environ.get("NOVAMIND_AUTH_FILE"):
        raise RuntimeError("Container authentication configuration is invalid")
    try:
        configuration = CognitoConfiguration(
            issuer=os.environ["NOVAMIND_COGNITO_ISSUER"],
            client_id=os.environ["NOVAMIND_COGNITO_CLIENT_ID"],
            customer_group=os.environ["NOVAMIND_COGNITO_CUSTOMER_GROUP"],
            reviewer_group=os.environ.get("NOVAMIND_COGNITO_REVIEWER_GROUP"),
            admin_group=os.environ["NOVAMIND_COGNITO_ADMIN_GROUP"],
        )
        raw_bindings = os.environ.get("NOVAMIND_COGNITO_CUSTOMER_BINDINGS", "[]")
        if len(raw_bindings) > 65536:
            raise ValueError("Configuration is too large")
        records = json.loads(raw_bindings, object_pairs_hook=_unique)
        if not isinstance(records, list) or len(records) > 1000:
            raise ValueError("Invalid Cognito customer bindings")
        bindings = tuple(CognitoCustomerBinding.model_validate(record) for record in records)
    except Exception:
        raise RuntimeError("Container authentication configuration is invalid") from None
    return configuration, bindings


def _sqlite_database_path():
    """Read an optional, absolute host-managed SQLite location."""
    location = os.environ.get("NOVAMIND_SQLITE_PATH")
    if location is None:
        return None
    try:
        path = Path(location)
        if not location or not path.is_absolute() or (path.exists() and not path.is_file()):
            raise ValueError("Invalid SQLite path")
    except Exception:
        raise RuntimeError("Container SQLite persistence configuration is invalid") from None
    return path


def _postgres_dsn():
    """Read local DSN or the strict, ECS-injected RDS application secret."""
    value = os.environ.get("NOVAMIND_POSTGRES_DSN")
    application_secret = os.environ.get("NOVAMIND_POSTGRES_SECRET")
    if value is None and application_secret is None:
        return None
    if value is not None and application_secret is not None:
        raise RuntimeError("Container PostgreSQL persistence configuration is invalid")
    if application_secret is not None:
        try:
            secret = parse_application_secret(application_secret)
            certificate_path = materialize_rds_ca_bundle(secret.rds_ca_pem)
            return application_dsn(secret, certificate_path=certificate_path)
        except RdsConfigurationError:
            raise RuntimeError("Container PostgreSQL persistence configuration is invalid") from None
    try:
        parsed = urlsplit(value)
        if (not value or len(value) > 4096 or any(character.isspace() for character in value)
                or parsed.scheme not in ("postgres", "postgresql")
                or not parsed.hostname or not parsed.path.strip("/")):
            raise ValueError("Invalid PostgreSQL DSN")
    except Exception:
        raise RuntimeError("Container PostgreSQL persistence configuration is invalid") from None
    return value


def _automatic_dispatcher_configuration(
        auth_provider: LocalAuthenticationProvider | None,
        *, cognito_mode: bool = False,
) -> tuple[bool, AuthenticatedIdentity | None]:
    """Resolve the opt-in dispatcher worker from the server credential registry.

    Environment values select only an already-configured staff subject; they
    cannot create an identity, read a bearer token, or select a customer.
    Missing configuration remains disabled.  Any partial or malformed opt-in
    configuration stops startup rather than silently changing execution mode.
    """
    enabled = os.environ.get("NOVAMIND_AUTOMATIC_DISPATCHER")
    worker_value = os.environ.get("NOVAMIND_DISPATCHER_USER_ID")
    worker_role = os.environ.get("NOVAMIND_DISPATCHER_ROLE")
    if enabled is None:
        if worker_value is not None or worker_role is not None:
            raise RuntimeError("Container automatic dispatcher configuration is invalid")
        return False, None
    if enabled == "false":
        if worker_value is not None or worker_role is not None:
            raise RuntimeError("Container automatic dispatcher configuration is invalid")
        return False, None
    if enabled != "true" or not worker_value:
        raise RuntimeError("Container automatic dispatcher configuration is invalid")
    try:
        worker_id = UUID(worker_value)
        if str(worker_id) != worker_value:
            raise ValueError("Non-canonical worker ID")
        if cognito_mode:
            # This is a task-definition-only service identity, never a caller
            # identity or a Cognito group/claim.  Its UUID makes automated
            # investigation audit entries attributable without letting a
            # browser nominate a worker or authorise a review.
            if auth_provider is not None or worker_role not in (Role.REVIEWER.value, Role.ADMIN.value):
                raise ValueError("Dispatcher identity is not staff")
            identity = AuthenticatedIdentity(user_id=worker_id, role=Role(worker_role))
        else:
            if worker_role is not None or auth_provider is None:
                raise ValueError("Dispatcher identity is not staff")
            identity = auth_provider.identity_for_user_id(worker_id)
            if identity is None or identity.role not in (Role.REVIEWER, Role.ADMIN):
                raise ValueError("Dispatcher identity is not staff")
    except Exception:
        raise RuntimeError("Container automatic dispatcher configuration is invalid") from None
    return True, identity


def _llm_provider():
    # Reject retired configuration under every selector; never ignore a stale key.
    if any(name.startswith("GEMINI_") for name in os.environ):
        raise RuntimeError("Container LLM provider selection is invalid")
    selected = os.environ.get("NOVAMIND_LLM_PROVIDER")
    offline = os.environ.get("NOVAMIND_OFFLINE_TESTING")
    if selected is None:
        if offline is not None:
            raise RuntimeError("Container LLM provider selection is invalid")
        return None
    if selected == "local_scripted":
        cloud_configuration = any(
            name.startswith(("AWS_", "BEDROCK_"))
            for name in os.environ
        )
        if offline != "true" or cloud_configuration:
            raise RuntimeError("Container offline provider selection is invalid")
        return _OfflineScriptedProvider()
    if selected == "bedrock":
        if offline is not None:
            raise RuntimeError("Container Bedrock configuration is invalid")
        try:
            region = os.environ.get("AWS_REGION", "")
            model_id = os.environ.get("BEDROCK_MODEL_ID", "")
            # Validate syntax locally; model availability and permissions are
            # determined by AWS only when a request is made.
            if not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+){1,3}-[0-9]{1,2}", region):
                raise ValueError("Invalid Bedrock region")
            identifier = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}"
            arn = (r"arn:aws(?:-us-gov|-cn)?:bedrock:[a-z0-9-]{1,32}:"
                   r"(?:[0-9]{12})?:(?:foundation-model|inference-profile|"
                   r"application-inference-profile)/" + identifier)
            if not ((not model_id.startswith("arn:") and re.fullmatch(identifier, model_id))
                    or re.fullmatch(arn, model_id)):
                raise ValueError("Invalid Bedrock model ID")
            return BedrockProvider(model_id=model_id, region_name=region)
        except Exception:
            raise RuntimeError("Container Bedrock configuration is invalid") from None
    raise RuntimeError("Container LLM provider selection is invalid")


def _agent_engine():
    """Read and validate the server-only NOVAMIND_AGENT_ENGINE selector."""
    engine = os.environ.get("NOVAMIND_AGENT_ENGINE", "langgraph")
    if engine not in ("langgraph", "strands"):
        raise RuntimeError("Container agent engine selection is invalid")
    if engine == "strands" and os.environ.get("NOVAMIND_LLM_PROVIDER") != "bedrock":
        raise RuntimeError("Container Strands agent engine requires Bedrock provider")
    if engine == "strands":
        _strands_model_id()
    return engine


def _strands_model_id():
    """Return the only Bedrock model IDs approved for the Strands agent."""
    model_id = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    if model_id not in _STRANDS_NOVA_LITE_MODEL_IDS:
        raise RuntimeError("Container Strands model selection is invalid")
    return model_id


def _strands_model_factory():
    """Create a Strands BedrockModel for the investigation agent.

    Uses the same AWS_REGION and BEDROCK_MODEL_ID as the Bedrock provider.
    The Strands SDK is imported lazily so the container starts without it installed.
    """
    from strands.models import BedrockModel
    return BedrockModel(
        model_id=_strands_model_id(),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def create_container_app():
    """Absent configuration stays default-deny; invalid configuration stops startup."""
    process_role = _process_role()
    if process_role in ("api", "publisher", "worker"):
        # Durable ECS processes never use the local/test runtime or the
        # LangGraph dispatcher.  The API is Cognito-authenticated; publisher
        # and worker are server processes with no HTTP authentication.
        if os.environ.get("NOVAMIND_AUTH_FILE"):
            raise RuntimeError("Container durable process authentication configuration is invalid")
        if process_role == "api":
            if os.environ.get("NOVAMIND_AUTH_MODE") != "cognito":
                raise RuntimeError("Container API authentication configuration is invalid")
        elif os.environ.get("NOVAMIND_AUTH_MODE") is not None:
            raise RuntimeError("Container server process authentication configuration is invalid")
    auth_provider, startup_customers = _authentication_provider()
    if process_role == "worker":
        # The worker has no HTTP surface and must not require Cognito secrets.
        # Its identity is generated by sqs_runtime and passed to the fenced
        # PostgreSQL worker; the empty local provider is never used for a
        # request.
        cognito_configuration, cognito_customer_bindings = None, ()
    else:
        cognito_configuration, cognito_customer_bindings = _cognito_authentication_configuration()
    automatic_dispatcher, dispatcher_identity = _automatic_dispatcher_configuration(
        auth_provider, cognito_mode=cognito_configuration is not None,
    )
    sqlite_path = _sqlite_database_path()
    postgres_dsn = _postgres_dsn()
    if sqlite_path is not None and postgres_dsn is not None:
        raise RuntimeError("Container persistence configuration is invalid")
    offline_demo = _offline_demo_enabled()
    bedrock_demo = _bedrock_demo_enabled()
    provider = _llm_provider()
    agent_engine = _agent_engine()
    if process_role in ("api", "publisher", "worker") and (
            postgres_dsn is None or agent_engine != "strands"):
        raise RuntimeError("Container durable process requires PostgreSQL and Strands")
    demo_sources = tuple(source for enabled, source in (
        (offline_demo, _OFFLINE_DEMO_SOURCE), (bedrock_demo, _BEDROCK_DEMO_SOURCE),
    ) if enabled)
    try:
        app = create_app(auth_provider=auth_provider,
                         local_provider_factory=(lambda: provider) if provider is not None else None,
                         provider_label=os.environ["NOVAMIND_LLM_PROVIDER"] if provider is not None
                                        else "local_scripted",
                         agent_engine=agent_engine,
                         strands_model_factory=_strands_model_factory if agent_engine == "strands" else None,
                         sqlite_path=sqlite_path,
                         postgres_dsn=postgres_dsn,
                         startup_customers=startup_customers,
                         cognito_configuration=cognito_configuration,
                         cognito_customer_bindings=cognito_customer_bindings,
                         trusted_delivery_sources=demo_sources,
                         automatic_dispatcher=automatic_dispatcher,
                         dispatcher_identity=dispatcher_identity,
                         process_role=process_role)
        if offline_demo:
            _seed_demo(app, startup_customers, source=_OFFLINE_DEMO_SOURCE,
                       sku="LAP-1", product_name="ProBook Laptop 15",
                       subject="[SYNTHETIC OFFLINE DEMO] Eligible refund review")
        if bedrock_demo:
            _seed_demo(app, startup_customers, source=_BEDROCK_DEMO_SOURCE,
                       sku="LAP-10", product_name="NovaBook Laptop 10",
                       subject="[SYNTHETIC BEDROCK DEMO] Eligible LAP-10 refund review")
    except Exception:
        if provider is not None:
            provider.close()
        raise
    if provider is not None:
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            try:
                async with original_lifespan(app):
                    yield
            finally:
                # The existing lifespan drains run workers before closing their client.
                provider.close()

        app.router.lifespan_context = lifespan
    return app
