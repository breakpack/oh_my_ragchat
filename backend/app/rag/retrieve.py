"""Graph RAG 검색 — 채팅(W2)이 부르는 유일한 진입점.

DB 접근은 sync(psycopg) 이므로 스레드로 넘겨 이벤트 루프를 막지 않는다.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import anyio

from .. import db, ollama

log = logging.getLogger("chatchat.rag.retrieve")

EXCERPT = 400


@dataclass
class RagContext:
    prompt_block: str = ""
    citations: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    empty: bool = True


_TOKEN = re.compile(r"[0-9A-Za-z가-힣]{2,}")
_STOP = {"그리고", "하지만", "무엇", "어떻게", "이것", "저것", "그것", "about", "what",
         "which", "there", "their", "this", "that", "with", "from", "have"}


def keywords(query: str, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for tok in _TOKEN.findall(query):
        low = tok.lower()
        if low in _STOP or low in seen:
            continue
        seen.append(low)
        if len(seen) >= limit:
            break
    return seen


# ─────────────────────────── 개별 모드 (sync) ───────────────────────────


def _naive_chunks(vec: list[float], k: int) -> list[dict]:
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT c.id, c.content, c.ord, d.id AS document_id, d.path,
                   1 - (c.embedding <=> %s::vector) AS score
              FROM chunks c JOIN documents d ON d.id = c.document_id
             WHERE c.embedding IS NOT NULL
             ORDER BY c.embedding <=> %s::vector
             LIMIT %s
            """,
            (vec, vec, k),
        )
        return cur.fetchall()


def _seed_entities(vec: list[float], k: int) -> list[dict]:
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, name, type, description, degree,
                   1 - (embedding <=> %s::vector) AS score
              FROM entities
             WHERE embedding IS NOT NULL
             ORDER BY embedding <=> %s::vector
             LIMIT %s
            """,
            (vec, vec, k),
        )
        return cur.fetchall()


def _expand(entity_ids: list[int], depth: int, limit: int) -> list[dict]:
    """관계 테이블 재귀 CTE 로 이웃을 넓힌다."""
    if not entity_ids or depth <= 0:
        return []
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            WITH RECURSIVE walk(id, hop) AS (
                SELECT unnest(%s::bigint[]), 0
              UNION
                SELECT CASE WHEN r.src_id = w.id THEN r.tgt_id ELSE r.src_id END, w.hop + 1
                  FROM walk w
                  JOIN relations r ON r.src_id = w.id OR r.tgt_id = w.id
                 WHERE w.hop < %s
            )
            SELECT e.id, e.name, e.type, e.description, e.degree, min(w.hop) AS hop
              FROM walk w JOIN entities e ON e.id = w.id
             WHERE w.hop > 0
             GROUP BY e.id
             ORDER BY min(w.hop), e.degree DESC
             LIMIT %s
            """,
            (entity_ids, depth, limit),
        )
        return cur.fetchall()


def _relations_for(entity_ids: list[int], limit: int) -> list[dict]:
    if not entity_ids:
        return []
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT r.id, s.name AS src, t.name AS tgt, r.description, r.keywords, r.weight
              FROM relations r
              JOIN entities s ON s.id = r.src_id
              JOIN entities t ON t.id = r.tgt_id
             WHERE r.src_id = ANY(%s) AND r.tgt_id = ANY(%s)
             ORDER BY r.weight DESC
             LIMIT %s
            """,
            (entity_ids, entity_ids, limit),
        )
        return cur.fetchall()


def _relations_by_keyword(words: list[str], limit: int) -> list[dict]:
    if not words:
        return []
    pattern = "|".join(re.escape(w) for w in words)
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT r.id, r.src_id, r.tgt_id, s.name AS src, t.name AS tgt,
                   r.description, r.keywords, r.weight
              FROM relations r
              JOIN entities s ON s.id = r.src_id
              JOIN entities t ON t.id = r.tgt_id
             WHERE r.keywords ~* %s OR r.description ~* %s
                OR s.name ~* %s OR t.name ~* %s
             ORDER BY r.weight DESC
             LIMIT %s
            """,
            (pattern, pattern, pattern, pattern, limit),
        )
        return cur.fetchall()


