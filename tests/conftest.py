"""Test fixtures: a fresh in-memory SQLite database seeded with the demo catalogue."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.db import Base, get_db
from app.main import app as api
from app.seed import load_catalogue, seed


@pytest.fixture(scope="session")
def catalogue() -> list[dict]:
    return load_catalogue()


@pytest.fixture
def db(catalogue) -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with TestSession() as session:
        seed(session, catalogue)
        yield session
    engine.dispose()


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    api.dependency_overrides[get_db] = lambda: db
    with TestClient(api) as c:
        yield c
    api.dependency_overrides.clear()
