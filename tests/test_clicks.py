"""Counting clicks off the redirect path (app/clicks.py): a redirect appends
the click to a Redis stream, and only falls back to PostgreSQL when Redis
can't take it."""

import pytest
import redis
from fastapi.testclient import TestClient

from app.clicks import STREAM
from app.worker import ClickWorker


def create(client: TestClient) -> str:
    code: str = client.post("/api/links", json={"url": "https://example.com/"}).json()["code"]
    return code


def clicks(client: TestClient, code: str) -> int:
    count: int = client.get(f"/api/links/{code}").json()["clicks"]
    return count


def test_a_click_goes_to_the_stream_not_the_database(
    client: TestClient, redis_client: redis.Redis
) -> None:
    code = create(client)
    assert client.get(f"/{code}").status_code == 302
    entries = redis_client.xrange(STREAM)
    assert entries is not None
    assert [fields for _, fields in entries] == [{"code": code}]
    assert clicks(client, code) == 0  # not counted yet: that's the worker's job


def test_a_head_request_is_not_a_click(client: TestClient, redis_client: redis.Redis) -> None:
    code = create(client)
    assert client.head(f"/{code}").status_code == 302
    assert redis_client.xlen(STREAM) == 0


def test_the_worker_counts_what_the_redirects_recorded(
    client: TestClient, click_worker: ClickWorker
) -> None:
    first, second = create(client), create(client)
    for code in [first, second, first, first, second]:
        client.get(f"/{code}")
    assert click_worker.process_batch() == 5
    assert (clicks(client, first), clicks(client, second)) == (3, 2)


def test_without_redis_a_click_is_counted_at_once(client_without_redis: TestClient) -> None:
    code = create(client_without_redis)
    client_without_redis.get(f"/{code}")
    assert clicks(client_without_redis, code) == 1


def test_when_redis_is_full_a_click_is_counted_at_once(
    client: TestClient, redis_client: redis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis refuses writes when its memory is full of keys it may not evict,
    such as the stream itself. The click is then counted the old way."""

    def full(*_args: object, **_kwargs: object) -> None:
        raise redis.exceptions.OutOfMemoryError(
            "command not allowed when used memory > 'maxmemory'"
        )

    code = create(client)
    monkeypatch.setattr(redis_client, "xadd", full)
    assert client.get(f"/{code}").status_code == 302
    assert clicks(client, code) == 1
