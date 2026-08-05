"""채팅 세션 목록/생성/삭제/설정 + 메시지 조회."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from .. import db, deps
from ..config import RAG_MODES
from .personas import default_persona

router = APIRouter(prefix="/api/sessions", tags=["sessions"], dependencies=[deps.Auth])

_COLS = ("id, title, persona_id, model, rag_enabled, rag_mode, web_enabled, "
         "created_at, updated_at")


class SessionIn(BaseModel):
    title: str | None = None
    persona_id: int | None = None
    model: str | None = None
    rag_enabled: bool | None = None
    rag_mode: str | None = None
    web_enabled: bool | None = None


class SessionPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    persona_id: int | None = None
    model: str | None = None
    rag_enabled: bool | None = None
    rag_mode: str | None = None
    web_enabled: bool | None = None


def load_session(session_id: int) -> dict:
    with db.cursor(commit=False) as cur:
        cur.execute(f"SELECT {_COLS} FROM chat_sessions WHERE id = %s", (session_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="세션을 찾을 수 없습니다")
    return row


@router.get("")
def list_sessions(limit: int = Query(100, ge=1, le=500)) -> dict:
    with db.cursor(commit=False) as cur:
        cur.execute(
            f"""
            SELECT s.{_COLS.replace(', ', ', s.')},
                   p.name AS persona_name,
                   (SELECT count(*) FROM chat_messages m WHERE m.session_id = s.id) AS message_count
              FROM chat_sessions s
              LEFT JOIN personas p ON p.id = s.persona_id
             ORDER BY s.updated_at DESC
             LIMIT %s
            """,
            (limit,),
        )
        return {"sessions": cur.fetchall()}


@router.post("")
def create(body: SessionIn, cfg: deps.Settings) -> dict:
    persona = default_persona() if body.persona_id is None else None
    persona_id = body.persona_id if body.persona_id is not None else (persona or {}).get("id")

    mode = body.rag_mode or cfg["rag_default_mode"]
    if mode not in RAG_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"rag_mode 는 {RAG_MODES} 중 하나")

    enabled = cfg["rag_default_enabled"] if body.rag_enabled is None else body.rag_enabled
    web = cfg["web_search_default_enabled"] if body.web_enabled is None else body.web_enabled

    with db.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO chat_sessions
                   (title, persona_id, model, rag_enabled, rag_mode, web_enabled)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_COLS}
            """,
            (body.title or "새 대화", persona_id, body.model, enabled, mode, web),
        )
        return {"session": cur.fetchone()}


@router.get("/{session_id}")
def get_session(session_id: int) -> dict:
    return {"session": load_session(session_id)}


@router.patch("/{session_id}")
def update(session_id: int, body: SessionPatch) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="변경할 항목이 없습니다")
    if "rag_mode" in fields and fields["rag_mode"] not in RAG_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"rag_mode 는 {RAG_MODES} 중 하나")

    sets = ", ".join(f"{k} = %s" for k in fields)
    with db.cursor() as cur:
        cur.execute(
            f"UPDATE chat_sessions SET {sets}, updated_at = now() WHERE id = %s RETURNING {_COLS}",
            (*fields.values(), session_id),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="세션을 찾을 수 없습니다")
    return {"session": row}


@router.delete("/{session_id}")
def delete(session_id: int) -> dict:
    with db.cursor() as cur:
        cur.execute("DELETE FROM chat_sessions WHERE id = %s RETURNING id", (session_id,))
        if not cur.fetchone():
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="세션을 찾을 수 없습니다")
    return {"ok": True}


@router.get("/{session_id}/messages")
def messages(session_id: int, limit: int = Query(500, ge=1, le=2000)) -> dict:
    load_session(session_id)
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, role, content, thinking, citations, model, attachments, created_at
              FROM chat_messages
             WHERE session_id = %s
             ORDER BY id
             LIMIT %s
            """,
            (session_id, limit),
        )
        return {"messages": cur.fetchall()}


@router.delete("/{session_id}/messages")
def clear_messages(session_id: int) -> dict:
    load_session(session_id)
    with db.cursor() as cur:
        cur.execute("DELETE FROM chat_messages WHERE session_id = %s", (session_id,))
    return {"ok": True}
