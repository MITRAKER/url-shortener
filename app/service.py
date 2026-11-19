"""Business logic, kept separate from HTTP so it is easy to test and reuse."""

import logging
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import metrics
from app.cache import CachedLink, LinkCache, Lookup
from app.clicks import ClickStream
from app.ids import LinkIds
from app.models import Link
from app.schemas import ALIAS_PATTERN

log = logging.getLogger(__name__)

# Codes that would clash with our own routes.
RESERVED = frozenset({"api", "docs", "redoc", "health", "metrics", "openapi.json", "favicon.ico"})
# Upper bound for the insert-and-retry loop (NASA/JPL rule 2).
MAX_ATTEMPTS = 5
# Every code we hand out matches this: aliases are validated against it, and
# generated codes use a subset of the same characters.
CODE_PATTERN = re.compile(ALIAS_PATTERN)


class AliasTakenError(Exception):
    """The requested alias is reserved or already in use."""


class IdClashError(Exception):
    """MAX_ATTEMPTS new IDs in a row clashed with existing rows.

    Should never happen: it means two instances share a number, or the clock
    jumped back across a restart. The database refused the duplicates, so no
    data is wrong, but something is misconfigured.
    """


class CacheStatus(StrEnum):
    """Sent to clients in the X-Cache header."""

    HIT = "HIT"  # answered from Redis
    MISS = "MISS"  # answered from the database, now cached
    BYPASS = "BYPASS"  # Redis not used: not configured, down, or circuit open


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
    # entry left by someone who tried the code before it existed.
    if cache is not None:
        expires_at = None if link.expires_at is None else _as_utc(link.expires_at)
        cache.set(link.code, CachedLink(link.target_url, expires_at))


def _code_exists(session: Session, code: str) -> bool:
    return session.scalar(select(Link.id).where(Link.code == code)) is not None


def create_link(
    session: Session,
    ids: LinkIds,
    url: str,
    alias: str | None = None,
    expires_in_days: int | None = None,
    cache: LinkCache | None = None,
) -> Link:
    if alias is not None and alias.lower() in RESERVED:
        raise AliasTakenError(alias)
    expires_at = datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None

    # Snowflake IDs and their codes are unique by construction, so the first
    # INSERT succeeds. The database's UNIQUE constraints still have the last
    # word: we never check-then-insert (two requests could both see "free"),
    # we insert and look at the answer.
    for _ in range(MAX_ATTEMPTS):
        link_id, code = ids.new()
        code = code if alias is None else alias
        link = Link(id=link_id, code=code, target_url=url, expires_at=expires_at)
        if _insert(session, link):
            _write_through(cache, link)
            return link
        if alias is not None and _code_exists(session, alias):
            raise AliasTakenError(alias)
        # The ID or the generated code is already taken. A generated code can
        # clash with an alias someone chose; anything else means trouble (see
        # IdClashError). Either way, a new ID solves it.
        log.warning("new link %d (code %r) clashed with an existing row, retrying", link_id, code)
    raise IdClashError


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
    if result is None:
        # Negative caching: remember "does not exist" briefly, so a flood of
        # requests for made-up codes can't all reach the database.
        cache.set_not_found(code)
    else:
        cache.set(code, result)
    return result, CacheStatus.MISS


def is_expired(link: Link | CachedLink) -> bool:
    return link.expires_at is not None and link.expires_at <= datetime.now(UTC)


def count_click(session: Session, clicks: ClickStream | None, code: str) -> None:
    """Count a click on `code`: on the stream, for the click worker to add up
    later, or, when Redis can't take it, in the database right away."""
    if clicks is not None and clicks.append(code):
        metrics.CLICKS.labels("stream").inc()
    else:
        record_click(session, code)
        metrics.CLICKS.labels("database").inc()


def record_click(session: Session, code: str) -> None:
    # An atomic UPDATE ... SET clicks = clicks + 1 lets the database do the
    # increment, so concurrent clicks never overwrite each other (no lost updates).
    session.execute(update(Link).where(Link.code == code).values(clicks=Link.clicks + 1))
    session.commit()
