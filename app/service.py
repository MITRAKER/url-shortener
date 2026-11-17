"""Business logic, kept separate from HTTP so it is easy to test and reuse."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import base62
from app.config import settings
from app.models import Link

# Codes that would clash with our own routes.
RESERVED = {"api", "docs", "redoc", "health", "openapi.json", "favicon.ico"}
MAX_ATTEMPTS = 5


class AliasTaken(Exception):
    pass


class CodeSpaceExhausted(Exception):
    pass


def _as_utc(dt: datetime | None) -> datetime | None:
    # SQLite drops timezone info; Postgres keeps it. Normalise to UTC.
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def create_link(
    session: Session, url: str, alias: str | None = None, expires_in_days: int | None = None
) -> Link:
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=expires_in_days) if expires_in_days else None
    )

    if alias:
        if alias.lower() in RESERVED:
            raise AliasTaken(alias)
        link = Link(code=alias, target_url=url, expires_at=expires_at)
        session.add(link)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise AliasTaken(alias)
        return link

    # Random code + "insert and retry on conflict". We do NOT check-then-insert,
    # because two requests could both see "free" and then both insert. The
    # database's UNIQUE constraint is the single source of truth.
    for _ in range(MAX_ATTEMPTS):
        link = Link(
            code=base62.random_code(settings.code_length), target_url=url, expires_at=expires_at
        )
        session.add(link)
        try:
            session.commit()
            return link
        except IntegrityError:
            session.rollback()
    raise CodeSpaceExhausted()


def get_link(session: Session, code: str) -> Link | None:
    link = session.scalar(select(Link).where(Link.code == code))
    if link is not None:
        link.expires_at = _as_utc(link.expires_at)
        link.created_at = _as_utc(link.created_at)
    return link


def is_expired(link: Link) -> bool:
    return link.expires_at is not None and link.expires_at <= datetime.now(timezone.utc)


def record_click(session: Session, code: str) -> None:
    # An atomic UPDATE ... SET clicks = clicks + 1 lets the database do the
    # increment, so concurrent clicks never overwrite each other (no lost updates).
    session.execute(update(Link).where(Link.code == code).values(clicks=Link.clicks + 1))
    session.commit()
