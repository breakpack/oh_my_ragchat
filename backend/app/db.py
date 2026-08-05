"""psycopg3 커넥션 풀 + app_settings 접근자."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from pathlib import Path

from . import ctx
from .config import DEFAULT_SETTINGS, env

_pool: ConnectionPool | None = None


def _configure(conn) -> None:
    register_vector(conn)


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            env.database_url,
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            configure=_configure,
            open=True,
        )
    return _pool


@contextmanager
def conn() -> Iterator[Any]:
    with pool().connection() as c:
        yield c


@contextmanager
def cursor(commit: bool = True, schema: str | None = None) -> Iterator[Any]:
    """커서 하나. search_path 를 현재 사용자 스키마로 맞춰서 준다.

    풀에서 꺼낸 커넥션은 재사용되므로 매번 설정한다. schema="public" 을 주면
    사용자와 무관한 테이블(users/jobs)을 다룬다.
    """
    if schema is None:
        user = ctx.get()
        schema = user.schema if user else "public"
    with pool().connection() as c:
        with c.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}", public')
            yield cur
        if commit:
            c.commit()


def create_user_schema(user_id: int) -> str:
    """새 사용자의 테이블 일습을 만든다."""
    schema = ctx.schema_for(user_id)
    ddl = (Path(__file__).parent / "user_schema.sql").read_text(encoding="utf-8")
    with cursor(schema="public") as cur:
        cur.execute(ddl.replace("{schema}", schema))
    return schema


def drop_user_schema(user_id: int) -> None:
    with cursor(schema="public") as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{ctx.schema_for(user_id)}" CASCADE')


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


# ─────────────────────────── settings ───────────────────────────


def get_settings() -> dict[str, Any]:
    """기본값 위에 DB 저장값을 덮어쓴 전체 설정."""
    merged = dict(DEFAULT_SETTINGS)
    with cursor(commit=False) as cur:
        cur.execute("SELECT key, value FROM app_settings")
        for row in cur.fetchall():
            if row["key"] in DEFAULT_SETTINGS:
                merged[row["key"]] = row["value"]
    return merged


def get_setting(key: str) -> Any:
    with cursor(commit=False) as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    return row["value"]


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """DEFAULT_SETTINGS 에 있는 키만 저장 (알 수 없는 키는 무시)."""
    known = {k: v for k, v in patch.items() if k in DEFAULT_SETTINGS}
    if known:
        with cursor() as cur:
            for key, value in known.items():
                cur.execute(
                    """
                    INSERT INTO app_settings (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (key, json.dumps(value)),
                )
    return get_settings()
