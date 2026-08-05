"""외부 채팅 API 제공자 — OpenAI · Claude(Anthropic).

DeepSeek 은 그래프 추출까지 겸해서 deepseek.py 에 따로 있다. 여기 둘은 채팅 전용이고,
모델 이름 앞에 `openai/` `anthropic/` 를 붙여 Ollama 모델과 구분한다.

키는 사용자별 secrets 가 아니라 public.global_secrets 에 둔다 — 모델·외부 API 는
서버 전체 설정이라 관리자만 건드린다. 어떤 API 로도 원문을 되돌려주지 않는다(마스킹만).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import anyio
import httpx

from . import db

log = logging.getLogger("chatchat.remote")

# 채팅 한 번의 출력 상한. thinking 도 여기서 같이 소비된다.
MAX_TOKENS = 16000


class RemoteError(RuntimeError):
    pass


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    prefix: str
    env: str  # 환경변수가 있으면 그쪽이 우선한다
    base_url: str
    key_hint: str


PROVIDERS: dict[str, Provider] = {
    p.name: p
    for p in (
        Provider("openai", "OpenAI", "openai/", "OPENAI_API_KEY",
                 "https://api.openai.com/v1", "sk-..."),
        Provider("anthropic", "Claude (Anthropic)", "anthropic/", "ANTHROPIC_API_KEY",
                 "https://api.anthropic.com", "sk-ant-..."),
    )
}

# 적응형(adaptive) thinking 을 받는 모델. 이 목록 밖이면 thinking 파라미터를 아예 안 보낸다
# (구형 모델은 budget_tokens 방식이라 adaptive 를 보내면 400 이 난다).
_ADAPTIVE_THINKING = (
    "claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6",
)

# 채팅에 못 쓰는 모델을 이름으로 걸러낸다 (OpenAI 목록에 임베딩·음성·이미지가 섞여 온다)
_NOT_CHAT = ("embedding", "embed", "tts", "whisper", "dall-e", "moderation",
             "audio", "image", "transcribe", "realtime", "search", "computer-use",
             "codex", "babbage", "davinci")


# ─────────────────────────── 키 ───────────────────────────

_CACHE_TTL = 30.0
_cache: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def _secret_name(name: str) -> str:
    return f"{name}_api_key"


def _db_key(name: str) -> str:
    now = time.time()
    with _lock:
        hit = _cache.get(name)
        if hit and now - hit[1] < _CACHE_TTL:
            return hit[0]
    try:
        with db.cursor(commit=False, schema="public") as cur:
            cur.execute("SELECT value FROM global_secrets WHERE key = %s", (_secret_name(name),))
            row = cur.fetchone()
        value = (row["value"] if row else "").strip()
    except Exception as exc:  # noqa: BLE001 - 마이그레이션 전이면 테이블이 없을 수 있다
        log.debug("global_secrets 조회 실패: %s", exc)
        value = ""
    with _lock:
        _cache[name] = (value, now)
    return value


def api_key(name: str) -> str:
    p = PROVIDERS[name]
    return os.getenv(p.env, "").strip() or _db_key(name)


def key_source(name: str) -> str:
    """'env' | 'db' | '' — 어디서 온 키인지."""
    if os.getenv(PROVIDERS[name].env, "").strip():
        return "env"
    return "db" if _db_key(name) else ""


def masked_key(name: str) -> str:
    key = api_key(name)
    if not key:
        return ""
    return f"{key[:6]}…{key[-4:]}" if len(key) > 14 else "…" * 4


def configured(name: str) -> bool:
    return bool(api_key(name))


def set_key(name: str, value: str | None) -> None:
    if name not in PROVIDERS:
        raise RemoteError(f"알 수 없는 제공자: {name}")
    value = (value or "").strip()
    with db.cursor(schema="public") as cur:
        if value:
            cur.execute(
                """
                INSERT INTO global_secrets (key, value, updated_at) VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (_secret_name(name), value),
            )
        else:
            cur.execute("DELETE FROM global_secrets WHERE key = %s", (_secret_name(name),))
    with _lock:
        _cache.pop(name, None)


def base_url(name: str) -> str:
    env_url = os.getenv(f"{name.upper()}_BASE_URL", "").strip()
    return (env_url or PROVIDERS[name].base_url).rstrip("/")


# ─────────────────────────── 모델 이름 ───────────────────────────


def provider_for(model: str | None) -> Provider | None:
    if not model:
        return None
    for p in PROVIDERS.values():
        if str(model).startswith(p.prefix):
            return p
    return None


def strip_prefix(model: str) -> str:
    p = provider_for(model)
    return model[len(p.prefix):] if p else model


