"""다중 사용자 인증 + 파일 잠금.

비밀번호 해시는 stdlib hashlib.scrypt 로 처리한다 (bcrypt/argon2 외부 의존성 없음).
세션은 사용자 id 를 담은 itsdangerous 서명 쿠키. 서버측 세션 저장소는 두지 않는다.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from . import ctx, db
from .config import env

_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return digest, salt


def verify_password(password: str, digest: bytes, salt: bytes) -> bool:
    if not digest or not salt:
        return False
    candidate = hashlib.scrypt(password.encode(), salt=bytes(salt), **_SCRYPT)
    return hmac.compare_digest(candidate, bytes(digest))


# ─────────────────────────── 계정 ───────────────────────────
# 단일 계정 → 다중 사용자. 사용자마다 별도 Postgres 스키마(u<id>)를 갖는다.

USER_COLS = "id, username, display_name, is_admin, schema_name, created_at"


def _to_ctx(row: dict) -> ctx.User:
    return ctx.User(
        id=row["id"], username=row["username"], schema=row["schema_name"],
        is_admin=row["is_admin"], display_name=row.get("display_name"),
    )


def has_users() -> bool:
    with db.cursor(commit=False, schema="public") as cur:
        cur.execute("SELECT 1 FROM users LIMIT 1")
        return cur.fetchone() is not None


def is_configured() -> bool:
    """최초 설정이 끝났는지 (사용자가 한 명이라도 있는지)."""
    return has_users()


def list_users() -> list[dict]:
    with db.cursor(commit=False, schema="public") as cur:
        cur.execute(f"SELECT {USER_COLS} FROM users ORDER BY id")
        return cur.fetchall()


def get_user(user_id: int) -> ctx.User | None:
    with db.cursor(commit=False, schema="public") as cur:
        cur.execute(f"SELECT {USER_COLS} FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
    return _to_ctx(row) if row else None


def create_user(username: str, password: str, *, is_admin: bool = False,
                display_name: str | None = None) -> ctx.User:
    digest, salt = hash_password(password)
    with db.cursor(schema="public") as cur:
        cur.execute("SELECT 1 FROM users WHERE lower(username) = lower(%s)", (username,))
        if cur.fetchone():
            raise ValueError("이미 있는 아이디입니다")
        cur.execute(
            f"""
            INSERT INTO users (username, display_name, password_hash, salt, is_admin, schema_name)
            VALUES (%s, %s, %s, %s, %s, '')
            RETURNING {USER_COLS}
            """,
            (username, display_name or username, digest, salt, is_admin),
        )
        row = cur.fetchone()
        schema = ctx.schema_for(row["id"])
        cur.execute("UPDATE users SET schema_name = %s WHERE id = %s", (schema, row["id"]))
    row["schema_name"] = schema
    db.create_user_schema(row["id"])
    return _to_ctx(row)


def delete_user(user_id: int) -> None:
    with db.cursor(schema="public") as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
    db.drop_user_schema(user_id)


def set_password(password: str, user_id: int | None = None) -> None:
    uid = user_id or ctx.require().id
    digest, salt = hash_password(password)
    with db.cursor(schema="public") as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s, salt = %s, updated_at = now() WHERE id = %s",
            (digest, salt, uid),
        )


def authenticate(username: str, password: str) -> ctx.User | None:
    with db.cursor(commit=False, schema="public") as cur:
        cur.execute(
            f"SELECT {USER_COLS}, password_hash, salt FROM users WHERE lower(username) = lower(%s)",
            (username,),
        )
        row = cur.fetchone()
    if not row or not verify_password(password, row["password_hash"], row["salt"]):
        return None
    return _to_ctx(row)


def check_password(password: str, user_id: int | None = None) -> bool:
    uid = user_id or ctx.require().id
    with db.cursor(commit=False, schema="public") as cur:
        cur.execute("SELECT password_hash, salt FROM users WHERE id = %s", (uid,))
        row = cur.fetchone()
    return bool(row) and verify_password(password, row["password_hash"], row["salt"])


# ─────────────────────────── 세션 쿠키 ───────────────────────────


def _signer() -> TimestampSigner:
    return TimestampSigner(env.secret_key, salt="chatchat-session")


def issue_session(user_id: int) -> str:
    return _signer().sign(str(user_id).encode()).decode()


def session_user(token: str | None, max_age_days: int) -> ctx.User | None:
    """쿠키에서 사용자를 복원한다. 서명·만료·존재를 모두 확인."""
    if not token:
        return None
    try:
        raw = _signer().unsign(token, max_age=max_age_days * 86400)
    except (BadSignature, SignatureExpired):
        return None
    try:
        return get_user(int(raw.decode()))
    except (ValueError, UnicodeDecodeError):
        return None


# ─────────────────────────── 파일 잠금 ───────────────────────────
# 잠금 해제는 프로세스 메모리에 짧게 캐시한다. 단일 사용자 전제라 이걸로 충분하고
# api 재시작 시 자동으로 다시 잠기는 쪽이 안전한 기본값이다.

_unlocked: dict[tuple[int, str], float] = {}


def _key(path: str) -> tuple[int, str]:
    user = ctx.get()
    return ((user.id if user else 0), path)


def set_file_lock(path: str, password: str | None, is_dir: bool = False) -> None:
    """password=None 이면 잠금 해제(삭제)."""
    if password:
        digest, salt = hash_password(password)
    else:
        digest, salt = None, None
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO path_flags (path, is_dir, lock_hash, lock_salt, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (path) DO UPDATE
               SET lock_hash = EXCLUDED.lock_hash,
                   lock_salt = EXCLUDED.lock_salt,
                   updated_at = now()
            """,
            (path, is_dir, digest, salt),
        )
    _unlocked.pop(_key(path), None)


def is_locked(path: str) -> bool:
    with db.cursor(commit=False) as cur:
        cur.execute(
            "SELECT 1 FROM path_flags WHERE path = %s AND lock_hash IS NOT NULL",
            (path,),
        )
        return cur.fetchone() is not None


def try_unlock(path: str, password: str, minutes: int) -> bool:
    with db.cursor(commit=False) as cur:
        cur.execute(
            "SELECT lock_hash, lock_salt FROM path_flags WHERE path = %s", (path,)
        )
        row = cur.fetchone()
    if not row or not row["lock_hash"]:
        return True  # 잠금이 없으면 통과
    if verify_password(password, row["lock_hash"], row["lock_salt"]):
        _unlocked[_key(path)] = time.time() + minutes * 60
        return True
    return False


def is_unlocked(path: str) -> bool:
    until = _unlocked.get(_key(path))
    if until is None:
        return False
    if until < time.time():
        _unlocked.pop(_key(path), None)
        return False
    return True


def forget_unlock(path: str) -> None:
    _unlocked.pop(_key(path), None)
