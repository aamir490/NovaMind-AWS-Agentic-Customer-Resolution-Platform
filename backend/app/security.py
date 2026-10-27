"""Replaceable authentication and authorization, outside model/workflow data."""

from contextlib import contextmanager
from contextvars import ContextVar
import base64
from collections.abc import Callable, Mapping
from enum import StrEnum
from hashlib import sha256
import json
import math
from threading import RLock
import time
from typing import Annotated, Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr, StringConstraints, model_validator

from .domain import InvestigationExecutionContext
from .service import CaseService, NotFoundError


class Role(StrEnum):
    CUSTOMER = "CUSTOMER"
    REVIEWER = "REVIEWER"
    ADMIN = "ADMIN"


class SecurityContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class AuthenticatedIdentity(SecurityContract):
    user_id: UUID
    role: Role
    customer_id: UUID | None = None

    @model_validator(mode="after")
    def customer_binding(self):
        if (self.role == Role.CUSTOMER) != (self.customer_id is not None):
            raise ValueError("Only CUSTOMER identities require a customer binding")
        return self


class LocalCredential(SecurityContract):
    token: SecretStr
    identity: AuthenticatedIdentity

    @model_validator(mode="after")
    def token_shape(self):
        token = self.token.get_secret_value()
        if not 16 <= len(token) <= 256 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("Token must be 16-256 non-whitespace ASCII characters")
        return self


class CognitoCustomerBinding(SecurityContract):
    """Trusted, durable binding from an immutable Cognito subject to a customer."""

    subject_id: UUID
    customer_id: UUID


class CognitoConfiguration(SecurityContract):
    """Non-secret Cognito access-token validation configuration."""

    issuer: str
    client_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
    customer_group: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    # Production Cognito uses only CUSTOMER and ADMIN groups. Keep this
    # optional for local/legacy reviewer fixtures without requiring a
    # reviewer group in the deployed configuration.
    reviewer_group: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)] | None = None
    admin_group: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]

    @model_validator(mode="after")
    def issuer_and_groups(self):
        value = self.issuer
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.query or parsed.fragment
                or not parsed.hostname or not parsed.path or parsed.path.endswith("/")):
            raise ValueError("Invalid Cognito issuer")
        if (not parsed.hostname.startswith("cognito-idp.")
                or not parsed.hostname.endswith((".amazonaws.com", ".amazonaws.com.cn"))):
            raise ValueError("Invalid Cognito issuer")
        user_pool = parsed.path.removeprefix("/")
        if not user_pool or "/" in user_pool or len(user_pool) > 128:
            raise ValueError("Invalid Cognito issuer")
        groups = tuple(group for group in (self.customer_group, self.reviewer_group, self.admin_group) if group is not None)
        if len(set(groups)) != len(groups):
            raise ValueError("Cognito groups must be distinct")
        return self

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"


class CognitoCustomerBindingLookup(Protocol):
    def customer_id_for_subject(self, subject_id: UUID) -> UUID | None: ...


class ReviewInput(SecurityContract):
    # Identity is deliberately not a client field; workflow/review IDs are
    # durable selectors for the server-side binding, not reviewer identity.
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    workflow_id: UUID | None = None
    review_id: UUID | None = None


class SecurityError(Exception):
    def __init__(self, status_code=403):
        self.status_code = 401 if status_code == 401 else 403
        self.code = "UNAUTHENTICATED" if self.status_code == 401 else "FORBIDDEN"
        self.detail = "Authentication required" if self.status_code == 401 else "Access denied"
        super().__init__(self.detail)


class AuthenticationProvider(Protocol):
    def authenticate(self, token: str) -> AuthenticatedIdentity | None: ...


