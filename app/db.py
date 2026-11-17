"""Database engine and session handling."""
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        # SQLite (used for quick local runs) needs this to be shared across threads.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # A small connection pool: reusing connections avoids paying the
        # TCP + auth handshake on every request.
        kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True)
    return create_engine(url, **kwargs)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed afterwards."""
    with SessionLocal() as session:
        yield session
