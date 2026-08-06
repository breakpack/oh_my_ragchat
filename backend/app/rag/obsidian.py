"""지식 그래프를 옵시디언 볼트(마크다운 + [[위키링크]])로 내보낸다.

옵시디언 그래프 뷰는 노트 사이의 `[[링크]]`를 간선으로 그린다. 그래서 엔티티 하나당
노트 하나를 만들고 관계를 링크로 적어 두면, 우리가 만든 그래프가 그대로 옵시디언에서
열린다 — 따로 플러그인이나 변환기가 필요 없다.

내보낸 폴더는 NAS 안에 있으므로, 옵시디언에서 그 폴더를 볼트로 열면 된다.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import ctx, db, paths
from ..config import env

log = logging.getLogger("chatchat.obsidian")

MARKER = ".chatchat-vault"  # 우리가 만든 볼트라는 표시. 남의 폴더를 덮어쓰지 않으려고 둔다
NO_DESC = "_설명 없음_"
ENTITY_DIR = "엔티티"
DOC_DIR = "문서"
INDEX = "지식그래프.md"

# 파일 이름에 못 쓰거나 위키링크를 깨뜨리는 글자들
_BAD = re.compile(r'[\\/:*?"<>|#^\[\]]+')


def location_label(external: bool) -> str:
    """화면에 보여줄 호스트 경로. 컨테이너 안에서는 알 수 없어 라벨을 env 로 받는다."""
    user = ctx.get()
    who = user.username if user else "_shared"
    if not external:
        return f"NAS/{who} 안"
    base = (env.obsidian_host_label or str(env.obsidian_root)).rstrip("/")
    # 관리자는 마운트 바로 아래에 쓴다 (paths.obsidian_root 와 같은 규칙)
    return base if (user and user.is_admin) else f"{base}/{who}"


def _safe(name: str, used: dict[str, str]) -> str:
    """파일 이름으로 쓸 수 있게 다듬는다. 다듬은 뒤 겹치면 번호를 붙인다."""
    s = _BAD.sub("-", name)
    s = re.sub(r"\s+", " ", s).strip(" .")[:80] or "이름없음"
    key = s.casefold()
    if used.get(key, name) != name:  # 다른 원본이 같은 파일 이름을 차지했다
        n = 2
        while used.get(f"{key}-{n}", name) != name:
            n += 1
        s, key = f"{s}-{n}", f"{key}-{n}"
    used[key] = name
    return s


def _link(folder: str, safe: str, label: str) -> str:
    """엔티티와 문서에 같은 이름이 있을 수 있으니 폴더까지 붙여 건다."""
    return f"[[{folder}/{safe}|{label}]]"


def _yaml(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _fetch() -> dict[str, Any]:
    with db.cursor(commit=False) as cur:
        cur.execute("SELECT id, name, type, description, degree FROM entities ORDER BY id")
        entities = cur.fetchall()
        cur.execute(
            "SELECT src_id, tgt_id, description, keywords, weight FROM relations ORDER BY id"
        )
        relations = cur.fetchall()
        cur.execute(
            "SELECT id, path, source, url, chunk_count FROM documents "
            " WHERE status = 'ready' ORDER BY id"
        )
        documents = cur.fetchall()
        cur.execute(
            """
            SELECT DISTINCT ce.entity_id, c.document_id
              FROM chunk_entities ce JOIN chunks c ON c.id = ce.chunk_id
            """
        )
        links = cur.fetchall()
        # 업로드한 파일 이름이 UUID 인 경우가 많다. 첫 청크 앞머리를 제목으로 쓴다.
        cur.execute(
            """
            SELECT DISTINCT ON (document_id) document_id, left(content, 300) AS head
              FROM chunks ORDER BY document_id, ord
            """
        )
        heads = {r["document_id"]: r["head"] for r in cur.fetchall()}
    return {"entities": entities, "relations": relations,
            "documents": documents, "links": links, "heads": heads}


def _title(doc: dict, heads: dict[int, str]) -> str:
    """문서 노트에 붙일 이름. 파일 이름이 UUID 면 본문 첫 줄이 훨씬 낫다."""
    fname = Path(doc["path"]).name or doc["path"]
    stem = Path(fname).stem
    looks_uuid = bool(re.fullmatch(r"[0-9a-fA-F-]{16,}", stem))
    if not looks_uuid:
        return fname

    for raw in (heads.get(doc["id"]) or "").splitlines()[:8]:
        line = re.sub(r"\s+", " ", raw.strip().lstrip("#").strip())
        if len(line) < 8:
            continue
        # 텍스트 레이어가 깨진 PDF 는 'gid00030-gid00035…' 같은 글자를 뱉는다.
        # 글자 비율이 낮은 줄은 제목이 아니라고 본다.
        if sum(ch.isalpha() for ch in line) / len(line) < 0.4:
            continue
        return line[:60]
    return fname


def export(dest: str, include_documents: bool, cfg: dict,
           external: bool = False) -> dict[str, Any]:
    """볼트를 만든다. 이미 있으면 우리가 만든 폴더만 갈아끼운다.

    external=True 면 NAS 가 아니라 별도 마운트(OBSIDIAN_HOST_PATH — iCloud 등) 아래에
    쓴다. 볼트를 아이클라우드에 두면 아이폰·아이패드 옵시디언에서도 같은 볼트가 열린다.
    """
    base = paths.obsidian_root() if external else paths.root()
    rel = paths.normalize(dest) or "지식그래프"
    target = paths.resolve_under(base, rel, must_exist=False)

    if target.exists() and not target.is_dir():
        raise ValueError(f"'{rel}' 은(는) 폴더가 아닙니다")
    if target.is_dir() and any(target.iterdir()) and not (target / MARKER).exists():
        raise ValueError(
            f"'{rel}' 에 다른 파일이 있습니다. 빈 폴더나 새 폴더를 지정하세요 "
            "(내보내기가 폴더 내용을 갈아끼웁니다)"
        )

    data = _fetch()
    by_id = {e["id"]: e for e in data["entities"]}

    # 파일 이름 정하기 — 링크가 파일 이름을 가리키므로 먼저 전부 확정해 둔다
    used: dict[str, str] = {}
    ent_name = {e["id"]: _safe(e["name"], used) for e in data["entities"]}
    doc_used: dict[str, str] = {}
    doc_label = {d["id"]: _title(d, data["heads"]) for d in data["documents"]}
    doc_name = {d["id"]: _safe(doc_label[d["id"]], doc_used) for d in data["documents"]}

    # 관계는 양쪽 노트에 다 적는다 (어느 쪽에서 열어도 이웃이 보이게)
    neighbors: dict[int, list[tuple[int, str, float]]] = {}
    for r in data["relations"]:
        if r["src_id"] not in by_id or r["tgt_id"] not in by_id:
            continue
        desc = r["description"] or r["keywords"] or ""
        neighbors.setdefault(r["src_id"], []).append((r["tgt_id"], desc, r["weight"]))
        neighbors.setdefault(r["tgt_id"], []).append((r["src_id"], desc, r["weight"]))

    ent_docs: dict[int, list[int]] = {}
    doc_ents: dict[int, list[int]] = {}
    for row in data["links"]:
        ent_docs.setdefault(row["entity_id"], []).append(row["document_id"])
        doc_ents.setdefault(row["document_id"], []).append(row["entity_id"])

    # 우리가 만든 폴더만 지운다 (같은 폴더에 사용자가 둔 노트는 건드리지 않는다)
    target.mkdir(parents=True, exist_ok=True)
    for sub in (ENTITY_DIR, DOC_DIR):
        shutil.rmtree(target / sub, ignore_errors=True)
    (target / ENTITY_DIR).mkdir(parents=True, exist_ok=True)
    if include_documents:
        (target / DOC_DIR).mkdir(parents=True, exist_ok=True)

    for e in data["entities"]:
        lines = [
            "---",
            f"type: {e['type'] or 'unknown'}",
            f"degree: {e['degree']}",
            f"aliases: [{_yaml(e['name'])}]",
            f"tags: [chatchat/엔티티, chatchat/type/{_BAD.sub('-', e['type'] or 'unknown')}]",
            "---",
            f"# {e['name']}",
            "",
            e["description"] or NO_DESC,
            "",
        ]

        rels = sorted(neighbors.get(e["id"], []), key=lambda x: -x[2])
        if rels:
            lines += ["## 관계", ""]
            seen: set[int] = set()
            for tgt, desc, weight in rels:
                if tgt in seen or tgt not in by_id:
                    continue
                seen.add(tgt)
                link = _link(ENTITY_DIR, ent_name[tgt], by_id[tgt]["name"])
                lines.append(f"- {link}" + (f" — {desc}" if desc else ""))
            lines.append("")

        docs = [d for d in ent_docs.get(e["id"], []) if d in doc_name]
        if include_documents and docs:
            lines += ["## 나온 문서", ""]
            lines += [f"- {_link(DOC_DIR, doc_name[d], doc_label[d])}" for d in sorted(set(docs))]
            lines.append("")

        (target / ENTITY_DIR / f"{ent_name[e['id']]}.md").write_text(
            "\n".join(lines), encoding="utf-8"
        )

    if include_documents:
        for d in data["documents"]:
            lines = [
                "---",
                f"source: {d['source']}",
                f"path: {_yaml(d['path'])}",
                f"chunks: {d['chunk_count']}",
                "tags: [chatchat/문서]",
                "---",
                f"# {doc_label[d['id']]}",
                "",
                f"`{d['path']}`",
                "",
            ]
            if d["url"]:
                lines += [f"[원본 열기]({d['url']})", ""]

            ents = [x for x in doc_ents.get(d["id"], []) if x in by_id]
            if ents:
                lines += ["## 등장 엔티티", ""]
                lines += [
                    f"- {_link(ENTITY_DIR, ent_name[x], by_id[x]['name'])}"
                    for x in sorted(set(ents), key=lambda i: -by_id[i]["degree"])
                ]
            (target / DOC_DIR / f"{doc_name[d['id']]}.md").write_text(
                "\n".join(lines), encoding="utf-8"
            )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    top = sorted(data["entities"], key=lambda e: -e["degree"])[:20]
    (target / INDEX).write_text(
        "\n".join([
            "# 지식그래프",
            "",
            f"chatchat 에서 {now} 에 내보냈습니다. 이 폴더를 옵시디언 볼트로 열면",
            "그래프 뷰에서 엔티티 사이의 관계를 그대로 볼 수 있습니다.",
            "",
            f"- 엔티티 {len(data['entities'])}개",
            f"- 관계 {len(data['relations'])}개",
            f"- 문서 {len(data['documents']) if include_documents else 0}개",
            "",
            "## 연결이 많은 엔티티",
            "",
            *[f"- {_link(ENTITY_DIR, ent_name[e['id']], e['name'])} · {e['degree']}"
              for e in top],
        ]),
        encoding="utf-8",
    )
    (target / MARKER).write_text(f"chatchat obsidian export {now}\n", encoding="utf-8")

    # 감시 폴더 안에 내보내면 방금 만든 노트를 다시 색인하게 된다 (NAS 안일 때만 해당)
    watched = [] if external else [
        w for w in (cfg.get("rag_watch_dirs") or []) if rel == w or rel.startswith(f"{w}/")
    ]
    warning = (
        f"'{rel}' 은 RAG 감시 폴더({', '.join(watched)}) 안입니다. "
        "내보낸 노트가 다시 색인되니 감시 폴더 밖으로 옮기는 걸 권합니다."
        if watched else ""
    )

    return {
        "path": rel,
        "external": external,
        "location": location_label(external),
        "entities": len(data["entities"]),
        "relations": len(data["relations"]),
        "documents": len(data["documents"]) if include_documents else 0,
        "warning": warning,
    }


# ─────────────────────────── 역가져오기 ───────────────────────────
# 옵시디언에서 고친 설명·관계를 DB 로 되돌린다. DB 가 원본이므로, 여기서 반영해 두면
# 다음 내보내기 때 그 내용이 그대로 다시 나간다(수정이 덮어써지지 않는다).
#
# 규칙: 추가와 수정만 한다. 볼트에서 지운 엔티티·관계는 DB 에서 지우지 않는다
# (한쪽 노트만 지워도 관계가 사라지는 사고를 막으려고 일부러 뺐다).

REL_SECTION = "관계"
DOC_SECTION = "나온 문서"

_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_LINK = re.compile(r"\[\[([^\]|#]+?)(?:\|([^\]]*))?\]\]")
_H1 = re.compile(r"^#\s+(.+)$", re.M)
_ALIAS = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _front(text: str) -> tuple[dict[str, str], str]:
    m = _FRONT.match(text)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, text[m.end():]


def _plain(text: str) -> str:
    """설명에 남은 [[링크]] 마크업을 사람이 읽는 글자로 되돌린다."""
    return _LINK.sub(lambda m: (m.group(2) or m.group(1).split("/")[-1]).strip(), text)


def _sections(body: str) -> dict[str, str]:
    """'## 제목' 기준으로 쪼갠다. 제목 앞부분은 '' 키에 담는다."""
    out: dict[str, str] = {}
    key = ""
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            out[key] = "\n".join(buf).strip()
            key, buf = line[3:].strip(), []
        else:
            buf.append(line)
    out[key] = "\n".join(buf).strip()
    return out


def _parse_note(text: str, fallback_name: str) -> dict[str, Any]:
    fm, body = _front(text)
    sections = _sections(body)

    head = sections.get("", "")
    title = _H1.search(head)
    alias = _ALIAS.search(fm.get("aliases", ""))
    # aliases 에 내보낼 때의 원래 이름을 넣어 뒀다. 파일 이름을 바꿔도 이걸로 찾는다.
    name = (alias.group(1) if alias else None) or (title.group(1).strip() if title else fallback_name)

    desc = _plain(_H1.sub("", head, count=1).strip())
    if desc == NO_DESC:
        desc = ""

    # 관계 섹션의 목록 줄에서만 설명을 읽는다. 본문에 흘려 쓴 [[링크]]도 관계로 잡되,
    # 그 뒤의 문장은 설명이 아니므로 빈 설명으로 둔다(기존 설명을 덮어쓰지 않게).
    rels: dict[str, str] = {}
    for sec, text_ in sections.items():
        if sec == DOC_SECTION:
            continue
        for line in text_.splitlines():
            stripped = line.lstrip()
            is_item = sec == REL_SECTION and stripped[:2] in ("- ", "* ")
            for m in _LINK.finditer(line):
                if m.group(1).startswith(f"{DOC_DIR}/"):
                    continue
                target = (m.group(2) or m.group(1).split("/")[-1]).strip()
                if not target:
                    continue
                rest = line[m.end():].strip().lstrip("—-–").strip() if is_item else ""
                if rest or target not in rels:
                    rels[target] = _plain(rest)

    return {"name": name, "type": (fm.get("type") or "unknown").strip(),
            "description": desc, "relations": list(rels.items())}


def import_vault(dest: str, cfg: dict, external: bool = False) -> dict[str, Any]:
    """볼트의 엔티티 노트를 읽어 설명·관계를 DB 에 반영한다."""
    from . import graph as graph_mod  # 순환 임포트 방지

    base = paths.obsidian_root() if external else paths.root()
    rel = paths.normalize(dest) or "지식그래프"
    target = paths.resolve_under(base, rel, must_exist=True)
    ent_dir = target / ENTITY_DIR
    if not ent_dir.is_dir():
        raise ValueError(f"'{rel}' 에 {ENTITY_DIR}/ 폴더가 없습니다. 먼저 내보내기를 하세요")

    notes = []
    for f in sorted(ent_dir.glob("*.md")):
        try:
            notes.append(_parse_note(f.read_text(encoding="utf-8"), f.stem))
        except OSError as exc:  # noqa: PERF203 - 파일 하나가 깨져도 나머지는 가져온다
            log.warning("노트를 읽지 못했습니다 %s: %s", f.name, exc)

    by_norm = {graph_mod.normalize_name(n["name"]): n for n in notes if n["name"].strip()}
    if not by_norm:
        return {"path": rel, "entities_created": 0, "entities_updated": 0,
                "relations_added": 0, "relations_updated": 0, "notes": 0, "warning": ""}

    with db.cursor(commit=False) as cur:
        cur.execute("SELECT id, name_norm, name, type, description FROM entities")
        current = {r["name_norm"]: r for r in cur.fetchall()}

    # 바뀐 것만 손댄다 (수천 개를 매번 다시 쓰지 않게)
    changed = []
    for norm, note in by_norm.items():
        old = current.get(norm)
        if old is None:
            changed.append((norm, note, True))
        elif note["description"] != old["description"] or (
            note["type"] != "unknown" and note["type"] != old["type"]
        ):
            changed.append((norm, note, False))

    # 설명이 바뀌면 임베딩도 낡는다. 벡터 검색에 그대로 쓰이므로 다시 만든다.
    vectors: dict[str, list[float] | None] = {n: None for n, _, _ in changed}
    warning = ""
    if changed:
        try:
            import httpx

            from .. import ollama
            texts = [f"{n['name']}: {n['description']}".strip(": ") for _, n, _ in changed]
            with httpx.Client(timeout=300) as client:
                got = ollama.embed_sync(texts, str(cfg["embed_model"]), client=client)
            vectors = {norm: v for (norm, _, _), v in zip(changed, got)}
        except Exception as exc:  # noqa: BLE001 - 임베딩이 없어도 그래프 자체는 반영한다
            log.warning("가져오기 임베딩 실패: %s", exc)
            warning = f"임베딩을 만들지 못했습니다({exc}). 벡터 검색에는 반영이 늦을 수 있습니다."

    created = updated = 0
    with db.cursor() as cur:
        for norm, note, is_new in changed:
            cur.execute(
                """
                INSERT INTO entities (name_norm, name, type, description, embedding, manual)
                VALUES (%s, %s, %s, %s, %s, true)
                ON CONFLICT (name_norm) DO UPDATE
                   SET name = EXCLUDED.name,
                       description = EXCLUDED.description,
                       type = CASE WHEN EXCLUDED.type = 'unknown' THEN entities.type
                                   ELSE EXCLUDED.type END,
                       embedding = COALESCE(EXCLUDED.embedding, entities.embedding),
                       -- 사람이 손댄 표시. 청크 연결이 없어도 색인이 지우지 않는다.
                       manual = true
                RETURNING id
                """,
                (norm, note["name"], note["type"], note["description"], vectors.get(norm)),
            )
            current[norm] = {"id": cur.fetchone()["id"], "name_norm": norm,
                             "name": note["name"], "type": note["type"],
                             "description": note["description"]}
            created += is_new
            updated += not is_new

    added = touched = 0
    with db.cursor() as cur:
        for norm, note in by_norm.items():
            src = current.get(norm)
            if not src:
                continue
            for target_name, desc in note["relations"]:
                tgt = current.get(graph_mod.normalize_name(target_name))
                if not tgt or tgt["id"] == src["id"]:
                    continue
                a, b = sorted((src["id"], tgt["id"]))  # (a,b)/(b,a) 중복 방지
                cur.execute(
                    """
                    INSERT INTO relations (src_id, tgt_id, description, keywords, weight)
                    VALUES (%s, %s, %s, '', 1.0)
                    ON CONFLICT (src_id, tgt_id) DO UPDATE
                       SET description = EXCLUDED.description
                     WHERE EXCLUDED.description <> ''
                       AND relations.description IS DISTINCT FROM EXCLUDED.description
                    RETURNING (xmax = 0) AS inserted
                    """,
                    (a, b, desc),
                )
                row = cur.fetchone()
                if row is None:      # 바뀐 게 없어 UPDATE 가 걸러졌다
                    continue
                added += bool(row["inserted"])
                touched += not row["inserted"]

    graph_mod.refresh_degrees()

    return {
        "path": rel,
        "location": location_label(external),
        "notes": len(notes),
        "entities_created": created,
        "entities_updated": updated,
        "relations_added": added,
        "relations_updated": touched,
        "warning": warning,
    }