def _supports_thinking(model_id: str) -> bool:
    return model_id.startswith(_ADAPTIVE_THINKING)


# ─────────────────────────── 사용량 ───────────────────────────


def record_usage(provider: str, model: str, prompt: int, completion: int, cached: int = 0) -> None:
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO llm_usage
                       (provider, model, prompt_tokens, completion_tokens, cached_tokens)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (provider, model, int(prompt), int(completion), int(cached)),
            )
    except Exception as exc:  # noqa: BLE001 - 기록 실패로 채팅을 끊지 않는다
        log.debug("사용량 기록 실패: %s", exc)


def usage_summary(provider: str) -> dict[str, Any]:
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT count(*) AS calls,
                   COALESCE(sum(prompt_tokens), 0)     AS prompt_tokens,
                   COALESCE(sum(completion_tokens), 0) AS completion_tokens
              FROM llm_usage WHERE provider = %s
            """,
            (provider,),
        )
        row = cur.fetchone()
    return {**row, "total_tokens": int(row["prompt_tokens"]) + int(row["completion_tokens"])}


# ─────────────────────────── 모델 목록 ───────────────────────────

_MODELS_TTL = 300.0
_models_cache: dict[str, tuple[list[dict], float]] = {}


async def list_models(name: str) -> list[dict[str, Any]]:
    """제공자의 채팅 모델 목록. 이름을 상수로 박아두면 금방 낡으므로 API 로 조회한다."""
    now = time.time()
    hit = _models_cache.get(name)
    if hit and now - hit[1] < _MODELS_TTL:
        return hit[0]

    try:
        items = await (_anthropic_models() if name == "anthropic" else _openai_models())
    except Exception as exc:  # noqa: BLE001 - 목록 실패로 설정 화면을 깨뜨리지 않는다
        log.warning("%s 모델 목록 조회 실패: %s", name, exc)
        items = hit[0] if hit else []

    _models_cache[name] = (items, now)
    return items


async def _openai_models() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{base_url('openai')}/models",
            headers={"Authorization": f"Bearer {api_key('openai')}"},
        )
        r.raise_for_status()
        data = r.json().get("data") or []

    out = []
    for m in data:
        mid = str(m.get("id") or "")
        if not mid.startswith("gpt") and not mid.startswith("o"):
            continue
        if any(bad in mid for bad in _NOT_CHAT):
            continue
        out.append({"id": mid, "label": mid, "thinking": mid.startswith("o")})
    out.sort(key=lambda x: x["id"], reverse=True)
    return out


async def _anthropic_models() -> list[dict[str, Any]]:
    from anthropic import AsyncAnthropic  # 이미지 재빌드 전에도 앱은 뜨도록 지연 임포트

    client = AsyncAnthropic(api_key=api_key("anthropic"))
    page = await client.models.list(limit=50)
    out = []
    for m in page.data:
        out.append({
            "id": m.id,
            "label": getattr(m, "display_name", None) or m.id,
            "thinking": _supports_thinking(m.id),
        })
    return out


# ─────────────────────────── 스트리밍 ───────────────────────────


def _media_type(b64: str) -> str:
    """base64 앞머리로 이미지 종류를 추정한다 (첨부 단계에서 확장자를 잃었다)."""
    if b64.startswith("/9j/"):
        return "image/jpeg"
    if b64.startswith("R0lGOD"):
        return "image/gif"
    if b64.startswith("UklGR"):
        return "image/webp"
    return "image/png"


async def chat_stream(
    model: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.7,
    max_tokens: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """ollama.chat_stream 과 같은 모양({content, thinking, done})으로 흘린다."""
    p = provider_for(model)
    if p is None:
        raise RemoteError(f"외부 모델이 아닙니다: {model}")
    if not configured(p.name):
        raise RemoteError(f"{p.label} API 키가 설정되지 않았습니다")

    model_id = strip_prefix(model)
    if p.name == "anthropic":
        source = _anthropic_stream(model_id, messages, max_tokens=max_tokens or MAX_TOKENS)
    else:
        source = _openai_stream(model_id, messages, temperature=temperature,
                                max_tokens=max_tokens)
    async for chunk in source:
        yield chunk


def _openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        images = m.get("images") or []
        text = m.get("content") or ""
        if not images:
            out.append({"role": m["role"], "content": text})
            continue
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        parts += [
            {"type": "image_url",
             "image_url": {"url": f"data:{_media_type(b)};base64,{b}"}}
            for b in images
        ]
        out.append({"role": m["role"], "content": parts})
    return out


async def _openai_stream(
    model_id: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float,
    max_tokens: int | None,
) -> AsyncIterator[dict[str, Any]]:
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": _openai_messages(messages),
        "stream": True,
        "temperature": temperature,
        "stream_options": {"include_usage": True},
    }
    if max_tokens:
        payload["max_completion_tokens"] = max_tokens

    usage: dict[str, Any] = {}
    timeout = httpx.Timeout(connect=15, read=None, write=30, pool=15)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in (1, 2):
            try:
                async for chunk in _openai_sse(client, payload, usage):
                    yield chunk
                break
            except RemoteError as exc:
                # 신형 모델은 기본값 외의 temperature 를 거부한다. 한 번만 빼고 재시도.
                if attempt == 1 and "temperature" in str(exc) and "temperature" in payload:
                    log.info("%s: temperature 미지원 — 빼고 재시도", model_id)
                    payload.pop("temperature")
                    continue
                raise

    if usage:
        await anyio.to_thread.run_sync(
            record_usage, "openai", model_id,
            int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0),
        )


async def _openai_sse(
    client: httpx.AsyncClient, payload: dict[str, Any], usage: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    async with client.stream(
        "POST",
        f"{base_url('openai')}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {api_key('openai')}"},
    ) as r:
        if r.status_code >= 400:
            body = (await r.aread()).decode(errors="replace")[:400]
            raise RemoteError(f"HTTP {r.status_code}: {body}")

        async for line in r.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue

            if obj.get("usage"):
                usage.update(obj["usage"])

            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or {}
                content = delta.get("content") or ""
                # reasoning 요약을 주는 모델이 있다 (OpenAI 호환 게이트웨이 포함)
                thinking = delta.get("reasoning_content") or ""
                done = bool(choice.get("finish_reason"))
                if content or thinking or done:
                    yield {"content": content, "thinking": thinking, "done": done}


def _anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """(system, messages). Anthropic 은 system 이 별도 파라미터고 첫 턴이 user 여야 한다."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [m for m in messages if m["role"] != "system"]
    while rest and rest[0]["role"] != "user":
        rest.pop(0)

    out = []
    for m in rest:
        images = m.get("images") or []
        text = m.get("content") or ""
        if not images:
            out.append({"role": m["role"], "content": text})
            continue
        blocks: list[dict[str, Any]] = [
            {"type": "image",
             "source": {"type": "base64", "media_type": _media_type(b), "data": b}}
            for b in images
        ]
        blocks.append({"type": "text", "text": text})
        out.append({"role": m["role"], "content": blocks})
    return system, out


