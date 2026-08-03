"""Application configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- App ----
    secret_key: str = "dev-insecure-secret-change-me"
    debug: bool = True
    app_name: str = "LearnLoop"

    # ---- Database ----
    database_url: str = "sqlite:///./data/app.db"

    # ---- Mesh API (all LLM/embedding traffic) ----
    mesh_api_base_url: str = "https://api.meshapi.ai/v1"
    mesh_api_key: str = ""
    mesh_chat_model: str = "tencent/hy3"
    # Mesh namespaces embedding models by provider (e.g. openai/..., baai/...).
    mesh_embed_model: str = "openai/text-embedding-3-small"
    mesh_embed_enabled: bool = True
    # tencent/hy3 is a reasoning model: real prompts take ~40s, so a short
    # timeout guarantees failure. Measured against the live API.
    mesh_timeout_seconds: float = 120.0
    mesh_max_retries: int = 2

    # ---- Vector store ----
    chroma_path: str = "./data/chroma"
    chroma_collection: str = "products"

    # ---- Recommendation tuning ----
    rec_cooldown_minutes: int = 10
    rec_min_events: int = 8
    rec_ttl_hours: int = 12
    rec_min_signal_events: int = 3
    rec_max_items: int = 4

    # ---- Scheduler ----
    scheduler_enabled: bool = True
    digest_hour: int = 16
    digest_minute: int = 0
    reconcile_minutes: int = 5
    event_retention_days: int = 30
    # Emails render outside a request, so links need an absolute origin.
    base_url: str = "http://127.0.0.1:8000"

    # ---- Email ----
    email_backend: str = "outbox"
    email_from: str = "recommendations@learnloop.dev"
    outbox_dir: str = "./outbox"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True

    # ---- Observability ----
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "learnloop-agent"

    @property
    def mesh_configured(self) -> bool:
        """True when a real Mesh credential is present.

        Everything AI-shaped checks this first, so the site stays fully
        browsable with no key configured.
        """
        return bool(self.mesh_api_key.strip())

    def resolve(self, value: str) -> Path:
        """Resolve a possibly-relative configured path against the project root."""
        path = Path(value)
        return path if path.is_absolute() else (BASE_DIR / path).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
