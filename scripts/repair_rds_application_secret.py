"""One-time in-place repair for an invalid NovaMind RDS seed secret.

The password remains inside the boto3 process and is never written to a file,
placed in a command-line argument, or printed.  This is intentionally not a
general secret editor: it accepts only the reviewed project secret and exact
three-field seed shape.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError


SECRET_NAME = "novamind-crp-postgres-app-dev"
EXPECTED_ROLE = "novamind_app"
EXPECTED_CA_PATH = "/etc/novamind/rds-ca-bundle.pem"
_SAFE_PASSWORD = re.compile(r"[A-Za-z0-9!#$%&()*+,\-.:;<=>?@\[\]^_`{|}~]{48}\Z")
_EXCLUDED = '"\'\\/ '


def _parse_seed(secret_string: str) -> dict[str, Any]:
    try:
        value = json.loads(secret_string)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("application secret is not valid JSON") from error
    if not isinstance(value, dict) or set(value) != {"username", "password", "rds_ca_path"}:
        raise RuntimeError("application secret fields are not the reviewed seed shape")
    if value["username"] != EXPECTED_ROLE or value["rds_ca_path"] != EXPECTED_CA_PATH:
        raise RuntimeError("application secret username or CA path is unexpected")
    if not isinstance(value["password"], str):
        raise RuntimeError("application secret password type is unexpected")
    return value


def repair_secret(client: Any, *, secret_name: str = SECRET_NAME) -> dict[str, Any]:
    if secret_name != SECRET_NAME:
        raise RuntimeError("refusing to operate on an unapproved secret name")
    try:
        metadata = client.describe_secret(SecretId=secret_name)
        if metadata.get("Name") != SECRET_NAME or not metadata.get("ARN"):
            raise RuntimeError("application secret metadata is unexpected")
        current = client.get_secret_value(SecretId=secret_name)
        seed = _parse_seed(current.get("SecretString"))
        current_password = seed["password"]
        if _SAFE_PASSWORD.fullmatch(current_password):
            raise RuntimeError("application secret password is already valid; refusing rotation")
        generated = client.get_random_password(
            PasswordLength=48,
            ExcludeCharacters=_EXCLUDED,
            RequireEachIncludedType=True,
        ).get("RandomPassword")
        if not isinstance(generated, str) or not _SAFE_PASSWORD.fullmatch(generated):
            raise RuntimeError("Secrets Manager returned a password outside the reviewed policy")
        payload = json.dumps(
            {"username": EXPECTED_ROLE, "password": generated, "rds_ca_path": EXPECTED_CA_PATH},
            separators=(",", ":"),
        )
        result = client.put_secret_value(SecretId=secret_name, SecretString=payload)
        return {"name": SECRET_NAME, "arn": metadata["ARN"], "password_length": len(generated),
                "version_id_present": bool(result.get("VersionId"))}
    except (ClientError, BotoCoreError) as error:
        raise RuntimeError("Secrets Manager repair failed; no secret value was printed") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair the approved NovaMind RDS seed secret in place.")
    parser.add_argument("--profile", default="novamind")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--secret-name", default=SECRET_NAME)
    parser.add_argument("--apply", action="store_true", required=True,
                        help="Required explicit opt-in; no dry-run mutation is implicit.")
    args = parser.parse_args()
    client = boto3.Session(profile_name=args.profile, region_name=args.region).client("secretsmanager")
    result = repair_secret(client, secret_name=args.secret_name)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
