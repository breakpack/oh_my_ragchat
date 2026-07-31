"""worker → api 실시간 이벤트 전달.

worker 와 api 는 별도 컨테이너라 메모리를 공유할 수 없다. 이미 있는 Postgres 의
LISTEN/NOTIFY 를 쓰면 브로커를 추가하지 않고도 인덱싱 진행률을 밀어줄 수 있다.
(NOTIFY payload 는 8000바이트 제한이므로 요약 정보만 담는다)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import psycopg

from . import db
from .config import env

log = logging.getLogger("chatchat.events")

CHANNEL = "chatchat_events"


# ─────────────────────────── 발행 (sync, worker) ───────────────────────────


def publish(kind: str, data: dict[str, Any]) -> None:
    """이벤트 발행. 실패해도 인덱싱 자체는 계속돼야 하므로 삼킨다."""
    payload = json.dumps({"kind": kind, **data}, ensure_ascii=False, default=str)
    if len(payload.encode()) > 7500:  # 페이로드 상한 방어
        payload = json.dumps({"kind": kind}, ensure_ascii=False)
    try:
        with db.cursor() as cur:
            cur.execute("SELECT pg_notify(%s, %s)", (CHANNEL, payload))
    except Exception as exc:  # noqa: BLE001
        log.debug("이벤트 발행 실패 (%s): %s", kind, exc)


# ─────────────────────────── 구독 (async, api) ───────────────────────────


async def _pump(queue: asyncio.Queue) -> None:
    """전용 커넥션으로 LISTEN 하며 큐에 밀어넣는다 (풀을 점유하지 않는다)."""
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(
                env.database_url, autocommit=True
            ) as aconn:
                await aconn.execute(f"LISTEN {CHANNEL}")
                async for note in aconn.notifies():
                    await queue.put(note.payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 끊기면 잠시 뒤 재연결
            log.warning("이벤트 구독 끊김, 재연결: %s", exc)
            await asyncio.sleep(2)


async def subscribe(heartbeat: float = 20.0) -> AsyncIterator[str | None]:
    """이벤트 payload(JSON 문자열)를 흘린다. 조용하면 None 을 내보내 연결을 살린다."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    task = asyncio.create_task(_pump(queue))
    try:
        while True:
            try:
                yield await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                yield None
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
