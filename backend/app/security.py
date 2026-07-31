"""단일 사용자 인증 + 파일 잠금.

비밀번호 해시는 stdlib hashlib.scrypt 로 처리한다 (bcrypt/argon2 외부 의존성 없음).
세션은 itsdangerous 서명 쿠키. 단일 사용자·로컬 전용이므로 서버측 세션 저장소는 두지 않는다.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from . import db
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


def is_configured() -> bool:
    with db.cursor(commit=False) as cur:
        cur.execute("SELECT password_hash FROM auth_user WHERE id = 1")
        row = cur.fetchone()
    return bool(row and row["password_hash"])


def set_password(password: str) -> None:
    digest, salt = hash_password(password)
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE auth_user
               SET password_hash = %s, salt = %s, updated_at = now()
             WHERE id = 1
            """,
            (digest, salt),
        )


def check_password(password: str) -> bool:
    with db.cursor(commit=False) as cur:
        cur.execute("SELECT password_hash, salt FROM auth_user WHERE id = 1")
        row = cur.fetchone()
    if not row:
        return False
    return verify_password(password, row["password_hash"], row["salt"])


# ─────────────────────────── 세션 쿠키 ───────────────────────────


def _signer() -> TimestampSigner:
    return TimestampSigner(env.secret_key, salt="chatchat-session")


def issue_session() -> str:
    return _signer().sign(b"owner").decode()


def valid_session(token: str | None, max_age_days: int) -> bool:
    if not token:
        return False
    try:
        _signer().unsign(token, max_age=max_age_days * 86400)
        return True
    except (BadSignature, SignatureExpired):
        return False


# ─────────────────────────── 파일 잠금 ───────────────────────────
# 잠금 해제는 프로세스 메모리에 짧게 캐시한다. 단일 사용자 전제라 이걸로 충분하고
# api 재시작 시 자동으로 다시 잠기는 쪽이 안전한 기본값이다.

_unlocked: dict[str, float] = {}


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
    _unlocked.pop(path, None)


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
        _unlocked[path] = time.time() + minutes * 60
        return True
    return False


def is_unlocked(path: str) -> bool:
    until = _unlocked.get(path)
    if until is None:
        return False
    if until < time.time():
        _unlocked.pop(path, None)
        return False
    return True


def forget_unlock(path: str) -> None:
    _unlocked.pop(path, None)
