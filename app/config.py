"""App settings, read from environment variables (12-factor style).

Example: DATABASE_URL=postgresql+psycopg://user:pass@db:5432/shortener
"""

from urllib.parse import urlsplit

import redis
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # hide_input_in_errors: a bad setting stops the app with an error message,
    # and that message must not repeat the value, which may hold a password
    # (redis://:password@host).
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str = "sqlite:///./dev.db"
    # Public base used to build short links, e.g. https://sho.rt
    base_url: str = "http://localhost:8000"
    # Length of randomly generated codes. 62^7 ~= 3.5 trillion possibilities.
    # Must stay within the 3 to 32 characters that redirects accept.
    code_length: int = Field(default=7, ge=3, le=32)

    # --- Step 2: Redis (cache + rate limiting) ---
    # e.g. redis://localhost:6379/0. Leave empty to run without Redis: the
    # app still works, just without caching and without rate limiting.
    redis_url: str = ""
    # How long a link stays cached. Each entry gets a random +/-10% on top, so
    # entries cached at the same moment don't all expire together.
    cache_ttl_seconds: int = Field(default=3600, ge=1)
    # How long we remember that a code does NOT exist ("negative caching").
    cache_negative_ttl_seconds: int = Field(default=60, ge=1)
    # Per-client limits, written as "<count>/<second|minute|hour>".
    rate_limit_create: str = "10/minute"  # POST /api/links
    rate_limit_read: str = "300/minute"  # redirects and stats share this one

    @field_validator("redis_url")
    @classmethod
    def _check_redis_url(cls, url: str) -> str:
        # Parse it now, so a typo stops the app at startup instead of breaking
        # every request later. Building a pool parses the URL but opens no
        # connection; it raises ValueError, e.g. for a missing redis:// scheme.
        if not url:
            return url
        problem = ValueError(
            "not a valid redis://, rediss:// or unix:// URL"
            " (percent-encode special characters in the password)"
        )
        try:
            redis.ConnectionPool.from_url(url)
        except ValueError:
            # redis-py's message can quote part of the URL, and so part of
            # the password: in redis://:abc#def@host, the parser stops at
            # the '#' and reports "abc" as a bad port. So we never pass its
            # message on.
            raise problem from None
        # If the text before that '#' (or '/' or '?') is a number, the URL
        # still parses, as that port and without the password. The '@' that
        # ends the password then sits outside the host part.
        if "@" in url and "@" not in urlsplit(url).netloc:
            raise problem
        return url


settings = Settings()
