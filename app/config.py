"""App settings, read from environment variables (12-factor style).

Example: DATABASE_URL=postgresql+psycopg://user:pass@db:5432/shortener
"""

import ipaddress
from typing import Final

import redis
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Used when CODE_SECRET is not set. It is public (it's in this file), so anyone
# could work out the codes we hand out: fine on your laptop, never in
# production. The app logs a warning while it is in use.
DEFAULT_CODE_SECRET: Final = "public-default-change-me"  # noqa: S105 - public on purpose


class Settings(BaseSettings):
    # hide_input_in_errors: a bad setting stops the app with an error message,
    # and that message must not repeat the value, which may hold a password
    # (redis://:password@host).
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str = "sqlite:///./dev.db"
    # Public base used to build short links, e.g. https://sho.rt
    base_url: str = "http://localhost:8000"

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

    # --- Step 3: several instances behind Nginx ---
    # The key that scrambles generated codes (app/ids.py). Every instance must
    # use the same one. Set a long random value in production, for example
    # from: python -c "import secrets; print(secrets.token_urlsafe(32))"
    code_secret: SecretStr = Field(default=SecretStr(DEFAULT_CODE_SECRET), min_length=16)
    # Who may tell us the client's real address in X-Forwarded-For: the IP
    # addresses or networks of our own proxies, comma-separated, such as
    # "10.89.0.10". Empty: trust nobody, right when no proxy is in front.
    trusted_proxies: str = ""

    @field_validator("redis_url")
    @classmethod
    def _check_redis_url(cls, url: str) -> str:
        # Parse it now, so a typo stops the app at startup instead of breaking
        # every request later. Building a pool parses the URL but opens no
        # connection; it raises ValueError, e.g. for a missing redis:// scheme.
        if url:
            redis.ConnectionPool.from_url(url)
        return url

    @field_validator("trusted_proxies")
    @classmethod
    def _check_trusted_proxies(cls, value: str) -> str:
        # Likewise: a typo here would otherwise be ignored without a word, and
        # every client would share the proxy's address, and its rate limit.
        for entry in _split(value):
            ipaddress.ip_network(entry, strict=False)  # ValueError if malformed
        return value

    @property
    def trusted_proxy_list(self) -> list[str]:
        return _split(self.trusted_proxies)


def _split(value: str) -> list[str]:
    """Split a comma-separated list: "a, b,,c" gives ["a", "b", "c"]."""
    return [part.strip() for part in value.split(",") if part.strip()]


settings = Settings()
