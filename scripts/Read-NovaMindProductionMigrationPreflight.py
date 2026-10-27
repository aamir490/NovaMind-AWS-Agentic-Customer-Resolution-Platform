"""Read-only production PostgreSQL migration/ownership preflight.

This module is intended for a one-shot diagnostic command override. It uses
the already injected application secret, never prints connection material or
row data, and emits only a bounded status contract.
"""

from __future__ import annotations

import json
import os

from backend.container.rds_config import (
    application_dsn,
    DEFAULT_RDS_CA_BUNDLE_PATH,
    load_trusted_rds_ca_bundle,
    materialize_rds_ca_bundle,
    parse_application_secret,
)
from backend.app.postgres_persistence import POSTGRES_MIGRATIONS


REQUIRED_TABLES = (
    "customers", "orders", "order_items", "support_cases", "trusted_delivery_sources",
    "delivery_facts", "eligibility_evidence", "resolution_proposals", "workflow_runs",
    "workflow_review_bindings", "workflow_audit_events", "workflow_run_events",
    "langgraph_checkpoints", "langgraph_checkpoint_blobs", "langgraph_checkpoint_writes",
    "langgraph_checkpoint_staged_writes", "case_resolution_decisions",
    "case_customer_communications", "investigation_jobs", "investigation_control",
    "investigation_mode_audit", "investigation_start_requests", "cognito_customer_bindings",
    "investigation_results", "investigation_outbox",
    "schema_migrations",
)

# Migration 10 replaces this trigger function, so the application role must
# own it. Do not broaden this to unrelated operator or extension functions.
REQUIRED_FUNCTIONS: tuple[str, ...] = ("novamind_workflow_guard",)

EXPECTED_SCHEMA_VERSIONS = tuple(migration.version for migration in POSTGRES_MIGRATIONS)
REQUIRED_SCHEMA_VERSION = max(EXPECTED_SCHEMA_VERSIONS)

_TABLE_VALUES = ", ".join(f"('{name}')" for name in REQUIRED_TABLES)
_FUNCTION_VALUES = ", ".join("('{}')".format(name) for name in REQUIRED_FUNCTIONS)
_FUNCTION_CTE = f"VALUES {_FUNCTION_VALUES}" if _FUNCTION_VALUES else "SELECT NULL::text WHERE FALSE"

SQL = f"""
WITH required_tables(name) AS (VALUES {_TABLE_VALUES}),
table_owners AS (
    SELECT r.name, c.relname, n.nspname AS schema_name,
           pg_get_userbyid(c.relowner) AS owner_name
    FROM required_tables AS r
    LEFT JOIN pg_class AS c ON c.relname = r.name AND c.relkind IN ('r', 'p')
    LEFT JOIN pg_namespace AS n ON n.oid = c.relnamespace AND n.nspname = 'novamind'
),
required_functions(name) AS ({_FUNCTION_CTE}),
function_owners AS (
    SELECT r.name, p.proname, pg_get_userbyid(p.proowner) AS owner_name
    FROM required_functions AS r
    LEFT JOIN pg_proc AS p ON p.proname = r.name
    LEFT JOIN pg_namespace AS n ON n.oid = p.pronamespace AND n.nspname = 'novamind'
    WHERE p.oid IS NULL OR n.nspname = 'novamind'
), ledger AS (
    SELECT version FROM novamind.schema_migrations
)
SELECT
    current_database() AS database_name,
    current_schema() AS session_schema,
    current_user AS session_role,
    session_user AS login_role,
    COALESCE((SELECT pg_get_userbyid(nspowner) = 'novamind_app'
              FROM pg_namespace WHERE nspname = 'novamind'), FALSE) AS schema_owned,
    has_database_privilege(current_user, current_database(), 'CONNECT') AS connect_privilege,
    has_schema_privilege(current_user, 'novamind', 'USAGE') AS usage_privilege,
    has_schema_privilege(current_user, 'novamind', 'CREATE') AS create_privilege,
    COALESCE((SELECT bool_and(relname IS NOT NULL AND schema_name = 'novamind'
              AND owner_name = 'novamind_app')
              FROM table_owners), FALSE) AS required_tables_owned,
    (SELECT count(*) FROM required_tables) AS required_table_count,
    COALESCE((SELECT bool_and(proname IS NOT NULL AND owner_name = 'novamind_app')
              FROM function_owners), TRUE) AS required_functions_owned,
    (SELECT count(*) FROM required_functions WHERE name IS NOT NULL) AS required_function_count,
    (SELECT count(*) FROM ledger) AS migration_count,
    (SELECT max(version) FROM ledger) AS current_version,
    COALESCE((SELECT bool_and(version BETWEEN 1 AND {REQUIRED_SCHEMA_VERSION}) FROM ledger), FALSE) AS ledger_versions_bounded,
    COALESCE((SELECT count(*) = max(version) FROM ledger), FALSE) AS ledger_contiguous
"""

