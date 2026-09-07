"""Auth routes: magic-link login, session issue, sign-out (this browser, or
every session), the signed-in user's profile, and erasure. The three
builders below fetch configured dependencies or refuse with a 503 scoped to
the endpoint — the worker refuses to boot without its transport, but the
API has other jobs to keep doing.

A session travels two ways. The web app gets it as an httpOnly cookie set
here, which page scripts cannot read (so an XSS hole cannot lift it) and the
browser sends by itself; a client that cannot hold cookies (the mobile app,
later) keeps the same token from the response body and presents it as a
bearer header. deps.current_user_id accepts either.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, HTTPException, Response

from autotrain.api.deps import SESSION_COOKIE, ConnDep, UserIdDep
from autotrain.api.schemas import LoginRequest, LoginVerify, SessionOut, UserOut
from autotrain.core.config import get_settings
from autotrain.modules.identity import service
from autotrain.sources.email import LogEmailSender, ResendEmailSender

router = APIRouter(prefix="/auth", tags=["auth"])


def _email_sender() -> service.EmailSender:
    """The configured email transport — 'log' in development, 'resend' in
    production. One branch per transport, the same shape as the scheduler's
    reader builder; adding one touches this function and sources/."""
    settings = get_settings()
    if settings.email_sender == "log":
        return LogEmailSender()
    if settings.email_sender == "resend":
        return _resend_sender()
    raise HTTPException(status_code=503, detail="no email sender configured")


@lru_cache(maxsize=1)
def _resend_sender() -> ResendEmailSender:
    """Built once and kept for the life of the process.

    Unlike LogEmailSender, this one owns an HTTP client, and a client built
    per request throws away a warm TLS connection and re-handshakes on
    every single login — three extra round trips to the provider while a
    user waits on the button they just pressed.

    Settings already refuses to boot with 'resend' and no credentials, so
    the check below is the type checker's and not a second policy; it stays
    a 503 rather than a crash because the API has other jobs to keep doing.
    """
    settings = get_settings()
    if settings.resend_api_key is None or not settings.email_from:
        raise HTTPException(status_code=503, detail="no email sender configured")
    return ResendEmailSender.from_api_key(
        settings.resend_api_key.get_secret_value(), from_address=settings.email_from
    )


def _jwt_secret() -> str:
    """The session signing secret, unwrapped from its SecretStr."""
    settings = get_settings()
    if settings.jwt_secret is None:
        raise HTTPException(status_code=503, detail="no JWT secret configured")
    return settings.jwt_secret.get_secret_value()


def _app_base_url() -> str:
    """The frontend origin login links point at. Blank counts as unset: an
    interpolated-but-unset env var arrives as '' (config's HSP check explains),
    and a host-less link would fail silently in the inbox."""
    settings = get_settings()
    if not settings.app_base_url:
        raise HTTPException(status_code=503, detail="no app base URL configured")
    return settings.app_base_url


def _set_session_cookie(response: Response, token: str) -> None:
    """The web session. HttpOnly: no script reads it. SameSite=Lax: a page
    on another site cannot make the browser send it with a cross-site POST
    (the CSRF that would otherwise come free with cookies), while the app's
    own origin — the same site as the API in every deployment — can. Secure
    is a setting because a local http development server cannot set it."""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=int(service.SESSION_TTL.total_seconds()),
        httponly=True,
        secure=get_settings().session_cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE,
        httponly=True,
        secure=get_settings().session_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/login/request", status_code=204)
def request_login(payload: LoginRequest, conn: ConnDep) -> None:
    """Always 204, account or not — the enumeration reasoning lives in
    identity.request_login; this route adds nothing that could leak."""
    settings = get_settings()
    try:
        service.request_login(
            conn,
            payload.email,
            _email_sender(),
            app_base_url=_app_base_url(),
            per_email_daily_cap=settings.login_requests_per_email_per_day,
            daily_cap=settings.login_requests_per_day,
        )
    except service.LoginRateLimited as exc:
        # One 429 for both caps, saying nothing about which was hit: which
        # one you reached is a fact about other people's traffic. No
        # Retry-After — the window is rolling, so any number here would be a
        # guess, and the middleware's fixed-window limiter is the one that
        # can answer that honestly.
        raise HTTPException(
            status_code=429, detail="too many login requests; try again later"
        ) from exc
    except service.EmailDeliveryError as exc:
        # 502, not 500: the fault is the provider's, and the distinction is
        # what sends whoever is on call to their status page instead of our
        # stack traces. The middleware rolls the transaction back on any
        # >=400 response, so the login token this request minted goes with
        # it and no token survives the email that was meant to carry it.
        #
        # This is the one answer this endpoint can give other than 204, and
        # it does not break the enumeration property: a provider refuses for
        # a bad key, an unverified domain or a malformed address — never for
        # whether the address has an account here. The detail is deliberately
        # ours rather than the provider's, which can name our sending domain.
        raise HTTPException(status_code=502, detail="could not send the login email") from exc


@router.post("/login/verify")
def verify_login(payload: LoginVerify, conn: ConnDep, response: Response) -> SessionOut:
    """Exchange a clicked link for a session. Every failure is the same 401.
    The session goes out twice: as the httpOnly cookie the web app relies
    on, and in the body for a client that holds its own token."""
    user_id = service.verify_login(conn, payload.token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid token")
    token = service.issue_session_token(user_id, secret=_jwt_secret())
    _set_session_cookie(response, token)
    return SessionOut(access_token=token)


@router.post("/logout", status_code=204)
def logout(response: Response) -> None:
    """Sign this browser out: drop the cookie. No session required — a
    browser that is already signed out gets the same 204. The token itself
    stays valid until it expires (sessions are stateless); to kill every
    token, the user signs out everywhere."""
    _clear_session_cookie(response)


@router.post("/sessions/revoke", status_code=204)
def revoke_sessions(conn: ConnDep, user_id: UserIdDep, response: Response) -> None:
    """Sign out everywhere: every session issued before now, this one
    included, is refused from here on (users.sessions_invalid_before, 0014).
    The answer to a lost phone or a session that may have been stolen."""
    service.revoke_sessions(conn, user_id)
    _clear_session_cookie(response)


@router.get("/me")
def me(conn: ConnDep, user_id: UserIdDep) -> UserOut:
    """Who the bearer token belongs to, answered from the verified token —
    the honest version of the user id SessionOut deliberately omits. 404
    when a still-valid session names an erased account."""
    profile = service.user_profile(conn, user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="unknown user")
    return UserOut.model_validate(profile)


@router.delete("/me", status_code=204)
def erase_me(conn: ConnDep, user_id: UserIdDep, response: Response) -> None:
    """Erase the account (GDPR): everything that identifies the person goes,
    the claims filed on their behalf stay for audit under an anonymous id
    (identity.erase_user has the full list). Every session dies with it."""
    if not service.erase_user(conn, user_id):
        # Unreachable past the gate, which already refused an erased account.
        raise HTTPException(status_code=404, detail="unknown user")
    _clear_session_cookie(response)
