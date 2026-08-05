"""Notion 페이지를 읽어 텍스트로 만든다.

공개 페이지 스크래핑은 구조가 자주 바뀌어 못 쓴다. 공식 API 만 쓰고, 그러려면
내부 통합(integration) 토큰과 "페이지를 통합에 연결"이 필요하다.
토큰은 DeepSeek 키와 같은 이유로 secrets 테이블에 둔다 (설정 API 로 새지 않게).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import httpx

from . import db

log = logging.getLogger("chatchat.notion")

API = os.getenv("NOTION_API_BASE", "https://api.notion.com/v1").rstrip("/")
VERSION = "2022-06-28"
SECRET_NAME = "notion_token"
RATE_SLEEP = 0.34  # Notion 은 초당 3요청 정도로 제한한다

# 32자리 hex(하이픈 유무 무관)가 페이지 id. URL 끝이나 ?p= 파라미터에 온다.
_ID = re.compile(r"([0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


class NotionError(RuntimeError):
    pass


# ─────────────────────────── 토큰 ───────────────────────────


def token() -> str:
    env = os.getenv("NOTION_TOKEN", "").strip()
    if env:
        return env
    try:
        with db.cursor(commit=False) as cur:
            cur.execute("SELECT value FROM secrets WHERE key = %s", (SECRET_NAME,))
            row = cur.fetchone()
        return (row["value"] if row else "").strip()
    except Exception:  # noqa: BLE001 - 마이그레이션 전이면 테이블이 없을 수 있다
        return ""


def set_token(value: str | None) -> None:
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


def configured() -> bool:
    return bool(token())


def masked() -> str:
    t = token()
    return f"{t[:7]}…{t[-4:]}" if len(t) > 14 else ("설정됨" if t else "")


def source() -> str:
    return "env" if os.getenv("NOTION_TOKEN", "").strip() else ("db" if token() else "")


def parse_id(url_or_id: str) -> str:
    """Notion URL 또는 id 문자열에서 페이지 id 를 뽑아 하이픈 형식으로 돌려준다."""
    hits = _ID.findall(url_or_id or "")
    if not hits:
        raise NotionError("Notion 페이지 ID 를 찾지 못했습니다. 페이지 URL 을 그대로 붙여넣으세요")
    raw = hits[-1].replace("-", "").lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


# ─────────────────────────── API ───────────────────────────


NOT_SHARED = (
    "이 페이지를 통합(integration)에 연결하지 않았습니다.\n"
    "'인터넷에 게시(Publish to web)'는 API 접근 권한이 아닙니다 — 별개입니다.\n"
    "Notion 에서 해당 페이지 우측 상단 ··· → 연결(Connections) → 만든 통합을 고르세요."
)


class NotFound(NotionError):
    """404 — 없거나, 통합에 연결되지 않았거나, 타입이 다르다."""


def _req(method: str, path: str, client: httpx.Client, **kw) -> dict:
    t = token()
    if not t:
        raise NotionError("Notion 토큰이 설정되지 않았습니다")
    r = client.request(
        method,
        f"{API}{path}",
        headers={"Authorization": f"Bearer {t}", "Notion-Version": VERSION},
        timeout=30,
        **kw,
    )
    time.sleep(RATE_SLEEP)
    if r.status_code == 404:
        raise NotFound(NOT_SHARED)
    if r.status_code >= 400:
        raise NotionError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _get(path: str, client: httpx.Client, params: dict | None = None) -> dict:
    return _req("GET", path, client, params=params)


def _rich(items: list[dict] | None) -> str:
    return "".join(i.get("plain_text") or "" for i in (items or []))


def page_title(page: dict) -> str:
    """페이지/데이터베이스 응답에서 제목을 뽑는다."""
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            t = _rich(prop.get("title"))
            if t:
                return t
    t = _rich((page.get("title") or []))  # database 는 최상위 title
    return t or "제목 없음"


BULLET = {"bulleted_list_item": "- ", "numbered_list_item": "- ", "to_do": "- "}
HEADING = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### "}


def fetch(page_id: str, client: httpx.Client, max_blocks: int = 2000) -> dict[str, Any]:
    """한 페이지의 제목·본문 텍스트·하위 페이지 id 목록."""
    try:
        meta = _get(f"/pages/{page_id}", client)
        is_db = False
    except NotFound:
        # 링크가 데이터베이스를 가리키면 /pages 는 404 다. /databases 로 다시 시도한다.
        meta = _get(f"/databases/{page_id}", client)
        is_db = True

    title = page_title(meta)
    url = meta.get("url") or ""

    lines: list[str] = [f"# {title}"]
    children: list[str] = []
    seen = 0

    if is_db:
        # 데이터베이스는 행(row)이 곧 하위 페이지다
        cursor = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = _req("POST", f"/databases/{page_id}/query", client, json=body)
            for row in data.get("results") or []:
                children.append(row["id"])
                lines.append(f"- (하위 페이지) {page_title(row)}")
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return {"id": page_id, "title": title, "url": url,
                "text": "\n".join(lines), "children": children}

    def walk(block_id: str, depth: int) -> None:
        nonlocal seen
        cursor = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = _get(f"/blocks/{block_id}/children", client, params)
            for b in data.get("results") or []:
                if seen >= max_blocks:
                    return
                seen += 1
                kind = b.get("type") or ""
                body = b.get(kind) or {}

                if kind in ("child_page", "child_database"):
                    children.append(b["id"])
                    lines.append(f"- (하위 페이지) {body.get('title') or ''}")
                    continue

                text = _rich(body.get("rich_text"))
                if kind == "code":
                    lines.append(f"```\n{text}\n```")
                elif kind in HEADING:
                    lines.append(HEADING[kind] + text)
                elif kind in BULLET:
                    lines.append(BULLET[kind] + text)
                elif kind == "table_row":
                    lines.append(" | ".join(_rich(c) for c in body.get("cells") or []))
                elif text:
                    lines.append(text)

                # 토글·컬럼 등 안쪽에 내용이 들어있는 블록을 따라간다
                if b.get("has_children") and depth < 4:
                    walk(b["id"], depth + 1)

            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    walk(page_id, 0)
    return {"id": page_id, "title": title, "url": url,
            "text": "\n".join(l for l in lines if l.strip()), "children": children}


def ping() -> dict[str, Any]:
    if not configured():
        return {"ok": False, "error": "Notion 토큰 미설정"}
    try:
        with httpx.Client() as c:
            me = _get("/users/me", c)
        with httpx.Client() as c:
            found = _req("POST", "/search", c, json={"page_size": 10})
        pages = [{"id": r["id"], "title": page_title(r)} for r in (found.get("results") or [])]
        return {
            "ok": True,
            "bot": (me.get("bot") or {}).get("workspace_name") or me.get("name"),
            "accessible": pages,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