def _json_object(raw: bytes) -> dict[str, Any]:
    """Decode a JWT/JWKS JSON object while rejecting duplicate claim names."""

    def pairs(values):
        result = {}
        for key, value in values:
            if not isinstance(key, str) or key in result:
                raise ValueError("Invalid JSON object")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("Invalid JSON constant")

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def _base64url(value: str, *, maximum: int) -> bytes:
    if (not isinstance(value, str) or not value or len(value) > maximum or "=" in value
            or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value)):
        raise ValueError("Invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class CognitoAuthenticationProvider:
    """Fail-closed Cognito access-token verifier with a bounded in-memory JWKS cache.

    The provider never accepts an ID token, never infers a role from a mutable
    display claim, and never creates a customer binding during authentication.
    A subject must already be bound by trusted application configuration.
    """

    _MAX_TOKEN_BYTES = 16 * 1024
    _JWKS_TTL_SECONDS = 300

    def __init__(self, configuration: CognitoConfiguration,
                 bindings: CognitoCustomerBindingLookup,
                 *, jwks_fetcher: Callable[[str], Mapping[str, Any]] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self.configuration = CognitoConfiguration.model_validate(configuration)
        self._bindings = bindings
        self._jwks_fetcher = jwks_fetcher if jwks_fetcher is not None else self._fetch_jwks
        self._clock = clock if clock is not None else time.time
        self._keys: dict[str, Any] = {}
        self._next_refresh = 0.0
        self._lock = RLock()

    @staticmethod
    def _fetch_jwks(url: str) -> Mapping[str, Any]:
        # Lazy import avoids a network-capable dependency at application import
        # time. The caller can inject a deterministic fetcher in tests.
        from urllib.request import Request, urlopen
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=3) as response:  # nosec B310: URL is derived from validated issuer
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("JWKS too large")
        return _json_object(raw)

    @staticmethod
    def _jwk_public_key(jwk: Mapping[str, Any], expected_kid: str):
        try:
            if (jwk.get("kty") != "RSA" or jwk.get("kid") != expected_kid
                    or jwk.get("use", "sig") != "sig" or jwk.get("alg", "RS256") != "RS256"):
                raise ValueError("Unsupported JWK")
            modulus = int.from_bytes(_base64url(jwk["n"], maximum=4096), "big")
            exponent = int.from_bytes(_base64url(jwk["e"], maximum=16), "big")
            if modulus.bit_length() < 2048 or modulus.bit_length() > 8192 or exponent < 3 or exponent % 2 == 0:
                raise ValueError("Invalid RSA key")
            from cryptography.hazmat.primitives.asymmetric import rsa
            return rsa.RSAPublicNumbers(exponent, modulus).public_key()
        except Exception:
            raise ValueError("Invalid JWK") from None

    def _refresh_keys(self, *, force: bool = False) -> None:
        now = self._clock()
        with self._lock:
            if not force and now < self._next_refresh:
                return
            document = self._jwks_fetcher(self.configuration.jwks_url)
            if not isinstance(document, Mapping) or set(document) - {"keys"} or not isinstance(document.get("keys"), list):
                raise ValueError("Invalid JWKS")
            keys = {}
            for raw in document["keys"]:
                if not isinstance(raw, Mapping):
                    raise ValueError("Invalid JWKS")
                kid = raw.get("kid")
                if not isinstance(kid, str) or not kid or len(kid) > 128 or kid in keys:
                    raise ValueError("Invalid JWKS")
                keys[kid] = self._jwk_public_key(raw, kid)
            if not keys:
                raise ValueError("Invalid JWKS")
            self._keys = keys
            self._next_refresh = now + self._JWKS_TTL_SECONDS

    def _key(self, kid: str):
        self._refresh_keys()
        with self._lock:
            key = self._keys.get(kid)
        if key is not None:
            return key
        # Cognito rotates keys. A kid miss may be a rotation, but failure to
        # refresh or a second miss remains unauthenticated rather than stale.
        self._refresh_keys(force=True)
        with self._lock:
            key = self._keys.get(kid)
        if key is None:
            raise ValueError("Unknown key")
        return key

    def _claims(self, token: str) -> tuple[dict[str, Any], bytes, bytes]:
        if (not isinstance(token, str) or len(token) > self._MAX_TOKEN_BYTES
                or any(character.isspace() for character in token)):
            raise ValueError("Invalid token")
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid token")
        header_raw = _base64url(parts[0], maximum=2048)
        payload_raw = _base64url(parts[1], maximum=self._MAX_TOKEN_BYTES)
        signature = _base64url(parts[2], maximum=8192)
        header = _json_object(header_raw)
        claims = _json_object(payload_raw)
        if header.get("alg") != "RS256" or header.get("typ", "JWT") != "JWT":
            raise ValueError("Unsupported JWT")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 128:
            raise ValueError("Invalid JWT")
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            self._key(kid).verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError, TypeError):
            raise ValueError("Invalid JWT signature") from None
        return claims, header_raw, payload_raw

    def authenticate(self, token: str) -> AuthenticatedIdentity | None:
        try:
            claims, _, _ = self._claims(token)
            now = self._clock()
            expiry = claims.get("exp")
            not_before = claims.get("nbf")
            if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                    or not math.isfinite(expiry) or expiry <= now
                    or (not_before is not None and (isinstance(not_before, bool)
                        or not isinstance(not_before, (int, float))
                        or not math.isfinite(not_before) or not_before > now))):
                raise ValueError("Token time validation failed")
            if (claims.get("iss") != self.configuration.issuer or claims.get("token_use") != "access"
                    or claims.get("client_id") != self.configuration.client_id):
                raise ValueError("Token claims validation failed")
            subject = UUID(claims.get("sub"))
            if str(subject) != claims.get("sub"):
                raise ValueError("Invalid subject")
            groups = claims.get("cognito:groups")
            if not isinstance(groups, list) or not groups or any(
                not isinstance(group, str) or not group or len(group) > 128 for group in groups
            ):
                raise ValueError("Invalid groups")
            group_to_role = {
                self.configuration.customer_group: Role.CUSTOMER,
                self.configuration.admin_group: Role.ADMIN,
            }
            if self.configuration.reviewer_group is not None:
                group_to_role[self.configuration.reviewer_group] = Role.REVIEWER
            resolved = {group_to_role[group] for group in groups if group in group_to_role}
            if len(groups) != 1 or len(resolved) != 1:
                raise ValueError("Unmapped or ambiguous groups")
            role = resolved.pop()
            customer_id = self._bindings.customer_id_for_subject(subject) if role == Role.CUSTOMER else None
            if role == Role.CUSTOMER and customer_id is None:
                raise ValueError("No customer binding")
            return AuthenticatedIdentity(user_id=subject, role=role, customer_id=customer_id)
        except Exception:
            return None


