"""Strict, opt-in RDS PostgreSQL connection material handling.

The normal ``NOVAMIND_POSTGRES_DSN`` path remains available for local and
integration-test PostgreSQL. ECS deployments can instead inject one JSON
application secret through ``NOVAMIND_POSTGRES_SECRET``. This module turns
that secret into a short-lived, certificate-verified connection string without
printing or persisting credentials in the repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import ssl
import stat
from tempfile import mkstemp
from typing import Any
from urllib.parse import quote, urlunsplit


class RdsConfigurationError(ValueError):
    """A secret or TLS configuration is malformed or incomplete."""


_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")
_MAX_SECRET_BYTES = 128 * 1024
_MAX_CA_FILE_BYTES = 1024 * 1024
DEFAULT_RDS_CA_BUNDLE_PATH = "/etc/novamind/rds-ca-bundle.pem"


@dataclass(frozen=True)
class RdsApplicationSecret:
    """Validated application-only RDS connection values.

    ``repr`` deliberately omits the password and certificate to prevent an
    accidental diagnostic from placing either in task logs.
    """

    username: str
    password: str = field(repr=False)
    host: str
    port: int
    dbname: str
    schema: str
    rds_ca_pem: str = field(repr=False)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RdsConfigurationError("Duplicate secret field")
        result[key] = value
    return result


def _secret_object(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_SECRET_BYTES:
        raise RdsConfigurationError("Invalid RDS application secret")
    try:
        parsed = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise RdsConfigurationError("Invalid RDS application secret") from error
    if not isinstance(parsed, dict):
        raise RdsConfigurationError("Invalid RDS application secret")
    return parsed


def _text(value: Any, name: str, *, maximum: int = 512, allow_newlines: bool = False) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or "\x00" in value or (not allow_newlines and any(char.isspace() for char in value))):
        raise RdsConfigurationError(f"Invalid RDS application secret {name}")
    return value


def _validate_ca_bundle(value: Any) -> str:
    certificate = _text(value, "certificate", maximum=_MAX_CA_FILE_BYTES, allow_newlines=True)
    if ("-----BEGIN CERTIFICATE-----" not in certificate
            or "-----END CERTIFICATE-----" not in certificate):
        raise RdsConfigurationError("Invalid RDS application certificate")
    try:
        ssl.create_default_context(cadata=certificate)
    except ssl.SSLError as error:
        raise RdsConfigurationError("Invalid RDS application certificate") from error
    return certificate


def _trusted_ca_bundle_path(value: Any) -> str:
    """Accept only the image-provided, operator-reviewed CA file path."""

    configured = os.environ.get("NOVAMIND_RDS_CA_BUNDLE_PATH", DEFAULT_RDS_CA_BUNDLE_PATH)
    if (not isinstance(value, str) or value != configured or not Path(value).is_absolute()
            or "\x00" in value):
        raise RdsConfigurationError("Invalid RDS application certificate path")
    return value


def load_trusted_rds_ca_bundle(path: str) -> str:
    """Read and validate the separately supplied CA bundle without persisting it."""

    path = _trusted_ca_bundle_path(path)
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if (not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_size < 128 or metadata.st_size > _MAX_CA_FILE_BYTES
                or (os.name != "nt" and metadata.st_uid not in (0, os.geteuid()))
                or (os.name != "nt" and mode & 0o022)):
            raise OSError("unsafe certificate file")
        certificate = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise RdsConfigurationError("Unable to read RDS application certificate") from error
    return _validate_ca_bundle(certificate)


def parse_seed_application_secret(value: str, *, expected_role: str = "novamind_app") -> tuple[str, str, str]:
    """Parse the short-lived secret shape before the bootstrap task adds endpoint data."""

    if not _IDENTIFIER.fullmatch(expected_role):
        raise RdsConfigurationError("Invalid application role")
    secret = _secret_object(value)
    if set(secret) != {"username", "password", "rds_ca_path"}:
        raise RdsConfigurationError("Invalid RDS application secret fields")
    username = _text(secret["username"], "username", maximum=63)
    password = _text(secret["password"], "password", maximum=256)
    if username != expected_role:
        raise RdsConfigurationError("Unexpected RDS application username")
    return username, password, load_trusted_rds_ca_bundle(secret["rds_ca_path"])


def parse_application_secret(value: str, *, expected_role: str = "novamind_app") -> RdsApplicationSecret:
    """Parse the post-bootstrap ECS secret and reject undeclared/stale fields."""

    if not _IDENTIFIER.fullmatch(expected_role):
        raise RdsConfigurationError("Invalid application role")
    secret = _secret_object(value)
    expected = {"username", "password", "host", "port", "dbname", "schema", "rds_ca_path"}
    if set(secret) != expected:
        raise RdsConfigurationError("Invalid RDS application secret fields")
    username = _text(secret["username"], "username", maximum=63)
    password = _text(secret["password"], "password", maximum=256)
    host = _text(secret["host"], "host", maximum=253)
    dbname = _text(secret["dbname"], "database", maximum=63)
    schema = _text(secret["schema"], "schema", maximum=63)
    if username != expected_role or not _HOST.fullmatch(host):
        raise RdsConfigurationError("Invalid RDS application endpoint")
    if not _IDENTIFIER.fullmatch(dbname) or not _IDENTIFIER.fullmatch(schema):
        raise RdsConfigurationError("Invalid RDS application database")
    port = secret["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RdsConfigurationError("Invalid RDS application endpoint")
    return RdsApplicationSecret(
        username=username,
        password=password,
        host=host,
        port=port,
        dbname=dbname,
        schema=schema,
        rds_ca_pem=load_trusted_rds_ca_bundle(secret["rds_ca_path"]),
    )


def materialize_rds_ca_bundle(certificate: str) -> str:
    """Write a validated CA bundle into the container's memory-backed tmpfs."""

    certificate = _validate_ca_bundle(certificate)
    directory = Path("/dev/shm/novamind")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = directory.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
            raise OSError("unsafe certificate directory")
        descriptor, path = mkstemp(prefix="rds-ca-", suffix=".pem", dir=directory, text=False)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(certificate.encode("utf-8"))
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(path, 0o600)
        return path
    except (OSError, UnicodeError) as error:
        raise RdsConfigurationError("Unable to prepare RDS certificate") from error


def application_dsn(secret: RdsApplicationSecret, *, certificate_path: str) -> str:
    """Build a libpq URI with host verification and a restricted search path."""

    if not isinstance(certificate_path, str) or not certificate_path.startswith("/"):
        raise RdsConfigurationError("Invalid RDS certificate path")
    netloc = "{}:{}@{}:{}".format(
        quote(secret.username, safe=""), quote(secret.password, safe=""), secret.host, secret.port,
    )
    query = "sslmode=verify-full&sslrootcert={}&options={}".format(
        quote(certificate_path, safe=""),
        quote(f"-c search_path={secret.schema},pg_catalog", safe=""),
    )
    return urlunsplit(("postgresql", netloc, f"/{quote(secret.dbname, safe='')}", query, ""))
