"""환경변수(불변) + DB 설정(런타임 변경 가능) 분리.

환경변수는 컨테이너 배선용 값만 담고, 사용자가 설정 페이지에서 바꾸는 값은
전부 app_settings 테이블에 둔다. DEFAULT_SETTINGS 가 그 스키마 겸 기본값이다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://chatchat:chatchat@db:5432/chatchat"
    secret_key: str = "dev-insecure-key"

    nas_root: Path = Path("/data/nas")
    trash_root: Path = Path("/data/trash")
    tmp_root: Path = Path("/data/tmp")

    ollama_base_url: str = "http://host.docker.internal:11434"
    chat_model: str = "qwen3.6:27b-mlx"
    extract_model: str = "gemma4:e4b"
    embed_model: str = "bge-m3"
    embed_dim: int = 1024

    session_cookie: str = "chatchat_session"


env = Env()


DEFAULT_SETTINGS: dict[str, Any] = {
    # 연결 / 모델
    "ollama_base_url": env.ollama_base_url,
    "chat_model": env.chat_model,
    "extract_model": env.extract_model,
    "embed_model": env.embed_model,
    "temperature": 0.7,
    "num_ctx": 8192,
    "history_turns": 12,
    "show_thinking": True,  # thinking 지원 모델의 사고 과정을 접이식으로 노출
    # RAG
    "rag_default_enabled": False,
    "rag_default_mode": "hybrid",  # naive | local | global | hybrid
    "rag_watch_dirs": ["documents"],
    "rag_chunk_size": 1200,
    "rag_chunk_overlap": 150,
    "rag_top_k_chunks": 6,
    "rag_top_k_entities": 12,
    "rag_top_k_relations": 12,
    "rag_graph_depth": 1,
    "rag_index_locked_files": False,
    "rag_max_file_mb": 32,
    "rag_extract_graph": True,  # 끄면 벡터 전용 RAG
    # 그래프 추출 제공자. local = Ollama, deepseek = 외부 API(빠름, 토큰 과금)
    "extract_provider": "local",
    "extract_max_entities": 12,  # 출력 토큰을 묶는 상한 (두 제공자 공통)
    "extract_max_relations": 12,
    "deepseek_model": "deepseek-chat",
    "deepseek_base_url": "https://api.deepseek.com",  # 환경변수가 있으면 그쪽이 우선
    "deepseek_concurrency": 4,  # 청크를 동시에 몇 개씩 보낼지
    "deepseek_max_input_chars": 4000,  # 청크를 이 길이로 잘라 보낸다
    # 출력 상한. 실제로 생성한 만큼만 과금되므로 넉넉히 잡아도 비용은 늘지 않는다.
    # 오히려 낮게 잡으면 JSON 이 중간에 잘려 결과를 통째로 버리게 된다.
    "deepseek_max_output_tokens": 3000,
    "deepseek_token_budget": 3_000_000,  # 누적 상한. 넘으면 로컬로 되돌린다 (0=무제한)
    "rag_use_keyword": True,  # 벡터 검색과 별개로 본문 키워드(trgm) 매칭을 병행
    "rag_top_k_keyword": 6,
    "rag_ocr_enabled": True,  # 이미지 / 텍스트 없는 PDF 를 OCR 로 읽는다
    "rag_ocr_langs": "kor+eng",  # tesseract 언어 코드
    "rag_ocr_min_chars": 80,  # PDF 에서 이 이하로 추출되면 스캔본으로 보고 OCR
    # NAS
    "nas_use_trash": True,
    "nas_show_hidden_default": False,
    "nas_preview_exts": [
        ".txt", ".md", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
        ".svg", ".json", ".csv", ".log", ".py", ".ts", ".tsx", ".js", ".sql",
    ],
    # 채팅 첨부
    "chat_attach_max_mb": 20,
    # 이미지 첨부를 OCR 해서 글자도 함께 넘긴다. vision 을 실제로 처리하지 못하는
    # 모델(예: 일부 MLX 빌드)에서도 이미지 속 텍스트를 읽을 수 있게 하는 안전판.
    "chat_attach_ocr_images": True,
    # 보안
    "session_days": 30,
    "file_unlock_minutes": 10,
}

RAG_MODES = ("naive", "local", "global", "hybrid")
EXTRACT_PROVIDERS = ("local", "deepseek")

TEXT_EXTS = {".txt", ".md", ".markdown", ".rst", ".csv", ".log", ".json",
             ".yaml", ".yml", ".py", ".ts", ".tsx", ".js", ".jsx", ".sql",
             ".sh", ".html", ".css", ".java", ".go", ".rs", ".c", ".h",
             ".cpp", ".toml", ".ini", ".env"}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
# OCR 대상. rag_ocr_enabled 가 꺼져 있으면 색인에서 제외된다.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
INDEXABLE_EXTS = TEXT_EXTS | PDF_EXTS | DOCX_EXTS | IMAGE_EXTS
