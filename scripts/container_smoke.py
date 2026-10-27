"""Read-only loopback checks for an already running Phase 17 Compose stack."""

import argparse
import json
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
from uuid import UUID


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(port, path):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(f"http://127.0.0.1:{port}{path}", timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read(4096)


def check_stack(port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid local port")
    checks = {}
    try:
        status, _, page = fetch(port, "/")
        checks["frontend_page"] = status == 200 and b'id="root"' in page
        asset = re.search(rb'src="(/assets/[A-Za-z0-9_.-]+\.js)"', page)
        if asset:
            status, headers, body = fetch(port, asset.group(1).decode("ascii"))
            checks["built_javascript"] = status == 200 and "javascript" in headers.get("Content-Type", "") and not body.lstrip().startswith(b"<")
        else:
            checks["built_javascript"] = False
        status, _, body = fetch(port, "/healthz")
        checks["frontend_health"] = status == 200 and body.strip() == b"ok"
        status, _, body = fetch(port, "/api/health")
        checks["backend_health_proxy"] = status == 200 and json.loads(body) == {"status": "ok", "service": "novamind-api"}
        status, headers, _ = fetch(port, "/api/cases")
        checks["anonymous_api_denied"] = status == 401 and headers.get("WWW-Authenticate", "").lower() == "bearer"
        try:
            UUID(headers.get("X-Trace-ID", ""))
            checks["backend_trace_header"] = True
        except ValueError:
            checks["backend_trace_header"] = False
        status, _, _ = fetch(port, "/assets/phase17-missing.js")
        checks["missing_asset_not_spa"] = status == 404
    except Exception:
        checks["stack_reachable_and_valid"] = False
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    checks = check_stack(args.port)
    print(json.dumps(checks, sort_keys=True, indent=2))
    return 0 if checks and all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
