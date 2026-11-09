"""App settings, read from environment variables / a .env file."""

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Local SQLite file in the project folder by default (the same file no
    # matter which directory alembic / seed / uvicorn are run from).
    # For Supabase, paste its Postgres connection string (session pooler, port 5432).
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'reelm.db').as_posix()}"

    # Frontend origins allowed to call the API. In .env use a JSON list:
    # CORS_ORIGINS=["https://reelm.example.com"]
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # Override "today" (YYYY-MM-DD) for release-date logic, e.g. to keep the
    # demo catalogue's NEW badges stable. Empty = the real date.
    catalog_today: date | None = None

    # Admin panel at /admin. It stays OFF until ADMIN_PASSWORD is set.
    admin_username: str = "admin"
    admin_password: SecretStr | None = None
    # Signs the admin login cookie. If unset, a random key is made at startup
    # (you'll just need to log in again after each restart).
    admin_secret_key: SecretStr | None = None
    # Send the admin cookie over HTTPS only. Turn on in production.
    admin_cookie_secure: bool = False

    # Mux video hosting (Settings > Access Tokens, "Mux Video: Read + Write").
    mux_token_id: str | None = None
    mux_token_secret: SecretStr | None = None
    # Webhooks > signing secret. Optional locally: the upload page also polls Mux.
    mux_webhook_secret: SecretStr | None = None

    # Supabase Storage for images (Project Settings > API). The service-role /
    # secret key has full access: keep it in this backend only, never in reelm-fe.
    supabase_url: str | None = None
    supabase_service_role_key: SecretStr | None = None
    supabase_bucket: str = "images"

    @property
    def storage_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def mux_enabled(self) -> bool:
        return bool(self.mux_token_id and self.mux_token_secret)

    @field_validator("database_url")
    @classmethod
    def use_psycopg3_driver(cls, v: str) -> str:
        """Supabase gives postgres:// or postgresql:// URLs; we use the psycopg 3 driver."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix) :]
        return v

    @field_validator("catalog_today", mode="before")
    @classmethod
    def empty_today_is_none(cls, v: object) -> object:
        return None if v == "" else v


@lru_cache
def get_settings() -> Settings:
    return Settings()
