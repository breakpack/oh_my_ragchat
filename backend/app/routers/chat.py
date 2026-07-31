"""채팅 SSE 스트리밍.

EventSource 는 POST 를 못 하므로 프론트는 fetch + ReadableStream 으로 이 응답을 읽는다.
이벤트 순서: meta → (citation)* → (thinking|token)* → done | error
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import db, deps, ollama
from ..config import RAG_MODES
from ..rag.retrieve import retrieve
from .personas import get_persona
from .sessions import load_session

log = logging.getLogger("chatchat.chat")
router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[deps.Auth])

# 단일 사용자 · 단일 프로세스 전제라 중단 신호는 메모리에 둔다
_stopped: set[int] = set()

RAG_INSTRUCTION = (
    "아래 [참고 자료]는 사용자의 개인 문서에서 검색한 내용이다. "
    "답변에 자료를 활용했다면 해당 문장 끝에 [S1] 같은 출처 태그를 붙여라. "
    "자료에 없는 내용을 자료인 것처럼 말하지 마라.\n\n"
)
RAG_EMPTY_INSTRUCTION = (
    "사용자의 개인 문서를 검색했지만 관련 자료를 찾지 못했다. "
    "그 사실을 짧게 알린 뒤 일반 지식으로 답하라.\n\n"
)


class MessageIn(BaseModel):
    content: str = Field(min_length=1)
    rag_enabled: bool | None = None  # 메시지 단위 오버라이드
    rag_mode: str | None = None
    model: str | None = None
    temperature: float | None = Field(None, ge=0.0, le=2.0)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _history(session_id: int, turns: int) -> list[dict[str, str]]:
    """최근 N 턴만 (user+assistant 를 한 턴으로 센다). 방금 넣은 user 메시지는 제외."""
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT role, content FROM chat_messages
             WHERE session_id = %s AND role <> 'system' AND content <> ''
             ORDER BY id DESC
             LIMIT %s
            """,
            (session_id, turns * 2 + 1),
        )
        rows = cur.fetchall()
    rows.reverse()
    return [{"role": r["role"], "content": r["content"]} for r in rows[:-1]]


def _save(session_id: int, role: str, content: str, **extra: Any) -> int:
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chat_messages (session_id, role, content, thinking, citations, model)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                session_id,
                role,
                content,
                extra.get("thinking") or None,
                json.dumps(extra["citations"], ensure_ascii=False) if extra.get("citations") else None,
                extra.get("model"),
            ),
        )
        msg_id = cur.fetchone()["id"]
        cur.execute("UPDATE chat_sessions SET updated_at = now() WHERE id = %s", (session_id,))
    return msg_id


def _autotitle(session: dict, content: str) -> str | None:
    if session["title"] not in ("새 대화", "", None):
        return None
    title = " ".join(content.split())[:40]
    with db.cursor() as cur:
        cur.execute("UPDATE chat_sessions SET title = %s WHERE id = %s", (title, session["id"]))
    return title


@router.post("/sessions/{session_id}/messages")
async def send(session_id: int, body: MessageIn, cfg: deps.Settings) -> StreamingResponse:
    session = load_session(session_id)
    persona = get_persona(session["persona_id"])

    use_rag = session["rag_enabled"] if body.rag_enabled is None else body.rag_enabled
    mode = body.rag_mode or session["rag_mode"] or cfg["rag_default_mode"]
    if mode not in RAG_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"rag_mode 는 {RAG_MODES} 중 하나")

    model = body.model or session["model"] or (persona or {}).get("model") or cfg["chat_model"]
    temperature = body.temperature
    if temperature is None:
        temperature = (persona or {}).get("temperature")
    if temperature is None:
        temperature = cfg["temperature"]

    _save(session_id, "user", body.content)
    new_title = _autotitle(session, body.content)
    _stopped.discard(session_id)

    async def stream() -> AsyncIterator[str]:
        citations: list[dict] = []
        rag_stats: dict | None = None
        answer: list[str] = []
        thinking: list[str] = []
        try:
            system_parts: list[str] = []
            if persona and persona["system_prompt"]:
                system_parts.append(persona["system_prompt"])

            if use_rag:
                ctx = await retrieve(body.content, mode, cfg)
                rag_stats = ctx.stats
                if ctx.empty:
                    system_parts.append(RAG_EMPTY_INSTRUCTION.strip())
                else:
                    system_parts.append(RAG_INSTRUCTION + ctx.prompt_block)
                    citations = ctx.citations

            yield _sse("meta", {
                "model": model,
                "rag": bool(use_rag),
                "rag_mode": mode if use_rag else None,
                "rag_stats": rag_stats,
                "persona": (persona or {}).get("name"),
                "title": new_title,
            })
            for c in citations:
                yield _sse("citation", c)

            messages: list[dict[str, str]] = []
            if system_parts:
                messages.append({"role": "system", "content": "\n\n".join(system_parts)})
            messages += _history(session_id, int(cfg["history_turns"]))
            messages.append({"role": "user", "content": body.content})

            async for chunk in ollama.chat_stream(
                model,
                messages,
                temperature=float(temperature),
                num_ctx=int(cfg["num_ctx"]),
            ):
                if session_id in _stopped:
                    _stopped.discard(session_id)
                    yield _sse("done", {"stopped": True})
                    break

                if chunk["thinking"] and cfg["show_thinking"]:
                    thinking.append(chunk["thinking"])
                    yield _sse("thinking", {"t": chunk["thinking"]})
                if chunk["content"]:
                    answer.append(chunk["content"])
                    yield _sse("token", {"t": chunk["content"]})
                if chunk["done"]:
                    yield _sse("done", {"stopped": False})

            text = "".join(answer)
            if text or thinking:
                msg_id = _save(
                    session_id, "assistant", text,
                    thinking="".join(thinking), citations=citations, model=model,
                )
                yield _sse("saved", {"message_id": msg_id})

        except asyncio.CancelledError:  # 브라우저가 연결을 끊음 — 여기까지 받은 답은 살린다
            text = "".join(answer)
            if text:
                _save(session_id, "assistant", text, citations=citations, model=model)
            raise
        except Exception as exc:  # noqa: BLE001 - 스트림 안에서는 에러를 이벤트로 전달한다
            log.exception("chat stream failed")
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            _stopped.discard(session_id)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/stop/{session_id}")
def stop(session_id: int) -> dict:
    _stopped.add(session_id)
    return {"ok": True}


@router.post("/preview")
async def preview(body: MessageIn, cfg: deps.Settings) -> dict:
    """RAG 컨텍스트만 미리 확인 (설정/디버깅용)."""
    mode = body.rag_mode or cfg["rag_default_mode"]
    ctx = await retrieve(body.content, mode, cfg)
    return {"prompt_block": ctx.prompt_block, "citations": ctx.citations,
            "stats": ctx.stats, "empty": ctx.empty}
