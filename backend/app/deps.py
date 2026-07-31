from __future__ import annotations

from typing import Annotated, Any

from fastapi import Cookie, Depends, HTTPException, status

from . import db, security
from .config import env


def require_auth(
    session: Annotated[str | None, Cookie(alias=env.session_cookie)] = None,
) -> None:
    days = int(db.get_setting("session_days") or 30)
    if not security.valid_session(session, days):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="로그인이 필요합니다" if security.is_configured() else "초기 설정이 필요합니다",
        )


def settings() -> dict[str, Any]:
    return db.get_settings()


Auth = Depends(require_auth)
Settings = Annotated[dict[str, Any], Depends(settings)]
