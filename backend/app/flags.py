"""path_flags 조회/변경 — 숨김(7번)과 파일 잠금(8번) 상태.

숨김은 상속된다: 폴더가 숨김이면 그 하위 전체가 숨김 취급이다.
잠금은 상속되지 않는다(파일 단위 게이트).
"""

from __future__ import annotations

from typing import Any

from . import db, paths


def row_for(path: str) -> dict[str, Any] | None:
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT path, is_dir, hidden, (lock_hash IS NOT NULL) AS locked, note
              FROM path_flags WHERE path = %s
            """,
            (paths.normalize(path),),
        )
        return cur.fetchone()


def flags_for(path_list: list[str]) -> dict[str, dict[str, Any]]:
    if not path_list:
        return {}
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT path, hidden, (lock_hash IS NOT NULL) AS locked, note
              FROM path_flags WHERE path = ANY(%s)
            """,
            (path_list,),
        )
        return {r["path"]: r for r in cur.fetchall()}


def hidden_paths() -> list[str]:
    with db.cursor(commit=False) as cur:
        cur.execute("SELECT path FROM path_flags WHERE hidden")
        return [r["path"] for r in cur.fetchall()]


def locked_paths() -> list[str]:
    with db.cursor(commit=False) as cur:
        cur.execute("SELECT path FROM path_flags WHERE lock_hash IS NOT NULL")
        return [r["path"] for r in cur.fetchall()]


def is_hidden_inherited(rel: str, hidden: list[str] | None = None) -> bool:
    """rel 자신이나 조상 폴더가 숨김인지."""
    hidden = hidden if hidden is not None else hidden_paths()
    return any(paths.is_under(rel, h) for h in hidden)


def set_hidden(path: str, hidden: bool, is_dir: bool) -> None:
    path = paths.normalize(path)
    if not path:
        raise paths.PathError("루트는 숨길 수 없습니다")
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO path_flags (path, is_dir, hidden, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (path) DO UPDATE
               SET hidden = EXCLUDED.hidden, is_dir = EXCLUDED.is_dir, updated_at = now()
            """,
            (path, is_dir, hidden),
        )
    prune(path)


def set_note(path: str, note: str | None, is_dir: bool) -> None:
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO path_flags (path, is_dir, note, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (path) DO UPDATE
               SET note = EXCLUDED.note, updated_at = now()
            """,
            (paths.normalize(path), is_dir, note),
        )
    prune(path)


def prune(path: str) -> None:
    """플래그가 전부 비워진 행은 지운다."""
    with db.cursor() as cur:
        cur.execute(
            """
            DELETE FROM path_flags
             WHERE path = %s AND hidden = false AND lock_hash IS NULL
               AND (note IS NULL OR note = '')
            """,
            (paths.normalize(path),),
        )


def move(old: str, new: str) -> None:
    """파일/폴더 이동·이름변경 시 플래그도 따라가게 한다 (하위 경로 포함)."""
    old, new = paths.normalize(old), paths.normalize(new)
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE path_flags
               SET path = %s || substring(path from %s), updated_at = now()
             WHERE path = %s OR path LIKE %s
            """,
            (new, len(old) + 1, old, old + "/%"),
        )


def drop(path: str) -> None:
    old = paths.normalize(path)
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM path_flags WHERE path = %s OR path LIKE %s",
            (old, old + "/%"),
        )


def list_all() -> list[dict[str, Any]]:
    with db.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT path, is_dir, hidden, (lock_hash IS NOT NULL) AS locked,
                   note, updated_at
              FROM path_flags
             ORDER BY path
            """
        )
        return cur.fetchall()
