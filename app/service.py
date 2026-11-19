"""Business logic, kept separate from HTTP so it is easy to test and reuse."""

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import base62
from app.cache import CachedLink, LinkCache, Lookup
from app.config import settings
from app.models import Link
from app.schemas import ALIAS_PATTERN

# Codes that would clash with our own routes.
RESERVED = frozenset({"api", "docs", "redoc", "health", "openapi.json", "favicon.ico"})
# Upper bound for the insert-and-retry loop (NASA/JPL rule 2).
MAX_ATTEMPTS = 5
# Every code we hand out matches this: aliases are validated against it, and
# random codes use a subset of the same characters.
CODE_PATTERN = re.compile(ALIAS_PATTERN)


class AliasTakenError(Exception):
    """The requested alias is reserved or already in use."""


class CodeSpaceExhaustedError(Exception):
    """No free random code was found within MAX_ATTEMPTS tries."""


class CacheStatus(StrEnum):
    """Sent to clients in the X-Cache header."""

    HIT = "HIT"  # answered from Redis
    MISS = "MISS"  # answered from the database, now cached
    # Answered from the database, not cached: Redis not configured, down, or
    # circuit open, or the cache write did not go through.
    BYPASS = "BYPASS"


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


def _write_through(cache: LinkCache | None, link: Link) -> None:
    # Write-through on create: new links are usually clicked right after they
    # are shared, so we cache them now. This also replaces any "not found"
    # entry left by someone who tried the code before it existed. If the write
    # fails (RedisGateway logs it), that entry stays: the new link answers 404
    # until the entry expires, at most cache_negative_ttl_seconds later.
    if cache is not None:
        expires_at = None if link.expires_at is None else _as_utc(link.expires_at)
        cache.set(link.code, CachedLink(link.target_url, expires_at))


def create_link(
    session: Session,
    url: str,
    alias: str | None = None,
    expires_in_days: int | None = None,
    cache: LinkCache | None = None,
) -> Link:
    expires_at = datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None

    if alias is not None:
        link = Link(code=alias, target_url=url, expires_at=expires_at)
        if alias.lower() in RESERVED or not _insert(session, link):
            raise AliasTakenError(alias)
        _write_through(cache, link)
        return link

    # Random code + "insert and retry on conflict". We do NOT check-then-insert,
    # because two requests could both see "free" and then both insert. The
    # database's UNIQUE constraint is the single source of truth.
    for _ in range(MAX_ATTEMPTS):
        link = Link(
            code=base62.random_code(settings.code_length), target_url=url, expires_at=expires_at
        )
        if _insert(session, link):
            _write_through(cache, link)
            return link
    raise CodeSpaceExhaustedError


def get_link(session: Session, code: str) -> Link | None:
    link = session.scalar(select(Link).where(Link.code == code))
    if link is not None:
        link.created_at = _as_utc(link.created_at)
        if link.expires_at is not None:
            link.expires_at = _as_utc(link.expires_at)
    return link


def resolve(
    session: Session, cache: LinkCache | None, code: str
) -> tuple[CachedLink | None, CacheStatus]:
    """Find where `code` points, using the cache when there is one."""
    found = Lookup.UNAVAILABLE if cache is None else cache.get(code)
    if isinstance(found, CachedLink):
        return found, CacheStatus.HIT
    if found is Lookup.NOT_FOUND:
        return None, CacheStatus.HIT

    link = get_link(session, code)
    result = None if link is None else CachedLink(link.target_url, link.expires_at)
    if cache is None or found is Lookup.UNAVAILABLE:
        # No cache, or Redis just failed: don't wait on it a second time to write.
        return result, CacheStatus.BYPASS
    # Negative caching: if the code does not exist, remember that briefly, so a
    # flood of requests for made-up codes can't all reach the database.
    stored = cache.set_not_found(code) if result is None else cache.set(code, result)
    # MISS says the answer is now cached. If Redis did not store it (the write
    # failed, the circuit opened, or a link created a moment ago is already
    # cached), say BYPASS.
    return result, CacheStatus.MISS if stored else CacheStatus.BYPASS


def is_expired(link: Link | CachedLink) -> bool:
    return link.expires_at is not None and link.expires_at <= datetime.now(UTC)


def record_click(session: Session, code: str) -> None:
    # An atomic UPDATE ... SET clicks = clicks + 1 lets the database do the
    # increment, so concurrent clicks never overwrite each other (no lost updates).
    session.execute(update(Link).where(Link.code == code).values(clicks=Link.clicks + 1))
    session.commit()
