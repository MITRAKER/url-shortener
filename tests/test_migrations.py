"""Database migrations (app/migrations/). Each test starts from an empty
database and leaves one behind; the other tests build their own tables."""

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db import MIGRATION_LOCK_ID, Base, engine

ROOT = Path(__file__).resolve().parent.parent
HEAD = "0002"
POSTGRES = engine.dialect.name == "postgresql"
needs_postgres = pytest.mark.skipif(not POSTGRES, reason="needs PostgreSQL (set TEST_DATABASE_URL)")


def alembic_config(output: io.StringIO | None = None) -> Config:
    """Like alembic.ini, but without its logging setup, which would replace
    pytest's. The migrations run against DATABASE_URL, as in production."""
    config = Config(output_buffer=output)
    config.set_main_option("script_location", str(ROOT / "app" / "migrations"))
    config.set_main_option("path_separator", "os")
    return config


def drop_everything() -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS links"))
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        if POSTGRES:
            connection.execute(text("DROP SEQUENCE IF EXISTS links_id_seq"))


@pytest.fixture(autouse=True)
def empty_database() -> Iterator[None]:
    drop_everything()
    engine.dispose()  # no pooled connection remembers the old tables
    yield
    drop_everything()


def differences() -> list[Any]:
    """What `alembic revision --autogenerate` would put in a new migration:
    every way the database differs from the models in app/models.py."""
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_server_default": True})
        return list(compare_metadata(context, Base.metadata))


def table_exists(name: str) -> bool:
    return sa.inspect(engine).has_table(name)


def test_the_migrations_build_exactly_what_the_models_describe() -> None:
    """If this fails, a model changed without a migration: run
    `alembic revision --autogenerate -m "..."` and check what it wrote."""
    command.upgrade(alembic_config(), "head")
    assert differences() == []


def test_every_migration_can_be_undone() -> None:
    config = alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert not table_exists("links")
    # And forward again. Each run takes the migration lock, so this also
    # shows that the run before let go of it.
    command.upgrade(config, "head")
    assert differences() == []


def v0_2_0_links_table() -> sa.Table:
    """The table as v0.2.0 created it (with create_all, at startup)."""
    big_int = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    return sa.Table(
        "links",
        sa.MetaData(),
        sa.Column("id", big_int, primary_key=True),  # a BIGSERIAL in PostgreSQL
        sa.Column("code", sa.String(32), nullable=False, unique=True, index=True),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clicks", big_int, nullable=False),
    )


def test_a_database_from_v0_2_0_is_adopted_with_its_links() -> None:
    old = v0_2_0_links_table()
    old.create(engine)
    with engine.begin() as connection:
        connection.execute(
            old.insert().values(
                code="old-link", target_url="https://a.com", created_at=sa.func.now(), clicks=7
            )
        )
    command.upgrade(alembic_config(), "head")
    with engine.connect() as connection:
        assert connection.execute(sa.select(old.c.code, old.c.clicks)).all() == [("old-link", 7)]
    assert differences() == []  # the counter is gone, as the models say
    if POSTGRES:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT to_regclass('links_id_seq')")) is None


@needs_postgres
def test_going_back_to_a_counter_starts_above_the_largest_id() -> None:
    config = alembic_config()
    command.upgrade(config, "head")
    links = v0_2_0_links_table()
    snowflake_id = 2**46  # IDs from the app are large
    with engine.begin() as connection:
        connection.execute(
            links.insert().values(
                id=snowflake_id,
                code="new",
                target_url="https://a.com",
                created_at=sa.func.now(),
                clicks=0,
            )
        )
    command.downgrade(config, "0001")
    with engine.begin() as connection:
        added = connection.execute(
            links.insert()
            .values(code="older", target_url="https://b.com", created_at=sa.func.now(), clicks=0)
            .returning(links.c.id)
        ).scalar_one()
    assert added == snowflake_id + 1


@needs_postgres
def test_a_second_migration_run_waits_its_turn_then_gives_up() -> None:
    """Two deploys at once must not both change the tables. The second waits
    for the first; here, the first never finishes, so it gives up after the
    lock timeout instead of hanging."""
    config = alembic_config()
    with engine.connect() as first_run:
        first_run.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        with pytest.raises(OperationalError, match="lock timeout"):
            command.upgrade(config, "head")
        first_run.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
    assert not table_exists("links")  # the second run changed nothing
    command.upgrade(config, "head")  # its turn now
    assert differences() == []


def test_sql_can_be_printed_instead_of_run() -> None:
    """`alembic upgrade head --sql`, for a database administrator to read
    (and run) by hand. Nothing is sent to the database."""
    output = io.StringIO()
    command.upgrade(alembic_config(output), "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE links" in sql
    assert "CREATE UNIQUE INDEX ix_links_code" in sql
    assert f"version_num='{HEAD}'" in sql
    assert ("DROP SEQUENCE IF EXISTS links_id_seq" in sql) is POSTGRES
    assert not table_exists("links")


def test_alembic_ini_finds_one_line_of_migrations() -> None:
    """What `alembic upgrade head` reads, run from the repository's root."""
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == [HEAD]  # one head: no two migrations branch off
    assert [r.revision for r in script.walk_revisions()] == [HEAD, "0001"]
