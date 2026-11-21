import logging
from collections.abc import Iterator

import pytest
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.breaker import CircuitBreaker
from app.config import settings
from app.redis_client import (
    MAX_CONNECTIONS,
    SOCKET_TIMEOUT_SECONDS,
    RedisGateway,
    get_redis,
    make_redis,
)

NOTHING_LISTENING = "redis://127.0.0.1:1/0"  # port 1 refuses connections


@pytest.fixture(autouse=True)
def fresh_get_redis() -> Iterator[None]:
    get_redis.cache_clear()  # get_redis remembers its result; start clean
    yield
    get_redis.cache_clear()


def test_no_redis_url_means_no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "redis_url", "")
    assert get_redis() is None


def test_gateway_is_created_once_and_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "redis_url", NOTHING_LISTENING)
    gateway = get_redis()
    assert gateway is not None
    # One connection pool and one circuit breaker for the whole process.
    assert get_redis() is gateway


def test_every_wait_and_the_pool_are_bounded() -> None:
    pool = make_redis(NOTHING_LISTENING).connection_pool
    assert pool.connection_kwargs["socket_connect_timeout"] == SOCKET_TIMEOUT_SECONDS
    assert pool.connection_kwargs["socket_timeout"] == SOCKET_TIMEOUT_SECONDS
    assert pool.max_connections == MAX_CONNECTIONS


def test_only_a_broken_connection_is_retried() -> None:
    """One immediate retry, and only for ConnectionError. A timeout is not
    retried, because the command may already have run (tests/test_cache.py
    shows that a hung Redis sees exactly one connection per call)."""
    connection = make_redis(NOTHING_LISTENING).connection_pool.make_connection()
    assert isinstance(connection, redis.Connection)
    assert connection.retry == Retry(NoBackoff(), 1, supported_errors=(redis.ConnectionError,))


def test_gateway_passes_results_through(redis_client: redis.Redis) -> None:
    redis_client.set("greeting", "hello")
    gateway = RedisGateway(redis_client)
    assert gateway.call("read", lambda r: r.get("greeting"), "fallback") == "hello"


def test_gateway_returns_the_fallback_and_logs_when_redis_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = RedisGateway(make_redis(NOTHING_LISTENING))
    with caplog.at_level(logging.WARNING, logger="app.redis_client"):
        assert gateway.call("cache read", lambda r: r.get("k"), "fallback") == "fallback"
    assert "cache read failed, carrying on without Redis" in caplog.text


def test_gateway_skips_redis_while_the_circuit_is_open(redis_client: redis.Redis) -> None:
    breaker = CircuitBreaker("Redis", failure_threshold=1, reset_after=60)
    gateway = RedisGateway(redis_client, breaker)
    breaker.record_failure()  # opens the circuit
    calls: list[str] = []
    assert gateway.call("read", lambda _: calls.append("called"), "skipped") == "skipped"
    assert calls == []  # the operation never ran


def test_gateway_opens_the_circuit_after_repeated_failures() -> None:
    breaker = CircuitBreaker("Redis", failure_threshold=3, reset_after=60)
    gateway = RedisGateway(make_redis(NOTHING_LISTENING), breaker)
    for _ in range(3):
        gateway.call("read", lambda r: r.get("k"), None)
    assert breaker.is_open


def test_gateway_closes_the_circuit_again_on_success(redis_client: redis.Redis) -> None:
    breaker = CircuitBreaker("Redis", failure_threshold=3, reset_after=60)
    gateway = RedisGateway(redis_client, breaker)
    breaker.record_failure()
    breaker.record_failure()
    gateway.call("read", lambda r: r.get("k"), None)
    breaker.record_failure()
    assert not breaker.is_open  # the success in between reset the count


def test_gateway_does_not_hide_bugs(redis_client: redis.Redis) -> None:
    """Only Redis errors mean "carry on without Redis". A bug in our own code
    must still fail loudly, or we would never find out about it."""
    gateway = RedisGateway(redis_client)
    with pytest.raises(ZeroDivisionError):
        gateway.call("read", lambda _: 1 / 0, None)
    assert not gateway.breaker.is_open
