"""Database engine and session handling."""

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# Every wait has an upper bound (NASA/JPL rule 2, applied to I/O). Talking to
# Postgres involves four different waits, and each one needs its own limit:
CONNECT_TIMEOUT_SECONDS = 5  # 1. opening a new connection
POOL_TIMEOUT_SECONDS = 10  # 2. waiting for a free connection from our pool
STATEMENT_TIMEOUT_MS = 5_000  # 3. running one SQL statement (Postgres cancels it)
LOCK_TIMEOUT_MS = 2_000  # 3b. waiting for a row lock another transaction holds
TCP_USER_TIMEOUT_MS = 10_000  # 4. a dead network: data we sent is never acknowledged
KEEPALIVE_IDLE_SECONDS = 10  # 4b. a silent connection is probed after 10 s,
KEEPALIVE_INTERVAL_SECONDS = 5  # then every 5 s,
KEEPALIVE_COUNT = 3  # and dropped after 3 unanswered probes
# The PostgreSQL advisory lock held while migrations run (app/migrations/env.py):
# "urls" in ASCII. A lock named by one number never clashes with the locks
# named by two numbers that lease instance numbers (app/instances.py).
MIGRATION_LOCK_ID = 0x75726C73


class Base(DeclarativeBase):
    pass


def postgres_connect_args(
    statement_timeout_ms: int = STATEMENT_TIMEOUT_MS, lock_timeout_ms: int = LOCK_TIMEOUT_MS
) -> dict[str, object]:
    """Connection settings for psycopg (libpq), applied to every connection."""
    return {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        # Server-side settings for every session we open: the limits on each
        # statement and lock wait, and the same keepalives as below, for the
        # other direction. When a client vanishes without closing its
        # connection (its machine lost power, say), PostgreSQL ends the
        # session about 25 s after it went quiet, not after the system's
        # default of over 2 hours. That frees the instance number the session
        # leased (app/instances.py).
        "options": (
            f"-c statement_timeout={statement_timeout_ms} -c lock_timeout={lock_timeout_ms}"
            f" -c tcp_keepalives_idle={KEEPALIVE_IDLE_SECONDS}"
            f" -c tcp_keepalives_interval={KEEPALIVE_INTERVAL_SECONDS}"
            f" -c tcp_keepalives_count={KEEPALIVE_COUNT}"
        ),
        # Client-side: notice a dead server or network instead of waiting forever.
        "tcp_user_timeout": TCP_USER_TIMEOUT_MS,
        "keepalives": 1,
        "keepalives_idle": KEEPALIVE_IDLE_SECONDS,
        "keepalives_interval": KEEPALIVE_INTERVAL_SECONDS,
        "keepalives_count": KEEPALIVE_COUNT,
    }


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        # SQLite (used for quick local runs) needs this to be shared across threads.
        return create_engine(url, connect_args={"check_same_thread": False})
    # A fixed-size connection pool: reusing connections avoids paying the
    # TCP + auth handshake on every request, and caps how many connections
    # one app instance can open (NASA/JPL rule 3: bounded resources).
    return create_engine(
        url,
        pool_size=10,
        max_overflow=20,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,
        connect_args=postgres_connect_args(),
    )


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed afterwards."""
    with SessionLocal() as session:
        yield session