class LocalAuthenticationProvider:
    """An explicit server-side token registry. Empty by default; no public login."""

    def __init__(self, credentials: tuple[LocalCredential, ...] = ()):
        self._identities = {}
        subjects = {}
        for raw in credentials:
            credential = LocalCredential.model_validate(raw)
            identity = credential.identity
            digest = sha256(credential.token.get_secret_value().encode("ascii")).digest()
            if digest in self._identities or (identity.user_id in subjects and subjects[identity.user_id] != identity):
                raise ValueError("Duplicate token or conflicting identity")
            self._identities[digest] = identity
            subjects[identity.user_id] = identity
        self._subjects = subjects

    def authenticate(self, token: str) -> AuthenticatedIdentity | None:
        if not isinstance(token, str) or not 16 <= len(token) <= 256 or not token.isascii():
            return None
        return self._identities.get(sha256(token.encode("ascii")).digest())

    def identity_for_user_id(self, user_id: UUID) -> AuthenticatedIdentity | None:
        """Trusted bootstrap lookup; it never accepts a request credential."""
        if not isinstance(user_id, UUID):
            return None
        identity = self._subjects.get(user_id)
        return (None if identity is None
                else AuthenticatedIdentity.model_validate(identity))


_identity: ContextVar[AuthenticatedIdentity | None] = ContextVar("authenticated_identity", default=None)
_investigation_execution: ContextVar[InvestigationExecutionContext | None] = ContextVar(
    "investigation_execution", default=None,
)


