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
    # NAS
    "nas_use_trash": True,
    "nas_show_hidden_default": False,
    "nas_preview_exts": [
        ".txt", ".md", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
        ".svg", ".json", ".csv", ".log", ".py", ".ts", ".tsx", ".js", ".sql",
    ],
    # 보안
    "session_days": 30,
    "file_unlock_minutes": 10,
}

RAG_MODES = ("naive", "local", "global", "hybrid")

TEXT_EXTS = {".txt", ".md", ".markdown", ".rst", ".csv", ".log", ".json",
             ".yaml", ".yml", ".py", ".ts", ".tsx", ".js", ".jsx", ".sql",
             ".sh", ".html", ".css", ".java", ".go", ".rs", ".c", ".h",
             ".cpp", ".toml", ".ini", ".env"}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
INDEXABLE_EXTS = TEXT_EXTS | PDF_EXTS | DOCX_EXTS
