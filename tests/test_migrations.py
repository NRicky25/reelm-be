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
