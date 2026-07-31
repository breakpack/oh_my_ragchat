"""NAS 파일 매니저.

경로는 전부 NAS 루트 기준 상대경로. 숨김(7번)은 목록에서 걸러내고,
잠금(8번)은 열람(content) 시점에 비밀번호를 요구한다.
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import db, deps, flags, jobs, paths, security
from ..config import INDEXABLE_EXTS, env

router = APIRouter(prefix="/api/files", tags=["files"], dependencies=[deps.Auth])


# ─────────────────────────── 공통 ───────────────────────────


def _entry(child: Path, rel: str, flag: dict[str, Any] | None) -> dict[str, Any]:
    try:
        st = child.stat()
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    is_dir = child.is_dir()
    return {
        "name": child.name,
        "path": rel,
        "is_dir": is_dir,
        "size": None if is_dir else size,
        "mtime": mtime,
        "ext": "" if is_dir else child.suffix.lower(),
        "hidden": bool(flag and flag.get("hidden")),
        "locked": bool(flag and flag.get("locked")),
        "note": (flag or {}).get("note"),
        "indexable": (not is_dir) and child.suffix.lower() in INDEXABLE_EXTS,
    }


def _watched(rel: str, cfg: dict) -> bool:
    """RAG 감시 폴더 하위인지."""
    return any(paths.is_under(rel, w) for w in (cfg.get("rag_watch_dirs") or []))


def _reindex_after_change(rel: str, cfg: dict, *, removed: bool) -> None:
    """감시 폴더 안에서 파일이 바뀌면 인덱싱 잡을 건다 (워커 스캔을 기다리지 않게)."""
    if not _watched(rel, cfg):
        return
    if removed:
        jobs.enqueue_delete(rel)
    elif Path(rel).suffix.lower() in INDEXABLE_EXTS:
        jobs.enqueue_index(rel)


# ─────────────────────────── 목록 ───────────────────────────


@router.get("")
def list_dir(
    cfg: deps.Settings,
    path: str = Query("", description="NAS 루트 기준 상대경로"),
    show_hidden: bool | None = Query(None),
    sort: str = Query("name", pattern="^(name|size|mtime)$"),
    desc: bool = Query(False),
) -> dict:
    rel = paths.normalize(path)
    target = paths.resolve(rel)
    if not target.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="폴더가 아닙니다")

    reveal = cfg["nas_show_hidden_default"] if show_hidden is None else show_hidden

    children = sorted(target.iterdir(), key=lambda p: p.name.lower())
    rels = [paths.join(rel, c.name) for c in children]
    flag_map = flags.flags_for(rels)

    items = [
        _entry(child, r, flag_map.get(r))
        for child, r in zip(children, rels)
        if reveal or not (flag_map.get(r) or {}).get("hidden")
    ]

    key = {"name": lambda i: i["name"].lower(),
           "size": lambda i: i["size"] or 0,
           "mtime": lambda i: i["mtime"]}[sort]
    items.sort(key=key, reverse=desc)
    items.sort(key=lambda i: not i["is_dir"])  # 폴더 먼저

    crumbs, acc = [], ""
    for part in (rel.split("/") if rel else []):
        acc = f"{acc}/{part}" if acc else part
        crumbs.append({"name": part, "path": acc})

    return {
        "path": rel,
        "parent": paths.parent_of(rel) if rel else None,
        "breadcrumbs": crumbs,
        "show_hidden": reveal,
        "watched": _watched(rel, cfg),
        "items": items,
    }


# ─────────────────────────── 변경 ───────────────────────────


class MkdirIn(BaseModel):
    path: str = ""
    name: str = Field(min_length=1)


class RenameIn(BaseModel):
    path: str
    name: str = Field(min_length=1)


class MoveIn(BaseModel):
    path: str
    dest: str = ""  # 목적지 폴더


class DeleteIn(BaseModel):
    path: str


@router.post("/mkdir")
def mkdir(body: MkdirIn) -> dict:
    rel = paths.join(body.path, body.name)
    paths.resolve(body.path)  # 부모 존재 확인
    target = paths.resolve(rel, must_exist=False)
    if target.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="같은 이름이 이미 있습니다")
    target.mkdir(parents=True)
    return {"ok": True, "path": rel}


@router.post("/upload")
async def upload(
    cfg: deps.Settings,
    files: Annotated[list[UploadFile], File()],
    path: Annotated[str, Form()] = "",
) -> dict:
    parent = paths.normalize(path)
    parent_dir = paths.resolve(parent)
    if not parent_dir.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="업로드 대상이 폴더가 아닙니다")

    saved: list[dict] = []
    for up in files:
        name = paths.check_name(Path(up.filename or "unnamed").name)
        rel = paths.join(parent, name)
        dest = paths.resolve(rel, must_exist=False)
        if dest.exists():  # 덮어쓰지 않고 (1), (2) … 를 붙인다
            stem, suffix, n = dest.stem, dest.suffix, 1
            while dest.exists():
                name = f"{stem} ({n}){suffix}"
                rel = paths.join(parent, name)
                dest = paths.resolve(rel, must_exist=False)
                n += 1

        tmp = env.tmp_root / f"up-{time.time_ns()}-{name}"
        size = 0
        try:
            with tmp.open("wb") as fp:
                while chunk := await up.read(1 << 20):
                    size += len(chunk)
                    fp.write(chunk)
            shutil.move(str(tmp), str(dest))
        finally:
            tmp.unlink(missing_ok=True)
            await up.close()

        saved.append({"path": rel, "name": name, "size": size})
        _reindex_after_change(rel, cfg, removed=False)

    return {"ok": True, "saved": saved}


@router.post("/rename")
def rename(body: RenameIn, cfg: deps.Settings) -> dict:
    rel = paths.normalize(body.path)
    if not rel:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="루트는 변경할 수 없습니다")
    src = paths.resolve(rel)
    new_rel = paths.join(paths.parent_of(rel), body.name)
    dest = paths.resolve(new_rel, must_exist=False)
    if dest.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="같은 이름이 이미 있습니다")

    src.rename(dest)
    flags.move(rel, new_rel)
    _reindex_after_change(rel, cfg, removed=True)
    _reindex_after_change(new_rel, cfg, removed=False)
    return {"ok": True, "path": new_rel}


@router.post("/move")
def move(body: MoveIn, cfg: deps.Settings) -> dict:
    rel = paths.normalize(body.path)
    if not rel:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="루트는 이동할 수 없습니다")
    dest_dir = paths.normalize(body.dest)
    if paths.is_under(dest_dir, rel):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="자기 자신 안으로는 이동할 수 없습니다")

    src = paths.resolve(rel)
    parent = paths.resolve(dest_dir)
    if not parent.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="목적지가 폴더가 아닙니다")

    new_rel = paths.join(dest_dir, src.name)
    dest = paths.resolve(new_rel, must_exist=False)
    if dest.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="목적지에 같은 이름이 있습니다")

    shutil.move(str(src), str(dest))
    flags.move(rel, new_rel)
    _reindex_after_change(rel, cfg, removed=True)
    _reindex_after_change(new_rel, cfg, removed=False)
    return {"ok": True, "path": new_rel}


@router.delete("")
def delete(body: DeleteIn, cfg: deps.Settings) -> dict:
    rel = paths.normalize(body.path)
    if not rel:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="루트는 삭제할 수 없습니다")
    target = paths.resolve(rel)
    was_dir = target.is_dir()  # 이동 후에는 확인할 수 없다

    if cfg["nas_use_trash"]:
        env.trash_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = env.trash_root / f"{stamp}__{rel.replace('/', '__')}"
        n = 1
        while dest.exists():
            dest = env.trash_root / f"{stamp}-{n}__{rel.replace('/', '__')}"
            n += 1
        shutil.move(str(target), str(dest))
        where = "trash"
    else:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        where = "gone"

    flags.drop(rel)
    if was_dir:
        # 폴더째 사라졌으면 하위 문서 인덱스도 함께 정리해야 한다
        jobs.enqueue_delete(rel)
    else:
        _reindex_after_change(rel, cfg, removed=True)
    return {"ok": True, "moved_to": where}


# ─────────────────────────── 열람 ───────────────────────────


@router.get("/content")
def content(
    cfg: deps.Settings,
    path: str = Query(...),
    download: bool = Query(False),
    x_file_password: Annotated[str | None, Header()] = None,
) -> FileResponse:
    rel = paths.normalize(path)
    target = paths.resolve(rel)
    if target.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="폴더는 열람할 수 없습니다")

    if security.is_locked(rel) and not security.is_unlocked(rel):
        minutes = int(cfg["file_unlock_minutes"])
        if not x_file_password or not security.try_unlock(rel, x_file_password, minutes):
            raise HTTPException(
                status.HTTP_423_LOCKED, detail="이 파일은 비밀번호로 잠겨 있습니다"
            )

    media, _ = mimetypes.guess_type(target.name)
    inline = (not download) and target.suffix.lower() in set(cfg["nas_preview_exts"])
    disposition = "inline" if inline else "attachment"
    return FileResponse(
        target,
        media_type=media or "application/octet-stream",
        filename=target.name,
        content_disposition_type=disposition,
    )


class UnlockIn(BaseModel):
    path: str
    password: str


@router.post("/unlock")
def unlock(body: UnlockIn, cfg: deps.Settings) -> dict:
    rel = paths.normalize(body.path)
    minutes = int(cfg["file_unlock_minutes"])
    if not security.try_unlock(rel, body.password, minutes):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="비밀번호가 올바르지 않습니다")
    return {"ok": True, "minutes": minutes}


@router.post("/lock/forget")
def forget(body: DeleteIn) -> dict:
    """열어둔 잠금을 즉시 닫는다."""
    security.forget_unlock(paths.normalize(body.path))
    return {"ok": True}


# ─────────────────────────── 플래그 (숨김 / 잠금) ───────────────────────────


class FlagsIn(BaseModel):
    path: str
    hidden: bool | None = None
    lock_password: str | None = None  # 설정할 비밀번호
    clear_lock: bool = False  # True 면 잠금 해제
    note: str | None = None


@router.put("/flags")
def set_flags(body: FlagsIn, cfg: deps.Settings) -> dict:
    rel = paths.normalize(body.path)
    if not rel:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="루트에는 설정할 수 없습니다")
    target = paths.resolve(rel)
    is_dir = target.is_dir()

    if body.hidden is not None:
        flags.set_hidden(rel, body.hidden, is_dir)

    if body.clear_lock:
        security.set_file_lock(rel, None, is_dir)
        flags.prune(rel)
    elif body.lock_password:
        if is_dir:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail="잠금은 파일에만 설정할 수 있습니다"
            )
        security.set_file_lock(rel, body.lock_password, is_dir)

    if body.note is not None:
        flags.set_note(rel, body.note or None, is_dir)

    # 숨김/잠금 상태가 바뀌면 RAG 인덱싱 대상 여부도 달라진다
    if _watched(rel, cfg):
        if is_dir:
            jobs.enqueue(jobs.REINDEX_ALL, {})
        elif flags.is_hidden_inherited(rel) or (
            security.is_locked(rel) and not cfg["rag_index_locked_files"]
        ):
            jobs.enqueue_delete(rel)
        else:
            jobs.enqueue_index(rel)

    return {"ok": True, "flags": flags.row_for(rel)}


# ─────────────────────────── 휴지통 ───────────────────────────


@router.get("/trash")
def list_trash() -> dict:
    root = env.trash_root
    root.mkdir(parents=True, exist_ok=True)
    items = []
    for child in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        try:
            st = child.stat()
        except OSError:
            continue
        stamp, _, original = child.name.partition("__")
        items.append({
            "name": child.name,
            "original": original.replace("__", "/") or child.name,
            "deleted_at": stamp,
            "is_dir": child.is_dir(),
            "size": None if child.is_dir() else st.st_size,
        })
    return {"items": items}


class TrashIn(BaseModel):
    name: str


@router.post("/trash/restore")
def restore_trash(body: TrashIn, cfg: deps.Settings) -> dict:
    name = paths.check_name(body.name)
    src = env.trash_root / name
    if not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="휴지통에 없습니다")

    _, _, original = name.partition("__")
    rel = paths.normalize(original.replace("__", "/")) or name
    dest = paths.resolve(rel, must_exist=False)
    if dest.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="원래 위치에 같은 이름이 있습니다")

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    _reindex_after_change(rel, cfg, removed=False)
    return {"ok": True, "path": rel}


@router.delete("/trash")
def empty_trash(body: TrashIn | None = Body(None)) -> dict:
    """name 을 주면 그 항목만, 없으면 휴지통 전체를 비운다."""
    root = env.trash_root
    root.mkdir(parents=True, exist_ok=True)
    targets = [root / paths.check_name(body.name)] if body and body.name else list(root.iterdir())
    removed = 0
    for t in targets:
        if not t.exists():
            continue
        if t.is_dir():
            shutil.rmtree(t)
        else:
            t.unlink()
        removed += 1
    return {"ok": True, "removed": removed}


@router.get("/tree")
def tree(depth: int = Query(2, ge=1, le=4)) -> dict:
    """이동 대상 선택용 폴더 트리."""

    def walk(base: Path, rel: str, level: int) -> list[dict]:
        if level > depth:
            return []
        out = []
        try:
            children = sorted(
                (c for c in base.iterdir() if c.is_dir()), key=lambda p: p.name.lower()
            )
        except OSError:
            return []
        for c in children:
            r = paths.join(rel, c.name)
            out.append({"name": c.name, "path": r, "children": walk(c, r, level + 1)})
        return out

    return {"path": "", "children": walk(paths.root(), "", 1)}


@router.get("/usage")
def usage() -> dict:
    """디스크 사용량 (설정 페이지 NAS 탭)."""
    total, used, free = shutil.disk_usage(paths.root())
    nas_bytes = 0
    files = 0
    for dirpath, _dirnames, filenames in os.walk(paths.root()):
        for f in filenames:
            try:
                nas_bytes += (Path(dirpath) / f).stat().st_size
                files += 1
            except OSError:
                pass
    return {"disk_total": total, "disk_used": used, "disk_free": free,
            "nas_bytes": nas_bytes, "nas_files": files}
