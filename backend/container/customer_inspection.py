"""Bounded, read-only customer/binding inspection for a one-shot ECS task."""

from __future__ import annotations

import json
import os

from .rds_config import (
    DEFAULT_RDS_CA_BUNDLE_PATH,
    application_dsn,
    load_trusted_rds_ca_bundle,
    materialize_rds_ca_bundle,
    parse_application_secret,
)

MAX_ROWS = 1000


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
        ca_path = os.environ.get("NOVAMIND_RDS_CA_BUNDLE_PATH", DEFAULT_RDS_CA_BUNDLE_PATH)
        certificate = load_trusted_rds_ca_bundle(ca_path)
        certificate_path = materialize_rds_ca_bundle(certificate)
        dsn = application_dsn(secret, certificate_path=certificate_path)
        import psycopg  # Imported only inside the diagnostic task.

        connection = psycopg.connect(dsn, connect_timeout=10)
        connection.read_only = True
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '10000ms'")
                cursor.execute(
                    "SELECT customer_id::text, name FROM novamind.customers "
                    "ORDER BY customer_id LIMIT %s", (MAX_ROWS + 1,),
                )
                customers = cursor.fetchall()
                cursor.execute(
                    "SELECT subject_id::text, customer_id::text "
                    "FROM novamind.cognito_customer_bindings ORDER BY subject_id LIMIT %s",
                    (MAX_ROWS + 1,),
                )
                bindings = cursor.fetchall()
        if len(customers) > MAX_ROWS or len(bindings) > MAX_ROWS:
            _emit(status="FAIL", phase="RESULT_TOO_LARGE")
            return 1
        _emit(
            status="PASS",
            customer_count=len(customers),
            customers=[{"customer_id": row[0], "name": row[1]} for row in customers],
            binding_count=len(bindings),
            bindings=[{"subject_id": row[0], "customer_id": row[1]} for row in bindings],
        )
        return 0
    except Exception:
        _emit(status="FAIL", phase="DATABASE_CONNECTION_OR_RESULT")
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


if __name__ == "__main__":
    raise SystemExit(run())
