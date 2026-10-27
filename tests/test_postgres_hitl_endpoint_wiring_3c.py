"""Bounded endpoint-wiring coverage for the PostgreSQL HITL boundary.

The live class is opt-in and accepts only the one disposable Phase 3C
database.  It calls the registered FastAPI route handlers with resolved
dependencies so authorization and PostgreSQL service wiring are exercised
without starting a server, provider, graph, or action executor.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import re
import unittest
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from backend.app.main import create_app
from backend.app.proposal_routes import approve_proposal, reject_proposal
from backend.app.proposals import ProposalCreate, ProposalStatus
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.security import (
    AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential,
    ReviewInput, SecurityError, authenticated,
)
from backend.app.service import ConflictError


_EXPECTED_DATABASE = "novamind_phase_c_test_20261026"
_DISPOSABLE_DATABASE = re.compile(r"novamind_phase_c_test_20261026$")


def _route(app, handler, proposal_id, body, identity_name):
    with authenticated(app.state.auth_provider, app.state.test_tokens[identity_name]):
        return handler(
            proposal_id, body, app.state.proposal_service,
            app.state.hitl_review_service, app.state.case_service,
        )


class PostgreSQLHITLEndpointWiringOfflineTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(agent_engine="strands", strands_model_factory=lambda: object())
        self.customer = self.app.state.case_service.create_customer(
            CustomerCreate(name="HITL endpoint customer")
        )
        self.order = self.app.state.case_service.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "HITL-1", "name": "HITL test item", "quantity": 1}],
        ))
        self.case = self.app.state.case_service.create_case(CaseCreate(
            customer_id=self.customer.id, order_id=self.order.id,
            subject="HITL endpoint test", description="Deterministic review.",
        ))
        self.identities = {
            "customer": AuthenticatedIdentity(user_id=uuid4(), role="CUSTOMER",
                                               customer_id=self.customer.id),
            "reviewer": AuthenticatedIdentity(user_id=uuid4(), role="REVIEWER"),
            "admin": AuthenticatedIdentity(user_id=uuid4(), role="ADMIN"),
        }
        self.app.state.test_tokens = {
            name: f"hitl-endpoint-wiring-{name}" for name in self.identities
        }
        self.app.state.auth_provider = LocalAuthenticationProvider(tuple(
            LocalCredential(token=token, identity=self.identities[name])
            for name, token in self.app.state.test_tokens.items()
        ))

    def proposal(self):
        return self.app.state.proposal_service.create(ProposalCreate(
            case_id=self.case.id, action="replacement", rationale="Review this replacement."
        ))

    def test_route_is_registered_and_customer_is_denied_before_review_service(self):
        paths = self.app.openapi()["paths"]
        self.assertIn("/api/proposals/{proposal_id}/approve", paths)
        self.assertIn("post", paths["/api/proposals/{proposal_id}/approve"])
        proposal = self.proposal()
        with self.assertRaises(SecurityError):
            _route(self.app, approve_proposal, proposal.id,
                   ReviewInput(note="customer must not approve"), "customer")
        self.assertEqual(self.app.state.proposal_service.get(proposal.id).status,
                         ProposalStatus.PENDING_REVIEW)

    def test_route_wires_staff_identity_and_never_starts_execution(self):
        proposal = self.proposal()
        reviewed = _route(
            self.app, approve_proposal, proposal.id,
            ReviewInput(note="approved by reviewer"), "reviewer",
        )
        self.assertEqual(reviewed.status, ProposalStatus.APPROVED)
        self.assertIsNone(self.app.state.investigation_executor)
        self.assertIsNone(self.app.state.frontend_runtime.provider_factory)


@unittest.skipUnless(
    os.environ.get("NOVAMIND_POSTGRES_TEST_DSN"),
    "NOVAMIND_POSTGRES_TEST_DSN is not configured",
)
class PostgreSQLHITLEndpointIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ["NOVAMIND_POSTGRES_TEST_DSN"]
        parsed = urlsplit(dsn)
        database = unquote(parsed.path.lstrip("/"))
        if (parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname
                or not _DISPOSABLE_DATABASE.fullmatch(database)):
            raise RuntimeError(
                "NOVAMIND_POSTGRES_TEST_DSN must target exactly "
                f"{_EXPECTED_DATABASE}"
            )
        cls.dsn = dsn
        from psycopg import connect
        connection = connect(dsn, autocommit=True, connect_timeout=5)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                if cursor.fetchone()[0] != _EXPECTED_DATABASE:
                    raise RuntimeError("PostgreSQL database identity mismatch")
        finally:
            connection.close()

    @classmethod
    def _reset_schema(cls):
        from psycopg import connect
        connection = connect(cls.dsn, autocommit=True, connect_timeout=5)
        try:
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA public CASCADE")
                cursor.execute("CREATE SCHEMA public")
        finally:
            connection.close()

    def setUp(self):
        self._reset_schema()
        self.app = create_app(
            postgres_dsn=self.dsn, agent_engine="strands",
            strands_model_factory=lambda: object(),
        )
        cases = self.app.state.case_service
        self.customer = cases.create_customer(CustomerCreate(name="PG HITL customer"))
        self.order = cases.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "PG-HITL-1", "name": "PostgreSQL item", "quantity": 1}],
        ))
        self.case = cases.create_case(CaseCreate(
            customer_id=self.customer.id, order_id=self.order.id,
            subject="PostgreSQL HITL endpoint", description="Durable endpoint review.",
        ))
        self.identities = {
            "customer": AuthenticatedIdentity(
                user_id=uuid4(), role="CUSTOMER", customer_id=self.customer.id,
            ),
            "reviewer": AuthenticatedIdentity(user_id=uuid4(), role="REVIEWER"),
            "admin": AuthenticatedIdentity(user_id=uuid4(), role="ADMIN"),
        }
        self.tokens = {name: f"pg-hitl-endpoint-{name}" for name in self.identities}
        provider = LocalAuthenticationProvider(tuple(
            LocalCredential(token=self.tokens[name], identity=identity)
            for name, identity in self.identities.items()
        ))
        self.app.state.auth_provider = provider
        self.app.state.test_tokens = self.tokens

    def tearDown(self):
        self._reset_schema()

    def proposal_and_binding(self):
        proposal = self.app.state.proposal_service.create(ProposalCreate(
            case_id=self.case.id, action="replacement", rationale="PG endpoint review."
        ))
        workflow_id, review_id = uuid4(), uuid4()
        self.app.state.proposal_service.bind_workflow_review(
            proposal.id, workflow_id=workflow_id, case_id=self.case.id, review_id=review_id,
        )
        return proposal, workflow_id, review_id

    def body(self, workflow_id, review_id):
        return ReviewInput(note="durable decision", workflow_id=workflow_id, review_id=review_id)

    def call(self, handler, proposal_id, body, identity_name):
        return _route(self.app, handler, proposal_id, body, identity_name)

    def test_approval_restart_audit_and_actions_are_durable(self):
        proposal, workflow_id, review_id = self.proposal_and_binding()
        reviewed = self.call(
            approve_proposal, proposal.id, self.body(workflow_id, review_id), "reviewer",
        )
        self.assertEqual(reviewed.status, ProposalStatus.APPROVED)
        events = self.app.state.proposal_service._repository.audit_events(workflow_id)
        self.assertEqual([event["kind"] for event in events], ["REVIEW_REQUIRED", "HUMAN_REVIEW"])
        self.assertFalse(events[-1]["actions_executed"])
        restarted = create_app(
            auth_provider=self.app.state.auth_provider, postgres_dsn=self.dsn,
            agent_engine="strands", strands_model_factory=lambda: object(),
        )
        self.assertEqual(restarted.state.proposal_service.get(proposal.id).status,
                         ProposalStatus.APPROVED)
        self.assertIsNone(restarted.state.investigation_executor)

    def test_binding_mismatch_customer_denial_and_duplicate_conflict(self):
        proposal, workflow_id, review_id = self.proposal_and_binding()
        with self.assertRaises(ConflictError):
            self.call(approve_proposal, proposal.id, self.body(uuid4(), review_id), "reviewer")
        with self.assertRaises(SecurityError):
            self.call(approve_proposal, proposal.id, self.body(workflow_id, review_id), "customer")
        reviewed = self.call(
            reject_proposal, proposal.id, self.body(workflow_id, review_id), "admin",
        )
        self.assertEqual(reviewed.status, ProposalStatus.REJECTED)
        with self.assertRaises(ConflictError):
            self.call(reject_proposal, proposal.id, self.body(workflow_id, review_id), "admin")
        with self.assertRaises(ConflictError):
            self.call(approve_proposal, proposal.id, self.body(workflow_id, review_id), "reviewer")

    def test_concurrent_endpoint_decisions_have_one_atomic_winner(self):
        proposal, workflow_id, review_id = self.proposal_and_binding()
        body = self.body(workflow_id, review_id)

        def decide(name):
            try:
                return self.call(approve_proposal, proposal.id, body, name)
            except ConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, ("reviewer", "admin")))
        self.assertEqual(sum(result is not None for result in results), 1)
        events = self.app.state.proposal_service._repository.audit_events(workflow_id)
        self.assertEqual([event["kind"] for event in events], ["REVIEW_REQUIRED", "HUMAN_REVIEW"])
        self.assertFalse(events[-1]["actions_executed"])


if __name__ == "__main__":
    unittest.main()
