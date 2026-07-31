"""설정 페이지 백엔드: app_settings 조회/저장 + Ollama 모델 목록."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, status

from .. import db, deps, flags, ollama, paths
from ..config import DEFAULT_SETTINGS, RAG_MODES

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[deps.Auth])

# 값이 벗어나면 저장을 거부할 범위 (min, max)
_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (0.0, 2.0),
    "num_ctx": (1024, 262144),
    "history_turns": (1, 100),
    "rag_chunk_size": (200, 8000),
    "rag_chunk_overlap": (0, 2000),
    "rag_top_k_chunks": (1, 50),
    "rag_top_k_entities": (1, 100),
    "rag_top_k_relations": (1, 100),
    "rag_graph_depth": (0, 2),
    "rag_max_file_mb": (1, 1024),
    "rag_top_k_keyword": (1, 50),
    "rag_ocr_min_chars": (0, 10000),
    "chat_attach_max_mb": (1, 256),
    "session_days": (1, 365),
    "file_unlock_minutes": (1, 1440),
}


def _coerce(key: str, value: Any) -> Any:
    """DEFAULT_SETTINGS 의 타입에 맞춰 캐스팅하고 범위를 검증한다."""
    default = DEFAULT_SETTINGS[key]

    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)

    if isinstance(default, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise HTTPException(400, detail=f"{key}: 정수가 필요합니다") from None
    elif isinstance(default, float):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, detail=f"{key}: 숫자가 필요합니다") from None
    elif isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise HTTPException(400, detail=f"{key}: 문자열 배열이 필요합니다")
        value = [v.strip() for v in value if v.strip()]
    elif isinstance(default, str):
        if not isinstance(value, str):
            raise HTTPException(400, detail=f"{key}: 문자열이 필요합니다")
        value = value.strip()

    if key in _RANGES:
        lo, hi = _RANGES[key]
        if not (lo <= value <= hi):
            raise HTTPException(400, detail=f"{key}: {lo}~{hi} 범위를 벗어났습니다")

    if key == "rag_default_mode" and value not in RAG_MODES:
        raise HTTPException(400, detail=f"rag_default_mode: {RAG_MODES} 중 하나여야 합니다")

    if key == "rag_watch_dirs":
        # 감시 폴더는 NAS 루트 기준 상대경로여야 한다
        value = sorted({paths.normalize(v) for v in value} - {""})

    if key == "nas_preview_exts":
        exts = (e.lower().strip() for e in value)
        value = sorted({e if e.startswith(".") else f".{e}" for e in exts if e})

    return value


@router.get("")
def get_settings() -> dict:
    return {"settings": db.get_settings(), "defaults": DEFAULT_SETTINGS,
            "rag_modes": list(RAG_MODES)}


@router.put("")
def put_settings(patch: dict[str, Any] = Body(...)) -> dict:
    unknown = [k for k in patch if k not in DEFAULT_SETTINGS]
    if unknown:
        raise HTTPException(400, detail=f"알 수 없는 설정 키: {unknown}")

    clean = {k: _coerce(k, v) for k, v in patch.items()}

    # 청크 오버랩이 청크 크기보다 크면 청킹이 무한루프에 빠진다
    merged = {**db.get_settings(), **clean}
    if merged["rag_chunk_overlap"] >= merged["rag_chunk_size"]:
        raise HTTPException(400, detail="rag_chunk_overlap 은 rag_chunk_size 보다 작아야 합니다")

    return {"settings": db.save_settings(clean)}


@router.get("/models")
async def models() -> dict:
    """Ollama 모델 목록 프록시. 채팅/추출/임베딩 모델 셀렉트박스용."""
    try:
        raw = await ollama.list_models()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=f"Ollama 에 연결할 수 없습니다: {exc}"
        ) from exc

    items = []
    for m in raw:
        caps = m.get("capabilities") or []
        details = m.get("details") or {}
        items.append({
            "name": m.get("name"),
            "size": m.get("size"),
            "family": details.get("family"),
            "parameter_size": details.get("parameter_size"),
            "capabilities": caps,
            "embedding": "embedding" in caps,
            "thinking": "thinking" in caps,
        })
    items.sort(key=lambda x: x["name"] or "")
    return {"models": items}


@router.get("/ocr")
def ocr_status() -> dict:
    """tesseract 설치 여부와 쓸 수 있는 언어 (설정 페이지 RAG 탭)."""
    from ..rag.extract import available_langs

    langs = available_langs()
    return {"available": bool(langs), "langs": langs}


@router.get("/flags")
def list_flags() -> dict:
    """숨김/잠금이 걸린 경로 전체 (설정 페이지 보안 탭에서 한눈에 보기)."""
    return {"flags": flags.list_all()}
