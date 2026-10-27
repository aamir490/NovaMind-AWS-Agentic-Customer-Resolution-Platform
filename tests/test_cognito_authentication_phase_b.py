"""Offline Phase B Cognito verification and durable-binding regression tests."""

import base64
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from backend.app.domain import Customer, InvestigationJobStatus, InvestigationMode
from backend.app.llm import LLMResponse
from backend.app.main import create_app
from backend.app.persistence import SQLitePersistence, SQLiteWorkflowMetadataRepository
from backend.app.schemas import OrderCreate
from backend.app.service import ConflictError
from backend.app.security import (
    AuthenticatedIdentity, CognitoAuthenticationProvider, CognitoConfiguration,
    CognitoCustomerBinding, Role,
)
from backend.container.bootstrap import _cognito_authentication_configuration


ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_PhaseBPool"
CLIENT_ID = "phaseBClient_123"
SUBJECT = UUID("00000000-0000-4000-8000-000000000071")
CUSTOMER_ID = UUID("00000000-0000-4000-8000-000000000072")


def encoded(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class Bindings:
    def __init__(self, customer_id=CUSTOMER_ID):
        self.customer_id = customer_id

    def customer_id_for_subject(self, subject_id):
        return self.customer_id if subject_id == SUBJECT else None


class CognitoAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = self.key.public_key().public_numbers()
        self.jwks = {"keys": [{
            "kty": "RSA", "kid": "phase-b-key", "use": "sig", "alg": "RS256",
            "n": encoded(public.n.to_bytes((public.n.bit_length() + 7) // 8, "big")),
            "e": encoded(public.e.to_bytes((public.e.bit_length() + 7) // 8, "big")),
        }]}
        self.fetches = []
        self.configuration = CognitoConfiguration(
            issuer=ISSUER, client_id=CLIENT_ID,
            customer_group="customers", reviewer_group="reviewers", admin_group="admins",
        )

    def provider(self, *, bindings=None, now=2_000_000_000):
        def fetch(url):
            self.fetches.append(url)
            return self.jwks
        return CognitoAuthenticationProvider(
            self.configuration, bindings or Bindings(), jwks_fetcher=fetch, clock=lambda: now,
        )

    def test_admin_customer_only_bootstrap_configuration_is_supported(self):
        with patch.dict(os.environ, {
            "NOVAMIND_AUTH_MODE": "cognito",
            "NOVAMIND_COGNITO_ISSUER": ISSUER,
            "NOVAMIND_COGNITO_CLIENT_ID": CLIENT_ID,
            "NOVAMIND_COGNITO_CUSTOMER_GROUP": "customers",
            "NOVAMIND_COGNITO_ADMIN_GROUP": "admins",
            "NOVAMIND_COGNITO_CUSTOMER_BINDINGS": "[]",
        }, clear=True):
            configuration, startup_customers = _cognito_authentication_configuration()
        self.assertIsNotNone(configuration)
        self.assertEqual(configuration.reviewer_group, None)
        self.assertEqual(startup_customers, ())

    def token(self, **overrides):
        header = {"alg": "RS256", "typ": "JWT", "kid": "phase-b-key"}
        claims = {
            "iss": ISSUER, "client_id": CLIENT_ID, "token_use": "access",
            "sub": str(SUBJECT), "exp": 2_000_000_100, "cognito:groups": ["customers"],
        }
        claims.update(overrides)
        signed = f"{encoded(json.dumps(header, separators=(',', ':')).encode())}."
        signed += encoded(json.dumps(claims, separators=(",", ":")).encode())
        signature = self.key.sign(signed.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return f"{signed}.{encoded(signature)}"

    def test_valid_access_token_requires_signature_claims_and_binding(self):
        provider = self.provider()
        token = self.token()
        identity = provider.authenticate(token)
        self.assertEqual(identity.user_id, SUBJECT)
        self.assertEqual(identity.role, Role.CUSTOMER)
        self.assertEqual(identity.customer_id, CUSTOMER_ID)
        self.assertEqual(self.fetches, [f"{ISSUER}/.well-known/jwks.json"])
        self.assertEqual(provider.authenticate(token).customer_id, CUSTOMER_ID)
        self.assertEqual(len(self.fetches), 1, "cached JWKS should be reused")

    def test_invalid_tokens_and_groups_fail_closed(self):
        variants = (
            self.token(token_use="id"),
            self.token(client_id="another-client"),
            self.token(iss="https://cognito-idp.us-east-1.amazonaws.com/another_pool"),
            self.token(exp=1_999_999_999),
            self.token(exp=float("inf")),
            self.token(nbf=2_000_000_001),
            self.token(**{"cognito:groups": ["unknown"]}),
            self.token(**{"cognito:groups": ["customers", "admins"]}),
            self.token(sub=str(UUID(int=99)).upper()),
        )
        provider = self.provider()
        for token in variants:
            with self.subTest(token=token[:32]):
                self.assertIsNone(provider.authenticate(token))
        token = self.token()
        self.assertIsNone(provider.authenticate(token[:-2] + "AA"))
        self.assertIsNone(self.provider(bindings=Bindings(None)).authenticate(self.token()))
        reviewer = self.provider(bindings=Bindings(None)).authenticate(
            self.token(**{"cognito:groups": ["reviewers"]})
        )
        self.assertEqual((reviewer.role, reviewer.customer_id), (Role.REVIEWER, None))

    def test_sqlite_binding_survives_restart_and_cannot_be_reassigned(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "phase-b.sqlite"
            binding = CognitoCustomerBinding(subject_id=SUBJECT, customer_id=CUSTOMER_ID)
            first = create_app(
                sqlite_path=path, startup_customers=(Customer(id=CUSTOMER_ID, name="Bound customer"),),
                cognito_configuration=self.configuration, cognito_customer_bindings=(binding,),
                cognito_jwks_fetcher=lambda _url: self.jwks,
            )
            with TestClient(first) as client:
                response = client.get("/api/identity", headers={"Authorization": f"Bearer {self.token()}"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["identity"]["role"], "CUSTOMER")
                self.assertEqual(response.json()["identity"]["customer_id"], str(CUSTOMER_ID))
                with self.assertRaisesRegex(ConflictError, "already bound"):
                    first.state.cognito_customer_bindings.bind(CognitoCustomerBinding(
                        subject_id=UUID(int=73), customer_id=CUSTOMER_ID,
                    ))

            restarted = create_app(
                sqlite_path=path, cognito_configuration=self.configuration,
                cognito_jwks_fetcher=lambda _url: self.jwks,
            )
            with TestClient(restarted) as client:
                response = client.get("/api/identity", headers={"Authorization": f"Bearer {self.token()}"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["identity"]["customer_id"], str(CUSTOMER_ID))

    def test_cognito_configuration_requires_durable_persistence(self):
        with self.assertRaisesRegex(ValueError, "durable persistence"):
            create_app(cognito_configuration=self.configuration)

    def test_container_cognito_configuration_is_opt_in_and_local_file_is_rejected(self):
        environment = {
            "NOVAMIND_AUTH_MODE": "cognito",
            "NOVAMIND_COGNITO_ISSUER": ISSUER,
            "NOVAMIND_COGNITO_CLIENT_ID": CLIENT_ID,
            "NOVAMIND_COGNITO_CUSTOMER_GROUP": "customers",
            "NOVAMIND_COGNITO_REVIEWER_GROUP": "reviewers",
            "NOVAMIND_COGNITO_ADMIN_GROUP": "admins",
            "NOVAMIND_COGNITO_CUSTOMER_BINDINGS": json.dumps([
                {"subject_id": str(SUBJECT), "customer_id": str(CUSTOMER_ID)},
            ]),
        }
        with patch.dict(os.environ, environment, clear=True):
            configuration, bindings = _cognito_authentication_configuration()
        self.assertEqual(configuration, self.configuration)
        self.assertEqual(bindings, (CognitoCustomerBinding(subject_id=SUBJECT, customer_id=CUSTOMER_ID),))
        with patch.dict(os.environ, {**environment, "NOVAMIND_AUTH_FILE": "ignored.json"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "authentication configuration is invalid"):
                _cognito_authentication_configuration()

    def test_cognito_auto_dispatcher_uses_server_staff_and_pauses_for_hitl(self):
        """A Cognito customer can start only a durable, staff-owned HITL run."""
        dispatcher_id = UUID("00000000-0000-4000-8000-000000000073")
        admin_id = UUID("00000000-0000-4000-8000-000000000074")

        class ReviewProvider:
            def generate(self, request, *, response_schema):
                payload = json.loads(request.messages[-1].content)
                return LLMResponse(text=json.dumps({"decision": {"kind": "tool",
                    "name": "create_resolution_proposal", "arguments": {
                        "case_id": payload["request"]["case_id"], "action": "replacement",
                        "rationale": "A reviewer must decide this proposal.",
                    }}}), finish_reason="stop")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "cognito-auto.sqlite"
            app = create_app(
                sqlite_path=path,
                startup_customers=(Customer(id=CUSTOMER_ID, name="Bound customer"),),
                cognito_configuration=self.configuration,
                cognito_customer_bindings=(CognitoCustomerBinding(
                    subject_id=SUBJECT, customer_id=CUSTOMER_ID,
                ),),
                cognito_jwks_fetcher=lambda _url: self.jwks,
                local_provider_factory=ReviewProvider,
                automatic_dispatcher=True,
                dispatcher_identity=AuthenticatedIdentity(user_id=dispatcher_id, role=Role.REVIEWER),
            )
            app.state.investigation_control.set_mode(
                InvestigationMode.AUTO, expected_version=1, admin_user_id=admin_id,
                idempotency_key=uuid4(),
            )
            order = app.state.case_service.create_order(OrderCreate(
                customer_id=CUSTOMER_ID,
                items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
            ))
            with TestClient(app) as client:
                response = client.post("/api/my/cases", headers={
                    "Authorization": f"Bearer {self.token()}", "Idempotency-Key": str(uuid4()),
                }, json={"order_id": str(order.id), "subject": "Damaged laptop",
                         "description": "The laptop arrived damaged."})
                self.assertEqual(response.status_code, 201, response.text)
                case_id = UUID(response.json()["id"])
                for _ in range(100):
                    job = app.state.investigation_jobs.get_by_case(case_id)
                    if job.execution_attempt_id is not None:
                        break
                    time.sleep(0.01)
                else:
                    self.fail("Cognito automatic dispatcher did not create an attempt")
                self.assertEqual(job.claimed_by, dispatcher_id)
                self.assertEqual(job.status, InvestigationJobStatus.CLAIMED)
                metadata = SQLiteWorkflowMetadataRepository(SQLitePersistence(path))
                for _ in range(100):
                    if metadata.get(job.id).status.value == "REVIEW_REQUIRED":
                        break
                    time.sleep(0.01)
                self.assertEqual(metadata.get(job.id).status.value, "REVIEW_REQUIRED")
                proposal = app.state.proposal_service.list_for_case(case_id)[0]
                self.assertEqual(proposal.status.value, "PENDING_REVIEW")


if __name__ == "__main__":
    unittest.main()
