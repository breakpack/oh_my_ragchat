"""RAG 관리 API — 인덱싱 상태, 재인덱싱, 검색·그래프 프리뷰."""

from __future__ import annotations

from typing import Any

import anyio
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import db, deps, events, jobs, paths
from ..config import RAG_MODES
from ..rag import graph as graph_mod
from ..rag import index as index_mod
from ..rag.retrieve import retrieve

router = APIRouter(prefix="/api/rag", tags=["rag"], dependencies=[deps.Auth])


@router.get("/documents")
def documents(
    status_filter: str | None = Query(None, alias="status"),
    q: str | None = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict:
    where, params = [], []
    if status_filter:
        where.append("status = %s")
        params.append(status_filter)
    if q:
        where.append("path ILIKE %s")
        params.append(f"%{q}%")
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    with db.cursor(commit=False) as cur:
        cur.execute(
            f"""
            SELECT id, path, size, status, chunk_count, error, indexed_at, created_at,
                   ocr, progress_done, progress_total, phase,
                   (SELECT count(DISTINCT ce.entity_id)
                      FROM chunks c JOIN chunk_entities ce ON ce.chunk_id = c.id
                     WHERE c.document_id = d.id) AS entity_count
              FROM documents d
              {clause}
             ORDER BY (status = 'error') DESC, indexed_at DESC NULLS FIRST, path
             LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        )
        rows = cur.fetchall()
        cur.execute(f"SELECT count(*) AS n FROM documents d {clause}", tuple(params))
        total = cur.fetchone()["n"]
        cur.execute("SELECT status, count(*) AS n FROM documents GROUP BY status")
        by_status = {r["status"]: r["n"] for r in cur.fetchall()}

    return {"documents": rows, "total": total, "by_status": by_status}


@router.get("/events")
async def rag_events() -> StreamingResponse:
    """인덱싱 진행률 SSE. worker 가 Postgres NOTIFY 로 밀어준 걸 그대로 흘린다.

    GET 이므로 프론트는 EventSource 로 붙는다 (쿠키는 same-origin 으로 자동 전송).
    """

    async def gen():
        yield ": connected\n\n"
        async for payload in events.subscribe():
            if payload is None:
                yield ": ping\n\n"  # 프록시가 유휴 연결을 끊지 않게
            else:
                yield f"data: {payload}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/documents/{doc_id}/chunks")
def doc_chunks(doc_id: int, limit: int = Query(50, ge=1, le=500)) -> dict:
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, ord, content, token_est,
                   (SELECT count(*) FROM chunk_entities ce WHERE ce.chunk_id = chunks.id)
                     AS entity_count
              FROM chunks WHERE document_id = %s ORDER BY ord LIMIT %s
            """,
            (doc_id, limit),
        )
        return {"chunks": cur.fetchall()}


class ReindexIn(BaseModel):
    path: str | None = None  # 없으면 전체
    force: bool = True


@router.post("/reindex")
def reindex(body: ReindexIn) -> dict:
    if body.path:
        rel = paths.normalize(body.path)
        target = paths.resolve(rel)
        if target.is_dir():
            # 폴더면 하위 문서를 개별 잡으로 편다
            queued = 0
            for doc in _docs_under(rel):
                if jobs.enqueue(jobs.INDEX_DOCUMENT,
                                {"path": doc, "force": body.force}) is not None:
                    queued += 1
            return {"ok": True, "queued": queued}
        job_id = jobs.enqueue(jobs.INDEX_DOCUMENT, {"path": rel, "force": body.force})
        return {"ok": True, "queued": 1 if job_id else 0}

    jobs.enqueue(jobs.REINDEX_ALL, {})
    return {"ok": True, "queued": "all"}


def _docs_under(rel: str) -> list[str]:
    with db.cursor(commit=False) as cur:
        cur.execute(
            "SELECT path FROM documents WHERE path LIKE %s ORDER BY path", (rel + "/%",)
        )
        return [r["path"] for r in cur.fetchall()]


@router.post("/scan")
def scan_now(cfg: deps.Settings) -> dict:
    """워커의 주기 스캔을 기다리지 않고 지금 훑는다."""
    return index_mod.scan(cfg)


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int) -> dict:
    with db.cursor() as cur:
        cur.execute("DELETE FROM documents WHERE id = %s RETURNING path", (doc_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="문서를 찾을 수 없습니다")
    graph_mod.prune_orphans()
    graph_mod.refresh_degrees()
    return {"ok": True, "path": row["path"]}


@router.get("/search")
async def search(
    cfg: deps.Settings,
    q: str = Query(min_length=1),
    mode: str = Query("hybrid"),
) -> dict:
    if mode not in RAG_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"mode 는 {RAG_MODES} 중 하나")
    ctx = await retrieve(q, mode, cfg)
    return {
        "citations": ctx.citations,
        "stats": ctx.stats,
        "empty": ctx.empty,
        "prompt_block": ctx.prompt_block,
    }


