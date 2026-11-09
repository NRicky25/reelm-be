"""Test fixtures: a fresh database seeded with the demo catalogue for every test.

By default: in-memory SQLite (fast, no setup).

To run the same tests on Postgres (e.g. Supabase), set TEST_DATABASE_URL:

    TEST_DATABASE_URL=postgresql://... uv run pytest

Tests then run inside a separate `reelm_test` schema, which is created and
dropped by the test run. Your real tables in `public` are never touched.
"""

import os

# Importing the app reads settings: make sure it can never reach the real
# database, Mux or Supabase from .env (environment variables beat .env).
os.environ.update(
    DATABASE_URL="sqlite://",
    ADMIN_PASSWORD="",
    SUPABASE_URL="",
    SUPABASE_SERVICE_ROLE_KEY="",
    MUX_TOKEN_ID="",
    MUX_TOKEN_SECRET="",
    MUX_WEBHOOK_SECRET="",
)

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, event, make_url, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.config import Settings
from app.db import Base, get_db, get_read_db, make_engine
from app.main import app as api
from app.seed import load_catalogue, seed

TEST_SCHEMA = "reelm_test"
TEMPLATE_SCHEMA = "reelm_test_template"
# every table, parents before children (copy order respects foreign keys)
TABLES = [t.name for t in Base.metadata.sorted_tables]
SERIAL_TABLES = [
    t.name
    for t in Base.metadata.sorted_tables
    if "id" in t.c and t.c.id.primary_key and isinstance(t.c.id.type, Integer)
]


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
    if make_url(url).port == 6543:
        pytest.exit(
            "TEST_DATABASE_URL uses the transaction pooler (port 6543); use the "
            "session pooler (5432) so tests stay inside their own schema.",
            returncode=2,
        )
    base_engine = make_engine(url)

    # Belt and braces: unqualified raw SQL lands in the test schema...
    @event.listens_for(base_engine, "connect")
    def _use_test_schema(dbapi_conn, _record):
        with dbapi_conn.cursor() as cur:
            cur.execute(f"SET search_path TO {TEST_SCHEMA}")
        dbapi_conn.commit()

    # ...and every ORM / DDL statement names the test schema explicitly, so
    # nothing can reach the real tables in `public` even if search_path is lost.
    engine = base_engine.execution_options(schema_translate_map={None: TEST_SCHEMA})

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
    base_engine.dispose()


def _restore_catalogue(engine: Engine) -> None:
    """Reset the test schema to the seeded catalogue with a few server-side copies.

    All names are schema-qualified: this TRUNCATE must never hit `public`.
    """
    test = [f"{TEST_SCHEMA}.{t}" for t in TABLES]
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(test)} RESTART IDENTITY CASCADE"))
        for table in TABLES:
            conn.execute(
                text(
                    f"INSERT INTO {TEST_SCHEMA}.{table} SELECT * FROM {TEMPLATE_SCHEMA}.{table}"
                )
            )
        for table in SERIAL_TABLES:  # serial ids: continue after the copied rows
            conn.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{TEST_SCHEMA}.{table}', 'id'), "
                    f"(SELECT max(id) FROM {TEST_SCHEMA}.{table}))"
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
    api.dependency_overrides[get_read_db] = lambda: db
    with TestClient(api) as c:
        yield c
    api.dependency_overrides.clear()
