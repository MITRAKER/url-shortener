"""A cache for short links, stored in Redis.

Pattern: cache-aside (also called lazy loading):
  1. Look in Redis.
  2. On a miss, read Postgres, then copy the answer into Redis for next time.

Redis errors never reach the caller. RedisGateway logs them, a read answers
UNAVAILABLE and a write answers False, so the request carries on with the
database alone: the cache may make us faster, but it must never make us
unavailable.
"""

import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Final

from app.redis_client import RedisGateway

log = logging.getLogger(__name__)

# The "v1" lets us change the stored format later without clashing with old
# entries: new code would read and write "link:v2:..." instead.
KEY_PREFIX: Final = "link:v1:"
# What we store for "this code does not exist".
NOT_FOUND_VALUE: Final = ""


@dataclass(frozen=True)
class CachedLink:
    """Only what a redirect needs. Click counts are NOT cached (they change)."""

    target_url: str
    expires_at: datetime | None


class Lookup(Enum):
    """What LinkCache.get found, when it did not find a link."""

    MISS = "miss"  # Redis has nothing for this code
    NOT_FOUND = "not found"  # Redis remembers that this code does not exist
    UNAVAILABLE = "unavailable"  # Redis is down or skipped, so we know nothing


class LinkCache:
    def __init__(
        self, gateway: RedisGateway, ttl: int, negative_ttl: int, jitter: float = 0.1
    ) -> None:
        if ttl < 1 or negative_ttl < 1:
            raise ValueError("cache TTLs must be at least 1 second")
        if not 0 <= jitter < 1:
            raise ValueError("jitter must be between 0 and 1")
        self.gateway = gateway
        self.ttl = ttl
        self.negative_ttl = negative_ttl
        self.jitter = jitter

    @staticmethod
    def key(code: str) -> str:
        return KEY_PREFIX + code

    def ttl_with_jitter(self) -> int:
        # Not security-sensitive, so the fast `random` module is fine here.
        factor = random.uniform(1 - self.jitter, 1 + self.jitter)  # noqa: S311
        return max(1, round(self.ttl * factor))

    def get(self, code: str) -> CachedLink | Lookup:
        """The cached link, or what we learned instead (see Lookup)."""
        key = self.key(code)
        raw = self.gateway.call("cache read", lambda r: r.get(key), Lookup.UNAVAILABLE)
        if isinstance(raw, Lookup):
            return raw
        if raw is None:
            return Lookup.MISS
        if raw == NOT_FOUND_VALUE:
            return Lookup.NOT_FOUND
        return self._decode(code, raw)

    @staticmethod
    def _decode(code: str, raw: object) -> CachedLink | Lookup:
        try:
            data = json.loads(str(raw))
            exp = data["exp"]
            expires_at = None if exp is None else datetime.fromtimestamp(float(exp), UTC)
            return CachedLink(str(data["url"]), expires_at)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            # Unreadable (e.g. written by a buggy or newer version): treat it as
            # a miss, so the caller reads the database and overwrites the entry.
            log.warning("ignoring unreadable cache entry for %r: %s", code, exc)
            return Lookup.MISS

    def set(self, code: str, link: CachedLink) -> bool:
        """Cache `link`. True if Redis stored it."""
        exp = link.expires_at.timestamp() if link.expires_at else None
        payload = json.dumps({"url": link.target_url, "exp": exp})
        key, ttl = self.key(code), self.ttl_with_jitter()
        return self.gateway.call("cache write", lambda r: bool(r.set(key, payload, ex=ttl)), False)

    def set_not_found(self, code: str) -> bool:
        """Remember that `code` does not exist. True if Redis stored it."""
        # nx=True: only write if the key is empty. If a create() cached the
        # real link a moment ago, "not found" must not overwrite it, and
        # nothing is stored.
        key, ttl = self.key(code), self.negative_ttl
        return self.gateway.call(
            "cache write", lambda r: bool(r.set(key, NOT_FOUND_VALUE, ex=ttl, nx=True)), False
        )
