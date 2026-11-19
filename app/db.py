"""Database engine and session handling."""

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# Every wait should have an upper bound (NASA/JPL rule 2, applied to I/O).
# Talking to Postgres involves four different waits, and each one needs its own
# limit (listed below). One case still has none. Limits 3 and 3b are enforced by
# the server, so they need a server that answers. Limits 4 and 4b only notice a
# dead peer or network: when Postgres is stuck, the kernel of its machine still
# acknowledges our packets. And libpq has no client-side read timeout. So when
# the server is alive but stuck (a paused container, a hung pooler or proxy), a
# query on an open connection waits until the server answers, and so does the
# request that sent it.
CONNECT_TIMEOUT_SECONDS = 5  # 1. opening a new connection
POOL_TIMEOUT_SECONDS = 10  # 2. waiting for a free connection from our pool
STATEMENT_TIMEOUT_MS = 5_000  # 3. running one SQL statement (Postgres cancels it)
LOCK_TIMEOUT_MS = 2_000  # 3b. waiting for a row lock another transaction holds
TCP_USER_TIMEOUT_MS = 10_000  # 4. a dead network: data we sent is never acknowledged
KEEPALIVE_IDLE_SECONDS = 10  # 4b. a silent connection is probed after 10 s,
KEEPALIVE_INTERVAL_SECONDS = 5  # then every 5 s,
KEEPALIVE_COUNT = 3  # and dropped after 3 unanswered probes


class Base(DeclarativeBase):
    pass


def postgres_connect_args() -> dict[str, object]:
    """Connection settings for psycopg (libpq), applied to every connection."""
    return {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        # Server-side limits for every session we open.
        "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout={LOCK_TIMEOUT_MS}",
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
