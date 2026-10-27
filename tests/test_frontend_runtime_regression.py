"""
Regression tests for three browser-visible bugs.

BUG-1 (frontend/src/api.js):
  eventNodes was missing 'evaluate_refund'. Covered by
  tests/frontend-regression-bug-fixes.test.mjs (JS side).

BUG-2 (frontend/src/diagnosticsContracts.js):
  operationLabels was missing 'refund_eligibility'. Covered by
  tests/frontend-regression-bug-fixes.test.mjs (JS side).

BUG-3 (backend/app/frontend_runtime.py):
  FrontendRuntime.events() accessed run.events[0].sequence without
  an empty-deque guard.  A concurrent read between run creation and
  the first _publish() call produced an unhandled IndexError (HTTP 500).
  Fixed by raising FrontendError(409, 'NO_EVENTS_YET') when the deque
  is empty.
"""

import asyncio
import time
import unittest
from threading import Event
from uuid import uuid4

import httpx

from backend.app.llm import LLMResponse
from backend.app.main import create_app
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, Role


class ScriptedProvider:
    """Minimal scripted LLM provider for ASGI integration tests."""

    def __init__(self, *, entered: Event | None = None, release: Event | None = None):
        self.entered = entered
        self.release = release

    def generate(self, request, *, response_schema):
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(5):
                raise AssertionError("Test did not release provider")
        import json
        return LLMResponse(text=json.dumps({"decision": {"kind": "final"}}), finish_reason="stop")


class EventsEmptyDequeRegressionTests(unittest.IsolatedAsyncioTestCase):
    """BUG-3 regression: events() must not IndexError on an empty deque."""

    async def asyncSetUp(self):
        self.entered = Event()
        self.release = Event()
        self.app = create_app(
            local_provider_factory=lambda: ScriptedProvider(
                entered=self.entered, release=self.release
            ),
        )
        self.runtime = self.app.state.frontend_runtime
        self.addCleanup(self.runtime.close)

        cases = self.app.state.case_service
        customer = cases.create_customer(CustomerCreate(name="Test"))
        order = cases.create_order(
            OrderCreate(customer_id=customer.id,
                        items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}])
        )
        self.case = cases.create_case(
            CaseCreate(customer_id=customer.id, order_id=order.id,
                       subject="Test case", description="Regression test")
        )
        identity = AuthenticatedIdentity(user_id=uuid4(), role=Role.REVIEWER)
        token = "regression-test-token-0001"
        self.app.state.auth_provider = LocalAuthenticationProvider(
            (LocalCredential(token=token, identity=identity),)
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://local.test",
        )
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {"Authorization": f"Bearer {token}"}
        self.instance_id = str(self.runtime.instance_id)

    async def _start_run(self):
        body = {
            "instance_id": self.instance_id,
            "request_id": str(uuid4()),
            "case_id": str(self.case.id),
            "message": "Please review this case",
        }
        response = await self.client.post(
            "/api/runs", json=body, headers=self.headers
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    async def _wait_finished(self, run_id):
        for _ in range(300):
            response = await self.client.get(
                f"/api/runs/{run_id}", headers=self.headers
            )
            self.assertEqual(response.status_code, 200)
            snapshot = response.json()
            if snapshot["status"] not in ("RUNNING", "RESUMING"):
                return snapshot
            await asyncio.sleep(0.01)
        self.fail("Run did not finish in time")

    async def test_bug3_events_endpoint_returns_clean_error_when_deque_is_empty(self):
        """
        FrontendRuntime.events() must never raise IndexError when events is empty.

        We directly manipulate the internal _runs dict to create a run whose
        events deque has been cleared, simulating the narrow window that would
        previously cause an unhandled IndexError (HTTP 500).

        After the fix, the response must be a 409 with detail='NO_EVENTS_YET'
        rather than a 500.
        """
        # Start a run so we have a valid run_id and identity binding.
        run = await self._start_run()
        run_id = run["run_id"]

        # Wait for the run to finish so we have a stable run object to manipulate.
        self.release.set()  # allow the provider to finish
        await self._wait_finished(run_id)

        # Directly empty the deque to simulate the race-condition window.
        from uuid import UUID
        internal_run = self.runtime._runs[UUID(run_id)]
        internal_run.events.clear()

        response = await self.client.get(
            f"/api/runs/{run_id}/events?after=0", headers=self.headers
        )
        # Before the fix: IndexError → HTTP 500
        # After the fix: FrontendError(409, 'NO_EVENTS_YET') → HTTP 409
        self.assertEqual(
            response.status_code, 409,
            f"Expected 409 NO_EVENTS_YET, got {response.status_code}: {response.text}",
        )
        self.assertEqual(response.json().get("detail"), "NO_EVENTS_YET")

    async def test_bug3_events_endpoint_still_works_normally_when_deque_is_non_empty(self):
        """
        Confirm the normal events path is unaffected by the empty-deque guard.
        The guard must only fire when the deque is genuinely empty.
        """
        run = await self._start_run()
        run_id = run["run_id"]

        self.release.set()
        await self._wait_finished(run_id)

        response = await self.client.get(
            f"/api/runs/{run_id}/events?after=0", headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        page = response.json()
        self.assertGreater(len(page["events"]), 0)
        self.assertGreaterEqual(page["first_available_sequence"], 1)
        # Verify dropped_events contract: dropped_events == first_available_sequence - 1
        self.assertEqual(
            page["dropped_events"], page["first_available_sequence"] - 1
        )

    async def test_bug3_events_cursor_ahead_still_returns_409(self):
        """
        The existing EVENT_CURSOR_AHEAD guard must remain unaffected.
        """
        run = await self._start_run()
        run_id = run["run_id"]

        self.release.set()
        snapshot = await self._wait_finished(run_id)
        last_seq = snapshot["last_sequence"]

        # after > last_sequence must still be 409 EVENT_CURSOR_AHEAD, not NO_EVENTS_YET
        response = await self.client.get(
            f"/api/runs/{run_id}/events?after={last_seq + 1}", headers=self.headers
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json().get("detail"), "EVENT_CURSOR_AHEAD")


if __name__ == "__main__":
    unittest.main()
