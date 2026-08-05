"""jobs 테이블 큐 — FOR UPDATE SKIP LOCKED 로 소비한다.

중복 큐잉은 부분 유니크 인덱스(kind, payload->>'path' WHERE status IN queued/running)가
막아주므로 ON CONFLICT DO NOTHING 으로 흡수한다.
"""

from __future__ import annotations

import json
from typing import Any

from . import db

INDEX_DOCUMENT = "index_document"
DELETE_DOCUMENT = "delete_document"
REINDEX_ALL = "reindex_all"
INDEX_NOTION = "index_notion"


def enqueue(kind: str, payload: dict[str, Any] | None = None) -> int | None:
    payload = payload or {}
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (kind, payload) VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (kind, json.dumps(payload)),
        )
        row = cur.fetchone()
    return row["id"] if row else None


def enqueue_index(path: str) -> int | None:
    return enqueue(INDEX_DOCUMENT, {"path": path})


def enqueue_delete(path: str) -> int | None:
    return enqueue(DELETE_DOCUMENT, {"path": path})


def claim(kinds: tuple[str, ...] | None = None) -> dict[str, Any] | None:
    """큐에서 잡 하나를 running 으로 선점한다."""
    sql = """
        WITH picked AS (
            SELECT id FROM jobs
             WHERE status = 'queued' {kind_filter}
             ORDER BY id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
        )
        UPDATE jobs j
           SET status = 'running', attempts = j.attempts + 1, started_at = now()
          FROM picked
         WHERE j.id = picked.id
        RETURNING j.id, j.kind, j.payload, j.attempts
    """
    params: tuple = ()
    if kinds:
        sql = sql.format(kind_filter="AND kind = ANY(%s)")
        params = (list(kinds),)
    else:
        sql = sql.format(kind_filter="")

    with db.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def finish(job_id: int, error: str | None = None) -> None:
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs
               SET status = %s, error = %s, done_at = now()
             WHERE id = %s
            """,
            ("failed" if error else "done", error[:2000] if error else None, job_id),
        )


def retry_failed() -> int:
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET status = 'queued', error = NULL, done_at = NULL
             WHERE status = 'failed'
            """
        )
        return cur.rowcount


def stats() -> dict[str, int]:
    with db.cursor(commit=False) as cur:
        cur.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: r["n"] for r in cur.fetchall()}


def requeue_running() -> int:
    """워커 기동 시 호출. 워커는 하나뿐이므로 이 시점의 running 은 전부 고아 잡이다.

    (SIGTERM 으로 중단된 잡을 되살리지 않으면 sweep_stale 의 시간 조건에 걸릴 때까지
    영영 running 으로 남아 큐가 막힌다)
    """
    with db.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'queued', started_at = NULL WHERE status = 'running'"
        )
        return cur.rowcount


def sweep_stale(minutes: int = 60) -> int:
    """워커가 죽어서 running 인 채 방치된 잡을 되살린다."""
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET status = 'queued'
             WHERE status = 'running'
               AND started_at < now() - make_interval(mins => %s)
            """,
            (minutes,),
        )
        return cur.rowcount
