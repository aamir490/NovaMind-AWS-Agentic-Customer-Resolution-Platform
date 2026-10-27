"""Exercise real HTTP on an ephemeral loopback port, without extra test packages."""

import json
import socket
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlencode
from uuid import uuid4

import uvicorn

from backend.app.main import create_app
from security_fixtures import ADMIN_TOKEN, admin_provider


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.listener = socket.socket()
        cls.listener.bind(("127.0.0.1", 0))
        cls.port = cls.listener.getsockname()[1]
        cls.server = uvicorn.Server(uvicorn.Config(create_app(auth_provider=admin_provider()), log_level="error"))
        cls.thread = threading.Thread(
            target=cls.server.run, kwargs={"sockets": [cls.listener]}, daemon=True,
        )
        cls.thread.start()
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 15
        while not cls.server.started:
            if not cls.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Local test server did not start")
            time.sleep(0.01)
        cls.client = build_opener(ProxyHandler({}))

    @classmethod
    def stop_server(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.listener.close()
        if cls.thread.is_alive():
            raise RuntimeError("Local test server did not stop")

    def request(self, method, path, body=None):
        request = Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + ADMIN_TOKEN},
        )
        try:
            response = self.client.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def fixture(self):
        status, customer = self.request("POST", "/api/customers", {"name": "Local test"})
        self.assertEqual(status, 201)
        status, order = self.request("POST", "/api/orders", {
            "customer_id": customer["id"],
            "items": [{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        })
        self.assertEqual(status, 201)
        body = {"customer_id": customer["id"], "order_id": order["id"],
                "subject": "Wrong laptop", "description": "Received another model."}
        return customer, order, body

    def test_health_contract_unchanged(self):
        self.assertEqual(self.request("GET", "/api/health"),
                         (200, {"status": "ok", "service": "novamind-api"}))

    def test_customer_order_case_round_trip_and_filter(self):
        customer, order, body = self.fixture()
        self.assertEqual(self.request("GET", f'/api/customers/{customer["id"]}'), (200, customer))
        self.assertEqual(self.request("GET", f'/api/orders/{order["id"]}'), (200, order))
        status, case = self.request("POST", "/api/cases", body)
        self.assertEqual(status, 201)
        self.assertEqual(case["status"], "open")
        self.assertEqual(self.request("GET", f'/api/cases/{case["id"]}'), (200, case))
        self.assertEqual(self.request("GET", f'/api/cases?customer_id={customer["id"]}'), (200, [case]))
        _, other = self.request("POST", "/api/customers", {"name": "No cases"})
        self.assertEqual(self.request("GET", f'/api/cases?customer_id={other["id"]}'), (200, []))
        self.assertIn(case, self.request("GET", "/api/cases")[1])

    def test_valid_invalid_and_repeated_status_requests(self):
        _, _, body = self.fixture()
        _, case = self.request("POST", "/api/cases", body)
        path = f'/api/cases/{case["id"]}/status'
        status, updated = self.request("PATCH", path, {"status": "in_review"})
        self.assertEqual(status, 200)
        self.assertEqual(updated["status"], "in_review")
        self.assertEqual(self.request("PATCH", path, {"status": "in_review"}), (200, updated))
        self.assertEqual(self.request("PATCH", path, {"status": "open"})[0], 409)
        self.assertEqual(self.request("PATCH", path, {"status": "resolved"})[0], 409)
        self.assertEqual(self.request("PATCH", path, {"status": "escalated"})[0], 200)
        self.assertEqual(self.request("PATCH", path, {"status": "in_review"})[0], 409)

    def test_ownership_conflict_and_missing_references(self):
        _, _, body = self.fixture()
        _, other = self.request("POST", "/api/customers", {"name": "Other"})
        self.assertEqual(self.request("POST", "/api/cases", {**body, "customer_id": other["id"]})[0], 409)
        self.assertEqual(self.request("POST", "/api/cases", {**body, "order_id": str(uuid4())})[0], 404)
        self.assertEqual(self.request("POST", "/api/orders", {
            "customer_id": str(uuid4()), "items": [{"sku": "x", "name": "x", "quantity": 1}],
        })[0], 404)

    def test_missing_resources_and_malformed_identifiers(self):
        for resource in ("customers", "orders", "cases"):
            with self.subTest(resource=resource):
                self.assertEqual(self.request("GET", f"/api/{resource}/{uuid4()}")[0], 404)
                self.assertEqual(self.request("GET", f"/api/{resource}/bad-id")[0], 422)
        self.assertEqual(self.request("GET", "/api/cases?customer_id=bad-id")[0], 422)
        self.assertEqual(self.request("PATCH", f"/api/cases/{uuid4()}/status", {"status": "in_review"})[0], 404)

    def test_bad_inputs_cannot_set_server_fields(self):
        for body in ({"name": " "}, {"name": "Test", "id": str(uuid4())}, {}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/customers", body)[0], 422)
        _, _, body = self.fixture()
        for changes in ({"status": "escalated"}, {"created_at": "2026-01-01"}, {"description": " "}):
            with self.subTest(changes=changes):
                self.assertEqual(self.request("POST", "/api/cases", {**body, **changes})[0], 422)

    def eligibility_url(self, customer, order, **changes):
        query = {"customer_id": customer["id"], "sku": "LAP-1", "quantity": 1,
                 "days_since_delivery": 30, "reason": "wrong_item", "condition": "unused",
                 **changes}
        return f'/api/orders/{order["id"]}/eligibility?' + urlencode(query)

    def test_business_lookups_are_read_only(self):
        for path in ("/api/inventory/LAP-1", "/api/inventory/LAP-2", "/api/policies/standard-return"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 200)
                self.assertEqual(self.request("POST", path, {})[0], 405)
        self.assertEqual(self.request("GET", "/api/inventory/LAP-2")[1]["available_quantity"], 0)
        for path in ("/api/inventory/unknown", "/api/policies/unknown"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_eligibility_round_trip_and_no_case_changes(self):
        customer, order, body = self.fixture()
        _, case = self.request("POST", "/api/cases", body)
        url = self.eligibility_url(customer, order)
        status, result = self.request("GET", url)
        self.assertEqual(status, 200)
        self.assertTrue(result["return_eligible"])
        self.assertTrue(result["refund_eligible"])
        self.assertFalse(result["authorization_granted"])
        self.assertEqual(result["policy_version"], "demo-v1")
        self.assertEqual(self.request("GET", url), (200, result))
        self.assertEqual(self.request("GET", f'/api/cases/{case["id"]}'), (200, case))
        status, denied = self.request("GET", self.eligibility_url(customer, order, days_since_delivery=31))
        self.assertEqual(status, 200)
        self.assertFalse(denied["refund_eligible"])
        self.assertEqual(denied["denial_reasons"], ["outside_return_window"])
        status, missing_item = self.request("GET", self.eligibility_url(customer, order, sku="LAP-2"))
        self.assertEqual(status, 200)
        self.assertEqual(missing_item["denial_reasons"], ["item_not_in_order"])

    def test_eligibility_errors(self):
        customer, order, _ = self.fixture()
        for change in ({"quantity": 0}, {"quantity": 1001}, {"days_since_delivery": -1},
                       {"reason": "other"}, {"condition": "unknown"}, {"sku": " "}):
            with self.subTest(change=change):
                self.assertEqual(self.request("GET", self.eligibility_url(customer, order, **change))[0], 422)
        _, other = self.request("POST", "/api/customers", {"name": "Other"})
        self.assertEqual(self.request("GET", self.eligibility_url(other, order))[0], 409)
        self.assertEqual(self.request("GET", self.eligibility_url(customer, order, sku="UNKNOWN"))[0], 404)
        self.assertEqual(self.request("GET", self.eligibility_url(customer, {"id": str(uuid4())}))[0], 404)


if __name__ == "__main__":
    unittest.main()
