import os
from collections.abc import Iterator

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from tests.fakes import SilentServer

# Tests use TEST_DATABASE_URL if set (CI points it at real Postgres),
# otherwise a throwaway SQLite file.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite:///./test.db"
# Likewise, TEST_REDIS_URL selects a real Redis; otherwise an in-memory fake.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")

# The app reads its settings when it is imported, so these imports must come
# after the environment variables above are set (hence noqa: E402).
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.redis_client import RedisGateway, get_redis, make_redis  # noqa: E402


@pytest.fixture
def redis_client() -> Iterator[redis.Redis]:
    """A clean Redis for every test."""
    if TEST_REDIS_URL:
        client = make_redis(TEST_REDIS_URL)
    else:
        client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    client.flushdb()
    yield client
    client.flushdb()


def _client_with(redis_client: redis.Redis | None) -> Iterator[TestClient]:
    """A test client on an empty database, using the given Redis (or none)."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # One gateway (and so one circuit breaker) shared by every request of the test.
    gateway = None if redis_client is None else RedisGateway(redis_client)
    app.dependency_overrides[get_redis] = lambda: gateway
    try:
        with TestClient(app, follow_redirects=False) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(redis_client: redis.Redis) -> Iterator[TestClient]:
    """The normal setup: database + Redis."""
    yield from _client_with(redis_client)


@pytest.fixture
def client_without_redis() -> Iterator[TestClient]:
    """Redis not configured at all (REDIS_URL empty)."""
    yield from _client_with(None)


@pytest.fixture
def client_with_redis_down() -> Iterator[TestClient]:
    """Redis configured, but nothing is listening (port 1 refuses connections).
    Built with make_redis, so it uses the same timeouts and retries as production."""
    yield from _client_with(make_redis("redis://127.0.0.1:1/0"))


@pytest.fixture
def silent_server() -> Iterator[SilentServer]:
    server = SilentServer()
    yield server
    server.close()


@pytest.fixture
def client_with_redis_hung(silent_server: SilentServer) -> Iterator[TestClient]:
    """Redis configured and reachable, but it never answers."""
    client = make_redis(silent_server.url)
    yield from _client_with(client)
    client.close()
