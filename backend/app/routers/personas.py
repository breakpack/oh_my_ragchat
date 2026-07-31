"""페르소나 CRUD — system_prompt + 모델 + temperature 묶음."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from .. import db, deps

router = APIRouter(prefix="/api/personas", tags=["personas"], dependencies=[deps.Auth])

_COLS = "id, name, system_prompt, model, temperature, is_default, created_at"


class PersonaIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    system_prompt: str = ""
    model: str | None = None
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    is_default: bool = False


class PersonaPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    system_prompt: str | None = None
    model: str | None = None
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    is_default: bool | None = None


def _clear_default(cur, keep_id: int | None = None) -> None:
    """부분 유니크 인덱스(is_default WHERE is_default) 때문에 먼저 비워야 한다."""
    cur.execute(
        "UPDATE personas SET is_default = false WHERE is_default AND id <> %s",
        (keep_id or -1,),
    )


@router.get("")
def list_personas() -> dict:
    with db.cursor(commit=False) as cur:
        cur.execute(f"SELECT {_COLS} FROM personas ORDER BY is_default DESC, id")
        return {"personas": cur.fetchall()}


@router.post("")
def create(body: PersonaIn) -> dict:
    with db.cursor() as cur:
        if body.is_default:
            _clear_default(cur)
        cur.execute(
            f"""
            INSERT INTO personas (name, system_prompt, model, temperature, is_default)
            VALUES (%s, %s, %s, %s, %s) RETURNING {_COLS}
            """,
            (body.name, body.system_prompt, body.model, body.temperature, body.is_default),
        )
        return {"persona": cur.fetchone()}


@router.patch("/{persona_id}")
def update(persona_id: int, body: PersonaPatch) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="변경할 항목이 없습니다")

    with db.cursor() as cur:
        if fields.get("is_default"):
            _clear_default(cur, persona_id)
        sets = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(
            f"UPDATE personas SET {sets} WHERE id = %s RETURNING {_COLS}",
            (*fields.values(), persona_id),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="페르소나를 찾을 수 없습니다")
    return {"persona": row}


@router.delete("/{persona_id}")
def delete(persona_id: int) -> dict:
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM personas")
        if cur.fetchone()["n"] <= 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail="마지막 페르소나는 삭제할 수 없습니다"
            )
        cur.execute("DELETE FROM personas WHERE id = %s RETURNING is_default", (persona_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="페르소나를 찾을 수 없습니다")
        if row["is_default"]:  # 기본값이 사라지면 가장 오래된 것을 승격
            cur.execute(
                "UPDATE personas SET is_default = true WHERE id = (SELECT min(id) FROM personas)"
            )
    return {"ok": True}


def default_persona() -> dict | None:
    with db.cursor(commit=False) as cur:
        cur.execute(f"SELECT {_COLS} FROM personas WHERE is_default LIMIT 1")
        row = cur.fetchone()
        if row is None:
            cur.execute(f"SELECT {_COLS} FROM personas ORDER BY id LIMIT 1")
            row = cur.fetchone()
    return row


def get_persona(persona_id: int | None) -> dict | None:
    if persona_id is None:
        return default_persona()
    with db.cursor(commit=False) as cur:
        cur.execute(f"SELECT {_COLS} FROM personas WHERE id = %s", (persona_id,))
        return cur.fetchone() or default_persona()
