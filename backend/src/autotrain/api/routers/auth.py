"""Auth routes: magic-link login, session issue, and the signed-in user's
profile. The three builders below fetch configured dependencies or refuse
with a 503 scoped to the endpoint — the worker refuses to boot without its
transport, but the API has other jobs to keep doing."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from autotrain.api.deps import ConnDep, UserIdDep
from autotrain.api.schemas import LoginRequest, LoginVerify, SessionOut, UserOut
from autotrain.core.config import get_settings
from autotrain.modules.identity import service
from autotrain.sources.email import LogEmailSender

router = APIRouter(prefix="/auth", tags=["auth"])


def _email_sender() -> service.EmailSender:
    """The configured email transport — 'log' in development."""
    settings = get_settings()
    if settings.email_sender == "log":
        return LogEmailSender()
    raise HTTPException(status_code=503, detail="no email sender configured")


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


@router.post("/login/request", status_code=204)
def request_login(payload: LoginRequest, conn: ConnDep) -> None:
    """Always 204, account or not — the enumeration reasoning lives in
    identity.request_login; this route adds nothing that could leak."""
    service.request_login(conn, payload.email, _email_sender(), app_base_url=_app_base_url())


@router.post("/login/verify")
def verify_login(payload: LoginVerify, conn: ConnDep) -> SessionOut:
    """Exchange a clicked link for a session. Every failure is the same 401."""
    user_id = service.verify_login(conn, payload.token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid token")
    token = service.issue_session_token(user_id, secret=_jwt_secret())
    return SessionOut(access_token=token)


@router.get("/me")
def me(conn: ConnDep, user_id: UserIdDep) -> UserOut:
    """Who the bearer token belongs to, answered from the verified token —
    the honest version of the user id SessionOut deliberately omits. 404
    when a still-valid session names an erased account."""
    profile = service.user_profile(conn, user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="unknown user")
    return UserOut.model_validate(profile)
