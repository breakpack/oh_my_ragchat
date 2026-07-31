"""파일 → 평문 텍스트 추출."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import DOCX_EXTS, PDF_EXTS, TEXT_EXTS

log = logging.getLogger("chatchat.rag.extract")


class Unsupported(Exception):
    """인덱싱 대상이 아닌 확장자."""


def extract(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return _text(path)
    if ext in PDF_EXTS:
        return _pdf(path)
    if ext in DOCX_EXTS:
        return _docx(path)
    raise Unsupported(f"지원하지 않는 확장자입니다: {ext or '(없음)'}")


def _text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # UTF-8 이 아니면 인코딩을 추정한다 (CP949 한글 문서가 흔하다)
    from charset_normalizer import from_bytes

    best = from_bytes(raw).best()
    return str(best) if best else raw.decode("utf-8", errors="replace")


def _pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 - 페이지 하나가 깨져도 나머지는 살린다
            log.warning("pdf 페이지 추출 실패 %s p.%d: %s", path.name, i + 1, exc)
    return "\n\n".join(pages)


def _docx(path: Path) -> str:
    import docx

    doc = docx.Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)
