"""Alembic runs this file for every migration command, such as `alembic upgrade head`.

It connects to DATABASE_URL, the same setting the app uses, and applies the
files in versions/ in order, recording the last one it applied in the
`alembic_version` table.
"""

from logging.config import fileConfig
from typing import Final

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.config import settings
from app.db import MIGRATION_LOCK_ID, postgres_connect_args
from app.models import Link

# Every table of the app (importing the models is what registers them).
# `alembic revision --autogenerate` compares the database against this.
TARGET_METADATA: Final = Link.metadata
# A migration may run longer than a request's 5 s, so each statement gets a
# minute. Waiting for a lock stays short (2 s): a schema change stuck in the
# queue for a busy table would block every query that comes after it.
STATEMENT_TIMEOUT_MS: Final = 60_000

if context.config.config_file_name is not None:  # run from the command line
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)


def run_offline() -> None:
    """`alembic upgrade head --sql`: print the SQL instead of running it."""
    context.configure(
        url=settings.database_url, target_metadata=TARGET_METADATA, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    url = settings.database_url
    postgres = url.startswith("postgresql")
    connect_args = postgres_connect_args(STATEMENT_TIMEOUT_MS) if postgres else {}
    # NullPool: one connection, really closed at the end (which also ends the
    # session, and with it the lock below).
    engine = create_engine(url, poolclass=pool.NullPool, connect_args=connect_args)
    with engine.connect() as connection:
        if postgres:
            # Only one migration run at a time. A second one waits here for
            # the first, then finds nothing left to do. (It waits 2 s at most,
            # the lock timeout, then fails instead of hanging.)
            connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
            connection.commit()
        context.configure(
            connection=connection, target_metadata=TARGET_METADATA, compare_server_default=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
