"""Settings' production lockdown: a deployment that says production refuses
to boot while any development setting is still in place, and names every
problem at once. Local stays permissive. Constructed directly with the .env
file switched off, so the developer's own settings never reach the test."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsConfigDict

from autotrain.core.config import Settings

_DB = "postgresql://x/y"


class _Settings(Settings):
    """Settings with the .env file switched off; everything else inherited."""

    model_config = SettingsConfigDict(env_file=None)


def test_production_refuses_every_development_setting_at_once() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _Settings(
            database_url=_DB,
            environment="production",
            jwt_secret=SecretStr("short"),
            session_cookie_secure=False,
            email_sender="log",
            push_sender="log",
            cors_origins=["http://localhost:3000"],
            app_base_url="http://localhost:3000",
            inbound_email_domain="in.autotrain.app",
            intake_secret=None,
        )
    message = str(excinfo.value)
    for setting in (
        "JWT_SECRET",
        "SESSION_COOKIE_SECURE",
        "EMAIL_SENDER",
        "PUSH_SENDER",
        "CORS_ORIGINS",
        "APP_BASE_URL",
        "INTAKE_SECRET",
    ):
        assert setting in message, setting

    # The same deployment with every problem fixed boots...
    _Settings(
        database_url=_DB,
        environment="production",
        jwt_secret=SecretStr("x" * 32),
        session_cookie_secure=True,
        email_sender="none",
        push_sender="none",
        cors_origins=["https://app.autotrain.example"],
        app_base_url="https://app.autotrain.example",
        inbound_email_domain="in.autotrain.app",
        intake_secret=SecretStr("s"),
    )
    # ...and local never minds any of it.
    _Settings(database_url=_DB, environment="local", jwt_secret=SecretStr("short"))


def test_resend_needs_a_key_and_a_from_address() -> None:
    """The same rule the HSP and Claude sources get: a transport selected
    without what it needs is refused at boot, in every process, rather than
    found by the first person who cannot sign in. Both missing settings are
    named at once, and blank counts as absent — an interpolated-but-unset
    env var arrives as '' and would otherwise sail past an `is None` check
    and send an unauthenticated request to the provider on every login."""
    with pytest.raises(ValidationError) as excinfo:
        _Settings(database_url=_DB, email_sender="resend")
    message = str(excinfo.value)
    assert "RESEND_API_KEY" in message
    assert "EMAIL_FROM" in message

    with pytest.raises(ValidationError):
        _Settings(
            database_url=_DB,
            email_sender="resend",
            resend_api_key=SecretStr("   "),
            email_from="AutoTrain <login@in.example.com>",
        )

    # Configured, it boots — and, unlike 'log', it is a transport production
    # will accept, which is the whole point of adding it.
    _Settings(
        database_url=_DB,
        environment="production",
        jwt_secret=SecretStr("x" * 32),
        session_cookie_secure=True,
        email_sender="resend",
        resend_api_key=SecretStr("re_live_key"),
        email_from="AutoTrain <login@in.example.com>",
        push_sender="none",
        # Pinned rather than inherited: the suite's environment sets an http
        # app base url, and the lockdown would refuse this on that instead.
        app_base_url="https://app.autotrain.example",
    )
