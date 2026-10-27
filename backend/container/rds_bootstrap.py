"""One-shot, task-role-only RDS bootstrap for the NovaMind PostgreSQL schema.

This ECS job entrypoint reads the RDS-managed administrator secret and an
application seed secret only in memory, creates the narrowly privileged
application role/schema, applies PostgreSQL migrations as that role, then
replaces the seed secret with runtime connection material. The permanent API
task never needs the RDS master secret.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
import re
import sys
from typing import Any

from .rds_config import (
    RdsApplicationSecret,
    RdsConfigurationError,
    DEFAULT_RDS_CA_BUNDLE_PATH,
    application_dsn,
    materialize_rds_ca_bundle,
    parse_seed_application_secret,
)


class RdsBootstrapError(RuntimeError):
    """A bootstrap failure safe to report to a CloudWatch log stream."""


_ARN = re.compile(r"arn:aws(?:-[a-z-]+)?:secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@!-]+\Z")
_DATABASE = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")


@dataclass(frozen=True)
class RdsMasterSecret:
    """Validated RDS-managed administrator values; sensitive fields are hidden."""

    username: str
    password: str = field(repr=False)
    host: str
    port: int
    dbname: str


@dataclass(frozen=True)
class _SchemaProvision:
    """Privileged work that must be cleaned up after application schema creation."""

    schema_missing: bool
    revoke_database_create: bool


def _object(value: str, *, code: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise RdsBootstrapError(code) from error
    if not isinstance(result, dict):
        raise RdsBootstrapError(code)
    return result


def _master_secret(value: str, *, target_database: str, rds_endpoint: str) -> RdsMasterSecret:
    """Parse the RDS-managed master secret.

    RDS-managed secrets (via ManageMasterUserPassword) store only username and
    password at creation time.  The host comes from the trusted ``rds_endpoint``
    environment variable; port defaults to 5432 for PostgreSQL.
    """
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise RdsBootstrapError("MASTER_SECRET_INVALID_JSON") from error
    if not isinstance(parsed, dict):
        raise RdsBootstrapError("MASTER_SECRET_NOT_DICT")
    username = parsed.get("username")
    password = parsed.get("password")
    if not isinstance(username, str) or not username or "\x00" in username:
        raise RdsBootstrapError("MASTER_SECRET_INVALID_USERNAME")
    if not isinstance(password, str) or not password or "\x00" in password:
        raise RdsBootstrapError("MASTER_SECRET_INVALID_PASSWORD")
    # Use host/port from the environment when absent or when the secret only
    # stores credentials (ManageMasterUserPassword pattern).
    raw_host = parsed.get("host") or rds_endpoint
    if not isinstance(raw_host, str) or not raw_host or "\x00" in raw_host:
        raise RdsBootstrapError("MASTER_SECRET_INVALID_HOST")
    if not _HOST.fullmatch(raw_host):
        raise RdsBootstrapError("MASTER_SECRET_INVALID_HOST_FORMAT")
    raw_port = parsed.get("port", 5432)
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        raise RdsBootstrapError("MASTER_SECRET_INVALID_PORT")
    if not 1 <= port <= 65535:
        raise RdsBootstrapError("MASTER_SECRET_INVALID_PORT_RANGE")
    # Use the trusted target database from the environment.
    return RdsMasterSecret(username=username, password=password, host=raw_host, port=port, dbname=target_database)


def _required_environment(name: str, pattern: re.Pattern[str]) -> str:
    value = os.environ.get(name, "")
    if not pattern.fullmatch(value):
        raise RdsBootstrapError("BOOTSTRAP_CONFIGURATION_INVALID")
    return value


def _read_secret(client: Any, secret_arn: str, *, failure_code: str) -> str:
    try:
        response = client.get_secret_value(SecretId=secret_arn)
        value = response.get("SecretString")
    except Exception as error:
        raise RdsBootstrapError(f"{failure_code}:{type(error).__name__}") from error
    if not isinstance(value, str):
        raise RdsBootstrapError(f"{failure_code}:NO_SECRET_STRING")
    return value


@contextmanager
def _bootstrap_lock(connection: Any) -> Any:
    """Serialize the cross-connection temporary CREATE privilege window."""

    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(hashtext('novamind-rds-bootstrap-v1'))")
    try:
        yield
    finally:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(hashtext('novamind-rds-bootstrap-v1'))")


def _run_privileged_setup(connection: Any, *, database: str, role: str, schema: str,
                          password: str) -> _SchemaProvision:
    """Create the role and a temporary database CREATE grant, never SET ROLE.

    RDS can set PostgreSQL 16's ``createrole_self_grant`` to an empty value.
    A CREATEROLE principal can then create a role but has neither a membership
    row nor SET ROLE permission for it.  The application role therefore creates
    its own schema while holding only a temporary database CREATE privilege.
    """

    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                row = cursor.fetchone()
                if not row or row[0] != database:
                    raise RdsBootstrapError("DATABASE_IDENTITY_MISMATCH")
                try:
                    from psycopg import sql
                except ImportError as error:
                    raise RdsBootstrapError("POSTGRES_DRIVER_UNAVAILABLE") from error

                # Reject a schema owned by another principal before changing the
                # application role or its password.  This also keeps a failed
                # re-run from taking ownership of an unrelated schema.
                cursor.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %s", (schema,))
                owner = cursor.fetchone()
                if owner is not None and owner[0] != role:
                    raise RdsBootstrapError("APPLICATION_SCHEMA_OWNER_MISMATCH")

                # Do not assume PostgreSQL's optional CREATEROLE self-grant.
                # Some RDS configurations disable it entirely.
                cursor.execute("SELECT rolname FROM pg_roles WHERE rolname = %s", (role,))
                role_exists = cursor.fetchone() is not None
                if not role_exists:
                    cursor.execute(sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {}"
                    ).format(sql.Identifier(role), sql.Literal(password)))

                # A pre-existing role could be operator-owned.  Do not alter its
                # password without an explicit membership/ownership proof; the
                # application connection below will fail closed if its seed
                # credential is no longer valid.
                cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(role),
                ))
                if owner is None:
                    cursor.execute("SELECT has_database_privilege(%s, %s, 'CREATE')", (role, database))
                    existing_create = cursor.fetchone()
                    if not existing_create or not isinstance(existing_create[0], bool):
                        raise RdsBootstrapError("APPLICATION_DATABASE_CREATE_CHECK_FAILED")
                    if existing_create[0]:
                        # We cannot distinguish an operator grant from a
                        # stale grant left by a killed bootstrap task.  Never
                        # preserve or revoke an ambiguous broad privilege.
                        raise RdsBootstrapError("APPLICATION_DATABASE_CREATE_PREEXISTING")
                    cursor.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                        sql.Identifier(database), sql.Identifier(role),
                    ))
                    return _SchemaProvision(schema_missing=True, revoke_database_create=True)
                return _SchemaProvision(schema_missing=False, revoke_database_create=False)
    except RdsBootstrapError:
        raise
    except Exception as error:
        raise RdsBootstrapError("PRIVILEGE_SETUP_FAILED") from error


def _create_application_schema(*, dsn: str, database: str, role: str, schema: str) -> None:
    """Create the schema as its application owner, with a verified app DSN."""

    try:
        from psycopg import connect, sql
        connection = connect(dsn, connect_timeout=10, autocommit=False)
    except Exception as error:
        raise RdsBootstrapError("APPLICATION_SCHEMA_CONNECTION_FAILED") from error
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                row = cursor.fetchone()
                if not row or row[0] != database:
                    raise RdsBootstrapError("DATABASE_IDENTITY_MISMATCH")
                cursor.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %s", (schema,))
                owner = cursor.fetchone()
                if owner is not None and owner[0] != role:
                    raise RdsBootstrapError("APPLICATION_SCHEMA_OWNER_MISMATCH")
                if owner is None:
                    cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    except RdsBootstrapError:
        raise
    except Exception as error:
        raise RdsBootstrapError("APPLICATION_SCHEMA_CREATE_FAILED") from error
    finally:
        connection.close()


def _revoke_temporary_database_create(connection: Any, *, database: str, role: str) -> None:
    try:
        from psycopg import sql
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(
                    sql.Identifier(database), sql.Identifier(role),
                ))
    except Exception as error:
        raise RdsBootstrapError("APPLICATION_DATABASE_CREATE_REVOKE_FAILED") from error


def _provision_application_schema(master_connection: Any, *, application_dsn: str,
                                  database: str, role: str, schema: str, password: str) -> None:
    """Provision an app-owned schema and always clean up our temporary grant."""

    temporary_create = False
    with _bootstrap_lock(master_connection):
        try:
            provision = _run_privileged_setup(
                master_connection, database=database, role=role, schema=schema, password=password,
            )
            temporary_create = provision.revoke_database_create
            if provision.schema_missing:
                _create_application_schema(
                    dsn=application_dsn, database=database, role=role, schema=schema,
                )
        finally:
            if temporary_create:
                _revoke_temporary_database_create(master_connection, database=database, role=role)


def _master_dsn(master: RdsMasterSecret, *, certificate_path: str) -> str:
    from urllib.parse import quote, urlunsplit

    return urlunsplit((
        "postgresql",
        "{}:{}@{}:{}".format(quote(master.username, safe=""), quote(master.password, safe=""), master.host, master.port),
        f"/{quote(master.dbname, safe='')}",
        "sslmode=verify-full&sslrootcert={}".format(quote(certificate_path, safe="")),
        "",
    ))


def _updated_application_secret(*, username: str, password: str, certificate: str,
                                master: RdsMasterSecret, schema: str) -> str:
    """Serialize the fixed, reviewed permanent secret shape."""

    return json.dumps({
        "username": username,
        "password": password,
        "host": master.host,
        "port": master.port,
        "dbname": master.dbname,
        "schema": schema,
        "rds_ca_path": DEFAULT_RDS_CA_BUNDLE_PATH,
    }, separators=(",", ":"))


def bootstrap(*, client: Any, master_secret_arn: str, application_secret_arn: str,
              database: str, role: str, schema: str, rds_endpoint: str) -> None:
    """Perform one idempotent role/schema bootstrap without logging secrets."""

    if (not _DATABASE.fullmatch(database) or not _DATABASE.fullmatch(role)
            or not _DATABASE.fullmatch(schema) or not _HOST.fullmatch(rds_endpoint)):
        raise RdsBootstrapError("BOOTSTRAP_CONFIGURATION_INVALID")
    master = _master_secret(_read_secret(client, master_secret_arn, failure_code="MASTER_SECRET_READ_FAILED"),
                            target_database=database, rds_endpoint=rds_endpoint)
    try:
        username, password, certificate = parse_seed_application_secret(
            _read_secret(client, application_secret_arn, failure_code="APPLICATION_SECRET_READ_FAILED"),
            expected_role=role,
        )
        certificate_path = materialize_rds_ca_bundle(certificate)
    except RdsConfigurationError as error:
        raise RdsBootstrapError("APPLICATION_SECRET_INVALID") from error
    try:
        try:
            from psycopg import connect
        except ImportError as error:
            raise RdsBootstrapError("POSTGRES_DRIVER_UNAVAILABLE") from error
        application = RdsApplicationSecret(
            username=username, password=password, host=master.host, port=master.port,
            dbname=database, schema=schema, rds_ca_pem=certificate,
        )
        app_dsn = application_dsn(application, certificate_path=certificate_path)
        try:
            master_connection = connect(_master_dsn(master, certificate_path=certificate_path),
                                        connect_timeout=10, autocommit=False)
        except Exception as error:
            raise RdsBootstrapError("MASTER_DATABASE_CONNECTION_FAILED") from error
        try:
            _provision_application_schema(
                master_connection, application_dsn=app_dsn, database=database,
                role=role, schema=schema, password=password,
            )
        finally:
            master_connection.close()

        try:
            from backend.app.postgres_persistence import PostgreSQLPersistence
            PostgreSQLPersistence(app_dsn, initialize=True)
        except Exception as error:
            raise RdsBootstrapError("APPLICATION_MIGRATIONS_FAILED") from error
        try:
            client.put_secret_value(
                SecretId=application_secret_arn,
                SecretString=_updated_application_secret(
                    username=username, password=password, certificate=certificate,
                    master=master, schema=schema,
                ),
            )
        except Exception as error:
            raise RdsBootstrapError("APPLICATION_SECRET_WRITE_FAILED") from error
    finally:
        try:
            os.unlink(certificate_path)
        except (NameError, OSError):
            pass


def main() -> int:
    try:
        import boto3
        master_secret_arn = _required_environment("NOVAMIND_RDS_MASTER_SECRET_ARN", _ARN)
        application_secret_arn = _required_environment("NOVAMIND_RDS_APPLICATION_SECRET_ARN", _ARN)
        database = _required_environment("NOVAMIND_RDS_DATABASE", _DATABASE)
        role = _required_environment("NOVAMIND_RDS_APP_ROLE", _DATABASE)
        schema = _required_environment("NOVAMIND_RDS_APP_SCHEMA", _DATABASE)
        rds_endpoint = _required_environment("NOVAMIND_RDS_ENDPOINT", _HOST)
        bootstrap(
            client=boto3.client("secretsmanager"),
            master_secret_arn=master_secret_arn,
            application_secret_arn=application_secret_arn,
            database=database,
            role=role,
            schema=schema,
            rds_endpoint=rds_endpoint,
        )
    except RdsBootstrapError as error:
        cause_msg = str(error.__cause__) if error.__cause__ else ''
        safe_msg = re.sub(r"(?i)PASSWORD\s+'[^']*'", "PASSWORD '<redacted>'", cause_msg)
        print(f"RDS bootstrap failed: {error} | cause: {type(error.__cause__).__name__ if error.__cause__ else 'none'}: {safe_msg}", file=sys.stderr)
        return 1
    except Exception:
        print("RDS bootstrap failed: UNEXPECTED", file=sys.stderr)
        return 1
    print("RDS bootstrap completed: application role, schema, and migrations are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
