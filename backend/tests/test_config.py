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

    # 'none' is refused too, and separately: it is not a development
    # setting but an absent one, and magic links are the only way in — a
    # production API without a sender boots cleanly and 503s every login.
    with pytest.raises(ValidationError) as excinfo:
        _Settings(
            database_url=_DB,
            environment="production",
            jwt_secret=SecretStr("x" * 32),
            session_cookie_secure=True,
            email_sender="none",
            app_base_url="https://app.autotrain.example",
        )
    assert "EMAIL_SENDER=none" in str(excinfo.value)

    # The same deployment with every problem fixed boots...
    _Settings(
        database_url=_DB,
        environment="production",
        jwt_secret=SecretStr("x" * 32),
        session_cookie_secure=True,
        email_sender="resend",
        resend_api_key=SecretStr("re_live_key"),
        email_from="AutoTrain <login@in.example.com>",
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


def test_the_per_inbox_cap_must_sit_under_the_daily_one() -> None:
    """Otherwise the daily cap fires first for everyone and the per-inbox
    rule is decoration: every refusal a person ever saw would be about other
    people's traffic. Boot-time, like every setting whose only failure mode
    is silent."""
    with pytest.raises(ValidationError) as excinfo:
        _Settings(database_url=_DB, login_requests_per_email_per_day=80, login_requests_per_day=80)
    assert "PER_EMAIL_PER_DAY" in str(excinfo.value)
    _Settings(database_url=_DB, login_requests_per_email_per_day=5, login_requests_per_day=80)


def test_a_mailbox_needs_a_server_a_login_and_an_owner() -> None:
    """The fourth of the same rule (HSP, Claude, Resend, and now this): a
    source selected without what it needs is an invalid deployment, refused
    at boot in every process, with everything missing named at once.

    The owner address is the one that is easy to miss and the one that
    matters most. Nothing in a mailbox says whose it is, and a journey
    belonging to nobody is a journey no claim can ever be filed for — so
    the poll is not allowed to start guessing.
    """
    with pytest.raises(ValidationError) as excinfo:
        _Settings(database_url=_DB, mailbox_source="imap")
    message = str(excinfo.value)
    assert "IMAP_HOST" in message
    assert "IMAP_USERNAME" in message
    assert "IMAP_PASSWORD" in message
    assert "MAILBOX_OWNER_EMAIL" in message

    # Blank counts as absent, as everywhere else: an interpolated-but-unset
    # env var arrives as '' and would otherwise sail past an `is None` check
    # and try to log in to a mail server with an empty password.
    with pytest.raises(ValidationError):
        _Settings(
            database_url=_DB,
            mailbox_source="imap",
            imap_host="imap.gmail.com",
            imap_username="you@example.com",
            imap_password=SecretStr("   "),
            mailbox_owner_email="you@example.com",
        )

    settings = _Settings(
        database_url=_DB,
        mailbox_source="imap",
        imap_host="imap.gmail.com",
        imap_username="you@example.com",
        imap_password=SecretStr("app-password"),
        mailbox_owner_email="you@example.com",
    )
    # The default folder reads everything; a Gmail label is how it narrows.
    assert settings.imap_folder == "INBOX"
    assert settings.imap_port == 993
