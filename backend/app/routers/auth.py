"""단일 사용자 인증: 최초 비밀번호 설정 → 로그인 → 서명 쿠키."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Response, status
from pydantic import BaseModel, Field

from .. import db, security
from ..config import env

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_LEN = 4


class PasswordIn(BaseModel):
    password: str = Field(min_length=1)


class ChangeIn(BaseModel):
    current_password: str = ""
    new_password: str = Field(min_length=1)


def _set_cookie(response: Response) -> None:
    days = int(db.get_setting("session_days") or 30)
    response.set_cookie(
        env.session_cookie,
        security.issue_session(),
        max_age=days * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )


@router.get("/me")
def me(session: Annotated[str | None, Cookie(alias=env.session_cookie)] = None) -> dict:
    configured = security.is_configured()
    days = int(db.get_setting("session_days") or 30)
    return {
        "configured": configured,
        "authenticated": configured and security.valid_session(session, days),
    }


@router.post("/setup")
def setup(body: PasswordIn, response: Response) -> dict:
    """최초 1회 비밀번호 설정. 이미 설정돼 있으면 거부한다."""
    if security.is_configured():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="이미 설정된 계정입니다")
    if len(body.password) < MIN_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"비밀번호는 {MIN_LEN}자 이상이어야 합니다"
        )
    security.set_password(body.password)
    _set_cookie(response)
    return {"ok": True}


@router.post("/login")
def login(body: PasswordIn, response: Response) -> dict:
    if not security.is_configured():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="초기 설정이 필요합니다")
    if not security.check_password(body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="비밀번호가 올바르지 않습니다")
    _set_cookie(response)
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(env.session_cookie, path="/")
    return {"ok": True}


@router.post("/password")
def change_password(
    body: ChangeIn,
    response: Response,
    session: Annotated[str | None, Cookie(alias=env.session_cookie)] = None,
) -> dict:
    days = int(db.get_setting("session_days") or 30)
    if not security.valid_session(session, days):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="로그인이 필요합니다")
    if not security.check_password(body.current_password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="현재 비밀번호가 올바르지 않습니다")
    if len(body.new_password) < MIN_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"비밀번호는 {MIN_LEN}자 이상이어야 합니다"
        )
    security.set_password(body.new_password)
    _set_cookie(response)  # 비밀번호 변경 시 쿠키 갱신
    return {"ok": True}
