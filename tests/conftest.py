import os
from collections.abc import Iterator

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from app.ids import EPOCH_SECONDS, CodeCipher, LinkIds, Snowflake
from tests.fakes import NGINX, FakeClock, SilentServer

# Tests use TEST_DATABASE_URL if set (CI points it at real Postgres),
# otherwise a throwaway SQLite file.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite:///./test.db"
# Likewise, TEST_REDIS_URL selects a real Redis; otherwise an in-memory fake.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
# As in docker-compose.yml: believe X-Forwarded-For only from Nginx.
os.environ["TRUSTED_PROXIES"] = NGINX
# A key of our own, so the app does not warn about the public default.
TEST_CODE_SECRET = "a-secret-for-the-tests-only"  # noqa: S105 - a test value
os.environ["CODE_SECRET"] = TEST_CODE_SECRET

# The app reads its settings when it is imported, so these imports must come
# after the environment variables above are set (hence noqa: E402).
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app, get_ids  # noqa: E402
from app.redis_client import RedisGateway, get_redis, make_redis  # noqa: E402
from app.worker import ClickWorker  # noqa: E402


def make_ids(instance_id: int = 0) -> LinkIds:
    """IDs on a fake clock. A real Snowflake waits up to a second before its
    first ID (see app/ids.py); this one only pretends to."""
    clock = FakeClock()
    snowflake = Snowflake(
        instance_id, wall=lambda: EPOCH_SECONDS + 1_000.5, monotonic=clock, sleep=clock.sleep
    )
    return LinkIds(snowflake, CodeCipher(TEST_CODE_SECRET))


@pytest.fixture
def redis_client() -> Iterator[redis.Redis]:
    """A clean Redis for every test, read the way the app reads it."""
    if TEST_REDIS_URL:
        client = make_redis(TEST_REDIS_URL)
    else:
        # In memory, but decoding replies as the app's own clients do (make_redis).
        decoding = make_redis("redis://unused/0").get_encoder()
        client = fakeredis.FakeRedis(
            server=fakeredis.FakeServer(),
            decode_responses=decoding.decode_responses,
            encoding_errors=decoding.encoding_errors,
        )
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
    ids = make_ids()
    app.dependency_overrides[get_ids] = lambda: ids
    try:
        with TestClient(app, follow_redirects=False) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def click_worker(redis_client: redis.Redis) -> ClickWorker:
    """The click worker (app/worker.py), on the test Redis and database. It
    doesn't wait for clicks: process_batch() counts what is there already."""
    return ClickWorker(redis_client, SessionLocal, wait_ms=None)


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
