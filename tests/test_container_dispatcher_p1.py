"""Offline P1-1C3 bootstrap configuration coverage; no provider calls."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

from backend.container.bootstrap import (
    _authentication_provider, _automatic_dispatcher_configuration, create_container_app,
)
from backend.app.security import AuthenticatedIdentity, Role


OFFLINE = {
    "NOVAMIND_LLM_PROVIDER": "local_scripted",
    "NOVAMIND_OFFLINE_TESTING": "true",
}
CUSTOMER_ID = UUID(int=27)
CUSTOMER_USER_ID = UUID(int=28)
REVIEWER_USER_ID = UUID(int=29)


class ContainerDispatcherConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        auth = self.directory / "auth.json"
        auth.write_text(json.dumps([
            {"token": "container-dispatch-customer-token", "identity": {
                "user_id": str(CUSTOMER_USER_ID), "role": "CUSTOMER",
                "customer_id": str(CUSTOMER_ID),
            }},
            {"token": "container-dispatch-reviewer-token", "identity": {
                "user_id": str(REVIEWER_USER_ID), "role": "REVIEWER",
            }},
        ]), encoding="utf-8")
        os.environ["NOVAMIND_AUTH_FILE"] = str(auth)

    def test_default_off_and_staff_resolution(self):
        auth, _ = _authentication_provider()
        self.assertEqual(_automatic_dispatcher_configuration(auth), (False, None))
        os.environ.update({
            "NOVAMIND_AUTOMATIC_DISPATCHER": "true",
            "NOVAMIND_DISPATCHER_USER_ID": str(REVIEWER_USER_ID),
        })
        enabled, identity = _automatic_dispatcher_configuration(auth)
        self.assertTrue(enabled)
        self.assertEqual(identity.user_id, REVIEWER_USER_ID)
        self.assertEqual(identity.role, "REVIEWER")

    def test_invalid_or_customer_identity_fails_closed(self):
        auth, _ = _authentication_provider()
        invalid = (
            {"NOVAMIND_AUTOMATIC_DISPATCHER": "TRUE", "NOVAMIND_DISPATCHER_USER_ID": str(REVIEWER_USER_ID)},
            {"NOVAMIND_AUTOMATIC_DISPATCHER": "true"},
            {"NOVAMIND_AUTOMATIC_DISPATCHER": "true", "NOVAMIND_DISPATCHER_USER_ID": str(CUSTOMER_USER_ID)},
            {"NOVAMIND_AUTOMATIC_DISPATCHER": "false", "NOVAMIND_DISPATCHER_USER_ID": str(REVIEWER_USER_ID)},
            {"NOVAMIND_DISPATCHER_USER_ID": str(REVIEWER_USER_ID)},
        )
        for values in invalid:
            with self.subTest(values=values), patch.dict(os.environ, values, clear=False):
                with self.assertRaisesRegex(RuntimeError, "^Container automatic dispatcher configuration is invalid$"):
                    _automatic_dispatcher_configuration(auth)

    def test_cognito_mode_uses_only_an_explicit_server_staff_identity(self):
        values = {
            "NOVAMIND_AUTOMATIC_DISPATCHER": "true",
            "NOVAMIND_DISPATCHER_USER_ID": str(REVIEWER_USER_ID),
            "NOVAMIND_DISPATCHER_ROLE": "REVIEWER",
        }
        with patch.dict(os.environ, values, clear=True):
            enabled, identity = _automatic_dispatcher_configuration(None, cognito_mode=True)
        self.assertTrue(enabled)
        self.assertEqual(identity, AuthenticatedIdentity(user_id=REVIEWER_USER_ID, role=Role.REVIEWER))
        for invalid in (
            {**values, "NOVAMIND_DISPATCHER_ROLE": "CUSTOMER"},
            {"NOVAMIND_AUTOMATIC_DISPATCHER": "true", "NOVAMIND_DISPATCHER_USER_ID": str(REVIEWER_USER_ID)},
        ):
            with self.subTest(invalid=invalid), patch.dict(os.environ, invalid, clear=True):
                with self.assertRaisesRegex(RuntimeError, "^Container automatic dispatcher configuration is invalid$"):
                    _automatic_dispatcher_configuration(None, cognito_mode=True)

    def test_container_passes_the_resolved_identity(self):
        database = self.directory / "novamind.sqlite"
        os.environ.update({
            **OFFLINE,
            "NOVAMIND_SQLITE_PATH": str(database),
            "NOVAMIND_AUTOMATIC_DISPATCHER": "true",
            "NOVAMIND_DISPATCHER_USER_ID": str(REVIEWER_USER_ID),
        })
        app = create_container_app()
        self.assertIsNotNone(app.state.investigation_dispatcher)
        self.assertEqual(app.state.investigation_dispatcher._worker_id, REVIEWER_USER_ID)
        app.state.investigation_dispatcher.close()
        app.state.frontend_runtime.close()


if __name__ == "__main__":
    unittest.main()
