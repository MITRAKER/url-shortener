"""Business logic, kept separate from HTTP so it is easy to test and reuse."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import base62
from app.config import settings
from app.models import Link

# Codes that would clash with our own routes.
RESERVED = frozenset({"api", "docs", "redoc", "health", "openapi.json", "favicon.ico"})
# Upper bound for the insert-and-retry loop (NASA/JPL rule 2).
MAX_ATTEMPTS = 5


class AliasTakenError(Exception):
    """The requested alias is reserved or already in use."""


class CodeSpaceExhaustedError(Exception):
    """No free random code was found within MAX_ATTEMPTS tries."""


def _as_utc(dt: datetime) -> datetime:
    # SQLite drops timezone info; Postgres keeps it. Normalise to UTC.
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _insert(session: Session, link: Link) -> bool:
    """Try to insert `link`. False means its code is already taken."""
    session.add(link)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    return True


def create_link(
    session: Session, url: str, alias: str | None = None, expires_in_days: int | None = None
) -> Link:
    expires_at = datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None

    if alias is not None:
        link = Link(code=alias, target_url=url, expires_at=expires_at)
        if alias.lower() in RESERVED or not _insert(session, link):
            raise AliasTakenError(alias)
        return link

    # Random code + "insert and retry on conflict". We do NOT check-then-insert,
    # because two requests could both see "free" and then both insert. The
    # database's UNIQUE constraint is the single source of truth.
    for _ in range(MAX_ATTEMPTS):
        link = Link(
            code=base62.random_code(settings.code_length), target_url=url, expires_at=expires_at
        )
        if _insert(session, link):
            return link
    raise CodeSpaceExhaustedError


def get_link(session: Session, code: str) -> Link | None:
    link = session.scalar(select(Link).where(Link.code == code))
    if link is not None:
        link.created_at = _as_utc(link.created_at)
        if link.expires_at is not None:
            link.expires_at = _as_utc(link.expires_at)
    return link


def is_expired(link: Link) -> bool:
    return link.expires_at is not None and link.expires_at <= datetime.now(UTC)


def record_click(session: Session, code: str) -> None:
    # An atomic UPDATE ... SET clicks = clicks + 1 lets the database do the
    # increment, so concurrent clicks never overwrite each other (no lost updates).
    session.execute(update(Link).where(Link.code == code).values(clicks=Link.clicks + 1))
    session.commit()
