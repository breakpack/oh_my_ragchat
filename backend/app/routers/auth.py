"""다중 사용자 인증 + 계정 관리.

사용자마다 Postgres 스키마와 저장소 디렉터리를 따로 갖는다. 첫 사용자는 자동으로
관리자가 되고, 이후 계정은 관리자만 만들 수 있다(공개 가입 없음).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from .. import ctx, db, deps, paths, security
from ..config import env

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PW = 4
USERNAME_OK = 3


class LoginIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class SetupIn(BaseModel):
    username: str = Field(min_length=USERNAME_OK, max_length=40)
    password: str = Field(min_length=MIN_PW)
    display_name: str | None = None


class ChangeIn(BaseModel):
    current_password: str = ""
    new_password: str = Field(min_length=MIN_PW)


class UserIn(BaseModel):
    username: str = Field(min_length=USERNAME_OK, max_length=40)
    password: str = Field(min_length=MIN_PW)
    display_name: str | None = None
    is_admin: bool = False


def _set_cookie(response: Response, user: ctx.User) -> None:
    with ctx.as_user(user):
        days = int(db.get_setting("session_days") or 30)
    response.set_cookie(
        env.session_cookie,
        security.issue_session(user.id),
        max_age=days * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _prepare(user: ctx.User) -> None:
    """로그인 직후 저장소 디렉터리를 만들어 둔다."""
    with ctx.as_user(user):
        paths.ensure_dirs()


@router.get("/me")
def me(session: Annotated[str | None, Cookie(alias=env.session_cookie)] = None) -> dict:
    configured = security.has_users()
    user = security.session_user(session, 365) if configured else None
    return {
        "configured": configured,
        "authenticated": user is not None,
        "user": None if user is None else {
            "id": user.id, "username": user.username,
            "display_name": user.display_name, "is_admin": user.is_admin,
        },
    }


@router.post("/setup")
def setup(body: SetupIn, response: Response) -> dict:
    """최초 관리자 계정. 사용자가 하나라도 있으면 거부한다."""
    if security.has_users():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="이미 설정된 서버입니다")
    user = security.create_user(
        body.username, body.password, is_admin=True, display_name=body.display_name
    )
    _prepare(user)
    _set_cookie(response, user)
    return {"ok": True}


@router.post("/login")
def login(body: LoginIn, response: Response) -> dict:
    if not security.has_users():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="초기 설정이 필요합니다")
    user = security.authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="아이디 또는 비밀번호가 올바르지 않습니다")
    _prepare(user)
    _set_cookie(response, user)
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(env.session_cookie, path="/")
    return {"ok": True}


@router.post("/password")
def change_password(body: ChangeIn, response: Response, user: deps.CurrentUser) -> dict:
    if not security.check_password(body.current_password, user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="현재 비밀번호가 올바르지 않습니다")
    security.set_password(body.new_password, user.id)
    _set_cookie(response, user)
    return {"ok": True}


# ─────────────────────────── 계정 관리 (관리자) ───────────────────────────


@router.get("/users", dependencies=[deps.AdminOnly])
def list_users() -> dict:
    return {"users": security.list_users()}


@router.post("/users", dependencies=[deps.AdminOnly])
def create_user(body: UserIn) -> dict:
    try:
        user = security.create_user(
            body.username, body.password,
            is_admin=body.is_admin, display_name=body.display_name,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    _prepare(user)
    return {"ok": True, "user": {"id": user.id, "username": user.username}}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: Annotated[ctx.User, Depends(deps.require_admin)]) -> dict:
    """계정과 그 사용자의 스키마를 통째로 지운다. 저장소 파일은 남긴다."""
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="자기 계정은 지울 수 없습니다")
    if len(security.list_users()) <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="마지막 계정은 지울 수 없습니다")
    if security.get_user(user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="없는 계정입니다")
    security.delete_user(user_id)
    return {"ok": True}
