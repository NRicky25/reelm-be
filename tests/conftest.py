"""Test fixtures: a fresh database seeded with the demo catalogue for every test.

By default: in-memory SQLite (fast, no setup).

To run the same tests on Postgres (e.g. Supabase), set TEST_DATABASE_URL:

    TEST_DATABASE_URL=postgresql://... uv run pytest

Tests then run inside a separate `reelm_test` schema, which is created and
dropped by the test run. Your real tables in `public` are never touched.
"""

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.config import Settings
from app.db import Base, get_db, make_engine
from app.main import app as api
from app.seed import load_catalogue, seed

TEST_SCHEMA = "reelm_test"
TEMPLATE_SCHEMA = "reelm_test_template"
# copy order respects foreign keys
TABLES = ["genres", "videos", "titles", "title_genres", "seasons", "episodes"]


@pytest.fixture(scope="session")
def catalogue() -> list[dict]:
    return load_catalogue()


def _postgres_url() -> str | None:
    raw = os.environ.get("TEST_DATABASE_URL")
    # reuse the app's driver normalisation (postgres:// -> postgresql+psycopg://)
    return Settings(_env_file=None, database_url=raw).database_url if raw else None


@pytest.fixture(scope="session")
def pg_engine(catalogue) -> Iterator[Engine | None]:
    """A Postgres engine pinned to the test schema, or None when using SQLite."""
    url = _postgres_url()
    if url is None:
        yield None
        return
    engine = make_engine(url)

    @event.listens_for(engine, "connect")
    def _use_test_schema(dbapi_conn, _record):
        with dbapi_conn.cursor() as cur:
            cur.execute(f"SET search_path TO {TEST_SCHEMA}")
        dbapi_conn.commit()

    with engine.begin() as conn:
        for schema in (TEST_SCHEMA, TEMPLATE_SCHEMA):
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {schema}"))
    Base.metadata.create_all(engine)
    # Seed once (slow over the network), then keep a copy to restore per test.
    with Session(engine) as s:
        seed(s, catalogue)
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(
                text(
                    f"CREATE TABLE {TEMPLATE_SCHEMA}.{table} AS TABLE {TEST_SCHEMA}.{table}"
                )
            )
    yield engine
    with engine.begin() as conn:
        for schema in (TEST_SCHEMA, TEMPLATE_SCHEMA):
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    engine.dispose()


def _restore_catalogue(engine: Engine) -> None:
    """Reset the test schema to the seeded catalogue with a few server-side copies."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
        for table in TABLES:
            conn.execute(
                text(f"INSERT INTO {table} SELECT * FROM {TEMPLATE_SCHEMA}.{table}")
            )
        for table in (
            "genres",
            "seasons",
        ):  # serial ids: continue after the copied rows
            conn.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT max(id) FROM {table}))"
                )
            )


@pytest.fixture
def db(catalogue, pg_engine) -> Iterator[Session]:
    if pg_engine is not None:
        engine = pg_engine
        _restore_catalogue(engine)
    else:
        engine = make_engine(
            "sqlite://", poolclass=StaticPool
        )  # same setup as the app (FKs on)
        Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with TestSession() as session:
        if pg_engine is None:
            seed(session, catalogue)
        yield session
    if pg_engine is None:
        engine.dispose()


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    api.dependency_overrides[get_db] = lambda: db
    with TestClient(api) as c:
        yield c
    api.dependency_overrides.clear()
