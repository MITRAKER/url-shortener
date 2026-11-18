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
