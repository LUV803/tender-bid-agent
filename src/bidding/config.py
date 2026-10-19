"""Project configuration loaded from the local ``.env`` file.

Secrets must never be printed or committed.  ``.env`` is ignored by Git;
``.env.example`` documents the available keys with empty secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load before any HTTP client is created.  NO_PROXY keeps the Windows system
# proxy away from localhost (Chroma/Redis).
load_dotenv(PROJECT_ROOT / ".env")
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
os.environ.setdefault("no_proxy", "localhost,127.0.0.1")


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env_str(name, "")
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on", "是"}


def _env_path(name: str, default: str) -> Path:
    path = Path(_env_str(name, default)).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path

    upload_dir: Path
    files_dir: Path
    parsed_dir: Path
    images_dir: Path
    bids_dir: Path

    llm_provider: str
    deepseek_api_key: str
    deepseek_model: str
    deepseek_base_url: str
    llm_timeout: float
    llm_max_retries: int
    llm_temperature: float
    llm_max_tokens: int
    llm_thinking: str

    zhipu_api_key: str
    zhipu_base_url: str
    embedding_model: str
    embedding_dimensions: int
    embedding_batch_size: int

    chroma_host: str
    chroma_port: int
    chroma_dir: Path
    redis_url: str

    mineru_mode: str
    mineru_api_key: str
    mineru_endpoint: str
    mineru_batch_upload_endpoint: str
    mineru_batch_result_endpoint: str
    mineru_timeout: float
    mineru_download_timeout: float
    mineru_model_version: str
    mineru_poll_interval: float
    mineru_max_polls: int
    mineru_max_retries: int
    mineru_retry_backoff: float
    mineru_language: str
    mineru_enable_formula: bool
    mineru_enable_table: bool
    mineru_is_ocr: bool
    mineru_max_file_mb: int
    mineru_max_pages: int

    extract_max_chars: int
    extract_bisect_min_chars: int
    extract_bisect_max_depth: int

    chunk_target: int
    chunk_hard_max: int
    chunk_overlap: int
    ingest_batch_size: int

    @property
    def llm_configured(self) -> bool:
        return bool(self.deepseek_api_key)

    @property
    def embedding_configured(self) -> bool:
        return bool(self.zhipu_api_key)

    @property
    def mineru_configured(self) -> bool:
        return bool(self.mineru_api_key)

    def ensure_directories(self) -> None:
        for directory in (
            self.upload_dir,
            self.files_dir,
            self.parsed_dir,
            self.images_dir,
            self.bids_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def relative(self, path: Path) -> str:
        """Return a project-relative POSIX path when possible."""
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.project_root).as_posix()
        except ValueError:
            return resolved.as_posix()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    upload_dir = _env_path("UPLOAD_DIR", "upload")
    return Settings(
        project_root=PROJECT_ROOT,
        upload_dir=upload_dir,
        files_dir=upload_dir / "files",
        parsed_dir=upload_dir / "parsed",
        images_dir=upload_dir / "images",
        bids_dir=upload_dir / "bids",
        llm_provider=_env_str("LLM_PROVIDER", "deepseek").lower(),
        deepseek_api_key=_env_str("DEEPSEEK_API_KEY"),
        deepseek_model=_env_str("DEEPSEEK_MODEL", "deepseek-chat"),
        deepseek_base_url=_env_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        llm_timeout=_env_float("LLM_TIMEOUT", 120.0),
        llm_max_retries=_env_int("LLM_MAX_RETRIES", 2),
        llm_temperature=_env_float("LLM_TEMPERATURE", 0.0),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 32768),
        llm_thinking=_env_str("LLM_THINKING", "disabled").lower(),
        zhipu_api_key=_env_str("ZHIPU_API_KEY"),
        zhipu_base_url=_env_str("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
        embedding_model=_env_str("EMBEDDING_MODEL", "embedding-3"),
        embedding_dimensions=_env_int("EMBEDDING_DIMENSIONS", 2048),
        embedding_batch_size=_env_int("EMBEDDING_BATCH_SIZE", 16),
        chroma_host=_env_str("CHROMA_HOST", "localhost"),
        chroma_port=_env_int("CHROMA_PORT", 8000),
        chroma_dir=_env_path("CHROMA_DIR", "chroma_db"),
        redis_url=_env_str("REDIS_URL", "redis://localhost:6379/0"),
        mineru_mode=_env_str("MINERU_MODE", "cloud").lower(),
        mineru_api_key=_env_str("MINERU_API_KEY"),
        mineru_endpoint=_env_str(
            "MINERU_ENDPOINT", "https://mineru.net/api/v4/extract/task"
        ),
        mineru_batch_upload_endpoint=_env_str(
            "MINERU_BATCH_UPLOAD_ENDPOINT",
            "https://mineru.net/api/v4/file-urls/batch",
        ),
        mineru_batch_result_endpoint=_env_str(
            "MINERU_BATCH_RESULT_ENDPOINT",
            "https://mineru.net/api/v4/extract-results/batch/{batch_id}",
        ),
        mineru_timeout=_env_float("MINERU_TIMEOUT", 180.0),
        mineru_download_timeout=_env_float("MINERU_DOWNLOAD_TIMEOUT", 300.0),
        mineru_model_version=_env_str("MINERU_MODEL_VERSION", "vlm"),
        mineru_poll_interval=_env_float("MINERU_POLL_INTERVAL", 5.0),
        mineru_max_polls=_env_int("MINERU_MAX_POLLS", 120),
        mineru_max_retries=_env_int("MINERU_MAX_RETRIES", 2),
        mineru_retry_backoff=_env_float("MINERU_RETRY_BACKOFF", 2.0),
        mineru_language=_env_str("MINERU_LANGUAGE", "ch"),
        mineru_enable_formula=_env_bool("MINERU_ENABLE_FORMULA", True),
        mineru_enable_table=_env_bool("MINERU_ENABLE_TABLE", True),
        mineru_is_ocr=_env_bool("MINERU_IS_OCR", False),
        mineru_max_file_mb=_env_int("MINERU_MAX_FILE_MB", 200),
        mineru_max_pages=_env_int("MINERU_MAX_PAGES", 200),
        extract_max_chars=_env_int("EXTRACT_MAX_CHARS", 12000),
        extract_bisect_min_chars=_env_int("EXTRACT_BISECT_MIN_CHARS", 1200),
        extract_bisect_max_depth=_env_int("EXTRACT_BISECT_MAX_DEPTH", 2),
        chunk_target=_env_int("CHUNK_TARGET_CHARS", 450),
        chunk_hard_max=_env_int("CHUNK_HARD_MAX_CHARS", 700),
        chunk_overlap=_env_int("CHUNK_OVERLAP_CHARS", 80),
        ingest_batch_size=_env_int("INGEST_BATCH_SIZE", 64),
    )
