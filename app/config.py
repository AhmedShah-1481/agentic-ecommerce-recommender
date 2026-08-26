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
    app_name: str = "Bazario Mart"

    # ---- Database ----
    database_url: str = "sqlite:///./data/app.db"

    # ---- Gemini (all LLM/embedding traffic) ----
    gemini_api_key: str = ""
    gemini_chat_model: str = "gemini-3.6-flash"
    gemini_embed_model: str = "gemini-embedding-001"
    gemini_embed_enabled: bool = True
    # gemini-embedding-001 is natively 3072-dim but supports Matryoshka
    # truncation. 768 keeps the Chroma index small without measurably hurting
    # retrieval, and is one of the three dimensions Google recommends (768,
    # 1536, 3072). Changing it changes the collection's dimensionality, which
    # Chroma pins at first insert - see catalog.reconcile().
    gemini_embed_dimensions: int = 768
    gemini_timeout_seconds: float = 60.0
    gemini_max_retries: int = 2

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
    # Emails render outside a request, so links need an absolute origin. This is
    # the *API* origin - anything customer-facing in an email points at
    # `storefront_url` instead, since the pages a shopper lands on are React now.
    base_url: str = "http://127.0.0.1:8000"

    # ---- Storefront (React + Vite) ----
    # Where the customer-facing app is served from. Used for links in emails and
    # for the API index at "/", so a digest sent by the scheduler points at a
    # page that exists rather than at a route that is now JSON.
    storefront_url: str = "http://localhost:5173"
    # Comma-separated rather than list[str]: pydantic-settings parses a list from
    # the environment as JSON, which means quoting a JSON array inside .env. A
    # comma-separated string is what anyone editing that file expects to write.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---- Email ----
    email_backend: str = "outbox"
    email_from: str = "recommendations@bazariomart.com"
    outbox_dir: str = "./outbox"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True

    # ---- Observability ----
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "bazario-agent"

    @property
    def gemini_configured(self) -> bool:
        """True when a real Gemini credential is present.

        Everything AI-shaped checks this first, so the site stays fully
        browsable with no key configured.
        """
        return bool(self.gemini_api_key.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        """Allowed browser origins for the JSON API.

        Explicit origins, never `["*"]`: the storefront authenticates with the
        session cookie, so the middleware runs with `allow_credentials=True`,
        and browsers refuse a wildcard `Access-Control-Allow-Origin` on a
        credentialed request. A wildcard here would fail at runtime rather than
        loosen anything.
        """
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def resolve(self, value: str) -> Path:
        """Resolve a possibly-relative configured path against the project root."""
        path = Path(value)
        return path if path.is_absolute() else (BASE_DIR / path).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
