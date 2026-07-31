"""DeepSeek(OpenAI 호환) 클라이언트 — 그래프 추출 가속용.

로컬 모델은 청크당 수 초씩 걸려 논문 PDF 한 건(수십 청크)에도 수 분이 든다.
외부 API 는 훨씬 빠르지만 토큰이 곧 비용이므로, 입력 길이·출력 토큰·추출 개수를
모두 상한으로 묶고 누적 사용량을 llm_usage 에 기록해 예산을 넘기면 로컬로 돌린다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import httpx

from . import db

log = logging.getLogger("chatchat.deepseek")

DEFAULT_BASE_URL = "https://api.deepseek.com"
SECRET_NAME = "deepseek_api_key"

# 청크마다 키를 조회하면 DB 왕복이 늘어난다. 짧게 캐시하고 저장 시 즉시 무효화한다.
_CACHE_TTL = 30.0
_cache: dict[str, Any] = {"value": None, "at": 0.0}
_lock = threading.Lock()


class DeepSeekError(RuntimeError):
    pass


# ─────────────────────────── 키 / 엔드포인트 ───────────────────────────
# 환경변수가 우선이다. .env 로 넣어둔 사람의 설정을 웹에서 덮어쓰지 않도록.


def _db_key() -> str:
    now = time.time()
    with _lock:
        if _cache["value"] is not None and now - _cache["at"] < _CACHE_TTL:
            return _cache["value"]
    try:
        with db.cursor(commit=False) as cur:
            cur.execute("SELECT value FROM secrets WHERE key = %s", (SECRET_NAME,))
            row = cur.fetchone()
        value = (row["value"] if row else "").strip()
    except Exception as exc:  # noqa: BLE001 - 마이그레이션 전이면 테이블이 없을 수 있다
        log.debug("secrets 조회 실패: %s", exc)
        value = ""
    with _lock:
        _cache.update(value=value, at=now)
    return value


def set_key(value: str | None) -> None:
    """웹에서 키 저장/삭제. 값은 어떤 API 로도 되돌려주지 않는다."""
    value = (value or "").strip()
    with db.cursor() as cur:
        if value:
            cur.execute(
                """
                INSERT INTO secrets (key, value, updated_at) VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (SECRET_NAME, value),
            )
        else:
            cur.execute("DELETE FROM secrets WHERE key = %s", (SECRET_NAME,))
    with _lock:
        _cache.update(value=None, at=0.0)


def api_key() -> str:
    return os.getenv("DEEPSEEK_API_KEY", "").strip() or _db_key()


def key_source() -> str:
    """'env' | 'db' | '' — 어디서 온 키인지."""
    if os.getenv("DEEPSEEK_API_KEY", "").strip():
        return "env"
    return "db" if _db_key() else ""


def masked_key() -> str:
    key = api_key()
    if not key:
        return ""
    return f"{key[:5]}…{key[-4:]}" if len(key) > 12 else "…" * 4


def base_url() -> str:
    env_url = os.getenv("DEEPSEEK_BASE_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    try:
        return str(db.get_setting("deepseek_base_url") or DEFAULT_BASE_URL).rstrip("/")
    except Exception:  # noqa: BLE001
        return DEFAULT_BASE_URL


def configured() -> bool:
    return bool(api_key())


# ─────────────────────────── 사용량 / 예산 ───────────────────────────


def record_usage(model: str, usage: dict[str, Any]) -> None:
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO llm_usage
                       (provider, model, prompt_tokens, completion_tokens, cached_tokens)
                VALUES ('deepseek', %s, %s, %s, %s)
                """,
                (
                    model,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                    int(usage.get("prompt_cache_hit_tokens") or 0),
                ),
            )
    except Exception as exc:  # noqa: BLE001 - 기록 실패로 추출을 멈추지 않는다
        log.debug("사용량 기록 실패: %s", exc)


def total_tokens() -> int:
    with db.cursor(commit=False) as cur:
        cur.execute(
            "SELECT COALESCE(sum(prompt_tokens + completion_tokens), 0) AS n "
            "FROM llm_usage WHERE provider = 'deepseek'"
        )
        return int(cur.fetchone()["n"])


def usage_summary() -> dict[str, Any]:
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT count(*) AS calls,
                   COALESCE(sum(prompt_tokens), 0)     AS prompt_tokens,
                   COALESCE(sum(completion_tokens), 0) AS completion_tokens,
                   COALESCE(sum(cached_tokens), 0)     AS cached_tokens,
                   max(created_at)                     AS last_call
              FROM llm_usage WHERE provider = 'deepseek'
            """
        )
        row = cur.fetchone()
        cur.execute(
            """
            SELECT count(*) AS calls,
                   COALESCE(sum(prompt_tokens + completion_tokens), 0) AS tokens
              FROM llm_usage
             WHERE provider = 'deepseek' AND created_at >= date_trunc('day', now())
            """
        )
        today = cur.fetchone()
    return {
        **row,
        "total_tokens": int(row["prompt_tokens"]) + int(row["completion_tokens"]),
        "today_calls": today["calls"],
        "today_tokens": today["tokens"],
    }


def budget_left(cfg: dict) -> int | None:
    """남은 토큰. 예산이 0(무제한)이면 None."""
    budget = int(cfg.get("deepseek_token_budget") or 0)
    if budget <= 0:
        return None
    return max(0, budget - total_tokens())


# ─────────────────────────── 호출 ───────────────────────────


def chat_json(
    system: str,
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    temperature: float = 0.0,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """JSON 모드로 한 번 호출. (파싱된 결과, usage) 를 돌려준다."""
    key = api_key()
    if not key:
        raise DeepSeekError("DeepSeek API 키가 설정되지 않았습니다")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        # DeepSeek 은 json_object 모드에서 프롬프트에 'json' 이 있어야 한다
        "response_format": {"type": "json_object"},
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    owned = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        r = client.post(
            f"{base_url()}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        if r.status_code >= 400:
            raise DeepSeekError(f"HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
    except httpx.HTTPError as exc:
        raise DeepSeekError(f"요청 실패: {exc}") from exc
    finally:
        if owned:
            client.close()

    usage = body.get("usage") or {}
    if usage:
        record_usage(model, usage)

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise DeepSeekError(f"예상과 다른 응답 형식: {str(body)[:200]}") from exc

    try:
        return json.loads(content), usage
    except json.JSONDecodeError as exc:
        raise DeepSeekError(f"JSON 파싱 실패: {content[:200]}") from exc


def ping(model: str = "deepseek-chat") -> dict[str, Any]:
    if not configured():
        return {"ok": False, "error": "DeepSeek API 키 미설정"}
    try:
        data, usage = chat_json(
            'JSON 으로만 답한다. 형식: {"ok": true}',
            'ok 를 true 로 하는 json 을 반환하라.',
            model=model,
            max_tokens=20,
            timeout=20,
        )
        return {"ok": True, "model": model, "tokens": usage.get("total_tokens")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
