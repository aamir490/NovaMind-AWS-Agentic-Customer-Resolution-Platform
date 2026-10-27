"""Focused Phase 17 contracts and offline startup checks; Docker runtime is separate."""

import fnmatch
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID

import httpx
import yaml

from backend.container.bootstrap import create_container_app
from backend.container.healthcheck import NoRedirect, healthy
from scripts.container_smoke import check_stack


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "phase17-local-admin-fixture"
CREDENTIAL = {"token": TOKEN, "identity": {"user_id": str(UUID(int=17)), "role": "ADMIN"}}


def included(context, path):
    """Apply this project's simple allowlist patterns to a synthetic relative path."""
    allowed = True
    for line in (context / ".dockerignore").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pattern = line.lstrip("!")
        if fnmatch.fnmatchcase(path, pattern):
            allowed = line.startswith("!")
    return allowed


class ContainerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No live calls")))
        self.directory = Path(self.enterContext(TemporaryDirectory()))

    def configure(self, value):
        path = self.directory / "credentials.json"
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        os.environ["NOVAMIND_AUTH_FILE"] = str(path)
        return path

    async def request(self, app, method, path, **kwargs):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
            return await client.request(method, path, **kwargs)

    async def test_default_startup_has_health_and_no_anonymous_business_access(self):
        app = create_container_app()
        health = await self.request(app, "GET", "/api/health")
        self.assertEqual(health.json(), {"status": "ok", "service": "novamind-api"})
        self.assertEqual(health.status_code, 200)
        denied = await self.request(app, "GET", "/api/cases")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers["WWW-Authenticate"], "Bearer")
        UUID(denied.headers["X-Trace-ID"])

    async def test_mounted_credentials_preserve_authentication_and_role_controls(self):
        self.configure([CREDENTIAL, {"token": "phase17-local-reviewer", "identity": {"user_id": str(UUID(int=18)), "role": "REVIEWER"}}])
        app = create_container_app()
        result = await self.request(app, "POST", "/api/customers", headers={"Authorization": f"Bearer {TOKEN}"}, json={"name": "Local"})
        self.assertEqual(result.status_code, 201)
        denied = await self.request(app, "POST", "/api/customers", headers={"Authorization": "Bearer phase17-local-reviewer"}, json={"name": "Local"})
        self.assertEqual(denied.status_code, 403)
        invalid = await self.request(app, "GET", "/api/cases", headers={"Authorization": "Bearer invalid"})
        self.assertEqual(invalid.status_code, 401)

    async def test_restart_does_not_persist_or_seed_business_records(self):
        self.configure([CREDENTIAL])
        first = create_container_app()
        record = await self.request(first, "POST", "/api/customers", headers={"Authorization": f"Bearer {TOKEN}"}, json={"name": "Local"})
        second = create_container_app()
        missing = await self.request(second, "GET", f"/api/customers/{record.json()['id']}", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(missing.status_code, 404)

    def test_invalid_credentials_fail_closed_without_secret_diagnostics(self):
        invalid = ("{SECRET", {"token": "SECRET"}, [{**CREDENTIAL, "extra": "SECRET"}],
                   [CREDENTIAL, CREDENTIAL], [{"token": TOKEN, "identity": {"user_id": str(UUID(int=17)), "role": "ROOT"}}],
                   '[{"token":"SECRET","token":"DUPLICATE"}]', " " * 65537)
        for value in invalid:
            self.configure(value)
            with self.assertRaisesRegex(RuntimeError, "^Container authentication configuration is invalid$") as caught:
                create_container_app()
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_missing_and_relative_secret_paths_fail_closed(self):
        for path in (str(self.directory / "missing.json"), "relative.json"):
            os.environ["NOVAMIND_AUTH_FILE"] = path
            with self.assertRaises(RuntimeError):
                create_container_app()

    def test_health_probe_uses_only_loopback_with_no_proxy_or_redirect(self):
        response = Mock(status=200)
        response.read.return_value = b'{"status":"ok","service":"novamind-api"}'
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("backend.container.healthcheck.build_opener", return_value=opener) as build:
            self.assertTrue(healthy())
        opener.open.assert_called_once_with("http://127.0.0.1:8000/api/health", timeout=2)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], NoRedirect)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://external.invalid"))

    def test_health_probe_rejects_unhealthy_malformed_and_oversized_responses(self):
        for status, data in ((503, b"{}"), (200, b"not json"), (200, b"[]"), (200, b'{"status":"ok"}'),
                             (200, b" " * 4097)):
            response = Mock(status=status)
            response.read.return_value = data
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value = response
            with patch("backend.container.healthcheck.build_opener", return_value=opener):
                self.assertFalse(healthy())
        with patch("backend.container.healthcheck.build_opener", side_effect=OSError("SECRET")):
            self.assertFalse(healthy())

    def test_compose_exposes_only_loopback_frontend_and_no_host_data(self):
        config = yaml.safe_load((ROOT / "compose.yaml").read_text())
        self.assertEqual(set(config["services"]), {"backend", "frontend"})
        backend, frontend = config["services"]["backend"], config["services"]["frontend"]
        self.assertNotIn("ports", backend)
        self.assertTrue(all(port.startswith("127.0.0.1:") for port in frontend["ports"]))
        self.assertTrue(config["networks"]["application"]["internal"])
        self.assertEqual(backend["networks"], ["application"])
        self.assertEqual(set(frontend["networks"]), {"application", "edge"})
        self.assertEqual(set(config["networks"]), {"application", "edge"})
        self.assertEqual(config["networks"]["edge"]["driver"], "bridge")
        self.assertFalse(config["networks"]["edge"]["internal"])
        self.assertEqual(frontend["ports"], ["127.0.0.1:${NOVAMIND_PORT:-8080}:8080"])
        for service in (backend, frontend):
            self.assertTrue(service["read_only"])
            self.assertTrue(service["init"])
            self.assertIn("ALL", service["cap_drop"])
            self.assertIn("no-new-privileges:true", service["security_opt"])
            self.assertNotIn("volumes", service)
            self.assertNotIn("env_file", service)
            self.assertNotIn("privileged", service)
            self.assertNotIn("command", service)
        self.assertEqual(frontend["depends_on"]["backend"]["condition"], "service_healthy")

    def test_build_contexts_exclude_secrets_stores_and_host_dependencies(self):
        config = yaml.safe_load((ROOT / "compose.yaml").read_text())
        for name, service in config["services"].items():
            context = (ROOT / service["build"]["context"]).resolve()
            self.assertEqual(context, ROOT / name)
            for path in (".env", ".env.production", "private.key", "state.sqlite3", ".venv/bin/python",
                         "node_modules/package/index.js", "dist/index.html", "app/__pycache__/main.pyc",
                         "inference-config.json", "messages.json", "nova-request.json"):
                self.assertFalse(included(context, path), (name, path))
        for path in ("app/main.py", "container/bootstrap.py", "container/healthcheck.py", "knowledge/support.json", "requirements.txt"):
            self.assertTrue(included(ROOT / "backend", path))
        for path in ("package-lock.json", "index.html", "src/main.jsx", "src/api.js", "nginx.conf"):
            self.assertTrue(included(ROOT / "frontend", path))

    def test_runtime_images_are_nonroot_and_backend_keeps_single_process(self):
        for name in ("backend", "frontend"):
            dockerfile = (ROOT / name / "Dockerfile").read_text()
            runtime = dockerfile.rsplit("FROM ", 1)[1]
            self.assertIn("USER 10001:10001", runtime)
            self.assertIn("HEALTHCHECK ", runtime)
            self.assertNotIn("COPY .", dockerfile)
            self.assertNotIn("ARG ", dockerfile)
        command = next(line[4:] for line in (ROOT / "backend/Dockerfile").read_text().splitlines() if line.startswith("CMD "))
        argv = json.loads(command)
        self.assertEqual(argv[argv.index("--workers") + 1], "1")
        self.assertIn("--no-proxy-headers", argv)
        self.assertNotIn("--reload", argv)
        self.assertIn("backend.container.bootstrap:create_container_app", argv)

    def test_proxy_configuration_retains_api_errors_auth_and_disables_replays(self):
        config = (ROOT / "frontend/nginx.conf").read_text()
        api = config.split("location /api/ {", 1)[1].split("}", 1)[0]
        self.assertIn("proxy_pass $backend$request_uri;", api)
        self.assertIn("Authorization $http_authorization;", api)
        self.assertIn("proxy_next_upstream off;", api)
        self.assertIn("proxy_intercept_errors off;", api)
        self.assertNotIn("try_files", api)
        self.assertNotIn("user root", config)
        self.assertIn("resolver 127.0.0.11", config)

    def test_secret_overlay_requires_explicit_file_and_only_mounts_backend(self):
        config = yaml.safe_load((ROOT / "compose.auth.yaml").read_text())
        self.assertEqual(set(config["services"]), {"backend"})
        self.assertIn("${NOVAMIND_AUTH_SOURCE:?", config["secrets"]["novamind_auth"]["file"])
        self.assertEqual(config["services"]["backend"]["environment"]["NOVAMIND_AUTH_FILE"], "/run/secrets/novamind_auth")

    def test_smoke_verifier_requires_static_assets_proxy_auth_and_trace(self):
        outcomes = [(200, {}, b'<div id="root"></div><script src="/assets/main-123.js"></script>'),
                    (200, {"Content-Type": "application/javascript"}, b"console.log('local');"),
                    (200, {}, b"ok\n"), (200, {}, b'{"status":"ok","service":"novamind-api"}'),
                    (401, {"WWW-Authenticate": "Bearer", "X-Trace-ID": str(UUID(int=17))}, b"{}"), (404, {}, b"")]
        with patch("scripts.container_smoke.fetch", side_effect=outcomes) as fetch:
            checks = check_stack(8080)
        self.assertTrue(all(checks.values()), checks)
        self.assertEqual(len(checks), 7)
        self.assertEqual(fetch.call_count, 6)
        bad = list(outcomes)
        bad[4] = (200, {}, b"[]")
        with patch("scripts.container_smoke.fetch", side_effect=bad):
            self.assertFalse(all(check_stack(8080).values()))

    def test_smoke_verifier_fails_for_unreachable_stack_and_invalid_port(self):
        with patch("scripts.container_smoke.fetch", side_effect=OSError("unreachable")):
            self.assertFalse(all(check_stack(8080).values()))
        for value in (0, 65536, True, "8080"):
            with self.assertRaises(ValueError):
                check_stack(value)


if __name__ == "__main__":
    unittest.main()
