"""외부 검색 — 논문(OpenAlex · arXiv) + 일반 웹(DuckDuckGo).

RAG 는 내 문서만 본다. 논문이나 출처를 찾으려면 바깥을 봐야 하는데, 모델 제공자가
내장한 web search 툴에 기대면 모델마다 되고 안 되고가 갈린다. 그래서 검색은 백엔드에서
직접 하고 결과를 `[W1]` 형태로 프롬프트에 넣는다 — 로컬 Ollama 모델에서도 똑같이 돈다.

API 키가 필요한 검색 엔진은 쓰지 않는다. OpenAlex·arXiv 는 공개 API 고, 일반 웹은
DuckDuckGo HTML 을 긁는다(최선 노력 — 막히면 논문 결과만 나온다).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import anyio
import httpx

log = logging.getLogger("chatchat.websearch")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SNIPPET_LIMIT = 1200   # 검색 결과 하나가 프롬프트에서 차지할 수 있는 최대 길이
PAGE_LIMIT = 4000      # 본문까지 받아온 경우의 상한
TIMEOUT = httpx.Timeout(connect=8, read=15, write=8, pool=8)


@dataclass
class WebContext:
    prompt_block: str = ""
    citations: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    empty: bool = True


# ─────────────────────────── HTML 유틸 ───────────────────────────

_TAG = re.compile(r"<[^>]+>")


def _text(raw: str) -> str:
    return html.unescape(_TAG.sub("", raw)).strip()


class _Extract(HTMLParser):
    """본문만 성기게 뽑는다. 정확한 본문 추출기(trafilatura 등)를 붙일 만큼은 아니다."""

    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _page_text(raw: str) -> str:
    p = _Extract()
    try:
        p.feed(raw)
    except Exception:  # noqa: BLE001 - 깨진 HTML 은 여기까지 모은 것만 쓴다
        pass
    return re.sub(r"\s+", " ", " ".join(p.parts)).strip()


# ─────────────────────────── 검색 소스 ───────────────────────────


async def _openalex(client: httpx.AsyncClient, query: str, k: int, cfg: dict) -> list[dict]:
    # mailto 를 붙이면 OpenAlex 의 polite pool 로 들어가 429 를 훨씬 덜 맞는다
    params: dict[str, Any] = {"search": query, "per_page": k,
                              "sort": "relevance_score:desc"}
    mail = str(cfg.get("web_contact_email") or "").strip()
    if mail:
        params["mailto"] = mail
    r = await client.get("https://api.openalex.org/works", params=params)
    r.raise_for_status()
    out = []
    for w in (r.json().get("results") or [])[:k]:
        authors = [
            (a.get("author") or {}).get("display_name")
            for a in (w.get("authorships") or [])[:4]
        ]
        oa = w.get("open_access") or {}
        loc = w.get("primary_location") or {}
        url = oa.get("oa_url") or loc.get("landing_page_url") or w.get("doi") or ""
        venue = ((loc.get("source") or {}) or {}).get("display_name") or ""
        out.append({
            "kind": "paper",
            "source": "OpenAlex",
            "title": w.get("display_name") or "(제목 없음)",
            "url": url,
            "meta": " · ".join(x for x in [
                venue, str(w.get("publication_year") or ""),
                ", ".join(a for a in authors if a),
                f"피인용 {w.get('cited_by_count', 0)}회",
                (w.get("doi") or "").replace("https://doi.org/", "doi:"),
            ] if x),
            "text": _invert(w.get("abstract_inverted_index")),
        })
    return out


async def _crossref(client: httpx.AsyncClient, query: str, k: int, cfg: dict) -> list[dict]:
    """DOI 원본 등록처. OpenAlex 가 속도 제한에 걸려도 여기는 대개 살아 있다."""
    mail = str(cfg.get("web_contact_email") or "").strip()
    r = await client.get(
        "https://api.crossref.org/works",
        params={"query.bibliographic": query, "rows": k,
                "select": "title,author,DOI,issued,container-title,abstract,URL"},
        headers={"User-Agent": f"chatchat/1.0 (mailto:{mail})" if mail else "chatchat/1.0"},
    )
    r.raise_for_status()
    out = []
    for w in ((r.json().get("message") or {}).get("items") or [])[:k]:
        authors = [
            " ".join(x for x in [a.get("given"), a.get("family")] if x)
            for a in (w.get("author") or [])[:4]
        ]
        year = ((w.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        out.append({
            "kind": "paper",
            "source": "Crossref",
            "title": (w.get("title") or ["(제목 없음)"])[0],
            "url": w.get("URL") or (f"https://doi.org/{w['DOI']}" if w.get("DOI") else ""),
            "meta": " · ".join(x for x in [
                (w.get("container-title") or [""])[0], str(year or ""),
                ", ".join(a for a in authors if a),
                f"doi:{w['DOI']}" if w.get("DOI") else "",
            ] if x),
            "text": _text(w.get("abstract") or ""),
        })
    return out


def _invert(inv: dict[str, list[int]] | None) -> str:
    """OpenAlex 는 초록을 역색인으로 준다. 위치대로 다시 이어 붙인다."""
    if not inv:
        return ""
    words = sorted((pos, word) for word, positions in inv.items() for pos in positions)
    return " ".join(w for _, w in words)


async def _arxiv(client: httpx.AsyncClient, query: str, k: int) -> list[dict]:
    # 단어를 흩뿌리면(all:a b c) arXiv 는 엉뚱한 논문을 준다. 구문으로 먼저 찾고,
    # 0건이면 그때 단어 검색으로 물러선다.
    out = await _arxiv_query(client, f'all:"{query}"', k)
    return out or await _arxiv_query(client, f"all:{query}", k)


async def _arxiv_query(client: httpx.AsyncClient, search: str, k: int) -> list[dict]:
    r = await client.get(
        "http://export.arxiv.org/api/query",
        params={"search_query": search, "max_results": k,
                "sortBy": "relevance", "sortOrder": "descending"},
    )
    r.raise_for_status()

    from xml.etree import ElementTree as ET  # 여기서만 쓴다

    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(r.text).findall("a:entry", ns)[:k]:
        def txt(tag: str) -> str:
            node = e.find(f"a:{tag}", ns)
            return (node.text or "").strip() if node is not None else ""

        authors = [
            (a.find("a:name", ns).text or "")
            for a in e.findall("a:author", ns)[:4]
            if a.find("a:name", ns) is not None
        ]
        out.append({
            "kind": "paper",
            "source": "arXiv",
            "title": re.sub(r"\s+", " ", txt("title")),
            "url": txt("id"),
            "meta": " · ".join(x for x in ["arXiv", txt("published")[:10],
                                           ", ".join(authors)] if x),
            "text": re.sub(r"\s+", " ", txt("summary")),
        })
    return out


_A_TAG = re.compile(r"<a\s([^>]*class=\"[^\"]*result__a[^\"]*\"[^>]*)>(.*?)</a>", re.S)
_SNIPPET = re.compile(r"class=\"[^\"]*result__snippet[^\"]*\"[^>]*>(.*?)</a>", re.S)
_HREF = re.compile(r"href=\"([^\"]+)\"")


async def _duckduckgo(client: httpx.AsyncClient, query: str, k: int) -> list[dict]:
    r = await client.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": UA, "Referer": "https://duckduckgo.com/"},
    )
    r.raise_for_status()

    snippets = [_text(s) for s in _SNIPPET.findall(r.text)]
    out = []
    for i, (attrs, title) in enumerate(_A_TAG.findall(r.text)[:k]):
        href = _HREF.search(attrs)
        if not href:
            continue
        url = html.unescape(href.group(1))
        if url.startswith("//"):
            url = "https:" + url
        # DDG 는 리다이렉트 URL(/l/?uddg=...)로 감싸서 준다
        if "duckduckgo.com/l/" in url:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("uddg")
            if q:
                url = q[0]
        out.append({
            "kind": "web",
            "source": urllib.parse.urlparse(url).netloc or "web",
            "title": _text(title) or url,
            "url": url,
            "meta": "",
            "text": snippets[i] if i < len(snippets) else "",
        })
    return out


async def _fetch_page(client: httpx.AsyncClient, url: str) -> str:
    """스니펫만으로는 근거가 얇아서 상위 몇 건은 본문을 받아 온다. HTML 만."""
    r = await client.get(url, headers={"User-Agent": UA}, follow_redirects=True)
    r.raise_for_status()
    if "html" not in r.headers.get("content-type", ""):
        return ""
    return _page_text(r.text)[:PAGE_LIMIT]


# ─────────────────────────── 조합 ───────────────────────────


def _merge(groups: list[list[dict]], k: int) -> list[dict]:
    """소스별 결과를 번갈아 뽑아 합친다 (한 소스가 목록을 독차지하지 않게)."""
    seen: set[str] = set()
    out: list[dict] = []
    for i in range(max((len(g) for g in groups), default=0)):
        for g in groups:
            if i >= len(g):
                continue
            r = g[i]
            # 같은 논문이 OpenAlex·arXiv·웹에서 각각 잡힌다. URL 과 제목 둘 다로 거른다.
            url_key = (r.get("url") or "").lower().rstrip("/")
            title_key = re.sub(r"[^a-z0-9가-힣]+", "", r["title"].lower())[:60]
            if (url_key and url_key in seen) or (title_key and title_key in seen):
                continue
            seen.update({url_key, title_key} - {""})
            out.append(r)
            if len(out) >= k:
                return out
    return out


async def _english_query(query: str, cfg: dict) -> str:
    """한글 질문은 arXiv·OpenAlex 에서 거의 안 잡힌다. 로컬 모델로 영어 키워드를 뽑는다.

    실패하면 원문 그대로 검색한다 — 검색어 변환 때문에 채팅이 멈추면 안 된다.
    """
    if query.isascii() or not cfg.get("web_query_rewrite", True):
        return query

    from . import ollama  # 순환 임포트 방지

    def run() -> str:
        with httpx.Client(timeout=30) as c:
            return ollama.generate_sync(
                str(cfg.get("extract_model") or "gemma4:e2b"),
                f"질문: {query}",
                # '최신 논문 찾아줘' 같은 말이 그대로 넘어가면 검색이 엉뚱한 데로 샌다
                system="질문에서 찾으려는 '주제'만 영어 명사구로 뽑아라. "
                       "'최신·논문·연구·찾아줘·알려줘·recent·paper·latest' 같은 "
                       "검색 지시어는 빼고 주제어만 남긴다. "
                       "설명 없이 3~6단어 한 줄로 출력.",
                num_ctx=2048,
                client=c,
            )

    try:
        raw = await anyio.to_thread.run_sync(run)
        first = next((ln.strip(" \"'.") for ln in raw.splitlines() if ln.strip()), "")
        return first[:120] or query
    except Exception as exc:  # noqa: BLE001
        log.warning("검색어 영어 변환 실패 (원문으로 검색): %s", exc)
        return query


async def search(query: str, cfg: dict) -> WebContext:
    """사용자 질문을 그대로 검색어로 쓴다.

    모델로 검색어를 다듬으면 품질은 오르지만 호출이 한 번 더 붙는다. 우선 그대로 쓰고,
    필요하면 사용자가 질문을 검색어처럼 쓰면 된다.
    """
    q = " ".join(query.split())[:250]
    if not q:
        return WebContext(stats={"error": "검색어가 비었습니다"})

    mode = str(cfg.get("web_search_mode") or "auto")
    k = int(cfg.get("web_top_k") or 5)
    fetch_n = int(cfg.get("web_fetch_pages") or 0)

    # 논문 DB 는 영어만 제대로 찾는다. 일반 웹은 한국어 질문이 오히려 낫다.
    scholar_q = await _english_query(q, cfg) if mode in ("auto", "scholar") else q

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        jobs: list[Any] = []
        names: list[str] = []
        if mode in ("auto", "scholar"):
            jobs += [_openalex(client, scholar_q, k, cfg),
                     _crossref(client, scholar_q, k, cfg),
                     _arxiv(client, scholar_q, k)]
            names += ["openalex", "crossref", "arxiv"]
        if mode in ("auto", "web"):
            jobs.append(_duckduckgo(client, q, k))
            names.append("duckduckgo")

        raw = await asyncio.gather(*jobs, return_exceptions=True)

        groups: list[list[dict]] = []
        errors: dict[str, str] = {}
        for name, res in zip(names, raw):
            if isinstance(res, Exception):
                log.warning("%s 검색 실패: %s", name, res)
                errors[name] = f"{type(res).__name__}: {res}"
            else:
                groups.append(res)

        results = _merge(groups, k)

        # 본문 보강은 웹 결과에만. 논문은 초록이 이미 들어 있다.
        targets = [r for r in results if r["kind"] == "web" and r.get("url")][:fetch_n]
        if targets:
            pages = await asyncio.gather(
                *(_fetch_page(client, r["url"]) for r in targets), return_exceptions=True
            )
            for r, page in zip(targets, pages):
                if isinstance(page, str) and len(page) > len(r["text"]):
                    r["text"] = page

    if not results:
        return WebContext(stats={"mode": mode, "query": q, "results": 0, "errors": errors})

    lines = ["[웹 검색 결과]", "-----Sources-----"]
    citations = []
    for i, r in enumerate(results, 1):
        tag = f"W{i}"
        limit = PAGE_LIMIT if len(r["text"]) > SNIPPET_LIMIT and r["kind"] == "web" else SNIPPET_LIMIT
        body = r["text"][:limit]
        lines.append(
            f"[{tag}] {r['title']}\n"
            f"{r['url']}\n"
            + (f"{r['meta']}\n" if r["meta"] else "")
            + body
        )
        citations.append({
            "tag": tag,
            "path": r["source"],
            "title": r["title"],
            "url": r["url"],
            "meta": r["meta"],
            "kind": r["kind"],
            "excerpt": body[:800],
            "document_id": 0,
            "chunk_id": 0,
            "score": 0,
        })

    return WebContext(
        prompt_block="\n\n".join(lines),
        citations=citations,
        stats={"mode": mode, "query": q, "scholar_query": scholar_q,
               "results": len(results), "fetched": len(targets), "errors": errors},
        empty=False,
    )
