"""Alembic environment: uses the app's DATABASE_URL and models."""

from logging.config import fileConfig

from alembic import context

import app.models  # noqa: F401  (registers all tables on Base.metadata)
from app.config import get_settings
from app.db import Base, make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
DATABASE_URL = get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=DATABASE_URL.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # SQLite changes a table by copying it and dropping the original. With
    # foreign keys enforced, that DROP would fire ON DELETE SET NULL / CASCADE on
    # every row pointing at the table (e.g. wipe every episode's video link).
    engine = make_engine(DATABASE_URL, enforce_foreign_keys=False)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name
            == "sqlite",  # SQLite needs batch mode for ALTERs
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
