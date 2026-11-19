import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize(
    "url",
    [
        "",  # no Redis: the app runs without cache and rate limits
        "redis://localhost:6379/0",
        "rediss://user:secret@cache.example.com:6380/1",  # TLS
        "unix:///run/redis/redis.sock",
    ],
)
def test_valid_redis_urls_are_accepted(url: str) -> None:
    assert Settings(redis_url=url).redis_url == url


@pytest.mark.parametrize(
    "url",
    [
        "localhost:6379",  # no scheme
        "redis//redis:6379/0",  # missing colon
        "http://redis:6379/0",  # wrong scheme
        "redis://redis:port/0",  # port is not a number
    ],
)
def test_malformed_redis_url_is_rejected(url: str) -> None:
    with pytest.raises(ValidationError, match="redis_url"):
        Settings(redis_url=url)


def test_malformed_redis_url_stops_the_app_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings are read when the app starts, so a typo in the environment
    fails loudly there, before the app reports itself healthy. It must never
    reach the first request, where it would turn every response into a 500."""
    monkeypatch.setenv("REDIS_URL", "localhost:6379")
    with pytest.raises(ValidationError, match="redis_url"):
        Settings()


@pytest.mark.parametrize(
    "url",
    [
        "redis//:hunter2@redis:6379/0",  # missing colon
        "http://:hunter2@redis:6379/0",  # wrong scheme
        "redis://:hunter2@redis:port/0",  # port is not a number
        # A '#', '/' or '?' that is not percent-encoded: the parser stops
        # there and reads the start of the password as the port.
        "redis://:hun#ter2@redis:6379/0",
        "redis://:hun/ter2@redis:6379/0",
        "redis://:hun?ter2@redis:6379/0",
        # A full-width '#': Python's URL parser quotes all of ":password@host:port".
        "redis://:hun\uff03ter2@redis:6379/0",
    ],
)
def test_error_for_a_malformed_redis_url_does_not_show_the_password(url: str) -> None:
    """The error is printed when the app stops, so it ends up in the logs.
    Not even a part of the password may appear in it."""
    with pytest.raises(ValidationError, match="redis_url") as error:
        Settings(redis_url=url)
    assert "hun" not in str(error.value)
    assert "ter2" not in str(error.value)
