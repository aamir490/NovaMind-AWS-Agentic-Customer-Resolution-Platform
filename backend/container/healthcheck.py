"""Process-health probe only; never authenticate, call a model, or use a proxy."""

import json
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def healthy():
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open("http://127.0.0.1:8000/api/health", timeout=2) as response:
            raw = response.read(4097)
            if response.status != 200 or len(raw) > 4096:
                return False
            body = json.loads(raw)
            return isinstance(body, dict) and body.get("status") == "ok" and body.get("service") == "novamind-api"
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy() else 1)
