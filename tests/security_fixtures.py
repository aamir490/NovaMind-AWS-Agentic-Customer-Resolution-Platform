"""Explicit local credentials for earlier authorized-behavior regression fixtures."""

from uuid import UUID

from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, authenticated


ADMIN_ID = UUID("00000000-0000-4000-8000-000000000013")
ADMIN_TOKEN = "unit-test-admin-token-only"


def admin_provider():
    return LocalAuthenticationProvider((LocalCredential(token=ADMIN_TOKEN,
        identity=AuthenticatedIdentity(user_id=ADMIN_ID, role="ADMIN")),))


def admin_scope():
    return authenticated(admin_provider(), ADMIN_TOKEN)


def call_as_admin(function, *args):
    with admin_scope():
        return function(*args)
