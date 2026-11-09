"""App settings, read from environment variables / a .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Local SQLite by default. Point at Supabase with e.g.
    # DATABASE_URL=postgresql+psycopg://postgres.<ref>:<password>@<host>:5432/postgres
    database_url: str = "sqlite:///./reelm.db"

    # Frontend origins allowed to call the API. In .env use a JSON list:
    # CORS_ORIGINS=["https://reelm.example.com"]
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # Override "today" for release-date logic (YYYY-MM-DD), e.g. to keep the demo
    # catalogue's NEW badges stable. Empty = the real date.
    catalog_today: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