@router.get("/graph")
async def graph_view(
    entity: str | None = Query(None),
    depth: int = Query(1, ge=1, le=2),
    limit: int = Query(60, ge=1, le=300),
) -> dict:
    """엔티티 이웃 그래프. entity 를 안 주면 degree 상위 노드를 보여준다."""
    return await anyio.to_thread.run_sync(_graph_sync, entity, depth, limit)


def _graph_sync(entity: str | None, depth: int, limit: int) -> dict[str, Any]:
    with db.cursor(commit=False) as cur:
        if entity:
            cur.execute(
                """
                SELECT id FROM entities
                 WHERE name_norm = %s OR name ILIKE %s
                 ORDER BY degree DESC LIMIT 5
                """,
                (graph_mod.normalize_name(entity), f"%{entity}%"),
            )
            seeds = [r["id"] for r in cur.fetchall()]
            if not seeds:
                return {"nodes": [], "edges": [], "seed": entity}
        else:
            cur.execute(
                "SELECT id FROM entities ORDER BY degree DESC, id LIMIT %s", (limit // 4 or 1,)
            )
            seeds = [r["id"] for r in cur.fetchall()]
            if not seeds:
                return {"nodes": [], "edges": [], "seed": None}

        cur.execute(
            """
            WITH RECURSIVE walk(id, hop) AS (
                SELECT unnest(%s::bigint[]), 0
              UNION
                SELECT CASE WHEN r.src_id = w.id THEN r.tgt_id ELSE r.src_id END, w.hop + 1
                  FROM walk w JOIN relations r ON r.src_id = w.id OR r.tgt_id = w.id
                 WHERE w.hop < %s
            )
            SELECT e.id, e.name, e.type, e.description, e.degree, min(w.hop) AS hop
              FROM walk w JOIN entities e ON e.id = w.id
             GROUP BY e.id
             ORDER BY min(w.hop), e.degree DESC
             LIMIT %s
            """,
            (seeds, depth, limit),
        )
        nodes = cur.fetchall()
        ids = [n["id"] for n in nodes]

        cur.execute(
            """
            SELECT r.id, r.src_id AS source, r.tgt_id AS target,
                   r.description, r.keywords, r.weight
              FROM relations r
             WHERE r.src_id = ANY(%s) AND r.tgt_id = ANY(%s)
             ORDER BY r.weight DESC
             LIMIT %s
            """,
            (ids, ids, limit * 3),
        )
        edges = cur.fetchall()

    return {"nodes": nodes, "edges": edges, "seed": entity}


@router.get("/entities")
def entities(
    q: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    where = "WHERE name ILIKE %s" if q else ""
    params: tuple = (f"%{q}%",) if q else ()
    with db.cursor(commit=False) as cur:
        cur.execute(
            f"""
            SELECT id, name, type, description, degree FROM entities
            {where} ORDER BY degree DESC, name LIMIT %s
            """,
            (*params, limit),
        )
        return {"entities": cur.fetchall()}


@router.get("/jobs")
def job_list(limit: int = Query(50, ge=1, le=500)) -> dict:
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, kind, payload, status, attempts, error, created_at, done_at
              FROM jobs ORDER BY id DESC LIMIT %s
            """,
            (limit,),
        )
        return {"jobs": cur.fetchall(), "stats": jobs.stats()}


@router.post("/jobs/retry")
def retry_jobs() -> dict:
    return {"ok": True, "requeued": jobs.retry_failed()}


@router.delete("/jobs")
def clear_jobs() -> dict:
    with db.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE status IN ('done', 'failed')")
        return {"ok": True, "removed": cur.rowcount}


@router.get("/stats")
def stats() -> dict:
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT
              (SELECT count(*) FROM documents)  AS documents,
              (SELECT count(*) FROM documents WHERE status = 'ready')   AS ready,
              (SELECT count(*) FROM documents WHERE status = 'error')   AS errors,
              (SELECT count(*) FROM documents WHERE status = 'skipped') AS skipped,
              (SELECT count(*) FROM chunks)     AS chunks,
              (SELECT count(*) FROM entities)   AS entities,
              (SELECT count(*) FROM relations)  AS relations
            """
        )
        row = cur.fetchone()
    return {"stats": {**row, "jobs": jobs.stats()}}
