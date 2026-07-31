"""호스트 Ollama 클라이언트 (async: API용, sync: worker용)."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Iterable

import httpx

from . import db
from .config import env


def base_url() -> str:
    return str(db.get_setting("ollama_base_url") or env.ollama_base_url).rstrip("/")


class OllamaError(RuntimeError):
    pass


# ─────────────────────────── async (FastAPI) ───────────────────────────


async def list_models() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{base_url()}/api/tags")
        r.raise_for_status()
        return r.json().get("models", [])


async def ping() -> dict[str, Any]:
    try:
        models = await list_models()
        return {"ok": True, "models": len(models),
                "names": [m["name"] for m in models]}
    except Exception as exc:  # noqa: BLE001 - 헬스체크는 원인 문자열만 필요
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def chat_stream(
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    num_ctx: int = 8192,
    think: bool | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Ollama NDJSON 스트림을 dict 로 흘려보낸다.

    각 청크: {"content": str, "thinking": str, "done": bool}
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if think is not None:
        payload["think"] = think

    timeout = httpx.Timeout(connect=10, read=None, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{base_url()}/api/chat", json=payload) as r:
            if r.status_code >= 400:
                body = (await r.aread()).decode(errors="replace")[:500]
                raise OllamaError(f"HTTP {r.status_code}: {body}")
            async for line in r.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("error"):
                    raise OllamaError(str(obj["error"]))
                msg = obj.get("message") or {}
                yield {
                    "content": msg.get("content") or "",
                    "thinking": msg.get("thinking") or "",
                    "done": bool(obj.get("done")),
                }
                if obj.get("done"):
                    break


async def embed(texts: Iterable[str], model: str | None = None) -> list[list[float]]:
    items = [t for t in texts]
    if not items:
        return []
    model = model or str(db.get_setting("embed_model"))
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{base_url()}/api/embed", json={"model": model, "input": items}
        )
        if r.status_code >= 400:
            raise OllamaError(f"embed HTTP {r.status_code}: {r.text[:300]}")
        return r.json()["embeddings"]


# ─────────────────────────── sync (worker) ───────────────────────────


def embed_sync(
    texts: list[str], model: str, *, client: httpx.Client | None = None
) -> list[list[float]]:
    if not texts:
        return []
    owned = client is None
    client = client or httpx.Client(timeout=180)
    try:
        r = client.post(f"{base_url()}/api/embed", json={"model": model, "input": texts})
        if r.status_code >= 400:
            raise OllamaError(f"embed HTTP {r.status_code}: {r.text[:300]}")
        return r.json()["embeddings"]
    finally:
        if owned:
            client.close()


def generate_sync(
    model: str,
    prompt: str,
    *,
    system: str | None = None,
    fmt: dict | str | None = None,
    temperature: float = 0.0,
    num_ctx: int = 8192,
    client: httpx.Client | None = None,
) -> str:
    """단발 생성. fmt 에 JSON Schema 를 주면 구조화 출력을 강제한다."""
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if system:
        payload["system"] = system
    if fmt is not None:
        payload["format"] = fmt

    owned = client is None
    client = client or httpx.Client(timeout=600)
    try:
        r = client.post(f"{base_url()}/api/generate", json=payload)
        if r.status_code >= 400:
            raise OllamaError(f"generate HTTP {r.status_code}: {r.text[:300]}")
        return r.json().get("response", "")
    finally:
        if owned:
            client.close()
