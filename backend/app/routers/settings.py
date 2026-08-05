"""설정 페이지 백엔드: app_settings 조회/저장 + Ollama 모델 목록."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Body, HTTPException, status
from pydantic import BaseModel

from .. import db, deepseek, deps, flags, ollama, paths, remote
from ..config import ADMIN_SETTINGS, DEFAULT_SETTINGS, EXTRACT_PROVIDERS, RAG_MODES

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
    "extract_max_entities": (1, 60),
    "extract_max_relations": (1, 60),
    "deepseek_concurrency": (1, 16),
    "deepseek_max_input_chars": (500, 20000),
    "deepseek_max_output_tokens": (100, 8000),
    "deepseek_token_budget": (0, 1_000_000_000),
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

    if key == "extract_provider":
        if value not in EXTRACT_PROVIDERS:
            raise HTTPException(
                400, detail=f"extract_provider: {EXTRACT_PROVIDERS} 중 하나여야 합니다"
            )
        if value == "deepseek" and not deepseek.configured():
            raise HTTPException(
                400, detail="DeepSeek API 키를 먼저 입력하세요"
            )

    if key == "rag_watch_dirs":
        # 감시 폴더는 NAS 루트 기준 상대경로여야 한다
        value = sorted({paths.normalize(v) for v in value} - {""})

    if key == "nas_preview_exts":
        exts = (e.lower().strip() for e in value)
        value = sorted({e if e.startswith(".") else f".{e}" for e in exts if e})

    return value


@router.get("")
def get_settings(user: deps.CurrentUser) -> dict:
    return {"settings": db.get_settings(), "defaults": DEFAULT_SETTINGS,
            "rag_modes": list(RAG_MODES), "admin_keys": sorted(ADMIN_SETTINGS),
            "is_admin": user.is_admin}


@router.put("")
def put_settings(user: deps.CurrentUser, patch: dict[str, Any] = Body(...)) -> dict:
    unknown = [k for k in patch if k not in DEFAULT_SETTINGS]
    if unknown:
        raise HTTPException(400, detail=f"알 수 없는 설정 키: {unknown}")

    admin_keys = [k for k in patch if k in ADMIN_SETTINGS]
    if admin_keys and not user.is_admin:
        raise HTTPException(403, detail=f"관리자만 바꿀 수 있는 설정입니다: {admin_keys}")

    clean = {k: _coerce(k, v) for k, v in patch.items()}

    # 청크 오버랩이 청크 크기보다 크면 청킹이 무한루프에 빠진다
    merged = {**db.get_settings(), **clean}
    if merged["rag_chunk_overlap"] >= merged["rag_chunk_size"]:
        raise HTTPException(400, detail="rag_chunk_overlap 은 rag_chunk_size 보다 작아야 합니다")

    return {"settings": db.save_settings(clean)}


@router.get("/models")
async def models() -> dict:
    """Ollama 모델 목록 프록시. 채팅/추출/임베딩 모델 셀렉트박스용."""
    # Ollama 가 꺼져 있어도 외부 API 모델은 쓸 수 있어야 하므로 실패를 삼킨다
    ollama_error = ""
    try:
        raw = await ollama.list_models()
    except Exception as exc:  # noqa: BLE001
        raw, ollama_error = [], f"Ollama 에 연결할 수 없습니다: {exc}"

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

    # DeepSeek 채팅 모델도 같은 목록에 실어 준다. 이름 앞에 deepseek/ 를 붙여 구분한다.
    if deepseek.configured():
        for m in deepseek.CHAT_MODELS:
            items.append({
                "name": f"{deepseek.MODEL_PREFIX}{m['id']}",
                "size": None,
                "family": "deepseek",
                "parameter_size": None,
                "capabilities": ["completion"] + (["thinking"] if m["thinking"] else []),
                "embedding": False,
                "thinking": m["thinking"],
                "remote": True,
                "label": m["label"],
            })

    # OpenAI / Claude 는 모델 목록을 API 로 조회한다 (이름을 상수로 박으면 금방 낡는다)
    for name, p in remote.PROVIDERS.items():
        if not remote.configured(name):
            continue
        for m in await remote.list_models(name):
            items.append({
                "name": f"{p.prefix}{m['id']}",
                "size": None,
                "family": name,
                "parameter_size": None,
                "capabilities": ["completion"] + (["thinking"] if m["thinking"] else []),
                "embedding": False,
                "thinking": m["thinking"],
                "remote": True,
                "label": m["label"],
            })

    if not items and ollama_error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=ollama_error)
    return {"models": items, "ollama_error": ollama_error}


@router.get("/providers")
def providers(cfg: deps.Settings) -> dict:
    """제공자 상태와 누적 토큰 사용량. 키 원문은 절대 담지 않는다 (마스킹만)."""
    usage = deepseek.usage_summary()
    budget = int(cfg.get("deepseek_token_budget") or 0)
    return {
        "current": cfg.get("extract_provider", "local"),
        "options": list(EXTRACT_PROVIDERS),
        "deepseek": {
            "configured": deepseek.configured(),
            "key_source": deepseek.key_source(),  # env | db | ''
            "key_masked": deepseek.masked_key(),
            "base_url": deepseek.base_url(),
            "base_url_from_env": bool(os.getenv("DEEPSEEK_BASE_URL", "").strip()),
            "model": cfg.get("deepseek_model"),
            "usage": usage,
            "budget": budget,
            "budget_left": max(0, budget - int(usage["total_tokens"])) if budget else None,
        },
        # 채팅 전용 외부 제공자 (OpenAI / Claude). 키 원문은 여기에도 담지 않는다.
        "chat": [
            {
                "name": name,
                "label": p.label,
                "prefix": p.prefix,
                "key_hint": p.key_hint,
                "env": p.env,
                "configured": remote.configured(name),
                "key_source": remote.key_source(name),
                "key_masked": remote.masked_key(name),
                "usage": remote.usage_summary(name),
            }
            for name, p in remote.PROVIDERS.items()
        ],
    }


class KeyIn(BaseModel):
    key: str = ""


@router.put("/providers/deepseek/key", dependencies=[deps.AdminOnly])
def set_deepseek_key(body: KeyIn) -> dict:
    """웹에서 키 저장. 빈 문자열이면 삭제한다. 저장된 값은 되돌려주지 않는다."""
    if os.getenv("DEEPSEEK_API_KEY", "").strip():
        raise HTTPException(
            400,
            detail="키가 이미 .env(환경변수)로 지정돼 있어 웹에서 바꿀 수 없습니다. "
                   ".env 에서 DEEPSEEK_API_KEY 를 지우고 재시작하세요",
        )
    key = body.key.strip()
    if key and len(key) < 8:
        raise HTTPException(400, detail="키가 너무 짧습니다")
    deepseek.set_key(key or None)
    return {"ok": True, "configured": deepseek.configured(), "masked": deepseek.masked_key()}


@router.post("/providers/deepseek/test", dependencies=[deps.AdminOnly])
def test_deepseek(cfg: deps.Settings) -> dict:
    """키가 실제로 동작하는지 최소 토큰으로 한 번 호출해 본다."""
    return deepseek.ping(str(cfg.get("deepseek_model") or "deepseek-chat"))


@router.put("/providers/{name}/key", dependencies=[deps.AdminOnly])
def set_provider_key(name: str, body: KeyIn) -> dict:
    """OpenAI / Claude 키 저장. 빈 문자열이면 삭제. 저장된 값은 되돌려주지 않는다."""
    if name not in remote.PROVIDERS:
        raise HTTPException(404, detail=f"알 수 없는 제공자: {name}")
    provider = remote.PROVIDERS[name]
    if os.getenv(provider.env, "").strip():
        raise HTTPException(
            400,
            detail=f"키가 이미 .env({provider.env})로 지정돼 있어 웹에서 바꿀 수 없습니다",
        )
    key = body.key.strip()
    if key and len(key) < 8:
        raise HTTPException(400, detail="키가 너무 짧습니다")
    remote.set_key(name, key or None)
    return {"ok": True, "configured": remote.configured(name), "masked": remote.masked_key(name)}


@router.post("/providers/{name}/test", dependencies=[deps.AdminOnly])
async def test_provider(name: str, model: str = "") -> dict:
    if name not in remote.PROVIDERS:
        raise HTTPException(404, detail=f"알 수 없는 제공자: {name}")
    if not model:
        models = await remote.list_models(name)
        if not models:
            return {"ok": False, "error": "모델 목록을 가져오지 못했습니다 (키를 확인하세요)"}
        model = models[0]["id"]
    return await remote.ping(name, model)


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
