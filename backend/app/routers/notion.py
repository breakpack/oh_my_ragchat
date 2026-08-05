"""Notion 크롤 — 링크 하나를 받아 하위 페이지까지 색인한다.

색인 결과는 NAS 문서와 같은 documents/chunks/entities 를 쓰고 source='notion' 으로만
구분한다. 그래서 진행률 SSE·그래프·검색이 전부 그대로 재사용된다.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db, deps, jobs, notion

router = APIRouter(prefix="/api/notion", tags=["notion"], dependencies=[deps.Auth])


class TokenIn(BaseModel):
    token: str = ""


class CrawlIn(BaseModel):
    url: str = Field(min_length=1)
    max_depth: int = Field(3, ge=0, le=6)


@router.get("/status")
def status() -> dict:
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT count(*) AS pages,
                   count(*) FILTER (WHERE status = 'ready') AS ready,
                   count(*) FILTER (WHERE status = 'error') AS errors
              FROM documents WHERE source = 'notion'
            """
        )
        stats = cur.fetchone()
    return {
        "configured": notion.configured(),
        "token_source": notion.source(),
        "token_masked": notion.masked(),
        "stats": stats,
        "queued": jobs.stats().get("queued", 0),
    }


@router.put("/token")
def set_token(body: TokenIn) -> dict:
    if os.getenv("NOTION_TOKEN", "").strip():
        raise HTTPException(400, detail="토큰이 .env 로 지정돼 있어 웹에서 바꿀 수 없습니다")
    t = body.token.strip()
    if t and not t.startswith(("secret_", "ntn_")):
        raise HTTPException(400, detail="Notion 내부 통합 토큰이 아닙니다 (secret_ 또는 ntn_ 로 시작)")
    notion.set_token(t or None)
    return {"ok": True, "configured": notion.configured(), "masked": notion.masked()}


@router.post("/test")
def test() -> dict:
    return notion.ping()


@router.post("/crawl")
def crawl(body: CrawlIn) -> dict:
    host = notion.public_host(body.url)  # 게시된 페이지면 토큰이 필요 없다
    if not host and not notion.configured():
        raise HTTPException(
            400,
            detail="비공개 페이지는 통합 토큰이 필요합니다. 설정 → 연결 에서 입력하거나, "
                   "'인터넷에 게시'된 notion.site 링크를 넣으세요",
        )
    try:
        page_id = notion.parse_id(body.url)
    except notion.NotionError as exc:
        raise HTTPException(400, detail=str(exc)) from None

    jobs.enqueue(jobs.INDEX_NOTION,
                 {"path": page_id, "depth": 0, "max_depth": body.max_depth, "host": host})
    return {"ok": True, "page_id": page_id, "mode": "public" if host else "integration"}


@router.get("/pages")
def pages(limit: int = Query(500, ge=1, le=2000)) -> dict:
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, path, url, status, chunk_count, error, indexed_at,
                   progress_done, progress_total, phase,
                   (SELECT count(DISTINCT ce.entity_id)
                      FROM chunks c JOIN chunk_entities ce ON ce.chunk_id = c.id
                     WHERE c.document_id = d.id) AS entity_count
              FROM documents d
             WHERE source = 'notion'
             ORDER BY (status = 'error') DESC, indexed_at DESC NULLS FIRST, path
             LIMIT %s
            """,
            (limit,),
        )
        return {"pages": cur.fetchall()}


@router.delete("/pages")
def clear() -> dict:
    """Notion 색인만 전부 지운다 (NAS 문서는 건드리지 않는다)."""
    from ..rag import graph

    with db.cursor() as cur:
        cur.execute("DELETE FROM documents WHERE source = 'notion' RETURNING id")
        n = len(cur.fetchall())
    graph.prune_orphans()
    graph.refresh_degrees()
    return {"ok": True, "removed": n}
