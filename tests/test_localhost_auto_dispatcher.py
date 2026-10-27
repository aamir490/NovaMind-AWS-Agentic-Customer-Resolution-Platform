"""Offline localhost AUTO-capability contracts; no Docker or provider transport."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import yaml

from backend.container.bootstrap import create_container_app


ROOT = Path(__file__).resolve().parents[1]
CUSTOMER_ID = UUID(int=101)
CUSTOMER_USER_ID = UUID(int=102)
ADMIN_ID = UUID(int=103)
REVIEWER_ID = UUID(int=104)
ADMIN_TOKEN = "localhost-auto-admin-token"
REVIEWER_TOKEN = "localhost-auto-reviewer-token"


class LocalhostAutomaticDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        # Isolate NovaMind/provider selectors without clearing Windows runtime
        # variables that Starlette's TestClient needs to create its event loop.
        original_environment = os.environ.copy()
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(original_environment)))
        for name in tuple(os.environ):
            if name.startswith(("NOVAMIND_", "AWS_", "BEDROCK_", "GEMINI_")):
                os.environ.pop(name)
        auth_file = self.directory / "auth.json"
        auth_file.write_text(json.dumps([
            {"token": "localhost-auto-customer-token", "identity": {
                "user_id": str(CUSTOMER_USER_ID), "role": "CUSTOMER",
                "customer_id": str(CUSTOMER_ID),
            }},
            {"token": ADMIN_TOKEN, "identity": {
                "user_id": str(ADMIN_ID), "role": "ADMIN",
            }},
            {"token": REVIEWER_TOKEN, "identity": {
                "user_id": str(REVIEWER_ID), "role": "REVIEWER",
            }},
        ]), encoding="utf-8")
        os.environ.update({
            "NOVAMIND_AUTH_FILE": str(auth_file),
            "NOVAMIND_SQLITE_PATH": str(self.directory / "novamind.sqlite"),
            "NOVAMIND_LLM_PROVIDER": "local_scripted",
            "NOVAMIND_OFFLINE_TESTING": "true",
            "NOVAMIND_AUTOMATIC_DISPATCHER": "true",
            "NOVAMIND_DISPATCHER_USER_ID": str(REVIEWER_ID),
        })

    @staticmethod
    def headers(token, key=None):
        result = {"Authorization": f"Bearer {token}"}
        if key is not None:
            result["Idempotency-Key"] = str(key)
        return result

    def app(self):
        return create_container_app()

    def test_generic_overlay_is_provider_neutral_and_default_off(self):
        generic = yaml.safe_load((ROOT / "compose.auto.yaml").read_text(encoding="utf-8"))
        legacy = yaml.safe_load((ROOT / "compose.bedrock.auto.yaml").read_text(encoding="utf-8"))
        self.assertEqual(generic["services"]["backend"]["environment"],
                         legacy["services"]["backend"]["environment"])
        values = generic["services"]["backend"]["environment"]
        self.assertNotIn("NOVAMIND_LLM_PROVIDER", values)
        self.assertNotIn("NOVAMIND_AGENT_ENGINE", values)
        helper = (ROOT / "scripts" / "NovaMind-Localhost.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$EnableAutomaticDispatcher", helper)
        self.assertIn("persisted mode remains MANUAL", helper)
        self.assertIn("supports only -Mode Offline", helper)

    def test_capability_is_available_but_manual_is_persisted_default(self):
        app = self.app()
        try:
            self.assertIsNotNone(app.state.investigation_dispatcher)
            with TestClient(app) as client:
                response = client.get("/api/admin/investigation-control",
                                      headers=self.headers(REVIEWER_TOKEN))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["configured_mode"], "MANUAL")
            self.assertEqual(response.json()["effective_mode"], "MANUAL")
            self.assertTrue(response.json()["automatic_capability_available"])
            self.assertEqual(response.json()["version"], 1)
        finally:
            app.state.investigation_executor.close()
            app.state.frontend_runtime.close()

    def test_admin_only_transition_is_optimistic_and_persists(self):
        app = self.app()
        try:
            with TestClient(app) as client:
                denied = client.patch("/api/admin/investigation-control",
                    headers=self.headers(REVIEWER_TOKEN, uuid4()),
                    json={"mode": "AUTO", "expected_version": 1})
                changed = client.patch("/api/admin/investigation-control",
                    headers=self.headers(ADMIN_TOKEN, uuid4()),
                    json={"mode": "AUTO", "expected_version": 1})
                stale = client.patch("/api/admin/investigation-control",
                    headers=self.headers(ADMIN_TOKEN, uuid4()),
                    json={"mode": "MANUAL", "expected_version": 1})
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual((changed.json()["configured_mode"], changed.json()["effective_mode"],
                              changed.json()["version"]), ("AUTO", "AUTO", 2))
            self.assertEqual(stale.status_code, 409)
        finally:
            app.state.investigation_executor.close()
            app.state.frontend_runtime.close()

        # The backing repository opens a fresh transaction for this read, so
        # this verifies durable storage without constructing a second app while
        # the first TestClient's SQLite lifecycle is still unwinding.
        persisted = app.state.investigation_control.get()
        self.assertEqual((persisted.mode.value, persisted.version), ("AUTO", 2))


if __name__ == "__main__":
    unittest.main()
