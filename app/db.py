"""Database engine, session, the declarative Base and shared column types."""

from collections.abc import Iterator

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

# A list stored as JSON: JSONB on Postgres (supports equality / DISTINCT),
# plain JSON on SQLite. MutableList makes in-place edits (.append) persist.
JSONList = MutableList.as_mutable(JSON().with_variant(JSONB(), "postgresql"))


class Base(DeclarativeBase):
    pass


def make_engine(url: str, *, enforce_foreign_keys: bool = True, **kwargs) -> Engine:
    parsed = make_url(url)
    connect_args: dict = {}
    if parsed.get_backend_name() == "sqlite":
        connect_args["check_same_thread"] = False
    elif parsed.port == 6543:
        # Supabase transaction pooler doesn't support prepared statements.
        connect_args["prepare_threshold"] = None

    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True, **kwargs)

    if parsed.get_backend_name() == "sqlite" and enforce_foreign_keys:
        # SQLite ignores foreign keys unless asked, so ON DELETE CASCADE /
        # SET NULL wouldn't behave like they do on Postgres.
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    with SessionLocal() as session:
        yield session
