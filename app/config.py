"""App settings, read from environment variables (12-factor style).

Example: DATABASE_URL=postgresql+psycopg://user:pass@db:5432/shortener
"""

import ipaddress
from typing import Final
from urllib.parse import parse_qs, urlsplit

import redis
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Used when CODE_SECRET is not set. It is public (it's in this file), so anyone
# could work out the codes we hand out: fine on your laptop, never in
# production. The app logs a warning while it is in use.
DEFAULT_CODE_SECRET: Final = "public-default-change-me"  # noqa: S105 - public on purpose

# The query arguments of a Redis URL win over the arguments that make_redis
# passes (redis-py's from_url), so a URL could quietly undo what keeps a slow
# or broken Redis from slowing down or breaking the app: how long a call may
# wait, the fixed pool, that a timeout is never retried, and how replies are
# read and decoded (app/redis_client.py). These are the app's to set.
APP_REDIS_OPTIONS: Final = frozenset(
    {
        "socket_timeout",
        "socket_connect_timeout",
        "max_connections",
        "retry",
        "retry_on_timeout",
        "retry_on_error",
        "protocol",
        "legacy_responses",
        "decode_responses",
        "encoding",
        "encoding_errors",
    }
)


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
        # every request later. Nothing here opens a connection.
        if not url:
            return url
        problem = ValueError(
            "not a valid redis://, rediss:// or unix:// URL (percent-encode special"
            " characters in the password, and check the query arguments)"
        )
        try:
            # Parses the URL; raises ValueError, e.g. for a missing redis://.
            pool = redis.ConnectionPool.from_url(url)
            # The pool keeps the query arguments for the connections it makes
            # later. Make one now, which opens no socket: it raises TypeError
            # for an argument it does not take, such as a typo (?dbb=1) or a
            # TLS option on redis:// instead of rediss://, and RedisError for
            # some values it refuses (?protocol=4).
            pool.connection_class(**pool.connection_kwargs)
        except (ValueError, TypeError, redis.RedisError):
            # redis-py's message can quote part of the URL, and so part of
            # the password: in redis://:abc#def@host, the parser stops at
            # the '#' and reports "abc" as a bad port. So we never pass its
            # message on.
            raise problem from None
        if _misread(url):
            raise problem
        overridden = sorted(APP_REDIS_OPTIONS.intersection(parse_qs(urlsplit(url).query)))
        if overridden:
            # Only names from our own list: the message shows nothing of the URL.
            raise ValueError(f"the URL may not set what the app sets: {', '.join(overridden)}")
        return url

    @field_validator("trusted_proxies")
    @classmethod
    def _check_trusted_proxies(cls, value: str) -> str:
        # Likewise: a typo here would otherwise be ignored without a word, and
        # every client would share the proxy's address, and its rate limit.
        # Strict, as uvicorn parses it: "10.89.0.10/24" (an address with a
        # prefix length, as `docker network inspect` prints it) is no network,
        # and uvicorn would keep it as text that no client address matches.
        for entry in _split(value):
            ipaddress.ip_network(entry)  # ValueError if malformed, or host bits set
        return value

    @property
    def trusted_proxy_list(self) -> list[str]:
        return _split(self.trusted_proxies)


def _misread(url: str) -> bool:
    """Whether redis-py would parse the URL, but wrongly. That happens when an
    unencoded '#', '/' or '?' in the password follows a port number, or an '@':
    redis-py then reads only the start of the password, or none of it, and may
    take the rest for the host. The '@' that ends the password lands after the
    host part. So we refuse any '#' (redis-py ignores what follows it, even in
    the query), an '@' in the path, which holds only a db number or a socket
    path, and a query argument without '=', which redis-py drops."""
    parts = urlsplit(url)
    # A socket path may hold an '@', unless user info comes before it.
    socket_path = parts.scheme == "unix" and not parts.netloc
    return (
        "#" in url
        or ("@" in parts.path and not socket_path)
        or any("=" not in field for field in parts.query.split("&") if field)
    )


def _split(value: str) -> list[str]:
    """Split a comma-separated list: "a, b,,c" gives ["a", "b", "c"]."""
    return [part.strip() for part in value.split(",") if part.strip()]


settings = Settings()
