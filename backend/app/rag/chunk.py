"""문단 경계를 우선하는 청킹."""

from __future__ import annotations

import re

_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")


def clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = _WS.sub(" ", text)
    return _BLANKS.sub("\n\n", text).strip()


def token_est(text: str) -> int:
    """한국어는 대략 2자/토큰, 영문은 4자/토큰. 컨텍스트 예산 가늠용 근사치."""
    hangul = sum(1 for c in text if "가" <= c <= "힣")
    return int(hangul / 2 + (len(text) - hangul) / 4) + 1


def split(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """문단 → 문장 → 강제 절단 순으로 size 를 넘지 않게 자른다."""
    text = clean(text)
    if not text:
        return []
    if overlap >= size:
        overlap = size // 4

    units = _units(text, size)

    chunks: list[str] = []
    buf = ""
    for unit in units:
        if not buf:
            buf = unit
        elif len(buf) + 2 + len(unit) <= size:
            buf = f"{buf}\n\n{unit}"
        else:
            chunks.append(buf)
            # 직전 청크 꼬리를 얹어 문맥이 경계에서 끊기지 않게 한다
            tail = buf[-overlap:] if overlap and overlap + len(unit) + 2 <= size else ""
            buf = f"{tail}\n\n{unit}" if tail else unit
    if buf.strip():
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


def _units(text: str, size: int) -> list[str]:
    """size 이하의 조각 목록. 긴 문단은 문장으로, 그래도 길면 강제로 자른다."""
    out: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            out.append(para)
            continue
        for sent in _sentences(para):
            if len(sent) <= size:
                out.append(sent)
            else:
                out += [sent[i:i + size] for i in range(0, len(sent), size)]
    return out


_SENT = re.compile(r"(?<=[.!?。！？])\s+|(?<=[다요])\.\s+|\n")


def _sentences(para: str) -> list[str]:
    return [s.strip() for s in _SENT.split(para) if s and s.strip()]
