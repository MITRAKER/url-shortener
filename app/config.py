"""App settings, read from environment variables (12-factor style).

Example: DATABASE_URL=postgresql+psycopg://user:pass@db:5432/shortener
"""

import redis
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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
        if url:
            redis.ConnectionPool.from_url(url)
        return url


settings = Settings()
