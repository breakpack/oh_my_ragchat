from __future__ import annotations

from typing import Annotated, Any

from fastapi import Cookie, Depends, HTTPException, status

from . import ctx, db, security
from .config import env


async def current_user(
    session: Annotated[str | None, Cookie(alias=env.session_cookie)] = None,
) -> ctx.User:
    """쿠키에서 사용자를 복원하고 요청 컨텍스트에 심는다.

    이후 db.cursor() 가 이 사용자의 스키마를 search_path 로 걸어 준다.

    async 여야 한다. 동기 의존성은 각각 별도 스레드에서 컨텍스트 '복사본' 위로
    돌기 때문에, 거기서 ContextVar 를 세팅해도 나머지 요청 처리에는 보이지 않는다.
    """
    # 먼저 넉넉한 상한으로 사용자를 식별한 뒤, 그 사용자의 설정값으로 다시 검증한다
    user = security.session_user(session, 365)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="로그인이 필요합니다" if security.has_users() else "초기 설정이 필요합니다",
        )
    ctx.set_user(user)
    # 로그인 유지 기간은 사용자 설정값을 따른다 (컨텍스트가 있어야 읽을 수 있다)
    try:
        days = int(db.get_setting("session_days") or 30)
    except Exception:  # noqa: BLE001
        days = 30
    if days < 365 and security.session_user(session, days) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="세션이 만료되었습니다")
    return user


async def require_admin(user: Annotated[ctx.User, Depends(current_user)]) -> ctx.User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="관리자만 할 수 있습니다")
    return user


def settings(user: Annotated[ctx.User, Depends(current_user)]) -> dict[str, Any]:
    return db.get_settings()


Auth = Depends(current_user)
AdminOnly = Depends(require_admin)
CurrentUser = Annotated[ctx.User, Depends(current_user)]
Settings = Annotated[dict[str, Any], Depends(settings)]
