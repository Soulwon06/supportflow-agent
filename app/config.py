"""Runtime configuration for SupportFlow's optional LLM integration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class SupportFlowSettings:
    provider: str = "openai_compatible"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    write_timeout: float = 10.0
    pool_timeout: float = 5.0
    max_retries: int = 1
    port: int = 8001
    reranker_model_path: str = "/models/bge-reranker-v2-m3"
    reranker_device: str = "cpu"
    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    embedding_device: str = "cpu"
    database_path: str = "data/supportflow_demo.db"

    @classmethod
    def from_env(cls) -> "SupportFlowSettings":
        # Local .env is useful for the explicit smoke test, but never required
        # for importing or starting the application.
        load_dotenv(override=False)
        return cls(
            provider=os.getenv("SUPPORTFLOW_AI_PROVIDER", cls.provider),
            api_key=os.getenv("SUPPORTFLOW_OPENAI_API_KEY", ""),
            base_url=os.getenv("SUPPORTFLOW_OPENAI_BASE_URL", cls.base_url).rstrip("/"),
            model=os.getenv("SUPPORTFLOW_OPENAI_MODEL", cls.model),
            connect_timeout=_float_env("SUPPORTFLOW_LLM_CONNECT_TIMEOUT", cls.connect_timeout),
            read_timeout=_float_env("SUPPORTFLOW_LLM_READ_TIMEOUT", cls.read_timeout),
            write_timeout=_float_env("SUPPORTFLOW_LLM_WRITE_TIMEOUT", cls.write_timeout),
            pool_timeout=_float_env("SUPPORTFLOW_LLM_POOL_TIMEOUT", cls.pool_timeout),
            max_retries=max(0, _int_env("SUPPORTFLOW_LLM_MAX_RETRIES", cls.max_retries)),
            port=max(1, _int_env("SUPPORTFLOW_PORT", cls.port)),
            reranker_model_path=os.getenv(
                "SUPPORTFLOW_RERANKER_PATH", cls.reranker_model_path
            ),
            reranker_device=os.getenv(
                "SUPPORTFLOW_RERANKER_DEVICE", cls.reranker_device
            ),
            embedding_model=os.getenv(
                "SUPPORTFLOW_EMBEDDING_MODEL", cls.embedding_model
            ),
            embedding_device=os.getenv(
                "SUPPORTFLOW_EMBEDDING_DEVICE", cls.embedding_device
            ),
            database_path=os.getenv(
                "SUPPORTFLOW_DATABASE_PATH", cls.database_path
            ),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.provider == "openai_compatible")


@lru_cache(maxsize=1)
def get_settings() -> SupportFlowSettings:
    return SupportFlowSettings.from_env()


def reload_settings() -> SupportFlowSettings:
    """Refresh settings for tests and explicit local configuration changes."""
    get_settings.cache_clear()
    return get_settings()
