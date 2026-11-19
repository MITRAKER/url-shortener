"""Timeouts on the PostgreSQL connection. They need a real Postgres server, so
they run in CI's PostgreSQL job (and locally when TEST_DATABASE_URL is set)."""

import time
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from app.db import (
    KEEPALIVE_COUNT,
    KEEPALIVE_IDLE_SECONDS,
    KEEPALIVE_INTERVAL_SECONDS,
    LOCK_TIMEOUT_MS,
    STATEMENT_TIMEOUT_MS,
    engine,
    make_engine,
)

pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="needs PostgreSQL (set TEST_DATABASE_URL)"
)
LOCK_KEY = 4242  # an arbitrary number naming one advisory lock


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    """A fresh engine built exactly like the app's."""
    fresh = make_engine(engine.url.render_as_string(hide_password=False))
    yield fresh
    fresh.dispose()  # close its pooled connections


def _setting(conn_engine: Engine, name: str) -> str:
    with conn_engine.connect() as conn:
        query = text("SELECT setting FROM pg_settings WHERE name = :name")
        return str(conn.execute(query, {"name": name}).scalar_one())


def test_every_session_has_query_and_lock_timeouts(pg_engine: Engine) -> None:
    assert _setting(pg_engine, "statement_timeout") == str(STATEMENT_TIMEOUT_MS)
    assert _setting(pg_engine, "lock_timeout") == str(LOCK_TIMEOUT_MS)


def test_every_session_asks_the_server_for_keepalives(pg_engine: Engine) -> None:
    """A client whose machine loses power closes nothing, so PostgreSQL only
    notices through its own TCP keepalives. With these, it ends the session,
    and frees the instance number the session leased, after about 25 s; with
    the system defaults, after over 2 hours. (Over TCP, as in CI: a session on
    a Unix socket has no keepalives, and reads 0 for all three.)"""
    assert _setting(pg_engine, "tcp_keepalives_idle") == str(KEEPALIVE_IDLE_SECONDS)
    assert _setting(pg_engine, "tcp_keepalives_interval") == str(KEEPALIVE_INTERVAL_SECONDS)
    assert _setting(pg_engine, "tcp_keepalives_count") == str(KEEPALIVE_COUNT)


def test_waiting_for_a_lock_fails_instead_of_hanging(pg_engine: Engine) -> None:
    with pg_engine.connect() as holder, pg_engine.connect() as waiter:
        holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_KEY})
        try:
            start = time.monotonic()
            with pytest.raises(OperationalError, match="lock timeout"):
                waiter.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_KEY})
            assert time.monotonic() - start < LOCK_TIMEOUT_MS / 1000 + 3
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
