"""The one door to Redis.

A `redis.Redis` object owns a connection pool, so we create it once and reuse
it for every request (like the SQLAlchemy engine in db.py). App code never
calls it directly: every call goes through `RedisGateway.call`, the single
place that decides what happens when Redis is slow or down. It skips Redis
while the circuit breaker is open, logs errors, and returns a fallback value,
so the request carries on without Redis.
"""

import logging
from collections.abc import Callable
from functools import lru_cache

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.breaker import CircuitBreaker
from app.config import settings

log = logging.getLogger(__name__)

# Every wait has an upper bound (NASA/JPL rule 2). The cache sits on the
# redirect path, so one Redis call may wait at most this long.
SOCKET_TIMEOUT_SECONDS = 0.25
# A fixed maximum number of connections (NASA/JPL rule 3). FastAPI runs sync
# endpoints on at most 40 threads, and each uses one connection at a time.
MAX_CONNECTIONS = 50
# A redirect makes up to three Redis calls. If Redis hangs, each would wait for
# the timeout, so after 3 failures in a row we stop calling Redis for 5 seconds.
BREAKER_FAILURE_THRESHOLD = 3
BREAKER_RESET_SECONDS = 5.0


def make_redis(url: str) -> redis.Redis:
    return redis.Redis.from_url(
        url,
        decode_responses=True,  # give us str instead of bytes
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        max_connections=MAX_CONNECTIONS,
        # Retry once, at once, but only if the connection itself broke, e.g. a
        # pooled connection that Redis closed while it sat idle. Never retry a
        # timeout: the command may already have run, and running the rate
        # limiter's script twice would charge the client twice.
        retry=Retry(NoBackoff(), retries=1, supported_errors=(redis.ConnectionError,)),
    )


class RedisGateway:
    def __init__(self, client: redis.Redis, breaker: CircuitBreaker | None = None) -> None:
        self.client = client
        self.breaker = breaker or CircuitBreaker(
            "Redis", BREAKER_FAILURE_THRESHOLD, BREAKER_RESET_SECONDS
        )

    def call[T, F](self, what: str, operation: Callable[[redis.Redis], T], fallback: F) -> T | F:
        """Run `operation` against Redis, or return `fallback` if Redis is unavailable."""
        if not self.breaker.allow():
            return fallback  # the circuit is open: don't even try
        try:
            result = operation(self.client)
        except redis.RedisError as exc:
            log.warning("%s failed, carrying on without Redis: %s", what, exc)
            self.breaker.record_failure()  # logs once more if this opens the circuit
            return fallback
        self.breaker.record_success()
        return result


@lru_cache(maxsize=1)
def get_redis() -> RedisGateway | None:
    """FastAPI dependency. None means Redis is not configured."""
    if not settings.redis_url:
        return None
    return RedisGateway(make_redis(settings.redis_url))
