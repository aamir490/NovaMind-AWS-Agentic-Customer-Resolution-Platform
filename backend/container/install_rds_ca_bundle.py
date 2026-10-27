"""Install the pinned AWS RDS global CA bundle during image construction.

This is a build-time helper, not a runtime network path. The release build
must provide the SHA-256 digest obtained from the reviewed official bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import ssl
import stat
import tempfile
from urllib.request import Request, urlopen


SOURCE_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
MAX_BYTES = 1024 * 1024
SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


def install(output: Path, expected_sha256: str) -> None:
    if not SHA256.fullmatch(expected_sha256):
        raise SystemExit("RDS CA bundle SHA-256 build argument is required")
    if output != Path("/etc/novamind/rds-ca-bundle.pem"):
        raise SystemExit("RDS CA bundle output path is fixed")
    parent = output.parent
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    parent_stat = parent.stat(follow_symlinks=False)
    if (not stat.S_ISDIR(parent_stat.st_mode) or stat.S_IMODE(parent_stat.st_mode) & 0o022):
        raise SystemExit("RDS CA bundle directory is unsafe")
    if output.exists() or output.is_symlink():
        raise SystemExit("RDS CA bundle output already exists")

    request = Request(SOURCE_URL, headers={"User-Agent": "novamind-crp-image-build"})
    with urlopen(request, timeout=30) as response:
        payload = response.read(MAX_BYTES + 1)
    if not payload or len(payload) > MAX_BYTES:
        raise SystemExit("RDS CA bundle size is invalid")
    if hashlib.sha256(payload).hexdigest().lower() != expected_sha256.lower():
        raise SystemExit("RDS CA bundle SHA-256 does not match the reviewed digest")
    try:
        certificate = payload.decode("utf-8")
        if ("-----BEGIN CERTIFICATE-----" not in certificate
                or "-----END CERTIFICATE-----" not in certificate):
            raise ValueError
        ssl.create_default_context(cadata=certificate)
    except (UnicodeDecodeError, ssl.SSLError, ValueError) as error:
        raise SystemExit("RDS CA bundle PEM validation failed") from error

    fd, temporary = tempfile.mkstemp(prefix=".rds-ca-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, 0o444)
        os.replace(temporary, output)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/etc/novamind/rds-ca-bundle.pem")
    args = parser.parse_args()
    install(Path(args.output), os.environ.get("RDS_CA_BUNDLE_SHA256", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
