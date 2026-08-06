"""NAS 루트 샌드박싱.

API 가 주고받는 경로는 항상 NAS 루트 기준 상대 경로다. resolve() 가 심볼릭 링크까지
해석한 뒤 루트 밖을 가리키면 거부하므로 `..` 트래버설과 탈출 symlink 를 함께 막는다.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from fastapi import HTTPException, status

from . import ctx
from .config import env

_BAD_NAMES = {"", ".", ".."}


class PathError(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(status.HTTP_400_BAD_REQUEST, detail=detail)


def user_dir() -> Path:
    """사용자 전용 디렉터리. 없으면 만든다."""
    user = ctx.get()
    base = env.nas_root / (user.username if user else "_shared")
    base.mkdir(parents=True, exist_ok=True)
    return base


def root() -> Path:
    r = user_dir() / "nas"
    r.mkdir(parents=True, exist_ok=True)
    return r.resolve()


def trash_root() -> Path:
    p = user_dir() / "trash"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tmp_root() -> Path:
    p = user_dir() / "tmp"
    p.mkdir(parents=True, exist_ok=True)
    return p


def normalize(rel: str | None) -> str:
    """상대 경로 정규화. 루트는 빈 문자열."""
    raw = (rel or "").strip().replace("\\", "/").strip("/")
    if not raw:
        return ""
    parts: list[str] = []
    for part in PurePosixPath(raw).parts:
        if part in _BAD_NAMES or part.startswith("/"):
            raise PathError(f"허용되지 않는 경로 조각입니다: {part!r}")
        parts.append(part)
    return "/".join(parts)


def obsidian_root() -> Path:
    """옵시디언 볼트를 내보낼 곳. NAS 와 분리된 마운트(iCloud 등)를 쓸 수 있다.

    외부 마운트는 서버 주인의 폴더다. 관리자는 그 폴더에 볼트를 바로 만들고(iOS 옵시디언은
    iCloud 컨테이너의 **최상위** 폴더만 볼트로 인식한다), 다른 사용자는 자기 하위 폴더에 쓴다.
    """
    user = ctx.get()
    p = env.obsidian_root
    if not user or not user.is_admin:
        p = p / (user.username if user else "_shared")
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PathError(
            f"외부 내보내기 폴더를 만들 수 없습니다: {exc}. "
            "OBSIDIAN_HOST_PATH 와 Docker Desktop 의 File sharing 설정을 확인하세요"
        ) from exc
    return p.resolve()


def resolve(rel: str | None, *, must_exist: bool = True) -> Path:
    """상대 경로 → 실제 절대 경로. NAS 루트 밖이면 거부."""
    return resolve_under(root(), rel, must_exist=must_exist)


def resolve_under(r: Path, rel: str | None, *, must_exist: bool = True) -> Path:
    """주어진 루트 기준으로 해석하고 그 밖이면 거부한다."""
    target = (r / normalize(rel)) if normalize(rel) else r

    probe = target if target.exists() else target.parent
    try:
        real_probe = probe.resolve()
    except OSError as exc:
        raise PathError(f"경로를 해석할 수 없습니다: {exc}") from exc

    if real_probe != r and r not in real_probe.parents:
        raise PathError("허용된 루트 밖의 경로는 접근할 수 없습니다")

    if must_exist and not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="경로를 찾을 수 없습니다")
    return target


def to_rel(absolute: Path) -> str:
    """실제 경로 → 루트 기준 상대 경로."""
    try:
        return absolute.resolve().relative_to(root()).as_posix()
    except ValueError:
        return absolute.as_posix()


def check_name(name: str) -> str:
    name = name.strip()
    if not name or name in _BAD_NAMES or "/" in name or "\\" in name:
        raise PathError(f"사용할 수 없는 이름입니다: {name!r}")
    if name.startswith("."):
        # 점 파일은 허용하되 숨김 플래그와 혼동되지 않게 그대로 둔다
        pass
    return name


def join(parent: str, name: str) -> str:
    parent = normalize(parent)
    name = check_name(name)
    return f"{parent}/{name}" if parent else name


def parent_of(rel: str) -> str:
    rel = normalize(rel)
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def is_under(rel: str, ancestor: str) -> bool:
    """rel 이 ancestor 자신이거나 그 하위인지."""
    rel, ancestor = normalize(rel), normalize(ancestor)
    if not ancestor:
        return True
    return rel == ancestor or rel.startswith(ancestor + "/")


def ensure_dirs() -> None:
    """현재 사용자의 디렉터리 일습. 사용자 컨텍스트가 없으면 최상위만 만든다."""
    env.nas_root.mkdir(parents=True, exist_ok=True)
    if ctx.get() is not None:
        for p in (root(), trash_root(), tmp_root()):
            p.mkdir(parents=True, exist_ok=True)
