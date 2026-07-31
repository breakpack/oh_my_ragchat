"""기동 시 db/migrations/*.sql 을 순서대로 적용한다.

initdb 스크립트(001_schema.sql)는 새 볼륨에서만 돌기 때문에, 이미 데이터가 있는
설치본에도 스키마 변경을 반영하려면 별도 경로가 필요하다. 마이그레이션 파일은
전부 멱등하게 작성하고, 적용 이력은 schema_migrations 에 남긴다.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import db

log = logging.getLogger("chatchat.migrate")

MIGRATIONS_DIR = Path(os.getenv("MIGRATIONS_DIR", "/srv/migrations"))


def run() -> list[str]:
    if not MIGRATIONS_DIR.is_dir():
        log.info("마이그레이션 디렉터리 없음: %s (건너뜀)", MIGRATIONS_DIR)
        return []

    with db.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name       text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute("SELECT name FROM schema_migrations")
        done = {r["name"] for r in cur.fetchall()}

    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        sql = path.read_text(encoding="utf-8")
        log.info("마이그레이션 적용: %s", path.name)
        with db.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (name) VALUES (%s) ON CONFLICT DO NOTHING",
                (path.name,),
            )
        applied.append(path.name)

    if applied:
        log.info("마이그레이션 %d건 적용 완료", len(applied))
    return applied