ALLOWED_VERSIONS = {REQUIRED_SCHEMA_VERSION}


def _emit(**values: object) -> None:
    print(json.dumps(values, sort_keys=True, separators=(",", ":")))


def run() -> int:
    raw_secret = os.environ.get("NOVAMIND_POSTGRES_SECRET")
    if not raw_secret:
        _emit(status="FAIL", phase="SECRET_ENVIRONMENT_MISSING")
        return 1
    try:
        secret = parse_application_secret(raw_secret)
    except Exception:
        _emit(status="FAIL", phase="SECRET_PARSE_INVALID")
        return 1

    certificate_path = None
    connection = None
    try:
        # The secret carries only the fixed trusted-file path.  Load that
        # separately; never treat a removed legacy PEM secret field as
        # connection input.
        certificate_path = materialize_rds_ca_bundle(
            load_trusted_rds_ca_bundle(
                os.environ.get("NOVAMIND_RDS_CA_BUNDLE_PATH", DEFAULT_RDS_CA_BUNDLE_PATH)
            )
        )
        dsn = application_dsn(secret, certificate_path=certificate_path)
        import psycopg  # Imported only inside the diagnostic path.

        connection = psycopg.connect(dsn, connect_timeout=10)
        connection.read_only = True
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(SQL)
                row = cursor.fetchone()
                description = cursor.description
        if not row or description is None:
            _emit(status="FAIL", phase="RESULT_VALIDATION", checks=None)
            return 1
        try:
            columns = tuple(column.name for column in description)
            if len(columns) != len(row) or len(set(columns)) != len(columns):
                raise ValueError("invalid diagnostic result shape")
            result = dict(zip(columns, row))
        except Exception:
            _emit(status="FAIL", phase="RESULT_VALIDATION", checks=None)
            return 1
    except Exception:
        _emit(status="FAIL", phase="DATABASE_CONNECTION")
        return 1
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if certificate_path:
            try:
                os.unlink(certificate_path)
            except OSError:
                pass

    required = {
        "database_identity": result.get("database_name") == "novamindcrp",
        "login_role": result.get("session_role") == "novamind_app"
        and result.get("login_role") == "novamind_app",
        "schema_owner": result.get("schema_owned") is True,
        "connect_privilege": result.get("connect_privilege") is True,
        "usage_privilege": result.get("usage_privilege") is True,
        "create_privilege": result.get("create_privilege") is True,
        "required_tables_owned": result.get("required_tables_owned") is True,
        "required_functions_owned": result.get("required_functions_owned") is True,
        "migration_ledger_contiguous": result.get("ledger_contiguous") is True
        and result.get("ledger_versions_bounded") is True,
        "migration_version_allowed": isinstance(result.get("current_version"), int)
        and result.get("current_version") in ALLOWED_VERSIONS,
        "session_schema": result.get("session_schema") == "novamind",
    }
    safe = all(required.values())
    if not safe:
        _emit(
            status="FAIL", phase="RESULT_VALIDATION", checks=required,
            current_schema_version=result.get("current_version")
            if isinstance(result.get("current_version"), int) else None,
        )
        return 1
    _emit(
        status="PASS",
        checks=required,
        current_schema_version=result.get("current_version"),
        migration_count=result.get("migration_count"),
        required_table_count=result.get("required_table_count"),
        required_function_count=result.get("required_function_count"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
