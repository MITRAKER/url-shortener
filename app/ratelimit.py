"""Distributed rate limiting: a token bucket stored in Redis.

Every client IP gets a bucket holding up to `limit` tokens. Each request
takes one token, and tokens drip back in at a steady rate (limit / period).
A full bucket allows a short burst; an empty bucket means 429 Too Many Requests.

The buckets live in Redis, not in the app's memory, so every app instance
sees the same bucket. That matters in step 3, when we run three instances.
"""

import re
from dataclasses import dataclass
from typing import Annotated, Final

import redis
from fastapi import Depends, HTTPException, Request, status

from app import metrics
from app.redis_client import RedisGateway, get_redis

# This script runs inside Redis, atomically: no other command can run between
# its read and its write, so two app instances can never both take the last token.
RATE_LIMIT_LUA: Final = """
local capacity = tonumber(ARGV[1])  -- max tokens = the largest allowed burst
local rate     = tonumber(ARGV[2])  -- tokens added back per second
local cost     = tonumber(ARGV[3])  -- tokens this request needs

-- Use the Redis server's clock, not the app server's. App servers' clocks can
-- disagree with each other, but there is only one Redis.
local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(bucket[1]) or capacity  -- a new client starts full
local ts     = tonumber(bucket[2]) or now

-- Add the tokens earned since the last request, but never above capacity.
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)

local allowed, retry_after = 0, 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_after = math.ceil((cost - tokens) / rate)  -- seconds until enough tokens
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
-- Delete idle buckets: by then they would be full again anyway.
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / rate) + 1)

-- Numbers returned from Lua are cut to integers, so round on purpose.
return {allowed, math.floor(tokens), retry_after}
"""

_PERIODS: Final = {"second": 1, "minute": 60, "hour": 3600}
_RATE_PATTERN: Final = re.compile(r"\s*(\d{1,6})\s*/\s*(second|minute|hour)\s*")


@dataclass(frozen=True)
class Rate:
    limit: int  # bucket size: the most requests allowed in one burst
    period_seconds: int  # time to refill an empty bucket completely

    def __post_init__(self) -> None:
        if self.limit < 1 or self.period_seconds < 1:
            raise ValueError("limit and period_seconds must be at least 1")

    @property
    def per_second(self) -> float:
        return self.limit / self.period_seconds

    @classmethod
    def parse(cls, text: str) -> "Rate":
        """'10/minute' -> Rate(limit=10, period_seconds=60)"""
        match = _RATE_PATTERN.fullmatch(text)
        if match is None:
            raise ValueError(f"invalid rate {text!r}, expected something like '10/minute'")
        return cls(int(match[1]), _PERIODS[match[2]])


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int  # whole tokens left after this request
    retry_after: int  # seconds to wait when not allowed


class TokenBucket:
    def __init__(self, client: redis.Redis) -> None:
        # register_script sends the script's SHA1 (EVALSHA) instead of the
        # whole script, and uploads the script only if Redis doesn't know it yet.
        self._script = client.register_script(RATE_LIMIT_LUA)

    def take(self, key: str, rate: Rate, cost: int = 1) -> Decision:
        if cost < 1:
            raise ValueError("cost must be at least 1")
        allowed, remaining, retry_after = self._script(
            keys=[key], args=[rate.limit, rate.per_second, cost]
        )
        return Decision(bool(allowed), int(remaining), int(retry_after))


class RateLimit:
    """A FastAPI dependency: `dependencies=[Depends(RateLimit("create", "10/minute"))]`.

    Routes that use the same name share one bucket per client.
    """

    def __init__(self, name: str, rate: str) -> None:
        self.name = name
        self.rate = Rate.parse(rate)  # a bad setting fails at startup, not later
        metrics.RATE_LIMITED.labels(name)  # 0 until the first refusal (app/metrics.py)

    def __call__(
        self, request: Request, gateway: Annotated[RedisGateway | None, Depends(get_redis)]
    ) -> None:
        if gateway is None:
            return  # Redis not configured: rate limiting is off
        # Behind a proxy (step 3) this would be the proxy's address for everyone.
        ip = request.client.host if request.client else "unknown"
        key = f"ratelimit:{self.name}:{ip}"
        decision = gateway.call("rate limiter", lambda r: TokenBucket(r).take(key, self.rate), None)
        # Fail open: if Redis is unavailable (no decision), we prefer serving
        # without limits over refusing everyone.
        if decision is not None and not decision.allowed:
            metrics.RATE_LIMITED.labels(self.name).inc()
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests, please slow down",
                headers={
                    "Retry-After": str(decision.retry_after),
                    "X-RateLimit-Limit": str(self.rate.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )
