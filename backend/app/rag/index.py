"""문서 인덱싱 파이프라인 (워커에서 sync 로 실행).

status 흐름: pending → extracting → embedding → graphing → ready | error | skipped
각 단계 전환과 진행률은 events.publish() 로 흘려보내 UI 가 실시간으로 받는다.
"""

from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from .. import db, deepseek, events, flags, jobs, ollama, paths, security
from ..config import IMAGE_EXTS, INDEXABLE_EXTS, env
from . import chunk as chunker
from . import extract as extractor
from . import graph

log = logging.getLogger("chatchat.rag.index")

EMBED_BATCH = 16


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        while block := fp.read(1 << 20):
            h.update(block)
    return h.hexdigest()


# ─────────────────────────── 상태 / 진행률 ───────────────────────────


def _set_status(doc_id: int, path: str, status: str, **extra) -> None:
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE documents
               SET status = %s,
                   error = %s,
                   chunk_count = COALESCE(%s, chunk_count),
                   phase = %s,
                   progress_done = 0,
                   progress_total = COALESCE(%s, 0),
                   indexed_at = CASE WHEN %s = 'ready' THEN now() ELSE indexed_at END
             WHERE id = %s
            """,
            (status, extra.get("error"), extra.get("chunk_count"), status,
             extra.get("total"), status, doc_id),
        )
    events.publish("document", {
        "document_id": doc_id, "path": path, "status": status,
        "done": 0, "total": extra.get("total") or 0,
        "error": extra.get("error"), "chunk_count": extra.get("chunk_count"),
    })


def _progress(doc_id: int, path: str, status: str, done: int, total: int, phase: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE documents SET progress_done = %s, progress_total = %s, phase = %s "
            "WHERE id = %s",
            (done, total, phase, doc_id),
        )
    events.publish("progress", {
        "document_id": doc_id, "path": path, "status": status,
        "done": done, "total": total, "phase": phase,
    })


def _upsert_document(rel: str, st: os.stat_result, digest: str) -> dict:
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (path, mtime, size, sha256, status)
            VALUES (%s, %s, %s, %s, 'pending')
            ON CONFLICT (path) DO UPDATE
               SET mtime = EXCLUDED.mtime, size = EXCLUDED.size, sha256 = EXCLUDED.sha256
            RETURNING id, status, sha256
            """,
            (rel, st.st_mtime, st.st_size, digest),
        )
        return cur.fetchone()


def excluded(rel: str, cfg: dict) -> str | None:
    """인덱싱에서 빼야 할 이유. 없으면 None."""
    ext = Path(rel).suffix.lower()
    if ext not in INDEXABLE_EXTS:
        return "지원하지 않는 확장자"
    if ext in IMAGE_EXTS and not cfg.get("rag_ocr_enabled"):
        return "이미지 OCR 이 꺼져 있음"
    if flags.is_hidden_inherited(rel):
        return "숨김 처리된 경로"
    if not cfg["rag_index_locked_files"] and security.is_locked(rel):
        return "잠긴 파일"
    if not any(paths.is_under(rel, w) for w in (cfg["rag_watch_dirs"] or [])):
        return "감시 폴더 밖"
    return None


def index_document(rel: str, cfg: dict, *, force: bool = False,
                   client: httpx.Client | None = None) -> str:
    """단일 문서 인덱싱. 반환값은 최종 status."""
    rel = paths.normalize(rel)
    abs_path = env.nas_root / rel

    if not abs_path.is_file():
        delete_document(rel)
        return "deleted"

    reason = excluded(rel, cfg)
    if reason:
        delete_document(rel)
        log.info("건너뜀 %s (%s)", rel, reason)
        return "skipped"

    st = abs_path.stat()
    size_mb = st.st_size / (1024 * 1024)
    if size_mb > float(cfg["rag_max_file_mb"]):
        _mark_skipped(rel, st, f"파일이 너무 큽니다 ({size_mb:.1f}MB)")
        return "skipped"

    digest = _sha256(abs_path)
    doc = _upsert_document(rel, st, digest)
    doc_id = doc["id"]

    if not force and doc["status"] == "ready" and doc["sha256"] == digest:
        return "ready"  # 내용이 그대로면 다시 만들 이유가 없다

    owned = client is None
    client = client or httpx.Client(timeout=600)
    try:
        _set_status(doc_id, rel, "extracting")
        try:
            result = extractor.extract(
                abs_path, cfg,
                on_progress=lambda d, t, ph: _progress(doc_id, rel, "extracting", d, t, ph),
            )
        except extractor.Unsupported as exc:
            _set_status(doc_id, rel, "skipped", error=str(exc))
            return "skipped"
        except extractor.OcrUnavailable as exc:
            _set_status(doc_id, rel, "error", error=str(exc))
            return "error"

        with db.cursor() as cur:
            cur.execute("UPDATE documents SET ocr = %s WHERE id = %s", (result.ocr, doc_id))

        pieces = chunker.split(
            result.text, int(cfg["rag_chunk_size"]), int(cfg["rag_chunk_overlap"])
        )
        if not pieces:
            msg = "OCR 로도 글자를 찾지 못했습니다" if result.ocr else "추출된 텍스트가 없습니다"
            _set_status(doc_id, rel, "skipped", error=msg, chunk_count=0)
            return "skipped"

        _set_status(doc_id, rel, "embedding", chunk_count=len(pieces), total=len(pieces))
        _replace_chunks(doc_id, rel, pieces, cfg, client)

        if cfg["rag_extract_graph"]:
            _set_status(doc_id, rel, "graphing", total=len(pieces))
            _build_graph(doc_id, rel, cfg, client)

        _set_status(doc_id, rel, "ready", chunk_count=len(pieces))
        log.info("인덱싱 완료 %s (청크 %d%s)", rel, len(pieces), ", OCR" if result.ocr else "")
        return "ready"
    except Exception as exc:  # noqa: BLE001 - 실패는 문서 행에 기록하고 큐는 계속 돈다
        log.exception("인덱싱 실패 %s", rel)
        _set_status(doc_id, rel, "error", error=f"{type(exc).__name__}: {exc}")
        return "error"
    finally:
        if owned:
            client.close()


