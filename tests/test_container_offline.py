"""Offline-only container provider and Compose isolation contracts."""

import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import yaml

from backend.app.schemas import CaseCreate, OrderCreate
from backend.container.bootstrap import (
    _OfflineScriptedProvider, _llm_provider, _sqlite_database_path, create_container_app,
)


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "offline-container-customer-token"
REVIEWER_TOKEN = "offline-container-reviewer-token"
CUSTOMER_ID = UUID(int=27)
OFFLINE = {
    "NOVAMIND_LLM_PROVIDER": "local_scripted",
    "NOVAMIND_OFFLINE_TESTING": "true",
}


class OfflineContainerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.enterContext(patch("socket.getaddrinfo", side_effect=AssertionError("No DNS")))

    def configure_auth(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        path = directory / "auth.json"
        path.write_text(json.dumps([
            {
                "token": TOKEN,
                "identity": {
                    "user_id": str(UUID(int=28)), "role": "CUSTOMER",
                    "customer_id": str(CUSTOMER_ID),
                },
            },
            {
                "token": REVIEWER_TOKEN,
                "identity": {
                    "user_id": str(UUID(int=29)), "role": "REVIEWER",
                    "customer_id": None,
                },
            },
        ]), encoding="utf-8")
        os.environ["NOVAMIND_AUTH_FILE"] = str(path)

    async def request(self, app, method, path, body=None, *, token=TOKEN):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local.test",
        ) as client:
            return await client.request(
                method, path, json=body,
                headers={"Authorization": f"Bearer {token}"},
            )

    def test_offline_provider_requires_exact_double_opt_in(self):
        invalid = (
            {"NOVAMIND_LLM_PROVIDER": "local_scripted"},
            {"NOVAMIND_OFFLINE_TESTING": "true"},
            {**OFFLINE, "NOVAMIND_OFFLINE_TESTING": "TRUE"},
            {**OFFLINE, "AWS_PROFILE": "must-not-be-read"},
            {**OFFLINE, "BEDROCK_MODEL_ID": "must-not-be-read"},
        )
        for environment in invalid:
            with self.subTest(environment=tuple(environment)):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(RuntimeError):
                        _llm_provider()

    def test_explicit_offline_provider_has_no_external_adapter(self):
        os.environ.update(OFFLINE)
        provider = _llm_provider()
        self.assertIsInstance(provider, _OfflineScriptedProvider)
        self.assertFalse(hasattr(provider, "client"))
        provider.close()

    async def test_offline_runtime_completes_and_emits_refund_node(self):
        self.configure_auth()
        os.environ.update(OFFLINE)
        app = create_container_app()
        service = app.state.case_service
        order = service.create_order(OrderCreate(
            customer_id=CUSTOMER_ID,
            items=[{"sku": "OFF-1", "name": "Offline fixture", "quantity": 1}],
        ))
        case = service.create_case(CaseCreate(
            customer_id=CUSTOMER_ID, order_id=order.id,
            subject="Offline refund", description="Verify deterministic tracking.",
        ))
        identity = await self.request(app, "GET", "/api/identity", token=REVIEWER_TOKEN)
        self.assertEqual(identity.status_code, 200)
        self.assertEqual(identity.json()["provider"], "local_scripted")
        body = {
            "instance_id": identity.json()["instance_id"],
            "request_id": str(uuid4()), "case_id": str(case.id),
            "message": "Check refund eligibility for OFF-1.",
            "resolution_intent": "refund", "refund_sku": "OFF-1",
        }
        started = await self.request(app, "POST", "/api/runs", body, token=REVIEWER_TOKEN)
        self.assertEqual(started.status_code, 202, started.text)
        run_id = started.json()["run_id"]
        for _ in range(200):
            snapshot = await self.request(app, "GET", f"/api/runs/{run_id}", token=REVIEWER_TOKEN)
            if snapshot.json()["status"] not in ("RUNNING", "RESUMING"):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(snapshot.status_code, 200)
        self.assertNotEqual(snapshot.json()["status"], "RUNNING")
        events = await self.request(app, "GET", f"/api/runs/{run_id}/events?after=0", token=REVIEWER_TOKEN)
        self.assertEqual(events.status_code, 200, events.text)
        self.assertIn("evaluate_refund", {
            event["node"] for event in events.json()["events"] if event["node"] is not None
        })
        app.state.frontend_runtime.close()

    async def test_explicit_demo_seed_reaches_atomic_human_review_without_model_call(self):
        self.configure_auth()
        os.environ.update({**OFFLINE, "NOVAMIND_OFFLINE_DEMO_DATA": "true"})
        app = create_container_app()
        cases = app.state.case_service.list_cases(CUSTOMER_ID)
        self.assertEqual(len(cases), 1)
        self.assertTrue(cases[0].subject.startswith("[SYNTHETIC OFFLINE DEMO]"))
        eligible = await self.request(
            app, "GET", f"/api/cases/{cases[0].id}/refund-eligibility?sku=LAP-1",
        )
        self.assertEqual(eligible.status_code, 200, eligible.text)
        self.assertTrue(eligible.json()["refund_eligible"])
        self.assertEqual(eligible.json()["assessment_basis"], "trusted_delivery_record")

        identity = await self.request(app, "GET", "/api/identity", token=REVIEWER_TOKEN)
        started = await self.request(app, "POST", "/api/runs", {
            "instance_id": identity.json()["instance_id"],
            "request_id": str(uuid4()), "case_id": str(cases[0].id),
            "message": "Request human review of the synthetic LAP-1 refund.",
            "resolution_intent": "refund", "refund_sku": "LAP-1",
        }, token=REVIEWER_TOKEN)
        self.assertEqual(started.status_code, 202, started.text)
        run_id = started.json()["run_id"]
        for _ in range(200):
            snapshot = await self.request(app, "GET", f"/api/runs/{run_id}", token=REVIEWER_TOKEN)
            if snapshot.json()["status"] != "RUNNING":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(snapshot.json()["status"], "REVIEW_REQUIRED")
        self.assertFalse(snapshot.json()["actions_executed"])

        reviews = await self.request(
            app, "GET", "/api/reviews", token=REVIEWER_TOKEN,
        )
        self.assertEqual(reviews.status_code, 200, reviews.text)
        self.assertEqual(len(reviews.json()["items"]), 1)
        review = snapshot.json()["review"]
        decision = await self.request(
            app, "POST", f"/api/runs/{run_id}/decision",
            {"instance_id": identity.json()["instance_id"],
             "workflow_id": review["workflow_id"], "case_id": review["case_id"],
             "proposal_id": review["proposal_id"], "review_id": review["review_id"],
             "decision": "APPROVE", "note": "Synthetic offline demo review."},
            token=REVIEWER_TOKEN,
        )
        self.assertEqual(decision.status_code, 202, decision.text)
        for _ in range(200):
            reviewed = await self.request(
                app, "GET", f"/api/runs/{run_id}", token=REVIEWER_TOKEN,
            )
            if reviewed.json()["status"] != "RESUMING":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(reviewed.json()["status"], "REVIEWED")
        self.assertEqual(reviewed.json()["reviewed_status"], "APPROVED")
        self.assertFalse(reviewed.json()["actions_executed"])
        app.state.frontend_runtime.close()

    def test_demo_seed_requires_exact_offline_opt_in(self):
        self.configure_auth()
        for environment in (
            {"NOVAMIND_OFFLINE_DEMO_DATA": "true"},
            {**OFFLINE, "NOVAMIND_OFFLINE_DEMO_DATA": "TRUE"},
        ):
            with self.subTest(environment=tuple(environment)):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(RuntimeError):
                        create_container_app()

    def test_offline_compose_override_is_backend_only_and_adds_no_network_or_mount(self):
        override = yaml.safe_load((ROOT / "compose.offline.yaml").read_text())
        self.assertEqual(set(override["services"]), {"backend"})
        backend = override["services"]["backend"]
        self.assertEqual(set(backend), {"environment"})
        self.assertEqual(backend["environment"], OFFLINE)
        base = yaml.safe_load((ROOT / "compose.yaml").read_text())
        self.assertEqual(base["services"]["backend"]["networks"], ["application"])
        self.assertTrue(base["networks"]["application"]["internal"])
        demo = yaml.safe_load((ROOT / "compose.demo.yaml").read_text())
        self.assertEqual(demo, {"services": {"backend": {"environment": {
            "NOVAMIND_OFFLINE_DEMO_DATA": "true",
        }}}})

    def test_sqlite_path_is_opt_in_and_persistence_overlay_mounts_a_named_volume(self):
        self.assertIsNone(_sqlite_database_path())
        with patch.dict(os.environ, {"NOVAMIND_SQLITE_PATH": "relative.sqlite"}, clear=True):
            with self.assertRaises(RuntimeError):
                _sqlite_database_path()

        database = Path(self.enterContext(TemporaryDirectory())) / "novamind.sqlite"
        with patch.dict(os.environ, {"NOVAMIND_SQLITE_PATH": str(database)}, clear=True):
            self.assertEqual(_sqlite_database_path(), database)

        base = yaml.safe_load((ROOT / "compose.yaml").read_text())
        backend = base["services"]["backend"]
        self.assertTrue(backend["read_only"])
        self.assertNotIn("volumes", backend)

        overlay = yaml.safe_load((ROOT / "compose.persistence.yaml").read_text())
        self.assertEqual(set(overlay), {"services", "volumes"})
        self.assertEqual(set(overlay["services"]), {"backend"})
        persisted_backend = overlay["services"]["backend"]
        self.assertEqual(persisted_backend["environment"], {
            "NOVAMIND_SQLITE_PATH": "/srv/state/novamind/novamind.sqlite",
        })
        self.assertEqual(persisted_backend["volumes"], [{
            "type": "volume", "source": "novamind_sqlite", "target": "/srv/state",
        }])
        self.assertEqual(overlay["volumes"], {
            "novamind_sqlite": {"name": "${NOVAMIND_SQLITE_VOLUME:-novamind_sqlite}"},
        })
        self.assertNotIn("read_only", persisted_backend["volumes"][0])
        self.assertNotIn("NOVAMIND_AGENT_ENGINE", persisted_backend["environment"])
        self.assertNotIn("networks", persisted_backend)
        self.assertNotIn("secrets", persisted_backend)

        # Persistence is additive: it does not select a provider, credentials,
        # demo data, or a different agent engine in any existing overlay.
        for filename in (
            "compose.auth.yaml", "compose.offline.yaml", "compose.demo.yaml",
            "compose.bedrock.yaml", "compose.bedrock.demo.yaml",
        ):
            with self.subTest(overlay=filename):
                other = yaml.safe_load((ROOT / filename).read_text())["services"]["backend"]
                self.assertNotIn("NOVAMIND_SQLITE_PATH", other.get("environment", {}))

        runtime = (ROOT / "backend/Dockerfile").read_text().rsplit("FROM ", 1)[1]
        self.assertIn("RUN install -d -o 10001 -g 10001 -m 0750 /srv/state/novamind", runtime)
        self.assertIn("USER 10001:10001", runtime)


if __name__ == "__main__":
    unittest.main()
