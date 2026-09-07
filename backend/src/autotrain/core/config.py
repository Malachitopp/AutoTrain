"""Process configuration.

All four process types (api, ingestor, worker, scheduler) share one image and one
settings object; they differ only in entrypoint. Everything comes from environment
variables so the same image can run locally, in staging and in production with no
code change — in AWS these are injected from SSM Parameter Store (ARCHITECTURE §7).

`extra="forbid"` is deliberate: a typo'd variable in a task definition should fail
the container at boot, loudly, rather than silently fall back to a default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ROOT = Path(__file__).resolve().parents[3]

# RFC 7518's floor for an HS256 key; pyjwt warns below it. Enforced only in
# production (_production_lockdown), so a local .env can be anything.
_SECRET_MIN_BYTES = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUTOTRAIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    environment: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"
    # 'text' for a terminal; 'json' for a log store (core/observability.py).
    log_format: Literal["text", "json"] = "text"

    database_url: str
    db_pool_min_size: int = Field(default=1, ge=0)
    db_pool_max_size: int = Field(default=10, ge=1)
    # Fail a request rather than queue behind an exhausted pool forever.
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)

    migrations_dir: Path = _BACKEND_ROOT / "migrations"

    # Bind address for the api process. Loopback by default so a dev machine
    # never exposes the stub-auth API by accident; containers set 0.0.0.0.
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    # Ingestor (delay sweep) — process type 2. 'none' makes the ingestor
    # refuse to start; 'hsp' is the real source (sources/hsp.py).
    arrivals_source: Literal["none", "hsp"] = "none"
    # Rail Data Portal account for HSP (free). SecretStr so a logged or
    # printed Settings object shows '**********', never the password.
    hsp_email: str | None = None
    hsp_password: SecretStr | None = None
    ingestor_batch_size: int = Field(default=200, ge=1)
    ingestor_interval_seconds: float = Field(default=900.0, gt=0)
    # Wait this long after scheduled arrival before asking the source — HSP
    # publishes next-day, so asking immediately is a guaranteed miss.
    ingestor_arrival_lag_minutes: int = Field(default=120, ge=0)
    # A journey still without data this many days after travel is marked
    # 'unmatched' and leaves the sweep.
    ingestor_give_up_days: int = Field(default=7, ge=1)

    # Worker (notification delivery) — process type 3. 'none' makes the worker
    # refuse to start; 'log' delivers to the process log — the development
    # transport until FCM/APNs arrive with the app.
    push_sender: Literal["none", "log"] = "none"
    worker_batch_size: int = Field(default=100, ge=1)
    # Shorter than the sweeps: a push is the product's magic moment, and its
    # value decays by the minute. Each idle poll is one indexed no-op query.
    worker_interval_seconds: float = Field(default=60.0, gt=0)

    # Scheduler (claims jobs) — process type 4. Interval matches the ingestor:
    # both jobs are cheap no-op sweeps when their partial-index work queues are
    # empty, so a short interval costs almost nothing.
    scheduler_batch_size: int = Field(default=200, ge=1)
    scheduler_interval_seconds: float = Field(default=900.0, gt=0)

    # Auth and the browser frontend (identity module, api process).
    # 'none' makes /auth/login/request refuse; 'log' writes the email to the
    # process log (development only — the lockdown below refuses it in
    # production); 'resend' delivers through Resend's HTTPS API.
    email_sender: Literal["none", "log", "resend"] = "none"
    # Resend's API key. Whoever holds it can send mail as our domain, which
    # is a phishing kit — it is a secret on the level of the JWT one.
    resend_api_key: SecretStr | None = None
    # The From address every AutoTrain email is sent as. Must be on a domain
    # verified with the provider, or every send is refused. RFC 5322 display
    # form is allowed and preferred, because "AutoTrain <...>" is what the
    # recipient's client shows in the sender column:
    #   AutoTrain <login@in.example.com>
    email_from: str | None = None
    # Signs session JWTs. SecretStr: a logged Settings shows '**********'.
    # Whoever holds it can mint a session for any user.
    jwt_secret: SecretStr | None = None
    # The frontend origin magic links point at (<app_base_url>/login?token=...).
    # None on purpose rather than a localhost default: an email carrying a
    # localhost link in production is a silent failure; the 503 is loud. The
    # route treats blank as unset too — an interpolated-but-unset env var
    # arrives as '' (see the HSP check below).
    app_base_url: str | None = None
    # Browser origins allowed to call the API (CORS). Empty by default: no
    # browser may call us until a deployment says so; curl and servers are
    # unaffected, since CORS is enforced only by browsers. The env value is
    # JSON, because the field is a list: AUTOTRAIN_CORS_ORIGINS=["http://..."]
    cors_origins: list[str] = Field(default_factory=list)
    # The web session cookie's Secure flag (routers/auth.py). False so a local
    # http development server can set the cookie at all; every https
    # deployment sets it True.
    session_cookie_secure: bool = False

    # Ticket-email intake (journeys module, ARCHITECTURE §6a).
    # The domain users forward ticket emails to: their address is
    # tickets-<code>@<this>. None on purpose (the address route answers 503)
    # rather than a placeholder that would be shown to users and bounce.
    inbound_email_domain: str | None = None
    # The shared secret the mail provider's webhook presents on every
    # POST /intake/email. Whoever holds it can queue emails for any user.
    intake_secret: SecretStr | None = None
    # Which reader turns a stored email into a ticket. 'none' (default) means
    # the scheduler skips the intake job; 'claude' needs the API key below.
    ticket_extractor: Literal["none", "claude"] = "none"
    anthropic_api_key: SecretStr | None = None
    ticket_extractor_model: str = "claude-opus-5"
    # Emails per scheduler pass. Small on purpose: one email is one model
    # call of a few seconds, and a pass should finish well inside the interval.
    intake_batch_size: int = Field(default=20, ge=1)
    # The whole pass, not the page. One email is one model call of a few
    # seconds, so an unbounded pass over a long backlog would run for hours
    # and outlast the interval. The queue is durable: whatever is left waits
    # for the next pass.
    intake_max_per_pass: int = Field(default=200, ge=1)
    # The door's per-user cap (journeys.intake.screen): emails stored for one
    # user in a rolling 24 hours. Generous — a year of tickets forwarded in
    # one sitting is the honest case it must survive — and the excess is
    # stored as rejected with the reason, so the user learns why.
    intake_daily_cap: int = Field(default=50, ge=1)
    # How long a decided email's raw body is kept before the scheduler's
    # retention job blanks it: the 28-day claim window, doubled for slack.
    intake_body_retention_days: int = Field(default=60, ge=1)

    # Only read by the integration test suite, which drops and recreates it.
    test_database_url: str | None = None

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _hsp_requires_credentials(self) -> Settings:
        """arrivals_source=hsp with absent or BLANK credentials is an invalid
        deployment — fail at boot, loudly, in every process (same philosophy
        as extra="forbid"). Blank matters: an interpolated-but-unset env var
        arrives as '', passes an `is None` check, and would otherwise send
        per-journey 401s that age real journeys into 'unmatched'."""
        if self.arrivals_source == "hsp":
            password = self.hsp_password.get_secret_value() if self.hsp_password else ""
            if not self.hsp_email or not password:
                raise ValueError(
                    "AUTOTRAIN_ARRIVALS_SOURCE=hsp requires AUTOTRAIN_HSP_EMAIL and "
                    "AUTOTRAIN_HSP_PASSWORD (free Rail Data Portal account: "
                    "https://raildata.org.uk)"
                )
        return self

    @model_validator(mode="after")
    def _claude_reader_requires_key(self) -> Settings:
        """Same shape as the HSP rule: ticket_extractor=claude with an absent
        or blank key is an invalid deployment, refused at boot."""
        if self.ticket_extractor == "claude":
            key = self.anthropic_api_key.get_secret_value() if self.anthropic_api_key else ""
            if not key:
                raise ValueError(
                    "AUTOTRAIN_TICKET_EXTRACTOR=claude requires AUTOTRAIN_ANTHROPIC_API_KEY"
                )
        return self

    @model_validator(mode="after")
    def _resend_requires_credentials(self) -> Settings:
        """Same shape as the HSP and Claude rules: a transport selected
        without what it needs is an invalid deployment, refused at boot in
        every process rather than discovered by the first person who cannot
        log in. Blank counts as absent for the same reason as everywhere
        else — an interpolated-but-unset env var arrives as ''."""
        if self.email_sender == "resend":
            key = self.resend_api_key.get_secret_value() if self.resend_api_key else ""
            missing = [
                name
                for name, value in (
                    ("AUTOTRAIN_RESEND_API_KEY", key),
                    ("AUTOTRAIN_EMAIL_FROM", self.email_from or ""),
                )
                if not value.strip()
            ]
            if missing:
                raise ValueError("AUTOTRAIN_EMAIL_SENDER=resend requires " + " and ".join(missing))
        return self

    @model_validator(mode="after")
    def _production_lockdown(self) -> Settings:
        """AUTOTRAIN_ENVIRONMENT=production refuses to boot while any
        development setting is still in place. Each of these is fine locally
        and a silent hole in production: a short signing secret, login links
        written to the log, a cookie that travels over http, a browser
        origin that is not https. Every problem is listed at once, so a
        deployment is fixed in one round, not one boot per setting."""
        if not self.is_production:
            return self
        problems: list[str] = []
        secret = self.jwt_secret.get_secret_value() if self.jwt_secret else ""
        if len(secret.encode()) < _SECRET_MIN_BYTES:
            problems.append(f"AUTOTRAIN_JWT_SECRET must be at least {_SECRET_MIN_BYTES} bytes")
        if not self.session_cookie_secure:
            problems.append("AUTOTRAIN_SESSION_COOKIE_SECURE must be true")
        if self.email_sender == "log":
            problems.append("AUTOTRAIN_EMAIL_SENDER=log would write login links to the log")
        if self.push_sender == "log":
            problems.append("AUTOTRAIN_PUSH_SENDER=log would deliver nothing")
        for origin in self.cors_origins:
            if not origin.startswith("https://"):
                problems.append(f"AUTOTRAIN_CORS_ORIGINS entry {origin!r} is not https")
        if self.app_base_url and not self.app_base_url.startswith("https://"):
            problems.append("AUTOTRAIN_APP_BASE_URL is not https")
        intake_secret = self.intake_secret.get_secret_value() if self.intake_secret else ""
        if self.inbound_email_domain and not intake_secret:
            problems.append(
                "AUTOTRAIN_INTAKE_SECRET is required once AUTOTRAIN_INBOUND_EMAIL_DOMAIN is set"
            )
        if problems:
            raise ValueError(
                "AUTOTRAIN_ENVIRONMENT=production refuses to start: " + "; ".join(problems)
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so config is parsed once per process, not once per import."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
