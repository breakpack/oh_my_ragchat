"""현재 요청/잡의 사용자.

모든 테이블에 user_id 를 붙이는 대신 사용자별 Postgres 스키마로 나눴다.
여기 담긴 스키마 이름을 db.cursor() 가 search_path 로 걸어 주므로,
기존 쿼리는 한 줄도 고치지 않고 사용자별로 격리된다.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class User:
    id: int
    username: str
    schema: str
    is_admin: bool = False
    display_name: str | None = None


_current: ContextVar[User | None] = ContextVar("current_user", default=None)


def get() -> User | None:
    return _current.get()


def require() -> User:
    user = _current.get()
    if user is None:
        raise RuntimeError("사용자 컨텍스트가 없습니다")
    return user


def set_user(user: User | None):
    return _current.set(user)


@contextmanager
def as_user(user: User) -> Iterator[User]:
    token = _current.set(user)
    try:
        yield user
    finally:
        _current.reset(token)


def schema_for(user_id: int) -> str:
    return f"u{user_id}"
