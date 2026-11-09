"""The Alembic migrations must build exactly the schema the models describe."""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from app.config import PROJECT_ROOT, get_settings
from app.db import Base, make_engine


def test_migrations_match_models(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'migrate.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        command.upgrade(cfg, "head")

        engine = make_engine(url)
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        engine.dispose()
        assert diff == [], f"models and migrations differ: {diff}"

        command.downgrade(cfg, "base")  # downgrade must work too
    finally:
        get_settings.cache_clear()


def test_migrations_keep_existing_data(tmp_path, monkeypatch, catalogue):
    """Upgrading/downgrading a database that already has data must not lose links.

    Regression: SQLite rebuilds a table to alter it; with foreign keys enforced
    the DROP of the old `videos` table nulled every episode's video link.
    """
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from app.seed import seed

    url = f"sqlite:///{(tmp_path / 'data.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = make_engine(url)

    def linked() -> int:
        with engine.connect() as conn:
            return conn.scalar(
                text("SELECT count(*) FROM episodes WHERE video_id IS NOT NULL")
            )

    try:
        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        command.upgrade(cfg, "head")
        with Session(engine) as s:
            seed(s, catalogue)
        assert linked() == 301

        command.downgrade(cfg, "be7f331a038e")  # rebuilds `videos` on SQLite
        assert linked() == 301
        command.upgrade(cfg, "head")  # and again
        assert linked() == 301
    finally:
        engine.dispose()
        get_settings.cache_clear()
