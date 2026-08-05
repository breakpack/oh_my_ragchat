"""FastAPI 엔트리포인트."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import db, migrate, ollama, paths
from .routers.auth import router as auth_router
from .routers.chat import router as chat_router
from .routers.files import router as files_router
from .routers.notion import router as notion_router
from .routers.personas import router as personas_router
from .routers.rag import router as rag_router
from .routers.sessions import router as sessions_router
from .routers.settings import router as settings_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("chatchat.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    paths.ensure_dirs()
    db.pool()  # 부팅 시 커넥션 확보 실패를 바로 드러낸다
    migrate.run()
    log.info("api ready (nas=%s)", paths.root())
    yield
    db.close_pool()


app = FastAPI(title="chatchat", lifespan=lifespan, docs_url="/api/docs",
              openapi_url="/api/openapi.json")

app.include_router(auth_router)
app.include_router(settings_router)
app.include_router(files_router)
app.include_router(personas_router)
app.include_router(sessions_router)
app.include_router(chat_router)
app.include_router(rag_router)
app.include_router(notion_router)


@app.get("/api/health")
async def health() -> JSONResponse:
    result: dict = {"ok": True}

    try:
        with db.cursor(commit=False) as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
        result["db"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001 - 헬스체크는 원인 문자열만 필요
        result["db"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result["ok"] = False

    result["ollama"] = await ollama.ping()
    if not result["ollama"]["ok"]:
        result["ok"] = False

    try:
        with db.cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT
                  (SELECT count(*) FROM documents)              AS documents,
                  (SELECT count(*) FROM documents WHERE status = 'ready') AS ready,
                  (SELECT count(*) FROM chunks)                 AS chunks,
                  (SELECT count(*) FROM entities)               AS entities,
                  (SELECT count(*) FROM relations)              AS relations,
                  (SELECT count(*) FROM jobs WHERE status = 'queued')  AS queued,
                  (SELECT count(*) FROM jobs WHERE status = 'running') AS running,
                  (SELECT count(*) FROM jobs WHERE status = 'failed')  AS failed
                """
            )
            result["stats"] = cur.fetchone()
    except Exception:  # noqa: BLE001 - 통계는 부가 정보
        result["stats"] = None

    return JSONResponse(result, status_code=200 if result["ok"] else 503)