def _mark_skipped(rel: str, st: os.stat_result, reason: str) -> None:
    # mtime/size 를 같이 남겨야 다음 스캔이 "변경됨" 으로 오인해 다시 큐잉하지 않는다
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (path, mtime, size, status, error)
            VALUES (%s, %s, %s, 'skipped', %s)
            ON CONFLICT (path) DO UPDATE
               SET mtime = EXCLUDED.mtime, size = EXCLUDED.size,
                   status = 'skipped', error = EXCLUDED.error
            """,
            (rel, st.st_mtime, st.st_size, reason),
        )
    events.publish("document", {"path": rel, "status": "skipped", "error": reason})


def _replace_chunks(doc_id: int, rel: str, pieces: list[str], cfg: dict,
                    client: httpx.Client) -> None:
    embed_model = str(cfg["embed_model"])
    with db.cursor() as cur:
        cur.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))

    for start in range(0, len(pieces), EMBED_BATCH):
        batch = pieces[start:start + EMBED_BATCH]
        vectors = ollama.embed_sync(batch, embed_model, client=client)
        if len(vectors) != len(batch):
            raise ollama.OllamaError(
                f"임베딩 개수 불일치: {len(vectors)} != {len(batch)}"
            )
        with db.cursor() as cur:
            for i, (content, vec) in enumerate(zip(batch, vectors)):
                cur.execute(
                    """
                    INSERT INTO chunks (document_id, ord, content, token_est, embedding)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (document_id, ord) DO UPDATE
                       SET content = EXCLUDED.content, embedding = EXCLUDED.embedding
                    """,
                    (doc_id, start + i, content, chunker.token_est(content), vec),
                )
        _progress(doc_id, rel, "embedding",
                  min(start + EMBED_BATCH, len(pieces)), len(pieces), "embedding")


def pick_provider(cfg: dict) -> str:
    """그래프 추출 제공자 결정. deepseek 을 쓸 수 없으면 조용히 로컬로 되돌린다."""
    provider = str(cfg.get("extract_provider") or "local")
    if provider != "deepseek":
        return "local"
    if not deepseek.configured():
        log.warning("extract_provider=deepseek 이지만 DEEPSEEK_API_KEY 가 없어 로컬로 대체합니다")
        return "local"
    left = deepseek.budget_left(cfg)
    if left is not None and left <= 0:
        log.warning("DeepSeek 토큰 예산을 모두 썼습니다 → 로컬로 대체")
        return "local"
    return "deepseek"


def _merge_one(row: dict, entities: list[dict], relations: list[dict],
               embed_model: str, client: httpx.Client) -> None:
    """DB 병합은 항상 메인 스레드에서 순차로 한다 (동시 upsert 교착을 피한다)."""
    if not entities:
        return
    texts = [f"{e['name']}: {e['description']}".strip(": ") for e in entities]
    vectors = ollama.embed_sync(texts, embed_model, client=client)
    embeddings = {e["name_norm"]: v for e, v in zip(entities, vectors)}

    ids = graph.merge_entities(entities, embeddings)
    graph.merge_relations(relations, ids)
    graph.link_chunk(row["id"], list(ids.values()))


def _build_graph(doc_id: int, rel: str, cfg: dict, client: httpx.Client) -> None:
    embed_model = str(cfg["embed_model"])
    provider = pick_provider(cfg)

    with db.cursor(commit=False) as cur:
        cur.execute(
            "SELECT id, content FROM chunks WHERE document_id = %s ORDER BY ord", (doc_id,)
        )
        rows = cur.fetchall()

    log.info("그래프 추출 시작 %s (청크 %d, provider=%s)", rel, len(rows), provider)

    if provider == "deepseek":
        _build_graph_parallel(doc_id, rel, rows, cfg, embed_model, client)
    else:
        for n, row in enumerate(rows, 1):
            try:
                entities, relations = graph.extract(
                    row["content"], cfg, provider="local", ollama_client=client
                )
                _merge_one(row, entities, relations, embed_model, client)
            except ollama.OllamaError as exc:
                log.warning("엔티티 추출 실패 (청크 %s): %s", row["id"], exc)
            _progress(doc_id, rel, "graphing", n, len(rows), "graphing")

    graph.refresh_degrees()


def _build_graph_parallel(doc_id: int, rel: str, rows: list[dict], cfg: dict,
                          embed_model: str, client: httpx.Client) -> None:
    """외부 API 는 동시에 여러 청크를 보낼 수 있다. 추출만 병렬, 병합은 순차."""
    workers = max(1, min(int(cfg["deepseek_concurrency"]), 16))
    ds_client = httpx.Client(timeout=120, limits=httpx.Limits(max_connections=workers * 2))
    fell_back = False
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    graph.extract, row["content"], cfg,
                    provider="deepseek", ds_client=ds_client,
                ): row
                for row in rows
            }
            for n, fut in enumerate(as_completed(futures), 1):
                row = futures[fut]
                try:
                    entities, relations = fut.result()
                except deepseek.DeepSeekError as exc:
                    # 키 만료·한도 초과 등은 남은 청크를 로컬로 처리한다
                    log.warning("DeepSeek 추출 실패 (청크 %s): %s", row["id"], exc)
                    fell_back = True
                    try:
                        entities, relations = graph.extract(
                            row["content"], cfg, provider="local", ollama_client=client
                        )
                    except Exception:  # noqa: BLE001
                        entities, relations = [], []
                except Exception as exc:  # noqa: BLE001
                    log.warning("추출 실패 (청크 %s): %s", row["id"], exc)
                    entities, relations = [], []

                _merge_one(row, entities, relations, embed_model, client)
                _progress(doc_id, rel, "graphing", n, len(rows), "graphing")
    finally:
        ds_client.close()
    if fell_back:
        log.info("일부 청크는 로컬 모델로 대체 처리했습니다: %s", rel)


def delete_document(rel: str) -> int:
    """문서 또는 폴더 하위 문서 전체를 인덱스에서 제거."""
    rel = paths.normalize(rel)
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM documents WHERE path = %s OR path LIKE %s RETURNING id",
            (rel, rel + "/%"),
        )
        n = len(cur.fetchall())
    if n:
        graph.prune_orphans()
        graph.refresh_degrees()
        events.publish("document", {"path": rel, "status": "deleted"})
    return n


# ─────────────────────────── 스캔 ───────────────────────────
# macOS 호스트의 bind mount 에서는 inotify 가 컨테이너로 전달되지 않는다.
# 그래서 파일 감시는 주기적인 DB 대조 스캔으로 처리한다.


def scan(cfg: dict, *, force: bool = False) -> dict:
    """감시 폴더를 훑어 새 파일/변경/삭제를 잡으로 큐잉한다."""
    watch = [paths.normalize(w) for w in (cfg["rag_watch_dirs"] or [])]
    hidden = flags.hidden_paths()
    locked = set(flags.locked_paths()) if not cfg["rag_index_locked_files"] else set()
    exts = INDEXABLE_EXTS if cfg.get("rag_ocr_enabled") else INDEXABLE_EXTS - IMAGE_EXTS

    on_disk: dict[str, tuple[float, int]] = {}
    for wdir in watch:
        base = env.nas_root / wdir if wdir else env.nas_root
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = paths.to_rel(Path(dirpath))
            if rel_dir == ".":  # NAS 루트 자신
                rel_dir = ""
            dirnames[:] = [
                d for d in dirnames
                if not any(paths.is_under(f"{rel_dir}/{d}".lstrip("/"), h) for h in hidden)
            ]
            for name in filenames:
                if Path(name).suffix.lower() not in exts:
                    continue
                rel = f"{rel_dir}/{name}".lstrip("/")
                if rel in locked or any(paths.is_under(rel, h) for h in hidden):
                    continue
                try:
                    st = (Path(dirpath) / name).stat()
                except OSError:
                    continue
                on_disk[rel] = (st.st_mtime, st.st_size)

    with db.cursor(commit=False) as cur:
        cur.execute("SELECT path, mtime, size, status FROM documents")
        known = {r["path"]: r for r in cur.fetchall()}

    queued = removed = 0
    for rel, (mtime, size) in on_disk.items():
        row = known.get(rel)
        changed = (
            row is None
            or row["status"] in ("pending", "error")
            or abs((row["mtime"] or 0) - mtime) > 1e-6
            or (row["size"] or -1) != size
        )
        if force or changed:
            if jobs.enqueue_index(rel) is not None:
                queued += 1

    for rel in known.keys() - on_disk.keys():
        if jobs.enqueue_delete(rel) is not None:
            removed += 1

    if queued or removed:
        events.publish("scan", {"queued": queued, "removed": removed})
    return {"seen": len(on_disk), "queued": queued, "removed": removed}
