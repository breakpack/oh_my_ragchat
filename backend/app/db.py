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
from .config import ADMIN_SETTINGS, DEFAULT_SETTINGS, env

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
    """기본값 ← 전역(관리자) ← 개인 순으로 덮어쓴 전체 설정.

    관리자 전용 키는 개인 설정으로 덮어쓸 수 없다.
    """
    merged = dict(DEFAULT_SETTINGS)
    with cursor(commit=False, schema="public") as cur:
        cur.execute("SELECT key, value FROM global_settings")
        for row in cur.fetchall():
            if row["key"] in DEFAULT_SETTINGS:
                merged[row["key"]] = row["value"]

    if ctx.get() is not None:
        with cursor(commit=False) as cur:
            cur.execute("SELECT key, value FROM app_settings")
            for row in cur.fetchall():
                if row["key"] in DEFAULT_SETTINGS and row["key"] not in ADMIN_SETTINGS:
                    merged[row["key"]] = row["value"]
    return merged


def get_setting(key: str) -> Any:
    table, schema = (
        ("global_settings", "public") if key in ADMIN_SETTINGS else ("app_settings", None)
    )
    if schema is None and ctx.get() is None:
        return DEFAULT_SETTINGS.get(key)
    with cursor(commit=False, schema=schema) as cur:
        cur.execute(f"SELECT value FROM {table} WHERE key = %s", (key,))
        row = cur.fetchone()
    return DEFAULT_SETTINGS.get(key) if row is None else row["value"]


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """DEFAULT_SETTINGS 에 있는 키만 저장. 관리자 키는 전역, 나머지는 개인 스키마로."""
    known = {k: v for k, v in patch.items() if k in DEFAULT_SETTINGS}
    admin = {k: v for k, v in known.items() if k in ADMIN_SETTINGS}
    personal = {k: v for k, v in known.items() if k not in ADMIN_SETTINGS}

    for values, table, schema in (
        (admin, "global_settings", "public"),
        (personal, "app_settings", None),
    ):
        if not values:
            continue
        with cursor(schema=schema) as cur:
            for key, value in values.items():
                cur.execute(
                    f"""
                    INSERT INTO {table} (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (key, json.dumps(value)),
                )
    return get_settings()