@contextmanager
def authenticated(provider: AuthenticationProvider, token: str):
    """Trusted host entry point; never take a provider/identity from a model or request body."""
    try:
        identity = AuthenticatedIdentity.model_validate(provider.authenticate(token))
    except Exception:
        raise SecurityError(401) from None
    context_token = _identity.set(identity)
    try:
        yield identity
    finally:
        _identity.reset(context_token)


def current_identity() -> AuthenticatedIdentity:
    identity = _identity.get()
    if identity is None:
        raise SecurityError(401)
    return AuthenticatedIdentity.model_validate(identity)


def current_investigation_execution() -> InvestigationExecutionContext | None:
    """Return the active trusted job scope, if this is a server execution."""
    context = _investigation_execution.get()
    return None if context is None else InvestigationExecutionContext.model_validate(context)


@contextmanager
def trusted_investigation_execution(context: InvestigationExecutionContext):
    """Enter a server-only LangGraph tool scope for one marked job attempt.

    This cannot be combined with an HTTP-authenticated identity.  The caller
    must obtain ``context`` from the investigation-job repository after it has
    verified persisted claim, reservation, descriptor, and attempt records.
    """
    if _identity.get() is not None or _investigation_execution.get() is not None:
        raise RuntimeError("Investigation execution cannot be nested in a request identity")
    context = InvestigationExecutionContext.model_validate(context)
    scope_token = _investigation_execution.set(context)
    identity_token = _identity.set(AuthenticatedIdentity(
        user_id=context.worker_id, role=Role.REVIEWER,
    ))
    try:
        yield context
    finally:
        _identity.reset(identity_token)
        _investigation_execution.reset(scope_token)


def require_roles(*roles: Role) -> AuthenticatedIdentity:
    identity = current_identity()
    if identity.role not in roles:
        raise SecurityError()
    return identity


class Authorization:
    """Selectors are untrusted. Ownership always comes from live domain records."""

    def __init__(self, cases: CaseService, proposals=None):
        self._cases = cases
        self._proposals = proposals

    @staticmethod
    def customer(customer_id: UUID):
        execution = current_investigation_execution()
        if execution is not None and execution.customer_id != customer_id:
            raise SecurityError()
        identity = current_identity()
        if identity.role == Role.CUSTOMER and identity.customer_id != customer_id:
            raise SecurityError()

    def _owned(self, lookup, resource_id):
        identity = current_identity()
        try:
            record = lookup(resource_id)
        except NotFoundError:
            # Do not distinguish unknown IDs from another customer's IDs.
            if identity.role == Role.CUSTOMER or current_investigation_execution() is not None:
                raise SecurityError() from None
            raise
        self.customer(record.customer_id)
        return record

    def order(self, order_id: UUID):
        record = self._owned(self._cases.get_order, order_id)
        execution = current_investigation_execution()
        if execution is not None and record.id != execution.order_id:
            raise SecurityError()
        return record

    def case(self, case_id: UUID):
        record = self._owned(self._cases.get_case, case_id)
        execution = current_investigation_execution()
        if execution is not None and (
            record.id != execution.case_id or record.order_id != execution.order_id
        ):
            raise SecurityError()
        return record

    def proposal(self, proposal_id: UUID):
        identity = current_identity()
        try:
            record = self._proposals.get(proposal_id)
        except NotFoundError:
            if identity.role == Role.CUSTOMER or current_investigation_execution() is not None:
                raise SecurityError() from None
            raise
        self.case(record.case_id)
        return record

    def tool(self, name, request):
        current_identity()
        if name == "get_customer":
            self.customer(request.customer_id)
        elif name in ("get_case", "create_resolution_proposal"):
            self.case(request.case_id)
        elif name == "get_order":
            self.order(request.order_id)
        elif name == "assess_eligibility":
            self.order(request.order_id)
            if request.case_id is not None:
                self.case(request.case_id)
            self.customer(request.customer_id)
        elif name == "assess_refund_eligibility":
            self.case(request.case_id)
        elif name == "get_proposal_status":
            self.proposal(request.proposal_id)