def _chunks_for_entities(entity_ids: list[int], vec: list[float], k: int) -> list[dict]:
    """엔티티에 연결된 청크를, 몇 개의 엔티티를 건드리는지 + 질문 유사도로 랭킹."""
    if not entity_ids:
        return []
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT c.id, c.content, c.ord, d.id AS document_id, d.path,
                   count(*) AS hits,
                   1 - (c.embedding <=> %s::vector) AS sim
              FROM chunk_entities ce
              JOIN chunks c ON c.id = ce.chunk_id
              JOIN documents d ON d.id = c.document_id
             WHERE ce.entity_id = ANY(%s) AND c.embedding IS NOT NULL
             GROUP BY c.id, d.id
             ORDER BY count(*) DESC, sim DESC
             LIMIT %s
            """,
            (vec, entity_ids, k),
        )
        rows = cur.fetchall()
    for r in rows:
        # 엔티티 적중 수를 가중치로 얹되 유사도 순서를 크게 흔들지 않게 한다
        r["score"] = float(r["sim"]) + min(int(r["hits"]), 5) * 0.03
    return rows


# ─────────────────────────── 조립 ───────────────────────────


def _gather(query: str, mode: str, vec: list[float], cfg: dict) -> dict[str, Any]:
    k_chunks = int(cfg["rag_top_k_chunks"])
    k_ents = int(cfg["rag_top_k_entities"])
    k_rels = int(cfg["rag_top_k_relations"])
    depth = int(cfg["rag_graph_depth"])

    chunks: dict[int, dict] = {}
    entities: dict[int, dict] = {}
    relations: dict[int, dict] = {}

    def add_chunks(rows: list[dict]) -> None:
        for r in rows:
            cur = chunks.get(r["id"])
            if cur is None or float(r["score"]) > float(cur["score"]):
                chunks[r["id"]] = r

    if mode in ("naive", "hybrid"):
        add_chunks(_naive_chunks(vec, k_chunks))

    if mode in ("local", "hybrid"):
        seeds = _seed_entities(vec, k_ents)
        for e in seeds:
            entities[e["id"]] = e
        neighbors = _expand([e["id"] for e in seeds], depth, k_ents)
        for e in neighbors:
            entities.setdefault(e["id"], e)
        ids = list(entities)
        for r in _relations_for(ids, k_rels):
            relations[r["id"]] = r
        add_chunks(_chunks_for_entities(ids, vec, k_chunks))

    if mode in ("global", "hybrid"):
        rels = _relations_by_keyword(keywords(query), k_rels)
        ends: list[int] = []
        for r in rels:
            relations.setdefault(r["id"], r)
            ends += [r["src_id"], r["tgt_id"]]
        if ends:
            with db.cursor(commit=False) as cur:
                cur.execute(
                    "SELECT id, name, type, description, degree FROM entities WHERE id = ANY(%s)",
                    (list(set(ends)),),
                )
                for e in cur.fetchall():
                    entities.setdefault(e["id"], e)
            add_chunks(_chunks_for_entities(list(set(ends)), vec, k_chunks))

    top = sorted(chunks.values(), key=lambda r: float(r["score"]), reverse=True)[:k_chunks]
    ents = sorted(entities.values(), key=lambda e: (-(e.get("degree") or 0), e["name"]))[:k_ents]
    rels = sorted(relations.values(), key=lambda r: -float(r["weight"]))[:k_rels]
    return {"chunks": top, "entities": ents, "relations": rels}


def _render(bundle: dict[str, Any]) -> tuple[str, list[dict]]:
    blocks: list[str] = []

    if bundle["entities"]:
        lines = ["-----Entities-----", "name | type | description"]
        lines += [
            f"{e['name']} | {e['type']} | {(e['description'] or '')[:200]}"
            for e in bundle["entities"]
        ]
        blocks.append("\n".join(lines))

    if bundle["relations"]:
        lines = ["-----Relations-----", "source | target | description"]
        lines += [
            f"{r['src']} | {r['tgt']} | {(r['description'] or '')[:200]}"
            for r in bundle["relations"]
        ]
        blocks.append("\n".join(lines))

    citations: list[dict] = []
    if bundle["chunks"]:
        lines = ["-----Sources-----"]
        for i, c in enumerate(bundle["chunks"], 1):
            tag = f"S{i}"
            lines.append(f"[{tag}] ({c['path']})\n{c['content']}")
            citations.append({
                "tag": tag,
                "path": c["path"],
                "document_id": c["document_id"],
                "chunk_id": c["id"],
                "excerpt": c["content"][:EXCERPT],
                "score": round(float(c["score"]), 4),
            })
        blocks.append("\n\n".join(lines))

    return ("[참고 자료]\n" + "\n\n".join(blocks)) if blocks else "", citations


async def retrieve(query: str, mode: str, cfg: dict) -> RagContext:
    started = time.perf_counter()
    try:
        vectors = await ollama.embed([query], model=str(cfg["embed_model"]))
    except Exception as exc:  # noqa: BLE001 - 임베딩이 죽어도 채팅은 계속돼야 한다
        log.warning("질문 임베딩 실패: %s", exc)
        return RagContext(stats={"mode": mode, "error": str(exc)}, empty=True)

    vec = vectors[0]
    bundle = await anyio.to_thread.run_sync(_gather, query, mode, vec, cfg)
    prompt_block, citations = _render(bundle)

    return RagContext(
        prompt_block=prompt_block,
        citations=citations,
        stats={
            "mode": mode,
            "chunks": len(bundle["chunks"]),
            "entities": len(bundle["entities"]),
            "relations": len(bundle["relations"]),
            "ms": int((time.perf_counter() - started) * 1000),
        },
        empty=not prompt_block,
    )
