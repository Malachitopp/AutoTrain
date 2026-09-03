from __future__ import annotations

from fastapi import APIRouter, HTTPException

from autotrain.api.deps import ConnDep
from autotrain.api.schemas import LoginRequest, LoginVerify, SessionOut
from autotrain.core.config import get_settings
from autotrain.modules.identity import service
from autotrain.sources.email import LogEmailSender


router = APIRouter(prefix="/auth", tags=["auth"])


def _email_sender() -> service.EmailSender:
    settings = get_settings()
    if settings.email_sender == "log":
        return LogEmailSender()
    raise HTTPException(status_code=503, detail="no email sender configured")


def _jwt_secret() -> str:
    settings = get_settings()
    if settings.jwt_secret is None:
        raise HTTPException(status_code=503, detail="no JWT secret configured")
    return settings.jwt_secret.get_secret_value()


@router.post("/login/request", status_code=204)
def request_login(payload: LoginRequest, conn: ConnDep) -> None:

    service.request_login(conn, payload.email, _email_sender())


@router.post("/login/verify")
def verify_login(payload: LoginVerify, conn: ConnDep) -> SessionOut:
    user_id = service.verify_login(conn, payload.token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid token")
    token = service.issue_session_token(user_id, secret=_jwt_secret())
    return SessionOut(access_token=token)