async def _anthropic_stream(
    model_id: str, messages: list[dict[str, Any]], *, max_tokens: int
) -> AsyncIterator[dict[str, Any]]:
    from anthropic import AsyncAnthropic  # 지연 임포트 (list_models 와 같은 이유)

    system, msgs = _anthropic_messages(messages)
    if not msgs:
        raise RemoteError("보낼 메시지가 없습니다")

    kwargs: dict[str, Any] = {}
    if system:
        kwargs["system"] = system
    if _supports_thinking(model_id):
        # temperature 는 최신 모델에서 거부되므로 아예 보내지 않는다.
        kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}

    client = AsyncAnthropic(api_key=api_key("anthropic"))
    async with client.messages.stream(
        model=model_id, max_tokens=max_tokens, messages=msgs, **kwargs
    ) as stream:
        async for event in stream:
            if event.type != "content_block_delta":
                continue
            delta = event.delta
            if delta.type == "thinking_delta":
                yield {"content": "", "thinking": delta.thinking, "done": False}
            elif delta.type == "text_delta":
                yield {"content": delta.text, "thinking": "", "done": False}
        final = await stream.get_final_message()

    yield {"content": "", "thinking": "", "done": True}

    u = final.usage
    await anyio.to_thread.run_sync(
        record_usage, "anthropic", model_id,
        u.input_tokens or 0, u.output_tokens or 0, u.cache_read_input_tokens or 0,
    )


async def ping(name: str, model_id: str) -> dict[str, Any]:
    """키가 실제로 동작하는지 최소 토큰으로 한 번 확인한다."""
    if not configured(name):
        return {"ok": False, "error": f"{PROVIDERS[name].label} API 키 미설정"}
    try:
        got = []
        async for chunk in chat_stream(
            f"{PROVIDERS[name].prefix}{model_id}",
            [{"role": "user", "content": "ok 라고만 답해라."}],
            temperature=0.0,
            max_tokens=1024,
        ):
            got.append(chunk["content"])
        return {"ok": True, "model": model_id, "reply": "".join(got).strip()[:60]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
