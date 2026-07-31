"""청크에서 엔티티/관계를 뽑아 그래프 테이블에 병합한다 (LightRAG 방식).

로컬 LLM 은 자유형식 출력이 흔들리므로 Ollama 의 구조화 출력(JSON Schema)을 강제한다.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from .. import db, ollama

log = logging.getLogger("chatchat.rag.graph")

ENTITY_TYPES = ("사람", "조직", "장소", "제품", "기술", "개념", "사건", "날짜", "기타")

SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": list(ENTITY_TYPES)},
                    "description": {"type": "string"},
                },
                "required": ["name", "type", "description"],
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "description": {"type": "string"},
                    "keywords": {"type": "string"},
                    "weight": {"type": "number"},
                },
                "required": ["source", "target", "description", "keywords"],
            },
        },
    },
    "required": ["entities", "relations"],
}

SYSTEM = (
    "너는 지식 그래프 구축기다. 주어진 텍스트에서 핵심 엔티티와 그들 사이의 관계를 뽑아 "
    "JSON 으로만 답한다. 텍스트에 실제로 등장하는 것만 추출하고 추측하지 마라. "
    "엔티티 이름은 텍스트에 나온 표기를 그대로 쓴다. 설명은 한국어 한 문장으로 짧게 쓴다. "
    "관계의 weight 는 관련도를 0.1~1.0 으로 매긴다. 뽑을 것이 없으면 빈 배열을 반환한다."
)

PROMPT = """다음 텍스트에서 엔티티와 관계를 추출하라.

--- 텍스트 시작 ---
{text}
--- 텍스트 끝 ---
"""

_NORM = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    return _NORM.sub(" ", name.strip().lower())


def extract_graph(
    text: str, model: str, *, num_ctx: int = 8192, client: httpx.Client | None = None
) -> tuple[list[dict], list[dict]]:
    raw = ollama.generate_sync(
        model,
        PROMPT.format(text=text[:12000]),
        system=SYSTEM,
        fmt=SCHEMA,
        temperature=0.0,
        num_ctx=num_ctx,
        client=client,
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("엔티티 추출 JSON 파싱 실패 (앞 200자): %s", raw[:200])
        return [], []

    entities, seen = [], set()
    for e in data.get("entities") or []:
        name = str(e.get("name") or "").strip()
        norm = normalize_name(name)
        if not norm or len(norm) > 200 or norm in seen:
            continue
        seen.add(norm)
        entities.append({
            "name": name,
            "name_norm": norm,
            "type": str(e.get("type") or "기타")[:40],
            "description": str(e.get("description") or "").strip()[:600],
        })

    relations = []
    for r in data.get("relations") or []:
        src, tgt = normalize_name(str(r.get("source") or "")), normalize_name(str(r.get("target") or ""))
        if not src or not tgt or src == tgt:
            continue
        try:
            weight = float(r.get("weight") or 0.5)
        except (TypeError, ValueError):
            weight = 0.5
        relations.append({
            "src": src,
            "tgt": tgt,
            "description": str(r.get("description") or "").strip()[:600],
            "keywords": str(r.get("keywords") or "").strip()[:300],
            "weight": max(0.1, min(weight, 1.0)),
        })
    return entities, relations


# ─────────────────────────── 병합 ───────────────────────────


def merge_entities(entities: list[dict], embeddings: dict[str, list[float]]) -> dict[str, int]:
    """name_norm → id. 기존 엔티티는 설명을 누적한다."""
    if not entities:
        return {}
    ids: dict[str, int] = {}
    with db.cursor() as cur:
        for e in entities:
            emb = embeddings.get(e["name_norm"])
            cur.execute(
                """
                INSERT INTO entities (name_norm, name, type, description, embedding)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name_norm) DO UPDATE
                   SET description = CASE
                         WHEN entities.description = '' THEN EXCLUDED.description
                         WHEN EXCLUDED.description = '' THEN entities.description
                         WHEN position(EXCLUDED.description in entities.description) > 0
                              THEN entities.description
                         ELSE left(entities.description || ' / ' || EXCLUDED.description, 3000)
                       END,
                       type = CASE WHEN entities.type = 'unknown' OR entities.type = '기타'
                                   THEN EXCLUDED.type ELSE entities.type END,
                       embedding = COALESCE(EXCLUDED.embedding, entities.embedding)
                RETURNING id
                """,
                (e["name_norm"], e["name"], e["type"], e["description"], emb),
            )
            ids[e["name_norm"]] = cur.fetchone()["id"]
    return ids


def merge_relations(relations: list[dict], ids: dict[str, int]) -> int:
    """양 끝 엔티티가 모두 아는 이름일 때만 저장. weight 는 누적한다."""
    pairs = 0
    with db.cursor() as cur:
        for r in relations:
            src_id, tgt_id = ids.get(r["src"]), ids.get(r["tgt"])
            if not src_id or not tgt_id:
                continue
            if src_id > tgt_id:  # (a,b)/(b,a) 중복을 막으려 방향을 정규화
                src_id, tgt_id = tgt_id, src_id
            cur.execute(
                """
                INSERT INTO relations (src_id, tgt_id, description, keywords, weight)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (src_id, tgt_id) DO UPDATE
                   SET weight = least(relations.weight + EXCLUDED.weight, 10.0),
                       description = CASE
                         WHEN position(EXCLUDED.description in relations.description) > 0
                              THEN relations.description
                         ELSE left(relations.description || ' / ' || EXCLUDED.description, 3000)
                       END,
                       keywords = left(relations.keywords || ' ' || EXCLUDED.keywords, 1000)
                """,
                (src_id, tgt_id, r["description"], r["keywords"], r["weight"]),
            )
            pairs += 1
    return pairs


def link_chunk(chunk_id: int, entity_ids: list[int]) -> None:
    if not entity_ids:
        return
    with db.cursor() as cur:
        cur.executemany(
            "INSERT INTO chunk_entities (chunk_id, entity_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            [(chunk_id, eid) for eid in entity_ids],
        )


def refresh_degrees() -> None:
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE entities e SET degree = COALESCE(d.n, 0)
              FROM (
                SELECT id, (SELECT count(*) FROM relations r
                             WHERE r.src_id = e2.id OR r.tgt_id = e2.id) AS n
                  FROM entities e2
              ) d
             WHERE d.id = e.id AND e.degree IS DISTINCT FROM COALESCE(d.n, 0)
            """
        )


def prune_orphans() -> int:
    """어느 청크와도 연결되지 않은 엔티티를 정리 (문서 삭제 후)."""
    with db.cursor() as cur:
        cur.execute(
            """
            DELETE FROM entities e
             WHERE NOT EXISTS (SELECT 1 FROM chunk_entities ce WHERE ce.entity_id = e.id)
            """
        )
        return cur.rowcount
