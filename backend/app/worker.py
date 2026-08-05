"""인덱싱 워커.

잡 큐를 소비하면서 주기적으로 감시 폴더를 스캔한다.
macOS 호스트의 bind mount 에서는 inotify 이벤트가 컨테이너까지 오지 않으므로
파일 감시는 watchdog 대신 mtime/size 대조 스캔으로 처리한다.
"""

from __future__ import annotations

import logging
import os
import signal
import time

import httpx

from . import db, jobs, paths
from .rag import index

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("chatchat.worker")

SCAN_SECONDS = int(os.getenv("RAG_SCAN_SECONDS", "30"))
IDLE_SLEEP = 1.0
MAX_ATTEMPTS = 3

_running = True


def _stop(signum, _frame) -> None:
    global _running
    log.info("종료 신호 수신 (%s)", signum)
    _running = False


def handle(job: dict, client: httpx.Client) -> None:
    kind = job["kind"]
    payload = job["payload"] or {}
    cfg = db.get_settings()

    if kind == jobs.INDEX_DOCUMENT:
        index.index_document(
            payload["path"], cfg, force=bool(payload.get("force")), client=client
        )
    elif kind == jobs.DELETE_DOCUMENT:
        n = index.delete_document(payload["path"])
        log.info("인덱스에서 제거: %s (문서 %d건)", payload["path"], n)
    elif kind == jobs.INDEX_NOTION:
        index.index_notion(payload["path"], cfg, depth=int(payload.get("depth", 0)),
                           max_depth=int(payload.get("max_depth", 3)),
                           host=payload.get("host"), client=client)
    elif kind == jobs.REINDEX_ALL:
        with db.cursor() as cur:
            cur.execute("UPDATE documents SET status = 'pending'")
        result = index.scan(cfg, force=True)
        log.info("전체 재인덱싱 큐잉: %s", result)
    else:
        raise ValueError(f"알 수 없는 잡 종류: {kind}")


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    paths.ensure_dirs()
    _wait_for_db()

    log.info("worker 시작 (스캔 주기 %ds, nas=%s)", SCAN_SECONDS, paths.root())
    revived = jobs.requeue_running()
    if revived:
        log.info("중단됐던 잡 %d건을 큐로 되돌렸습니다", revived)

    last_scan = 0.0
    client = httpx.Client(timeout=600)
    try:
        while _running:
            now = time.monotonic()
            if now - last_scan >= SCAN_SECONDS:
                last_scan = now
                try:
                    result = index.scan(db.get_settings())
                    if result["queued"] or result["removed"]:
                        log.info("스캔: %s", result)
                except Exception:  # noqa: BLE001 - 스캔 실패로 워커가 죽으면 안 된다
                    log.exception("스캔 실패")

            job = jobs.claim()
            if job is None:
                time.sleep(IDLE_SLEEP)
                continue

            try:
                handle(job, client)
                jobs.finish(job["id"])
            except Exception as exc:  # noqa: BLE001
                log.exception("잡 %s(%s) 실패", job["id"], job["kind"])
                if job["attempts"] >= MAX_ATTEMPTS:
                    jobs.finish(job["id"], error=f"{type(exc).__name__}: {exc}")
                else:
                    with db.cursor() as cur:  # 재시도 가능하면 큐로 되돌린다
                        cur.execute(
                            "UPDATE jobs SET status = 'queued', error = %s WHERE id = %s",
                            (f"{type(exc).__name__}: {exc}"[:2000], job["id"]),
                        )
                    time.sleep(2)
    finally:
        client.close()
        db.close_pool()
        log.info("worker 종료")


def _wait_for_db(timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while True:
        try:
            with db.cursor(commit=False) as cur:
                cur.execute("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001
            if time.time() > deadline:
                raise
            log.info("DB 대기 중… (%s)", exc)
            time.sleep(2)


if __name__ == "__main__":
    main()
